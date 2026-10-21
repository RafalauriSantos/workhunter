from datetime import datetime

from core.local_schedule import LOCAL_TZ, next_fixed_slot_poll

SLOTS = ["10:00", "22:00"]


def local(day, hour, minute):
    return datetime(2026, 10, day, hour, minute, tzinfo=LOCAL_TZ)


def test_waits_for_next_slot_when_last_poll_used_previous_slot():
    now = local(20, 9, 50)
    last_poll = local(19, 22, 5)

    assert next_fixed_slot_poll(now, last_poll, SLOTS) == local(20, 10, 0)


def test_allows_poll_when_current_slot_is_unused():
    now = local(20, 10, 4)
    last_poll = local(19, 22, 5)

    assert next_fixed_slot_poll(now, last_poll, SLOTS) is None


def test_waits_until_evening_slot_after_morning_poll():
    now = local(20, 10, 40)
    last_poll = local(20, 10, 4)

    assert next_fixed_slot_poll(now, last_poll, SLOTS) == local(20, 22, 0)


def test_waits_until_next_morning_after_evening_poll():
    now = local(20, 23, 0)
    last_poll = local(20, 22, 3)

    assert next_fixed_slot_poll(now, last_poll, SLOTS) == local(21, 10, 0)


def test_allows_first_poll_when_never_polled():
    assert next_fixed_slot_poll(local(20, 15, 0), None, SLOTS) is None
