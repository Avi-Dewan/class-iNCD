import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.optim import SGD, lr_scheduler
from sklearn.metrics.cluster import normalized_mutual_info_score as nmi_score
from sklearn.metrics import adjusted_rand_score as ari_score
from sklearn.cluster import KMeans
from utils.util import BCE, PairEnum, cluster_acc, Identity, AverageMeter, seed_torch, CentroidTracker
from utils import ramps
from utils.logging import Logger
from models.resnet import ResNet, BasicBlock 
from data.cifarloader import CIFAR10Loader, CIFAR10LoaderMix, CIFAR100Loader, CIFAR100LoaderMix
from data.tinyimagenetloader import TinyImageNetLoader
from data.svhnloader import SVHNLoader, SVHNLoaderMix
from tqdm import tqdm
import numpy as np
import os
import sys
import copy
import wandb
from collections.abc import Iterable


def train_AutoNovel(model, old_model, train_loader, labeled_eval_loader, unlabeled_eval_loader, all_eval_loader, args):
    print("=" * 100)
    print("\t\t\t\tCiao bella! I am AutoNovel!")
    print("=" * 100)

    optimizer = SGD(filter(lambda p: p.requires_grad, model.parameters()),
                         lr=args.lr, momentum=args.momentum, weight_decay=args.weight_decay)
    exp_lr_scheduler = lr_scheduler.StepLR(optimizer, step_size=args.step_size, gamma=args.gamma)
    criterion1 = nn.CrossEntropyLoss()
    criterion2 = BCE()

    for epoch in range(args.epochs):
        # create loss statistics recorder for each loss
        loss_bce_record = AverageMeter()            # BCE loss recorder
        consistency_loss_record = AverageMeter()    # MSE consistency loss recorder

        # turn on the training mode of the model
        model.train()
        exp_lr_scheduler.step()
        w = args.rampup_coefficient * ramps.sigmoid_rampup(epoch, args.rampup_length)

        for batch_idx, ((x, x_bar), label, idx) in enumerate(tqdm(train_loader)):
            # send the vars to GPU
            x, x_bar, label = x.to(device), x_bar.to(device), label.to(device)
            # create a mask for labeled data
            mask_lb = label < args.num_labeled_classes

            # filter out the labeled entries for x, x_bar, label
            x = x[~mask_lb]
            x_bar = x_bar[~mask_lb]
            label = label[~mask_lb]

            # normalize the prototypes
            if args.l2_classifier:
                model.l2_classifier = True
                with torch.no_grad():
                    w_head = model.head1.weight.data.clone()
                    w_head = F.normalize(w_head, dim=1, p=2)
                    model.head1.weight.copy_(w_head)
                    # if epoch == 5 and w_head_fix is None:
                    #     w_head_fix = w_head[:args.num_labeled_classes, :]
            else:
                model.l2_classifier = False

            output1, output2, feat = model(x)                   # outputs of x from head-1 and head-2 and extracted feat
            output1_bar, output2_bar, feat_bar = model(x_bar)   # outputs of x_bar from head-1 and head-2 and extracted feat

            # use softmax to get the probability distribution for each head
            prob1, prob1_bar = F.softmax(output1, dim=1), F.softmax(output1_bar, dim=1)
            prob2, prob2_bar = F.softmax(output2, dim=1), F.softmax(output2_bar, dim=1)

            # calculate rank statistics
            rank_feat = (feat).detach()

            rank_idx = torch.argsort(rank_feat, dim=1, descending=True)
            rank_idx1, rank_idx2 = PairEnum(rank_idx)
            rank_idx1, rank_idx2 = rank_idx1[:, :args.topk], rank_idx2[:, :args.topk]

            rank_idx1, _ = torch.sort(rank_idx1, dim=1)
            rank_idx2, _ = torch.sort(rank_idx2, dim=1)

            rank_diff = rank_idx1 - rank_idx2
            rank_diff = torch.sum(torch.abs(rank_diff), dim=1)
            target_ulb = torch.ones_like(rank_diff).float().to(device)
            target_ulb[rank_diff > 0] = -1

            # get the probability distribution of the prediction for head-2
            prob1_ulb, _ = PairEnum(prob2)
            _, prob2_ulb = PairEnum(prob2_bar)

            # get the pseudo label from head-2
            loss_bce = criterion2(prob1_ulb, prob2_ulb, target_ulb)
            consistency_loss = F.mse_loss(prob2, prob2_bar)         # + F.mse_loss(prob1, prob1_bar)

            # record the losses
            loss_bce_record.update(loss_bce.item(), prob1_ulb.size(0))
            consistency_loss_record.update(consistency_loss.item(), prob2.size(0))

            loss = loss_bce + w * consistency_loss

            optimizer.zero_grad()
            loss.backward()
            optimizer.step()

        # wandb loss logging
        # wandb.log({
        #            "loss/bce": loss_bce_record.avg,
        #            "loss/consistency": consistency_loss_record.avg,
        #            }, step=epoch)

        print('Head2: test on unlabeled classes')
        args.head = 'head2'
        acc_head2_ul, ind = test(model, unlabeled_eval_loader, args, return_ind=True)

        print('Head1: test on labeled classes')
        args.head = 'head1'
        acc_head1_lb = test(model, labeled_eval_loader, args, cluster=False)

        print('Head1: test on unlabeled classes')
        acc_head1_ul = test(model, unlabeled_eval_loader, args, cluster=False, ind=ind)

        print('Head1: test on all classes w/o clustering')
        acc_head1_all_wo_cluster = test(model, all_eval_loader, args, cluster=False, ind=ind)

        print('Head1: test on all classes w/ clustering')
        acc_head1_all_w_cluster = test(model, all_eval_loader, args, cluster=True)

        # wandb metrics logging
        # wandb.log({
        #     "val_acc/head2_ul": acc_head2_ul,
        #     "val_acc/head1_lb": acc_head1_lb,
        #     "val_acc/head1_ul": acc_head1_ul,
        #     "val_acc/head1_all_wo_clutering": acc_head1_all_wo_cluster,
        #     "val_acc/head1_all_w_clustering": acc_head1_all_w_cluster
        # }, step=epoch)

        # wandb.log({
        #     "misc/w_ramp": w
        # }, step=epoch)


def train_OGwoPseudo(model, old_model, train_loader, labeled_eval_loader, unlabeled_eval_loader, all_eval_loader,
                    class_mean, class_sig, class_cov, args):
    print("=" * 100)
    print("\t\t\t\tCiao bella! I am {} model!".format(args.IL_version))
    print("=" * 100)

    optimizer = SGD(model.parameters(), lr=args.lr, momentum=args.momentum,
                    weight_decay=args.weight_decay)
    exp_lr_scheduler = lr_scheduler.StepLR(optimizer, step_size=args.step_size, gamma=args.gamma)
    criterion1 = nn.CrossEntropyLoss()  # CE loss for labeled data
    criterion2 = BCE()                  # BCE loss for unlabeled data

    for epoch in range(args.epochs):
        # create loss statistics recorder for each loss
        loss_kd_record = AverageMeter()             # KD loss recorder
        loss_record = AverageMeter()                # Total loss recorder
        # loss_ce_add_record = AverageMeter()       # CE loss recorder
        loss_bce_record = AverageMeter()            # BCE loss recorder
        consistency_loss_record = AverageMeter()    # MSE consistency loss recorder

        # turn on the training mode of the model
        model.train()
        # model.apply(freeze_bn) # freeze

        # update LR scheduler for the current epoch
        exp_lr_scheduler.step()
        # update ramp-up coefficient for the current epoch
        w = args.rampup_coefficient * ramps.sigmoid_rampup(epoch, args.rampup_length)

        for batch_idx, ((x, x_bar), label, idx) in enumerate(tqdm(train_loader)):
            x, x_bar, label = x.to(device), x_bar.to(device), label.to(device)
            # create a mask for labeled data
            mask_lb = label < args.num_labeled_classes

            # filter out the labeled entries for x, x_bar, label
            x = x[~mask_lb]
            x_bar = x_bar[~mask_lb]
            label = label[~mask_lb]

            # normalize the prototypes
            if args.l2_classifier:
                model.l2_classifier = True
                with torch.no_grad():
                    w_head = model.head1.weight.data.clone()
                    w_head = F.normalize(w_head, dim=1, p=2)
                    model.head1.weight.copy_(w_head)
                    # if epoch == 5 and w_head_fix is None:
                    #     w_head_fix = w_head[:args.num_labeled_classes, :]
            else:
                model.l2_classifier = False

            output1, output2, feat = model(x)                   # outputs of x from head-1 and head-2 and extracted feat
            output1_bar, output2_bar, feat_bar = model(x_bar)   # outputs of x_bar from head-1 and head-2 and extracted feat

            # use softmax to get the probability distribution for each head
            prob1, prob1_bar = F.softmax(output1, dim=1), F.softmax(output1_bar, dim=1)
            prob2, prob2_bar = F.softmax(output2, dim=1), F.softmax(output2_bar, dim=1)

            # calculate rank statistics
            rank_feat = (feat).detach()

            rank_idx = torch.argsort(rank_feat, dim=1, descending=True)
            rank_idx1, rank_idx2 = PairEnum(rank_idx)
            rank_idx1, rank_idx2 = rank_idx1[:, :args.topk], rank_idx2[:, :args.topk]

            rank_idx1, _ = torch.sort(rank_idx1, dim=1)
            rank_idx2, _ = torch.sort(rank_idx2, dim=1)

            rank_diff = rank_idx1 - rank_idx2
            rank_diff = torch.sum(torch.abs(rank_diff), dim=1)
            target_ulb = torch.ones_like(rank_diff).float().to(device)
            target_ulb[rank_diff > 0] = -1

            # get the probability distribution of the prediction for head-2
            prob1_ulb, _ = PairEnum(prob2)
            _, prob2_ulb = PairEnum(prob2_bar)

            loss_bce = criterion2(prob1_ulb, prob2_ulb, target_ulb)
            consistency_loss = F.mse_loss(prob2, prob2_bar)  # + F.mse_loss(prob1, prob1_bar)

            # record the losses
            loss_bce_record.update(loss_bce.item(), prob1_ulb.size(0))
            consistency_loss_record.update(consistency_loss.item(), prob2.size(0))

            if args.labeled_center > 0:
                labeled_feats, labeled_labels = sample_labeled_features(class_mean, class_sig, args)
                labeled_output1 = model.forward_feat(labeled_feats)
                loss_ce_la = args.lambda_proto * criterion1(labeled_output1, labeled_labels)
            else:
                loss_ce_la = 0

            if args.w_kd > 0:
                _, _, old_feat = old_model(x)
                size_1, size_2 = old_feat.size()
                loss_kd = torch.dist(F.normalize(old_feat.view(size_1 * size_2, 1), dim=0),
                                     F.normalize(feat.view(size_1 * size_2, 1), dim=0)) * args.w_kd
            else:
                loss_kd = torch.tensor(0.0)

            # record losses
            loss_kd_record.update(loss_kd.item(), x.size(0))

            loss = loss_bce + w * consistency_loss + loss_ce_la + loss_kd

            if args.labeled_center > 0 and isinstance(loss_ce_la, torch.Tensor):
                loss_record.update(loss_ce_la.item(), x.size(0))

            # zero gradients
            optimizer.zero_grad()
            loss.backward()
            # update the parameters
            optimizer.step()

        # wandb loss logging
        # wandb.log({#"loss/pseudo-unlab": loss_ce_add_record.avg,
        #            "loss/bce": loss_bce_record.avg,
        #            "loss/consistency": consistency_loss_record.avg,
        #            "loss/proto_lab": loss_record.avg,
        #            "loss/kd": loss_kd_record.avg
        #            }, step=epoch)

        print('Train Epoch: {} Avg Loss: {:.4f}'.format(epoch, loss_record.avg))

        print('Head2: test on unlabeled classes')
        args.head = 'head2'
        acc_head2_ul, ind = test(model, unlabeled_eval_loader, args, return_ind=True)

        print('Head1: test on labeled classes')
        args.head = 'head1'
        acc_head1_lb = test(model, labeled_eval_loader, args, cluster=False)

        print('Head1: test on unlabeled classes')
        acc_head1_ul = test(model, unlabeled_eval_loader, args, cluster=False, ind=ind)

        print('Head1: test on all classes w/o clustering')
        acc_head1_all_wo_cluster = test(model, all_eval_loader, args, cluster=False, ind=ind)

        print('Head1: test on all classes w/ clustering')
        acc_head1_all_w_cluster = test(model, all_eval_loader, args, cluster=True)

        # wandb metrics logging
        # wandb.log({
        #     "val_acc/head2_ul": acc_head2_ul,
        #     "val_acc/head1_lb": acc_head1_lb,
        #     "val_acc/head1_ul": acc_head1_ul,
        #     "val_acc/head1_all_wo_clutering": acc_head1_all_wo_cluster,
        #     "val_acc/head1_all_w_clustering": acc_head1_all_w_cluster
        # }, step=epoch)

        # wandb.log({
        #     "misc/w_ramp": w
        # }, step=epoch)


