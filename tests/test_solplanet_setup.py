"""Regression coverage for Solplanet config-entry startup routing."""

from __future__ import annotations

import ast
import asyncio
import copy
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

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
        and _contains_call(node, "SolplanetEnergyCoordinator")
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
        and _contains_call(node, "SolplanetEnergyCoordinator")
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


def test_solplanet_force_commands_bypass_tesla_credentials():
    source = INIT_PATH.read_text()
    setup = next(
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "async_setup_entry"
    )

    handlers = {
        node.name: ast.unparse(node)
        for node in ast.walk(setup)
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name in {
            "handle_force_charge",
            "handle_force_discharge",
            "handle_restore_normal",
        }
    }

    assert "solplanet_coord.force_discharge" in handlers["handle_force_discharge"]
    assert "solplanet_coord.force_charge" in handlers["handle_force_charge"]
    assert "solplanet_coord.restore_normal" in source


def test_solplanet_hold_and_backup_reserve_routing():
    source = INIT_PATH.read_text()

    assert '("solplanet_coordinator", "solplanet")' in source
    assert "solplanet_coord.set_backup_reserve(percent)" in source


@pytest.mark.parametrize("succeeds", [True, False])
def test_optimizer_solplanet_discharge_uses_guarded_hardware_only_path(succeeds):
    tree = ast.parse(INIT_PATH.read_text())
    handler = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "handle_force_discharge"
    )
    branch = next(
        node for node in ast.walk(handler)
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Name)
        and node.test.id == "solplanet_coord"
        and "Solplanet force discharge hardware refresh" in ast.unparse(node)
    )
    wrapper = ast.parse(
        "async def run(solplanet_coord, duration, power_w):\n"
        "    pass\n"
        "    return 'fell through'\n"
    )
    wrapper.body[0].body[0] = copy.deepcopy(branch)
    ast.fix_missing_locations(wrapper)

    class HomeAssistantError(Exception):
        pass

    async def guarded_write(writer):
        return await writer(7500)

    namespace = {
        "_guarded_force_discharge_write": guarded_write,
        "HomeAssistantError": HomeAssistantError,
        "_LOGGER": logging.getLogger(__name__),
    }
    exec(compile(wrapper, str(INIT_PATH), "exec"), namespace)
    coordinator = SimpleNamespace(force_discharge=AsyncMock(return_value=succeeds))

    if succeeds:
        assert asyncio.run(namespace["run"](coordinator, 30, 10000)) is None
    else:
        with pytest.raises(HomeAssistantError, match="not confirmed"):
            asyncio.run(namespace["run"](coordinator, 30, 10000))
    coordinator.force_discharge.assert_awaited_once_with(30, power_w=7500)
