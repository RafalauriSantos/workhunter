from datetime import datetime, timezone

from storage.sqlite_store import SQLiteStore
from core.health import health_snapshot


def test_health_snapshot_reports_healthy_runtime(tmp_path):
    store = SQLiteStore(tmp_path / 'state.db')
    with store.connect() as db:
        db.execute("INSERT INTO cycles(started,finished,status) VALUES ('2026-10-04T12:00:00+00:00','2026-10-04T12:01:00+00:00','COMPLETED')")
    result = health_snapshot(
        store, tmp_path, cycle_max_age_seconds=200000,
        now=datetime(2026, 10, 4, 13, 0, tzinfo=timezone.utc),
    )
    assert result['database'] == 'OK'
    assert result['last_cycle_status'] == 'COMPLETED'
    assert result['status'] == 'HEALTHY'


def test_health_snapshot_marks_stale_cycle_degraded(tmp_path):
    store = SQLiteStore(tmp_path / 'state.db')
    with store.connect() as db:
        db.execute("INSERT INTO cycles(started,finished,status) VALUES ('2020-01-01T12:00:00+00:00','2020-01-01T12:01:00+00:00','COMPLETED')")
    result = health_snapshot(store, tmp_path, cycle_max_age_seconds=60)
    assert result['status'] == 'DEGRADED'
    assert 'cycle_stale' in result['issues']


def test_health_snapshot_reports_llm_state_without_calling_provider(tmp_path, monkeypatch):
    monkeypatch.setenv('GEMINI_API_KEY', 'configured-test-key')
    monkeypatch.delenv('OPENROUTER_API_KEY', raising=False)
    store = SQLiteStore(tmp_path / 'state.db')
    store.event('llm_configuration', {
        'providers': ['gemini'], 'timestamp': '2026-10-04T12:00:00+00:00',
    })
    store.event('llm_health', {
        'provider': 'gemini', 'model': 'test-model', 'status': 'OK',
        'timestamp': '2026-10-04T12:30:00+00:00',
    })

    result = health_snapshot(
        store, tmp_path,
        now=datetime(2026, 10, 4, 13, 0, tzinfo=timezone.utc),
    )

    assert result['llm'].startswith('IA: Gemini OK há ')


def test_llm_health_uses_existing_success_counters_without_claiming_provider(tmp_path, monkeypatch):
    from core.llm_health import format_health

    monkeypatch.setenv('GEMINI_API_KEY', 'configured-test-key')
    monkeypatch.setenv('OPENROUTER_API_KEY', 'configured-test-key')
    store = SQLiteStore(tmp_path / 'state.db')
    with store.connect() as db:
        db.execute('INSERT INTO cycles(started,finished,status,report) VALUES (?,?,?,?)', (
            '2026-10-05T12:00:00+00:00', '2026-10-05T12:01:00+00:00', 'COMPLETED',
            '{"llm_calls_today":4,"llm_fallbacks":0}',
        ))

    result = format_health(store, datetime(2026, 10, 5, 13, 0, tzinfo=timezone.utc))

    assert '4 análises bem-sucedidas hoje' in result
    assert '0 fallback(s) no último ciclo' in result
    assert 'provedor não individualizado' in result