def train_LwF_Proto(model, old_model, train_loader, labeled_eval_loader, unlabeled_eval_loader, all_eval_loader,
                    class_mean, class_sig, class_cov, args):
    print("=" * 100)
    print("\t\t\t\tCiao bella! I am LwF+Prototyping! I don't have Feat-KD")
    print("=" * 100)

    optimizer = SGD(model.parameters(), lr=args.lr, momentum=args.momentum, weight_decay=args.weight_decay)
    exp_lr_scheduler = lr_scheduler.StepLR(optimizer, step_size=args.step_size, gamma=args.gamma)
    criterion1 = nn.CrossEntropyLoss()
    criterion2 = BCE()

    for epoch in range(args.epochs):
        # create loss statistics recorder for each loss
        loss_record = AverageMeter()                # Total loss recorder
        loss_ce_add_record = AverageMeter()         # CE loss recorder
        loss_bce_record = AverageMeter()            # BCE loss recorder
        consistency_loss_record = AverageMeter()    # MSE consistency loss recorder
        loss_kd_record = AverageMeter()             # KD loss recorder

        # turn on the training mode of the model
        model.train()

        # update LR scheduler for the current epoch
        exp_lr_scheduler.step()
        # update ramp-up coefficient for the current epoch
        w = args.rampup_coefficient * ramps.sigmoid_rampup(epoch, args.rampup_length)

        for batch_idx, ((x, x_bar), label, idx) in enumerate(tqdm(train_loader)):
            # send the vars to GPU
            x, x_bar, label = x.to(device), x_bar.to(device), label.to(device)
            # create a mask for labeled data
            mask_lb = label < args.num_labeled_classes

            # filter out the labeled entries for x, x_bar, label
            x = x[~mask_lb]
            x_bar = x_bar[~mask_lb]
            label = label[~mask_lb]

            # normalize the prototypes
            if args.l2_classifier:
                model.l2_classifier = True
                with torch.no_grad():
                    w_head = model.head1.weight.data.clone()
                    w_head = F.normalize(w_head, dim=1, p=2)
                    model.head1.weight.copy_(w_head)
            else:
                model.l2_classifier = False

            output1, output2, feat = model(x)
            output1_bar, output2_bar, feat_bar = model(x_bar)

            # use softmax to get the probability distribution for each head
            prob1, prob1_bar = F.softmax(output1, dim=1), F.softmax(output1_bar, dim=1)
            prob2, prob2_bar = F.softmax(output2, dim=1), F.softmax(output2_bar, dim=1)

            # calculate rank statistics
            rank_feat = (feat).detach()

            rank_idx = torch.argsort(rank_feat, dim=1, descending=True)
            rank_idx1, rank_idx2 = PairEnum(rank_idx)
            rank_idx1, rank_idx2 = rank_idx1[:, :args.topk], rank_idx2[:, :args.topk]

            rank_idx1, _ = torch.sort(rank_idx1, dim=1)
            rank_idx2, _ = torch.sort(rank_idx2, dim=1)

            rank_diff = rank_idx1 - rank_idx2
            rank_diff = torch.sum(torch.abs(rank_diff), dim=1)
            target_ulb = torch.ones_like(rank_diff).float().to(device)
            target_ulb[rank_diff > 0] = -1

            # get the probability distribution of the prediction for head-2
            prob1_ulb, _ = PairEnum(prob2)
            _, prob2_ulb = PairEnum(prob2_bar)

            # get the pseudo label from head-2
            label = (output2).detach().max(1)[1] + args.num_labeled_classes

            loss_ce_add = w * criterion1(output1, label) / args.rampup_coefficient * args.increment_coefficient
            loss_bce = criterion2(prob1_ulb, prob2_ulb, target_ulb)
            consistency_loss = F.mse_loss(prob2, prob2_bar)  # + F.mse_loss(prob1, prob1_bar)

            # record the losses
            loss_ce_add_record.update(loss_ce_add.item(), output1.size(0))
            loss_bce_record.update(loss_bce.item(), prob1_ulb.size(0))
            consistency_loss_record.update(consistency_loss.item(), prob2.size(0))

            if args.labeled_center > 0:
                labeled_feats, labeled_labels = sample_labeled_features(class_mean, class_sig, args)
                labeled_output1 = model.forward_feat(labeled_feats)
                loss_ce_la = args.lambda_proto * criterion1(labeled_output1, labeled_labels)
            else:
                loss_ce_la = 0

            if args.w_kd > 0:
                # old_output1, _, _ = old_model(x)
                # size_1, size_2 = old_output1.size()
                # loss_kd = torch.dist(F.normalize(old_output1.view(size_1 * size_2, 1), dim=0),
                #                      F.normalize(output1[:, :size_2].reshape(size_1 * size_2, 1), dim=0)) * args.w_kd
                # # LwF loss
                # old_output1, _, _ = old_model(x)
                # size_1, size_2 = old_output1.size()
                # soft_old_output1 = F.softmax(old_output1 / 2, dim=1)
                # soft_new_output1 = F.softmax(output1[:, :size_2] / 2, dim=1)
                # loss_kd = soft_old_output1.mul(-1 * torch.log(soft_new_output1))
                # loss_kd = loss_kd.sum(1)
                # loss_kd = loss_kd.mean() * 2 * 2 * args.w_kd

                # LwF simpler
                old_output1, _, _ = old_model(x)
                size_1, size_2 = old_output1.size()
                soft_old_output1 = F.softmax(old_output1 / 2, dim=1)
                log_soft_new_output1 = F.log_softmax(output1[:, :size_2] / 2, dim=1)
                loss_kd = -torch.mean(torch.sum(soft_old_output1*log_soft_new_output1, dim=1)) * args.w_kd
            else:
                loss_kd = torch.tensor(0.0)

            # record losses
            loss_kd_record.update(loss_kd.item(), x.size(0))

            loss = loss_bce + loss_ce_add + w * consistency_loss + loss_kd + loss_ce_la

            if args.labeled_center > 0 and isinstance(loss_ce_la, torch.Tensor):
                loss_record.update(loss_ce_la.item(), x.size(0))
            # zero gradients
            optimizer.zero_grad()
            # calculate gradients w.r.t. parameters of Backbone + head-2 (head-1 is frozen)
            loss.backward()
            # update the parameters
            optimizer.step()

        # wandb loss logging
        # wandb.log({"loss/pseudo-unlab": loss_ce_add_record.avg,
        #            "loss/bce": loss_bce_record.avg,
        #            "loss/consistency": consistency_loss_record.avg,
        #            "loss/proto_lab": loss_record.avg,
        #            "loss/LwF_logits_kd": loss_kd_record.avg
        #            }, step=epoch)

        print('Head2: test on unlabeled classes')
        args.head = 'head2'
        acc_head2_ul, ind = test(model, unlabeled_eval_loader, args, return_ind=True)

        print('Head1: test on labeled classes')
        args.head = 'head1'
        acc_head1_lb = test(model, labeled_eval_loader, args, cluster=False)

        print('Head1: test on unlabeled classes')
        acc_head1_ul = test(model, unlabeled_eval_loader, args, cluster=False, ind=ind)

        print('Head1: test on all classes w/o clustering')
        acc_head1_all_wo_cluster = test(model, all_eval_loader, args, cluster=False, ind=ind)

        print('Head1: test on all classes w/ clustering')
        acc_head1_all_w_cluster = test(model, all_eval_loader, args, cluster=True)

        # wandb metrics logging
        # wandb.log({
        #     "val_acc/head2_ul": acc_head2_ul,
        #     "val_acc/head1_lb": acc_head1_lb,
        #     "val_acc/head1_ul": acc_head1_ul,
        #     "val_acc/head1_all_wo_clutering": acc_head1_all_wo_cluster,
        #     "val_acc/head1_all_w_clustering": acc_head1_all_w_cluster
        # }, step=epoch)

        # wandb.log({
        #     "misc/w_ramp": w
        # }, step=epoch)



