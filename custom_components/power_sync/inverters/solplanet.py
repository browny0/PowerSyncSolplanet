"""Solplanet battery bridge via the calvinbui Home Assistant integration."""

from __future__ import annotations

import logging
import math
from datetime import datetime
from typing import Any

from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)

_ENTITY_SUFFIXES: dict[str, tuple[str, ...]] = {
    "battery_power": ("battery_power",),
    "battery_level": ("battery_state_of_charge", "state_of_charge"),
    "solar_power": ("pv_power",),
    "grid_power": ("meter_power",),
    "work_mode": ("work_mode",),
    "schedule_input_power": ("schedule_input_power",),
    "schedule_output_power": ("schedule_output_power",),
    "schedule_configured": ("schedule_configured",),
}

_DAY_NAMES = ("Mon", "Tus", "Wen", "Thu", "Fri", "Sat", "Sun")
_MODE_SELF_CONSUMPTION = "Self-consumption mode"
_MODE_CUSTOM = "Custom mode"
_MAX_SLOT_HOURS = 4


class SolplanetBatteryController:
    """Read telemetry and control battery schedules through Home Assistant."""

    def __init__(
        self,
        hass: Any,
        config_entry_id: str,
        battery_capacity_kwh: float = 51.2,
        max_charge_kw: float = 5.0,
        max_discharge_kw: float = 5.0,
    ) -> None:
        self.hass = hass
        self._config_entry_id = config_entry_id
        self._battery_capacity_kwh = float(battery_capacity_kwh)
        self._max_charge_w = float(max_charge_kw) * 1000
        self._max_discharge_w = float(max_discharge_kw) * 1000
        self._entity_map: dict[str, str] = {}
        self._active_schedule_day: str | None = None
        self._active_schedule_slot: dict[str, int | str] | None = None

    async def connect(self) -> bool:
        """Validate the selected Solplanet entry exposes required entities."""
        self._discover_entities()
        required = tuple(_ENTITY_SUFFIXES)
        missing = [key for key in required if key not in self._entity_map]
        if missing:
            missing_entities = [
                f"{domain}.{suffix}"
                for key in missing
                for domain, suffix in [(self._entity_domain(key), _ENTITY_SUFFIXES[key][0])]
            ]
            raise ValueError(f"solplanet_missing_entities:{','.join(missing_entities)}")

        options = self._select_options()
        if options and _MODE_CUSTOM not in options:
            raise ValueError("solplanet_custom_mode_unavailable")
        if options and _MODE_SELF_CONSUMPTION not in options:
            raise ValueError("solplanet_self_consumption_unavailable")
        has_service = getattr(self.hass.services, "has_service", None)
        if callable(has_service) and not all(
            has_service("solplanet", service)
            for service in ("replace_schedule_day", "clear_schedule")
        ):
            raise ValueError("solplanet_schedule_service_unavailable")
        return True

    def _discover_entities(self) -> None:
        registry = er.async_get(self.hass)
        entries = er.async_entries_for_config_entry(registry, self._config_entry_id)
        entity_map: dict[str, str] = {}
        for key, suffixes in _ENTITY_SUFFIXES.items():
            domain = self._entity_domain(key)
            for entry in entries:
                if entry.domain != domain:
                    continue
                identities = (entry.entity_id.lower(), (entry.unique_id or "").lower())
                if any(identity.endswith(suffix) for identity in identities for suffix in suffixes):
                    entity_map[key] = entry.entity_id
                    break
        optional_min_soc = self._find_optional_entity_in_entries(entries, "number", ("soc_min",))
        if optional_min_soc:
            entity_map["min_soc"] = optional_min_soc
        self._entity_map = entity_map

    @staticmethod
    def _entity_domain(key: str) -> str:
        if key == "work_mode":
            return "select"
        if key in ("schedule_input_power", "schedule_output_power"):
            return "number"
        if key == "schedule_configured":
            return "binary_sensor"
        return "sensor"

    def _select_options(self) -> tuple[str, ...]:
        entity_id = self._entity_map.get("work_mode")
        state = self.hass.states.get(entity_id) if entity_id else None
        options = state.attributes.get("options", ()) if state else ()
        return tuple(str(option) for option in options)

    def _read_number(self, key: str) -> float | None:
        entity_id = self._entity_map.get(key)
        state = self.hass.states.get(entity_id) if entity_id else None
        if state is None or state.state in ("unknown", "unavailable", ""):
            return None
        try:
            value = float(state.state)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(value):
            return None
        unit = str(state.attributes.get("unit_of_measurement", "")).lower()
        if unit in ("kw", "kilowatt", "kilowatts"):
            value *= 1000
        elif unit in ("mw", "megawatt", "megawatts"):
            value *= 1_000_000
        return value

    def get_status(self) -> dict[str, Any]:
        """Return PowerSync-canonical power and battery values in kW."""
        battery_w = self._read_number("battery_power")
        grid_w = self._read_number("grid_power")
        solar_w = self._read_number("solar_power")
        soc = self._read_number("battery_level")
        telemetry_ready = all(value is not None for value in (battery_w, grid_w, solar_w, soc))
        battery_kw = (battery_w or 0.0) / 1000
        grid_kw = (grid_w or 0.0) / 1000
        solar_kw = max(0.0, solar_w or 0.0) / 1000
        return {
            "telemetry_ready": telemetry_ready,
            "battery_power": battery_kw,
            "battery_level": soc,
            "solar_power": solar_kw,
            "grid_power": grid_kw,
            "load_power": max(0.0, solar_kw + grid_kw + battery_kw),
            "load_power_estimated": True,
            "battery_capacity_kwh": self._battery_capacity_kwh,
            "battery_max_charge_power_w": self._max_charge_w,
            "battery_max_discharge_power_w": self._max_discharge_w,
            "mode": self._current_mode(),
        }

    def _current_mode(self) -> str | None:
        entity_id = self._entity_map.get("work_mode")
        state = self.hass.states.get(entity_id) if entity_id else None
        if not state or state.state in ("unknown", "unavailable", ""):
            return None
        return str(state.state)

    async def force_charge(self, duration_minutes: int, power_w: int | float) -> bool:
        """Schedule charging at the configured charge and discharge limits."""
        del power_w
        return await self._set_temporary_schedule("charge", duration_minutes)

    async def force_discharge(self, duration_minutes: int, power_w: int | float) -> bool:
        """Schedule discharging at the configured charge and discharge limits."""
        del power_w
        return await self._set_temporary_schedule("discharge", duration_minutes)

    async def set_backup_mode(self) -> bool:
        """Hold both battery directions at zero using the custom schedule."""
        _, start_hour, start_minute = self._current_slot_start()
        now = dt_util.now()
        start_total_minutes = start_hour * 60 + start_minute
        elapsed_since_boundary = now.minute - start_minute
        available_slot_hours = min(
            _MAX_SLOT_HOURS,
            (24 * 60 - start_total_minutes) // 60,
        )
        duration_minutes = (
            available_slot_hours * 60 - elapsed_since_boundary
        )
        if duration_minutes <= 0:
            _LOGGER.error(
                "Solplanet cannot create a valid zero-power hold slot before midnight"
            )
            return False
        return await self._set_temporary_schedule("hold", duration_minutes)

    async def _set_temporary_schedule(
        self,
        mode: str,
        duration_minutes: int,
    ) -> bool:
        await self._ensure_entities()
        if duration_minutes <= 0:
            _LOGGER.error("Solplanet dispatch duration must be positive")
            return False
        try:
            day_index, start_hour, start_minute = self._current_slot_start()
            day = _DAY_NAMES[day_index]
            now = dt_util.now()
            elapsed_since_boundary = now.minute - start_minute
            duration_hours = max(
                1,
                math.ceil((elapsed_since_boundary + duration_minutes) / 60),
            )
            if duration_hours > _MAX_SLOT_HOURS:
                _LOGGER.error(
                    "Solplanet dispatch duration exceeds the %d-hour schedule-slot limit",
                    _MAX_SLOT_HOURS,
                )
                return False
            if (start_hour * 60) + start_minute + (duration_hours * 60) > 24 * 60:
                _LOGGER.error(
                    "Solplanet dispatch would cross midnight and cannot be scheduled"
                )
                return False
            pin = 0 if mode == "hold" else int(round(self._max_charge_w))
            pout = 0 if mode == "hold" else int(round(self._max_discharge_w))

            new_slot: dict[str, int | str] = {
                "start_hour": start_hour,
                "start_minute": start_minute,
                "duration": duration_hours,
                "mode": "discharge" if mode == "discharge" else "charge",
            }
            await self._replace_schedule_day(
                day,
                [new_slot],
                pin=pin,
                pout=pout,
            )
            await self._set_mode(_MODE_CUSTOM)
            self._active_schedule_day = day
            self._active_schedule_slot = new_slot
            _LOGGER.info(
                "Solplanet temporary %s schedule set for %s %02d:%02d (%d hour(s))",
                mode,
                day,
                start_hour,
                start_minute,
                duration_hours,
            )
            return True
        except Exception:
            _LOGGER.exception("Solplanet temporary %s schedule failed", mode)
            return False

    @staticmethod
    def _current_slot_start() -> tuple[int, int, int]:
        """Return the most recent local half-hour boundary."""
        now: datetime = dt_util.now()
        return now.weekday(), now.hour, 30 if now.minute >= 30 else 0

    async def restore_normal(self) -> bool:
        await self._ensure_entities()
        try:
            day = self._active_schedule_day
            if day is not None and self._active_schedule_slot is not None:
                await self._clear_schedule()
            await self._set_mode(_MODE_SELF_CONSUMPTION)
            self._active_schedule_day = None
            self._active_schedule_slot = None
            return True
        except Exception:
            _LOGGER.exception("Solplanet restore to self-consumption failed")
            return False

    async def restore_work_mode_from_idle(self) -> bool:
        return await self.restore_normal()

    async def set_backup_reserve(self, percent: int) -> bool:
        await self._ensure_entities()
        entity_id = self._find_optional_entity("number", ("soc_min",))
        if not entity_id:
            _LOGGER.warning("Solplanet minimum SOC entity was not found")
            return False
        try:
            await self.hass.services.async_call(
                "number",
                "set_value",
                {"entity_id": entity_id, "value": int(percent)},
                blocking=True,
            )
            return True
        except Exception:
            _LOGGER.exception("Solplanet minimum SOC update failed")
            return False

    async def get_backup_reserve(self) -> int | None:
        await self._ensure_entities()
        value = self._read_number("min_soc")
        return int(round(value)) if value is not None else None

    async def _ensure_entities(self) -> None:
        if not self._entity_map:
            self._discover_entities()
        missing = [key for key in _ENTITY_SUFFIXES if key not in self._entity_map]
        if missing:
            raise ValueError(f"solplanet_missing_entities:{','.join(missing)}")

    async def _replace_schedule_day(
        self,
        day: str,
        slots: list[dict[str, int | str]],
        *,
        pin: int | None = None,
        pout: int | None = None,
    ) -> None:
        data: dict[str, Any] = {
            "entity_id": self._entity_map["schedule_configured"],
            "day": day,
            "slots": slots,
        }
        if pin is not None:
            data["pin"] = pin
        if pout is not None:
            data["pout"] = pout
        await self.hass.services.async_call(
            "solplanet",
            "replace_schedule_day",
            data,
            blocking=True,
        )

    async def _clear_schedule(self) -> None:
        await self.hass.services.async_call(
            "solplanet",
            "clear_schedule",
            {
                "entity_id": self._entity_map["schedule_configured"],
                "day": "all",
            },
            blocking=True,
        )

    async def _set_mode(self, option: str) -> None:
        entity_id = self._entity_map["work_mode"]
        options = self._select_options()
        if options and option not in options:
            raise ValueError(f"Solplanet work mode is not available: {option}")
        await self.hass.services.async_call(
            "select",
            "select_option",
            {"entity_id": entity_id, "option": option},
            blocking=True,
        )

    def _find_optional_entity(self, domain: str, suffixes: tuple[str, ...]) -> str | None:
        registry = er.async_get(self.hass)
        entries = er.async_entries_for_config_entry(registry, self._config_entry_id)
        return self._find_optional_entity_in_entries(entries, domain, suffixes)

    @staticmethod
    def _find_optional_entity_in_entries(
        entries: list[Any],
        domain: str,
        suffixes: tuple[str, ...],
    ) -> str | None:
        for entry in entries:
            if entry.domain != domain:
                continue
            identities = (entry.entity_id.lower(), (entry.unique_id or "").lower())
            if any(identity.endswith(suffix) for identity in identities for suffix in suffixes):
                return entry.entity_id
        return None
