"""Tests for the heating that runs before a quick departure.

The sequence drives the car through the integration's own entities, so the
tests replace ``hass`` with a tiny stand-in that records the service calls and
answers the state lookups. Delays are shrunk to zero and the polling helper is
replaced, so nothing here waits for a real clock.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

pytest.importorskip("homeassistant")

from homeassistant.util import dt as dt_util  # noqa: E402

from custom_components.chery_europe import quick_departure as qd  # noqa: E402

ENTRY = "01M75D7QBZ64KXQ16V72XE88ZJ"


class _States:
    """Answer state lookups from a plain mapping."""

    def __init__(self, states: dict[str, SimpleNamespace]) -> None:
        self._states = states

    def get(self, entity_id: str) -> SimpleNamespace | None:
        """Return the state or None for an unknown entity."""
        return self._states.get(entity_id)


class _Services:
    """Record every service call the sequence makes."""

    def __init__(self, fail_on: tuple[str, str] | None = None) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self._fail_on = fail_on

    async def async_call(
        self, domain: str, service: str, data: dict, blocking: bool = False
    ) -> None:
        """Record the call, refusing the one the test asked to break."""
        if (domain, service) == self._fail_on:
            raise RuntimeError("car refused")
        self.calls.append((domain, service, data))


class _Hass:
    """A stand-in carrying states, services and a task factory."""

    def __init__(
        self,
        states: dict[str, SimpleNamespace] | None = None,
        fail_on: tuple[str, str] | None = None,
    ) -> None:
        self.states = _States(states or {})
        self.services = _Services(fail_on)

    def async_create_task(self, coro, name=None, eager_start=False):
        """Run the sequence as a real task on the test's event loop."""
        return asyncio.get_running_loop().create_task(coro)


def _state(state: str, **attributes) -> SimpleNamespace:
    """Build a state object with attributes."""
    return SimpleNamespace(state=state, attributes=attributes)


def _registry_entries(rows: list[tuple[str, str]]) -> list[SimpleNamespace]:
    """Build registry entries from (unique_id, entity_id) pairs."""
    return [
        SimpleNamespace(unique_id=unique_id, entity_id=entity_id)
        for unique_id, entity_id in rows
    ]


@pytest.fixture(autouse=True)
def registry(monkeypatch):
    """Point the registry lookup at a full set of entity ids, for every test."""
    rows = [
        ("abc_hvac_climate", "climate.tiggo"),
        ("abc_heat_everything", "switch.tiggo_grzanie"),
        ("abc_steering_wheel_heating", "switch.tiggo_kierownica"),
        ("abc_driver_seat_heating_level_select", "select.tiggo_fotel"),
        ("abc_wake", "button.tiggo_wake"),
        ("abc_online", "binary_sensor.tiggo_online"),
        ("abc_door_front_left", "binary_sensor.tiggo_drzwi_fl"),
        ("abc_door_front_right", "binary_sensor.tiggo_drzwi_fr"),
        ("abc_door_rear_left", "binary_sensor.tiggo_drzwi_rl"),
        ("abc_door_rear_right", "binary_sensor.tiggo_drzwi_rr"),
        ("abc_battery_level", "sensor.tiggo_bateria"),
    ]
    monkeypatch.setattr(qd.er, "async_get", lambda hass: "registry")
    monkeypatch.setattr(
        qd.er,
        "async_entries_for_config_entry",
        lambda entry, entry_id: _registry_entries(rows),
    )
    return rows


@pytest.fixture
def ids(registry):
    """Return the resolved ids for the full registry."""
    return qd.entity_ids(_Hass(), ENTRY)


@pytest.fixture
def fast(monkeypatch):
    """Collapse every pause and replace the polling helper."""
    monkeypatch.setattr(qd, "WAKE_DELAY", 0)
    monkeypatch.setattr(qd, "WAKE_TIMEOUT", 0)
    monkeypatch.setattr(qd, "SETPOINT_DELAY", 0)
    monkeypatch.setattr(qd, "POLL", 0)
    return monkeypatch


@pytest.fixture
def notes(monkeypatch):
    """Collect the persistent notifications instead of creating them."""
    created = []

    def _create(hass, message, title=None, notification_id=None):
        created.append({"message": message, "title": title, "id": notification_id})

    monkeypatch.setattr(qd.persistent_notification, "async_create", _create)
    return created


def _snapshot(**overrides) -> dict:
    """Build the settings captured before the heating started."""
    snapshot = {
        "temperature": 21.5,
        "hvac": "off",
        "wheel": "off",
        "seat": "off",
        "captured_at": "2026-11-26T05:00:00+00:00",
    }
    snapshot.update(overrides)
    return snapshot