def train_SplitHead12(model, old_model, train_loader, labeled_eval_loader, unlabeled_eval_loader, all_eval_loader,
                      class_mean, class_sig, class_cov, args):
    print("=" * 100)
    print("\t\t\t\tCiao bella! I am Splited Head1 and Head2. I need to be manually concatenated! I don't have pseudo label loss.")
    print("=" * 100)

    optimizer = SGD(model.parameters(), lr=args.lr, momentum=args.momentum, weight_decay=args.weight_decay)
    exp_lr_scheduler = lr_scheduler.StepLR(optimizer, step_size=args.step_size, gamma=args.gamma)
    criterion1 = nn.CrossEntropyLoss()  # CE loss for labeled data
    criterion2 = BCE()                  # BCE loss for unlabeled data

    for epoch in range(args.epochs):
        # create loss statistics recorder for each loss
        loss_record = AverageMeter()                # Total loss recorder
        # loss_ce_add_record = AverageMeter()       # CE loss recorder
        loss_bce_record = AverageMeter()            # BCE loss recorder
        consistency_loss_record = AverageMeter()    # MSE consistency loss recorder
        loss_kd_record = AverageMeter()             # KD loss recorder

        # turn on the training mode of the model
        model.train()

        # update LR scheduler for the current epoch
        exp_lr_scheduler.step()
        # update ramp-up coefficient for the current epoch
        w = args.rampup_coefficient * ramps.sigmoid_rampup(epoch, args.rampup_length)

        for batch_idx, ((x, x_bar), label, idx) in enumerate(tqdm(train_loader)):
            # send the vars to GPU
            x, x_bar, label = x.to(device), x_bar.to(device), label.to(device)
            # create a mask for labeled data
            mask_lb = label < args.num_labeled_classes

            # filter out the labeled entries for x, x_bar, label
            x = x[~mask_lb]
            x_bar = x_bar[~mask_lb]
            label = label[~mask_lb]

            # normalize the prototypes
            if args.l2_classifier:
                model.l2_classifier = True
                with torch.no_grad():
                    w_head = model.head1.weight.data.clone()
                    w_head = F.normalize(w_head, dim=1, p=2)
                    model.head1.weight.copy_(w_head)
            else:
                model.l2_classifier = False

            output1, output2, feat = model(x)
            output1_bar, output2_bar, feat_bar = model(x_bar)

            # use softmax to get the probability distribution for each head
            prob1, prob1_bar = F.softmax(output1, dim=1), F.softmax(output1_bar, dim=1)
            prob2, prob2_bar = F.softmax(output2, dim=1), F.softmax(output2_bar, dim=1)

            # calculate rank statistics
            rank_feat = (feat).detach()

            rank_idx = torch.argsort(rank_feat, dim=1, descending=True)
            rank_idx1, rank_idx2 = PairEnum(rank_idx)
            rank_idx1, rank_idx2 = rank_idx1[:, :args.topk], rank_idx2[:, :args.topk]

            rank_idx1, _ = torch.sort(rank_idx1, dim=1)
            rank_idx2, _ = torch.sort(rank_idx2, dim=1)

            rank_diff = rank_idx1 - rank_idx2
            rank_diff = torch.sum(torch.abs(rank_diff), dim=1)
            target_ulb = torch.ones_like(rank_diff).float().to(device)
            target_ulb[rank_diff > 0] = -1

            # get the probability distribution of the prediction for head-2
            prob1_ulb, _ = PairEnum(prob2)
            _, prob2_ulb = PairEnum(prob2_bar)

            loss_bce = criterion2(prob1_ulb, prob2_ulb, target_ulb)
            consistency_loss = F.mse_loss(prob2, prob2_bar)  # + F.mse_loss(prob1, prob1_bar)

            # record the losses
            loss_bce_record.update(loss_bce.item(), prob1_ulb.size(0))
            consistency_loss_record.update(consistency_loss.item(), prob2.size(0))

            if args.labeled_center > 0:
                labeled_feats, labeled_labels = sample_labeled_features(class_mean, class_sig, args)
                labeled_output1 = model.forward_feat(labeled_feats)
                loss_ce_la = args.lambda_proto * criterion1(labeled_output1[:, :args.num_labeled_classes], labeled_labels)
            else:
                loss_ce_la = 0

            if args.w_kd > 0:
                _, _, old_feat = old_model(x)
                size_1, size_2 = old_feat.size()
                loss_kd = torch.dist(F.normalize(old_feat.view(size_1 * size_2, 1), dim=0),
                                     F.normalize(feat.view(size_1 * size_2, 1), dim=0)) * args.w_kd
            else:
                loss_kd = torch.tensor(0.0)

            # record losses
            loss_kd_record.update(loss_kd.item(), x.size(0))

            loss = loss_bce + w * consistency_loss + loss_kd + loss_ce_la

            if args.labeled_center > 0 and isinstance(loss_ce_la, torch.Tensor):
                loss_record.update(loss_ce_la.item(), x.size(0))

            # zero gradients
            optimizer.zero_grad()
            # calculate gradients w.r.t. parameters of Backbone + head-2 (head-1 is frozen)
            loss.backward()
            # update the parameters
            optimizer.step()

        # wandb loss logging
        # wandb.log({"loss/bce": loss_bce_record.avg,
        #            "loss/consistency": consistency_loss_record.avg,
        #            "loss/proto_lab": loss_record.avg,
        #            "loss/kd": loss_kd_record.avg
        #            }, step=epoch)

        print('Train Epoch: {} Avg Loss: {:.4f}'.format(epoch, loss_record.avg))

        print('Head2: test on unlabeled classes')
        args.head = 'head2'
        acc_head2_ul, ind = test(model, unlabeled_eval_loader, args, return_ind=True)

        print('Head1: test on labeled classes')
        args.head = 'head1'
        acc_head1_lb = test(model, labeled_eval_loader, args, cluster=False)

        print('Synthesized Head1: test on unlabeled classes')
        acc_head1_ul = test(model, unlabeled_eval_loader, args, cluster=False, ind=ind)

        print('Synthesized Head1: test on all classes w/o clustering')
        acc_head1_all_wo_cluster = test(model, all_eval_loader, args, cluster=False, ind=ind)

        print('Synthesized Head1: test on all classes w/ clustering')
        acc_head1_all_w_cluster = test(model, all_eval_loader, args, cluster=True)

        # wandb metrics logging
        # wandb.log({
        #     "val_acc/head2_ul": acc_head2_ul,
        #     "val_acc/Syn_head1_lb": acc_head1_lb,
        #     "val_acc/Syn_head1_ul": acc_head1_ul,
        #     "val_acc/Syn_head1_all_wo_clutering": acc_head1_all_wo_cluster,
        #     "val_acc/Syn_head1_all_w_clustering": acc_head1_all_w_cluster
        # }, step=epoch)

        # wandb.log({
        #     "misc/w_ramp": w
        # }, step=epoch)


def train_JointHead1_woPseudo(model, old_model, train_loader, labeled_eval_loader, unlabeled_eval_loader, all_eval_loader,
                             class_mean, class_sig, class_cov, args):
    print("=" * 100)
    print("\t\t\t\tCiao bella! I am Joint Head1 w/o Pseudo Label. I don't have Head2!")
    print("=" * 100)

    optimizer = SGD(model.parameters(), lr=args.lr, momentum=args.momentum, weight_decay=args.weight_decay)
    exp_lr_scheduler = lr_scheduler.StepLR(optimizer, step_size=args.step_size, gamma=args.gamma)
    criterion1 = nn.CrossEntropyLoss()      # CE loss for labeled data
    criterion2 = BCE()                      # BCE loss for unlabeled data

    # START training epoch-by-epoch
    for epoch in range(args.epochs):
        # create loss statistics recorder for each loss
        loss_record = AverageMeter()                # Total loss recorder
        # loss_ce_add_record = AverageMeter()       # CE loss recorder
        loss_bce_record = AverageMeter()            # BCE loss recorder
        consistency_loss_record = AverageMeter()    # MSE consistency loss recorder
        loss_kd_record = AverageMeter()             # KD loss recorder

        # turn on the training mode of the model
        model.train()

        # update LR scheduler for the current epoch
        exp_lr_scheduler.step()
        # update ramp-up coefficient for the current epoch
        w = args.rampup_coefficient * ramps.sigmoid_rampup(epoch, args.rampup_length)

        for batch_idx, ((x, x_bar), label, idx) in enumerate(tqdm(train_loader)):
            # send the vars to GPU
            x, x_bar, label = x.to(device), x_bar.to(device), label.to(device)
            # create a mask for labeled data
            mask_lb = label < args.num_labeled_classes

            # filter out the labeled entries for x, x_bar, label
            x = x[~mask_lb]
            x_bar = x_bar[~mask_lb]
            label = label[~mask_lb]

            # normalize the prototypes
            if args.l2_classifier:
                model.l2_classifier = True
                with torch.no_grad():
                    w_head = model.head1.weight.data.clone()
                    w_head = F.normalize(w_head, dim=1, p=2)
                    model.head1.weight.copy_(w_head)
            else:
                model.l2_classifier = False

            output1, _, feat = model(x)                 # outputs of x from head-1 and head-2 and extracted feat
            output2 = output1[:, -args.num_unlabeled_classes:]
            output1_bar, _, feat_bar = model(x_bar)     # outputs of x_bar from head-1 and head-2 and extracted feat
            output2_bar = output1_bar[:, -args.num_unlabeled_classes:]

            # use softmax to get the probability distribution for each head
            prob1, prob1_bar = F.softmax(output1, dim=1), F.softmax(output1_bar, dim=1)
            prob2, prob2_bar = F.softmax(output2, dim=1), F.softmax(output2_bar, dim=1)

            # calculate rank statistics
            rank_feat = (feat).detach()

            rank_idx = torch.argsort(rank_feat, dim=1, descending=True)
            rank_idx1, rank_idx2 = PairEnum(rank_idx)
            rank_idx1, rank_idx2 = rank_idx1[:, :args.topk], rank_idx2[:, :args.topk]

            rank_idx1, _ = torch.sort(rank_idx1, dim=1)
            rank_idx2, _ = torch.sort(rank_idx2, dim=1)

            rank_diff = rank_idx1 - rank_idx2
            rank_diff = torch.sum(torch.abs(rank_diff), dim=1)
            target_ulb = torch.ones_like(rank_diff).float().to(device)
            target_ulb[rank_diff > 0] = -1

            # get the probability distribution of the prediction for head-2
            prob1_ulb, _ = PairEnum(prob2)
            _, prob2_ulb = PairEnum(prob2_bar)


            loss_bce = criterion2(prob1_ulb, prob2_ulb, target_ulb)
            consistency_loss = F.mse_loss(prob2, prob2_bar)  # + F.mse_loss(prob1, prob1_bar)

            # record the losses
            loss_bce_record.update(loss_bce.item(), prob1_ulb.size(0))
            consistency_loss_record.update(consistency_loss.item(), prob2.size(0))

            if args.labeled_center > 0:
                labeled_feats, labeled_labels = sample_labeled_features(class_mean, class_sig, args)
                labeled_output1 = model.forward_feat(labeled_feats)
                loss_ce_la = args.lambda_proto * criterion1(labeled_output1[:, :args.num_labeled_classes], labeled_labels)
            else:
                loss_ce_la = 0

            if args.w_kd > 0:
                _, _, old_feat = old_model(x)
                size_1, size_2 = old_feat.size()
                loss_kd = torch.dist(F.normalize(old_feat.view(size_1 * size_2, 1), dim=0),
                                     F.normalize(feat.view(size_1 * size_2, 1), dim=0)) * args.w_kd
            else:
                loss_kd = torch.tensor(0.0)

            # record losses
            loss_kd_record.update(loss_kd.item(), x.size(0))

            loss = loss_bce + w * consistency_loss + loss_ce_la + loss_kd #+ loss_ce_add

            if args.labeled_center > 0 and isinstance(loss_ce_la, torch.Tensor):
                loss_record.update(loss_ce_la.item(), x.size(0))

            # zero gradients
            optimizer.zero_grad()
            # calculate gradients w.r.t. parameters of Backbone + head-2 (head-1 is frozen)
            loss.backward()
            # update the parameters
            optimizer.step()

        # wandb loss logging
        # wandb.log({#"loss/pseudo-unlab": loss_ce_add_record.avg,
        #            "loss/bce": loss_bce_record.avg,
        #            "loss/consistency": consistency_loss_record.avg,
        #            "loss/proto_lab": loss_record.avg,
        #            "loss/kd": loss_kd_record.avg
        #            }, step=epoch)

        print('Train Epoch: {} Avg Loss: {:.4f}'.format(epoch, loss_record.avg))

        print('Fake Head2: test on unlabeled classes')
        args.head = 'head2'
        acc_head2_ul, ind = test(model, unlabeled_eval_loader, args, return_ind=True)

        print('Head1: test on labeled classes')
        args.head = 'head1'
        acc_head1_lb = test(model, labeled_eval_loader, args, cluster=False)

        print('Head1: test on unlabeled classes')
        acc_head1_ul = test(model, unlabeled_eval_loader, args, cluster=False, ind=ind)

        print('Head1: test on all classes w/o clustering')
        acc_head1_all_wo_cluster = test(model, all_eval_loader, args, cluster=False, ind=ind)

        print('Head1: test on all classes w/ clustering')
        acc_head1_all_w_cluster = test(model, all_eval_loader, args, cluster=True)

        # wandb metrics logging
        # wandb.log({
        #     "val_acc/fake_head2_ul_head1_last": acc_head2_ul,
        #     "val_acc/head1_lb": acc_head1_lb,
        #     "val_acc/head1_ul": acc_head1_ul,
        #     "val_acc/head1_all_wo_clutering": acc_head1_all_wo_cluster,
        #     "val_acc/head1_all_w_clustering": acc_head1_all_w_cluster
        # }, step=epoch)

        # wandb.log({
        #     "misc/w_ramp": w
        # }, step=epoch)

        # END of the current epoch



