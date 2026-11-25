"""Status/recommendation views must add the EV's own draw back into surplus."""

import ast
import asyncio
import copy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

INIT = Path(__file__).parents[1] / "custom_components/power_sync/__init__.py"


def test_no_view_computes_surplus_with_a_hardcoded_zero_ev_draw():
    calls = [
        node for node in ast.walk(ast.parse(INIT.read_text()))
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", None) == "_calculate_solar_surplus"
    ]
    assert calls
    for call in calls:
        ev_arg = call.args[1] if len(call.args) > 1 else None
        assert not (isinstance(ev_arg, ast.Constant) and ev_arg.value == 0), ast.unparse(call)


def test_views_keep_the_coordinator_home_load_basis():
    """Adding the car's draw back is only right when load includes it.

    Tesla/Sigenergy coordinators publish ``home_load_basis="excludes_ev"``;
    dropping it made the direct method subtract the car twice (8 kW solar,
    1 kW house, 7 kW car read 13.5 kW available instead of 6.5 kW). Both
    views read the site through the shared builder, which keeps the basis.
    """
    import importlib.util

    tree = ast.parse(INIT.read_text())
    for name in ("SolarSurplusStatusView", "PriceRecommendationView"):
        view = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == name)
        dicts = [
            value
            for node in ast.walk(view)
            if isinstance(node, ast.Assign)
            and any(getattr(t, "id", None) == "live_status" for t in node.targets)
            for value in ast.walk(node.value)
            if isinstance(value, ast.Dict)
        ]
        assert dicts, name
        for value in dicts:
            keys = {k.value for k in value.keys if isinstance(k, ast.Constant)}
            assert "home_load_basis" in keys, name
        reads = [

            node for node in ast.walk(view)
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "_surplus_view_site"
        ]
        assert len(reads) == 4, name  # tesla, sigenergy, sungrow, foxess

    path = INIT.parent / "automations" / "live_status.py"
    spec = importlib.util.spec_from_file_location("live_status_basis_test", path)
    live_status_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(live_status_module)
    for basis in ("excludes_ev", "includes_ev"):
        status = live_status_module.coordinator_data_to_ev_live_status({
            "solar_power": 8.0, "load_power": 1.0, "grid_power": 0.0,
            "battery_power": 0.0, "ev_power": 7.0, "home_load_basis": basis,
        })
        assert status["home_load_basis"] == basis


def test_solar_status_uses_normalized_solplanet_telemetry_and_ev_power():
    tree = ast.parse(INIT.read_text())
    view = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "SolarSurplusStatusView"
    )
    method = copy.deepcopy(next(
        node for node in view.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "get"
    ))
    method.returns = None
    for arg in method.args.args:
        arg.annotation = None

    class StripImports(ast.NodeTransformer):
        def visit_ImportFrom(self, node):
            return None

    method = StripImports().visit(method)
    module = ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[]))
    entry = SimpleNamespace(entry_id="entry")
    hass = SimpleNamespace(
        config_entries=SimpleNamespace(async_entries=lambda _: [entry]),
        data={"power_sync": {"entry": {}}},
    )
    live_status = {
        "solar_power": 3000.0,
        "grid_power": -500.0,
        "battery_power": -500.0,
        "load_power": 1000.0,
        "battery_soc": 37.0,
        "home_load_basis": "excludes_ev",
    }
    calculations = []

    def calculate(status, ev_power, config):
        calculations.append((status, ev_power, config))
        return 1.0

    namespace = {
        "DOMAIN": "power_sync",
        "_dynamic_ev_state": {},
        "_calculate_solar_surplus": calculate,
        "_get_tesla_live_status": AsyncMock(return_value=live_status),
        "get_stored_solar_surplus_config": lambda _: {"surplus_calculation": "direct"},
        "_get_ev_vehicle_status": lambda *_: {"ev_power_kw": 2.0, "ev_soc": 70},
        "web": SimpleNamespace(json_response=lambda data, **_: data),
    }
    exec(compile(module, str(INIT), "exec"), namespace)
    result = asyncio.run(namespace["get"](SimpleNamespace(_hass=hass), None))

    assert result["success"] is True
    assert result["solar_power_kw"] == 3.0
    assert result["battery_soc"] == 37.0
    assert calculations == [(live_status, 2.0, {"surplus_calculation": "direct"})]

