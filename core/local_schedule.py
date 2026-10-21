from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

LOCAL_TZ = ZoneInfo("America/Sao_Paulo")


def daily_slots_around(now, slots):
    """Return the last fixed local slot at or before now, and the next one after it."""
    local = now.astimezone(LOCAL_TZ)
    candidates = []
    for day_offset in (-1, 0, 1):
        day = local.date() + timedelta(days=day_offset)
        for slot in slots:
            hour, minute = (int(part) for part in slot.split(":"))
            candidates.append(datetime.combine(day, time(hour, minute), tzinfo=LOCAL_TZ))
    return max(c for c in candidates if c <= local), min(c for c in candidates if c > local)


def next_fixed_slot_poll(now, last_poll_at, slots):
    """Return the next slot to wait for, or None when the current slot is still unused."""
    last_slot, next_slot = daily_slots_around(now, slots)
    if last_poll_at is not None and last_poll_at >= last_slot:
        return next_slot
    return None


def interval_at(now, configured_minutes=60):
    """Use the configured cadence; it must be short enough for the source window."""
    return timedelta(minutes=max(1, int(configured_minutes or 60)))


def is_due(now, last_started, configured_minutes=60):
    return last_started is None or now >= last_started + interval_at(now, configured_minutes)