def train_JointHead1(model, old_model, train_loader, labeled_eval_loader, unlabeled_eval_loader, all_eval_loader,
                     class_mean, class_sig, class_cov, args):
    print("=" * 100)
    print("\t\t\t\tCiao bella! I am Joint Head1. I don't have Head2!")
    print("=" * 100)

    optimizer = SGD(model.parameters(), lr=args.lr, momentum=args.momentum, weight_decay=args.weight_decay)
    exp_lr_scheduler = lr_scheduler.StepLR(optimizer, step_size=args.step_size, gamma=args.gamma)
    criterion1 = nn.CrossEntropyLoss()      # CE loss for labeled data
    criterion2 = BCE()                      # BCE loss for unlabeled data

    # START training epoch-by-epoch
    for epoch in range(args.epochs):
        # create loss statistics recorder for each loss
        loss_record = AverageMeter()                # Total loss recorder
        loss_ce_add_record = AverageMeter()         # CE loss recorder
        loss_bce_record = AverageMeter()            # BCE loss recorder
        consistency_loss_record = AverageMeter()    # MSE consistency loss recorder
        loss_kd_record = AverageMeter()             # KD loss recorder

        # turn on the training mode of the model
        model.train()
        # update LR scheduler for the current epoch
        exp_lr_scheduler.step()
        # update ramp-up coefficient for the current epoch
        w = args.rampup_coefficient * ramps.sigmoid_rampup(epoch, args.rampup_length)

        for batch_idx, ((x, x_bar), label, idx) in enumerate(tqdm(train_loader)):
            # send the vars to GPU
            x, x_bar, label = x.to(device), x_bar.to(device), label.to(device)
            # create a mask for labeled data
            mask_lb = label < args.num_labeled_classes

            # filter out the labeled entries for x, x_bar, label
            x = x[~mask_lb]
            x_bar = x_bar[~mask_lb]
            label = label[~mask_lb]

            # normalize the prototypes
            if args.l2_classifier:
                model.l2_classifier = True
                with torch.no_grad():
                    w_head = model.head1.weight.data.clone()
                    w_head = F.normalize(w_head, dim=1, p=2)
                    model.head1.weight.copy_(w_head)
            else:
                model.l2_classifier = False

            output1, _, feat = model(x)                             # outputs of x from head-1 and head-2 and extracted feat
            output2 = output1[:, -args.num_unlabeled_classes:]
            output1_bar, _, feat_bar = model(x_bar)                 # outputs of x_bar from head-1 and head-2 and extracted feat
            output2_bar = output1_bar[:, -args.num_unlabeled_classes:]

            # use softmax to get the probability distribution for each head
            prob1, prob1_bar = F.softmax(output1, dim=1), F.softmax(output1_bar, dim=1)
            prob2, prob2_bar = F.softmax(output2, dim=1), F.softmax(output2_bar, dim=1)

            # calculate rank statistics
            rank_feat = (feat).detach()

            rank_idx = torch.argsort(rank_feat, dim=1, descending=True)
            rank_idx1, rank_idx2 = PairEnum(rank_idx)
            rank_idx1, rank_idx2 = rank_idx1[:, :args.topk], rank_idx2[:, :args.topk]

            rank_idx1, _ = torch.sort(rank_idx1, dim=1)
            rank_idx2, _ = torch.sort(rank_idx2, dim=1)

            rank_diff = rank_idx1 - rank_idx2
            rank_diff = torch.sum(torch.abs(rank_diff), dim=1)
            target_ulb = torch.ones_like(rank_diff).float().to(device)
            target_ulb[rank_diff > 0] = -1

            # get the probability distribution of the prediction for head-2
            prob1_ulb, _ = PairEnum(prob2)
            _, prob2_ulb = PairEnum(prob2_bar)

            # get the pseudo label from head-2
            label = (output2).detach().max(1)[1] + args.num_labeled_classes

            loss_ce_add = w * criterion1(output1, label) / args.rampup_coefficient * args.increment_coefficient
            loss_bce = criterion2(prob1_ulb, prob2_ulb, target_ulb)
            consistency_loss = F.mse_loss(prob2, prob2_bar)  # + F.mse_loss(prob1, prob1_bar)

            # record the losses
            loss_ce_add_record.update(loss_ce_add.item(), output1.size(0))
            loss_bce_record.update(loss_bce.item(), prob1_ulb.size(0))
            consistency_loss_record.update(consistency_loss.item(), prob2.size(0))

            if args.labeled_center > 0:
                labeled_feats, labeled_labels = sample_labeled_features(class_mean, class_sig, args)  # Prototype
                labeled_output1 = model.forward_feat(labeled_feats)

                loss_ce_la = args.lambda_proto * criterion1(labeled_output1, labeled_labels)
            else:
                loss_ce_la = 0

            if args.w_kd > 0:
                _, _, old_feat = old_model(x)
                size_1, size_2 = old_feat.size()
                loss_kd = torch.dist(F.normalize(old_feat.view(size_1 * size_2, 1), dim=0),
                                     F.normalize(feat.view(size_1 * size_2, 1), dim=0)) * args.w_kd
            else:
                loss_kd = torch.tensor(0.0)

            # record losses
            loss_kd_record.update(loss_kd.item(), x.size(0))

            loss = loss_bce + loss_ce_add + w * consistency_loss + loss_ce_la + loss_kd

            if args.labeled_center > 0 and isinstance(loss_ce_la, torch.Tensor):
                loss_record.update(loss_ce_la.item(), x.size(0))

            # zero gradients
            optimizer.zero_grad()
            # calculate gradients w.r.t. parameters of Backbone + head-2 (head-1 is frozen)
            loss.backward()
            # update the parameters
            optimizer.step()

        # wandb loss logging
        # wandb.log({"loss/pseudo-unlab": loss_ce_add_record.avg,
        #            "loss/bce": loss_bce_record.avg,
        #            "loss/consistency": consistency_loss_record.avg,
        #            "loss/proto_lab": loss_record.avg,
        #            "loss/kd": loss_kd_record.avg
        #            }, step=epoch)

        print('Train Epoch: {} Avg Loss: {:.4f}'.format(epoch, loss_record.avg))

        print('Fake Head2: test on unlabeled classes')
        args.head = 'head2'
        acc_head2_ul, ind = test(model, unlabeled_eval_loader, args, return_ind=True)

        print('Head1: test on labeled classes')
        args.head = 'head1'
        acc_head1_lb = test(model, labeled_eval_loader, args, cluster=False)

        print('Head1: test on unlabeled classes')
        acc_head1_ul = test(model, unlabeled_eval_loader, args, cluster=False, ind=ind)

        print('Head1: test on all classes w/o clustering')
        acc_head1_all_wo_cluster = test(model, all_eval_loader, args, cluster=False, ind=ind)

        print('Head1: test on all classes w/ clustering')
        acc_head1_all_w_cluster = test(model, all_eval_loader, args, cluster=True)

        # wandb metrics logging
        # wandb.log({
        #     "val_acc/fake_head2_ul_head1_last": acc_head2_ul,
        #     "val_acc/head1_lb": acc_head1_lb,
        #     "val_acc/head1_ul": acc_head1_ul,
        #     "val_acc/head1_all_wo_clutering": acc_head1_all_wo_cluster,
        #     "val_acc/head1_all_w_clustering": acc_head1_all_w_cluster
        # }, step=epoch)

        # wandb.log({
        #     "misc/w_ramp": w
        # }, step=epoch)

