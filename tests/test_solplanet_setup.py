"""Regression coverage for Solplanet config-entry startup routing."""

from __future__ import annotations

import ast
from pathlib import Path


INIT_PATH = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "power_sync"
    / "__init__.py"
)


def _contains_call(node: ast.AST, function_name: str) -> bool:
    return any(
        isinstance(child, ast.Call)
        and isinstance(child.func, ast.Name)
        and child.func.id == function_name
        for child in ast.walk(node)
    )


def test_solplanet_setup_does_not_require_a_tesla_energy_site():
    source = INIT_PATH.read_text()
    setup = next(
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "async_setup_entry"
    )
    solplanet_branch = next(
        node
        for node in ast.walk(setup)
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Name)
        and node.test.id == "is_solplanet"
    )
    assert _contains_call(solplanet_branch, "SolplanetEnergyCoordinator")

    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(setup):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent

    tesla_site_guard = next(
        node
        for node in ast.walk(setup)
        if isinstance(node, ast.Raise)
        and any(
            isinstance(child, ast.Constant)
            and isinstance(child.value, str)
            and "No Tesla energy site configured" in child.value
            for child in ast.walk(node)
        )
    )
    ancestor = tesla_site_guard
    while ancestor in parents:
        ancestor = parents[ancestor]
        if not isinstance(ancestor, ast.If):
            continue
        test = ancestor.test
        if (
            isinstance(test, ast.Compare)
            and isinstance(test.left, ast.Name)
            and test.left.id == "active_battery_system"
            and isinstance(test.comparators[0], ast.Name)
            and test.comparators[0].id == "BATTERY_SYSTEM_TESLA"
        ):
            break
    else:
        raise AssertionError(
            "Tesla energy-site validation must only run for Tesla battery setups"
        )