def _door_waits(*answers: bool):
    """Answer the polling helper in order, so each wait can end differently."""
    remaining = list(answers)

    async def _wait(self, predicate, timeout):
        return remaining.pop(0) if remaining else False

    return _wait


def _waits(*doors: bool):
    """Let the car answer the wake-up at once, then answer the door waits."""
    return _door_waits(True, *doors)


# --- registry lookup ------------------------------------------------------


def test_entity_ids_follow_a_renamed_entity(registry, ids):
    """A user rename is followed, because the mapping comes from the registry."""
    assert ids["climate"] == "climate.tiggo"
    assert ids["heat"] == "switch.tiggo_grzanie"
    assert ids["seat"] == "select.tiggo_fotel"
    assert ids["wake"] == "button.tiggo_wake"
    assert ids["online"] == "binary_sensor.tiggo_online"
    assert ids["doors"] == [
        "binary_sensor.tiggo_drzwi_fl",
        "binary_sensor.tiggo_drzwi_fr",
        "binary_sensor.tiggo_drzwi_rl",
        "binary_sensor.tiggo_drzwi_rr",
    ]


def test_entity_ids_skip_roles_the_car_does_not_offer(monkeypatch):
    """Absent roles are left out instead of being guessed."""
    rows = _registry_entries(
        [
            ("abc_hvac_climate", "climate.tiggo"),
            ("abc_door_front_left", "binary_sensor.drzwi"),
        ]
    )
    monkeypatch.setattr(qd.er, "async_get", lambda hass: "registry")
    monkeypatch.setattr(
        qd.er, "async_entries_for_config_entry", lambda entry, entry_id: rows
    )
    resolved = qd.entity_ids(_Hass(), ENTRY)
    assert set(resolved) == {"climate", "doors"}
    assert resolved["doors"] == ["binary_sensor.drzwi"]


# --- snapshot -------------------------------------------------------------


def test_heating_snapshot_reads_the_climate_wheel_and_seat():
    """The snapshot keeps the setpoint, the mode and both heat switches."""
    hass = _Hass(
        {
            "climate.tiggo": _state("heat", temperature=22.5),
            "switch.kierownica": _state("on"),
            "select.fotel": _state("medium"),
        }
    )
    snapshot = qd.heating_snapshot(
        hass,
        {
            "climate": "climate.tiggo",
            "wheel": "switch.kierownica",
            "seat": "select.fotel",
        },
    )
    assert snapshot["temperature"] == 22.5
    assert snapshot["hvac"] == "heat"
    assert snapshot["wheel"] == "on"
    assert snapshot["seat"] == "medium"
    assert snapshot["captured_at"]


def test_heating_snapshot_tolerates_entities_that_are_not_there():
    """A missing entity leaves its slot empty rather than raising."""
    snapshot = qd.heating_snapshot(_Hass(), {})
    assert snapshot["temperature"] is None
    assert snapshot["hvac"] is None
    assert snapshot["wheel"] is None
    assert snapshot["seat"] is None


# --- the sequence ---------------------------------------------------------


async def test_the_sequence_wakes_heats_and_puts_everything_back(fast, notes):
    """The happy path: wake, heat, wheel, seat, then the old settings."""
    fast.setattr(qd.QuickDepartureHeating, "_wait", _waits(False, False))
    hass = _Hass()
    heating = qd.QuickDepartureHeating(hass, ENTRY, dt_util.now() + timedelta(minutes=15))
    await heating.async_start(_snapshot())
    await heating._task

    assert hass.services.calls == [
        ("button", "press", {"entity_id": "button.tiggo_wake"}),
        ("switch", "turn_on", {"entity_id": "switch.tiggo_grzanie"}),
        ("switch", "turn_on", {"entity_id": "switch.tiggo_kierownica"}),
        (
            "select",
            "select_option",
            {"entity_id": "select.tiggo_fotel", "option": qd.SEAT_LEVEL},
        ),
        (
            "climate",
            "set_temperature",
            {"entity_id": "climate.tiggo", "temperature": 21.5},
        ),
        ("climate", "turn_off", {"entity_id": "climate.tiggo"}),
        ("switch", "turn_off", {"entity_id": "switch.tiggo_kierownica"}),
        (
            "select",
            "select_option",
            {"entity_id": "select.tiggo_fotel", "option": "off"},
        ),
    ]
    assert notes[0]["id"] == "chery_europe_quick_departure"
    assert "Grzanie zakończone" in notes[0]["message"]
    assert heating.snapshot is None
    assert heating.running is False


