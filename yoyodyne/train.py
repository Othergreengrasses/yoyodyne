"""Trains a sequence-to-sequence neural network."""

import argparse
import os

from typing import List, Optional

import pytorch_lightning as pl
from pytorch_lightning import callbacks, loggers
from torch.utils import data

from . import collators, datasets, evaluators, models, predict, util


# We treat this as fixed.
DATALOADER_WORKERS = 1


class Error(Exception):
    pass


def _make_logger(experiment: str, model_dir: str, wandb: bool) -> List:
    """Makes the logger(s).

    Args:
        experiment (str).
        model_dir (str).
        wandb (bool).

    Returns:
        List: logger.
    """
    trainer_logger = [loggers.CSVLogger(model_dir, name=experiment)]
    if wandb:
        trainer_logger.append(
            loggers.WandbLogger(project=experiment, log_model="all")
        )
    return trainer_logger


def _make_callbacks(save_top_k: int, patience: Optional[int] = None) -> List:
    """Makes the callbacks.

    We will reach into the callback metrics list to picks ckp_callback to find
    the best checkpoint path.

    Args:
        save_top_k (int).
        patience (int, optional).

    Returns:
        List: callbacks.
    """
    trainer_callbacks = [
        callbacks.ModelCheckpoint(
            save_top_k=save_top_k,
            monitor="val_accuracy",
            mode="max",
            filename="model-{epoch:02d}-{val_accuracy:.2f}",
        ),
        callbacks.LearningRateMonitor(logging_interval="epoch"),
        callbacks.TQDMProgressBar(),
    ]
    if patience is not None:
        trainer_callbacks.append(
            callbacks.early_stopping.EarlyStopping(
                monitor="val_accuracy",
                min_delta=0.0,
                patience=patience,
                verbose=False,
                mode="max",
            )
        )
    return trainer_callbacks


def make_trainer(
    experiment: str,
    model_dir: str,
    save_top_k: int,
    patience: Optional[int] = None,
    **kwargs,
) -> pl.Trainer:
    """Makes the trainer.

    Args:
        experiment (str).
        model_dir (str).
        patience (int, optional).
        save_top_k (int).
        wandb (bool).

    Returns:
        pl.Trainer.
    """
    return pl.Trainer(
        callbacks=_make_callbacks(save_top_k, patience),
        default_root_dir=model_dir,
        enable_checkpointing=True,
        logger=_make_logger(experiment, model_dir, wandb),
        **kwargs,
    )


def _make_trainer_from_argparse(
    args: argparse.Namespace,
) -> pl.Trainer:
    """Makes the trainer from an argparse namespace."""
    return pl.Trainer.from_argparse_args(
        args,
        callbacks=_make_callbacks(args.save_top_k, args.patience),
        default_root_dir=args.model_dir,
        enable_checkpointing=True,
        logger=_make_logger(args.experiment, args.model_dir, args.wandb),
    )


def make_loaders(
    train: str,
    dev: str,
    arch: str,
    batch_size: int,
) -> Tuple[data.DataLoader, data.DataLoader]:
    """Makes loaders.

    Args:
        train (str).
        dev (str).
        config (dataconfig.Dataconfig)
        arch (str).
        batch_size (int).
        experiment (str).

    Returns:
        Tuple[data.DataLoader, data.DataLoader].
    """
    if config.target_col == 0:
        raise Error("target_col must be specified for training")
    train_set = datasets.get_dataset(train, config)
    dev_set = datasets.get_dataset(dev, config)
    util.log_info(f"Source vocabulary: {train_set.source_symbol2i}")
    util.log_info(f"Target vocabulary: {train_set.target_symbol2i}")
    train_set.write_index(trainer.loggers[0].log_dir, experiment)
    dev_set.load_index(trainer.loggers[0].log_dir, experiment)
    collator = collators.get_collator(
        train_set.pad_idx, train_set.config, arch
    )
    train_loader = data.DataLoader(
        train_set,
        collate_fn=collator,
        batch_size=batch_size,
        shuffle=True,
        num_workers=DATALOADER_WORKERS,
    )
    dev_loader = data.DataLoader(
        dev_set,
        collate_fn=collator,
        batch_size=batch_size,
        shuffle=True,
        num_workers=DATALOADER_WORKERS,
    )
    return train_loader, dev_loader


