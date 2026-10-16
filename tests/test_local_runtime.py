import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone

import pytest

from storage.sqlite_store import SQLiteStore
from core.local_schedule import is_due
from core.local_runtime import exclusive_process


def test_default_state_is_shared_across_packaged_and_startup_launches(tmp_path, monkeypatch):
    from pathlib import Path
    from core.local_runtime import data_directory
    monkeypatch.delenv('JOB_FINDER_DATA_DIR', raising=False)
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: tmp_path))
    # AppData may resolve to a private MSIX overlay in a packaged parent process.
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'AppData' / 'Local'))
    packaged = data_directory()
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'Packages' / 'LocalCache'))
    startup = data_directory()
    assert packaged == startup == tmp_path / '.job-finder'


def test_explicit_data_directory_is_preserved(tmp_path, monkeypatch):
    from core.local_runtime import data_directory
    configured = tmp_path / 'custom-state'
    monkeypatch.setenv('JOB_FINDER_DATA_DIR', str(configured))
    assert data_directory() == configured


def test_corrupt_database_fails_closed(tmp_path):
    path = tmp_path / 'state.db'
    path.write_bytes(b'not a database')
    with pytest.raises(sqlite3.DatabaseError):
        SQLiteStore(path)


def test_claim_preserves_unsaved_state_and_expires(tmp_path):
    store = SQLiteStore(tmp_path / 'state.db')
    store.record_llm_call()
    assert store.claim_delivery('one', now=100, lease_seconds=10)
    assert SQLiteStore(store.filepath).get_llm_usage()['calls'] == 1
    other = SQLiteStore(store.filepath)
    assert not other.claim_delivery('one', now=105)
    assert other.claim_delivery('one', now=111)


def test_migration_repeat_and_backup_restore(tmp_path):
    source = tmp_path / 'legacy.json'
    source.write_text(json.dumps({'seen_ids': ['42'], 'seen_fingerprints': ['abc'],
                                 'llm_usage': {'date': '2026-09-20', 'calls': 2}}))
    store = SQLiteStore(tmp_path / 'state.db')
    assert store.import_json(source)['imported'] is True
    assert store.import_json(source)['imported'] is False
    assert not store.is_seen('', '42')
    assert store.is_seen('abc')
    assert store.state['legacy_seen_ids'] == ['42']
    backup = store.backup(tmp_path / 'backups')
    restored = SQLiteStore(backup)
    assert restored.state == store.state


@pytest.mark.parametrize(('now', 'last', 'expected'), [
    ('2026-09-19T03:05:00+00:00', '2026-09-19T02:00:00+00:00', True),
    ('2026-09-21T03:05:00+00:00', '2026-09-21T02:00:00+00:00', True),
    ('2026-09-21T06:00:00+00:00', '2026-09-21T02:00:00+00:00', True),
])
def test_local_schedule_honors_sixty_minute_configured_interval(now, last, expected):
    assert is_due(datetime.fromisoformat(now), datetime.fromisoformat(last)) is expected


def test_local_schedule_supports_explicit_configured_interval():
    now = datetime.fromisoformat('2026-09-21T03:05:00+00:00')
    last = datetime.fromisoformat('2026-09-21T02:00:00+00:00')

    assert is_due(now, last, 180) is False


def test_first_run_and_long_offline():
    assert is_due(datetime.now(timezone.utc), None)
    assert is_due(datetime(2026, 9, 20, tzinfo=timezone.utc),
                  datetime(2026, 1, 1, tzinfo=timezone.utc))


def test_transaction_rollback(tmp_path):
    store = SQLiteStore(tmp_path / 'state.db')
    with pytest.raises(RuntimeError):
        with store.connect() as db:
            db.execute("INSERT INTO metadata VALUES ('rollback', '{}')")
            raise RuntimeError('interrupted')
    with store.connect() as db:
        assert db.execute("SELECT value FROM metadata WHERE key='rollback'").fetchone() is None


def test_process_lock_and_release(tmp_path):
    path = tmp_path / 'run.lock'
    with exclusive_process(path) as first:
        assert first
        with exclusive_process(path) as second:
            assert not second
    with exclusive_process(path) as recovered:
        assert recovered


def test_uncertain_telegram_does_not_fallback(tmp_path):
    from core.durable_delivery import deliver
    from models.job import Job
    store = SQLiteStore(tmp_path / 'state.db')
    job = Job('Developer', 'Test', 'remote')
    class Unknown:
        last_status = 'UNKNOWN'
        def send_job_alert(self, job):
            return False
    class Forbidden:
        def send_job_alert(self, job):
            pytest.fail('Uncertain result must not send fallback')
    assert not deliver(store, job, [], Unknown(), Forbidden())
    assert store.pending_jobs() == []


def test_confirmed_failure_uses_email_and_persists(tmp_path):
    from core.durable_delivery import deliver
    from models.job import Job
    store = SQLiteStore(tmp_path / 'state.db')
    job = Job('Developer', 'Test', 'remote')
    class Failed:
        last_status = 'FAILED'
        def send_job_alert(self, job):
            return False
    class Success:
        def send_job_alert(self, job):
            return True
    assert deliver(store, job, [], Failed(), Success())
    assert SQLiteStore(store.filepath).is_seen(job.fingerprint)
    with store.connect() as db:
        assert db.execute('SELECT channel,status FROM channel_attempts ORDER BY id').fetchall() == [
            ('telegram', 'FAILED'), ('email', 'DELIVERED')]


