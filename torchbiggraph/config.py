#!/usr/bin/env python3

# Copyright (c) Facebook, Inc. and its affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE.txt file in the root directory of this source tree.

import argparse
import importlib
import logging
import os.path
import shutil
import sys
import tempfile
import uuid

from enum import Enum
from itertools import chain
from typing import Any, ClassVar, Dict, List, Optional

import attr
from attr.validators import optional

from torchbiggraph.schema import (
    DeepTypeError,
    Schema,
    extract_nested_type,
    has_origin,
    inject_nested_value,
    non_empty,
    non_negative,
    positive,
    schema,
)


logger = logging.getLogger("torchbiggraph")


class BucketOrder(Enum):
    # Random permutation/shuffle.
    RANDOM = 'random'
    # Each bucket will have as many partitions as possible in common with the
    # preceding bucket (ideally both, otherwise only one, else none). If
    # multiple candidate buckets exist, one is picked randomly.
    AFFINITY = 'affinity'
    # Enforce that (L1, R1) comes before (L2, R2) iff min(L1, R1) > min(L2, R2)
    # (subject to that condition, shuffle randomly).
    INSIDE_OUT = 'inside_out'
    # The "per-layer" reverse of outside-in: (L1, R1) comes before (L2, R2) iff
    # min(L1, R1) > min(L2, R2).
    OUTSIDE_IN = 'outside_in'


@schema
class EntitySchema(Schema):

    NAME: ClassVar[str] = "entity"

    num_partitions: int = attr.ib(
        validator=positive,
        metadata={'help': "Number of partitions for this entity type. Set to 1 "
                          "if unpartitioned. All other entity types must have "
                          "the same number of partitions."},
    )
    featurized: bool = attr.ib(
        default=False,
        metadata={'help': "Whether the entities of this type are represented "
                          "as sets of features."},
    )
    dimension: Optional[int] = attr.ib(
        default=None,
        validator=optional(positive),
        metadata={'help': "Override the default dimension for this entity."}
    )


@schema
class RelationSchema(Schema):

    NAME: ClassVar[str] = "relation"

    name: str = attr.ib(
        validator=non_empty,
        metadata={'help': "A human-readable identifier for the relation type. "
                          "Not needed for training, only used for logging."},
    )
    lhs: str = attr.ib(
        validator=non_empty,
        metadata={'help': "The type of entities on the left-hand side of this "
                          "relation, i.e., its key in the entities dict."},
    )
    rhs: str = attr.ib(
        validator=non_empty,
        metadata={'help': "The type of entities on the right-hand side of this "
                          "relation, i.e., its key in the entities dict."},
    )
    weight: float = attr.ib(
        default=1.0,
        validator=positive,
        metadata={'help': "The weight by which the loss induced by edges of "
                          "this relation type will be multiplied."},
    )
    operator: str = attr.ib(
        default="none",
        metadata={'help': "The transformation to apply to the embedding of one "
                          "of the sides of the edge (typically the right-hand "
                          "one) before comparing it with the other one."},
    )
    all_negs: bool = attr.ib(
        default=False,
        metadata={'help': "If enabled, the negatives for (x, r, y) will "
                          "consist of (x, r, y') for all entities y' of the "
                          "same type and in the same partition as y, and, "
                          "symmetrically, of (x', r, y) for all entities x' of "
                          "the same type and in the same partition as x."},
    )


