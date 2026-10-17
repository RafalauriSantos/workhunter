import json

from storage.sqlite_store import SQLiteStore
from workhunter_cli import diagnose, main


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


def test_diagnose_links_job_to_search_query_and_capture_timestamps(tmp_path):
    store = SQLiteStore(tmp_path / 'state.db')
    store.record_manual_case('https://www.linkedin.com/jobs/view/123')
    store.record_collection_attempt({
        'cycle_id': 'cycle-123', 'source': 'linkedin', 'operation': 'search',
        'query': {'keywords': 'developer OR software', 'query_id': 'broad-v1'},
        'started': '2026-10-17T10:00:00+00:00', 'finished': '2026-10-17T10:00:02+00:00',
        'http_status': 200, 'result_count': 2, 'native_ids': ['123'],
    })
    with store.connect() as db:
        db.execute('INSERT INTO jobs(source,external_id,payload) VALUES (?,?,?)', (
            'linkedin', '123', json.dumps({
                'title': 'Junior Developer', 'published_at': '2026-10-17T09:45:00+00:00',
                'first_seen': '2026-10-17T10:00:02+00:00', 'match_score': 74,
                'score_breakdown': {'seniority': 35, 'freshness': 30},
            })
        ))
    store.record_decision('linkedin:123', 'linkedin', 'fingerprint', 'content',
                          'DISCARD_LOW_SCORE', 'Requisito sem evidência', final_score=74)

    item = diagnose(store)[0]

    assert item['first_collected_at'] == '2026-10-17T10:00:00+00:00'
    assert item['published_at'] == '2026-10-17T09:45:00+00:00'
    assert item['first_seen'] == '2026-10-17T10:00:02+00:00'
    assert item['collection_attempts'][0]['query'] == {
        'keywords': 'developer OR software', 'query_id': 'broad-v1'
    }
    assert item['candidate_score'] == 74
    assert item['decision_reason'] == 'Requisito sem evidência'


def test_diagnose_reports_candidate_rejected_inside_source_collector(tmp_path):
    store = SQLiteStore(tmp_path / 'state.db')
    store.record_manual_case('https://jobs.example/124', source='rss', native_id='item-124')
    store.record_candidate_rejection({
        'cycle_id': 'cycle-124', 'source': 'rss', 'source_job_id': 'item-124',
        'title': 'Analista Sênior', 'query': {'feed': 'Indeed RSS'},
        'stage': 'source_filter', 'reason': 'Título contém termo excluído: sênior',
        'url': 'https://jobs.example/124',
        'rejected_at': '2026-10-17T10:00:00+00:00',
    })

    item = diagnose(store)[0]

    assert item['classification'] == 'source_filtered'
    assert item['decision_reason'] == 'Título contém termo excluído: sênior'
    assert item['rejection_stage'] == 'source_filter'
    assert item['rejection_query'] == {'feed': 'Indeed RSS'}


def test_diagnose_json_exposes_full_vacancy_trace(tmp_path, monkeypatch, capsys):
    store = SQLiteStore(tmp_path / 'state.db')
    store.record_manual_case('https://www.linkedin.com/jobs/view/123')
    store.record_collection_attempt({
        'cycle_id': 'cycle-json', 'source': 'linkedin', 'operation': 'search',
        'query': {'keywords': 'developer OR software', 'query_id': 'broad-v1'},
        'started': '2026-10-17T10:00:00+00:00', 'http_status': 200,
        'native_ids': ['123'],
    })
    store.record_decision('linkedin:123', 'linkedin', 'fp', 'hash',
                          'DISCARD_LOCATION', 'Híbrida fora da região', final_score=68)
    monkeypatch.setenv('JOB_FINDER_DATA_DIR', str(tmp_path))

    assert main(['diagnose', '--json']) == 0
    result = json.loads(capsys.readouterr().out)
    assert result[0]['collection_attempts'][0]['query']['query_id'] == 'broad-v1'
    assert result[0]['decision_reason'] == 'Híbrida fora da região'
    assert result[0]['candidate_score'] == 68