async def test_a_door_opened_before_the_wheel_stops_the_sequence(fast, notes):
    """Somebody getting in early ends the run before the wheel is switched on."""
    fast.setattr(qd.QuickDepartureHeating, "_wait", _waits(True))
    hass = _Hass()
    heating = qd.QuickDepartureHeating(hass, ENTRY, dt_util.now() + timedelta(minutes=15))
    await heating.async_start(_snapshot())
    await heating._task

    domains = [(domain, service) for domain, service, _ in hass.services.calls]
    assert ("switch", "turn_on") in domains
    assert hass.services.calls.count(
        ("switch", "turn_on", {"entity_id": "switch.tiggo_kierownica"})
    ) == 0
    assert "drzwi otwarte w trakcie grzania" in notes[0]["message"]
    # the old settings still came back
    assert hass.services.calls[-1][0] == "select"


async def test_a_door_opened_before_leaving_stops_the_sequence(fast, notes):
    """A door after the wheel and the seat stops the run before the departure."""
    fast.setattr(qd.QuickDepartureHeating, "_wait", _waits(False, True))
    hass = _Hass()
    heating = qd.QuickDepartureHeating(hass, ENTRY, dt_util.now() + timedelta(minutes=15))
    await heating.async_start(_snapshot())
    await heating._task

    assert (
        "switch",
        "turn_on",
        {"entity_id": "switch.tiggo_kierownica"},
    ) in hass.services.calls
    assert "drzwi otwarte przed wyjazdem" in notes[0]["message"]


async def test_a_failing_command_restores_the_settings_and_says_so(fast, notes):
    """A refusal from the car ends the run, restores and reports the error."""
    fast.setattr(qd.QuickDepartureHeating, "_wait", _waits(False, False))
    hass = _Hass(fail_on=("switch", "turn_on"))
    heating = qd.QuickDepartureHeating(hass, ENTRY, dt_util.now() + timedelta(minutes=15))
    await heating.async_start(_snapshot())
    await heating._task

    assert "błędem" in notes[0]["message"]
    assert heating.snapshot is None
    assert ("climate", "turn_off", {"entity_id": "climate.tiggo"}) in hass.services.calls


async def test_a_car_that_never_answers_stops_before_the_heat(fast, notes):
    """No answer to the wake-up ends the run instead of driving a sleeping car."""
    fast.setattr(qd, "WAKE_TIMEOUT", 45)
    fast.setattr(qd.QuickDepartureHeating, "_wait", _door_waits(False))
    hass = _Hass()
    heating = qd.QuickDepartureHeating(hass, ENTRY, dt_util.now() + timedelta(minutes=15))
    await heating.async_start(_snapshot())
    await heating._task

    assert hass.services.calls == [("button", "press", {"entity_id": "button.tiggo_wake"})]
    assert "auto nie odpowiedziało w 45 s" in notes[0]["message"]
    assert heating.snapshot is None
    assert heating.running is False


async def test_a_car_without_the_online_sensor_is_heated_anyway(monkeypatch, fast, notes):
    """A car that does not report "online" is not mistaken for a sleeping one."""
    rows = _registry_entries(
        [
            ("abc_hvac_climate", "climate.tiggo"),
            ("abc_heat_everything", "switch.tiggo_grzanie"),
            ("abc_steering_wheel_heating", "switch.tiggo_kierownica"),
            ("abc_driver_seat_heating_level_select", "select.tiggo_fotel"),
        ]
    )
    monkeypatch.setattr(qd.er, "async_get", lambda hass: "registry")
    monkeypatch.setattr(
        qd.er, "async_entries_for_config_entry", lambda entry, entry_id: rows
    )
    fast.setattr(qd.QuickDepartureHeating, "_wait", _door_waits(False, False))
    hass = _Hass()
    heating = qd.QuickDepartureHeating(hass, ENTRY, dt_util.now() + timedelta(minutes=15))
    await heating.async_start(_snapshot())
    await heating._task

    assert ("switch", "turn_on", {"entity_id": "switch.tiggo_grzanie"}) in hass.services.calls
    assert "Grzanie zakończone" in notes[0]["message"]


# --- lifecycle ------------------------------------------------------------


async def test_cancel_stops_the_sequence_and_restores_the_settings(fast, notes):
    """Turning the switch off restores at once and forgets the snapshot."""

    async def _hang(self, predicate, timeout):
        await asyncio.Event().wait()

    fast.setattr(qd.QuickDepartureHeating, "_wait", _hang)
    hass = _Hass()
    heating = qd.QuickDepartureHeating(hass, ENTRY, dt_util.now() + timedelta(minutes=15))
    await heating.async_start(_snapshot())
    assert heating.running is True

    await heating.async_cancel()

    assert heating.running is False
    assert heating.snapshot is None
    assert ("climate", "turn_off", {"entity_id": "climate.tiggo"}) in hass.services.calls
    assert notes == []