def train_LwF(model, old_model, train_loader, labeled_eval_loader, unlabeled_eval_loader, all_eval_loader, args):
    print("="*100)
    print("\t\t\t\tCiao bella! I am LwF! I don't have Feat-KD and Feat-Proto!")
    print("="*100)

    optimizer = SGD(model.parameters(), lr=args.lr, momentum=args.momentum, weight_decay=args.weight_decay)
    exp_lr_scheduler = lr_scheduler.StepLR(optimizer, step_size=args.step_size, gamma=args.gamma)
    criterion1 = nn.CrossEntropyLoss()
    criterion2 = BCE()

    for epoch in range(args.epochs):
        # create loss statistics recorder for each loss
        loss_ce_add_record = AverageMeter()         # CE loss recorder
        loss_bce_record = AverageMeter()            # BCE loss recorder
        consistency_loss_record = AverageMeter()    # MSE consistency loss recorder
        loss_kd_record = AverageMeter()             # KD loss recorder

        # turn on the training mode of the model
        model.train()

        exp_lr_scheduler.step()
        w = args.rampup_coefficient * ramps.sigmoid_rampup(epoch, args.rampup_length)

        for batch_idx, ((x, x_bar), label, idx) in enumerate(tqdm(train_loader)):
            # send the vars to GPU
            x, x_bar, label = x.to(device), x_bar.to(device), label.to(device)
            # create a mask for labeled data
            mask_lb = label < args.num_labeled_classes

            # filter out the labeled entries for x, x_bar, label
            x = x[~mask_lb]
            x_bar = x_bar[~mask_lb]
            label = label[~mask_lb]

            # normalize the prototypes
            if args.l2_classifier:
                model.l2_classifier = True
                with torch.no_grad():
                    w_head = model.head1.weight.data.clone()
                    w_head = F.normalize(w_head, dim=1, p=2)
                    model.head1.weight.copy_(w_head)
            else:
                model.l2_classifier = False

            output1, output2, feat = model(x)
            output1_bar, output2_bar, feat_bar = model(x_bar)

            # use softmax to get the probability distribution for each head
            prob1, prob1_bar = F.softmax(output1, dim=1), F.softmax(output1_bar, dim=1)
            prob2, prob2_bar = F.softmax(output2, dim=1), F.softmax(output2_bar, dim=1)

            # calculate rank statistics
            rank_feat = (feat).detach()

            rank_idx = torch.argsort(rank_feat, dim=1, descending=True)
            rank_idx1, rank_idx2 = PairEnum(rank_idx)
            rank_idx1, rank_idx2 = rank_idx1[:, :args.topk], rank_idx2[:, :args.topk]

            rank_idx1, _ = torch.sort(rank_idx1, dim=1)
            rank_idx2, _ = torch.sort(rank_idx2, dim=1)

            rank_diff = rank_idx1 - rank_idx2
            rank_diff = torch.sum(torch.abs(rank_diff), dim=1)
            target_ulb = torch.ones_like(rank_diff).float().to(device)
            target_ulb[rank_diff > 0] = -1

            # get the probability distribution of the prediction for head-2
            prob1_ulb, _ = PairEnum(prob2)
            _, prob2_ulb = PairEnum(prob2_bar)

            # get the pseudo label from head-2
            label = (output2).detach().max(1)[1] + args.num_labeled_classes

            loss_ce_add = w * criterion1(output1, label) / args.rampup_coefficient * args.increment_coefficient
            loss_bce = criterion2(prob1_ulb, prob2_ulb, target_ulb)
            consistency_loss = F.mse_loss(prob2, prob2_bar)  # + F.mse_loss(prob1, prob1_bar)

            # record the losses
            loss_ce_add_record.update(loss_ce_add.item(), output1.size(0))
            loss_bce_record.update(loss_bce.item(), prob1_ulb.size(0))
            consistency_loss_record.update(consistency_loss.item(), prob2.size(0))

            if args.w_kd > 0:
                old_output1, _, _ = old_model(x)
                size_1, size_2 = old_output1.size()
                loss_kd = torch.dist(F.normalize(old_output1.view(size_1 * size_2, 1), dim=0),
                                     F.normalize(output1[:, :size_2].reshape(size_1 * size_2, 1), dim=0)) * args.w_kd
                # # LwF loss
                # old_output1, _, _ = old_model(x)
                # size_1, size_2 = old_output1.size()
                # soft_old_output1 = F.softmax(old_output1 / 2, dim=1)
                # soft_new_output1 = F.softmax(output1[:, :size_2] / 2, dim=1)
                # loss_kd = soft_old_output1.mul(-1*torch.log(soft_new_output1))
                # loss_kd = loss_kd.sum(1)
                # loss_kd = loss_kd.mean() * 2 * 2 * args.w_kd

                old_output1, _, _ = old_model(x)
                size_1, size_2 = old_output1.size()
                soft_old_output1 = F.softmax(old_output1 / 2, dim=1)
                log_soft_new_output1 = F.log_softmax(output1[:, :size_2] / 2, dim=1)
                loss_kd = -torch.mean(torch.sum(soft_old_output1*log_soft_new_output1, dim=1)) * args.w_kd

            else:
                loss_kd = torch.tensor(0.0)

            # record losses
            loss_kd_record.update(loss_kd.item(), x.size(0))

            loss = loss_bce + loss_ce_add + w * consistency_loss + loss_kd

            # zero gradients
            optimizer.zero_grad()
            # calculate gradients w.r.t. parameters of Backbone + head-2 (head-1 is frozen)
            loss.backward()
            # update the parameters
            optimizer.step()

        # wandb loss logging
        # wandb.log({"loss/pseudo-unlab": loss_ce_add_record.avg,
        #            "loss/bce": loss_bce_record.avg,
        #            "loss/consistency": consistency_loss_record.avg,
        #            "loss/LwF_logits_kd": loss_kd_record.avg
        #            }, step=epoch)

        print('Head2: test on unlabeled classes')
        args.head = 'head2'
        acc_head2_ul, ind = test(model, unlabeled_eval_loader, args, return_ind=True)

        print('Head1: test on labeled classes')
        args.head = 'head1'
        acc_head1_lb = test(model, labeled_eval_loader, args, cluster=False)

        print('Head1: test on unlabeled classes')
        acc_head1_ul = test(model, unlabeled_eval_loader, args, cluster=False, ind=ind)

        print('Head1: test on all classes w/o clustering')
        acc_head1_all_wo_cluster = test(model, all_eval_loader, args, cluster=False, ind=ind)

        print('Head1: test on all classes w/ clustering')
        acc_head1_all_w_cluster = test(model, all_eval_loader, args, cluster=True)

        # wandb metrics logging
        # wandb.log({
        #     "val_acc/head2_ul": acc_head2_ul,
        #     "val_acc/head1_lb": acc_head1_lb,
        #     "val_acc/head1_ul": acc_head1_ul,
        #     "val_acc/head1_all_wo_clutering": acc_head1_all_wo_cluster,
        #     "val_acc/head1_all_w_clustering": acc_head1_all_w_cluster
        # }, step=epoch)

        # wandb.log({
        #     "misc/w_ramp": w
        # }, step=epoch)


