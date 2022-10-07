"Supervised loss benchmark"
import argparse
import gc
import json
import os
import shutil

import keras_cv
import numpy as np
import tensorflow as tf
import tensorflow.keras.backend
import tensorflow.random
from tabulate import tabulate
from tensorflow.keras.callbacks import ModelCheckpoint
from tensorflow.keras.optimizers import Adam
from tensorflow_addons.optimizers import LAMB
from termcolor import cprint

from tensorflow_similarity.architectures import (
    EfficientNetSim,
    ResNet18Sim,
    ResNet50Sim,
)
from tensorflow_similarity.callbacks import EvalCallback
from tensorflow_similarity.losses import (
    XBM,
    CircleLoss,
    MultiSimilarityLoss,
    PNLoss,
    SoftNearestNeighborLoss,
    TripletLoss,
)
from tensorflow_similarity.retrieval_metrics import MapAtK, PrecisionAtK, RecallAtK
from tensorflow_similarity.samplers import TFDatasetMultiShotMemorySampler
from tensorflow_similarity.utils import tf_cap_memory

os.environ["TF_CPP_MIN_LOG_LEVEL"] = "1"
tf.compat.v1.logging.set_verbosity(tf.compat.v1.logging.ERROR)

_BENCHMARK_DIR = "benchmark_results"


class NpEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, np.integer):
            return int(obj)
        if isinstance(obj, np.floating):
            return float(obj)
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        return json.JSONEncoder.default(self, obj)


LOSSES = {}
LOSSES["circle_loss"] = lambda p: CircleLoss(
    distance=p.get("distance", "cosine"),
    gamma=p.get("gamma", 80.0),
    margin=p.get("margin", 0.40),
)
LOSSES["multisim_loss"] = lambda p: MultiSimilarityLoss(
    distance=p.get("distance", "cosine"),
    alpha=p.get("alpha", 2.0),
    beta=p.get("beta", 40.0),
    epsilon=p.get("epsilon", 0.5),
    lmda=p.get("lmda", 0.5),
    center=p.get("center", 1.0),
)
LOSSES["pn_loss"] = lambda p: PNLoss(
    distance=p.get("distance", "cosine"),
    positive_mining_strategy=p.get("positive_mining", "hard"),
    negative_mining_strategy=p.get("negative_mining", "semi-hard"),
    soft_margin=p.get("soft_margin", False),
    margin=p.get("margin", 1.0),
)
LOSSES["soft_nn"] = lambda p: SoftNearestNeighborLoss(
    distance=p.get("distance", "cosine"),
    margin=p.get("temperature", 1.0),
)
LOSSES["triplet_loss"] = lambda p: TripletLoss(
    distance=p.get("distance", "cosine"),
    positive_mining_strategy=p.get("positive_mining", "hard"),
    negative_mining_strategy=p.get("negative_mining", "semi-hard"),
    soft_margin=p.get("soft_margin", False),
    margin=p.get("margin", 1.0),
)

OPTIMIZERS = {}
OPTIMIZERS["adam"] = lambda p: Adam(
    learning_rate=p.get("lr", 0.001),
    beta_1=p.get("beta_1", 0.9),
    beta_2=p.get("beta_2", 0.999),
    epsilon=p.get("epsilon", 1e-07),
    amsgrad=p.get("amsgrad", False),
)
OPTIMIZERS["lamb"] = lambda p: LAMB(
    learning_rate=p.get("learning_rate", 0.001),
    beta_1=p.get("beta_1", 0.9),
    beta_2=p.get("beta_2", 0.999),
    epsilon=p.get("epsilon", 1e-06),
    weight_decay=p.get("weight_decay", 0.0),
    exclude_from_weight_decay=p.get("exclude_from_weight_decay", None),
    exclude_from_layer_adaptation=p.get("exclude_from_layer_adaptation", None),
)

ARCHITECTURES = {}
ARCHITECTURES["effnet"] = lambda p: EfficientNetSim(
    input_shape=p["input_shape"],
    embedding_size=p.get("embedding", 128),
    variant=p.get("variant", "B0"),
    weights=p.get("weights", "imagenet"),
    trainable=p.get("trainable", "frozen"),
    l2_norm=p.get("l2_norm", True),
    include_top=p.get("include_top", True),
    pooling=p.get("pooling", "gem"),
    gem_p=p.get("gem_p", 3.0),
)
ARCHITECTURES["resnet50"] = lambda p: ResNet50Sim(
    input_shape=p["input_shape"],
    embedding_size=p.get("embedding", 128),
    weights=p.get("weights", "imagenet"),
    trainable=p.get("trainable", "frozen"),
    l2_norm=p.get("l2_norm", True),
    include_top=p.get("include_top", True),
    pooling=p.get("pooling", "gem"),
    gem_p=p.get("gem_p", 3.0),
)
ARCHITECTURES["resnet18"] = lambda p: ResNet18Sim(
    input_shape=p["input_shape"],
    embedding_size=p.get("embedding", 128),
    l2_norm=p.get("l2_norm", True),
    include_top=p.get("include_top", True),
    pooling=p.get("pooling", "gem"),
    gem_p=p.get("gem_p", 3.0),
)


