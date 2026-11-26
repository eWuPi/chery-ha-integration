"""Give the comfort setpoint back after a HI/LO macro.

The car reports no event for "the macro run is over": it keeps the extreme
setpoint (31 °C for HI, 15 °C for LO) next to a switched-off climate. This
watcher remembers the setpoint that was in force before the macro and puts it
back as soon as the climate is off or a door opens.

Only a macro the integration sent is remembered. The trace is the
``heat_everything`` / ``cool_everything`` flag that ``apply_command_feedback``
writes into the coordinator data, so a 31 °C set by hand (or from the Chery
app) is left alone.

Nothing is sent while the departure sequence owns the climate
(``departure_restore_at`` is armed): that sequence restores the settings
itself and two writers would collide.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .command_exec import async_send_vehicle_command
from .data import HI_SETPOINT, LO_SETPOINT
from .departure import departure_restore_at
from .exceptions import CheryEuropeCommandError
from .pin import ask_for_pin
from .quick_departure import WAKE_DELAY, entity_ids

if TYPE_CHECKING:
    from .coordinator import CheryEuropeDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

#: Same tolerance the entity state uses to call 31 °C HI and 15 °C LO.
EXTREME_TOLERANCE = 0.5
#: ``ve_1104`` takes any setpoint, so it is the one used for the restore.
RESTORE_COMMAND_ID = "ve_1104"
#: Give up after this many failed restore attempts (the car may be asleep).
MAX_ATTEMPTS = 2
#: Pause between the "car asleep" answer and the single retry.
ASLEEP_RETRY_DELAY = 10
#: A snapshot older than this is dropped instead of restored.
MAX_AGE = timedelta(hours=6)
#: Doors that end a heating run early.
DOOR_FIELDS = (
    "door_front_left_open",
    "door_front_right_open",
    "door_rear_left_open",
    "door_rear_right_open",
)
#: Codes the car answers with when it has to be woken first.
ASLEEP_MARKERS = ("A07900", "asleep")

STORE_VERSION = 1
STORE_SAVE_DELAY = 2


def _extreme_of(temperature: float | None) -> float | None:
    """Return 31/15 when the setpoint sits on a macro end, else None."""
    if temperature is None:
        return None
    for extreme in (HI_SETPOINT, LO_SETPOINT):
        if abs(temperature - extreme) <= EXTREME_TOLERANCE:
            return extreme
    return None


class ComfortRestore:
    """Remember the setpoint a HI/LO macro replaced and put it back."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        coordinator: CheryEuropeDataUpdateCoordinator,
    ) -> None:
        self._hass = hass
        self._entry = entry
        self._coordinator = coordinator
        self._store: Store[dict[str, Any]] = Store(
            hass, STORE_VERSION, f"chery_europe.comfort_restore.{entry.entry_id}"
        )
        self._unsub: CALLBACK_TYPE | None = None
        self._task: asyncio.Task[None] | None = None
        #: Setpoint to put back, None when nothing is armed.
        self._previous: float | None = None
        self._captured_at: datetime | None = None
        self._attempts = 0
        #: Last normal setpoint seen, i.e. what "before the macro" means.
        self._last_comfort: float | None = None
        self._last_restore: datetime | None = None

    # ------------------------------------------------------------------ setup

    async def async_start(self) -> None:
        """Load the snapshot left by the previous run and watch the data."""
        stored = await self._store.async_load() or {}
        previous = stored.get("previous")
        captured_at = stored.get("captured_at")
        if isinstance(previous, (int, float)) and isinstance(captured_at, str):
            try:
                moment = datetime.fromisoformat(captured_at)
            except ValueError:
                moment = None
            if moment is not None and dt_util.utcnow() - moment < MAX_AGE:
                self._previous = float(previous)
                self._captured_at = moment
                _LOGGER.debug(
                    "Restored comfort snapshot %s °C from %s", previous, captured_at
                )
        self._unsub = self._coordinator.async_add_listener(self._handle_update)
        self._handle_update()

    @callback
    def stop(self) -> None:
        """Detach the watcher and persist the snapshot (config entry unload)."""
        if self._unsub is not None:
            self._unsub()
            self._unsub = None
        if self._task is not None:
            self._task.cancel()
            self._task = None
        self._store.async_delay_save(self._payload, 0)

    # ------------------------------------------------------------------- watch

    @callback
    def _handle_update(self) -> None:
        """Capture the previous setpoint and decide whether to restore."""
        data = self._coordinator.data
        if data is None:
            return

        temperature = data.target_temperature
        extreme = _extreme_of(temperature)

        if temperature is not None and extreme is None:
            self._last_comfort = temperature

        # While the departure sequence drives the climate it restores the
        # settings itself; capturing here would only add a duplicate command.
        if departure_restore_at(self._coordinator) is not None:
            return

        macro_trace = bool(data.heat_everything or data.cool_everything)

        if macro_trace and extreme is not None and self._previous is None:
            previous = self._last_comfort
            if previous is not None and _extreme_of(previous) is None:
                self._previous = previous
                self._captured_at = dt_util.utcnow()
                _LOGGER.info(
                    "HI/LO macro at %s °C: remembering %s °C", temperature, previous
                )
                self._save()

        if self._previous is None or self._task is not None:
            return
        if extreme is None:
            # Somebody already put a normal setpoint in place.
            self._forget("setpoint changed outside HI/LO")
            return
        door_open = any(getattr(data, name) is True for name in DOOR_FIELDS)
        if door_open or data.hvac_enabled is False:
            self._task = self._hass.async_create_task(
                self._async_restore(door_open=door_open),
                "chery comfort restore",
                eager_start=False,
            )

    # ----------------------------------------------------------------- restore

    async def _async_restore(self, *, door_open: bool) -> None:
        """Send the remembered setpoint back to the car."""
        reason = ""
        try:
            previous = self._previous
            data = self._coordinator.data
            if previous is None or data is None:
                return
            if ask_for_pin(self._entry):
                self._forget("restore skipped: PIN is asked for every command")
                return
            if self._captured_at is not None:
                if dt_util.utcnow() - self._captured_at > MAX_AGE:
                    self._forget("snapshot too old")
                    return
            # Unknown climate state must never switch the heating on by accident.
            enabled = data.hvac_enabled is True
            reason = "door open" if door_open and enabled else "climate off"
            self._attempts += 1
            await self._async_send(data.vin, previous, enabled)
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001 - a watcher must not break the entry
            _LOGGER.warning(
                "Could not restore the comfort setpoint (%s/%s): %s",
                self._attempts,
                MAX_ATTEMPTS,
                err,
            )
            if self._attempts >= MAX_ATTEMPTS:
                self._forget("giving up after repeated failures")
        else:
            self._last_restore = dt_util.utcnow()
            _LOGGER.info("Comfort setpoint %s °C restored (%s)", previous, reason)
            self._forget(f"restored {previous} °C")
            current = self._coordinator.data
            if current is not None and current.target_temperature != previous:
                self._coordinator.async_set_updated_data(
                    replace(current, target_temperature=previous)
                )
        finally:
            self._task = None

    async def _async_send(
        self, vin: str | None, temperature: float, enabled: bool
    ) -> None:
        """Wake the car once, then send the setpoint (one retry when asleep)."""
        wake = entity_ids(self._hass, self._entry.entry_id).get("wake")
        if wake:
            await self._hass.services.async_call(
                "button", "press", {"entity_id": wake}, blocking=True
            )
            await asyncio.sleep(WAKE_DELAY)
        for attempt in (1, 2):
            try:
                await async_send_vehicle_command(
                    self._coordinator,
                    self._entry,
                    vin,
                    {},
                    command_id=RESTORE_COMMAND_ID,
                    enabled=enabled,
                    temperature=temperature,
                )
                return
            except CheryEuropeCommandError as err:
                text = str(err)
                asleep = any(marker in text for marker in ASLEEP_MARKERS)
                if attempt == 2 or not asleep:
                    raise
                _LOGGER.debug("Car asleep during restore, retrying once: %s", text)
                await asyncio.sleep(ASLEEP_RETRY_DELAY)

    # ------------------------------------------------------------------- state

    def _payload(self) -> dict[str, Any]:
        """Snapshot as it is written to disk."""
        return {
            "previous": self._previous,
            "captured_at": self._captured_at.isoformat() if self._captured_at else None,
        }

    def _save(self) -> None:
        """Persist the snapshot soon, so a restart does not lose it."""
        self._store.async_delay_save(self._payload, STORE_SAVE_DELAY)

    def _forget(self, reason: str) -> None:
        """Drop the snapshot."""
        if self._previous is not None:
            _LOGGER.debug("Comfort snapshot cleared: %s", reason)
        self._previous = None
        self._captured_at = None
        self._attempts = 0
        self._save()