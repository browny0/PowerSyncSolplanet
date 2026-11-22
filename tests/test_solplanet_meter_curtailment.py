import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

COMPONENT_ROOT = (
    Path(__file__).resolve().parent.parent / "custom_components" / "power_sync"
)
if "power_sync" not in sys.modules:
    power_sync = types.ModuleType("power_sync")
    power_sync.__path__ = [str(COMPONENT_ROOT)]
    sys.modules["power_sync"] = power_sync

from power_sync.solplanet_meter_curtailment import (
    meter_restore_payload,
    resolve_solplanet_meter,
    solplanet_meter_devices,
    zero_export_limit_service_data,
)


def test_selects_only_meter_automatically():
    meters = {"device-1": {"meter_isn": "meter-serial", "name": "Grid meter"}}

    assert resolve_solplanet_meter(meters, None) == ("device-1", "meter-serial")
    assert resolve_solplanet_meter(meters, "device-1") == (
        "device-1",
        "meter-serial",
    )
    assert resolve_solplanet_meter(meters, "other-device") is None


def test_discovers_only_meters_from_selected_solplanet_entry(monkeypatch):
    devices = [
        SimpleNamespace(
            id="grid-meter",
            config_entries={"selected-entry"},
            identifiers={("solplanet", "meter_grid-serial")},
            name_by_user=None,
            name="Eastron Grid",
        ),
        SimpleNamespace(
            id="battery",
            config_entries={"selected-entry"},
            identifiers={("solplanet", "battery-battery-serial")},
            name_by_user=None,
            name="Battery",
        ),
        SimpleNamespace(
            id="sub-meter",
            config_entries={"selected-entry"},
            identifiers={("solplanet", "meter_submeter-serial")},
            name_by_user=None,
            name="Submeter",
        ),
        SimpleNamespace(
            id="other-meter",
            config_entries={"other-entry"},
            identifiers={("solplanet", "meter_other-serial")},
            name_by_user=None,
            name="Other gateway meter",
        ),
    ]
    registry = SimpleNamespace(devices={device.id: device for device in devices})
    device_registry = types.ModuleType("homeassistant.helpers.device_registry")
    device_registry.async_get = lambda _hass: registry
    helpers = types.ModuleType("homeassistant.helpers")
    helpers.device_registry = device_registry
    homeassistant = types.ModuleType("homeassistant")
    homeassistant.helpers = helpers
    monkeypatch.setitem(sys.modules, "homeassistant", homeassistant)
    monkeypatch.setitem(sys.modules, "homeassistant.helpers", helpers)
    monkeypatch.setitem(
        sys.modules, "homeassistant.helpers.device_registry", device_registry
    )

    hass = SimpleNamespace(
        data={
            "solplanet": {
                "selected-entry": SimpleNamespace(
                    data={
                        "meter": {
                            "grid-serial": {"meter_req": {"regulate": 5}},
                            "submeter-serial": {"app_info": {"sn": "submeter-serial"}},
                        }
                    }
                )
            }
        }
    )
    assert solplanet_meter_devices(hass, "selected-entry") == {
        "grid-meter": {"meter_isn": "grid-serial", "name": "Eastron Grid"}
    }


def test_requires_explicit_selection_when_multiple_meters_exist():
    meters = {
        "device-1": {"meter_isn": "meter-1", "name": "Grid meter"},
        "device-2": {"meter_isn": "meter-2", "name": "Other meter"},
    }

    assert resolve_solplanet_meter(meters, None) is None
    assert resolve_solplanet_meter(meters, "device-2") == (
        "device-2",
        "meter-2",
    )


def test_zero_export_action_matches_solplanet_power_service_fields():
    assert zero_export_limit_service_data("device-1") == {
        "device_id": "device-1",
        "abs": 0,
        "limitType": 0,
        "target": 0,
        "powerDiff": 0,
        "lostTime": 30,
        "lostPowerMax": 5000,
    }


def test_restores_absolute_power_limit_without_dropping_fields():
    meter_req = {
        "regulate": "10",
        "ctrlType": "0",
        "abs": "0",
        "limitType": "0",
        "target": "1200",
        "powerDiff": "10",
        "lostTime": "30",
        "lostPowerMax": "5000",
        "vendorOption": "preserved",
    }

    assert meter_restore_payload(meter_req) == meter_req


def test_restores_percentage_power_limit():
    meter_req = {
        "regulate": 10,
        "ctrlType": 0,
        "abs": 1,
        "limitType": 1,
        "targetPer": 80,
        "powerDiff": 0,
        "lostTime": 30,
        "lostPowerMax": 5000,
    }

    assert meter_restore_payload(meter_req) == meter_req


def test_restores_current_limit():
    meter_req = {
        "regulate": 10,
        "ctrlType": 1,
        "failSafe": 1,
        "usageType": 0,
        "maxOutCurr": 62,
        "maxInCurr": 62,
        "currDiff": 0,
        "lostTime": 30,
        "lostCurrMax": 20,
    }

    assert meter_restore_payload(meter_req) == meter_req


@pytest.mark.parametrize(
    "meter_req",
    [
        {"regulate": 5},
        {"regulate": 10, "ctrlType": 2, "lostTime": 30},
    ],
)
def test_restores_disabled_and_zero_power_modes(meter_req):
    assert meter_restore_payload(meter_req) == meter_req


def test_refuses_incomplete_or_unrestorable_settings():
    with pytest.raises(TypeError, match="must be a mapping"):
        meter_restore_payload(None)

    with pytest.raises(ValueError, match="missing lostCurrMax"):
        meter_restore_payload(
            {
                "regulate": 10,
                "ctrlType": 1,
                "maxOutCurr": 62,
                "maxInCurr": 62,
                "currDiff": 0,
                "lostTime": 30,
            },
        )

    with pytest.raises(ValueError, match="Unsupported Solplanet meter control type"):
        meter_restore_payload(
            {
                "regulate": 10,
                "ctrlType": 3,
            },
        )
