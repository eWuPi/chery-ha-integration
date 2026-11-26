"""Switch platform for safe Chery Europe remote commands."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import dt as dt_util

from .charge_schedule import plan_duration_hours, plan_start_time
from .command_exec import async_send_vehicle_command
from .const import SWITCH
from .coordinator import CheryEuropeDataUpdateCoordinator
from .data import CheryData, climate_at_hi, climate_at_lo
from .departure import (
    QUICK_DEPARTURE_MINUTES,
    async_cancel_departure_restore,
    async_restore_departure,
    async_set_departure,
    async_set_departure_days,
    departure_days,
    departure_restore_at,
    departure_restore_at_iso,
    departure_snapshot,
    departure_state,
    schedule_departure_restore,
)
from .entity import (
    CheryEuropeEntity,
    async_sync_unsupported,
    control_permissions,
    iter_entry_coordinators,
    keep_feature,
    stable_unique_id,
    vehicle_uid,
)
from .quick_departure import (
    QuickDepartureHeating,
    entity_ids,
    heating_snapshot,
)

PARALLEL_UPDATES = 0

DEPARTURE_SWITCH_DESCRIPTION = SwitchEntityDescription(
    key="departure",
    name="Departure plan",
    translation_key="departure",
    icon="mdi:car-clock",
)

#: Minutes ahead of the switch that a quick departure is scheduled.
DEPARTURE_SOON_MINUTES = QUICK_DEPARTURE_MINUTES

DEPARTURE_SOON_SWITCH_DESCRIPTION = SwitchEntityDescription(
    key="departure_in_15_min",
    name="Departure in 15 minutes",
    translation_key="departure_in_15_min",
    icon="mdi:car-clock",
)

#: One switch per weekday, so the departure plan can be limited per day.
DEPARTURE_DAY_DESCRIPTIONS: tuple[tuple[int, SwitchEntityDescription], ...] = (
    (
        1,
        SwitchEntityDescription(
            key="departure_monday",
            name="Departure plan Monday",
            translation_key="departure_monday",
            icon="mdi:calendar-today",
        ),
    ),
    (
        2,
        SwitchEntityDescription(
            key="departure_tuesday",
            name="Departure plan Tuesday",
            translation_key="departure_tuesday",
            icon="mdi:calendar-today",
        ),
    ),
    (
        3,
        SwitchEntityDescription(
            key="departure_wednesday",
            name="Departure plan Wednesday",
            translation_key="departure_wednesday",
            icon="mdi:calendar-today",
        ),
    ),
    (
        4,
        SwitchEntityDescription(
            key="departure_thursday",
            name="Departure plan Thursday",
            translation_key="departure_thursday",
            icon="mdi:calendar-today",
        ),
    ),
    (
        5,
        SwitchEntityDescription(
            key="departure_friday",
            name="Departure plan Friday",
            translation_key="departure_friday",
            icon="mdi:calendar-today",
        ),
    ),
    (
        6,
        SwitchEntityDescription(
            key="departure_saturday",
            name="Departure plan Saturday",
            translation_key="departure_saturday",
            icon="mdi:calendar-weekend",
        ),
    ),
    (
        7,
        SwitchEntityDescription(
            key="departure_sunday",
            name="Departure plan Sunday",
            translation_key="departure_sunday",
            icon="mdi:calendar-weekend",
        ),
    ),
)



@dataclass(frozen=True, kw_only=True)
class CheryEuropeSwitchEntityDescription(SwitchEntityDescription):
    """Describes a safe Chery Europe command switch."""

    command_id: str
    state_fn: Callable[[CheryData], bool | None]
    command_values: dict[str, Any] | None = None


SWITCH_DESCRIPTIONS: tuple[CheryEuropeSwitchEntityDescription, ...] = (
    CheryEuropeSwitchEntityDescription(
        key="front_windshield_heating",
        name="Front windshield heating",
        translation_key="front_windshield_heating",
        icon="mdi:car-defrost-front",
        command_id="ve_1103",
        state_fn=lambda data: data.front_windshield_heating,
    ),
    CheryEuropeSwitchEntityDescription(
        key="front_windshield_defrost",
        name="Front windshield defrost",
        translation_key="front_windshield_defrost",
        icon="mdi:car-defrost-front",
        command_id="ve_1108",
        state_fn=lambda data: data.front_windshield_defrost,
    ),
    CheryEuropeSwitchEntityDescription(
        key="air_purification",
        name="Air purification",
        translation_key="air_purification",
        icon="mdi:air-purifier",
        command_id="ve_1109",
        state_fn=lambda data: data.air_purification,
    ),
    CheryEuropeSwitchEntityDescription(
        key="rear_window_defrost",
        name="Rear window heating",
        translation_key="rear_window_defrost",
        icon="mdi:car-defrost-rear",
        command_id="ve_1135",
        state_fn=lambda data: data.rear_window_defrost,
    ),
    CheryEuropeSwitchEntityDescription(
        key="steering_wheel_heating",
        name="Steering wheel heating",
        translation_key="steering_wheel_heating",
        icon="mdi:steering",
        command_id="ve_1203",
        state_fn=lambda data: data.steering_wheel_heating,
    ),
    # Seat heating/ventilation are three-level functions, so they live in the
    # select platform (see select.py, SEAT_SELECT_DESCRIPTIONS) instead of a
    # plain switch that could only send on/off at level 3.
    CheryEuropeSwitchEntityDescription(
        key="heat_everything",
        name="Climate HI",
        translation_key="heat_everything",
        icon="mdi:thermometer-high",
        command_id="ve_1301",
        state_fn=climate_at_hi,
    ),
    CheryEuropeSwitchEntityDescription(
        key="cool_everything",
        name="Climate LO",
        translation_key="cool_everything",
        icon="mdi:thermometer-low",
        command_id="ve_1302",
        state_fn=climate_at_lo,
    ),
    CheryEuropeSwitchEntityDescription(
        key="theft_alarm",
        name="Theft alarm",
        translation_key="theft_alarm",
        icon="mdi:shield-car",
        command_id="ve_1303",
        state_fn=lambda data: data.theft_alarm,
    ),
)

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Chery Europe switches from a config entry."""
    unsupported: list[str] = []
    entities: list[SwitchEntity] = []
    for coordinator in iter_entry_coordinators(entry):
        perms = control_permissions(coordinator)
        vin = vehicle_uid(coordinator, entry)
        entities.extend(
            CheryEuropeCommandSwitch(coordinator, description, entry)
            for description in SWITCH_DESCRIPTIONS
            if keep_feature(perms, description.key, f"{vin}_{description.key}", unsupported)
        )
        if keep_feature(perms, "charging_switch", f"{vin}_charging_switch", unsupported):
            entities.append(CheryEuropeChargeSwitch(coordinator, entry))
        if keep_feature(perms, "scheduled_charging", f"{vin}_scheduled_charging", unsupported):
            entities.append(CheryEuropeScheduledChargeSwitch(coordinator, entry))
        if keep_feature(perms, "departure", f"{vin}_departure", unsupported):
            entities.append(CheryEuropeDepartureSwitch(coordinator, entry))
        if keep_feature(
            perms,
            "departure",
            f"{vin}_{DEPARTURE_SOON_SWITCH_DESCRIPTION.key}",
            unsupported,
        ):
            entities.append(CheryEuropeDepartureSoonSwitch(coordinator, entry))
        entities.extend(
            CheryEuropeDepartureDaySwitch(coordinator, description, entry, day)
            for day, description in DEPARTURE_DAY_DESCRIPTIONS
            if keep_feature(perms, description.key, f"{vin}_{description.key}", unsupported)
        )
        entities.append(CheryEuropePollingSwitch(coordinator, entry))
    async_sync_unsupported(
        hass,
        SWITCH,
        entities,
        unsupported,
    )
    async_add_entities(entities)


