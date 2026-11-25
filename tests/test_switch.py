# pyright: reportArgumentType=false, reportOptionalMemberAccess=false
"""Tests for the Chery Europe switch entity feedback state and PIN guard.

Verifies that ``is_on`` reads from ``CheryData.front_windshield_heating``
via ``_feedback_state``, that ``assumed_state`` is ``True`` when feedback is
absent (``None``), and that ``async_turn_on`` raises ``HomeAssistantError``
when no PIN is supplied.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

pytest.importorskip("homeassistant")

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from custom_components.chery_europe.data import CheryData
from custom_components.chery_europe.switch import (
    SWITCH_DESCRIPTIONS,
    CheryEuropeCommandSwitch,
)

VIN = "VIN123456"


class _FakeRegistry:
    """Minimal entity registry stand-in for the permission gate."""

    def __init__(self, entries: dict[str, SimpleNamespace] | None = None) -> None:
        self.entries = entries or {}
        self.updates: list[tuple[str, dict]] = []
        self.removed: list[str] = []

    def async_get_entity_id(self, domain: str, _platform: str, unique_id: str):
        return f"{domain}.{unique_id}"

    def async_get(self, entity_id: str):
        return self.entries.get(entity_id)

    def async_update_entity(self, entity_id: str, **kwargs) -> None:
        self.updates.append((entity_id, kwargs))
        entry = self.entries.setdefault(entity_id, SimpleNamespace(disabled_by=None))
        for key, value in kwargs.items():
            setattr(entry, key, value)

    def async_remove(self, entity_id: str) -> None:
        self.removed.append(entity_id)
        self.entries.pop(entity_id, None)


def _description(key: str):
    """Return the switch entity description with the matching key."""
    for desc in SWITCH_DESCRIPTIONS:
        if desc.key == key:
            return desc
    raise KeyError(key)


def _make_switch(
    data: CheryData,
    key: str = "front_windshield_heating",
) -> CheryEuropeCommandSwitch:
    """Build a CheryEuropeCommandSwitch wired to a stub coordinator."""
    coordinator = SimpleNamespace(
        data=data,
        last_update_success=True,
        api=SimpleNamespace(send_command=AsyncMock()),
        async_request_refresh=AsyncMock(),
    )
    entry = SimpleNamespace(entry_id="entry-1", options={"pin": "1234"})
    return CheryEuropeCommandSwitch(coordinator, _description(key), entry)


def test_front_windshield_heating_on():
    """front_windshield_heating=True -> is_on is True."""
    switch = _make_switch(CheryData(vin=VIN, front_windshield_heating=True))
    assert switch.is_on is True


def test_front_windshield_defrost_reads_its_own_field():
    """front_windshield_defrost has its own feedback field."""
    switch = _make_switch(
        CheryData(vin=VIN, front_windshield_heating=False, front_windshield_defrost=True),
        key="front_windshield_defrost",
    )
    assert switch.is_on is True


@pytest.mark.asyncio
async def test_front_windshield_defrost_sends_climate_target():
    """ve_1108 rides on airControl and carries the current climate target."""
    switch = _make_switch(
        CheryData(vin=VIN, target_temperature=23.0), key="front_windshield_defrost"
    )
    switch.coordinator.api.send_command.return_value = {"ok": True}
    switch.coordinator.async_set_updated_data = lambda data: None
    switch.coordinator.schedule_refresh_after_command = lambda: None

    await switch.async_turn_on()

    switch.coordinator.api.send_command.assert_awaited_once_with(
        VIN, "ve_1108", "1234", enabled=True, temperature=23.0
    )


def test_rear_window_heating_reads_state_and_sends_back_defrost():
    """Rear window heating is the existing rear_window_defrost switch (ve_1135)."""
    switch = _make_switch(
        CheryData(vin=VIN, rear_window_defrost=True), key="rear_window_defrost"
    )
    assert switch.is_on is True
    assert switch.command_description.command_id == "ve_1135"
    assert switch.entity_description.name == "Rear window heating"


@pytest.mark.asyncio
async def test_rear_window_heating_sends_enabled_flag():
    switch = _make_switch(CheryData(vin=VIN), key="rear_window_defrost")
    switch.coordinator.api.send_command.return_value = {"ok": True}
    switch.coordinator.async_set_updated_data = lambda data: None
    switch.coordinator.schedule_refresh_after_command = lambda: None

    await switch.async_turn_on()

    switch.coordinator.api.send_command.assert_awaited_once_with(
        VIN, "ve_1135", "1234", enabled=True
    )


def test_air_purification_reads_its_own_field():
    """air_purification follows the realtime airPurification flag."""
    switch = _make_switch(
        CheryData(vin=VIN, air_purification=True), key="air_purification"
    )
    assert switch.is_on is True


@pytest.mark.asyncio
async def test_air_purification_off_sends_climate_state():
    """ve_1109 carries the climate target and state so OFF keeps the climate."""
    switch = _make_switch(
        CheryData(vin=VIN, target_temperature=21.5, hvac_enabled=True, air_purification=True),
        key="air_purification",
    )
    switch.coordinator.api.send_command.return_value = {"ok": True}
    switch.coordinator.async_set_updated_data = lambda data: None
    switch.coordinator.schedule_refresh_after_command = lambda: None

    await switch.async_turn_off()

    switch.coordinator.api.send_command.assert_awaited_once_with(
        VIN, "ve_1109", "1234", enabled=False, temperature=21.5, hvac_enabled=True
    )


def test_no_feedback_assumed_state():
    """No feedback fields -> is_on is None and assumed_state is True."""
    switch = _make_switch(CheryData(vin=VIN))
    assert switch.is_on is None
    assert switch.assumed_state is True


@pytest.mark.asyncio
async def test_turn_on_requires_pin():
    """async_turn_on without PIN raises HomeAssistantError before any API call."""
    switch = _make_switch(CheryData(vin=VIN))
    switch._entry = SimpleNamespace(entry_id="entry-1", options={})

    with pytest.raises(HomeAssistantError):
        await switch.async_turn_on()

    switch.coordinator.api.send_command.assert_not_awaited()


@pytest.mark.asyncio
async def test_setup_disables_denied_switch_instead_of_deleting_it():
    from custom_components.chery_europe.switch import async_setup_entry

    coordinator = SimpleNamespace(
        data=CheryData(vin=VIN),
        api=SimpleNamespace(
            permissions={232: 0, 2321: 0, 20411: 0, 208: 1, 401: 0}
        ),
        last_update_success=True,
    )
    entry = SimpleNamespace(entry_id="entry-1", runtime_data=coordinator)
    added: list = []
    # Both switches existed before the backend stopped reporting the permission.
    registry = _FakeRegistry(
        {
            "switch.entry-1_rear_window_defrost": SimpleNamespace(disabled_by=None),
            "switch.entry-1_theft_alarm": SimpleNamespace(disabled_by=None),
        }
    )

    with patch(
        "custom_components.chery_europe.entity.er.async_get",
        return_value=registry,
    ):
        await async_setup_entry(Mock(), entry, lambda entities: added.extend(entities))

    keys = [entity.entity_description.key for entity in added]
    denied = {
        entity.entity_description.key
        for entity in added
        if entity._permission_denied
    }
    # Denied controls are still created, so they keep their entity id and can
    # come back on their own; they are only flagged and disabled.
    assert "rear_window_defrost" in keys
    assert "theft_alarm" in keys
    assert {"rear_window_defrost", "theft_alarm"} <= denied
    assert "steering_wheel_heating" not in denied
    # The registry entry survives, disabled instead of deleted.
    assert registry.removed == []
    assert (
        registry.entries["switch.entry-1_rear_window_defrost"].disabled_by
        is er.RegistryEntryDisabler.INTEGRATION
    )
    assert (
        registry.entries["switch.entry-1_theft_alarm"].disabled_by
        is er.RegistryEntryDisabler.INTEGRATION
    )


@pytest.mark.asyncio
async def test_setup_enables_a_switch_the_backend_allows_again():
    from custom_components.chery_europe.switch import async_setup_entry

    coordinator = SimpleNamespace(
        data=CheryData(vin=VIN),
        api=SimpleNamespace(permissions={208: 1}),
        last_update_success=True,
    )
    entry = SimpleNamespace(entry_id="entry-1", runtime_data=coordinator)
    added: list = []
    registry = _FakeRegistry(
        {
            "switch.entry-1_theft_alarm": SimpleNamespace(
                disabled_by=er.RegistryEntryDisabler.INTEGRATION
            )
        }
    )

    with patch(
        "custom_components.chery_europe.entity.er.async_get",
        return_value=registry,
    ):
        await async_setup_entry(Mock(), entry, lambda entities: added.extend(entities))

    assert "theft_alarm" in [entity.entity_description.key for entity in added]
    assert registry.entries["switch.entry-1_theft_alarm"].disabled_by is None


@pytest.mark.asyncio
async def test_setup_leaves_a_user_disabled_switch_alone():
    """Only the integration's own flag is cleared, never the user's choice."""
    from custom_components.chery_europe.switch import async_setup_entry

    coordinator = SimpleNamespace(
        data=CheryData(vin=VIN),
        api=SimpleNamespace(permissions={208: 1}),
        last_update_success=True,
    )
    entry = SimpleNamespace(entry_id="entry-1", runtime_data=coordinator)
    registry = _FakeRegistry(
        {
            "switch.entry-1_theft_alarm": SimpleNamespace(
                disabled_by=er.RegistryEntryDisabler.USER
            )
        }
    )

    with patch(
        "custom_components.chery_europe.entity.er.async_get",
        return_value=registry,
    ):
        await async_setup_entry(Mock(), entry, lambda entities: None)

    assert registry.updates == []
    assert (
        registry.entries["switch.entry-1_theft_alarm"].disabled_by
        is er.RegistryEntryDisabler.USER
    )


@pytest.mark.asyncio
async def test_setup_keeps_every_switch_when_permissions_are_unknown():
    from custom_components.chery_europe.switch import SWITCH_DESCRIPTIONS, async_setup_entry

    coordinator = SimpleNamespace(
        data=CheryData(vin=VIN),
        api=SimpleNamespace(permissions={}),
        last_update_success=True,
    )
    entry = SimpleNamespace(entry_id="entry-1", runtime_data=coordinator)
    added: list = []

    with patch(
        "custom_components.chery_europe.entity.er.async_get",
        return_value=_FakeRegistry(),
    ):
        await async_setup_entry(Mock(), entry, lambda entities: added.extend(entities))

    keys = {entity.entity_description.key for entity in added}
    assert "rear_window_defrost" in keys
    # seat levels moved to the select platform
    assert "driver_seat_heating" not in keys
    assert "departure" in keys
    assert "departure_monday" in keys
    assert len(added) == len(SWITCH_DESCRIPTIONS) + 11


def test_keep_feature_creates_the_entity_even_when_the_control_is_denied():
    """A denied control must still be built, or its registry entry never exists."""
    from custom_components.chery_europe.entity import keep_feature

    unsupported: list[str] = []
    assert keep_feature({206: 0}, "windows", "VIN_windows", unsupported) is True
    assert unsupported == ["VIN_windows"]

    unsupported.clear()
    assert keep_feature({206: 1}, "windows", "VIN_windows", unsupported) is True
    assert unsupported == []


@pytest.mark.asyncio
async def test_disable_entity_sets_the_integration_flag_only_once():
    from custom_components.chery_europe.entity import _disable_entity

    updates: list[tuple[str, dict]] = []
    entries = {
        "switch.a": SimpleNamespace(disabled_by=None),
        "switch.b": SimpleNamespace(disabled_by=er.RegistryEntryDisabler.USER),
    }
    registry = SimpleNamespace(
        async_get=lambda entity_id: entries.get(entity_id),
        async_update_entity=lambda entity_id, **kwargs: updates.append(
            (entity_id, kwargs)
        ),
    )

    with patch(
        "custom_components.chery_europe.entity.er.async_get", return_value=registry
    ):
        _disable_entity(Mock(), "switch.a")
        _disable_entity(Mock(), "switch.b")
        _disable_entity(Mock(), "switch.missing")

    assert updates == [
        ("switch.a", {"disabled_by": er.RegistryEntryDisabler.INTEGRATION})
    ]
