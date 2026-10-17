import json

from storage.sqlite_store import SQLiteStore
from workhunter_cli import audit_job, main


def test_audit_job_traces_search_to_capture_and_decision(tmp_path):
    store = SQLiteStore(tmp_path / 'state.db')
    store.record_collection_attempt({
        'cycle_id': 'cycle-1', 'source': 'linkedin', 'operation': 'search',
        'query': {'keywords': 'frontend OR developer', 'query_id': 'wide-v1'},
        'started': '2026-10-17T10:00:00+00:00', 'finished': '2026-10-17T10:00:02+00:00',
        'http_status': 200, 'result_count': 2, 'native_ids': ['4473616899'],
    })
    payload = {
        'title': 'Junior Frontend Developer', 'company': 'Jungle Gaming',
        'published_at': '2026-10-17T09:50:00+00:00',
        'first_seen': '2026-10-17T10:00:02+00:00', 'match_score': 68,
        'identity_fingerprint': 'fp-1',
        'sources': {'linkedin': {'source_job_id': '4473616899', 'first_seen': '2026-10-17T10:00:02+00:00'}},
    }
    with store.connect() as db:
        db.execute('INSERT INTO jobs(source,external_id,payload) VALUES (?,?,?)',
                   ('linkedin', '4473616899', json.dumps(payload)))
    store.record_decision('linkedin:4473616899', 'linkedin', 'fp-1', 'hash-1',
                          'DISCARD_SCOPE', 'Requisito eliminatório', final_score=68)

    result = audit_job(store, 'linkedin', '4473616899')

    assert result['classification'] == 'discarded'
    assert result['published_at'] == '2026-10-17T09:50:00+00:00'
    assert result['first_seen'] == '2026-10-17T10:00:02+00:00'
    assert result['search_attempts'][0]['query']['query_id'] == 'wide-v1'
    assert result['decision']['reason'] == 'Requisito eliminatório'
    assert result['decision']['score'] == 68


def test_audit_job_distinguishes_search_hit_from_persisted_job(tmp_path):
    store = SQLiteStore(tmp_path / 'state.db')
    store.record_collection_attempt({
        'cycle_id': 'cycle-2', 'source': 'linkedin', 'operation': 'search',
        'query': {'keywords': 'developer', 'query_id': 'search-v2'},
        'started': '2026-10-17T11:00:00+00:00', 'http_status': 200,
        'result_count': 10, 'native_ids': ['12345'],
    })

    result = audit_job(store, 'linkedin', '12345')

    assert result['classification'] == 'found_in_search_not_persisted'
    assert result['stored_job'] is None
    assert result['decision'] is None


def test_audit_job_reports_no_evidence_without_claiming_source_miss(tmp_path):
    store = SQLiteStore(tmp_path / 'state.db')

    result = audit_job(store, 'linkedin', '99999')

    assert result['classification'] == 'no_collection_evidence_in_retained_audit'


def test_audit_job_connects_delivery_attempt_to_the_vacancy(tmp_path):
    store = SQLiteStore(tmp_path / 'state.db')
    payload = {'sources': {'linkedin': {'source_job_id': '55555'}}}
    with store.connect() as db:
        db.execute('INSERT INTO outbox(fingerprint,payload,status,attempts,retry_at) VALUES (?,?,?,?,?)',
                   ('delivery-fp', json.dumps(payload), 'DELIVERED', 1, 0))
    store.record_channel('delivery-fp', 'telegram', 'DELIVERED')
    store.record_decision('linkedin:55555', 'linkedin', 'identity-fp', 'content-hash',
                          'DELIVERED', 'Enviada ao Telegram', final_score=72)

    result = audit_job(store, 'linkedin', '55555')

    assert result['classification'] == 'delivered'
    assert result['outbox'][0]['status'] == 'DELIVERED'
    assert result['channel_attempts'][0]['channel'] == 'telegram'


def test_audit_job_identifies_source_filter_with_reason(tmp_path):
    store = SQLiteStore(tmp_path / 'state.db')
    store.record_candidate_rejection({
        'cycle_id': 'filter-cycle', 'source': 'linkedin', 'source_job_id': '66666',
        'title': 'Analista de Sistemas Sênior', 'stage': 'source_filter',
        'reason': 'Título contém termo excluído: sênior',
    })

    result = audit_job(store, 'linkedin', '66666')

    assert result['classification'] == 'source_filtered'
    assert result['source_rejections'][0]['reason'] == 'Título contém termo excluído: sênior'


def test_audit_job_cli_is_read_only_and_prints_json(tmp_path, monkeypatch, capsys):
    SQLiteStore(tmp_path / 'state.db')
    monkeypatch.setenv('JOB_FINDER_DATA_DIR', str(tmp_path))

    assert main(['audit-job', 'linkedin', '77777', '--json']) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['native_id'] == '77777'
    assert result['classification'] == 'no_collection_evidence_in_retained_audit'

