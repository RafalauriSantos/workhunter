import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from storage.sqlite_store import SQLiteStore
from workhunter_cli import audit_job, audit_sources, main
from core.deduplicator import Deduplicator
from models.job import Job
from monitor import compose_discovered_jobs


def test_source_audit_counts_unique_captures_filters_and_verified_latency(tmp_path):
    store = SQLiteStore(tmp_path / 'state.db')
    store.record_collection_attempt({
        'cycle_id': 'cycle-1', 'source': 'github', 'operation': 'issues',
        'query': {'repo': 'backend-br/vagas'},
        'started': '2026-10-16T10:04:59+00:00',
        'finished': '2026-10-16T10:05:00+00:00',
        'http_status': 200, 'result_count': 3,
        'native_ids': ['job-1', 'job-1', 'job-2'],
    })
    store.record_collection_attempt({
        'cycle_id': 'cycle-1', 'source': 'github', 'operation': 'detail',
        'started': '2026-10-16T10:05:00+00:00',
        'finished': '2026-10-16T10:05:03+00:00',
        'http_status': 200, 'result_count': 1, 'native_ids': ['job-1'],
    })
    store.record_collection_attempt({
        'cycle_id': 'cycle-old', 'source': 'github', 'operation': 'issues',
        'started': '2026-10-01T10:00:00+00:00',
        'finished': '2026-10-01T10:00:01+00:00',
        'http_status': 200, 'result_count': 1, 'native_ids': ['old-job'],
    })
    with store.connect() as db:
        for native_id, published in (
            ('job-1', '2026-10-16T10:00:00+00:00'), ('job-2', ''),
        ):
            db.execute('INSERT INTO jobs(source,external_id,payload) VALUES (?,?,?)', (
                'github', native_id, json.dumps({
                    'published_at': published,
                    'sources': {'github': {'source_job_id': native_id}},
                }),
            ))
        db.execute("INSERT INTO events(kind,payload) VALUES ('decision',?)", (json.dumps({
            'source': 'github', 'decision': 'DELIVERED',
            'timestamp': '2026-10-16T10:06:00+00:00',
        }),))
        db.execute("INSERT INTO events(kind,payload) VALUES ('decision',?)", (json.dumps({
            'source': 'github', 'decision': 'DISCARD_SCOPE',
            'timestamp': '2026-10-16T10:06:00+00:00',
        }),))
    store.record_candidate_rejection({
        'cycle_id': 'cycle-1', 'source': 'rss', 'source_job_id': 'rss-1',
        'stage': 'freshness_filter', 'reason': 'older than configured window',
        'rejected_at': '2026-10-16T10:05:00+00:00',
    })
    store.record_candidate_rejection({
        'cycle_id': 'cycle-2', 'source': 'rss', 'source_job_id': 'rss-1',
        'stage': 'freshness_filter', 'reason': 'older than configured window',
        'rejected_at': '2026-10-16T10:35:00+00:00',
    })

    result = audit_sources(
        store, since_days=7, now=datetime(2026, 10, 17, 12, tzinfo=timezone.utc)
    )

    github = result['sources']['github']
    assert github['calls'] == 1
    assert github['http_success'] == 1
    assert github['items_returned'] == 3
    assert github['unique_ids_returned'] == 2
    assert github['first_captures_in_window'] == 2
    assert github['publication_to_capture_minutes'] == {
        'samples': 1, 'median': 5.0, 'p90': 5.0, 'unknown_publication_time': 1,
    }
    assert github['decisions'] == {'DELIVERED': 1, 'DISCARD_SCOPE': 1}
    assert result['sources']['rss']['rejections'] == {
        'freshness_filter: older than configured window': 2,
    }
    assert result['sources']['rss']['rejection_records'] == 2
    assert result['sources']['rss']['unique_rejected_candidates'] == 1
    assert result['sources']['rss']['unique_rejections_by_reason'] == {
        'freshness_filter: older than configured window': 1,
    }


