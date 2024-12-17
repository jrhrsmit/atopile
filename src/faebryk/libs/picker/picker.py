# This file is part of the faebryk project
# SPDX-License-Identifier: MIT

import logging
import pprint
from abc import ABC, abstractmethod
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from textwrap import indent
from typing import Callable, Iterable

from rich.progress import Progress

import faebryk.library._F as F
from faebryk.core.module import Module
from faebryk.core.moduleinterface import ModuleInterface
from faebryk.core.parameter import (
    And,
    Is,
    Or,
    Parameter,
    ParameterOperatable,
    Predicate,
)
from faebryk.core.solver.solver import LOG_PICK_SOLVE, Solver
from faebryk.libs.util import (
    ConfigFlag,
    KeyErrorNotFound,
    Tree,
    not_none,
    partition,
    try_or,
)

NO_PROGRESS_BAR = ConfigFlag("NO_PROGRESS_BAR", default=False)

logger = logging.getLogger(__name__)


class Supplier(ABC):
    @abstractmethod
    def attach(self, module: Module, part: "PickerOption"): ...


@dataclass(frozen=True)
class Part:
    partno: str
    supplier: Supplier


class DescriptiveProperties(StrEnum):
    manufacturer = "Manufacturer"
    partno = "Partnumber"
    datasheet = "Datasheet"


@dataclass(frozen=True)
class PickerOption:
    part: Part
    params: dict[str, ParameterOperatable.SetLiteral] | None = None
    """
    Parameters that need to be matched for this option to be valid.

    Assumes specified params are narrowest possible value for this part
    """
    filter: Callable[[Module], bool] | None = None
    pinmap: dict[str, F.Electrical] | None = None
    info: dict[str | DescriptiveProperties, str] | None = None

    def __hash__(self):
        return hash(self.part)


class PickError(Exception):
    def __init__(self, message: str, module: Module):
        super().__init__(message)
        self.message = message
        self.module = module

    def __repr__(self):
        return f"{type(self).__name__}({self.module}, {self.message})"


class PickErrorNotImplemented(PickError):
    def __init__(self, module: Module):
        message = f"Could not pick part for {module}: Not implemented"
        super().__init__(message, module)


class PickErrorChildren(PickError):
    def __init__(self, module: Module, children: dict[Module, PickError]):
        self.children = children

        message = f"Could not pick parts for children of {module}:\n" + "\n".join(
            f"{m}: caused by {v.message}" for m, v in self.get_all_children().items()
        )
        super().__init__(message, module)

    def get_all_children(self):
        return {
            k: v
            for k, v in self.children.items()
            if not isinstance(v, PickErrorChildren)
        } | {
            module: v2
            for v in self.children.values()
            if isinstance(v, PickErrorChildren)
            for module, v2 in v.get_all_children().items()
        }


class PickErrorParams(PickError):
    def __init__(self, module: Module, options: list[PickerOption], solver: Solver):
        self.options = options

        MAX = 5

        options_str = "\n".join(
            f"{pprint.pformat(o.params, indent=4)}" for o in self.options[:MAX]
        )
        if len(self.options) > MAX:
            options_str += f"\n... and {len(self.options) - MAX} more"

        message = (
            f"Could not find part for {module}"
            f"\nwith params:\n{indent(module.pretty_params(solver), ' '*4)}"
            f"\nin options:\n {indent(options_str, ' '*4)}"
        )
        super().__init__(message, module)


class has_part_picked(Module.TraitT):
    @abstractmethod
    def get_part(self) -> Part: ...


class has_part_picked_defined(has_part_picked.impl()):
    def __init__(self, part: Part):
        super().__init__()
        self.part = part

    def get_part(self) -> Part:
        return self.part


class has_part_picked_remove(has_part_picked.impl()):
    class RemovePart(Part):
        class NoSupplier(Supplier):
            def attach(self, module: Module, part: PickerOption):
                pass

        def __init__(self):
            super().__init__("REMOVE", self.NoSupplier())

    def __init__(self) -> None:
        super().__init__()
        self.part = self.RemovePart()

    def get_part(self) -> Part:
        return self.part

    @staticmethod
    def mark_no_pick_needed(module: Module):
        module.add(
            F.has_multi_picker(
                -1000,
                F.has_multi_picker.FunctionPicker(
                    lambda m, _: m.add(has_part_picked_remove()) and None
                ),
            )
        )


class skip_self_pick(Module.TraitT.decless()):
    """Indicates that a node exists only to contain children, and shouldn't itself be picked"""  # noqa: E501  # pre-existing


