import json
from storage.sqlite_store import SQLiteStore
import requests
from collectors.gupy_collector import GupyCollector
from models.job import Job


class _EmptyResponse:
    status_code = 200
    text = 'data: {"result":{"content":[{"text":"{\\"data\\":{\\"data\\":[]}}"}]}}'


def test_empty_response_is_a_successful_auditable_attempt():
    class Session:
        def post(self, *args, **kwargs):
            return _EmptyResponse()
    collector = GupyCollector(Session(), [{'term': 'python'}])
    assert collector.collect() == []
    assert collector.collection_attempts[0]['http_status'] == 200
    assert collector.collection_attempts[0]['result_count'] == 0


def test_timeout_is_recorded_without_breaking_collection():
    class Session:
        def post(self, *args, **kwargs):
            raise requests.Timeout('read timeout')
    collector = GupyCollector(Session(), [{'term': 'python'}])
    assert collector.collect() == []
    attempt = collector.collection_attempts[0]
    assert attempt['timed_out'] is True
    assert attempt['error_type'] == 'Timeout'


def test_collection_attempts_and_manual_cases_are_persisted(tmp_path):
    store = SQLiteStore(tmp_path / 'state.db')
    store.record_collection_attempt({
        'cycle_id': 'cycle-1', 'source': 'linkedin', 'operation': 'search',
        'query': 'python', 'query_hash': 'abc', 'page': 0,
        'http_status': 429, 'result_count': 0, 'native_ids': [],
        'error_type': 'HTTP_429', 'timed_out': False, 'retry_count': 1,
        'reason': 'rate limit',
        'duration_ms': 12,
    })
    store.record_manual_case('https://www.linkedin.com/jobs/view/123/?trk=x', 'linkedin', '123')
    fresh = SQLiteStore(store.filepath)
    with fresh.connect() as db:
        attempt = db.execute('SELECT source,http_status,native_ids,duration_ms FROM collection_attempts').fetchone()
        manual = db.execute('SELECT normalized_url,native_id,identity_status FROM manual_cases').fetchone()
    assert attempt == ('linkedin', 429, '[]', 12)
    assert manual == ('https://www.linkedin.com/jobs/view/123', '123', 'identity_resolved')


def test_collection_attempt_serializes_structured_query(tmp_path):
    store = SQLiteStore(tmp_path / 'state.db')
    store.record_collection_attempt({
        'cycle_id': 'cycle-2', 'source': 'gupy', 'operation': 'search',
        'query': {'term': 'python', 'limit': 20}, 'started': '2026-01-01T00:00:00+00:00',
        'finished': '2026-01-01T00:00:00.100000+00:00', 'duration_ms': 100,
    })
    with store.connect() as db:
        assert db.execute('SELECT query FROM collection_attempts').fetchone()[0] == '{"limit": 20, "term": "python"}'


def test_latest_collection_attempt_returns_decoded_query_for_rotation(tmp_path):
    store = SQLiteStore(tmp_path / 'state.db')
    store.record_collection_attempt({
        'cycle_id': 'old', 'source': 'linkedin', 'operation': 'search',
        'query': {'keywords': 'older'}, 'started': '2026-10-17T10:00:00+00:00',
        'finished': '2026-10-17T10:00:01+00:00',
    })
    store.record_collection_attempt({
        'cycle_id': 'latest', 'source': 'linkedin', 'operation': 'search',
        'query': {'keywords': 'broad', 'query_id': 'broad-v1'},
        'started': '2026-10-17T16:00:00+00:00',
        'finished': '2026-10-17T16:00:01+00:00',
    })

    latest = store.get_last_collection_attempt('linkedin', 'search')

    assert latest['cycle_id'] == 'latest'
    assert latest['query'] == {'keywords': 'broad', 'query_id': 'broad-v1'}


def test_remember_job_preserves_original_first_seen_timestamp(tmp_path):
    store = SQLiteStore(tmp_path / 'state.db')
    first = Job(title='Junior Developer', company='Acme', workplace_type='remote',
                first_seen='2026-10-01T10:00:00+00:00')
    first.add_source('gupy', 'job-1', 'https://acme.gupy.io/jobs/1')
    store.remember_job(first)

    seen_again = Job(title='Junior Developer', company='Acme', workplace_type='remote',
                     first_seen='2026-10-02T10:00:00+00:00')
    seen_again.add_source('gupy', 'job-1', 'https://acme.gupy.io/jobs/1')
    store.remember_job(seen_again)

    with store.connect() as db:
        payload = json.loads(db.execute(
            'SELECT payload FROM jobs WHERE source=? AND external_id=?', ('gupy', 'job-1')
        ).fetchone()[0])
    assert payload['first_seen'] == '2026-10-01T10:00:00+00:00'


def test_candidate_filter_rejection_is_persisted_with_query_and_reason(tmp_path):
    store = SQLiteStore(tmp_path / 'state.db')
    store.record_candidate_rejection({
        'cycle_id': 'cycle-123', 'source': 'rss', 'source_job_id': 'item-123',
        'title': 'Senior Platform Engineer', 'query': {'url': 'https://feed.example/rss'},
        'stage': 'source_filter', 'reason': 'Título excluído: senior',
        'published_at': '2026-10-17T09:00:00+00:00',
    })

    with store.connect() as db:
        row = db.execute('''SELECT cycle_id,source,source_job_id,title,query,stage,reason,published_at
            FROM candidate_rejections''').fetchone()
    assert row[:4] == ('cycle-123', 'rss', 'item-123', 'Senior Platform Engineer')
    assert json.loads(row[4]) == {'url': 'https://feed.example/rss'}
    assert row[5:] == ('source_filter', 'Título excluído: senior', '2026-10-17T09:00:00+00:00')


def test_candidate_rejections_are_written_as_a_batch(tmp_path):
    store = SQLiteStore(tmp_path / 'state.db')
    store.record_candidate_rejections([
        {'cycle_id': 'cycle-1', 'source': 'rss', 'source_job_id': 'a',
         'stage': 'source_filter', 'reason': 'reason-a'},
        {'cycle_id': 'cycle-1', 'source': 'gupy', 'source_job_id': 'b',
         'stage': 'source_parse', 'reason': 'reason-b'},
    ])
    with store.connect() as db:
        rows = db.execute('SELECT source,source_job_id,reason FROM candidate_rejections ORDER BY id').fetchall()
    assert rows == [('rss', 'a', 'reason-a'), ('gupy', 'b', 'reason-b')]