def test_audit_sources_cli_emits_read_only_json(tmp_path, monkeypatch, capsys):
    store = SQLiteStore(tmp_path / 'state.db')
    started = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    store.record_collection_attempt({
        'cycle_id': 'cycle-1', 'source': 'rss', 'operation': 'feed',
        'started': started,
        'finished': started,
        'http_status': 429, 'result_count': 0, 'native_ids': [],
    })
    monkeypatch.setenv('JOB_FINDER_DATA_DIR', str(tmp_path))

    assert main(['audit-sources', '--days', '1', '--json']) == 0
    report = json.loads(capsys.readouterr().out)
    assert report['sources']['rss']['http_429'] == 1
    assert 'source_health' not in store.state


def test_source_audit_separates_direct_rss_from_google_news_aggregator(tmp_path):
    store = SQLiteStore(tmp_path / 'state.db')
    for label, url in (
        ('google', 'https://news.google.com/rss/search?q=site%3Ajobs.lever.co+developer'),
        ('official-java', 'https://jobs.gft.com/services/rss/job/?keywords=java&locale=en_US'),
        ('official-python', 'https://jobs.gft.com/services/rss/job/?keywords=python&locale=en_US'),
    ):
        store.record_collection_attempt({
            'cycle_id': label, 'source': 'rss', 'operation': 'feed',
            'query': {'url': url}, 'started': '2026-10-16T10:00:00+00:00',
            'finished': '2026-10-16T10:00:01+00:00',
            'http_status': 200, 'result_count': 1, 'native_ids': [label],
        })
    store.record_candidate_rejection({
        'cycle_id': 'official-java', 'source': 'rss', 'source_job_id': 'official-java',
        'title': 'Senior Java Developer', 'stage': 'source_filter',
        'reason': 'title excluded by seniority',
        'query': {'url': 'https://jobs.gft.com/services/rss/job/?keywords=java&locale=en_US'},
        'rejected_at': '2026-10-16T10:00:02+00:00',
    })

    result = audit_sources(
        store, since_days=7, now=datetime(2026, 10, 17, 12, tzinfo=timezone.utc)
    )
    queries = result['sources']['rss']['queries']

    assert next(item for key, item in queries.items() if key.startswith('Google News:'))['type'] == 'aggregator'
    official = [item for key, item in queries.items() if key.startswith('jobs.gft.com')]
    assert len(official) == 2
    assert all(item['type'] == 'direct' for item in official)
    java_query = next(item for key, item in queries.items() if key.endswith(' · java'))
    assert java_query['rejections'] == {'source_filter: title excluded by seniority': 1}


def test_source_audit_text_reports_direct_and_aggregator_contribution(tmp_path, monkeypatch, capsys):
    store = SQLiteStore(tmp_path / 'state.db')
    for label, url in (
        ('direct', 'https://jobs.gft.com/services/rss/job/?keywords=java'),
        ('aggregate', 'https://news.google.com/rss/search?q=developer'),
    ):
        store.record_collection_attempt({
            'cycle_id': label, 'source': 'rss', 'operation': 'feed',
            'query': {'url': url}, 'started': '2026-10-16T10:00:00+00:00',
            'finished': '2026-10-16T10:00:01+00:00', 'http_status': 200,
            'result_count': 1, 'native_ids': [label],
        })
    monkeypatch.setenv('JOB_FINDER_DATA_DIR', str(tmp_path))

    assert main(['audit-sources', '--days', '7']) == 0
    output = capsys.readouterr().out
    assert 'RSS direct: 1 consultas; 1 primeiras capturas por consulta' in output
    assert 'RSS aggregator: 1 consultas; 1 primeiras capturas por consulta' in output


def test_source_audit_interprets_legacy_naive_decision_time_as_sao_paulo(tmp_path):
    store = SQLiteStore(tmp_path / 'state.db')
    with store.connect() as db:
        db.execute("INSERT INTO events(kind,payload) VALUES ('decision',?)", (json.dumps({
            'source': 'github', 'decision': 'DISCARD_SCOPE',
            # This is the exact naive local-time format emitted by the older
            # StateStore.record_decision implementation.
            'timestamp': '2026-10-17T16:27:13.894487',
        }),))

    result = audit_sources(
        store, since_days=1, now=datetime(2026, 10, 17, 19, 44, tzinfo=timezone.utc)
    )

    assert result['sources']['github']['decisions'] == {'DISCARD_SCOPE': 1}