def train_IL_center(model, old_model, train_loader, labeled_eval_loader, unlabeled_eval_loader, all_eval_loader,
                    class_mean, class_sig, class_cov, args):
    print("=" * 100)
    print("\t\t\t\tCiao bella! I am {} model!".format(args.IL_version))
    print("=" * 100)

    optimizer = SGD(model.parameters(), lr=args.lr, momentum=args.momentum, weight_decay=args.weight_decay)
    exp_lr_scheduler = lr_scheduler.StepLR(optimizer, step_size=args.step_size, gamma=args.gamma)
    criterion1 = nn.CrossEntropyLoss()      # CE loss for labeled data
    criterion2 = BCE()                      # BCE loss for unlabeled data

    for epoch in range(args.epochs):
        # create loss statistics recorder for each loss
        loss_record = AverageMeter()                    # Total loss recorder
        loss_ce_add_record = AverageMeter()             # CE loss recorder
        loss_bce_record = AverageMeter()                # BCE loss recorder
        consistency_loss_record = AverageMeter()        # MSE consistency loss recorder
        loss_kd_record = AverageMeter()                 # KD loss recorder

        # turn on the training mode of the model
        model.train()
        # update LR scheduler for the current epoch
        exp_lr_scheduler.step()
        # update ramp-up coefficient for the current epoch
        w = args.rampup_coefficient * ramps.sigmoid_rampup(epoch, args.rampup_length)

        for batch_idx, ((x, x_bar), label, idx) in enumerate(tqdm(train_loader)):
            # send the vars to GPU
            x, x_bar, label = x.to(device), x_bar.to(device), label.to(device)
            # create a mask for labeled data
            mask_lb = label < args.num_labeled_classes

            # filter out the labeled entries for x, x_bar, label
            x = x[~mask_lb]
            x_bar = x_bar[~mask_lb]
            label = label[~mask_lb]

            # normalize the prototypes
            if args.l2_classifier:
                model.l2_classifier = True
                with torch.no_grad():
                    w_head = model.head1.weight.data.clone()
                    w_head = F.normalize(w_head, dim=1, p=2)
                    model.head1.weight.copy_(w_head)
                    # if epoch == 5 and w_head_fix is None:
                    #     w_head_fix = w_head[:args.num_labeled_classes, :]
            else:
                model.l2_classifier = False

            # LOOK: now, x only contains unlabeled data
            output1, output2, feat = model(x)                   # outputs of x from head-1 and head-2 and extracted feat
                                                                # for unlabeled data
            output1_bar, output2_bar, feat_bar = model(x_bar)   # outputs of x_bar from head-1 and head-2 and extracted feat
                                                                # for unlabeled data

            # use softmax to get the probability distribution for each head
            prob1, prob1_bar = F.softmax(output1, dim=1), F.softmax(output1_bar, dim=1)
            prob2, prob2_bar = F.softmax(output2, dim=1), F.softmax(output2_bar, dim=1)

            # calculate rank statistics
            # first cut the gradient propagation of the feat
            rank_feat = (feat).detach()

            rank_idx = torch.argsort(rank_feat, dim=1, descending=True)
            rank_idx1, rank_idx2 = PairEnum(rank_idx)
            rank_idx1, rank_idx2 = rank_idx1[:, :args.topk], rank_idx2[:, :args.topk]

            rank_idx1, _ = torch.sort(rank_idx1, dim=1)
            rank_idx2, _ = torch.sort(rank_idx2, dim=1)

            rank_diff = rank_idx1 - rank_idx2
            rank_diff = torch.sum(torch.abs(rank_diff), dim=1)
            target_ulb = torch.ones_like(rank_diff).float().to(device)
            target_ulb[rank_diff > 0] = -1

            # get the probability distribution of the prediction for head-2
            prob1_ulb, _ = PairEnum(prob2)
            _, prob2_ulb = PairEnum(prob2_bar)

            # get the pseudo label from head-2
            label = (output2).detach().max(1)[1] + args.num_labeled_classes

            loss_ce_add = w * criterion1(output1, label) / args.rampup_coefficient * args.increment_coefficient
            loss_bce = criterion2(prob1_ulb, prob2_ulb, target_ulb)
            consistency_loss = F.mse_loss(prob2, prob2_bar) # + F.mse_loss(prob1, prob1_bar)

            # record the losses
            loss_ce_add_record.update(loss_ce_add.item(), output1.size(0))
            loss_bce_record.update(loss_bce.item(), prob1_ulb.size(0))
            consistency_loss_record.update(consistency_loss.item(), prob2.size(0))

            if args.labeled_center > 0:
                labeled_feats, labeled_labels = sample_labeled_features(class_mean, class_sig, args)
                labeled_output1 = model.forward_feat(labeled_feats)
                loss_ce_la = args.lambda_proto * criterion1(labeled_output1, labeled_labels)
            else:
                loss_ce_la = 0
        
            if args.w_kd > 0:
                _, _, old_feat = old_model(x)
                size_1, size_2 = old_feat.size()
                loss_kd = torch.dist(F.normalize(old_feat.view(size_1 * size_2, 1), dim=0), F.normalize(feat.view(size_1 * size_2, 1), dim=0)) * args.w_kd
            else:
                loss_kd = torch.tensor(0.0)
            
            # record losses
            loss_kd_record.update(loss_kd.item(), x.size(0))

            loss = loss_bce + loss_ce_add + w * consistency_loss + loss_ce_la + loss_kd

            if args.labeled_center > 0 and isinstance(loss_ce_la, torch.Tensor):
                loss_record.update(loss_ce_la.item(), x.size(0))

            optimizer.zero_grad()
            loss.backward()
            optimizer.step()

        # wandb loss logging
        # wandb.log({"loss/pseudo-unlab": loss_ce_add_record.avg,
        #            "loss/bce": loss_bce_record.avg,
        #            "loss/consistency": consistency_loss_record.avg,
        #            "loss/proto_lab": loss_record.avg,
        #            "loss/kd": loss_kd_record.avg
        #           }, step=epoch)

        print('Train Epoch: {} Avg Loss: {:.4f}'.format(epoch, loss_record.avg))

        print('Head2: test on unlabeled classes')
        args.head = 'head2'
        acc_head2_ul, ind = test(model, unlabeled_eval_loader, args, return_ind=True)

        print('Head1: test on labeled classes')
        args.head = 'head1'
        acc_head1_lb = test(model, labeled_eval_loader, args, cluster=False)

        print('Head1: test on unlabeled classes')
        acc_head1_ul = test(model, unlabeled_eval_loader, args, cluster=False, ind=ind)

        print('Head1: test on all classes w/o clustering')
        acc_head1_all_wo_cluster = test(model, all_eval_loader, args, cluster=False, ind=ind)

        print('Head1: test on all classes w/ clustering')
        acc_head1_all_w_cluster = test(model, all_eval_loader, args, cluster=True)

        # wandb metrics logging
        # wandb.log({
        #     "val_acc/head2_ul": acc_head2_ul,
        #     "val_acc/head1_lb": acc_head1_lb,
        #     "val_acc/head1_ul": acc_head1_ul,
        #     "val_acc/head1_all_wo_clutering": acc_head1_all_wo_cluster,
        #     "val_acc/head1_all_w_clustering": acc_head1_all_w_cluster
        # }, step=epoch)

        # wandb.log({
        #     "misc/w_ramp": w
        # }, step=epoch)


def Generate_Center(model, labeled_train_loader, args):
    all_feat = []
    all_labels = []

    class_mean = torch.zeros(args.num_labeled_classes, 512).cuda()
    class_sig = torch.zeros(args.num_labeled_classes, 512).cuda()

    print ('Extract Labeled Feature')
    for epoch in range(1):
        model.eval()
        for batch_idx, (x, label, idx) in enumerate(tqdm(labeled_train_loader)):
            x, label = x.to(device), label.to(device)

            output1, output2, feat = model(x)

            all_feat.append(feat.detach().clone().cuda())
            all_labels.append(label.detach().clone().cuda())

    all_feat = torch.cat(all_feat, dim=0).cuda()
    all_labels = torch.cat(all_labels, dim=0).cuda()

    print ('Calculate Labeled Mean-Var')
    for i in range(args.num_labeled_classes):
        this_feat = all_feat[all_labels==i]
        this_mean = this_feat.mean(dim=0)
        this_var = this_feat.var(dim=0)
        class_mean[i, :] = this_mean
        class_sig[i, :] = (this_var + 1e-5).sqrt()
    print ('Finish')

    class_mean, class_sig, class_cov = class_mean.cuda(), class_sig.cuda(), 0

    return class_mean, class_sig, class_cov

def sample_labeled_features(class_mean, class_sig, args):
    feats = []
    labels = []

    if args.dataset_name == 'cifar10':
        num_per_class = 20
    elif args.dataset_name == 'cifar100':
        num_per_class = 2
    else:
        num_per_class = 3

    for i in range(args.num_labeled_classes):
        dist = torch.distributions.Normal(class_mean[i], class_sig.mean(dim=0))
        this_feat = dist.sample((num_per_class,)).cuda()
        this_label = torch.ones(this_feat.size(0)).cuda() * i

        feats.append(this_feat)
        labels.append(this_label)

    feats = torch.cat(feats, dim=0)
    labels = torch.cat(labels, dim=0).long()

    return feats, labels

def isda_aug(fc, features, y, labels, cv_matrix, ratio=1):
    N = features.size(0)
    C = y.size(1)
    A = features.size(1)

    weight_m = list(fc.parameters())[0]

    NxW_ij = weight_m.expand(N, C, A)

    NxW_kj = torch.gather(NxW_ij, 1, labels.view(N, 1, 1).expand(N, C, A))

    CV_temp = cv_matrix[labels]

    sigma2 = ratio * torch.bmm(torch.bmm(NxW_ij - NxW_kj, CV_temp), (NxW_ij - NxW_kj).permute(0, 2, 1))

    sigma2 = sigma2.mul(torch.eye(C).cuda().expand(N, C, C)).sum(2).view(N, C)

    aug_result = y + 0.5 * sigma2

    return aug_result

def wandb_logits_norm(args, this_epoch, head, dloader_type, logits_mean):
    panel_prefix = head + '_' + dloader_type
    if head == 'head1':
        old_part = np.linalg.norm(logits_mean[:args.num_labeled_classes])
        ncd_part = np.linalg.norm(logits_mean[args.num_labeled_classes:])
        print("HEAD1: old_norm = {},  ncd_norm = {}".format(old_part, ncd_part))
        wandb.log({
            "logits_norm/"+panel_prefix+'_old_part': old_part,
            "logits_norm/"+panel_prefix+'_ncd_part': ncd_part,
        }, step=this_epoch)
    elif head == 'head2':
        ncd_part = np.linalg.norm(logits_mean)
        print("HEAD2: ncd_norm = {}".format(ncd_part))
        wandb.log({
            "logits_norm/"+panel_prefix+'_ncd_part': ncd_part,
        }, step=this_epoch)

def test(model, test_loader, args, cluster=True, ind=None, return_ind=False):
    model.eval()
    preds = np.array([])
    targets = np.array([])

    for batch_idx, (x, label, _) in enumerate(tqdm(test_loader)):
        x, label = x.to(device), label.to(device)
        output1, output2, _ = model(x)
        if args.head == 'head1':
            if args.IL_version == 'SplitHead12' or 'AutoNovel':
                output = torch.cat((output1, output2), dim=1)
            else:
                output = output1
        else:
            if args.IL_version == 'JointHead1' or args.IL_version == 'JointHead1woPseudo':
                output = output1[:, -args.num_unlabeled_classes:]
            else:
                output = output2

        _, pred = output.max(1)
        targets = np.append(targets, label.cpu().numpy())
        preds = np.append(preds, pred.cpu().numpy())

    if cluster:
        if return_ind:
            acc, ind = cluster_acc(targets.astype(int), preds.astype(int), return_ind)
        else:
            acc = cluster_acc(targets.astype(int), preds.astype(int), return_ind)
        nmi, ari = nmi_score(targets, preds), ari_score(targets, preds)
        print('Test acc {:.4f}, nmi {:.4f}, ari {:.4f}'.format(acc, nmi, ari))
    else:
        if ind is not None:
            ind = ind[:args.num_unlabeled_classes, :]
            idx = np.argsort(ind[:, 1])
            id_map = ind[idx, 0]
            id_map += args.num_labeled_classes

            # targets_new = targets <-- this is not deep copy anymore due to NumPy version change
            targets_new = np.copy(targets)
            for i in range(args.num_unlabeled_classes):
                targets_new[targets == i + args.num_labeled_classes] = id_map[i]
            targets = targets_new

        preds = torch.from_numpy(preds)
        targets = torch.from_numpy(targets)
        correct = preds.eq(targets).float().sum(0)
        acc = float(correct / targets.size(0))
        print('Test acc {:.4f}'.format(acc))

    if return_ind:
        return acc, ind
    else:
        return acc


def freeze_layers(model, layer_names, freeze=True):
    if not isinstance(layer_names, Iterable):
        layer_names = [layer_names]
    for name, child in model.named_children():
        if name not in layer_names:
            continue
        for param in child.parameters():
            param.requires_grad = not freeze

