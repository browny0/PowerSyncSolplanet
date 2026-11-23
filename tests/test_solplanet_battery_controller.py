"""Tests for the Solplanet Home Assistant battery bridge."""

from __future__ import annotations

import asyncio
import sys
import types
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
COMPONENT_ROOT = ROOT / "custom_components" / "power_sync"


def _install_stubs() -> None:
    ha_root = types.ModuleType("homeassistant")
    ha_helpers = types.ModuleType("homeassistant.helpers")
    ha_entity_registry = types.ModuleType("homeassistant.helpers.entity_registry")
    ha_util = types.ModuleType("homeassistant.util")
    ha_dt = types.ModuleType("homeassistant.util.dt")

    ha_entity_registry.async_get = lambda hass: hass.entity_registry
    ha_entity_registry.async_entries_for_config_entry = (
        lambda registry, entry_id: registry.entries_for(entry_id)
    )
    ha_dt.now = lambda: datetime(2026, 11, 19, 14, 52)

    ha_helpers.entity_registry = ha_entity_registry
    ha_util.dt = ha_dt
    ha_root.helpers = ha_helpers
    ha_root.util = ha_util

    sys.modules["homeassistant"] = ha_root
    sys.modules["homeassistant.helpers"] = ha_helpers
    sys.modules["homeassistant.helpers.entity_registry"] = ha_entity_registry
    sys.modules["homeassistant.util"] = ha_util
    sys.modules["homeassistant.util.dt"] = ha_dt

    power_sync = types.ModuleType("power_sync")
    power_sync.__path__ = [str(COMPONENT_ROOT)]
    sys.modules["power_sync"] = power_sync
    inverters = types.ModuleType("power_sync.inverters")
    inverters.__path__ = [str(COMPONENT_ROOT / "inverters")]
    sys.modules["power_sync.inverters"] = inverters


_install_stubs()

from power_sync.inverters.solplanet import SolplanetBatteryController


class _FakeState:
    def __init__(
        self,
        entity_id: str,
        state: str,
        *,
        options: list[str] | None = None,
        unit: str = "W",
    ):
        self.entity_id = entity_id
        self.state = state
        self.attributes = {"unit_of_measurement": unit}
        if options is not None:
            self.attributes["options"] = options


class _FakeStates:
    def __init__(self, states: list[_FakeState]):
        self._states = {state.entity_id: state for state in states}

    def get(self, entity_id: str | None):
        return self._states.get(entity_id or "")

    def set(self, entity_id: str, state: str) -> None:
        old = self._states.get(entity_id)
        self._states[entity_id] = _FakeState(
            entity_id,
            state,
            options=old.attributes.get("options") if old else None,
        )


class _FakeRegistry:
    def __init__(self, entity_ids: list[str]):
        self._entries = [
            SimpleNamespace(
                entity_id=entity_id,
                unique_id=entity_id.split(".", 1)[1],
                domain=entity_id.split(".", 1)[0],
                platform="solplanet",
            )
            for entity_id in entity_ids
        ]

    def entries_for(self, entry_id: str):
        assert entry_id == "solplanet-entry"
        return self._entries