def pick_module_by_params(
    module: Module, solver: Solver, options: Iterable[PickerOption]
):
    if module.has_trait(has_part_picked):
        logger.debug(f"Ignoring already picked module: {module}")
        return

    params = {
        not_none(p.get_parent())[1]: p
        for p in module.get_children(direct_only=True, types=Parameter)
    }

    filtered_options = [o for o in options if not o.filter or o.filter(module)]
    predicates: dict[PickerOption, ParameterOperatable.BooleanLike] = {}
    for o in filtered_options:
        predicate_list: list[Predicate] = []

        for k, v in (o.params or {}).items():
            if not k.startswith("_"):
                param = params[k]
                predicate_list.append(Is(param, v))

        # No predicates, thus always valid option
        if len(predicate_list) == 0:
            predicates[o] = Or(True)
            continue

        predicates[o] = And(*predicate_list)

    if len(predicates) == 0:
        raise PickErrorParams(module, list(options), solver)

    solve_result = solver.assert_any_predicate(
        [(p, k) for k, p in predicates.items()], lock=True
    )

    # FIXME handle failure parameters

    # pick first valid option
    if not solve_result.true_predicates:
        raise PickErrorParams(module, list(options), solver)

    _, option = next(iter(solve_result.true_predicates))

    if option.pinmap:
        module.add(F.can_attach_to_footprint_via_pinmap(option.pinmap))

    option.part.supplier.attach(module, option)
    module.add(has_part_picked_defined(option.part))

    logger.debug(f"Attached {option.part.partno} to {module}")
    return option


def _get_mif_top_level_modules(mif: ModuleInterface) -> set[Module]:
    return Module.get_children_modules(mif, types=Module, direct_only=True) | {
        m
        for nmif in mif.get_children(direct_only=True, types=ModuleInterface)
        for m in _get_mif_top_level_modules(nmif)
    }


class PickerProgress:
    def __init__(self, tree: Tree[Module]):
        self.tree = tree
        self.progress = Progress(disable=bool(NO_PROGRESS_BAR))
        leaves = list(tree.leaves())
        count = len(leaves)

        logger.info(f"Picking parts for {count} leaf modules")
        self.task = self.progress.add_task("Picking", total=count)

    def advance(self, module: Module):
        leaf_count = len(list(self.tree.get_subtree(module).leaves()))
        # module is leaf
        if not leaf_count:
            leaf_count = 1
        self.progress.advance(self.task, leaf_count)

    @contextmanager
    def context(self):
        with self.progress:
            yield self


def get_pick_tree(module: Module | ModuleInterface) -> Tree[Module]:
    if isinstance(module, Module):
        module = module.get_most_special()

    tree = Tree()
    merge_tree = tree

    if module.has_trait(has_part_picked):
        return tree

    if module.has_trait(F.has_picker) and not module.has_trait(skip_self_pick):
        merge_tree = Tree()
        tree[module] = merge_tree

    for child in module.get_children(
        direct_only=True, types=(Module, ModuleInterface), include_root=False
    ):
        child_tree = get_pick_tree(child)
        merge_tree.update(child_tree)

    return tree


def check_missing_picks(module: Module):
    # - not skip self pick
    # - no parent with part picked
    # - not specialized
    # - no module children
    # - no parent with picker

    missing = module.get_children_modules(
        types=Module,
        direct_only=False,
        include_root=True,
        # not specialized
        most_special=True,
        # leaf == no children
        f_filter=lambda m: not m.get_children_modules(types=Module)
        # no parent with part picked
        and not try_or(
            lambda: m.get_parent_with_trait(has_part_picked),
            default=False,
            catch=KeyErrorNotFound,
        )
        # not skip self pick
        and not m.has_trait(skip_self_pick)
        # no parent with picker
        and not try_or(
            lambda: m.get_parent_with_trait(F.has_picker),
            default=False,
            catch=KeyErrorNotFound,
        ),
    )

    if missing:
        no_fp, fp = map(
            list, partition(lambda m: not m.has_trait(F.has_footprint), missing)
        )

        if fp:
            logger.warning(f"No pickers for {fp}")
        if no_fp:
            logger.warning(
                f"No pickers and no footprint for {no_fp}."
                "Attention: These modules will not apperar in netlist or pcb."
            )


def pick_topologically(tree: Tree[Module], solver: Solver, progress: PickerProgress):
    if LOG_PICK_SOLVE:
        pickable_modules = next(iter(tree.iter_by_depth()))
        names = sorted(p.get_full_name(types=True) for p in pickable_modules)
        logger.info(f"Picking parts for \n\t{'\n\t'.join(names)}")

    candidates = tree.copy()

    # TODO implement order (by heuristic)

    while candidates:
        module, subtree = candidates.popitem()
        try:
            module.get_trait(F.has_picker).pick(solver)
            progress.advance(module)
        except PickError:
            if not subtree:
                raise
            if LOG_PICK_SOLVE:
                logger.warning(f"Could not pick {module}, descending into {subtree}")
            candidates.update(subtree)

    if LOG_PICK_SOLVE:
        logger.info("Done picking")


# TODO should be a Picker
def pick_part_recursively(module: Module, solver: Solver):
    pick_tree = get_pick_tree(module)
    if LOG_PICK_SOLVE:
        logger.info(f"Pick tree:\n{pick_tree.pretty()}")

    check_missing_picks(module)

    pp = PickerProgress(pick_tree)
    try:
        with pp.context():
            pick_topologically(pick_tree, solver, pp)
    except PickErrorChildren as e:
        failed_parts = e.get_all_children()
        for m, sube in failed_parts.items():
            logger.error(
                f"Could not find pick for {m}:\n {sube.message}\n"
                f"Params:\n{indent(m.pretty_params(solver), prefix=' '*4)}"
            )
        raise e