def make_model(
    # Data arguments.
    train_set: torch.Dataset,
    train_from: str,
    *,
    # Architecture arguments.
    arch: str = "lstm",
    attention: bool = True,
    attention_heads: int = 4,
    bidirectional: bool = True,
    decoder_layers: int = 1,
    embedding_size: int = 128,
    encoder_layers: int = 1,
    hidden_size: int = 512,
    max_decode_length: int = 128,
    max_sequence_length: int = 128,
    # Training arguments.
    beta1: float = 0.9,
    beta2: float = 0.999,
    batch_size: int = 32,
    dropout: float = 0.2,
    learning_rate: float = 0.001,
    oracle_em_epochs: int = 5,
    oracle_factor: int = 1,
    optimizer: str = "adam",
    sed_params: Optional[str] = None,
    scheduler: Optional[str] = None,
    warmup_steps: int = 0,
) -> pl.LightningModule:
    model_cls = models.get_model_cls(arch, attention, train_set.has_features)
    if train_from is not None:
        # Restart training.
        util.log_info(f"Loading model from {train_from}")
        return model_cls.load_from_checkpoint(train_from)
    expert = (
        models.expert.get_expert(
            train_set,
            epochs=oracle_em_epochs,
            oracle_factor=oracle_factor,
            sed_params_path=sed_params_path,
        )
        if arch in ["transducer"]
        else None
    )
    # Please pass all arguments by keyword and keep in lexicographic order.
    return model_cls(
        arch=arch,
        attention_heads=attention_heads,
        beta1=beta1,
        beta2=beta2,
        bidirectional=bidirectional,
        decoder_layers=decoder_layers,
        dropout=dropout,
        embedding_size=embedding_size,
        encoder_layers=encoder_layers,
        end_idx=train_set.end_idx,
        evaluator=evaluators.Evaluator(),
        expert=expert,
        features_vocab_size=getattr(train_set, "features_vocab_size", -1),
        features_idx=getattr(train_set, "features_idx", -1),
        hidden_size=hidden_size,
        learning_rate=learning_rate,
        max_decode_length=max_decode_length,
        max_sequence_length=max_sequence_length,
        optimizer=optimizer,
        output_size=train_set.target_vocab_size,
        pad_idx=train_set.pad_idx,
        scheduler=scheduler,
        start_idx=train_set.start_idx,
        train_set=train_set,
        vocab_size=train_set.source_vocab_size,
        warmup_steps=warmup_steps,
    )


def train(
    trainer: pl.Trainer,
    model: pl.LightningModule,
    train_loader: data.DataLoader,
    dev_loader: data.DataLoader,
) -> str:
    """Trains the model.

    Args:
         trainer (pl.Trainer).
         model (pl.LightningModule).
         train_loader (data.DataLoader).
         dev_loader (data.DataLoader).

    Returns:
        (str) Path to best checkpoint.
    """
    util.log_info("Training...")
    trainer.fit(model, train_loader, dev_loader)
    print(trainer.callback_metrics)
    print(trainer.loggers)
    util.log_info("Training complete")
    return "FIXME"