@schema
class ConfigSchema(Schema):

    NAME: ClassVar[str] = "config"

    # model config

    entities: Dict[str, EntitySchema] = attr.ib(
        validator=non_empty,
        metadata={'help': "The entity types. The ID with which they are "
                          "referenced by the relation types is the key they "
                          "have in this dict."},
    )
    relations: List[RelationSchema] = attr.ib(
        validator=non_empty,
        metadata={'help': "The relation types. The ID with which they will be "
                          "referenced in the edge lists is their index in this "
                          "list."},
    )
    dimension: int = attr.ib(
        validator=positive,
        metadata={'help': "The dimension of the real space the embedding live "
                          "in."},
    )
    init_scale: float = attr.ib(
        default=1e-3,
        validator=positive,
        metadata={'help': "If no initial embeddings are provided, they are "
                          "generated by sampling each dimension from a "
                          "centered normal distribution having this standard "
                          "deviation. (For performance reasons, sampling isn't "
                          "fully independent.)"},
    )
    max_norm: Optional[float] = attr.ib(
        default=None,
        validator=optional(positive),
        metadata={'help': "If set, rescale the embeddings if their norm "
                          "exceeds this value."},
    )
    global_emb: bool = attr.ib(
        default=True,
        metadata={'help': "If enabled, add to each embedding a vector that is "
                          "common to all the entities of a certain type. This "
                          "vector is learned during training."},
    )
    comparator: str = attr.ib(
        default="cos",
        metadata={'help': "How the embeddings of the two sides of an edge "
                          "(after having already undergone some processing) "
                          "are compared to each other to produce a score."},
    )
    bias: bool = attr.ib(
        default=False,
        metadata={'help': "If enabled, withhold the first dimension of the "
                          "embeddings from the comparator and instead use it "
                          "as a bias, adding back to the score. Makes sense "
                          "for logistic and softmax loss functions."},
    )
    loss_fn: str = attr.ib(
        default="ranking",
        metadata={'help': "How the scores of positive edges and their "
                          "corresponding negatives are evaluated."},
    )
    margin: float = attr.ib(
        default=0.1,
        metadata={'help': "When using ranking loss, this value controls the "
                          "minimum separation between positive and negative "
                          "scores, below which a (linear) loss is incured."},
    )

    # data config

    entity_path: str = attr.ib(
        metadata={'help': "The path of the directory containing entity count "
                          "files."},
    )
    edge_paths: List[str] = attr.ib(
        metadata={'help': "A list of paths to directories containing "
                          "(partitioned) edgelists. Typically a single path is "
                          "provided."},
    )
    checkpoint_path: str = attr.ib(
        metadata={'help': "The path to the directory where checkpoints (and "
                          "thus the output) will be written to. If checkpoints "
                          "are found in it, training will resume from them."},
    )
    init_path: Optional[str] = attr.ib(
        default=None,
        metadata={'help': "If set, it must be a path to a directory that "
                          "contains initial values for the embeddings of all "
                          "the entities of some types."},
    )
    checkpoint_preservation_interval: Optional[int] = attr.ib(
        default=None,
        metadata={'help': "If set, every so many epochs a snapshot of the "
                          "checkpoint will be archived. The snapshot will be "
                          "located inside a `epoch_{N}` sub-directory of the "
                          "checkpoint directory, and will contain symbolic "
                          "links to the original checkpoint files, which will "
                          "not be cleaned-up as it would normally happen."},
    )

    # training config

    num_epochs: int = attr.ib(
        default=1,
        validator=non_negative,
        metadata={'help': "The number of times the training loop iterates over "
                          "all the edges."},
    )
    num_edge_chunks: Optional[int] = attr.ib(
        default=None,
        validator=optional(positive),
        metadata={'help': "The number of equally-sized parts each bucket will "
                          "be split into. Training will first proceed over all "
                          "the first chunks of all buckets, then over all the "
                          "second chunks, and so on. A higher value allows "
                          "better mixing of partitions, at the cost of more "
                          "time spent on I/O. If unset, will be automatically "
                          "calculated so that no chunk has more than "
                          "max_edges_per_chunk edges."},
    )
    max_edges_per_chunk: int = attr.ib(
        default=1_000_000_000,  # Each edge having 3 int64s, this is 12GB.
        validator=positive,
        metadata={'help': "The maximum number of edges that each edge chunk "
                          "should contain if the number of edge chunks is left "
                          "unspecified and has to be automatically figured "
                          "out. Each edge takes up at least 12 bytes (3 "
                          "int64s), more if using featurized entities."},
    )
    bucket_order: BucketOrder = attr.ib(
        default=BucketOrder.INSIDE_OUT,
        metadata={'help': "The order in which to iterate over the buckets."},
    )
    workers: Optional[int] = attr.ib(
        default=None,
        validator=optional(positive),
        metadata={'help': "The number of worker processes for \"Hogwild!\" "
                          "training. If not given, set to CPU count."},
    )
    batch_size: int = attr.ib(
        default=1000,
        validator=positive,
        metadata={'help': "The number of edges per batch."},
    )
    num_batch_negs: int = attr.ib(
        default=50,
        validator=non_negative,
        metadata={'help': "The number of negatives sampled from the batch, per "
                          "positive edge."},
    )
    num_uniform_negs: int = attr.ib(
        default=50,
        validator=non_negative,
        metadata={'help': "The number of negatives uniformly sampled from the "
                          "currently active partition, per positive edge."},
    )
    disable_lhs_negs : bool = attr.ib(
        default=False,
        metadata={'help': "Disable negative sampling on the left-hand side."},
    )
    disable_rhs_negs : bool = attr.ib(
        default=False,
        metadata={'help': "Disable negative sampling on the right-hand side."},
    )
    lr: float = attr.ib(
        default=1e-2,
        validator=non_negative,
        metadata={'help': "The learning rate for the optimizer."},
    )
    relation_lr: Optional[float] = attr.ib(
        default=None,
        validator=optional(non_negative),
        metadata={'help': "If set, the learning rate for the optimizer"
                          "for relations. Otherwise, `lr' is used."},
    )
    eval_fraction: float = attr.ib(
        default=0.05,
        validator=non_negative,
        metadata={'help': "The fraction of edges withheld from training and "
                          "used to track evaluation metrics during training."},
    )
    eval_num_batch_negs: int = attr.ib(
        default=1000,
        validator=non_negative,
        metadata={'help': "The value that overrides the number of negatives "
                          "per positive edge sampled from the batch during the "
                          "evaluation steps that occur before and after each "
                          "training step."},
    )
    eval_num_uniform_negs: int = attr.ib(
        default=1000,
        validator=non_negative,
        metadata={'help': "The value that overrides the number of "
                          "uniformly-sampled negatives per positive edge "
                          "during the evaluation steps that occur before and "
                          "after each training step."},
    )

    # expert options

    background_io: bool = attr.ib(
        default=False,
        metadata={'help': "Whether to do load/save in a background process. "
                          "DEPRECATED."},
    )
    verbose: int = attr.ib(
        default=0,
        validator=non_negative,
        metadata={'help': "The verbosity level of logging, currently 0 or 1."},
    )
    hogwild_delay: float = attr.ib(
        default=2,
        validator=non_negative,
        metadata={'help': "The number of seconds by which to delay the start "
                          "of all \"Hogwild!\" processes except the first one, "
                          "on the first epoch."},
    )
    dynamic_relations: bool = attr.ib(
        default=False,
        metadata={'help': "If enabled, activates the dynamic relation mode, in "
                          "which case, there must be a single relation type in "
                          "the config (whose parameters will apply to all "
                          "dynamic relations types) and there must be a file "
                          "called dynamic_rel_count.txt in the entity path that "
                          "contains the number of dynamic relations. In this "
                          "mode, batches will contain edges of multiple "
                          "relation types and negatives will be sampled "
                          "differently."},
    )

    # distributed training config options

    num_machines: int = attr.ib(
        default=1,
        validator=positive,
        metadata={'help': "The number of machines for distributed training."},
    )
    num_partition_servers: int = attr.ib(
        default=-1,
        metadata={'help': "If -1, use trainer as partition servers. If 0, "
                          "don't use partition servers (instead, swap "
                          "partitions through disk). If >1, then that number "
                          "of partition servers must be started manually."},
    )
    distributed_init_method: Optional[str] = attr.ib(
        default=None,
        metadata={'help': "A URI defining how to synchronize all the workers "
                          "of a distributed run. Must start with a scheme "
                          "(e.g., file:// or tcp://) supported by PyTorch."}
    )
    distributed_tree_init_order: bool = attr.ib(
        default=True,
        metadata={'help': "If enabled, then distributed training can occur on "
                          "a bucket only if at least one of its partitions was "
                          "already trained on before in the same round (or if "
                          "one of its partitions is 0, for bootstrapping)."},
    )

    num_gpus: int = attr.ib(
        default=0,
        metadata={'help': "Number of GPUs to use for GPU training. "
                          "Experimental: Not yet supported."},
    )
    num_groups_for_partition_server: int = attr.ib(
        default=16,
        metadata={'help': "Number of td.distributed 'groups' to use. Setting "
                          "this to a value around 16 typically increases "
                          "communication bandwidth."},
    )
    half_precision: bool = attr.ib(
        default=False,
        metadata={'help': "Use half-precision training (GPU ONLY)"},
    )

    # Additional global validation.

    def __attrs_post_init__(self):
        for rel_id, rel_config in enumerate(self.relations):
            if rel_config.lhs not in self.entities:
                raise ValueError("Relation type %s (#%d) has an unknown "
                                 "left-hand side entity type %s"
                                 % (rel_config.name, rel_id, rel_config.lhs))
            if rel_config.rhs not in self.entities:
                raise ValueError("Relation type %s (#%d) has an unknown "
                                 "right-hand side entity type %s"
                                 % (rel_config.name, rel_id, rel_config.rhs))
        if self.dynamic_relations:
            if len(self.relations) != 1:
                raise ValueError("When dynamic relations are in use only one "
                                 "relation type must be defined.")
        # TODO Check that all partitioned entity types have the same number of partitions
        # TODO Check that the batch size is a multiple of the batch negative number
        if self.loss_fn == "logistic" and self.comparator == "cos":
            logger.warning("You have logistic loss and cosine distance. Are you sure?")

        if self.disable_lhs_negs and self.disable_rhs_negs:
            raise ValueError("Cannot disable negative sampling on both sides.")

        if self.background_io:
            logger.warning("`background_io` is deprecated and will have no effect.")



