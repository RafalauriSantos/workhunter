from datetime import datetime, timezone

from storage.sqlite_store import SQLiteStore


def test_source_rate_limit_persists_exponential_cooldown(tmp_path):
    store = SQLiteStore(tmp_path / "state.db")
    now = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)

    first = store.register_source_rate_limit("linkedin", now=now)
    reloaded = SQLiteStore(tmp_path / "state.db")

    assert first["consecutive_failures"] == 1
    assert first["cooldown_seconds"] == 1800
    assert reloaded.get_source_cooldown("linkedin", now=now)["until"] == first["until"]


def test_source_cooldown_expires_and_success_clears_it(tmp_path):
    store = SQLiteStore(tmp_path / "state.db")
    now = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
    store.register_source_rate_limit("linkedin", now=now)

    assert store.get_source_cooldown("linkedin", now=now) is not None
    assert store.get_source_cooldown("linkedin", now=datetime(2026, 10, 11, 12, 31, tzinfo=timezone.utc)) is None

    store.clear_source_cooldown("linkedin")
    assert store.get_source_cooldown("linkedin", now=now) is None


def test_last_source_poll_ignores_throttle_and_cooldown_cycles(tmp_path):
    store = SQLiteStore(tmp_path / "state.db")
    store.record_source_health("linkedin", "OK", 3)
    first = store.get_last_source_poll("linkedin")
    store.record_source_health("linkedin", "THROTTLED", 0)
    store.record_source_health("linkedin", "COOLDOWN", 0)

    assert store.get_last_source_poll("linkedin") == first