def test_source_audit_counts_confirmed_delivery_separately_from_decision(tmp_path):
    store = SQLiteStore(tmp_path / 'state.db')
    store.record_collection_attempt({
        'cycle_id': 'cycle-1', 'source': 'linkedin', 'operation': 'search',
        'query': {'keywords': 'frontend OR desenvolvedor', 'start': 0},
        'started': '2026-10-16T09:59:59+00:00',
        'finished': '2026-10-16T10:00:00+00:00',
        'http_status': 200, 'result_count': 2, 'native_ids': ['4473616899'],
    })
    with store.connect() as db:
        db.execute("INSERT INTO events(kind,payload) VALUES ('decision',?)", (json.dumps({
            'source': 'linkedin', 'job_id': 'linkedin:4473616899',
            'decision': 'DELIVERED', 'timestamp': '2026-10-16T07:00:00',
        }),))
    store.state['delivery_audit'] = [
        {
            'attempt_id': 'delivery-1', 'fingerprint': 'fingerprint-1',
            'source_ids': ['linkedin:4473616899'], 'channel': 'telegram',
            'status': 'DELIVERED', 'timestamp': '2026-10-16T10:00:00+00:00',
            'finished_at': '2026-10-16T10:00:02+00:00',
        },
        {
            'attempt_id': 'delivery-2', 'fingerprint': 'fingerprint-2',
            'source_ids': ['linkedin:4473616900'], 'channel': 'email',
            'status': 'FAILED', 'timestamp': '2026-10-16T10:05:00+00:00',
            'finished_at': '2026-10-16T10:05:03+00:00',
        },
    ]
    store.save()

    result = audit_sources(
        store, since_days=7, now=datetime(2026, 10, 17, 12, tzinfo=timezone.utc)
    )

    assert result['sources']['linkedin']['decisions'] == {'DELIVERED': 1}
    assert result['sources']['linkedin']['delivery'] == {
        'attempts': 2,
        'statuses': {'DELIVERED': 1, 'FAILED': 1},
        'delivered_alerts': 1,
        'by_channel': {
            'email': {'attempts': 1, 'statuses': {'FAILED': 1}, 'delivered_alerts': 0},
            'telegram': {'attempts': 1, 'statuses': {'DELIVERED': 1}, 'delivered_alerts': 1},
        },
    }
    query = result['sources']['linkedin']['queries']['keywords: frontend OR desenvolvedor']
    assert query['decisions'] == {'DELIVERED': 1}
    assert query['delivery'] == {
        'attempts': 1,
        'statuses': {'DELIVERED': 1},
        'delivered_alerts': 1,
        'attempts_by_channel': {'telegram': 1},
    }


def test_source_audit_measures_detail_enrichment_and_rss_host_failure_streak(tmp_path):
    store = SQLiteStore(tmp_path / 'state.db')
    for source, operation, url, status, result_count, timeout in (
        ('linkedin', 'detail', None, 200, 1, False),
        ('linkedin', 'detail', None, 200, 0, False),
        ('linkedin', 'detail', None, 429, 0, False),
        ('rss', 'feed', 'https://jobs.example.com/rss?a=1', 200, 2, False),
        ('rss', 'feed', 'https://jobs.example.com/rss?a=2', None, 0, True),
        ('rss', 'feed', 'https://jobs.example.com/rss?a=3', 503, 0, False),
    ):
        query = {'job_id': 'job-1'} if source == 'linkedin' else {'url': url}
        store.record_collection_attempt({
            'cycle_id': f'{source}-{operation}-{status}-{result_count}',
            'source': source, 'operation': operation, 'query': query,
            'started': '2026-10-17T10:00:00+00:00',
            'finished': '2026-10-17T10:00:01+00:00',
            'http_status': status, 'result_count': result_count,
            'timed_out': timeout,
        })

    result = audit_sources(
        store, since_days=1, now=datetime(2026, 10, 17, 12, tzinfo=timezone.utc)
    )

    assert result['sources']['linkedin']['detail_enrichment'] == {
        'attempts': 3, 'successes': 1, 'empty_results': 1,
        'http_429': 1, 'timeouts': 0, 'other_failures': 0,
        'publication_dates_found': 0, 'publication_dates_missing': 0,
    }
    assert result['rss_hosts']['jobs.example.com'] == {
        'attempts': 3, 'successes': 1, 'timeouts': 1, 'http_429': 0,
        'failures': 2, 'consecutive_failures': 2,
        'last_status': 503, 'last_error': None,
    }


