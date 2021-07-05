from model.anchor import Anchors
import os 
import time
import wandb
import torch
import argparse
import numpy as np
from torch import optim
from torch.utils.data import DataLoader, dataloader

from model.config import *
from utils.mlops_tool import use_data_wandb
from model.model import RetinaFace
from utils.data_tool import create_exp_dir
from model.multibox_loss import MultiBoxLoss
from utils.dataset import WiderFaceDataset, detection_collate


def parse_args():
    """parse command line arguments"""
    parser = argparse.ArgumentParser(description='Train image segmentation')
    parser.add_argument('--run', type=str, default=RUN_NAME, help="run name")
    parser.add_argument('--epoch', type=int, default=EPOCHS, help="number of epoch")
    parser.add_argument('--model', type=str, default='mobilenet0.25', help='select model')
    parser.add_argument('--weight', type=str, default=None, help='path to pretrained weight')
    parser.add_argument('--weight_decay', type=int, default=WEIGHT_DECAY, help="weight decay of optimizer")
    parser.add_argument('--momentum', type=int, default=MOMENTUM, help="momemtum of optimizer")
    parser.add_argument('--startfm', type=int, default=START_FRAME, help="architecture start frame")
    parser.add_argument('--batchsize', type=int, default=BATCH_SIZE, help="total batch size for all GPUs (default:")
    parser.add_argument('--lr', type=float, default=LEARNING_RATE, help="init learning rate (default: 0.0001)")
    parser.add_argument('--download', action='store_true', help="download dataset from Wandb Database")
    parser.add_argument('--tuning', action='store_true', help="no plot image for tuning")

    args = parser.parse_args()
    return args

def train(model, anchors, trainloader, optimizer, loss_function, best_ap, device='cpu'):
    model.train()
    loss_cls, loss_box, loss_pts = 0, 0, 0
    epoch_ap = 0
    for i, (input, targets) in enumerate(trainloader):
        # load data into cuda
        input   = input.to(device)
        targets = [annos.to(device) for annos in targets]

        # forward
        predict = model(input)
        loss_l, loss_c, loss_landm = loss_function(predict, anchors, targets)
        loss = 1.3*loss_l + loss_c + loss_landm

        # metric
        loss_cls += loss_c
        loss_box += loss_l 
        loss_pts += loss_landm

        # zero the gradient + backprpagation + step
        optimizer.zero_grad()

        loss.backward()
        optimizer.step()
    
    # cls = classification; box = box regressionl; pts = landmark regression
    loss_cls = loss_cls/len(trainloader)
    loss_box = loss_box/len(trainloader)
    loss_pts = loss_pts/len(trainloader)

    if epoch_ap>best_ap:
        # export to onnx + pt
        torch.onnx.export(model, input, os.path.join(SAVE_PATH+args.run+'.onnx'))
        torch.save(model.state_dict(), os.path.join(SAVE_PATH+args.run+'.pth'))

    return loss_cls, loss_box, loss_pts, epoch_ap

if __name__ == '__main__':
    args = parse_args()

    # init wandb
    config = dict(
        epoch           = args.epoch,
        weight_decay    = args.weight_decay,
        momentum        = args.momentum,
        lr              = args.lr,
        batchsize       = args.batchsize,
        startfm         = args.startfm,
    )
    
    # log experiments to
    run = wandb.init(project=PROJECT, config=config, entity='nmd2000')
    
    # use artifact
    use_data_wandb(run=run, data_name=DATASET, download=args.download)

    # train on device
    device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
    print(f"\tCurrent training device {torch.cuda.get_device_name(device)}".expandtabs(4))

    # get dataloader
    train_set = WiderFaceDataset(root_path=DATA_PATH, is_train=True)
    valid_set = WiderFaceDataset(root_path=DATA_PATH, is_train=False)
    
    print(f"\tNumber of training example: {len(train_set)}\n\tNumber of validation example: {len(valid_set)}".expandtabs(4))

    torch.manual_seed(RANDOM_SEED)

    trainloader = DataLoader(train_set, batch_size=BATCH_SIZE, shuffle=True, num_workers=NUM_WORKERS, collate_fn=detection_collate)
    validloader = DataLoader(valid_set, batch_size=BATCH_SIZE, shuffle=False, num_workers=NUM_WORKERS, collate_fn=detection_collate)

    n_classes = N_CLASSES
    epochs = args.epoch
    # create dir for save weight
    save_dir = create_exp_dir()

    # get model and define loss func, optimizer
    model = RetinaFace(model_name=args.model).to(device)

    with torch.no_grad():
        anchors = Anchors(pyramid_levels=model.feature_map).forward().to(device)

    # optimizer + citeration
    optimizer   = optim.SGD(model.parameters(), lr=args.lr, momentum=args.momentum, weight_decay=args.weight_decay)
    criterion   = MultiBoxLoss(N_CLASSES, 
                    overlap_thresh=OVERLAP_THRES, 
                    prior_for_matching=True, 
                    bkg_label=BKG_LABEL, neg_pos=True, 
                    neg_mining=NEG_MINING, neg_overlap=NEG_OVERLAP, 
                    encode_target=False, device=device)

    scheduler = torch.optim.lr_scheduler.MultiStepLR(optimizer, milestones=LR_MILESTONE, gamma=0.7)

    # wandb watch
    run.watch(models=model, criterion=criterion, log='all', log_freq=10)

    # training
    best_ap = -1

    for epoch in range(epochs):
        print(f'\tEpoch\tbox\t\tlandmarks\tcls\t\ttotal'.expandtabs(4))
        t0 = time.time()
        loss_box, loss_pts, loss_cls, train_ap = train(model, anchors, trainloader, optimizer, criterion, best_ap, device)
        t1 = time.time()

        total_loss = loss_box + loss_pts + loss_cls
        wandb.log({'loss_cls': loss_cls, 'loss_box': loss_box, 'loss_landmark': loss_pts}, step=epoch)
        print(f'\t{epoch}/{epochs}\t{loss_box:.5f}\t\t{loss_pts:.5f}\t\t{loss_cls:.5f}\t\t{total_loss:.5f}\t\t{(t1-t0):.2f}s'.expandtabs(4))
        
        # summary
        # print(f'\tImages\tLabels\t\tP\t\tR\t\tmAP@.5\t\tmAP.5.95')
        # images, labels, P, R, map_5, map_95
        # print(f'\t{images}\t{labels}\t\t{P}\t\t{R}\t\t{map_5}\t\t{map_95}')
    
        wandb.log({"lr": scheduler.get_last_lr()[0]}, step=epoch)
        
        # decrease lr
        scheduler.step()

        # Wandb summary
        if train_ap > best_ap:
            best_ap = train_ap
            wandb.run.summary["best_accuracy"] = best_ap

    if not args.tuning:
        trained_weight = wandb.Artifact(args.run, type='weights')
        trained_weight.add_file(SAVE_PATH+args.run+'.onnx')
        trained_weight.add_file(SAVE_PATH+args.run+'.pth')
        wandb.log_artifact(trained_weight)