async def test_stop_leaves_the_car_alone(fast, notes):
    """A reload stops the task without touching the car."""

    async def _hang(self, predicate, timeout):
        await asyncio.Event().wait()

    fast.setattr(qd.QuickDepartureHeating, "_wait", _hang)
    hass = _Hass()
    heating = qd.QuickDepartureHeating(hass, ENTRY, dt_util.now() + timedelta(minutes=15))
    await heating.async_start(_snapshot())

    await heating.async_stop()

    assert heating.running is False
    assert hass.services.calls == []
    assert heating.snapshot is not None


async def test_retarget_keeps_the_snapshot_and_the_old_settings(fast, notes):
    """A second switch-on moves the deadline instead of capturing a new state."""
    fast.setattr(qd.QuickDepartureHeating, "_wait", _waits(False, False))
    hass = _Hass()
    first = dt_util.now() + timedelta(minutes=15)
    heating = qd.QuickDepartureHeating(hass, ENTRY, first)
    await heating.async_start(_snapshot())
    later = first + timedelta(hours=1)

    await heating.async_retarget(later)
    await heating._task

    assert heating.snapshot is None  # the run finished and restored
    assert hass.services.calls.count(
        ("switch", "turn_on", {"entity_id": "switch.tiggo_kierownica"})
    ) == 1
    assert notes[0]["message"].startswith("Grzanie zakończone")


async def test_retarget_without_a_snapshot_does_nothing(fast, notes):
    """Nothing to restore means nothing to restart."""
    hass = _Hass()
    heating = qd.QuickDepartureHeating(hass, ENTRY, dt_util.now() + timedelta(minutes=15))

    await heating.async_retarget(dt_util.now() + timedelta(hours=2))

    assert heating.running is False
    assert heating.snapshot is None
    assert hass.services.calls == []


async def test_resume_after_a_restart_only_restores(fast, notes):
    """An adopted snapshot puts the settings back without driving the car."""
    hass = _Hass()
    heating = qd.QuickDepartureHeating(hass, ENTRY, dt_util.utcnow())
    heating.adopt(_snapshot())

    await heating.async_restore()

    assert hass.services.calls == [
        (
            "climate",
            "set_temperature",
            {"entity_id": "climate.tiggo", "temperature": 21.5},
        ),
        ("climate", "turn_off", {"entity_id": "climate.tiggo"}),
        ("switch", "turn_off", {"entity_id": "switch.tiggo_kierownica"}),
        (
            "select",
            "select_option",
            {"entity_id": "select.tiggo_fotel", "option": "off"},
        ),
    ]
    assert heating.snapshot is None


# --- restoring ------------------------------------------------------------


async def test_restore_keeps_a_climate_that_was_already_on(fast, notes):
    """A climate that was heating is put back into its mode, not turned off."""
    hass = _Hass()
    await qd.restore_heating(hass, ENTRY, _snapshot(hvac="heat"))

    assert (
        "climate",
        "set_hvac_mode",
        {"entity_id": "climate.tiggo", "hvac_mode": "heat"},
    ) in hass.services.calls
    assert ("climate", "turn_off", {"entity_id": "climate.tiggo"}) not in hass.services.calls


async def test_restore_skips_states_it_cannot_put_back(fast, notes):
    """An unknown switch state is left alone instead of being guessed."""
    hass = _Hass()
    await qd.restore_heating(
        hass, ENTRY, _snapshot(hvac=None, temperature=None, wheel="unknown", seat="unavailable")
    )

    assert hass.services.calls == [("climate", "turn_off", {"entity_id": "climate.tiggo"})]


async def test_restore_is_best_effort_when_the_car_refuses(monkeypatch):
    """A refusal while restoring is logged, not raised at the caller."""
    monkeypatch.setattr(qd, "SETPOINT_DELAY", 0)
    hass = _Hass(fail_on=("climate", "set_temperature"))

    await qd.restore_heating(hass, ENTRY, _snapshot())


# --- small helpers --------------------------------------------------------


async def test_seconds_never_go_negative(fast):
    """A deadline in the past means zero, not a negative wait."""
    heating = qd.QuickDepartureHeating(_Hass(), ENTRY, dt_util.now())
    assert heating._seconds(dt_util.now() - timedelta(minutes=5)) == 0.0
    assert heating._seconds(dt_util.now() + timedelta(minutes=5)) > 0.0