class CheryEuropeCommandSwitch(CheryEuropeEntity, SwitchEntity):
    """Representation of a safe Chery Europe command switch."""

    def __init__(
        self,
        coordinator: CheryEuropeDataUpdateCoordinator,
        description: CheryEuropeSwitchEntityDescription,
        entry: ConfigEntry,
    ) -> None:
        """Initialize the switch."""
        super().__init__(coordinator, description, entry)
        self._attr_translation_key = description.translation_key
        self._attr_unique_id = stable_unique_id(entry, description.key, coordinator)

    @property
    def is_on(self) -> bool | None:  # type: ignore[reportIncompatibleVariableOverride]
        """Return the switch state from coordinator feedback, if available."""
        description = self.entity_description
        assert isinstance(description, CheryEuropeSwitchEntityDescription)
        return description.state_fn(self.chery_data)

    @property
    def assumed_state(self) -> bool:  # type: ignore[reportIncompatibleVariableOverride]
        """Return true when the API does not provide reliable command feedback."""
        return self.is_on is None

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Send the safe remote command using the PIN from the service call."""
        await self._send_command(kwargs, enabled=True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Send the safe remote command using the PIN from the service call."""
        await self._send_command(kwargs, enabled=False)

    async def _send_command(self, kwargs: dict[str, Any], *, enabled: bool) -> None:
        """Send this switch command without storing or logging the PIN."""
        extra = dict(self.command_description.command_values or {})
        extra["enabled"] = enabled
        if self.command_description.command_id == "ve_1108":
            # the defrost rides on airControl: keep the current climate target
            extra["temperature"] = self.chery_data.target_temperature
        elif self.command_description.command_id == "ve_1109":
            # purification rides on airControl too; OFF keeps the climate state
            extra["temperature"] = self.chery_data.target_temperature
            extra["hvac_enabled"] = self.chery_data.hvac_enabled
        await async_send_vehicle_command(
            self.coordinator,
            self._entry,
            self.chery_data.vin,
            kwargs,
            command_id=self.command_description.command_id,
            **extra,
        )

    @property
    def available(self) -> bool:  # type: ignore[reportIncompatibleVariableOverride]
        """Return if entity data is available."""
        return self.coordinator.data is not None and super().available

    @property
    def command_description(self) -> CheryEuropeSwitchEntityDescription:
        """Return the typed switch description."""
        description = self.entity_description
        assert isinstance(description, CheryEuropeSwitchEntityDescription)
        return description