def test_alert_after_two_failures_and_single_recovery(tmp_path):
    from core.operational_alerts import update_source_alerts
    store = SQLiteStore(tmp_path / 'state.db')
    messages = []
    def send(message):
        messages.append(message)
        return True
    update_source_alerts(store, {'gupy': 'FAILED'}, send)
    assert not messages
    update_source_alerts(store, {'gupy': 'PARTIAL'}, send)
    update_source_alerts(store, {'gupy': 'FAILED'}, send)
    assert len(messages) == 1
    update_source_alerts(store, {'gupy': 'OK'}, send)
    update_source_alerts(store, {'gupy': 'OK'}, send)
    assert len(messages) == 2


def test_throttled_source_does_not_trigger_failure_alert(tmp_path):
    from core.operational_alerts import update_source_alerts
    store = SQLiteStore(tmp_path / 'state.db')
    messages = []

    update_source_alerts(store, {'linkedin': 'THROTTLED'}, messages.append)
    update_source_alerts(store, {'linkedin': 'THROTTLED'}, messages.append)

    assert messages == []
    assert 'linkedin' not in store.state.get('source_alerts', {})


def test_stale_writer_cannot_overwrite(tmp_path):
    first = SQLiteStore(tmp_path / 'state.db')
    second = SQLiteStore(first.filepath)
    first.set_last_heartbeat('new')
    first.save()
    second.set_last_heartbeat('stale')
    with pytest.raises(RuntimeError, match='concurrently'):
        second.save()
    assert SQLiteStore(first.filepath).get_last_heartbeat() == 'new'


def test_source_identity_does_not_collide(tmp_path):
    from models.job import Job
    store = SQLiteStore(tmp_path / 'state.db')
    job = Job('Developer', 'Test', 'remote')
    job.add_source('gupy', '42', 'https://example.test/one')
    job.add_source('github', '42', 'https://example.test/two')
    store.remember_job(job)
    with store.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == 2
    store.mark_seen('a', ['gupy:42'])
    assert not store.is_seen('b', 'github:42')


def test_due_cycle_is_not_replayed_and_failure_recorded(tmp_path):
    from types import SimpleNamespace
    from core.local_runtime import execute_cycle
    store = SQLiteStore(tmp_path / 'state.db')
    health = tmp_path / 'health.json'
    calls = []
    def collect():
        calls.append(True)
        health.write_text('{"status":"SUCCESS"}')
    monitor = SimpleNamespace(run_check=collect, HEALTH_REPORT_FILE=str(health))
    assert execute_cycle(monitor, store, True) == 'COMPLETED'
    assert execute_cycle(monitor, store, True) == 'NOT_DUE'
    assert len(calls) == 1
    def failure():
        raise RuntimeError('failed collection')
    monitor.run_check = failure
    with pytest.raises(RuntimeError):
        execute_cycle(monitor, store)
    with store.connect() as db:
        assert db.execute('SELECT status FROM cycles ORDER BY id DESC LIMIT 1').fetchone()[0] == 'FAILED'


def test_stale_running_cycle_is_recovered(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from core.local_runtime import execute_cycle
    store = SQLiteStore(tmp_path / 'state.db')
    health = tmp_path / 'health.json'
    with store.connect() as db:
        db.execute("INSERT INTO cycles(started,status) VALUES (?, 'RUNNING')",
                   ('2026-09-20T00:00:00+00:00',))
    monkeypatch.setenv('JOB_FINDER_CYCLE_TIMEOUT_SECONDS', '10')
    calls = []
    def collect():
        calls.append(True)
        health.write_text('{"status":"SUCCESS"}')
    monitor = SimpleNamespace(run_check=collect, HEALTH_REPORT_FILE=str(health))
    assert execute_cycle(monitor, store, True) == 'COMPLETED'
    assert len(calls) == 1
    with store.connect() as db:
        assert db.execute("SELECT status FROM cycles WHERE id=1").fetchone()[0] == 'INTERRUPTED'


def test_collection_distinguishes_empty_and_partial():
    from collectors.base import collect_result
    class Collector:
        def collect(self):
            return []
    assert collect_result(Collector()).status == 'OK'
    class Partial:
        def collect(self):
            self.collection_issues.append('HTTP_503')
            return ['valid job']
    result = collect_result(Partial())
    assert result.status == 'PARTIAL'
    assert result.jobs == ['valid job']


def test_runner_diagnose_is_printed_to_terminal(tmp_path):
    env = dict(__import__('os').environ, JOB_FINDER_DATA_DIR=str(tmp_path))
    SQLiteStore(tmp_path / 'state.db')
    result = subprocess.run(
        [sys.executable, 'deployment/run_local.py', '--diagnose'],
        capture_output=True, text=True, env=env, cwd=__import__('pathlib').Path(__file__).parents[1],
        check=True,
    )
    payload = json.loads(result.stdout)
    assert payload['database'].endswith('state.db')
    assert 'pending_deliveries' in payload