AUGMENTATIONS = {}
AUGMENTATIONS["random_resized_crop"] = lambda p: keras_cv.layers.RandomCropAndResize(
    target_size=p.get("target_size", (227, 227)),
    crop_area_factor=p.get("crop_area_factor", (0.15625, 1.0)),
    aspect_ratio_factor=p.get("aspect_ratio_factor", (0.75, 1.333)),
)
AUGMENTATIONS["random_flip"] = lambda p: keras_cv.layers.RandomFlip(
    mode=p.get("mode", "horizontal"),
)
AUGMENTATIONS["center_crop"] = lambda p: tf.keras.layers.Resizing(
    height=p.get("height", 256),
    width=p.get("width", 256),
    crop_to_aspect_ratio=True,
)


def clean_dir(fpath):
    "delete previous content and recreate dir"
    if os.path.exists(fpath):
        shutil.rmtree(fpath)
    os.makedirs(fpath, exist_ok=True)


def make_loss(params):
    loss_name = params.get("loss", "None")
    try:
        loss = LOSSES[loss_name](params)
    except KeyError as exc:
        raise ValueError(f"Unknown loss name: {loss_name}") from exc

    if params.get("xbm", False):
        return XBM(loss=loss, memory_size=params.get("memory_size", 1), warmup_steps=params.get("warmup_steps", 0))

    return loss


def make_optimizer(params):
    opt = params.get("optimizer", "None")
    try:
        return OPTIMIZERS[opt](params)
    except KeyError as exc:
        raise ValueError(f"Unknown optimizer name: {opt}") from exc


def make_architecture(params):
    architecture = params.get("architecture", "None")
    try:
        return ARCHITECTURES[architecture](params)
    except KeyError as exc:
        raise ValueError(f"Unknown architecture name: {architecture}") from exc


def make_eval_metrics(tconf, econf, class_counts):
    metrics = []
    drop_closest_lookup = True if tconf["eval_split"]["mode"] == "all" else False
    for metric_name, params in econf.items():
        if metric_name == "recall_at_k":
            for k in params["k"]:
                metrics.append(
                    RecallAtK(
                        k=k,
                        average=params.get("average", "micro"),
                        drop_closest_lookup=drop_closest_lookup,
                    )
                )
        elif metric_name == "precision_at_k":
            for k in params["k"]:
                metrics.append(
                    PrecisionAtK(
                        k=k,
                        average=params.get("average", "micro"),
                        drop_closest_lookup=drop_closest_lookup,
                    )
                )
        elif metric_name == "map_at_k":
            for k in params["k"]:
                metrics.append(MapAtK(r=class_counts, k=k, drop_closest_lookup=drop_closest_lookup))
        else:
            raise ValueError(f"Unknown metric name: {metric_name}")

    return metrics


def make_eval_callback(train_ds, num_queries, num_targets):
    # Setup EvalCallback by splitting the test data into targets and queries.
    queries_x, queries_y = train_ds.get_slice(0, num_queries)
    queries_x, queries_y = train_ds.augmenter(
        queries_x, queries_y, train_ds.num_augmentations_per_example, train_ds.is_warmup
    )
    targets_x, targets_y = train_ds.get_slice(num_queries, num_targets)
    targets_x, targets_y = train_ds.augmenter(
        targets_x, targets_y, train_ds.num_augmentations_per_example, train_ds.is_warmup
    )
    return EvalCallback(
        queries_x,
        queries_y,
        targets_x,
        targets_y,
        metrics=["binary_accuracy", "precision", "recall"],
        k=1,
    )


def make_train_sampler(dataset_name, dconf, tconf, pconf, aconf):
    preprocs = [AUGMENTATIONS[pparams["name"]](pparams) for pparams in pconf["train"]]

    def preprocess_fn(x, y):
        with tf.device("/cpu:0"):
            for p in preprocs:
                x = p(x)
        return x, y

    augs = [AUGMENTATIONS[aparams["name"]](aparams) for aparams in aconf["train"]]

    def augmentation_fn(x, y, *args):
        for a in augs:
            x = a(x)
        return x, y

    return TFDatasetMultiShotMemorySampler(
        dataset_name=dataset_name,
        x_key=dconf["x_key"],
        y_key=dconf["y_key"],
        splits=dconf.get("splits", None),
        classes_per_batch=tconf.get("classes_per_batch", 8),
        examples_per_class_per_batch=tconf.get("examples_per_class_per_batch", 4),
        class_list=list(range(*dconf["train_classes"])),
        preprocess_fn=preprocess_fn,
        augmenter=augmentation_fn,
    )