POLLING_SWITCH_DESCRIPTION = SwitchEntityDescription(
    key="automatic_updates",
    name="Automatic updates",
    translation_key="automatic_updates",
    icon="mdi:autorenew",
    entity_category=EntityCategory.CONFIG,
)

CHARGING_SWITCH_DESCRIPTION = SwitchEntityDescription(
    key="charging_switch",
    name="Charging",
    translation_key="charging",
    icon="mdi:battery-charging",
)

SCHEDULED_CHARGING_SWITCH_DESCRIPTION = SwitchEntityDescription(
    key="scheduled_charging",
    name="Scheduled charging",
    translation_key="scheduled_charging",
    icon="mdi:calendar-clock",
)


class CheryEuropePollingSwitch(CheryEuropeEntity, SwitchEntity, RestoreEntity):
    """Enable or disable automatic telemetry polling."""

    def __init__(
        self,
        coordinator: CheryEuropeDataUpdateCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator, POLLING_SWITCH_DESCRIPTION, entry)
        self._attr_unique_id = stable_unique_id(entry, POLLING_SWITCH_DESCRIPTION.key, coordinator)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state in ("on", "off"):
            self.coordinator.poll_enabled = last.state == "on"
            if self.coordinator.data is not None:
                self.coordinator._apply_scan_interval(self.coordinator.data)

    @property
    def is_on(self) -> bool:
        return bool(self.coordinator.poll_enabled)

    async def async_turn_on(self, **kwargs: Any) -> None:
        self.coordinator.poll_enabled = True
        if self.coordinator.data is not None:
            self.coordinator._apply_scan_interval(self.coordinator.data)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        self.coordinator.poll_enabled = False
        self.coordinator.update_interval = None
        self.async_write_ha_state()


