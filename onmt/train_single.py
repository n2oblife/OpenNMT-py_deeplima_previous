#!/usr/bin/env python
"""Training on a single process."""
import torch
import sys
from trankit.tpipeline import TaggerDataset, TPipeline
from .utils.logging import init_logger, logger
from .utils.parse import ArgumentParser
from .constants import CorpusTask
from .transforms import (
    make_transforms,
    save_transforms,
    get_specials,
    get_transforms_cls,
)
from .inputters import build_vocab, IterOnDevice
from .inputters.inputter import dict_to_vocabs, vocabs_to_dict
from .inputters.dynamic_iterator import build_dynamic_dataset_iter
from .inputters.text_corpus import save_transformed_sample
from .model_builder import build_model
from .models.model_saver import load_checkpoint
from .utils.optimizers import Optimizer
from .utils.misc import set_random_seed
from .trainer import build_trainer
from .models import build_model_saver
from .modules.embeddings import prepare_pretrained_embeddings
from onmt.utils.trankit_utils import TConfig

def prepare_transforms_vocabs(opt, transforms_cls):
    """Prepare or dump transforms before training."""
    # if transform + options set in 'valid' we need to copy in main
    # transform / options for scoring considered as inference
    validset_transforms = opt.data.get("valid", {}).get("transforms", None)
    if validset_transforms:
        opt.transforms = validset_transforms
        if opt.data.get("valid", {}).get("tgt_prefix", None):
            opt.tgt_prefix = opt.data.get("valid", {}).get("tgt_prefix", None)
        if opt.data.get("valid", {}).get("src_prefix", None):
            opt.src_prefix = opt.data.get("valid", {}).get("src_prefix", None)
        if opt.data.get("valid", {}).get("tgt_suffix", None):
            opt.tgt_suffix = opt.data.get("valid", {}).get("tgt_suffix", None)
        if opt.data.get("valid", {}).get("src_suffix", None):
            opt.src_suffix = opt.data.get("valid", {}).get("src_suffix", None)
    specials = get_specials(opt, transforms_cls)

    vocabs = build_vocab(opt, specials)

    # maybe prepare pretrained embeddings, if any
    prepare_pretrained_embeddings(opt, vocabs)

    if opt.dump_transforms or opt.n_sample != 0:
        transforms = make_transforms(opt, transforms_cls, vocabs)
    if opt.dump_transforms:
        save_transforms(transforms, opt.save_data, overwrite=opt.overwrite)
    if opt.n_sample != 0:
        logger.warning(
            "`-n_sample` != 0: Training will not be started. "
            f"Stop after saving {opt.n_sample} samples/corpus."
        )
        save_transformed_sample(opt, transforms, n_sample=opt.n_sample)
        logger.info("Sample saved, please check it before restart training.")
        sys.exit()
    logger.info(
        "The first 10 tokens of the vocabs are:"
        f"{vocabs_to_dict(vocabs)['src'][0:10]}"
    )
    logger.info(f"The decoder start token is: {opt.decoder_start_token}")
    return vocabs


def _init_train(opt):
    """Common initilization stuff for all training process.
    We need to build or rebuild the vocab in 3 cases:
    - training from scratch (train_from is false)
    - resume training but transforms have changed
    - resume training but vocab file has been modified
    """
    ArgumentParser.validate_prepare_opts(opt)
    transforms_cls = get_transforms_cls(opt._all_transform)
    if opt.train_from:
        # Load checkpoint if we resume from a previous training.
        checkpoint = load_checkpoint(ckpt_path=opt.train_from)
        vocabs = dict_to_vocabs(checkpoint["vocab"])
        if (
            hasattr(checkpoint["opt"], "_all_transform")
            and len(
                opt._all_transform.symmetric_difference(
                    checkpoint["opt"]._all_transform
                )
            )
            != 0
        ):
            _msg = "configured transforms is different from checkpoint:"
            new_transf = opt._all_transform.difference(checkpoint["opt"]._all_transform)
            old_transf = checkpoint["opt"]._all_transform.difference(opt._all_transform)
            if len(new_transf) != 0:
                _msg += f" +{new_transf}"
            if len(old_transf) != 0:
                _msg += f" -{old_transf}."
            logger.warning(_msg)
            vocabs = prepare_transforms_vocabs(opt, transforms_cls)
        if opt.update_vocab:
            logger.info("Updating checkpoint vocabulary with new vocabulary")
            vocabs = prepare_transforms_vocabs(opt, transforms_cls)
    else:
        checkpoint = None
        vocabs = prepare_transforms_vocabs(opt, transforms_cls)

    return checkpoint, vocabs, transforms_cls


def configure_process(opt, device_id):
    if device_id >= 0:
        torch.cuda.set_device(device_id)
    set_random_seed(opt.seed, device_id >= 0)