def test_known_vacancy_casebook_reproduces_fast_late_and_untraceable_cases(tmp_path):
    casebook_path = Path(__file__).parent / 'fixtures' / 'known_vacancy_cases.json'
    cases = json.loads(casebook_path.read_text(encoding='utf-8'))['cases']
    store = SQLiteStore(tmp_path / 'state.db')
    delivery_audit = []

    for case in cases[:2]:
        native_id = case['native_id']
        source = case['source']
        store.record_collection_attempt({
            'cycle_id': case['case_id'], 'source': source,
            'operation': 'issues' if source == 'github' else 'search',
            'query': case['query'], 'started': case['captured_at'],
            'finished': case['captured_at'], 'http_status': 200,
            'result_count': 1, 'native_ids': [native_id],
        })
        payload = {
            'title': case['title'], 'published_at': case['published_at'],
            'sources': {source: {'source_job_id': native_id}},
        }
        with store.connect() as db:
            db.execute('INSERT INTO jobs(source,external_id,payload) VALUES (?,?,?)', (
                source, native_id, json.dumps(payload),
            ))
        delivery_audit.append({
            'attempt_id': f"delivery-{case['case_id']}",
            'fingerprint': f"fingerprint-{case['case_id']}",
            'source_ids': [f'{source}:{native_id}'], 'channel': 'telegram',
            'status': 'DELIVERED', 'timestamp': case['delivered_at'],
            'finished_at': case['delivered_at'],
        })
        store.record_decision(
            f'{source}:{native_id}', source, f"identity-{native_id}", 'hash',
            'DELIVERED', 'Confirmed delivery',
        )

    store.state['delivery_audit'] = delivery_audit
    store.save()
    linked_in_case = cases[2]
    with store.connect() as db:
        db.execute('INSERT INTO jobs(source,external_id,payload) VALUES (?,?,?)', (
            linked_in_case['source'], linked_in_case['native_id'], json.dumps({
                'title': linked_in_case['title'],
                'first_seen': linked_in_case['legacy_first_seen'],
                'sources': {linked_in_case['source']: {
                    'source_job_id': linked_in_case['native_id'],
                    'first_seen': linked_in_case['legacy_first_seen'],
                }},
            }),
        ))
    store.state['seen_ids'].append(linked_in_case['native_id'])
    store.save()

    audit = audit_sources(
        store, since_days=30, now=datetime(2026, 10, 17, 20, tzinfo=timezone.utc)
    )
    github_query = audit['sources']['github']['queries']['GitHub repository: backend-br/vagas']
    gupy_query = audit['sources']['gupy']['queries']['term: Full Stack Junior']
    assert github_query['publication_to_capture_minutes']['median'] == cases[0]['expected_capture_delay_minutes']
    assert github_query['delivery']['delivered_alerts'] == 1
    assert gupy_query['publication_to_capture_minutes']['median'] == cases[1]['expected_capture_delay_minutes']
    assert gupy_query['delivery']['delivered_alerts'] == 1

    linkedin_audit = audit_job(store, linked_in_case['source'], linked_in_case['native_id'])
    assert linkedin_audit['classification'] == linked_in_case['expected_classification']
    assert 'não há tentativa de busca' in linkedin_audit['evidence_gap']
    assert linkedin_audit['search_attempt_count'] == linked_in_case['collection_attempts']
    assert linked_in_case['root_cause'] == 'unknown_from_retained_evidence'


def test_direct_source_is_primary_when_rss_finds_the_same_listing():
    direct = Job('Junior Developer', 'Example Co', 'remote', location='Brasil',
                 description='Employer-owned listing', canonical_url='https://jobs.example.com/1')
    direct.add_source('gupy', 'direct-1', direct.canonical_url)
    rss = Job('Junior Developer', 'Example Co', 'remote', location='Brasil',
              description='Employer-owned listing', raw_url='https://news.google.com/rss/articles/1')
    rss.add_source('rss', 'aggregate-1', rss.raw_url)

    ordered = compose_discovered_jobs([], [direct], [], [], [], [rss])
    unique = Deduplicator().process(ordered)

    assert len(unique) == 1
    assert unique[0].canonical_url == 'https://jobs.example.com/1'
    assert set(unique[0].sources) == {'gupy', 'rss'}
