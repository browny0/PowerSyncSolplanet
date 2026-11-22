"""Helpers for selecting and restoring Solplanet meter limits."""

from __future__ import annotations

import copy
from typing import Any

SOLPLANET_DOMAIN = "solplanet"
SOLPLANET_METER_PREFIX = "meter_"


def solplanet_meter_devices(
    hass: Any, solplanet_entry_id: str | None
) -> dict[str, dict[str, str]]:
    """Return registered meter devices belonging to the selected gateway."""
    if not solplanet_entry_id:
        return {}

    from homeassistant.helpers import device_registry as dr

    registry = dr.async_get(hass)
    runtime = hass.data.get(SOLPLANET_DOMAIN, {}).get(solplanet_entry_id)
    runtime_data = getattr(runtime, "data", None)
    runtime_meters = (
        runtime_data.get("meter", {})
        if isinstance(runtime_data, dict)
        else {}
    )
    meters: dict[str, dict[str, str]] = {}
    for device in registry.devices.values():
        if solplanet_entry_id not in device.config_entries:
            continue
        meter_isn = next(
            (
                identifier.removeprefix(SOLPLANET_METER_PREFIX)
                for domain, identifier in device.identifiers
                if domain == SOLPLANET_DOMAIN
                and identifier.startswith(SOLPLANET_METER_PREFIX)
            ),
            None,
        )
        if not meter_isn:
            continue
        meter_entry = runtime_meters.get(meter_isn)
        if not isinstance(meter_entry, dict) or not meter_entry.get("meter_req"):
            continue
        meters[device.id] = {
            "meter_isn": meter_isn,
            "name": device.name_by_user or device.name or meter_isn,
        }
    return meters


def resolve_solplanet_meter(
    meters: dict[str, dict[str, str]], configured_device_id: str | None
) -> tuple[str, str] | None:
    """Resolve the configured meter, or auto-select only when there is one."""
    if configured_device_id:
        meter = meters.get(configured_device_id)
        if meter:
            return configured_device_id, meter["meter_isn"]
        return None
    if len(meters) == 1:
        device_id, meter = next(iter(meters.items()))
        return device_id, meter["meter_isn"]
    return None


def zero_export_limit_service_data(device_id: str) -> dict[str, Any]:
    """Build the Solplanet power-mode action used for zero-export curtailment."""
    return {
        "device_id": device_id,
        "abs": 0,
        "limitType": 0,
        "target": 0,
        "powerDiff": 0,
        "lostTime": 30,
        "lostPowerMax": 5000,
    }


def meter_restore_payload(meter_req: dict[str, Any]) -> dict[str, Any]:
    """Validate and preserve the complete captured meter settings payload."""
    if not isinstance(meter_req, dict):
        raise TypeError("Solplanet meter settings must be a mapping")

    regulate = _required_int(meter_req, "regulate")
    if regulate == 5:
        return copy.deepcopy(meter_req)
    if regulate != 10:
        raise ValueError(f"Unsupported Solplanet meter regulate mode: {regulate}")

    ctrl_type = _required_int(meter_req, "ctrlType")
    if ctrl_type == 0:
        limit_type = _required_int(meter_req, "limitType")
        _required_int(meter_req, "abs")
        _required_int(meter_req, "powerDiff")
        _required_int(meter_req, "lostTime")
        _required_int(meter_req, "lostPowerMax")
        if limit_type == 0:
            _required_int(meter_req, "target")
        elif limit_type == 1:
            _required_int(meter_req, "targetPer")
        else:
            raise ValueError(f"Unsupported Solplanet meter limit type: {limit_type}")
        return copy.deepcopy(meter_req)

    if ctrl_type == 1:
        for key in (
            "maxOutCurr",
            "maxInCurr",
            "currDiff",
            "lostTime",
            "lostCurrMax",
        ):
            _required_int(meter_req, key)
        return copy.deepcopy(meter_req)

    if ctrl_type == 2:
        _required_int(meter_req, "lostTime")
        return copy.deepcopy(meter_req)

    raise ValueError(f"Unsupported Solplanet meter control type: {ctrl_type}")


def _required_int(payload: dict[str, Any], key: str) -> int:
    if key not in payload:
        raise ValueError(f"Solplanet meter settings are missing {key}")
    value = payload[key]
    if isinstance(value, bool) or (
        isinstance(value, float) and not value.is_integer()
    ):
        raise ValueError(f"Solplanet meter setting {key} is invalid")
    try:
        return int(value)
    except (TypeError, ValueError) as err:
        raise ValueError(f"Solplanet meter setting {key} is invalid") from err
