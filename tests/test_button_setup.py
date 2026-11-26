"""Setup tests for the Chery Europe button platform."""

from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

pytest.importorskip("homeassistant")

from custom_components.chery_europe.data import CheryData
from custom_components.chery_europe.button import async_setup_entry


def _entry(permissions):
    coordinator = Mock()
    coordinator.data = CheryData(vin="VIN123456")
    coordinator.api = SimpleNamespace(permissions=permissions)
    coordinator.permissions = {}
    coordinator._permissions_loaded = False
    coordinator.entity_slot = None
    coordinator.coordinators = None
    coordinator.last_update_success = True
    return SimpleNamespace(entry_id="entry-1", runtime_data=coordinator)


def _patched_registry(updates: list):
    """Denied controls are disabled in the registry, so the registry must be stubbed."""
    registry = SimpleNamespace(
        async_get_entity_id=lambda domain, _platform, unique_id: f"{domain}.{unique_id}",
        async_get=lambda entity_id: None,
        async_update_entity=lambda entity_id, **kwargs: updates.append((entity_id, kwargs)),
        async_remove=lambda entity_id: None,
    )
    stack = ExitStack()
    stack.enter_context(
        patch("custom_components.chery_europe.entity.er.async_get", return_value=registry)
    )
    stack.enter_context(
        patch(
            "custom_components.chery_europe.entity.er.async_entries_for_config_entry",
            return_value=[],
        )
    )
    return stack


@pytest.mark.asyncio
async def test_setup_adds_the_quick_departure_button() -> None:
    """The quick departure button is created next to the other actions."""
    added: list = []
    updates: list = []

    with _patched_registry(updates):
        await async_setup_entry(
            Mock(), _entry({}), lambda entities: added.extend(entities)
        )

    keys = [entity.entity_description.key for entity in added]
    assert "departure_in_15_min" in keys
    assert "departure_restore" not in keys
    button = next(
        entity
        for entity in added
        if entity.entity_description.key == "departure_in_15_min"
    )
    assert button.unique_id == "entry-1_departure_in_15_min"
    assert button.entity_description.icon == "mdi:car-clock"


@pytest.mark.asyncio
async def test_setup_keeps_the_button_when_permissions_are_unknown() -> None:
    """An unread permission map must not hide the button."""
    added: list = []
    updates: list = []

    with _patched_registry(updates):
        await async_setup_entry(
            Mock(), _entry({232: 0, 2321: 0}), lambda entities: added.extend(entities)
        )

    keys = {entity.entity_description.key for entity in added}
    assert "departure_in_15_min" in keys