def unfreeze_layers(model, layer_names):
    freeze_layers(model, layer_names, False)

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description='cluster', formatter_class=argparse.ArgumentDefaultsHelpFormatter)

    parser.add_argument('--lr', type=float, default=0.1)
    parser.add_argument('--gamma', type=float, default=0.1)
    parser.add_argument('--momentum', type=float, default=0.9)
    parser.add_argument('--weight_decay', type=float, default=1e-4)
    parser.add_argument('--w_kd', type=float, default=10.0)
    parser.add_argument('--epochs', default=200, type=int)
    parser.add_argument('--rampup_length', default=150, type=int)
    parser.add_argument('--rampup_coefficient', type=float, default=50)
    parser.add_argument('--increment_coefficient', type=float, default=0.05)
    parser.add_argument('--step_size', default=170, type=int)
    parser.add_argument('--batch_size', default=128, type=int)
    parser.add_argument('--num_unlabeled_classes', default=5, type=int)
    parser.add_argument('--num_labeled_classes', default=5, type=int)
    parser.add_argument('--dataset_root', type=str, default='./data/datasets/CIFAR/')
    parser.add_argument('--exp_root', type=str, default='./data/experiments/')
    parser.add_argument('--warmup_model_dir', type=str, default='./data/experiments/pretrain/auto_novel/resnet_rotnet_cifar10.pth')
    parser.add_argument('--finetune_model_dir', type=str, default='./data/experiments/pretrain/auto_novel/resnet_rotnet_cifar10.pth')
    parser.add_argument('--topk', default=5, type=int)
    # parser.add_argument('--IL', action='store_true', default=False, help='w/ incremental learning')
    parser.add_argument('--IL_version', type=str, default='OG', choices=['OG', 'LwF', 'LwFProto', 'JointHead1',
                                                                         'JointHead1woPseudo', 'SplitHead12',
                                                                         'OGwoKD', 'OGwoProto', 'OGwoPseudo',
                                                                         'AutoNovel', 'OGwoKDwoProto', 'OGwoKDwoPseudo',
                                                                         'OGwoProtowoPseudo', 'OGwoKDwoProtowoPseudo'])
    parser.add_argument('--detach_B', action='store_true', default=False, help='Detach the feature of the backbone')
    parser.add_argument('--l2_classifier', action='store_true', default=False, help='L2 normalize classifier')
    parser.add_argument('--labeled_center', type=float, default=10.0)
    parser.add_argument('--model_name', type=str, default='resnet')
    parser.add_argument('--dataset_name', type=str, default='cifar10', help='options: cifar10, cifar100, svhn')
    parser.add_argument('--seed', default=1, type=int)
    parser.add_argument('--mode', type=str, default='train')
    parser.add_argument('--wandb_mode', type=str, default='online', choices=['online', 'offline', 'disabled'])
    parser.add_argument('--wandb_entity', type=str, default='unitn-mhug')
    parser.add_argument('--lambda_proto', type=float, default=1.0, help='weight for the source prototypes loss')
    parser.add_argument('--track_centroid', action='store_true', default=False, help='track the centroid epoch-wise')
    parser.add_argument('--track_interval', default=10, type=int, help="the frequency to save the feature statistics")

    args = parser.parse_args()

    args.cuda = torch.cuda.is_available()
    device = torch.device("cuda" if args.cuda else "cpu")
    seed_torch(args.seed)

    runner_name = os.path.basename(__file__).split(".")[0]
    model_dir = os.path.join(args.exp_root, runner_name+'_'+args.dataset_name)
    if not os.path.exists(model_dir):
        os.makedirs(model_dir)
    args.model_dir = model_dir+'/'+'{}.pth'.format(args.model_name)

    args.log_dir = model_dir + '/' + args.model_name + '_fixl1_s_' + str(args.seed) + '_log.txt'
    sys.stdout = Logger(args.log_dir)

    print('log_dir=', args.log_dir)
    print(args)

    num_classes = args.num_labeled_classes + args.num_unlabeled_classes

    # WandB setting
    # use wandb logging
    # if args.mode == 'train':
    #     wandb_run_name = args.model_name + '_fixl1_s_' + str(args.seed)
    #     wandb.init(project='incd_dev_miu',
    #                entity=args.wandb_entity,
    #                name=wandb_run_name,
    #                mode=args.wandb_mode)

    # Dataloader creation
    print("used batch size is {}".format(args.batch_size))
    if args.dataset_name == 'cifar10':
        mix_train_loader = CIFAR10LoaderMix(root=args.dataset_root, batch_size=args.batch_size, split='train', aug='twice', shuffle=True, labeled_list=range(args.num_labeled_classes), unlabeled_list=range(args.num_labeled_classes, num_classes))
        labeled_train_loader = CIFAR10Loader(root=args.dataset_root, batch_size=args.batch_size, split='train', aug=None, shuffle=True, target_list = range(args.num_labeled_classes))
        unlabeled_eval_loader = CIFAR10Loader(root=args.dataset_root, batch_size=args.batch_size, split='train', aug=None, shuffle=False, target_list = range(args.num_labeled_classes, num_classes))
        unlabeled_eval_loader_test = CIFAR10Loader(root=args.dataset_root, batch_size=args.batch_size, split='test', aug=None, shuffle=False, target_list = range(args.num_labeled_classes, num_classes))
        labeled_eval_loader = CIFAR10Loader(root=args.dataset_root, batch_size=args.batch_size, split='test', aug=None, shuffle=False, target_list = range(args.num_labeled_classes))
        all_eval_loader = CIFAR10Loader(root=args.dataset_root, batch_size=args.batch_size, split='test', aug=None, shuffle=False, target_list = range(num_classes))
    elif args.dataset_name == 'cifar100':
        mix_train_loader = CIFAR100LoaderMix(root=args.dataset_root, batch_size=args.batch_size, split='train', aug='twice', shuffle=True, labeled_list=range(args.num_labeled_classes), unlabeled_list=range(args.num_labeled_classes, num_classes))
        labeled_train_loader = CIFAR100Loader(root=args.dataset_root, batch_size=args.batch_size, split='train', aug=None, shuffle=True, target_list = range(args.num_labeled_classes))
        unlabeled_eval_loader = CIFAR100Loader(root=args.dataset_root, batch_size=args.batch_size, split='train', aug=None, shuffle=False, target_list = range(args.num_labeled_classes, num_classes))
        unlabeled_eval_loader_test = CIFAR100Loader(root=args.dataset_root, batch_size=args.batch_size, split='test', aug=None, shuffle=False, target_list = range(args.num_labeled_classes, num_classes))
        labeled_eval_loader = CIFAR100Loader(root=args.dataset_root, batch_size=args.batch_size, split='test', aug=None, shuffle=False, target_list = range(args.num_labeled_classes))
        all_eval_loader = CIFAR100Loader(root=args.dataset_root, batch_size=args.batch_size, split='test', aug=None, shuffle=False, target_list = range(num_classes))
    elif args.dataset_name == 'svhn':
        mix_train_loader = SVHNLoaderMix(root=args.dataset_root, batch_size=args.batch_size, split='train', aug='twice', shuffle=True, labeled_list=range(args.num_labeled_classes), unlabeled_list=range(args.num_labeled_classes, num_classes))
        labeled_train_loader = SVHNLoader(root=args.dataset_root, batch_size=args.batch_size, split='train', aug='once', shuffle=True, target_list = range(args.num_labeled_classes))
        unlabeled_eval_loader = SVHNLoader(root=args.dataset_root, batch_size=args.batch_size, split='train', aug=None, shuffle=False, target_list = range(args.num_labeled_classes, num_classes))
        unlabeled_eval_loader_test = SVHNLoader(root=args.dataset_root, batch_size=args.batch_size, split='test', aug=None, shuffle=False, target_list = range(args.num_labeled_classes, num_classes))
        labeled_eval_loader = SVHNLoader(root=args.dataset_root, batch_size=args.batch_size, split='test', aug=None, shuffle=False, target_list = range(args.num_labeled_classes))
        all_eval_loader = SVHNLoader(root=args.dataset_root, batch_size=args.batch_size, split='test', aug=None, shuffle=False, target_list = range(num_classes))
    elif args.dataset_name == 'tinyimagenet':
        mix_train_loader = TinyImageNetLoader(batch_size=args.batch_size, num_workers=8, path=args.dataset_root, aug='twice', shuffle=True, class_list = range(args.num_labeled_classes, num_classes), subfolder='train')
        unlabeled_eval_loader = TinyImageNetLoader(batch_size=args.batch_size, num_workers=8, path=args.dataset_root, aug=None, shuffle=False, class_list = range(args.num_labeled_classes, num_classes), subfolder='train')
        unlabeled_eval_loader_test = TinyImageNetLoader(batch_size=args.batch_size, num_workers=8, path=args.dataset_root, aug=None, shuffle=False, class_list = range(args.num_labeled_classes, num_classes), subfolder='val')
        labeled_train_loader = TinyImageNetLoader(batch_size=args.batch_size, num_workers=8, path=args.dataset_root, aug=None, shuffle=True, class_list = range(args.num_labeled_classes), subfolder='train')
        labeled_eval_loader = TinyImageNetLoader(batch_size=args.batch_size, num_workers=8, path=args.dataset_root, aug=None, shuffle=False, class_list = range(args.num_labeled_classes), subfolder='val')
        all_eval_loader = TinyImageNetLoader(batch_size=args.batch_size, num_workers=8, path=args.dataset_root, aug=None, shuffle=False, class_list = range(num_classes), subfolder='val')

    # Model Creation
    model = ResNet(BasicBlock, [2, 2, 2, 2], args.num_labeled_classes, args.num_unlabeled_classes).to(device)

    if args.mode == 'train':
        state_dict = torch.load(args.warmup_model_dir)
        model.load_state_dict(state_dict, strict=False)
        for name, param in model.named_parameters():
            if 'head' not in name and 'layer4' not in name and 'layer3' not in name and 'layer2' not in name:
                param.requires_grad = False

    if args.w_kd > 0:
        old_model = copy.deepcopy(model)
        old_model = old_model.to(device)
        old_model.eval()
    else:
        old_model = None

    if args.track_centroid:
        if args.IL_version == 'OG':
            cntr_tracker = CentroidTracker(model, labeled_train_loader, args.num_labeled_classes, device,
                                               args.dataset_name,'IL', save_root=model_dir, mode=args.gen_centroid_mode)
        else:
            cntr_tracker = None
    else:
        cntr_tracker = None

    if args.mode == 'train':
        if args.IL_version == 'OG':
            # Our method: FRoST
            save_weight = model.head1.weight.data.clone()                       # save the weights of head-1
            save_bias = model.head1.bias.data.clone()                           # save the bias of head-1
            model.head1 = nn.Linear(512, num_classes).to(device)                # replace the labeled-class only head-1
                                                                                # with the head-1-new include nodes for
                                                                                # novel calsses
            model.head1.weight.data[:args.num_labeled_classes] = save_weight    # put the old weights into the old part
            model.head1.bias.data[:] = torch.min(save_bias) - 1.                # put the bias
            model.head1.bias.data[:args.num_labeled_classes] = save_bias

            if args.labeled_center > 0:
                class_mean, class_sig, class_cov = Generate_Center(model, labeled_train_loader, args)
            else:
                class_mean, class_sig, class_cov = None, None, None

            train_IL_center(model, old_model, mix_train_loader, labeled_eval_loader, unlabeled_eval_loader,
                            all_eval_loader, class_mean, class_sig, class_cov, args)
        # Below is ablation studies.
        elif args.IL_version == 'LwF':
            save_weight = model.head1.weight.data.clone()           # save the weights of head-1
            save_bias = model.head1.bias.data.clone()               # save the bias of head-1
            model.head1 = nn.Linear(512, num_classes).to(device)    # replace the labeled-class only head-1

            model.head1.weight.data[:args.num_labeled_classes] = save_weight    # put the old weights into the old part
            model.head1.bias.data[:] = torch.min(save_bias) - 1.                # put the bias
            model.head1.bias.data[:args.num_labeled_classes] = save_bias

            train_LwF(model, old_model, mix_train_loader, labeled_eval_loader, unlabeled_eval_loader, all_eval_loader, args)
        elif args.IL_version == 'LwFProto':
            save_weight = model.head1.weight.data.clone()           # save the weights of head-1
            save_bias = model.head1.bias.data.clone()               # save the bias of head-1
            model.head1 = nn.Linear(512, num_classes).to(device)    # replace the labeled-class only head-1

            model.head1.weight.data[:args.num_labeled_classes] = save_weight  # put the old weights into the old part
            model.head1.bias.data[:] = torch.min(save_bias) - 1.              # put the bias
            model.head1.bias.data[:args.num_labeled_classes] = save_bias

            if args.labeled_center > 0:
                class_mean, class_sig, class_cov = Generate_Center(model, labeled_train_loader, args)
            else:
                class_mean, class_sig, class_cov = None, None, None

            train_LwF_Proto(model, old_model, mix_train_loader, labeled_eval_loader, unlabeled_eval_loader,
                            all_eval_loader, class_mean, class_sig, class_cov, args)
        elif args.IL_version == 'JointHead1':
            frozen_layers = ['head2']
            freeze_layers(model, frozen_layers, True)

            save_weight = model.head1.weight.data.clone()           # save the weights of head-1
            save_bias = model.head1.bias.data.clone()               # save the bias of head-1
            model.head1 = nn.Linear(512, num_classes).to(device)    # replace the labeled-class only head-1

            model.head1.weight.data[:args.num_labeled_classes] = save_weight    # put the old weights into the old part
            model.head1.bias.data[:] = torch.min(save_bias) - 1.                # put the bias
            model.head1.bias.data[:args.num_labeled_classes] = save_bias

            if args.labeled_center > 0:
                class_mean, class_sig, class_cov = Generate_Center(model, labeled_train_loader, args)
            else:
                class_mean, class_sig, class_cov = None, None, None
            train_JointHead1(model, old_model, mix_train_loader, labeled_eval_loader, unlabeled_eval_loader,
                             all_eval_loader, class_mean, class_sig, class_cov, args)
        elif args.IL_version == 'JointHead1woPseudo':
            frozen_layers = ['head2']
            freeze_layers(model, frozen_layers, True)

            save_weight = model.head1.weight.data.clone()           # save the weights of head-1
            save_bias = model.head1.bias.data.clone()               # save the bias of head-1
            model.head1 = nn.Linear(512, num_classes).to(device)    # replace the labeled-class only head-1

            model.head1.weight.data[:args.num_labeled_classes] = save_weight    # put the old weights into the old part
            model.head1.bias.data[:] = torch.min(save_bias) - 1.                # put the bias
            model.head1.bias.data[:args.num_labeled_classes] = save_bias

            if args.labeled_center > 0:
                class_mean, class_sig, class_cov = Generate_Center(model, labeled_train_loader, args)
            else:
                class_mean, class_sig, class_cov = None, None, None

            train_JointHead1_woPseudo(model, old_model, mix_train_loader, labeled_eval_loader, unlabeled_eval_loader,
                                      all_eval_loader, class_mean, class_sig, class_cov, args)
        elif args.IL_version == 'SplitHead12':
            if args.labeled_center > 0:
                class_mean, class_sig, class_cov = Generate_Center(model, labeled_train_loader, args)
            else:
                class_mean, class_sig, class_cov = None, None, None

            train_SplitHead12(model, old_model, mix_train_loader, labeled_eval_loader, unlabeled_eval_loader,
                              all_eval_loader, class_mean, class_sig, class_cov, args)
        elif args.IL_version in ['OGwoPseudo','OGwoKDwoPseudo', 'OGwoProtowoPseudo', 'OGwoKDwoProtowoPseudo']:
            save_weight = model.head1.weight.data.clone()           # save the weights of head-1
            save_bias = model.head1.bias.data.clone()               # save the bias of head-1
            model.head1 = nn.Linear(512, num_classes).to(device)    # replace the labeled-class only head-1

            model.head1.weight.data[:args.num_labeled_classes] = save_weight    # put the old weights into the old part
            model.head1.bias.data[:] = torch.min(save_bias) - 1.                # put the bias
            model.head1.bias.data[:args.num_labeled_classes] = save_bias

            if args.labeled_center > 0:
                class_mean, class_sig, class_cov = Generate_Center(model, labeled_train_loader, args)
            else:
                class_mean, class_sig, class_cov = None, None, None

            train_OGwoPseudo(model, old_model, mix_train_loader, labeled_eval_loader, unlabeled_eval_loader,
                             all_eval_loader, class_mean, class_sig, class_cov, args)
        elif args.IL_version == 'AutoNovel':
            frozen_layers = ['head1']
            freeze_layers(model, frozen_layers, True)

            train_AutoNovel(model, old_model, mix_train_loader, labeled_eval_loader, unlabeled_eval_loader,
                            all_eval_loader, args)
        else: # EXPT: OGwoKD, OGwoProto, OGwoKDandProto
            save_weight = model.head1.weight.data.clone()           # save the weights of head-1
            save_bias = model.head1.bias.data.clone()               # save the bias of head-1
            model.head1 = nn.Linear(512, num_classes).to(device)    # replace the labeled-class only head-1

            model.head1.weight.data[:args.num_labeled_classes] = save_weight    # put the old weights into the old part
            model.head1.bias.data[:] = torch.min(save_bias) - 1.                # put the bias
            model.head1.bias.data[:args.num_labeled_classes] = save_bias

            if args.labeled_center > 0:
                class_mean, class_sig, class_cov = Generate_Center(model, labeled_train_loader, args)
            else:
                class_mean, class_sig, class_cov = None, None, None

            train_IL_center(model, old_model, mix_train_loader, labeled_eval_loader, unlabeled_eval_loader,
                            all_eval_loader, class_mean, class_sig, class_cov, args)

        torch.save(model.state_dict(), args.model_dir)
        print("model saved to {}.".format(args.model_dir))
    else:
        print("test the trained model only")
        if args.IL_version == 'OG':
            model.head1 = nn.Linear(512, num_classes).to(device)
            state_dict = torch.load(args.model_dir)
            model.load_state_dict(state_dict, strict=False)
        elif args.IL_version == 'LwF':
            model.head1 = nn.Linear(512, num_classes).to(device)
            state_dict = torch.load(args.model_dir)
            model.load_state_dict(state_dict, strict=False)
        elif args.IL_version == 'LwFProto':
            model.head1 = nn.Linear(512, num_classes).to(device)
            state_dict = torch.load(args.model_dir)
            model.load_state_dict(state_dict, strict=False)
        elif args.IL_version == 'JointHead1':
            model.head1 = nn.Linear(512, num_classes).to(device)
            state_dict = torch.load(args.model_dir)
            model.load_state_dict(state_dict, strict=False)
        elif args.IL_version == 'JointHead1woPseudo':
            model.head1 = nn.Linear(512, num_classes).to(device)
            state_dict = torch.load(args.model_dir)
            model.load_state_dict(state_dict, strict=False)
        elif args.IL_version == 'SplitHead12':
            state_dict = torch.load(args.model_dir)
            model.load_state_dict(state_dict, strict=False)
        elif args.IL_version in ['OGwoPseudo', 'OGwoKDwoPseudo', 'OGwoProtowoPseudo', 'OGwoKDwoProtowoPseudo']:
            model.head1 = nn.Linear(512, num_classes).to(device)
            state_dict = torch.load(args.model_dir)
            model.load_state_dict(state_dict, strict=False)
        elif args.IL_version == 'AutoNovel':
            state_dict = torch.load(args.model_dir)
            model.load_state_dict(state_dict, strict=False)
        else:  # OGwoKD, OGwoProto, OGwoKDandProto
            model.head1 = nn.Linear(512, num_classes).to(device)
            state_dict = torch.load(args.model_dir)
            model.load_state_dict(state_dict, strict=False)

