from __future__ import annotations

import ast
import itertools
import logging
import os
import shutil
from argparse import Namespace
from copy import deepcopy
from pathlib import Path
from typing import Dict
from typing import TYPE_CHECKING
from typing import Union

import click
import numpy as np
import torch
from matplotlib import pyplot as plt
from pytorch_lightning.callbacks import ModelCheckpoint
from pytorch_lightning.logging import TestTubeLogger
from sklearn import metrics
from sklearn.metrics import auc
from sklearn.metrics import roc_auc_score
from torch import nn
from torch.utils.tensorboard import SummaryWriter
from torch.utils.tensorboard.summary import hparams
from torchvision.utils import make_grid

from forgery_detection.lightning.logging.confusion_matrix import confusion_matrix
from forgery_detection.lightning.logging.confusion_matrix import plot_cm
from forgery_detection.lightning.logging.confusion_matrix import plot_to_image
from forgery_detection.lightning.logging.const import CHECKPOINTS
from forgery_detection.lightning.logging.const import NAN_TENSOR
from forgery_detection.lightning.logging.const import RUNS
from forgery_detection.lightning.logging.const import SystemMode
from forgery_detection.lightning.logging.const import VAL_ACC

if TYPE_CHECKING:
    from forgery_detection.data.set import FileListDataset

logger = logging.getLogger(__file__)


class DictHolder(dict):
    """This just makes sure that the pytorch_lightning syntax works."""

    def __init__(self, kwargs: Union[dict, Namespace]):
        # if loading from checkpoint hparams will be a namespace
        if isinstance(kwargs, Namespace):
            kwargs = kwargs.__dict__
        if "cli" not in kwargs:
            kwargs["cli"] = self._construct_cli_arguments_from_hparams(kwargs)

        super().__init__(**kwargs)
        self.__dict__: dict = self

    def add_dataset_size(self, nb_samples: int, name: str):
        self[f"{name}_batches"] = (nb_samples // self["batch_size"]) * self[
            "val_check_interval"
        ]
        self[f"{name}_samples"] = nb_samples

    def add_class_weights(self, labels, weights):
        logger.info("Using class weights:")
        logger.info(self._class_weights_to_string(labels, weights))
        self["class_weights"] = {value[0]: value[1] for value in zip(labels, weights)}

    def add_nb_trainable_params(self, model: nn.Module):
        model_parameters = filter(lambda p: p.requires_grad, model.parameters())
        params = sum([np.prod(p.size()) for p in model_parameters])
        logger.info(f"Trainable params: " f"{params}")
        self["nb_trainable_params"] = params

    def to_dict(self):
        return dict(self)

    @staticmethod
    def _construct_cli_arguments_from_hparams(hparams: dict):
        hparams_copy = deepcopy(hparams)
        hparams_copy.pop("mode")

        cli_arguments = ""
        for key, value in hparams_copy.items():
            if isinstance(value, bool):
                if value:
                    cli_arguments += f" --{key}"
            elif isinstance(value, (int, float, dict, str, type(None), list)):
                cli_arguments += f" --{key}={value}"
            else:
                logger.warning(f"Not logging item_type {type(value)}.")
        return cli_arguments

    @staticmethod
    def _class_weights_to_string(labels: np.array, class_weights: np.array) -> str:
        return "\n".join(
            map(
                lambda value: f"{value[0]}:\t{value[1]:.3g}", zip(labels, class_weights)
            )
        )


def log_confusion_matrix(
    _logger, global_step, target: torch.tensor, pred: torch.tensor, class_to_idx
) -> Dict[str, torch.Tensor]:
    if len(class_to_idx) > 50:
        # assume that only the last 5 classes are relevant for logging
        disregarded_classes = len(class_to_idx) - 5
        class_to_idx = {
            x: class_to_idx[x] - disregarded_classes
            for x in list(class_to_idx.keys())[-5:]
        }
    cm = confusion_matrix(target, pred, num_classes=len(class_to_idx))

    figure = plot_cm(cm, class_names=class_to_idx.keys())

    cm_image = plot_to_image(figure)

    plt.close()
    _logger.experiment.add_image(
        "metrics/cm", cm_image, dataformats="HWC", global_step=global_step
    )

    # use cm to calculate class accuracies
    class_accuracies = cm.diagonal() / cm.sum(dim=1)
    class_accuracies_dict = {}
    for key, value in class_to_idx.items():
        class_accuracies_dict[str(key)] = class_accuracies[value]
    return class_accuracies_dict


