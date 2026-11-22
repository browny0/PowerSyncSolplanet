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


def test_solplanet_setup_reads_persisted_battery_values():
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
    branch_source = ast.unparse(solplanet_branch)

    assert "_entry_value(CONF_SOLPLANET_BATTERY_CAPACITY_KWH)" in branch_source
    assert "_entry_value(CONF_SOLPLANET_INVERTER_AC_LIMIT_KW)" in branch_source
    assert "DEFAULT_SOLPLANET_BATTERY_CAPACITY_KWH" not in branch_source
    assert "DEFAULT_SOLPLANET_INVERTER_AC_LIMIT_KW" not in branch_source


def test_legacy_solplanet_entry_is_detected_without_battery_system():
    source = INIT_PATH.read_text()
    resolver = next(
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.FunctionDef)
        and node.name == "_active_battery_system"
    )
    resolver_source = ast.unparse(resolver)

    assert "CONF_SOLPLANET_CONFIG_ENTRY_ID" in resolver_source
    assert "BATTERY_SYSTEM_SOLPLANET" in resolver_source