class _FakeServices:
    def __init__(
        self,
        states: _FakeStates,
        *,
        replace_schedule: bool = True,
        clear_schedule: bool = True,
    ):
        self.states = states
        self.replace_schedule = replace_schedule
        self.clear_schedule = clear_schedule
        self.calls: list[tuple[str, str, dict]] = []

    def has_service(self, domain: str, service: str) -> bool:
        return domain == "solplanet" and (
            (service == "replace_schedule_day" and self.replace_schedule)
            or (service == "clear_schedule" and self.clear_schedule)
        )

    async def async_call(
        self,
        domain: str,
        service: str,
        data: dict,
        blocking: bool = True,
    ):
        self.calls.append((domain, service, dict(data)))
        if domain == "select" and service == "select_option":
            self.states.set(data["entity_id"], data["option"])
        elif domain == "number" and service == "set_value":
            self.states.set(data["entity_id"], str(data["value"]))
        elif domain == "solplanet" and service == "replace_schedule_day":
            state = self.states.get(data["entity_id"])
            raw_schedule = state.attributes["raw_schedule"]
            raw_schedule[data["day"]] = [
                0x3C02
                + (slot["start_hour"] * 0x1000000)
                + ((slot["start_minute"] // 30) * 0x1E0000)
                + ((slot["duration"] - 1) * 0x3C00)
                + (1 if slot["mode"] == "discharge" else 0)
                for slot in data["slots"]
            ]
            if "pin" in data:
                state.attributes["pin"] = data["pin"]
            if "pout" in data:
                state.attributes["pout"] = data["pout"]
        elif domain == "solplanet" and service == "clear_schedule":
            state = self.states.get(data["entity_id"])
            for day in state.attributes["raw_schedule"]:
                state.attributes["raw_schedule"][day] = []


class _FakeHass:
    def __init__(self):
        states = [
            _FakeState("sensor.battery_eq1_battery_power", "-2000"),
            _FakeState("sensor.battery_eq1_battery_state_of_charge", "72"),
            _FakeState("sensor.battery_eq1_battery_state_of_health", "96"),
            _FakeState("sensor.battery_eq1_pv_power", "4000"),
            _FakeState("sensor.eastron_meter_power", "500"),
            _FakeState(
                "select.battery_eq1_work_mode",
                "Self-consumption mode",
                options=[
                    "Self-consumption mode",
                    "Reserve power mode",
                    "Time of use mode",
                    "Custom mode",
                ],
            ),
            _FakeState("number.battery_eq1_schedule_input_power", "0"),
            _FakeState("number.battery_eq1_schedule_output_power", "0"),
            _FakeState("binary_sensor.battery_eq1_schedule_configured", "off"),
        ]
        self.states = _FakeStates(states)
        schedule_state = self.states.get(
            "binary_sensor.battery_eq1_schedule_configured"
        )
        schedule_state.attributes.update(
            {
                "raw_schedule": {
                    day: []
                    for day in ("Mon", "Tus", "Wen", "Thu", "Fri", "Sat", "Sun")
                },
                "pin": 0,
                "pout": 0,
            }
        )
        self.services = _FakeServices(self.states)
        self.entity_registry = _FakeRegistry([state.entity_id for state in states])


def _controller(
    hass: _FakeHass,
    *,
    inverter_ac_limit_kw: float = 10.0,
) -> SolplanetBatteryController:
    return SolplanetBatteryController(
        hass,
        "solplanet-entry",
        battery_capacity_kwh=51.2,
        max_charge_kw=5,
        max_discharge_kw=10,
        inverter_ac_limit_kw=inverter_ac_limit_kw,
    )


def test_connect_discovers_entities_and_returns_canonical_telemetry():
    hass = _FakeHass()
    controller = _controller(hass)

    assert asyncio.run(controller.connect())
    status = controller.get_status()

    assert status["telemetry_ready"] is True
    assert status["battery_power"] == -2
    assert status["grid_power"] == 0.5
    assert status["solar_power"] == 4
    assert status["load_power"] == 2.5
    assert status["battery_level"] == 72
    assert status["battery_soh"] == 96
    assert status["battery_capacity_kwh"] == 51.2
    assert status["battery_max_charge_power_w"] == 5000
    assert status["battery_max_discharge_power_w"] == 10000


def test_unavailable_required_telemetry_does_not_publish_home_load_zero():
    hass = _FakeHass()
    hass.states._states.pop("sensor.battery_eq1_battery_power")
    controller = _controller(hass)

    assert asyncio.run(controller.connect())
    status = controller.get_status()

    assert status["telemetry_ready"] is False
    assert status["load_power"] is None


def test_soc_discovery_ignores_tesla_vehicle_state_of_charge():
    hass = _FakeHass()
    hass.states._states["sensor.tesla_car_state_of_charge"] = _FakeState(
        "sensor.tesla_car_state_of_charge",
        "100",
    )
    hass.entity_registry._entries.insert(
        0,
        SimpleNamespace(
            entity_id="sensor.tesla_car_state_of_charge",
            unique_id="tesla_car_state_of_charge",
            domain="sensor",
            platform="tesla",
        ),
    )
    controller = _controller(hass)

    assert asyncio.run(controller.connect())
    assert controller._entity_map["battery_level"] == (
        "sensor.battery_eq1_battery_state_of_charge"
    )
    assert controller.get_status()["battery_level"] == 72


def test_soh_is_optional_and_does_not_gate_telemetry():
    hass = _FakeHass()
    hass.states._states.pop("sensor.battery_eq1_battery_state_of_health")
    hass.entity_registry._entries = [
        entry
        for entry in hass.entity_registry._entries
        if not entry.entity_id.endswith("_battery_state_of_health")
    ]
    controller = _controller(hass)

    assert asyncio.run(controller.connect())
    status = controller.get_status()

    assert status["telemetry_ready"] is True
    assert status["battery_soh"] is None


def test_force_charge_caps_requested_power_at_configured_maximum():
    hass = _FakeHass()
    controller = _controller(hass)

    assert asyncio.run(controller.force_charge(30, 6000))

    calls = hass.services.calls
    assert calls[0][0:2] == ("solplanet", "replace_schedule_day")
    assert calls[0][2] == {
        "entity_id": "binary_sensor.battery_eq1_schedule_configured",
        "day": "Thu",
        "slots": [
            {
                "start_hour": 14,
                "start_minute": 30,
                "duration": 1,
                "mode": "charge",
            }
        ],
        "pin": 5000,
        "pout": 10000,
    }
    assert type(calls[0][2]["pin"]) is int
    assert calls[1][0:2] == ("select", "select_option")
    assert calls[1][2]["option"] == "Custom mode"


def test_force_charge_uses_requested_power_below_configured_maximum():
    hass = _FakeHass()
    controller = _controller(hass)

    assert asyncio.run(controller.force_charge(30, 3000))

    schedule_call = hass.services.calls[0]
    assert schedule_call[2]["pin"] == 3000
    assert type(schedule_call[2]["pin"]) is int
    assert schedule_call[2]["pout"] == 10000


@pytest.mark.parametrize("power_w", [-1, float("nan"), float("inf")])
def test_force_charge_rejects_invalid_requested_power(power_w):
    hass = _FakeHass()
    controller = _controller(hass)

    with pytest.raises(ValueError, match="finite nonnegative"):
        asyncio.run(controller.force_charge(30, power_w))

    assert hass.services.calls == []


def test_restore_clears_all_schedule_days_and_returns_to_self_consumption():
    hass = _FakeHass()
    controller = _controller(hass)

    assert asyncio.run(controller.force_discharge(60, 8000))
    hass.services.calls.clear()
    assert asyncio.run(controller.restore_normal())

    assert hass.services.calls[0][0:2] == ("solplanet", "clear_schedule")
    assert hass.services.calls[0][2] == {
        "entity_id": "binary_sensor.battery_eq1_schedule_configured",
        "day": "all",
    }
    assert hass.services.calls[1][0:2] == ("select", "select_option")
    assert hass.services.calls[1][2]["option"] == "Self-consumption mode"


def test_restore_clears_schedule_when_persisted_force_ownership_is_recovered():
    hass = _FakeHass()
    controller = _controller(hass)

    assert asyncio.run(controller.restore_normal(clear_owned_schedule=True))

    assert hass.services.calls[0][0:2] == ("solplanet", "clear_schedule")
    assert hass.services.calls[0][2]["day"] == "all"
    assert hass.services.calls[1][2]["option"] == "Self-consumption mode"


def test_dispatch_that_would_cross_midnight_fails_without_writing():
    hass = _FakeHass()
    controller = _controller(hass)
    controller._current_slot_start = lambda: (3, 23, 30)

    assert asyncio.run(controller.force_charge(60, 3000)) is False
    assert hass.services.calls == []


def test_temporary_slot_replaces_the_day_and_sets_both_power_limits():
    hass = _FakeHass()
    hass.states.get(
        "binary_sensor.battery_eq1_schedule_configured"
    ).attributes["raw_schedule"]["Thu"] = [
        0x3C02 + (8 * 0x1000000) + 0x3C00
    ]
    controller = _controller(hass)

    assert asyncio.run(controller.force_discharge(30, 8000))

    assert hass.services.calls[0][2]["slots"] == [
        {
            "start_hour": 14,
            "start_minute": 30,
            "duration": 1,
            "mode": "discharge",
        },
    ]
    assert hass.services.calls[0][2]["pout"] == 8000
    assert hass.services.calls[0][2]["pin"] == 5000


def test_force_discharge_respects_requested_power_and_solar_headroom():
    hass = _FakeHass()
    controller = _controller(hass, inverter_ac_limit_kw=10)

    assert asyncio.run(
        controller.force_discharge(30, 8000, solar_forecast_kw=4)
    )
    assert hass.services.calls[0][2]["pout"] == 6000

    hass.services.calls.clear()
    assert asyncio.run(
        controller.force_discharge(30, 3000, solar_forecast_kw=4)
    )
    assert hass.services.calls[0][2]["pout"] == 3000

    hass.services.calls.clear()
    assert asyncio.run(
        controller.force_discharge(30, 8000, solar_forecast_kw=12)
    )
    assert hass.services.calls[0][2]["pout"] == 0


def test_force_discharge_without_forecast_uses_requested_power():
    hass = _FakeHass()
    controller = _controller(hass)

    assert asyncio.run(controller.force_discharge(30, 8000))
    assert hass.services.calls[0][2]["pout"] == 8000

    hass.services.calls.clear()
    assert asyncio.run(controller.force_discharge(30, 12000))
    assert hass.services.calls[0][2]["pout"] == 10000


def test_forecast_power_change_requests_schedule_refresh_only_when_limit_changes():
    hass = _FakeHass()
    controller = _controller(hass, inverter_ac_limit_kw=10)

    assert asyncio.run(
        controller.force_discharge(60, 8000, solar_forecast_kw=4)
    )
    assert controller.forecast_limited_discharge_power_changed(
        8000,
        solar_forecast_kw=4,
    ) is False
    assert controller.forecast_limited_discharge_power_changed(
        8000,
        solar_forecast_kw=4.1,
    ) is False
    assert controller.forecast_limited_discharge_power_changed(
        8000,
        solar_forecast_kw=7,
    ) is True
    assert controller.forecast_limited_discharge_power_changed(
        8000,
        solar_forecast_kw=None,
    ) is True


def test_temporary_slot_replaces_an_overlapping_existing_schedule():
    hass = _FakeHass()
    hass.states.get(
        "binary_sensor.battery_eq1_schedule_configured"
    ).attributes["raw_schedule"]["Thu"] = [
        0x3C02 + (14 * 0x1000000) + 0x1E0000
    ]
    controller = _controller(hass)

    assert asyncio.run(controller.force_charge(30, 3000))
    assert hass.services.calls[0][0:2] == ("solplanet", "replace_schedule_day")
    assert hass.services.calls[0][2]["slots"] == [
        {
            "start_hour": 14,
            "start_minute": 30,
            "duration": 1,
            "mode": "charge",
        }
    ]


def test_hold_creates_a_zero_power_slot():
    hass = _FakeHass()
    controller = _controller(hass)

    assert asyncio.run(controller.set_backup_mode())

    schedule_call = hass.services.calls[0]
    assert schedule_call[0:2] == ("solplanet", "replace_schedule_day")
    assert schedule_call[2]["slots"] == [
        {
            "start_hour": 14,
            "start_minute": 30,
            "duration": 4,
            "mode": "charge",
        }
    ]
    assert schedule_call[2]["pin"] == 0
    assert schedule_call[2]["pout"] == 0


def test_schedule_actions_are_required():
    hass = _FakeHass()
    hass.services = _FakeServices(hass.states, replace_schedule=False)
    controller = _controller(hass)

    try:
        asyncio.run(controller.connect())
    except ValueError as exc:
        assert str(exc) == "solplanet_schedule_service_unavailable"
    else:
        raise AssertionError("Expected missing schedule action to fail")


def test_clear_schedule_action_is_required():
    hass = _FakeHass()
    hass.services = _FakeServices(hass.states, clear_schedule=False)
    controller = _controller(hass)

    try:
        asyncio.run(controller.connect())
    except ValueError as exc:
        assert str(exc) == "solplanet_schedule_service_unavailable"
    else:
        raise AssertionError("Expected missing Clear Schedule action to fail")