async def _async_send_switch_command(
    coordinator: CheryEuropeDataUpdateCoordinator,
    entry: ConfigEntry,
    vin: str | None,
    kwargs: dict[str, Any],
    *,
    command_id: str,
    enabled: bool,
    start_minutes: int | None = None,
    duration_hours: int | None = None,
) -> None:
    extra: dict[str, Any] = {"enabled": enabled}
    if start_minutes is not None:
        extra["start_minutes"] = start_minutes
    if duration_hours is not None:
        extra["duration_hours"] = duration_hours
    await async_send_vehicle_command(
        coordinator,
        entry,
        vin,
        kwargs,
        command_id=command_id,
        **extra,
    )


class CheryEuropeChargeSwitch(CheryEuropeEntity, SwitchEntity, RestoreEntity):
    """Start or stop immediate charging through chargeStartStopControl."""

    def __init__(
        self,
        coordinator: CheryEuropeDataUpdateCoordinator,
        entry: ConfigEntry,
    ) -> None:
        """Initialize the immediate charging switch."""
        super().__init__(coordinator, CHARGING_SWITCH_DESCRIPTION, entry)
        self._optimistic: bool | None = None
        self._restored: bool | None = None
        self._attr_unique_id = stable_unique_id(entry, CHARGING_SWITCH_DESCRIPTION.key, coordinator)

    async def async_added_to_hass(self) -> None:
        """Restore the last known charging switch state."""
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state in ("on", "off"):
            self._restored = last.state == "on"

    @property
    def is_on(self) -> bool | None:  # type: ignore[reportIncompatibleVariableOverride]
        """Return charging state from telemetry, optimistic update, or restore."""
        if self._optimistic is not None:
            return self._optimistic
        if self.chery_data.is_charging is not None:
            return self.chery_data.is_charging
        return self._restored

    @property
    def assumed_state(self) -> bool:  # type: ignore[reportIncompatibleVariableOverride]
        """Return true when no reliable charging state is available."""
        return self.is_on is None

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Start immediate charging."""
        self._optimistic = True
        self.async_write_ha_state()
        await _async_send_switch_command(
            self.coordinator,
            self._entry,
            self.chery_data.vin,
            kwargs,
            command_id="ve_1201",
            enabled=True,
        )
        self._optimistic = None
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Stop immediate charging."""
        self._optimistic = False
        self.async_write_ha_state()
        await _async_send_switch_command(
            self.coordinator,
            self._entry,
            self.chery_data.vin,
            kwargs,
            command_id="ve_1201",
            enabled=False,
        )
        self._optimistic = None
        self.async_write_ha_state()


