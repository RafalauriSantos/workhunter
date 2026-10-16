import json

from storage.sqlite_store import SQLiteStore
from workhunter_cli import diagnose


def test_diagnose_classifies_collected_and_uncollected_cases(tmp_path):
    store = SQLiteStore(tmp_path / 'state.db')
    store.record_manual_case('https://www.linkedin.com/jobs/view/123/?trk=x')
    store.record_manual_case('https://www.linkedin.com/jobs/view/999/?trk=x')
    store.record_collection_attempt({
        'cycle_id': 'c', 'source': 'linkedin', 'operation': 'search',
        'native_ids': ['123'], 'http_status': 200,
    })
    with store.connect() as db:
        db.execute('INSERT INTO jobs(source,external_id,payload) VALUES (?,?,?)',
                   ('linkedin', '123', json.dumps({'sources': {'linkedin': {'url': 'https://www.linkedin.com/jobs/view/123'}}})))
    result = diagnose(store)
    assert {item['native_id']: item['classification'] for item in result} == {
        '123': 'collected_no_decision', '999': 'not_collected'
    }


def test_diagnose_distinguishes_manual_analysis_states(tmp_path):
    store = SQLiteStore(tmp_path / 'state.db')
    failed_id = store.record_manual_case('https://www.linkedin.com/jobs/view/111')
    pending_id = store.record_manual_case('https://www.linkedin.com/jobs/view/222')
    done_id = store.record_manual_case('https://www.linkedin.com/jobs/view/333')
    with store.connect() as db:
        for case_id, status, result, error in (
            (failed_id, 'FAILED', None, 'timeout'),
            (done_id, 'DONE', 'FULL_EVIDENCE', None),
        ):
            db.execute('''INSERT INTO manual_analysis_queue
                (manual_case_id,status,result,error,created_at) VALUES (?,?,?,?,?)''',
                (case_id, status, result, error, 'now'))
    result = {item['native_id']: item for item in diagnose(store)}
    assert result['111']['classification'] == 'analysis_failed'
    assert result['111']['analysis_error'] == 'timeout'
    assert result['333']['classification'] == 'analyzed_without_decision'


def test_diagnose_matches_prefixed_source_ids_in_decision_audit(tmp_path):
    store = SQLiteStore(tmp_path / 'state.db')
    store.record_manual_case('https://www.linkedin.com/jobs/view/123')
    store.record_decision('linkedin:123', 'linkedin', 'fingerprint', 'content',
                          'DISCARD_LOCATION', 'fora da região')
    item = diagnose(store)[0]
    assert item['classification'] == 'collected_discarded'
    assert item['decision'] == 'DISCARD_LOCATION'
    assert item['decision_reason'] == 'fora da região'


def test_diagnose_inherits_decision_from_deduplicated_capture_alias(tmp_path):
    store = SQLiteStore(tmp_path / 'state.db')
    store.record_manual_case('https://www.linkedin.com/feed/', source='linkedin',
                             native_id='duplicate-capture')
    payload = {
        'sources': {'linkedin': {'source_job_id': 'primary-capture',
                                 'url': 'https://www.linkedin.com/feed/'}},
        'analysis': {'duplicate_listings': [
            {'source_job_id': 'duplicate-capture', 'url': 'https://www.linkedin.com/feed/'}
        ]},
    }
    with store.connect() as db:
        db.execute('INSERT INTO jobs(source,external_id,payload) VALUES (?,?,?)',
                   ('linkedin', 'primary-capture', json.dumps(payload)))
    store.record_decision('linkedin:primary-capture', 'linkedin', 'fp', 'hash',
                          'DISCARD_SCOPE', 'duplicata já avaliada')
    item = diagnose(store)[0]
    assert item['classification'] == 'collected_discarded'
    assert item['decision'] == 'DISCARD_SCOPE'
    assert item['decision_reason'] == 'duplicata já avaliada'