def _get_model_opts(opt, checkpoint=None):
    """Get `model_opt` to build model, may load from `checkpoint` if any."""
    if checkpoint is not None:
        model_opt = ArgumentParser.ckpt_model_opts(checkpoint["opt"])
        if opt.override_opts:
            logger.info("Over-ride model option set to true - use with care")
            args = list(opt.__dict__.keys())
            model_args = list(model_opt.__dict__.keys())
            for arg in args:
                if arg in model_args and getattr(opt, arg) != getattr(model_opt, arg):
                    logger.info(
                        "Option: %s , value: %s overriding model: %s"
                        % (arg, getattr(opt, arg), getattr(model_opt, arg))
                    )
            model_opt = opt
        else:
            model_opt = ArgumentParser.ckpt_model_opts(checkpoint["opt"])
            ArgumentParser.update_model_opts(model_opt)
            ArgumentParser.validate_model_opts(model_opt)
            if opt.tensorboard_log_dir == model_opt.tensorboard_log_dir and hasattr(
                model_opt, "tensorboard_log_dir_dated"
            ):
                # ensure tensorboard output is written in the directory
                # of previous checkpoints
                opt.tensorboard_log_dir_dated = (
                    model_opt.tensorboard_log_dir_dated
                )  # noqa: E501
            # Override checkpoint's update_embeddings as it defaults to false
            model_opt.update_vocab = opt.update_vocab
            # Override checkpoint's freezing settings as it defaults to false
            model_opt.freeze_encoder = opt.freeze_encoder
            model_opt.freeze_decoder = opt.freeze_decoder
    else:
        model_opt = opt
    return model_opt


def main(opt, device_id):
    """Start training on `device_id`."""
    # NOTE: It's important that ``opt`` has been validated and updated
    # at this point.

    configure_process(opt, device_id)
    init_logger(opt.log_file)
    checkpoint, vocabs, transforms_cls = _init_train(opt)
    model_opt = _get_model_opts(opt, checkpoint=checkpoint)

    # Build model.
    model = build_model(model_opt, opt, vocabs, checkpoint)

    model.count_parameters(log=logger.info)
    trainable = {
        "torch.float32": 0,
        "torch.float16": 0,
        "torch.uint8": 0,
        "torch.int8": 0,
    }
    non_trainable = {
        "torch.float32": 0,
        "torch.float16": 0,
        "torch.uint8": 0,
        "torch.int8": 0,
    }
    for n, p in model.named_parameters():
        if p.requires_grad:
            trainable[str(p.dtype)] += p.numel()
        else:
            non_trainable[str(p.dtype)] += p.numel()
    logger.info("Trainable parameters = %s" % str(trainable))
    logger.info("Non trainable parameters = %s" % str(non_trainable))
    logger.info(" * src vocab size = %d" % len(vocabs["src"]))
    logger.info(" * tgt vocab size = %d" % len(vocabs["tgt"]))
    if "src_feats" in vocabs:
        for i, feat_vocab in enumerate(vocabs["src_feats"]):
            logger.info(f"* src_feat {i} vocab size = {len(feat_vocab)}")

    # Build optimizer.
    optim = Optimizer.from_opt(model, opt, checkpoint=checkpoint)

    del checkpoint

    # Build model saver
    model_saver = build_model_saver(model_opt, opt, model, vocabs, optim)

    trainer = build_trainer(
        opt, device_id, model, vocabs, optim, model_saver=model_saver
    )

    if opt.trankit :
        # tester sur un fichier a part
        # tconfig might need some adjustements from the 
        # TPipeline._setup_config() function
        # tconfig = TConfig(opt)

        # train_iter = TaggerDataset(
        #     config=tconfig,
        #     input_conllu=tconfig.train_conllu_fpath,
        #     gold_conllu=tconfig.train_conllu_fpath,
        #     evaluate=False
        # )
        # train_iter.numberize()

        # valid_iter = TaggerDataset(
        #     config=tconfig,
        #     input_conllu=tconfig.dev_conllu_fpath,
        #     gold_conllu=tconfig.dev_conllu_fpath,
        #     evaluate=False
        # )
        # valid_iter.numberize()

        train_iter = model.decoder.train_set
        valid_iter = model.decoder.dev_set
    else :
        _train_iter = _build_train_iter(
            opt,
            transforms_cls,
            vocabs,
            stride=max(1, len(opt.gpu_ranks)),
            offset=max(0, device_id),
        )
        train_iter = IterOnDevice(_train_iter, device_id)
        valid_iter = _build_valid_iter(opt, transforms_cls, vocabs)
        if valid_iter is not None:
            valid_iter = IterOnDevice(valid_iter, device_id)
        
    if len(opt.gpu_ranks):
        logger.info("Starting training on GPU: %s" % opt.gpu_ranks)
    else:
        logger.info("Starting training on CPU, could be very slow")
    train_steps = opt.train_steps
    if opt.single_pass and train_steps > 0:
        logger.warning("Option single_pass is enabled, ignoring train_steps.")
        train_steps = 0

    trainer.train(
        train_iter,
        train_steps,
        save_checkpoint_steps=opt.save_checkpoint_steps,
        valid_iter=valid_iter,
        valid_steps=opt.valid_steps,
        trankit = opt.trankit,
        epoch=opt.max_epoch
    )

    if trainer.report_manager.tensorboard_writer is not None:
        trainer.report_manager.tensorboard_writer.close()