class CheryEuropeScheduledChargeSwitch(CheryEuropeEntity, SwitchEntity, RestoreEntity):
    """Enable or disable scheduled charging through chargeAppointControl."""

    def __init__(
        self,
        coordinator: CheryEuropeDataUpdateCoordinator,
        entry: ConfigEntry,
    ) -> None:
        """Initialize the scheduled charging switch."""
        super().__init__(coordinator, SCHEDULED_CHARGING_SWITCH_DESCRIPTION, entry)
        self._optimistic: bool | None = None
        self._restored: bool | None = None
        self._attr_unique_id = stable_unique_id(
            entry, SCHEDULED_CHARGING_SWITCH_DESCRIPTION.key, coordinator
        )

    async def async_added_to_hass(self) -> None:
        """Restore the last known scheduled charging switch state."""
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state in ("on", "off"):
            self._restored = last.state == "on"

    @property
    def is_on(self) -> bool | None:  # type: ignore[reportIncompatibleVariableOverride]
        """Return whether the scheduled charging plan is enabled on the vehicle."""
        if self._optimistic is not None:
            return self._optimistic
        if self.chery_data.scheduled_charge_enabled is not None:
            return self.chery_data.scheduled_charge_enabled
        return self._restored

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Expose the plan reported by the vehicle when available."""
        plan = self.chery_data.charge_appoint_plan
        if plan is None:
            return None
        attrs: dict[str, Any] = {}
        start = plan_start_time(plan)
        if start is not None:
            attrs["vehicle_start_time"] = f"{start.hour:02d}:{start.minute:02d}"
        hours = plan_duration_hours(plan)
        if hours is not None:
            attrs["vehicle_duration_hours"] = hours
        days = plan.get("cycleData")
        if isinstance(days, list) and days:
            attrs["vehicle_days"] = days
        return attrs or None

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable scheduled charging with the configured start time and duration."""
        self._optimistic = True
        self.async_write_ha_state()
        await _async_send_switch_command(
            self.coordinator,
            self._entry,
            self.chery_data.vin,
            kwargs,
            command_id="ve_1202",
            enabled=True,
            start_minutes=self.coordinator.charge_start_minutes,
            duration_hours=self.coordinator.charge_duration_hours,
        )
        self._optimistic = None
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable scheduled charging."""
        self._optimistic = False
        self.async_write_ha_state()
        await _async_send_switch_command(
            self.coordinator,
            self._entry,
            self.chery_data.vin,
            kwargs,
            command_id="ve_1202",
            enabled=False,
            start_minutes=self.coordinator.charge_start_minutes,
            duration_hours=self.coordinator.charge_duration_hours,
        )
        self._optimistic = None
        self.async_write_ha_state()


class CheryEuropeDepartureSwitch(CheryEuropeEntity, SwitchEntity, RestoreEntity):
    """Arm the managed preparation plan so the car leaves at that time."""

    def __init__(
        self,
        coordinator: CheryEuropeDataUpdateCoordinator,
        entry: ConfigEntry,
    ) -> None:
        """Initialize the departure switch."""
        super().__init__(coordinator, DEPARTURE_SWITCH_DESCRIPTION, entry)
        self._attr_unique_id = stable_unique_id(
            entry, DEPARTURE_SWITCH_DESCRIPTION.key, coordinator
        )
        self._armed_commanded = False

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Expose that the state came from a command, so a reload keeps it."""
        if not self._armed_commanded:
            return None
        return {"armed_commanded": True}

    async def async_added_to_hass(self) -> None:
        """Restore the arm state; the car cannot be read back.

        A restored ``off`` is only believed when it was commanded from here,
        which the ``armed_commanded`` attribute records. Anything else is a
        leftover of the days when this switch defaulted to off, and the plan in
        the car is the one the owner armed in the app.
        """
        await super().async_added_to_hass()
        if self.coordinator.departure_armed is not None:
            return
        last = await self.async_get_last_state()
        if last is None:
            self.coordinator.departure_armed = True
            self.async_write_ha_state()
            return
        commanded = bool((last.attributes or {}).get("armed_commanded"))
        if last.state == "on":
            self._armed_commanded = commanded
            self.coordinator.departure_armed = True
        elif commanded:
            self._armed_commanded = True
            self.coordinator.departure_armed = False
        else:
            self.coordinator.departure_armed = True
        self.async_write_ha_state()

    @property
    def is_on(self) -> bool:
        """Return whether the departure plan is armed."""
        return departure_state(self.coordinator)[1]

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Arm the departure plan, creating it from the configured time."""
        await async_set_departure(self.coordinator, self._entry, enabled=True)
        self._armed_commanded = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disarm the departure plan without deleting it."""
        await async_set_departure(self.coordinator, self._entry, enabled=False)
        self._armed_commanded = True
        self.async_write_ha_state()


class CheryEuropeDepartureDaySwitch(CheryEuropeEntity, SwitchEntity):
    """One weekday of the managed departure plan.

    The weekday list travels with the plan on every write, so toggling a day
    re-sends the whole plan (time, armed state and the new day list) to the car
    and is refused when it would leave the plan with no day at all.
    """

    def __init__(
        self,
        coordinator: CheryEuropeDataUpdateCoordinator,
        description: SwitchEntityDescription,
        entry: ConfigEntry,
        day: int,
    ) -> None:
        """Initialize the weekday switch."""
        super().__init__(coordinator, description, entry)
        self._day = day
        self._attr_translation_key = description.translation_key
        self._attr_unique_id = stable_unique_id(entry, description.key, coordinator)

    @property
    def is_on(self) -> bool:
        """Return whether the departure plan repeats on this weekday."""
        return self._day in departure_days(self.coordinator)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Add this weekday to the plan and write it to the car."""
        await async_set_departure_days(self.coordinator, self._entry, add=[self._day])
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Drop this weekday from the plan and write it to the car."""
        await async_set_departure_days(self.coordinator, self._entry, remove=[self._day])
        self.async_write_ha_state()