def log_roc_graph(
    logger, global_step, target: torch.tensor, pred: torch.tensor, pos_label
) -> float:
    fpr, tpr, thresholds = metrics.roc_curve(target, pred, pos_label=pos_label)
    roc_auc = auc(fpr, tpr)
    figure = plt.figure(figsize=(8, 8))
    lw = 2
    plt.plot(
        fpr, tpr, color="darkorange", lw=lw, label="ROC curve (area = %0.2f)" % roc_auc
    )
    plt.plot([0, 1], [0, 1], color="navy", lw=lw, linestyle="--")
    plt.xlim([0.0, 1.0])
    plt.ylim([0.0, 1.05])
    plt.xlabel("False Positive Rate")
    plt.ylabel("True Positive Rate")
    plt.title(f"Receiver operating characteristic curve for label {pos_label}")
    plt.legend(loc="lower right")

    ax2 = plt.gca().twinx()
    ax2.plot(fpr, thresholds, markeredgecolor="r", linestyle="dashed", color="r")
    ax2.set_ylabel("Threshold", color="r")
    ax2.set_ylim([thresholds[-1], thresholds[0]])
    try:
        ax2.set_xlim([fpr[0], fpr[-1]])
    except ValueError:
        del ax2

    cm_image = plot_to_image(figure)
    plt.close()
    logger.experiment.add_image(
        "metrics/roc", cm_image, dataformats="HWC", global_step=global_step
    )
    return roc_auc


def multiclass_roc_auc_score(y_target, y_pred, label_binarizer):
    y_target = label_binarizer.transform(y_target)
    y_pred = label_binarizer.transform(y_pred)
    try:
        return roc_auc_score(y_target, y_pred)
    except ValueError:
        # if the batch size is quite small it can happen that there is only one class
        # present
        return NAN_TENSOR


