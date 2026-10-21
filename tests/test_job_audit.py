import json

from storage.sqlite_store import SQLiteStore
from workhunter_cli import audit_job, main
from monitor import record_pipeline_skip
from models.job import Job


def test_audit_job_traces_search_to_capture_and_decision(tmp_path):
    store = SQLiteStore(tmp_path / 'state.db')
    store.record_collection_attempt({
        'cycle_id': 'cycle-1', 'source': 'linkedin', 'operation': 'search',
        'query': {'keywords': 'frontend OR developer', 'query_id': 'wide-v1'},
        'started': '2026-10-17T10:00:00+00:00', 'finished': '2026-10-17T10:00:02+00:00',
        'http_status': 200, 'result_count': 2, 'native_ids': ['4473616899'],
    })
    store.record_collection_attempt({
        'cycle_id': 'cycle-1', 'source': 'linkedin', 'operation': 'detail',
        'query': {'job_id': '4473616899'}, 'http_status': 200,
        'result_count': 1, 'native_ids': ['4473616899'],
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
                          'DISCARD_SCOPE', 'Requisito eliminatório', final_score=68,
                          score_breakdown={'skills': 24, 'seniority': 35, 'freshness': 9},
                          match_reasons=['React identificado', 'Experiência acima do nível'])

    result = audit_job(store, 'linkedin', '4473616899')

    assert result['classification'] == 'discarded'
    assert result['published_at'] == '2026-10-17T09:50:00+00:00'
    assert result['first_seen'] == '2026-10-17T10:00:02+00:00'
    assert result['search_attempts'][0]['query']['query_id'] == 'wide-v1'
    assert len(result['search_attempts']) == 1
    assert result['decision']['reason'] == 'Requisito eliminatório'
    assert result['decision']['score'] == 68
    assert result['candidate_score'] == 68
    assert result['score_breakdown'] == {'skills': 24, 'seniority': 35, 'freshness': 9}
    assert result['match_reasons'] == ['React identificado', 'Experiência acima do nível']


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


def test_audit_job_links_provider_named_collection_operation_and_uses_first_hit_time(tmp_path):
    store = SQLiteStore(tmp_path / 'state.db')
    store.record_collection_attempt({
        'cycle_id': 'cycle-github', 'source': 'github', 'operation': 'issues',
        'query': {'repo': 'backend-br/vagas'},
        'started': '2026-10-17T11:00:00+00:00',
        'finished': '2026-10-17T11:00:02+00:00',
        'http_status': 200, 'result_count': 2, 'native_ids': ['5746308554'],
    })
    store.record_collection_attempt({
        'cycle_id': 'cycle-github', 'source': 'github', 'operation': 'detail',
        'query': {'job_id': '5746308554'}, 'started': '2026-10-17T11:00:02+00:00',
        'finished': '2026-10-17T11:00:05+00:00', 'http_status': 200,
        'result_count': 1, 'native_ids': ['5746308554'],
    })
    with store.connect() as db:
        db.execute('INSERT INTO jobs(source,external_id,payload) VALUES (?,?,?)', (
            'github', '5746308554', json.dumps({
                'title': 'Full Stack Engineer', 'first_seen': '2026-10-18T10:00:00',
                'sources': {'github': {'source_job_id': '5746308554',
                                       'first_seen': '2026-10-18T10:00:00'}},
            }),
        ))

    result = audit_job(store, 'github', '5746308554')

    assert len(result['search_attempts']) == 1
    assert result['search_attempts'][0]['operation'] == 'issues'
    assert result['search_attempts'][0]['query'] == {'repo': 'backend-br/vagas'}
    assert result['first_seen'] == '2026-10-17T11:00:02+00:00'
    assert result['persisted_first_seen'] == '2026-10-18T10:00:00'


def test_audit_job_bounds_repeated_search_observations_but_preserves_first_hit(tmp_path):
    store = SQLiteStore(tmp_path / 'state.db')
    for index in range(8):
        store.record_collection_attempt({
            'cycle_id': f'cycle-{index}', 'source': 'github', 'operation': 'issues',
            'query': {'repo': 'backend-br/vagas'},
            'started': f'2026-10-17T11:00:{index:02d}+00:00',
            'finished': f'2026-10-17T11:00:{index:02d}+00:00',
            'http_status': 200, 'result_count': 2, 'native_ids': ['job-1'],
        })

    result = audit_job(store, 'github', 'job-1')

    assert result['search_attempt_count'] == 8
    assert result['search_attempts_truncated'] is True
    assert len(result['search_attempts']) == 5
    assert result['search_attempts'][0]['cycle_id'] == 'cycle-0'
    assert result['queries'][0]['observations'] == 8
    assert result['queries'][0]['first_observed_at'] == '2026-10-17T11:00:00+00:00'
    assert result['queries'][0]['last_observed_at'] == '2026-10-17T11:00:07+00:00'
    assert result['queries'][0]['distinct_cycles'] == 8
    assert 'cycles' not in result['queries'][0]
    assert result['deduplication']['repeat_observations'] == 7


def test_audit_job_reports_no_evidence_without_claiming_source_miss(tmp_path):
    store = SQLiteStore(tmp_path / 'state.db')

    result = audit_job(store, 'linkedin', '99999')

    assert result['classification'] == 'no_collection_evidence_in_retained_audit'


def test_audit_job_explains_a_persisted_already_seen_skip(tmp_path):
    store = SQLiteStore(tmp_path / 'state.db')
    job = Job('Junior Frontend Developer', 'Example', 'remote')
    job.add_source('linkedin', '88888', 'https://www.linkedin.com/jobs/view/88888/')
    job.analysis['semantic_fingerprint'] = 'semantic-fp'
    payload = {
        'title': 'Junior Frontend Developer', 'company': 'Example',
        'analysis': {'semantic_fingerprint': 'semantic-fp'},
        'sources': {'linkedin': {'source_name': 'linkedin', 'source_job_id': '88888',
                                 'url': 'https://www.linkedin.com/jobs/view/88888/'}},
    }
    with store.connect() as db:
        db.execute('INSERT INTO jobs(source,external_id,payload) VALUES (?,?,?)',
                   ('linkedin', '88888', json.dumps(payload)))
    record_pipeline_skip(store, job, 'cycle-skip', 'semantic-fp', 'deduplication',
                         'Já estava marcado como visto')

    result = audit_job(store, 'linkedin', '88888')

    assert result['classification'] == 'skipped_as_already_seen'
    assert result['pipeline_skips'][0]['reason'] == 'Já estava marcado como visto'


def test_audit_job_labels_legacy_seen_state_without_inventing_a_reason(tmp_path):
    store = SQLiteStore(tmp_path / 'state.db')
    payload = {
        'title': 'Junior Frontend Developer', 'company': 'Example',
        'analysis': {'semantic_fingerprint': 'legacy-fp'},
        'sources': {'linkedin': {'source_job_id': '99988'}},
    }
    with store.connect() as db:
        db.execute('INSERT INTO jobs(source,external_id,payload) VALUES (?,?,?)',
                   ('linkedin', '99988', json.dumps(payload)))
    store.state['seen_fingerprints'].append('legacy-fp')
    store.save()

    result = audit_job(store, 'linkedin', '99988')

    assert result['classification'] == 'seen_without_decision_evidence'
    assert result['legacy_seen_state'] is True
    assert result['pipeline_skips'] == []


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


def test_audit_job_text_shows_query_filter_score_and_delivery_path(tmp_path, monkeypatch, capsys):
    store = SQLiteStore(tmp_path / 'state.db')
    store.record_collection_attempt({
        'cycle_id': 'cycle-1', 'source': 'github', 'operation': 'issues',
        'query': {'repo': 'backend-br/vagas'}, 'started': '2026-10-17T10:00:00+00:00',
        'finished': '2026-10-17T10:00:02+00:00', 'http_status': 200,
        'result_count': 1, 'native_ids': ['job-1'],
    })
    store.record_decision(
        'github:job-1', 'github', 'fp-1', 'hash-1', 'DELIVERED', 'Perfil compatível',
        final_score=72, score_breakdown={'skills': 30, 'seniority': 35, 'freshness': 7},
        match_reasons=['React identificado'],
    )
    monkeypatch.setenv('JOB_FINDER_DATA_DIR', str(tmp_path))

    assert main(['audit-job', 'github', 'job-1']) == 0
    output = capsys.readouterr().out
    assert 'backend-br/vagas' in output
    assert 'score 72/100' in output
    assert 'Componentes do score' in output
    assert 'React identificado' in output


def test_job_snapshot_is_refreshed_after_scoring(tmp_path):
    store = SQLiteStore(tmp_path / 'state.db')
    job = Job('Junior Developer', 'Example', 'remote')
    job.add_source('github', 'issue-1', 'https://github.com/example/vagas/issues/1')
    store.remember_job(job)

    job.match_score = 72
    job.score_breakdown = {'skills': 30, 'seniority': 35, 'freshness': 7}
    job.match_reasons = ['React identificado']
    store.remember_job(job)

    result = audit_job(store, 'github', 'issue-1')
    assert result['stored_job']['match_score'] == 72
    assert result['stored_job']['score_breakdown'] == job.score_breakdown
    assert result['candidate_score'] == 72