# TODO make this a non-inplace operation
def override_config_dict(config_dict: Any, overrides: Optional[List[List[str]]]) -> Any:
    if overrides is None:
        overrides = []
    overrides = chain.from_iterable(overrides)
    for override in overrides:
        try:
            key, _, value = override.rpartition("=")
            path = key.split(".")
            param_type = extract_nested_type(ConfigSchema, path)
            # this is a bit of a hack; we should do something better
            # but this is convenient for specifying lists of strings
            # e.g. edge_paths
            if has_origin(param_type, list):
                value = value.split(",")
            # Convert numbers (caution: ignore bools, which are ints)
            if isinstance(param_type, type) \
                    and issubclass(param_type, (int, float)) \
                    and not issubclass(param_type, bool):
                value = param_type(value)
            inject_nested_value(config_dict, path, value)
        except Exception as err:
            raise RuntimeError("Can't parse override: %s" % override) from err
    return config_dict


def parse_config(config_dict: Any) -> ConfigSchema:
    try:
        config = ConfigSchema.from_dict(config_dict)
    except DeepTypeError as err:
        logger.critical("Error in the configuration file, aborting.")
        logger.critical(f"{err}")
        raise SystemExit(1)
    return config


class ConfigFileLoader:
    """Load configs from source files, after setting them up as modules.

    In order to support configs defined in Python files whose paths are passed
    on the command line, we need to first load those files as modules (using
    eval is for savages). If those files define classes or functions that need
    to be pickled to be sent to the workers (say, custom operators, comparators,
    ...) then their modules need to be "normally" importable: their filename
    must match their module name and they must reside in a standard location
    (i.e., a directory in the path).

    All the above is taken care of by this class, which creates a temporary
    directory in which to copy over the configs, with unique names, and then
    imports them from there.
    """

    def __init__(self) -> None:
        self.config_dir = tempfile.TemporaryDirectory(prefix="torchbiggraph_config_")
        # Hold a reference because at destruction time it may not be available anymore.
        self.sys_path = sys.path
        self.sys_path.append(self.config_dir.name)

    def __del__(self) -> None:
        self.sys_path.remove(self.config_dir.name)
        self.config_dir.cleanup()

    def load_raw_config(self, path: str, overrides: Optional[List[List[str]]] = None) -> Any:
        module_name = f"torchbiggraph_config_{uuid.uuid4().hex}"
        shutil.copyfile(path, os.path.join(self.config_dir.name, f"{module_name}.py"))
        importlib.invalidate_caches()
        module = importlib.import_module(module_name)
        raw_config = module.get_torchbiggraph_config()
        config_with_overrides = override_config_dict(raw_config, overrides)
        return config_with_overrides

    def load_config(
        self,
        path: str,
        overrides: Optional[List[List[str]]] = None,
    ) -> ConfigSchema:
        config_dict = self.load_raw_config(path, overrides=overrides)
        config = parse_config(config_dict)
        return config


def add_to_sys_path(path: str) -> None:
    sys.path.append(path)


def main():
    # Late import to avoid circular dependency.
    from torchbiggraph.util import set_logging_verbosity, setup_logging
    setup_logging()
    parser = argparse.ArgumentParser()
    parser.add_argument('config', help="Path to config file")
    parser.add_argument('query', help="Name of param to retrieve")
    parser.add_argument('-p', '--param', action='append', nargs='*')
    opt = parser.parse_args()

    if opt.param is not None:
        overrides = chain.from_iterable(opt.param)  # flatten
    else:
        overrides = None
    loader = ConfigFileLoader()
    config = loader.load_config(opt.config, overrides)
    set_logging_verbosity(config.verbose)

    print(config[opt.query])


if __name__ == '__main__':
    main()