def make_test_data(dataset_name, dconf, tconf, pconf):
    preprocs = [AUGMENTATIONS[pparams["name"]](pparams) for pparams in pconf["test"]]

    def preprocess_fn(x, y):
        with tf.device("/cpu:0"):
            for p in preprocs:
                x = p(x)
        return x, y

    test_ds = TFDatasetMultiShotMemorySampler(
        dataset_name=dataset_name,
        x_key=dconf["x_key"],
        y_key=dconf["y_key"],
        splits=dconf.get("splits", None),
        # This doesn't matter as we going to directly pull the preprocessed data.
        classes_per_batch=1,
        class_list=list(range(*dconf["test_classes"])),
        preprocess_fn=preprocess_fn,
    )
    class_counts = {k: len(v) for k, v in test_ds.index_per_class.items()}
    return test_ds._x, test_ds._y, class_counts


def make_stub(version, dataset_name, architecture_name, loss_name, opt_name):
    run_grp = "_".join([dataset_name, architecture_name, loss_name, opt_name])
    return os.path.join(_BENCHMARK_DIR, version, run_grp)


def build_model(aconf, lconf, oconf):
    model = make_architecture(aconf)
    loss = make_loss(lconf)
    opt = make_optimizer(oconf)
    model.compile(optimizer=opt, loss=loss)
    return model


def run(config):
    if config.get("tfds_data_dir", None):
        os.environ["TFDS_DATA_DIR"] = config["tfds_data_dir"]

    agg_results = {}
    for dataset_name, dconf in config["datasets"].items():
        for architecture_name, aconf in config["architectures"].items():
            for loss_name, lconf in config["losses"].items():
                for opt_name, oconf in config["optimizer"].items():
                    for training_name, tconf in config["training"].items():
                        headers = ["dataset_name", "architecture_name", "loss_name", "opt_name", "training_name"]
                        row = [
                            [
                                f"{dataset_name}",
                                f"{architecture_name}",
                                f"{loss_name}",
                                f"{opt_name}",
                                f"{training_name}",
                            ]
                        ]
                        cprint(tabulate(row, headers=headers), "yellow")

                        gc.collect()
                        tf.keras.backend.clear_session()

                        tf.random.set_seed(config["random_seed"])
                        version = config["version"]

                        # Build dataset sampler
                        cprint("|-loading dataset", "blue")
                        pconf = config["preprocess"]
                        aug_conf = config["augmentations"]
                        train_ds = make_train_sampler(dataset_name, dconf, tconf, pconf, aug_conf)

                        # Build model
                        model = build_model(aconf, lconf, oconf)

                        # Make result path
                        stub = make_stub(
                            version,
                            dataset_name,
                            architecture_name,
                            loss_name,
                            opt_name,
                        )
                        clean_dir(stub)

                        # Training params
                        cprint("Training", "green")
                        val_split = dconf["train_validation_split"]
                        callbacks = [
                            make_eval_callback(
                                train_ds,
                                val_split["num_queries"],
                                val_split["num_targets"],
                            ),
                            ModelCheckpoint(
                                stub,
                                monitor="loss",
                                save_best_only=True,
                            ),
                        ]
                        history = model.fit(
                            train_ds,
                            steps_per_epoch=tconf.get("steps_per_epoch", 1),
                            epochs=tconf.get("epochs", 1),
                            callbacks=callbacks,
                        )

                        # Evaluation
                        test_x, test_y, class_counts = make_test_data(dataset_name, dconf, tconf, pconf)

                        eval_metrics = make_eval_metrics(dconf, config["evaluation"], class_counts)

                        model.reset_index()
                        model.index(test_x, test_y)

                        eval_results = model.evaluate_retrieval(
                            test_x,
                            test_y,
                            retrieval_metrics=eval_metrics,
                        )

                        agg_results[os.path.basename(stub)] = eval_results

                        # Save history
                        with open(os.path.join(stub, "history.json"), "w") as o:
                            o.write(json.dumps(history.history, cls=NpEncoder))

                        # Save eval metrics
                        with open(os.path.join(stub, "eval_metrics.json"), "w") as o:
                            o.write(json.dumps(eval_results, cls=NpEncoder))

    with open(os.path.join(os.path.dirname(stub), "all_eval_metrics.json"), "w") as o:
        o.write(json.dumps(agg_results, cls=NpEncoder))


if __name__ == "__main__":
    tf_cap_memory()

    parser = argparse.ArgumentParser(description="Train model")
    parser.add_argument("--config", "-c", help="config path")
    args = parser.parse_args()

    if not args.config:
        parser.print_usage()
        quit()
    config = json.loads(open(args.config).read())
    run(config)