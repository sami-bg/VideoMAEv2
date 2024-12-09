    # --------------------------------------------------------
# Based on BEiT, timm, DINO and DeiT code bases
# https://github.com/microsoft/unilm/tree/master/beit
# https://github.com/rwightman/pytorch-image-models/tree/master/timm
# https://github.com/facebookresearch/deit
# https://github.com/facebookresearch/dino
# --------------------------------------------------------'
import math
import sys
import itertools
from typing import Iterable

import torch
from einops import rearrange
from timm.data.constants import IMAGENET_DEFAULT_MEAN, IMAGENET_DEFAULT_STD

import utils


def train_one_epoch(model: torch.nn.Module,
                    data_loader: Iterable,
                    optimizer: torch.optim.Optimizer,
                    device: torch.device,
                    epoch: int,
                    ipe: int | None,
                    loss_scaler,
                    max_norm: float = 0,
                    patch_size: int = 16,
                    normlize_target: bool = True,
                    log_writer=None,
                    lr_scheduler=None,
                    start_steps=None,
                    lr_schedule_values=None,
                    wd_schedule_values=None):
    
    ipe: int = ipe or len(data_loader)
    
    model.train()
    metric_logger = utils.MetricLogger(delimiter="  ")
    metric_logger.add_meter(
        'lr', utils.SmoothedValue(window_size=1, fmt='{value:.6f}'))
    metric_logger.add_meter(
        'min_lr', utils.SmoothedValue(window_size=1, fmt='{value:.6f}'))
    metric_logger.add_meter(
        'rankme', utils.SmoothedValue(window_size=utils.rankme().limit, fmt='{value:.6f}'))
    header = 'Epoch: [{}]'.format(epoch)
    print_freq = 20
        
    itr = 0
    while itr == 0 or itr % ipe != 0: 
        # either:   keep refreshing dataloader until we hit ipe
        # or:       break out of dataloader early once ipe is hit
        data_loader = iter(data_loader)
        print(f'attempting to refresh dataloader')
        for step, batch in enumerate(metric_logger.log_every(data_loader, print_freq, header)):
            if itr % ipe == 0:
                # we hit ipe, can stop refreshing dataloader 
                break
            # assign learning rate & weight decay for each step
            it = start_steps + step  # global training iteration
            if lr_schedule_values is not None or wd_schedule_values is not None:
                for i, param_group in enumerate(optimizer.param_groups):
                    if lr_schedule_values is not None:
                        param_group["lr"] = lr_schedule_values[it] * param_group[
                            "lr_scale"]
                    if wd_schedule_values is not None and param_group[
                            "weight_decay"] > 0:
                        param_group["weight_decay"] = wd_schedule_values[it]

            # NOTE: When the decoder mask ratio is 0,
            # in other words, when decoder masking is not used,
            # decode_masked_pos = ~bool_masked_pos
            images, bool_masked_pos, decode_masked_pos = batch

            images = images.to(device, non_blocking=True)
            bool_masked_pos = bool_masked_pos.to(
                device, non_blocking=True).flatten(1).to(torch.bool)
            decode_masked_pos = decode_masked_pos.to(
                device, non_blocking=True).flatten(1).to(torch.bool)

            with torch.no_grad():
                # calculate the predict label
                mean = torch.as_tensor(IMAGENET_DEFAULT_MEAN).to(device)[None, :,
                                                                        None,
                                                                        None,
                                                                        None]
                std = torch.as_tensor(IMAGENET_DEFAULT_STD).to(device)[None, :,
                                                                    None, None,
                                                                    None]
                unnorm_images = images * std + mean  # in [0, 1]

                if normlize_target:
                    images_squeeze = rearrange(
                        unnorm_images,
                        'b c (t p0) (h p1) (w p2) -> b (t h w) (p0 p1 p2) c',
                        p0=2,
                        p1=patch_size,
                        p2=patch_size)
                    images_norm = (images_squeeze - images_squeeze.mean(
                        dim=-2, keepdim=True)) / (
                            images_squeeze.var(
                                dim=-2, unbiased=True, keepdim=True).sqrt() + 1e-6)
                    images_patch = rearrange(images_norm, 'b n p c -> b n (p c)')
                else:
                    images_patch = rearrange(
                        unnorm_images,
                        'b c (t p0) (h p1) (w p2) -> b (t h w) (p0 p1 p2 c)',
                        p0=2,
                        p1=patch_size,
                        p2=patch_size)

                B, N, C = images_patch.shape
                labels = images_patch[~decode_masked_pos].reshape(B, -1, C)

            if loss_scaler is None:
                outputs = model(images, bool_masked_pos, decode_masked_pos)
                loss = (outputs - labels)**2
                loss = loss.mean(dim=-1)
                cal_loss_mask = bool_masked_pos[~decode_masked_pos].reshape(B, -1)
                loss = (loss * cal_loss_mask).sum() / cal_loss_mask.sum()
            else:
                with torch.cuda.amp.autocast(dtype=torch.bfloat16):
                    outputs = model(images, bool_masked_pos, decode_masked_pos)
                    loss = (outputs - labels)**2
                    loss = loss.mean(dim=-1)
                    cal_loss_mask = bool_masked_pos[~decode_masked_pos].reshape(
                        B, -1)
                    loss = (loss * cal_loss_mask).sum() / cal_loss_mask.sum()

            loss_value = loss.item()

            print(f"{epoch=} {step=}/{ipe} loss={loss_value}")
            if not math.isfinite(loss_value):
                print("Loss is {}, stopping training".format(loss_value))
                sys.exit(2)

            optimizer.zero_grad()

            if loss_scaler is None:
                loss.backward()
                if max_norm is None:
                    grad_norm = utils.get_grad_norm_(model.parameters())
                else:
                    grad_norm = torch.nn.utils.clip_grad_norm_(
                        model.parameters(), max_norm)
                optimizer.step()
                loss_scale_value = 0
            else:
                # this attribute is added by timm on one optimizer (adahessian)
                is_second_order = hasattr(
                    optimizer, 'is_second_order') and optimizer.is_second_order
                grad_norm = loss_scaler(
                    loss,
                    optimizer,
                    clip_grad=max_norm,
                    parameters=model.parameters(),
                    create_graph=is_second_order)
                loss_scale_value = loss_scaler.state_dict()["scale"]

            torch.cuda.synchronize()

            metric_logger.update(loss=loss_value)
            metric_logger.update(loss_scale=loss_scale_value)
            min_lr = 10.
            max_lr = 0.
            for group in optimizer.param_groups:
                min_lr = min(min_lr, group["lr"])
                max_lr = max(max_lr, group["lr"])

            metric_logger.update(lr=max_lr)
            metric_logger.update(min_lr=min_lr)
            weight_decay_value = None
            for group in optimizer.param_groups:
                if group["weight_decay"] > 0:
                    weight_decay_value = group["weight_decay"]
            metric_logger.update(weight_decay=weight_decay_value)
            metric_logger.update(grad_norm=grad_norm)
            metric_logger.update(rankme=utils.rankme().latest())

            if log_writer is not None:
                log_writer.update(loss=loss_value, head="loss")
                log_writer.update(loss_scale=loss_scale_value, head="opt")
                log_writer.update(lr=max_lr, head="opt")
                log_writer.update(min_lr=min_lr, head="opt")
                log_writer.update(weight_decay=weight_decay_value, head="opt")
                log_writer.update(grad_norm=grad_norm, head="opt")
                log_writer.update(rankme=utils.rankme().latest())
                log_writer.set_step()

            if lr_scheduler is not None:
                lr_scheduler.step_update(start_steps + step)
            itr += 1

    # gather the stats from all processes
    metric_logger.synchronize_between_processes()
    print("Averaged stats:", metric_logger)
    return {k: meter.global_avg for k, meter in metric_logger.meters.items()}
