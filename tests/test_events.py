"""Event calendar loading and spike attribution."""
import datetime as dt
from steammkt.events import EventCalendar

CAL = EventCalendar("config/events.yaml")


def test_calendar_loads():
    assert len(CAL.events) >= 25
    assert all(isinstance(e.date, dt.date) for e in CAL.events)


def test_events_are_sorted():
    dates = [e.date for e in CAL.events]
    assert dates == sorted(dates)


def test_attributes_the_tradeup_crash():
    why = CAL.attribute(dt.date(2025, 10, 24), -0.62, "knife")
    assert "TRADE-UP" in why


def test_attributes_a_supply_flood_to_the_rotation():
    why = CAL.attribute(dt.date(2026, 7, 9), -0.30, "sticker")
    assert "ARMORY ROTATION" in why


def test_unexplained_moves_say_so():
    assert CAL.attribute(dt.date(2019, 1, 1), 0.5) == "unexplained"


def test_direction_mismatch_is_flagged_not_hidden():
    """An event that moved the wrong way must be reported as a coincidence,
    never dressed up as a cause."""
    why = CAL.attribute(dt.date(2025, 10, 23), +0.40, "knife")
    assert "direction mismatch" in why


def test_decay_phases():
    assert CAL.decay_phase(3) == "launch"
    assert CAL.decay_phase(20) == "supply_flood"
    assert CAL.decay_phase(60) == "stabilise"
    assert CAL.decay_phase(400) == "maturity"