def _remaining_seconds(deadline: Any) -> float:
    """Return how long is left until an ISO deadline, never negative."""
    parsed = dt_util.parse_datetime(deadline) if isinstance(deadline, str) else None
    if parsed is None:
        return 0.0
    return max(0.0, (parsed - dt_util.utcnow()).total_seconds())


def departure_soon_target(now: datetime) -> tuple[str, list[int]]:
    """Return the departure time and weekday list for a "in 15 minutes" request.

    The time is rounded down to the minute Home Assistant shows, and only the
    weekday of the request is added: the plan must fire today, not repeat.
    """
    target = (now + timedelta(minutes=DEPARTURE_SOON_MINUTES)).replace(
        second=0, microsecond=0
    )
    return f"{target:%H:%M}", [now.isoweekday()]


class CheryEuropeDepartureSoonSwitch(CheryEuropeEntity, SwitchEntity, RestoreEntity):
    """Pull the departure plan 15 minutes ahead, armed, for the rest of today.

    Turning it on does what would otherwise take three entities: it shifts the
    departure time, switches the plan on and adds the current weekday, so the
    car is prepared for a drive that starts soon. Turning it off puts the
    parameters in force before the switch was turned on back immediately, and so
    does the timer that expires :data:`~.departure.QUICK_DEPARTURE_MINUTES` later:
    a quick departure never outlives the drive it was meant for.

    On top of the plan it runs the pre-departure heating the YAML macro used to
    do: wake the car, switch the climate to HI, add the wheel and the driver seat
    five minutes before departure and put the settings captured on the way back
    when the drive starts or the switch is turned off. A door opening aborts the
    heating, exactly like the macro did.

    The saved parameters, the captured settings and their deadlines are
    published as state attributes, so a restart inside that window still
    restores both the plan and the climate.
    """

    def __init__(
        self,
        coordinator: CheryEuropeDataUpdateCoordinator,
        entry: ConfigEntry,
    ) -> None:
        """Initialize the quick departure switch."""
        super().__init__(coordinator, DEPARTURE_SOON_SWITCH_DESCRIPTION, entry)
        self._attr_translation_key = DEPARTURE_SOON_SWITCH_DESCRIPTION.translation_key
        self._attr_unique_id = stable_unique_id(
            entry, DEPARTURE_SOON_SWITCH_DESCRIPTION.key, coordinator
        )
        self._heat: QuickDepartureHeating | None = None
        self._heat_unsub: Callable[[], None] | None = None

    @property
    def is_on(self) -> bool:
        """Return whether a quick departure is pending."""
        return departure_restore_at(self.coordinator) is not None

    async def async_added_to_hass(self) -> None:
        """Resume a restore that was pending when Home Assistant stopped."""
        await super().async_added_to_hass()
        state = await self.async_get_last_state()
        attributes = (state.attributes or {}) if state else {}
        self._async_resume_heating(attributes)
        if getattr(self.coordinator, "departure_previous", None) is not None:
            return
        saved = attributes.get("departure_previous")
        if not isinstance(saved, dict) or not saved:
            return
        self.coordinator.departure_previous = dict(saved)
        remaining = _remaining_seconds(attributes.get("departure_restore_at"))
        schedule_departure_restore(self.coordinator, self._entry, delay=remaining)

    def _async_resume_heating(self, attributes: dict[str, Any]) -> None:
        """Restore the climate at its deadline after a restart mid-heating."""
        saved = attributes.get("heat_previous")
        if not isinstance(saved, dict) or not saved:
            return
        deadline = dt_util.parse_datetime(str(attributes.get("heat_restore_at")))
        remaining = 0.0
        if deadline is not None:
            remaining = max(0.0, (deadline - dt_util.utcnow()).total_seconds())
        self._heat = QuickDepartureHeating(
            self.hass, self._entry.entry_id, deadline or dt_util.utcnow()
        )
        self._heat.adopt(saved)
        self._async_schedule_heat_restore(remaining)

    def _async_schedule_heat_restore(self, delay: float) -> None:
        """Restore the climate settings when the delay expires."""
        self._async_cancel_heat_restore()
        self._heat_unsub = async_call_later(self.hass, delay, self._async_restore_heat)

    def _async_cancel_heat_restore(self) -> None:
        """Drop a pending climate restore timer."""
        if self._heat_unsub is not None:
            self._heat_unsub()
            self._heat_unsub = None

    async def _async_restore_heat(self, _now: datetime | None = None) -> None:
        """Put the climate, the wheel and the seat back."""
        self._heat_unsub = None
        if self._heat is not None:
            await self._heat.async_restore()
        self.async_write_ha_state()

    async def async_will_remove_from_hass(self) -> None:
        """Drop the timer; a reload reschedules it from the saved deadline."""
        self._async_cancel_heat_restore()
        if self._heat is not None and self._heat.running:
            await self._heat.async_stop()
        async_cancel_departure_restore(self.coordinator, keep_deadline=True)
        await super().async_will_remove_from_hass()

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Expose the saved parameters so they survive a restart."""
        previous = getattr(self.coordinator, "departure_previous", None)
        heat = self._heat.snapshot if self._heat is not None else None
        if not previous and not heat:
            return None
        attributes: dict[str, Any] = {}
        if previous:
            attributes["departure_previous"] = dict(previous)
            attributes["departure_restore_at"] = departure_restore_at_iso(
                self.coordinator
            )
        if heat:
            attributes["heat_previous"] = dict(heat)
            deadline = departure_restore_at(self.coordinator)
            attributes["heat_restore_at"] = (
                deadline.isoformat() if deadline is not None else None
            )
        return attributes

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Schedule the departure 15 minutes from now and arm it for today.

        The parameters in force before the first switch-on are kept, so a second
        on keeps what has to be restored and only shifts the time again.
        """
        took_snapshot = getattr(self.coordinator, "departure_previous", None) is None
        if took_snapshot:
            self.coordinator.departure_previous = departure_snapshot(self.coordinator)
        travel_time, weekday = departure_soon_target(dt_util.now())
        days = sorted(set(departure_days(self.coordinator)) | set(weekday))
        schedule_departure_restore(
            self.coordinator,
            self._entry,
            delay=QUICK_DEPARTURE_MINUTES * 60,
        )
        try:
            await async_set_departure(
                self.coordinator,
                self._entry,
                travel_time=travel_time,
                enabled=True,
                days=days,
            )
        except Exception:
            async_cancel_departure_restore(self.coordinator)
            if took_snapshot:
                self.coordinator.departure_previous = None
            raise
        await self._async_heat(travel_time)
        self.async_write_ha_state()

    async def _async_heat(self, travel_time: str) -> None:
        """Start the pre-departure heating aimed at the scheduled time."""
        now = dt_util.now()
        target = now.replace(second=0, microsecond=0) + timedelta(
            hours=int(travel_time[:2]), minutes=int(travel_time[3:5])
        )
        if target < now:
            target += timedelta(days=1)
        if self._heat is not None and self._heat.running:
            await self._heat.async_retarget(target)
        elif self._heat is not None and self._heat.snapshot is not None:
            await self._heat.async_restore()
            self._heat = None
        if self._heat is None:
            snapshot = heating_snapshot(
                self.hass, entity_ids(self.hass, self._entry.entry_id)
            )
            self._heat = QuickDepartureHeating(
                self.hass, self._entry.entry_id, target
            )
            await self._heat.async_start(snapshot)
        self._async_schedule_heat_restore(
            max(0.0, (target - dt_util.now()).total_seconds())
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Cancel the quick departure, stop the heating and restore everything."""
        async_cancel_departure_restore(self.coordinator)
        self._async_cancel_heat_restore()
        if self._heat is not None:
            await self._heat.async_cancel()
            self._heat = None
        if getattr(self.coordinator, "departure_previous", None) is None:
            self.async_write_ha_state()
            return
        await async_restore_departure(self.coordinator, self._entry)
        self.async_write_ha_state()