# =============================== Final Test ===============================
    acc_list = []

    print('Head2: test on unlabeled classes')

    args.head = 'head2'
    _, ind = test(model, unlabeled_eval_loader, args, return_ind=True)

    print('Evaluating on Head1')
    args.head = 'head1'

    print('test on labeled classes (test split)')
    acc = test(model, labeled_eval_loader, args, cluster=False)
    acc_list.append(acc)

    print('test on unlabeled classes (test split)')
    acc = test(model, unlabeled_eval_loader_test, args, cluster=False, ind=ind)
    acc_list.append(acc)

    print('test on all classes w/o clustering (test split)')
    acc = test(model, all_eval_loader, args, cluster=False, ind=ind)
    acc_list.append(acc)

    print('test on all classes w/ clustering (test split)')
    acc = test(model, all_eval_loader, args, cluster=True)
    acc_list.append(acc)


    print('Evaluating on Head2')
    args.head = 'head2'

    print('test on unlabeled classes (train split)')
    acc = test(model, unlabeled_eval_loader, args)
    acc_list.append(acc)

    print('test on unlabeled classes (test split)')
    acc = test(model, unlabeled_eval_loader_test, args)
    acc_list.append(acc)

    if args.IL_version == 'JointHead1' or args.IL_version == 'JointHead1woPseudo':
        print('Acc List: Head1->Old, New, All_wo_cluster, All_w_cluster, Fake Head2(Head1[-num_unlabeled_classes:])->Train, Test')
        print(acc_list)
    elif args.IL_version == 'SplitHead12':
        print('Acc List: Synthesized Head1->Old, New, All_wo_cluster, All_w_cluster, Head2->Train, Test')
        print(acc_list)
    else:
        print('Acc List: Head1->Old, New, All_wo_cluster, All_w_cluster, Head2->Train, Test')
        print(acc_list)