def main() -> None:
    """Trainer."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--experiment", required=True, help="Name of experiment"
    )
    # Path arguments.
    parser.add_argument(
        "--train",
        required=True,
        help="Path to input training data TSV",
    )
    parser.add_argument(
        "--dev",
        required=True,
        help="Path to input development data TSV",
    )
    parser.add_argument(
        "--model_dir",
        required=True,
        help="Path to output model directory",
    )
    # FIXME: dev predictions.
    # Data configuration arguments.
    parser.add_argument(
        "--source_col",
        type=int,
        default=1,
        help="1-based index for source column. Default: %(default)s.",
    )
    parser.add_argument(
        "--target_col",
        type=int,
        default=2,
        help="1-based index for target column. Default: %(default)s.",
    )
    parser.add_argument(
        "--features_col",
        type=int,
        default=0,
        help="1-based index for features column; "
        "0 indicates the model will not use features. Default: %(default)s.",
    )
    parser.add_argument(
        "--source_sep",
        type=str,
        default="",
        help="String used to split source string into symbols; "
        "an empty string indicates that each Unicode codepoint "
        "is its own symbol. Default: %(default)r.",
    )
    parser.add_argument(
        "--target_sep",
        type=str,
        default="",
        help="String used to split target string into symbols; "
        "an empty string indicates that each Unicode codepoint "
        "is its own symbol. Default: %(default)r.",
    )
    parser.add_argument(
        "--features_sep",
        type=str,
        default=";",
        help="String used to split features string into symbols; "
        "an empty string indicates that each Unicode codepoint "
        "is its own symbol. Default: %(default)r.",
    )
    parser.add_argument(
        "--tied_vocabulary",
        action="store_true",
        default=True,
        help="Share source and target embeddings. Default: %(default)s.",
    )
    parser.add_argument(
        "--no_tied_vocabulary",
        action="store_false",
        dest="tied_vocabulary",
        default=True,
    )
    # Architecture arguments.
    parser.add_argument(
        "--arch",
        choices=[
            "feature_invariant_transformer",
            "lstm",
            "pointer_generator_lstm",
            "transducer",
            "transformer",
        ],
        default="lstm",
        help="Model architecture to use",
    )
    parser.add_argument(
        "--attention",
        action="store_true",
        default=True,
        help="Uses attention (LSTM architecture only). Default: %(default)s.",
    )
    parser.add_argument(
        "--no_attention", action="store_false", dest="attention"
    )
    parser.add_argument(
        "--attention_heads",
        type=int,
        default=4,
        help="Number of attention heads "
        "(transformer-backed architectures only. Default: %(default)s.",
    )
    parser.add_argument(
        "--bidirectional",
        action="store_true",
        default=True,
        help="Uses a bidirectional encoder "
        "(LSTM-backed architectures only. Default: %(default)s.",
    )
    parser.add_argument(
        "--no_bidirectional",
        action="store_false",
        dest="bidirectional",
    )
    parser.add_argument(
        "--decoder_layers",
        type=int,
        default=1,
        help="Number of decoder layers. Default: %(default)s",
    )
    parser.add_argument(
        "--embedding_size",
        type=int,
        default=128,
        help="Dimensionality of embeddings. Default: %(default)s",
    )
    parser.add_argument(
        "--encoder_layers",
        type=int,
        default=1,
        help="Number of encoder layers. Default: %(default)s",
    )
    parser.add_argument(
        "--hidden_size",
        type=int,
        default=512,
        help="Dimensionality of the hidden layer(s). Default: %(default)s",
    )
    parser.add_argument(
        "--max_decode_len",
        type=int,
        default=128,
        help="Maximum decoder string length. Default: %(default)s",
    )
    parser.add_argument(
        "--max_sequence_len",
        type=int,
        default=128,
        help="Maximum sequence length. Default: %(default)s",
    )
    # Training arguments.
    parser.add_argument(
        "--batch_size",
        type=int,
        default=128,
        help="Batch size. Default: %(default)s",
    )
    parser.add_argument(
        "--beta1",
        type=float,
        default=0.9,
        help="beta_1 (Adam optimizer only). Default: %(default)s",
    )
    parser.add_argument(
        "--beta2",
        type=float,
        default=0.999,
        help="beta_2 (Adam optimizer only). Default: %(default)s",
    )
    parser.add_argument(
        "--dropout",
        type=float,
        default=0.2,
        help="Dropout probability. Default: %(default)s",
    )
    parser.add_argument(
        "--label_smoothing",
        type=float,
        help="Coefficient for label smoothing",
    )
    parser.add_argument(
        "--learning_rate",
        type=float,
        default=0.001,
        help="Learning rate. Default: %(default)s",
    )
    parser.add_argument(
        "--oracle_em_epochs",
        type=int,
        default=5,
        help="Number of EM epochs "
        "(transducer architecture only. Default: %(default)s",
    )
    parser.add_argument(
        "--oracle_factor",
        type=int,
        default=1,
        help="Roll-in schedule parameter "
        "(transducer architecture only. Default: %(default)s",
    )
    parser.add_argument(
        "--optimizer",
        choices=["adadelta", "adam", "sgd"],
        default="adam",
        help="Optimizer. Default: %(default)s",
    )
    parser.add_argument(
        "--patience", type=int, help="Patience for early stopping"
    )
    parser.add_argument(
        "--save_top_k",
        type=int,
        default=1,
        help="Number of checkpoints to save. Default: %(default)s",
    )
    parser.add_argument(
        "--scheduler",
        choices=["warmupinvsqrt"],
        help="Learning rate scheduler",
    )
    parser.add_argument(
        "--sed_path",
        type=str,
        help="Path to input SED parameters (transducer architecture only)",
    )
    parser.add_argument("--seed", type=int, help="Random seed")
    parser.add_argument(
        "--wandb",
        action="store_true",
        default=False,
        help="Use Weights & Biases logging (log-in required). Default: True",
    )
    parser.add_argument(
        "--no_wandb",
        action="store_false",
        dest="wandb",
    )
    parser.add_argument(
        "--warmup_steps",
        type=int,
        default=0,
        help="Number of warmup steps (warmupinvsqrt scheduler only). "
        "Default: %(default)s",
    )
    # Among the things this adds, the following are likely to be useful to users:
    # --accelerator ("gpu" for GPU)
    # --check_val_every_n_epoch
    # --devices (for multiple device support)
    # --gradient_clip_val
    # --max_epochs
    # --min_epochs
    # --max_steps
    # --min_steps
    # --max_time
    pl.Trainer.add_argparse_args(parser)
    args = parser.parse_args()
    util.log_info("Arguments:")
    for arg, val in vars(args).items():
        if val is None:
            continue
        util.log_info(f"\t{arg}: {val!r}")
    pl.seed_everything(args.seed)
    trainer = _make_trainer_from_argparse(args)
    model = make_model(**vars(args))
    config = dataconfig.DataConfig(
        source_col=args.source_col,
        features_col=args.features_col,
        target_col=args.target_col,
        source_sep=args.source_sep,
        target_sep=args.target_sep,
        features_sep=args.features_sep,
        tied_vocabulary=args.tied_vocabulary,
    )
    train_loader, dev_loader = make_loaders(
        args.train, args.dev, config, args.arch, args.batch_size
    )
    train(trainer, model, train_loader, dev_loader)
