"""Pre-departure heating, run by Home Assistant instead of a YAML script.

The macro this replaces woke the car, switched the climate to HI, added the
wheel and the driver seat five minutes before departure and put the previous
settings back afterwards - with a door opening in the meantime aborting the
whole thing. Doing it here keeps the sequence next to the departure plan it
belongs to, survives an entity reload and needs no ``scripts.yaml``.

All vehicle traffic still goes through the integration's own entities, so the
permission checks and the coordinator feedback stay in one place.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any, Callable

from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

#: The climate setpoint the car treats as HI.
HEAT_SETPOINT = 31.0
#: Wheel and driver seat join this long before the departure time.
WHEEL_LEAD = timedelta(minutes=5)
#: Grace after the departure time before heating is switched off again.
HEAT_TAIL = timedelta(seconds=30)
#: How long the wake-up may take before the sequence gives up on it.
WAKE_TIMEOUT = 45
#: Pause after the wake request; the car needs it before commands land.
WAKE_DELAY = 15
#: Pause between the setpoint and the switch-off, so the car takes the change.
SETPOINT_DELAY = 10
#: Polling step while waiting for the car, the clock or a door.
POLL = 5
#: Seat level used for the driver.
SEAT_LEVEL = "high"

#: Registry keys of the entities the sequence drives.
KEYS = {
    "climate": "hvac_climate",
    "heat": "heat_everything",
    "wheel": "steering_wheel_heating",
    "seat": "driver_seat_heating_level_select",
    "wake": "wake",
    "online": "online",
    "doors": (
        "door_front_left",
        "door_front_right",
        "door_rear_left",
        "door_rear_right",
    ),
}

DOOR_DOMAIN = "binary_sensor"
HVAC_ACTIVE = ("heat", "heat_cool", "cool", "auto", "dry", "fan_only")


def entity_ids(hass: HomeAssistant, entry_id: str) -> dict[str, Any]:
    """Map the sequence's roles to entity ids, following any user rename."""
    registry = er.async_get(hass)
    known: dict[str, str] = {}
    for entity in er.async_entries_for_config_entry(registry, entry_id):
        unique_id = entity.unique_id or ""
        for role, key in KEYS.items():
            keys = key if isinstance(key, tuple) else (key,)
            for one in keys:
                if unique_id.endswith(f"_{one}"):
                    known.setdefault(one, entity.entity_id)
    resolved = {
        role: known[key]
        for role, key in KEYS.items()
        if isinstance(key, str) and key in known
    }
    resolved["doors"] = [known[one] for one in KEYS["doors"] if one in known]
    return resolved


def heating_snapshot(hass: HomeAssistant, ids: dict[str, Any]) -> dict[str, Any]:
    """Read what has to be put back after the heating ends."""
    climate = hass.states.get(ids.get("climate", ""))
    wheel = hass.states.get(ids.get("wheel", ""))
    seat = hass.states.get(ids.get("seat", ""))
    return {
        "temperature": (climate.attributes or {}).get("temperature") if climate else None,
        "hvac": climate.state if climate else None,
        "wheel": wheel.state if wheel else None,
        "seat": seat.state if seat else None,
        "captured_at": dt_util.utcnow().isoformat(),
    }