def log_dataset_preview(
    dataset: FileListDataset, name: str, _logger: TestTubeLogger, nb_images=32, nrow=8
):
    np.random.seed(len(dataset))
    nb_datapoints = nb_images // dataset.sequence_length
    datapoints_idx = np.random.uniform(0, len(dataset), nb_datapoints).astype(int)
    # idx to sequence idxs
    datapoints_idx = map(
        lambda idx: range(
            dataset.samples_idx[idx] + 1 - dataset.sequence_length,
            dataset.samples_idx[idx] + 1,
        ),
        datapoints_idx,
    )
    # flatten
    datapoints_idx = list(itertools.chain.from_iterable(datapoints_idx))
    np.random.seed()

    datapoints, labels = list(
        zip(*(dataset[(idx, idx), idx] for idx in datapoints_idx))
    )

    # log labels
    try:
        labels = torch.tensor(labels, dtype=torch.float).reshape(
            (nb_images // nrow, nrow)
        ).unsqueeze(0) / (len(dataset.classes) - 1)
        _logger.experiment.add_image(name, labels, dataformats="CHW", global_step=0)
    except RuntimeError:
        logger.warning(
            f"there was a runtime error during logging labels. Probably because nrow"
            f"does not devide number of images to log."
        )
    # log images
    if isinstance(datapoints[0], tuple):
        # this means there is audio data as well
        audio = [x[1] for x in datapoints]
        audio = np.stack(audio, axis=0)
        if audio.shape[1] != 1:
            audio = np.expand_dims(audio, 1)
        audio -= audio.min()
        audio /= audio.max()
        try:
            _logger.experiment.add_image(name, audio, dataformats="NCHW", global_step=2)
        except AssertionError:
            logger.warning(
                f"Could not log preview for audio data due to assertion error."
            )
        datapoints = [x[0] for x in datapoints]

    datapoints = torch.stack(datapoints, dim=0)
    datapoints = datapoints.reshape(
        (-1, *datapoints.shape[-3:])
    )  # make sure it's b x c x w x h
    datapoints = make_grid(datapoints, nrow=nrow, range=(-1, 1), normalize=True)
    _logger.experiment.add_image(name, datapoints, dataformats="CHW", global_step=1)


def _map_to_loggable_hparam_types(hparams: dict) -> dict:
    for key, value in hparams.items():
        if isinstance(value, (type(None), list)):
            hparams[key] = str(value)
    return hparams


def _filter_loggable_hparams(hparams: dict) -> dict:
    return dict(
        filter(
            lambda item: isinstance(item[1], (int, float, str, bool)), hparams.items()
        )
    )


def log_hparams(
    hparam_dict: dict, metric_dict: dict, _logger: TestTubeLogger, global_step=None
):
    hparam_dict = _map_to_loggable_hparam_types(hparam_dict)
    hparam_dict = _filter_loggable_hparams(hparam_dict)
    _log_hparams(
        hparam_dict=hparam_dict,
        metric_dict=metric_dict,
        experiment=_logger.experiment,
        name="hparams",
        global_step=global_step,
    )


def _log_hparams(
    experiment, hparam_dict=None, metric_dict=None, name=None, global_step=None
):
    if type(hparam_dict) is not dict or type(metric_dict) is not dict:
        raise TypeError("hparam_dict and metric_dict should be dictionary.")

    # todo is it possible to use the default file_writer here?
    with SummaryWriter(
        log_dir=os.path.join(experiment.file_writer.get_logdir(), name)
    ) as w_hp:
        if global_step == 0:
            exp, ssi, sei = hparams(hparam_dict, metric_dict)
            w_hp.file_writer.add_summary(exp)
            w_hp.file_writer.add_summary(ssi)
            w_hp.file_writer.add_summary(sei)

        if global_step > 0:
            for k, v in metric_dict.items():
                # this needs to be added to the same summarywriter object as the hparams
                # either log hparams in the other summary writer object
                # or log after each epoch values in same summarywriter object as hparams
                if isinstance(v, dict):
                    w_hp.add_scalars(k, v, global_step=global_step)
                    logger.warning(
                        "Logging multiple scalars with dict will not work for"
                        "hparams and metrics. Because add_scalars generates new"
                        " filewriters but everything that should be shown in hparams"
                        "needs be written with the same filewriter."
                    )
                else:
                    w_hp.add_scalar(k, v, global_step=global_step)


def get_checkpoint(checkpoint_folder: Path, checkpoint_nr: int = -1) -> str:
    """Returns the latest checkpoint in given path.

    Raises FileNotFoundError if folder does not contain any .ckpt files."""

    checkpoints = sorted(
        checkpoint_folder.glob("*.ckpt"),
        key=lambda x: int(x.with_suffix("").name.split("_")[-1]),
    )
    if len(checkpoints) == 0:
        raise FileNotFoundError(
            f"Could not find any .ckpt files in {checkpoint_folder}"
        )

    checkpoint = str(checkpoints[checkpoint_nr])
    logger.info(f"Using {checkpoint} to load weights.")
    return checkpoint


def backwards_compatible_get_checkpoint(
    checkpoint_folder: Path, checkpoint_nr: int = -1
):
    try:
        return get_checkpoint(checkpoint_folder, checkpoint_nr)
    except FileNotFoundError:
        try:
            return get_checkpoint(checkpoint_folder / CHECKPOINTS, checkpoint_nr)
        except FileNotFoundError:
            raise FileNotFoundError(
                f"Could not find any .ckpt files in {checkpoint_folder} or "
                f"{checkpoint_folder/CHECKPOINTS}"
            )


class PythonLiteralOptionGPUs(click.Option):
    def type_cast_value(self, ctx, value):
        try:
            gpus = ast.literal_eval(value)
            if not isinstance(gpus, list):
                raise TypeError("gpus needs to be a list (i.e. [], [1], or [1,2].")
            gpus = 0 if len(gpus) == 0 else gpus
            return gpus
        except ValueError:
            raise click.BadParameter(value)


def get_logger_and_checkpoint_callback(
    log_dir, mode: SystemMode, debug, logger_info=None
):
    """Sets up a logger and a checkpointer.

    The code is mostly copied from pl.trainer.py.
    """
    if debug:
        name = "debug"
        description = ""
    else:
        log_dir = str(Path(log_dir) / RUNS / str(mode))
        if logger_info:
            name = logger_info["name"]
            description = logger_info["description"]
        else:
            # if the user provides a name create its own folder in the default folder
            name = click.prompt("Name of run", type=str, default="default").replace(
                " ", "_"
            )
            description = click.prompt("Description of run", type=str, default="")

    logger = TestTubeLogger(save_dir=log_dir, name=name, description=description)
    logger_dir = get_logger_dir(logger)

    checkpoint_callback = OldModelCheckpoint(
        filepath=logger_dir / CHECKPOINTS,
        save_best_only=False,
        monitor=VAL_ACC,
        mode="max",
        prefix="",
    )
    return checkpoint_callback, logger


def get_logger_dir(logger):
    return (
        Path(logger.save_dir)
        / logger.experiment.name
        / f"version_{logger.experiment.version}"
    )


class OldModelCheckpoint(ModelCheckpoint):
    """Save the model after every epoch.
    `filepath` can contain named formatting options,
    which will be filled the value of `epoch` and
    keys in `logs` (passed in `on_epoch_end`).
    For example: if `filepath` is `weights.{epoch:02d}-{val_loss:.2f}.hdf5`,
    then the model checkpoints will be saved with the epoch number and
    the validation loss in the filename.
    # Arguments
        filepath: string, path to save the model file.
        monitor: quantity to monitor.
        verbose: verbosity mode, 0 or 1.
        save_best_only: if `save_best_only=True`,
            the latest best model according to
            the quantity monitored will not be overwritten.
        mode: one of {auto, min, max}.
            If `save_best_only=True`, the decision
            to overwrite the current save file is made
            based on either the maximization or the
            minimization of the monitored quantity. For `val_acc`,
            this should be `max`, for `val_loss` this should
            be `min`, etc. In `auto` mode, the direction is
            automatically inferred from the name of the monitored quantity.
        save_weights_only: if True, then only the model's weights will be
            saved (`model.save_weights(filepath)`), else the full model
            is saved (`model.save(filepath)`).
        period: Interval (number of epochs) between checkpoints.
    """

    def __init__(
        self,
        filepath,
        monitor="val_loss",
        verbose=0,
        save_best_only=True,
        save_weights_only=False,
        mode="auto",
        period=1,
        prefix="",
    ):
        super(ModelCheckpoint, self).__init__()
        if save_best_only and os.path.isdir(filepath) and len(os.listdir(filepath)) > 0:
            logger.warn(
                f"Checkpoint directory {filepath} exists and is not empty with save_best_only=True."
                "All files in this directory will be deleted when a checkpoint is saved!"
            )

        self.monitor = monitor
        self.verbose = verbose
        self.filepath = filepath
        self.dirpath, self.filename = filepath, "{epoch}"
        self.save_best_only = save_best_only
        self.save_weights_only = save_weights_only
        self.period = period
        self.epochs_since_last_save = 0
        self.prefix = prefix
        self.save_top_k = -1
        self.epoch_last_check = None

        if mode not in ["auto", "min", "max"]:
            logger.warn(
                f"ModelCheckpoint mode {mode} is unknown, " "fallback to auto mode.",
                RuntimeWarning,
            )
            mode = "auto"

        if mode == "min":
            self.monitor_op = np.less
            self.best = np.Inf
        elif mode == "max":
            self.monitor_op = np.greater
            self.best = -np.Inf
        else:
            if "acc" in self.monitor or self.monitor.startswith("fmeasure"):
                self.monitor_op = np.greater
                self.best = -np.Inf
            else:
                self.monitor_op = np.less
                self.best = np.Inf

    def save_model(self, filepath, overwrite):
        dirpath = "/".join(filepath.split("/")[:-1])

        # make paths
        os.makedirs(os.path.dirname(filepath), exist_ok=True)

        if overwrite:
            for filename in os.listdir(dirpath):
                if self.prefix in filename:
                    path_to_delete = os.path.join(dirpath, filename)
                    try:
                        shutil.rmtree(path_to_delete)
                    except OSError:
                        os.remove(path_to_delete)

        # delegate the saving to the model
        self.save_function(filepath)

    def on_validation_end(self, trainer, pl_module):
        metrics = trainer.callback_metrics
        epoch = trainer.current_epoch
        self.epoch_last_check = epoch

        logs = metrics
        self.epochs_since_last_save += 1
        if self.epochs_since_last_save >= self.period:
            self.epochs_since_last_save = 0
            filepath = "{}/{}_ckpt_epoch_{}.ckpt".format(
                self.filepath, self.prefix, epoch + 1
            )
            if self.save_best_only:
                current = logs.get(self.monitor)
                if current is None:
                    logger.warn(
                        f"Can save best model only with {self.monitor} available,"
                        " skipping.",
                        RuntimeWarning,
                    )
                else:
                    if self.monitor_op(current, self.best):
                        if self.verbose > 0:
                            logging.info(
                                f"\nEpoch {epoch + 1:05d}: {self.monitor} improved"
                                f" from {self.best:0.5f} to {current:0.5f},"
                                f" saving model to {filepath}"
                            )
                        self.best = current
                        self.save_model(filepath, overwrite=True)

                    else:
                        if self.verbose > 0:
                            logging.info(
                                f"\nEpoch {epoch + 1:05d}: {self.monitor} did not improve"
                            )
            else:
                if self.verbose > 0:
                    logging.info(f"\nEpoch {epoch + 1:05d}: saving model to {filepath}")
                self.save_model(filepath, overwrite=False)