class QuickDepartureHeating:
    """Drive the pre-departure heating for one quick departure."""

    def __init__(self, hass: HomeAssistant, entry_id: str, target: datetime) -> None:
        """Store the departure the sequence is aimed at."""
        self._hass = hass
        self._entry_id = entry_id
        self._target = target
        self._task: asyncio.Task | None = None
        self._snapshot: dict[str, Any] | None = None

    @property
    def snapshot(self) -> dict[str, Any] | None:
        """Return the settings that will be restored."""
        return self._snapshot

    @property
    def running(self) -> bool:
        """Return whether the sequence is still working."""
        return self._task is not None and not self._task.done()

    async def async_start(self, snapshot: dict[str, Any]) -> None:
        """Store the snapshot and run the sequence in the background."""
        self._snapshot = dict(snapshot)
        self._task = self._hass.async_create_task(
            self._async_run(), "chery quick departure heating", eager_start=False
        )

    async def async_retarget(self, target: datetime) -> None:
        """Aim a running sequence at a later departure, keeping the snapshot."""
        self._target = target
        await self._async_stop()
        if self._snapshot is None:
            return
        self._task = self._hass.async_create_task(
            self._async_run(), "chery quick departure heating", eager_start=False
        )

    def adopt(self, snapshot: dict[str, Any]) -> None:
        """Take over a snapshot that was saved before Home Assistant stopped."""
        self._snapshot = dict(snapshot)

    async def async_stop(self) -> None:
        """Stop the sequence without touching the car."""
        await self._async_stop()

    async def _async_stop(self) -> None:
        """Stop the background task without touching the car."""
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    async def async_cancel(self) -> None:
        """Stop the sequence and put the previous settings back."""
        await self._async_stop()
        await self.async_restore()

    async def async_restore(self) -> None:
        """Restore the settings captured before the heating started."""
        if self._snapshot is None:
            return
        snapshot, self._snapshot = self._snapshot, None
        await restore_heating(self._hass, self._entry_id, snapshot)

    async def _async_run(self) -> None:
        """Wake the car, heat it, add wheel and seat, then restore."""
        hass, ids = self._hass, entity_ids(self._hass, self._entry_id)
        try:
            await self._call("button", "press", ids.get("wake"))
            await asyncio.sleep(WAKE_DELAY)
            await self._wait(lambda: self._online(ids), WAKE_TIMEOUT)
            await self._call("switch", "turn_on", ids.get("heat"))
            early = self._target - WHEEL_LEAD
            if await self._wait(lambda: self._door_open(ids), self._seconds(early)):
                await self._async_finish("drzwi otwarte w trakcie grzania")
                return
            await self._call("switch", "turn_on", ids.get("wheel"))
            await self._call(
                "select", "select_option", ids.get("seat"), option=SEAT_LEVEL
            )
            late = self._target + HEAT_TAIL
            if await self._wait(lambda: self._door_open(ids), self._seconds(late)):
                await self._async_finish("drzwi otwarte przed wyjazdem")
                return
            await self._async_finish(None)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a broken sequence must not kill the entity
            _LOGGER.exception("Quick departure heating failed")
            await self._async_finish("błąd sekwencji grzania", error=True)
        finally:
            _LOGGER.debug("Quick departure heating for entry %s finished", hass)

    async def _async_finish(self, reason: str | None, *, error: bool = False) -> None:
        """Put the settings back and tell the user how it ended."""
        await self.async_restore()
        if error:
            message = "Sekwencja grzania przerwana błędem - nastawa przywrócona."
        elif reason:
            message = f"Grzanie przerwane ({reason}), nastawa przywrócona."
        else:
            message = (
                f"Grzanie zakończone, nastawa przywrócona, klimatyzacja wyłączona."
            )
        persistent_notification.async_create(
            self._hass,
            message,
            title="Tiggo - grzanie przed wyjazdem",
            notification_id=f"{DOMAIN}_quick_departure",
        )

    def _seconds(self, moment: datetime) -> float:
        """Return how long to wait for a moment, never negative."""
        return max(0.0, (moment - dt_util.now()).total_seconds())

    def _online(self, ids: dict[str, Any]) -> bool:
        """Return whether the car answered the wake-up."""
        state = self._hass.states.get(ids.get("online", ""))
        return state is not None and state.state == "on"

    def _door_open(self, ids: dict[str, Any]) -> bool:
        """Return whether any door is open, i.e. somebody got in."""
        return any(
            (state := self._hass.states.get(entity_id)) is not None
            and state.state == "on"
            for entity_id in ids.get("doors", [])
        )

    async def _wait(self, predicate: Callable[[], bool], timeout: float) -> bool:
        """Wait until the predicate holds or the timeout passes."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if predicate():
                return True
            await asyncio.sleep(POLL)
        return predicate()

    async def _call(self, domain: str, service: str, entity_id: str | None, **data: Any) -> None:
        """Call one vehicle entity, skipping roles the vehicle does not offer."""
        if not entity_id:
            _LOGGER.debug("Skipping %s.%s: entity not found", domain, service)
            return
        await self._hass.services.async_call(
            domain, service, {"entity_id": entity_id, **data}, blocking=True
        )


async def restore_heating(
    hass: HomeAssistant, entry_id: str, snapshot: dict[str, Any]
) -> None:
    """Put the climate, the wheel and the driver seat back as they were."""
    ids = entity_ids(hass, entry_id)
    temperature = snapshot.get("temperature")
    try:
        if temperature is not None and ids.get("climate"):
            await hass.services.async_call(
                "climate",
                "set_temperature",
                {"entity_id": ids["climate"], "temperature": float(temperature)},
                blocking=True,
            )
            await asyncio.sleep(SETPOINT_DELAY)
        hvac = snapshot.get("hvac")
        if ids.get("climate"):
            if hvac in HVAC_ACTIVE:
                await hass.services.async_call(
                    "climate",
                    "set_hvac_mode",
                    {"entity_id": ids["climate"], "hvac_mode": hvac},
                    blocking=True,
                )
            else:
                await hass.services.async_call(
                    "climate", "turn_off", {"entity_id": ids["climate"]}, blocking=True
                )
        if ids.get("wheel") and snapshot.get("wheel") in ("on", "off"):
            await hass.services.async_call(
                "switch",
                "turn_on" if snapshot["wheel"] == "on" else "turn_off",
                {"entity_id": ids["wheel"]},
                blocking=True,
            )
        if ids.get("seat") and snapshot.get("seat") in ("off", "low", "medium", "high"):
            await hass.services.async_call(
                "select",
                "select_option",
                {"entity_id": ids["seat"], "option": snapshot["seat"]},
                blocking=True,
            )
    except Exception:  # noqa: BLE001 - restoring is best effort
        _LOGGER.exception("Restoring the settings before the quick departure failed")
