"""Small operator CLI for WorkHunter investigation data."""
import argparse
import json
import os
import sqlite3
import math
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse
from zoneinfo import ZoneInfo

from storage.sqlite_store import SQLiteStore
from core.source_identity import extract_source_identity


def audit_job(store, source, native_id):
    """Return the retained per-vacancy search, collection, decision and delivery evidence."""
    source, native_id = str(source), str(native_id)
    namespaced_id = f'{source}:{native_id}'
    with store.connect() as db:
        db.row_factory = sqlite3.Row
        job_row = db.execute(
            'SELECT payload FROM jobs WHERE source=? AND external_id=?',
            (source, native_id),
        ).fetchone()
        # Collectors use provider-specific operation names for discovery
        # (e.g. GitHub `issues`, RSS `feed`, GeekHunter `listing`). Only
        # `detail` calls enrich a vacancy already found and must not count as
        # evidence that a search returned it.
        attempt_rows = db.execute('''SELECT cycle_id,operation,query,started,finished,
            result_count,native_ids,http_status,error_type,timed_out,reason
            FROM collection_attempts WHERE source=? AND operation!='detail' ORDER BY id''', (source,)).fetchall()
        rejection_rows = db.execute('''SELECT cycle_id,title,query,url,published_at,
            stage,reason,created_at FROM candidate_rejections
            WHERE source=? AND source_job_id=? ORDER BY id''', (source, native_id)).fetchall()
        event_rows = db.execute("SELECT payload FROM events WHERE kind='decision'").fetchall()
        pipeline_skip_rows = db.execute(
            "SELECT payload FROM events WHERE kind='pipeline_skip'"
        ).fetchall()
        outbox_rows = db.execute('SELECT fingerprint,payload,status,attempts,retry_at FROM outbox').fetchall()
        channel_rows = db.execute('SELECT fingerprint,channel,status,created FROM channel_attempts').fetchall()
        state_row = db.execute("SELECT value FROM metadata WHERE key='state'").fetchone()
        state = json.loads(state_row[0]) if state_row else {}

    search_attempts = []
    for row in attempt_rows:
        try:
            ids = [str(item) for item in json.loads(row['native_ids'] or '[]')]
        except (TypeError, ValueError):
            continue
        if native_id not in ids:
            continue
        try:
            query = json.loads(row['query']) if row['query'] else None
        except (TypeError, ValueError):
            query = row['query']
        search_attempts.append({
            'cycle_id': row['cycle_id'], 'operation': row['operation'], 'query': query,
            'started': row['started'], 'finished': row['finished'],
            'result_count': row['result_count'], 'http_status': row['http_status'],
            'error_type': row['error_type'], 'timed_out': bool(row['timed_out']),
            'reason': row['reason'],
        })

    query_summary = {}
    for attempt in search_attempts:
        query = attempt.get('query')
        try:
            query_key = json.dumps(query, ensure_ascii=False, sort_keys=True, default=str)
        except (TypeError, ValueError):
            query_key = str(query)
        summary = query_summary.setdefault(query_key, {
            'query': query, 'observations': 0, 'first_observed_at': None,
            'last_observed_at': None, 'cycles': [],
        })
        summary['observations'] += 1
        observed_at = attempt.get('finished') or attempt.get('started')
        if observed_at and (summary['first_observed_at'] is None
                            or observed_at < summary['first_observed_at']):
            summary['first_observed_at'] = observed_at
        if observed_at and (summary['last_observed_at'] is None
                            or observed_at > summary['last_observed_at']):
            summary['last_observed_at'] = observed_at
        if attempt.get('cycle_id') and attempt['cycle_id'] not in summary['cycles']:
            summary['cycles'].append(attempt['cycle_id'])

    rejections = []
    for row in rejection_rows:
        try:
            query = json.loads(row['query']) if row['query'] else None
        except (TypeError, ValueError):
            query = row['query']
        rejections.append({
            'cycle_id': row['cycle_id'], 'title': row['title'], 'query': query,
            'url': row['url'], 'published_at': row['published_at'],
            'stage': row['stage'], 'reason': row['reason'], 'created_at': row['created_at'],
        })

    decisions = []
    for row in event_rows:
        try:
            decision = json.loads(row['payload'])
        except (TypeError, ValueError):
            continue
        if decision.get('source') == source and str(decision.get('job_id')) in {native_id, namespaced_id}:
            decisions.append(decision)

    payload = json.loads(job_row['payload']) if job_row else None
    payload_analysis = (payload or {}).get('analysis') or {}
    fingerprint = (payload_analysis.get('semantic_fingerprint')
                   or (payload or {}).get('identity_fingerprint'))
    pipeline_skips = []
    for row in pipeline_skip_rows:
        try:
            item = json.loads(row['payload'])
        except (TypeError, ValueError):
            continue
        source_ids = [str(value) for value in item.get('source_ids', [])]
        if (namespaced_id in source_ids or native_id in source_ids
                or (fingerprint and item.get('fingerprint') == fingerprint)):
            pipeline_skips.append(item)

    legacy_seen_state = bool(
        (fingerprint and fingerprint in state.get('seen_fingerprints', []))
        or native_id in [str(value) for value in state.get('seen_ids', [])]
        or namespaced_id in [str(value) for value in state.get('seen_ids', [])]
    )

    matched_outbox = []
    matched_fingerprints = set()
    for row in outbox_rows:
        try:
            data = json.loads(row['payload'])
        except (TypeError, ValueError):
            continue
        sources = data.get('sources') or {}
        identity = sources.get(source) or {}
        aliases = (data.get('analysis') or {}).get('duplicate_listings') or []
        if (str(identity.get('source_job_id', '')) == native_id or
                any(str(item.get('source_job_id', '')) == native_id for item in aliases)):
            matched_fingerprints.add(row['fingerprint'])
            matched_outbox.append({
                'status': row['status'], 'attempts': row['attempts'],
                'retry_at': row['retry_at'], 'fingerprint': row['fingerprint'],
            })
    channels = [{
        'channel': row['channel'], 'status': row['status'], 'created_at': row['created'],
    } for row in channel_rows if row['fingerprint'] in matched_fingerprints]
    delivery_history = [item for item in state.get('delivery_audit', []) if (
        namespaced_id in [str(value) for value in item.get('source_ids', [])]
        or native_id in [str(value) for value in item.get('source_ids', [])]
    )]

    if decisions:
        latest = decisions[-1]
        decision_name = latest.get('decision', '')
        classification = ('delivered' if decision_name == 'DELIVERED' else
                          'delivery_failed' if decision_name == 'DELIVERY_FAILED' else 'discarded')
        decision_summary = {
            'decision': decision_name, 'reason': latest.get('decision_reason'),
            'score': latest.get('final_score'), 'timestamp': latest.get('timestamp'),
            'score_breakdown': latest.get('score_breakdown') or {},
            'match_reasons': latest.get('match_reasons') or [],
        }
    else:
        decision_summary = None
        classification = ('skipped_as_already_seen' if pipeline_skips and
                          pipeline_skips[-1].get('stage') == 'deduplication' else
                          'delivery_retry_deferred' if pipeline_skips else
                          'seen_without_decision_evidence' if payload and legacy_seen_state else
                          'collected_no_decision' if payload else
                          'source_filtered' if rejections else
                          'found_in_search_not_persisted' if search_attempts else
                          'no_collection_evidence_in_retained_audit')

    evidence_gap = None
    if classification == 'seen_without_decision_evidence':
        evidence_gap = (
            'A vaga consta no estado legado de "já vista", mas não há tentativa de busca, '
            'rejeição ou decisão retida para reconstruir por que não foi enviada.'
        )
    elif classification == 'found_in_search_not_persisted':
        evidence_gap = (
            'O ID apareceu em uma resposta de busca, mas não há registro persistido da vaga; '
            'a evidência disponível não identifica em qual etapa ela se perdeu.'
        )

    source_info = (payload or {}).get('sources', {}).get(source, {})
    persisted_first_seen = source_info.get('first_seen') or (payload or {}).get('first_seen')
    capture_times = [item['finished'] for item in search_attempts if item.get('finished')]
    first_seen = min(capture_times) if capture_times else persisted_first_seen
    # Busy feeds can return the same vacancy on every scheduled poll. Keep
    # every observation in SQLite while bounding CLI output to the first hit
    # and the four latest observations.
    attempt_count = len(search_attempts)
    if attempt_count > 5:
        search_attempts = [search_attempts[0], *search_attempts[-4:]]
    compact_queries = []
    for summary in query_summary.values():
        cycles = summary.pop('cycles')
        summary['distinct_cycles'] = len(cycles)
        summary['first_cycle_id'] = cycles[0] if cycles else None
        summary['last_cycle_id'] = cycles[-1] if cycles else None
        compact_queries.append(summary)

    return {
        'source': source, 'native_id': native_id, 'classification': classification,
        'evidence_gap': evidence_gap,
        'title': (payload or {}).get('title') or (rejections[-1]['title'] if rejections else None),
        'published_at': (payload or {}).get('published_at') or (rejections[-1]['published_at'] if rejections else None),
        'first_seen': first_seen,
        'persisted_first_seen': persisted_first_seen,
        'search_attempts': search_attempts, 'source_rejections': rejections,
        'search_attempt_count': attempt_count,
        'search_attempts_truncated': attempt_count > len(search_attempts),
        'queries': compact_queries,
        'pipeline_skips': pipeline_skips,
        'legacy_seen_state': legacy_seen_state,
        'stored_job': payload,
        'decision': decision_summary,
        'candidate_score': (decision_summary.get('score') if decision_summary else
                            (payload or {}).get('match_score')),
        'score_breakdown': ((decision_summary or {}).get('score_breakdown')
                            or (payload or {}).get('score_breakdown') or {}),
        'match_reasons': ((decision_summary or {}).get('match_reasons')
                          or (payload or {}).get('match_reasons') or []),
        'outbox': matched_outbox, 'channel_attempts': channels,
        'delivery_history': delivery_history,
        'deduplication': {
            'source_count': len((payload or {}).get('sources') or {}),
            'duplicate_listings': ((payload or {}).get('analysis') or {}).get('duplicate_listings', []),
            'capture_observations': attempt_count,
            'repeat_observations': max(0, attempt_count - 1),
            'distinct_queries': len(query_summary),
        },
    }


def _parse_utc(value, naive_timezone=timezone.utc):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=naive_timezone)
    return parsed.astimezone(timezone.utc)


def _query_descriptor(source, raw_query):
    try:
        query = json.loads(raw_query) if isinstance(raw_query, str) else raw_query
    except (TypeError, ValueError):
        query = raw_query
    if isinstance(query, dict):
        if query.get('repo'):
            return 'direct', f"GitHub repository: {query['repo']}"
        url = query.get('url')
        if url:
            parsed = urlparse(str(url))
            host = parsed.netloc.lower()
            if host == 'news.google.com' or host.endswith('.google.com'):
                phrase = parse_qs(parsed.query).get('q', [''])[0]
                return 'aggregator', f"Google News: {unquote(phrase)[:140]}"
            label = f"{host}{parsed.path[:70]}"
            query_text = parse_qs(parsed.query).get('keywords', [''])[0]
            if query_text:
                label += f" · {unquote(query_text)[:100]}"
            return 'direct', label
        for key in ('keywords', 'term', 'query', 'companyId'):
            if query.get(key):
                return ('direct' if source != 'rss' else 'aggregator'), f"{key}: {str(query[key])[:140]}"
        return 'unknown', json.dumps(query, ensure_ascii=False, sort_keys=True)[:140]
    return 'unknown', str(query or 'query not recorded')[:140]


def audit_sources(store, since_days=7, now=None):
    """Summarize source attempts, captured IDs, pipeline outcomes, and latency."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=max(1, int(since_days)))
    with store.connect() as db:
        attempts = db.execute('''SELECT source,operation,query,started,finished,http_status,result_count,
            native_ids,timed_out,error_type,reason FROM collection_attempts ORDER BY id''').fetchall()
        jobs = db.execute('SELECT source,external_id,payload FROM jobs').fetchall()
        decisions = db.execute("SELECT payload FROM events WHERE kind='decision'").fetchall()
        state_row = db.execute("SELECT value FROM metadata WHERE key='state'").fetchone()
        rejections = db.execute('''SELECT source,source_job_id,stage,reason,created_at,query
            FROM candidate_rejections WHERE created_at >= ?''', (cutoff.isoformat(),)).fetchall()
    try:
        persisted_state = json.loads(state_row[0]) if state_row else {}
    except (TypeError, ValueError):
        persisted_state = {}

    stats = defaultdict(lambda: {
        'calls': 0, 'http_success': 0, 'http_429': 0, 'timeouts': 0,
        'items_returned': 0, 'unique_ids_returned': set(), 'first_captures': set(),
        'decisions': Counter(), 'rejections': Counter(), 'latency_minutes': [],
        'unique_rejected_ids': set(), 'unique_rejections_by_reason': defaultdict(set),
        'delivery_attempts': Counter(), 'delivery_statuses': Counter(),
        'delivery_by_channel': defaultdict(lambda: {
            'attempts': 0, 'statuses': Counter(), 'delivered_fingerprints': set(),
        }),
        'unknown_publication_time': 0, 'queries': defaultdict(lambda: {
            'type': 'unknown', 'calls': 0, 'http_success': 0, 'http_429': 0,
            'timeouts': 0, 'items_returned': 0, 'unique_ids_returned': set(),
            'first_captures': set(), 'latency_minutes': [], 'unknown_publication_time': 0,
            'decisions': Counter(), 'delivery_attempts': Counter(),
            'delivery_statuses': Counter(), 'delivered_fingerprints': set(),
            'rejections': Counter(), 'unique_rejected_ids': set(),
            'unique_rejections_by_reason': defaultdict(set),
        }),
    })
    first_capture = {}
    detail_metrics = defaultdict(lambda: {
        'attempts': 0, 'successes': 0, 'empty_results': 0, 'http_429': 0,
        'timeouts': 0, 'other_failures': 0,
        'publication_dates_found': 0, 'publication_dates_missing': 0,
    })
    rss_host_attempts = defaultdict(list)
    for (source, operation, raw_query, started, finished, http_status, result_count,
         raw_ids, timed_out, error_type, reason) in attempts:
        source = str(source)
        if operation == 'detail':
            detail_finished = _parse_utc(finished)
            if detail_finished and detail_finished >= cutoff:
                stats[source]
                detail = detail_metrics[source]
                detail['attempts'] += 1
                detail['successes'] += int(isinstance(http_status, int) and 200 <= http_status < 300 and int(result_count or 0) > 0)
                detail['empty_results'] += int(isinstance(http_status, int) and 200 <= http_status < 300 and int(result_count or 0) == 0)
                detail['http_429'] += int(http_status == 429)
                detail['timeouts'] += int(bool(timed_out))
                detail['other_failures'] += int(bool(error_type) and http_status != 429 and not timed_out)
                detail['publication_dates_found'] += int(reason == 'publication_date_found')
                detail['publication_dates_missing'] += int(reason == 'publication_date_missing')
        if source == 'rss' and operation == 'feed':
            try:
                query = json.loads(raw_query) if raw_query else {}
            except (TypeError, ValueError):
                query = {}
            feed_url = query.get('url', '') if isinstance(query, dict) else ''
            host = (urlparse(str(feed_url)).hostname or 'host-desconhecido').lower()
            rss_host_attempts[host].append({
                'started': started, 'finished': finished, 'http_status': http_status,
                'timed_out': bool(timed_out), 'error_type': error_type,
            })
        captured_at = _parse_utc(finished)
        if operation == 'detail' or not captured_at:
            continue
        query_type, query_label = _query_descriptor(str(source), raw_query)
        try:
            ids = {str(item) for item in json.loads(raw_ids or '[]') if item}
        except (TypeError, ValueError):
            ids = set()
        for native_id in ids:
            key = (str(source), native_id)
            if key not in first_capture or captured_at < first_capture[key]['at']:
                first_capture[key] = {'at': captured_at, 'query': query_label}
        if captured_at < cutoff:
            continue
        item = stats[str(source)]
        query_item = item['queries'][query_label]
        query_item['type'] = query_type
        item['calls'] += 1
        item['http_success'] += isinstance(http_status, int) and 200 <= http_status < 300
        item['http_429'] += http_status == 429
        item['timeouts'] += bool(timed_out)
        item['items_returned'] += int(result_count or 0)
        item['unique_ids_returned'].update(ids)
        query_item['calls'] += 1
        query_item['http_success'] += isinstance(http_status, int) and 200 <= http_status < 300
        query_item['http_429'] += http_status == 429
        query_item['timeouts'] += bool(timed_out)
        query_item['items_returned'] += int(result_count or 0)
        query_item['unique_ids_returned'].update(ids)

    job_publication = {}
    for source, native_id, raw_payload in jobs:
        try:
            payload = json.loads(raw_payload or '{}')
        except (TypeError, ValueError):
            continue
        job_publication[(str(source), str(native_id))] = payload.get('published_at')

    for key, capture in first_capture.items():
        captured_at = capture['at']
        if captured_at < cutoff:
            continue
        source, native_id = key
        item = stats[source]
        item['first_captures'].add(native_id)
        query_item = item['queries'][capture['query']]
        query_item['first_captures'].add(native_id)
        published_at = _parse_utc(job_publication.get(key))
        if not published_at:
            item['unknown_publication_time'] += 1
            query_item['unknown_publication_time'] += 1
            continue
        delay = (captured_at - published_at).total_seconds() / 60
        if delay < 0:
            item['unknown_publication_time'] += 1
            query_item['unknown_publication_time'] += 1
            continue
        item['latency_minutes'].append(delay)
        query_item['latency_minutes'].append(delay)

    for (raw,) in decisions:
        try:
            decision = json.loads(raw)
        except (TypeError, ValueError):
            continue
        # StateStore.record_decision historically writes datetime.now() with
        # no timezone, so those values are local wall time (America/Sao_Paulo).
        # Collection-attempt and job timestamps are UTC-aware; only decisions
        # need this legacy compatibility rule.
        timestamp = _parse_utc(decision.get('timestamp'), ZoneInfo('America/Sao_Paulo'))
        source = str(decision.get('source') or '')
        if timestamp and timestamp >= cutoff and source:
            decision_name = str(decision.get('decision') or 'UNKNOWN')
            stats[source]['decisions'][decision_name] += 1
            raw_job_id = str(decision.get('job_id') or '')
            prefix = f'{source}:'
            native_id = raw_job_id[len(prefix):] if raw_job_id.startswith(prefix) else raw_job_id
            capture = first_capture.get((source, native_id))
            query_label = capture.get('query') if capture else None
            if query_label in stats[source]['queries']:
                stats[source]['queries'][query_label]['decisions'][decision_name] += 1
    for delivery in persisted_state.get('delivery_audit', []):
        timestamp = _parse_utc(delivery.get('finished_at') or delivery.get('timestamp'))
        if not timestamp or timestamp < cutoff:
            continue
        status = str(delivery.get('status') or 'UNKNOWN').upper()
        channel = str(delivery.get('channel') or 'unknown')
        fingerprint = str(delivery.get('fingerprint') or delivery.get('attempt_id') or '')
        for raw_source_id in delivery.get('source_ids') or []:
            source = str(raw_source_id).split(':', 1)[0]
            if not source:
                continue
            item = stats[source]
            item['delivery_attempts'][channel] += 1
            item['delivery_statuses'][status] += 1
            channel_stats = item['delivery_by_channel'][channel]
            channel_stats['attempts'] += 1
            channel_stats['statuses'][status] += 1
            if status == 'DELIVERED' and fingerprint:
                channel_stats['delivered_fingerprints'].add(fingerprint)
            raw_source_id = str(raw_source_id)
            native_id = raw_source_id.split(':', 1)[1] if ':' in raw_source_id else raw_source_id
            capture = first_capture.get((source, native_id))
            query_label = capture.get('query') if capture else None
            if query_label in item['queries']:
                query_stats = item['queries'][query_label]
                query_stats['delivery_attempts'][channel] += 1
                query_stats['delivery_statuses'][status] += 1
                if status == 'DELIVERED' and fingerprint:
                    query_stats['delivered_fingerprints'].add((channel, fingerprint))
    for source, source_job_id, stage, reason, created_at, raw_query in rejections:
        if _parse_utc(created_at):
            source = str(source)
            rejection = f'{stage}: {reason}'
            item = stats[source]
            item['rejections'][rejection] += 1
            query_type, query_label = _query_descriptor(source, raw_query)
            query_item = item['queries'][query_label]
            query_item['type'] = query_type
            query_item['rejections'][rejection] += 1
            native_id = str(source_job_id or '')
            if native_id:
                item['unique_rejected_ids'].add(native_id)
                item['unique_rejections_by_reason'][rejection].add(native_id)
                query_item['unique_rejected_ids'].add(native_id)
                query_item['unique_rejections_by_reason'][rejection].add(native_id)

    rss_hosts = {}
    for host, host_attempts in sorted(rss_host_attempts.items()):
        in_window = [a for a in host_attempts if (parsed := _parse_utc(a['finished'])) and parsed >= cutoff]
        if not in_window:
            continue
        in_window.sort(key=lambda a: (_parse_utc(a['finished']) or datetime.min.replace(tzinfo=timezone.utc)))
        streak = 0
        for attempt in reversed(in_window):
            success = (isinstance(attempt['http_status'], int)
                       and 200 <= attempt['http_status'] < 400 and not attempt['timed_out'])
            if success:
                break
            streak += 1
        rss_hosts[host] = {
            'attempts': len(in_window),
            'successes': sum(isinstance(a['http_status'], int) and 200 <= a['http_status'] < 400 and not a['timed_out'] for a in in_window),
            'timeouts': sum(a['timed_out'] for a in in_window),
            'http_429': sum(a['http_status'] == 429 for a in in_window),
            'failures': sum(not (isinstance(a['http_status'], int) and 200 <= a['http_status'] < 400 and not a['timed_out']) for a in in_window),
            'consecutive_failures': streak,
            'last_status': in_window[-1]['http_status'],
            'last_error': in_window[-1]['error_type'],
        }

    output = {}
    for source, item in sorted(stats.items()):
        latencies = sorted(item['latency_minutes'])
        p90 = latencies[max(0, math.ceil(0.9 * len(latencies)) - 1)] if latencies else None
        query_output = {}
        for label, query_item in sorted(item['queries'].items()):
            query_latencies = sorted(query_item['latency_minutes'])
            query_p90 = (query_latencies[max(0, math.ceil(0.9 * len(query_latencies)) - 1)]
                         if query_latencies else None)
            query_output[label] = {
                'type': query_item['type'], 'calls': query_item['calls'],
                'http_success': query_item['http_success'], 'http_429': query_item['http_429'],
                'timeouts': query_item['timeouts'], 'items_returned': query_item['items_returned'],
                'unique_ids_returned': len(query_item['unique_ids_returned']),
                'first_captures': len(query_item['first_captures']),
                'decisions': dict(query_item['decisions']),
                'rejections': dict(query_item['rejections'].most_common(10)),
                'rejection_records': sum(query_item['rejections'].values()),
                'unique_rejected_candidates': len(query_item['unique_rejected_ids']),
                'unique_rejections_by_reason': {
                    key: len(ids) for key, ids in sorted(query_item['unique_rejections_by_reason'].items())
                },
                'delivery': {
                    'attempts': sum(query_item['delivery_attempts'].values()),
                    'statuses': dict(query_item['delivery_statuses']),
                    'delivered_alerts': len(query_item['delivered_fingerprints']),
                    'attempts_by_channel': dict(query_item['delivery_attempts']),
                },
                'publication_to_capture_minutes': {
                    'samples': len(query_latencies),
                    'median': round(statistics.median(query_latencies), 1) if query_latencies else None,
                    'p90': round(query_p90, 1) if query_p90 is not None else None,
                    'unknown_publication_time': query_item['unknown_publication_time'],
                },
            }
        delivery_by_channel = {
            channel: {
                'attempts': value['attempts'],
                'statuses': dict(value['statuses']),
                'delivered_alerts': len(value['delivered_fingerprints']),
            }
            for channel, value in sorted(item['delivery_by_channel'].items())
        }
        output[source] = {
            'calls': item['calls'], 'http_success': item['http_success'],
            'http_429': item['http_429'], 'timeouts': item['timeouts'],
            'items_returned': item['items_returned'],
            'unique_ids_returned': len(item['unique_ids_returned']),
            'first_captures_in_window': len(item['first_captures']),
            'publication_to_capture_minutes': {
                'samples': len(latencies),
                'median': round(statistics.median(latencies), 1) if latencies else None,
                'p90': round(p90, 1) if p90 is not None else None,
                'unknown_publication_time': item['unknown_publication_time'],
            },
            'decisions': dict(item['decisions']),
            'rejections': dict(item['rejections'].most_common(10)),
            'rejection_records': sum(item['rejections'].values()),
            'unique_rejected_candidates': len(item['unique_rejected_ids']),
            'unique_rejections_by_reason': {
                key: len(ids) for key, ids in sorted(item['unique_rejections_by_reason'].items())
            },
            'delivery': {
                'attempts': sum(item['delivery_attempts'].values()),
                'statuses': dict(item['delivery_statuses']),
                'delivered_alerts': sum(
                    channel['delivered_alerts'] for channel in delivery_by_channel.values()
                ),
                'by_channel': delivery_by_channel,
            },
            'detail_enrichment': detail_metrics[source],
            'queries': query_output,
        }
    return {'since': cutoff.isoformat(), 'until': now.isoformat(), 'sources': output,
            'rss_hosts': rss_hosts}


def diagnose(store):
    """Classify manually found cases using durable collection and decision evidence."""
    with store.connect() as db:
        manual = db.execute('''SELECT url,normalized_url,source,native_id,identity_status,manual_found_at,raw_text
                               FROM manual_cases ORDER BY manual_found_at''').fetchall()
        jobs = db.execute('SELECT source,external_id,payload FROM jobs').fetchall()
        attempts = db.execute('''SELECT cycle_id,source,operation,query,started,finished,
            result_count,native_ids,error_type,http_status,timed_out,reason
            FROM collection_attempts ORDER BY id''').fetchall()
        queue_rows = db.execute('''SELECT c.url,c.source,c.native_id,q.status,q.result,q.error
            FROM manual_analysis_queue q JOIN manual_cases c ON c.id=q.manual_case_id
            ORDER BY q.id''').fetchall()
        rejection_rows = db.execute('''SELECT cycle_id,source,source_job_id,title,query,url,
            published_at,stage,reason,created_at FROM candidate_rejections ORDER BY id''').fetchall()
        decisions = [json.loads(row[0]) for row in db.execute(
            "SELECT payload FROM events WHERE kind='decision'").fetchall()]
        state_row = db.execute("SELECT value FROM metadata WHERE key='state'").fetchone()
        state = json.loads(state_row[0]) if state_row else {}
        analyses = state.get('recent_decisions', [])

    job_keys = {(str(source), str(external_id)) for source, external_id, _ in jobs}
    job_payloads = {}
    job_aliases = {}
    login_page_aliases = set()
    job_urls = set()
    for job_source, external_id, payload in jobs:
        try:
            data = json.loads(payload)
            canonical_key = (str(job_source), str(external_id))
            job_payloads[canonical_key] = data
            title = str(data.get('title', '')).casefold()
            description = str(data.get('description', '')).casefold()
            is_login_page = (str(job_source) == 'linkedin' and (
                'linkedin login' in title or 'sign in | linkedin' in title or
                'keep in touch with people you know' in description
            ))
            source_ids = {str(external_id)}
            for source_data in data.get('sources', {}).values():
                job_urls.add(extract_source_identity(source_data.get('url', '')).normalized_url)
            for source_name, source_data in data.get('sources', {}).items():
                source_ids.add(str(source_data.get('source_job_id', '')))
                for duplicate in data.get('analysis', {}).get('duplicate_listings', []):
                    if source_name == 'linkedin' and duplicate.get('source_job_id'):
                        source_ids.add(str(duplicate['source_job_id']))
            for source_id in source_ids - {''}:
                identity_key = (str(job_source), source_id)
                if is_login_page:
                    login_page_aliases.add(identity_key)
                else:
                    job_aliases.setdefault(identity_key, set()).add(canonical_key)
        except (TypeError, ValueError):
            continue
    attempt_keys = set()
    attempts_by_key = {}
    for (cycle_id, source, operation, query, started, finished, result_count,
         native_ids, error_type, http_status, timed_out, reason) in attempts:
        try:
            ids = [str(value) for value in json.loads(native_ids)]
            attempt_keys.update((str(source), value) for value in ids)
            try:
                query_data = json.loads(query) if query else None
            except (TypeError, ValueError):
                query_data = query
            attempt = {
                'cycle_id': cycle_id, 'operation': operation, 'query': query_data,
                'started': started, 'finished': finished, 'result_count': result_count,
                'http_status': http_status, 'error_type': error_type,
                'timed_out': bool(timed_out), 'reason': reason,
            }
            for native_id in ids:
                attempts_by_key.setdefault((str(source), native_id), []).append(attempt)
        except (TypeError, ValueError):
            pass

    rejections_by_key = {}
    for (cycle_id, source, source_job_id, title, query, rejected_url,
         published_at, stage, reason, created_at) in rejection_rows:
        try:
            query_data = json.loads(query) if query else None
        except (TypeError, ValueError):
            query_data = query
        rejections_by_key.setdefault((str(source), str(source_job_id)), []).append({
            'cycle_id': cycle_id, 'title': title, 'query': query_data,
            'url': rejected_url, 'published_at': published_at, 'stage': stage,
            'reason': reason, 'created_at': created_at,
        })

    queue_by_key = {}
    for url, source, native_id, status, result, error in queue_rows:
        queue_by_key[(str(source), str(native_id or ''), url)] = {
            'status': status, 'result': result, 'error': error,
        }

    output = []
    for url, normalized, source, native_id, identity_status, found_at, raw_text in manual:
        key = (str(source), str(native_id)) if native_id else None
        collected = (key in job_keys if key else normalized in job_urls) if (key or normalized) else False
        seen_by_collection = key in attempt_keys if key else normalized in job_urls
        matching_job_ids = {str(native_id), f'{source}:{native_id}'} if native_id else set()
        stale_login_payload = (str(source), str(native_id or '')) in login_page_aliases
        login_placeholder = not raw_text and stale_login_payload
        matching = [item for item in decisions if not stale_login_payload and (
            str(item.get('source')) == str(source) and
            (str(item.get('job_id')) in matching_job_ids if native_id else item.get('raw_url') == url)
        )]
        if not matching and native_id and not stale_login_payload:
            canonical_keys = job_aliases.get((str(source), str(native_id)), set())
            canonical_ids = {value for key_source, value in canonical_keys
                             if key_source == str(source)}
            canonical_ids.update(f'{source}:{value}' for value in tuple(canonical_ids))
            matching = [item for item in decisions if (
                str(item.get('source')) == str(source) and
                str(item.get('job_id')) in canonical_ids
            )]
        matching_analysis = [item for item in analyses if (
            str(item.get('source')) == str(source) and
            (str(item.get('job_id')) in matching_job_ids if native_id else item.get('raw_url') == url)
        )]
        queue = queue_by_key.get((str(source), str(native_id or ''), url), {})
        matching_attempts = attempts_by_key.get(key, []) if key else []
        matching_rejections = rejections_by_key.get(key, []) if key else []
        matching_job = job_payloads.get(key, {}) if key else {}
        if login_placeholder:
            classification = 'source_auth_required'
        elif stale_login_payload and raw_text:
            classification = 'analyzed_without_decision'
        elif matching:
            decision = matching[-1].get('decision', '')
            classification = 'approved_or_notified' if decision in {'DELIVERED', 'DELIVERY_FAILED'} else 'collected_discarded'
        elif queue.get('status') == 'FAILED':
            classification = 'analysis_failed'
        elif queue.get('status') == 'RUNNING':
            classification = 'analysis_running'
        elif queue.get('status') == 'DONE' and queue.get('result') == 'identity_unresolved':
            classification = 'identity_unresolved'
        elif queue.get('status') == 'DONE' and queue.get('result') == 'SOURCE_LOGIN_REQUIRED':
            classification = 'source_auth_required'
        elif queue.get('status') == 'DONE' and queue.get('result'):
            classification = 'analyzed_without_decision'
        elif matching_rejections:
            classification = 'source_filtered'
        elif collected:
            classification = 'collected_no_decision'
        elif seen_by_collection:
            classification = 'seen_in_collection_not_persisted'
        elif identity_status == 'identity_unresolved':
            classification = 'identity_unresolved'
        else:
            classification = 'not_collected'
        output.append({'url': url, 'source': source, 'native_id': native_id,
                       'identity_status': identity_status, 'manual_found_at': found_at,
                       'classification': classification,
                       'analysis_status': queue.get('status'),
                       'analysis_result': queue.get('result'),
                       'has_captured_text': bool(raw_text),
                       'analysis_error': queue.get('error'),
                       'collection_attempts': matching_attempts,
                       'source_rejections': matching_rejections,
                       'first_collected_at': min(
                           (attempt.get('started') for attempt in matching_attempts if attempt.get('started')),
                           default=None,
                       ),
                       'published_at': matching_job.get('published_at'),
                       'first_seen': matching_job.get('first_seen'),
                       'candidate_score': matching[-1].get('final_score') if matching else matching_job.get('match_score'),
                       'score_breakdown': matching_job.get('score_breakdown'),
                       'duplicate_listings': (matching_job.get('analysis') or {}).get('duplicate_listings', []),
                       'decision': matching[-1].get('decision') if matching else None,
                       'decision_reason': (matching[-1].get('decision_reason') if matching else
                                           matching_rejections[-1].get('reason') if matching_rejections else None),
                       'rejection_stage': matching_rejections[-1].get('stage') if matching_rejections else None,
                       'rejection_query': matching_rejections[-1].get('query') if matching_rejections else None,
                       'audit': matching_analysis[-1] if matching_analysis else None})
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(prog='workhunter')
    sub = parser.add_subparsers(dest='command', required=True)
    add = sub.add_parser('add', help='register a manually found job URL')
    add.add_argument('url')
    audit_cmd = sub.add_parser('audit-job', help='show retained evidence for one source vacancy')
    audit_cmd.add_argument('source')
    audit_cmd.add_argument('native_id')
    audit_cmd.add_argument('--json', action='store_true', help='print the full vacancy evidence trace as JSON')
    diagnose_cmd = sub.add_parser('diagnose', help='classify manually found URLs against collection evidence')
    diagnose_cmd.add_argument('--json', action='store_true', help='print the full vacancy evidence trace as JSON')
    source_audit_cmd = sub.add_parser('audit-sources', help='summarize collection performance by source')
    source_audit_cmd.add_argument('--days', type=int, default=7, help='lookback window in days (default: 7)')
    source_audit_cmd.add_argument('--json', action='store_true', help='print machine-readable source metrics')
    sub.add_parser('worker-once', help='run one isolated manual analysis cycle')
    sub.add_parser('health', help='show operational health checks')
    args = parser.parse_args(argv)
    if args.command == 'add':
        data_dir = Path(os.getenv('JOB_FINDER_DATA_DIR', Path.home() / '.job-finder'))
        SQLiteStore(data_dir / 'state.db').record_manual_case(args.url)
        print('manual case registered')
        return 0
    if args.command == 'diagnose':
        data_dir = Path(os.getenv('JOB_FINDER_DATA_DIR', Path.home() / '.job-finder'))
        result = diagnose(SQLiteStore(data_dir / 'state.db'))
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            return 0
        if not result:
            print('no manual cases registered')
        else:
            for item in result:
                details = ''
                if item.get('analysis_status'):
                    details = f" queue={item['analysis_status']} result={item.get('analysis_result') or '-'}"
                    if item.get('analysis_error'):
                        details += f" error={item['analysis_error']}"
                if item.get('decision'):
                    details += f" decision={item['decision']} reason={item.get('decision_reason') or '-'}"
                elif item.get('decision_reason'):
                    details += f" reason={item['decision_reason']}"
                if item.get('collection_attempts'):
                    attempt = item['collection_attempts'][0]
                    query = attempt.get('query')
                    details += f" query={query.get('keywords', query.get('term', query)) if isinstance(query, dict) else query}"
                    details += f" first_collected={item.get('first_collected_at') or '-'}"
                if item.get('published_at'):
                    details += f" published={item['published_at']}"
                if item.get('candidate_score') is not None:
                    details += f" score={item['candidate_score']}"
                if item.get('rejection_stage'):
                    details += f" filtered_at={item['rejection_stage']}"
                print(f"{item['classification']}: {item['source']}:{item['native_id'] or '-'}{details} {item['url']}")
        return 0
    if args.command == 'audit-job':
        data_dir = Path(os.getenv('JOB_FINDER_DATA_DIR', Path.home() / '.job-finder'))
        result = audit_job(SQLiteStore(data_dir / 'state.db'), args.source, args.native_id)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        else:
            print(f"{result['classification']}: {result['source']}:{result['native_id']} — {result.get('title') or 'sem registro local'}")
            print(f"Publicação: {result.get('published_at') or 'desconhecida'} | Primeira captura: {result.get('first_seen') or 'sem captura persistida'}")
            print(f"Consultas relacionadas: {len(result['search_attempts'])} | Rejeições: {len(result['source_rejections'])}")
            if result.get('evidence_gap'):
                print(f"Limite da auditoria: {result['evidence_gap']}")
            for query_info in result['queries']:
                query = query_info.get('query') or {}
                if isinstance(query, dict):
                    label = next((query.get(key) for key in ('keywords', 'term', 'query', 'url', 'repo')
                                  if query.get(key)), query)
                else:
                    label = query
                print(f"Consulta observada {query_info['observations']}x em "
                      f"{query_info['distinct_cycles']} ciclo(s): {label}")
            for attempt in result['search_attempts']:
                query = attempt.get('query') or {}
                if isinstance(query, dict):
                    label = next((query.get(key) for key in ('keywords', 'term', 'query', 'url', 'repo')
                                  if query.get(key)), query)
                else:
                    label = query
                label = str(label)
                if len(label) > 160:
                    label = label[:157] + '...'
                print(f"Busca: {attempt.get('finished') or attempt.get('started') or '?'} — {label} "
                      f"(HTTP {attempt.get('http_status') or 'N/D'}, {attempt.get('result_count', 0)} resultados)")
            for rejection in result['source_rejections']:
                print(f"Filtro: {rejection['stage']} — {rejection['reason']}")
            for skip in result['pipeline_skips']:
                print(f"Pipeline: {skip.get('stage') or 'skip'} — {skip.get('reason') or 'sem motivo'}")
            if result.get('decision'):
                decision = result['decision']
                score = f" | score {decision['score']}/100" if decision.get('score') is not None else ''
                print(f"Decisão: {decision['decision']}{score} — {decision['reason'] or 'sem motivo'}")
                if decision.get('score_breakdown'):
                    print(f"Componentes do score: {decision['score_breakdown']}")
                for reason in decision.get('match_reasons', []):
                    print(f"  • {reason}")
            print(f"Outbox: {len(result['outbox'])} | Tentativas por canal: {len(result['channel_attempts'])}")
            for delivery in result['delivery_history']:
                print(f"Entrega: {delivery.get('channel')} — {delivery.get('status')} em "
                      f"{delivery.get('timestamp') or delivery.get('finished_at') or 'horário indisponível'}")
        return 0
    if args.command == 'audit-sources':
        data_dir = Path(os.getenv('JOB_FINDER_DATA_DIR', Path.home() / '.job-finder'))
        result = audit_sources(SQLiteStore(data_dir / 'state.db'), args.days)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            return 0
        print(f"Métricas por fonte desde {result['since']}")
        for source, item in result['sources'].items():
            timing = item['publication_to_capture_minutes']
            median = timing['median']
            median_text = f"{median} min" if median is not None else 'N/D'
            print(
                f"{source}: {item['first_captures_in_window']} novas capturas; "
                f"{item['unique_ids_returned']} IDs únicos vistos; "
                f"{item['delivery']['delivered_alerts']} alertas entregues; "
                f"{item['http_429']} HTTP 429; {item['timeouts']} timeouts; "
                f"latência mediana {median_text} ({timing['samples']} com data verificável)"
            )
            detail = item.get('detail_enrichment', {})
            if detail.get('attempts'):
                print(f"  enriquecimento: {detail['successes']}/{detail['attempts']} úteis; "
                      f"{detail['empty_results']} sem dados, {detail['http_429']} HTTP 429, "
                      f"{detail['timeouts']} timeouts; datas Gupy recuperadas/ausentes: "
                      f"{detail['publication_dates_found']}/{detail['publication_dates_missing']}")
            if item['decisions']:
                print(f"  decisões: {item['decisions']}")
            if item['rejections']:
                print(
                    f"  descartes: {item['unique_rejected_candidates']} IDs únicos / "
                    f"{item['rejection_records']} registros; motivos: {item['unique_rejections_by_reason']}"
                )
            if source == 'rss' and item.get('queries'):
                modes = defaultdict(lambda: {'queries': 0, 'query_first_captures': 0, 'delivered_alerts': 0})
                for query_stats in item['queries'].values():
                    mode = query_stats.get('type', 'unclassified')
                    modes[mode]['queries'] += 1
                    modes[mode]['query_first_captures'] += query_stats.get('first_captures', 0)
                    modes[mode]['delivered_alerts'] += query_stats.get('delivery', {}).get('delivered_alerts', 0)
                for mode, stats in sorted(modes.items()):
                    print(f"  RSS {mode}: {stats['queries']} consultas; "
                          f"{stats['query_first_captures']} primeiras capturas por consulta; "
                          f"{stats['delivered_alerts']} alertas entregues")
        for host, item in result.get('rss_hosts', {}).items():
            print(f"RSS host {host}: {item['successes']}/{item['attempts']} respostas úteis; "
                  f"{item['timeouts']} timeouts, {item['http_429']} HTTP 429, "
                  f"{item['consecutive_failures']} falhas consecutivas")
        return 0
    if args.command == 'worker-once':
        from workhunter_worker import run as run_worker
        return run_worker()
    if args.command == 'health':
        from core.health import health_snapshot
        data_dir = Path(os.getenv('JOB_FINDER_DATA_DIR', Path.home() / '.job-finder'))
        print(json.dumps(health_snapshot(SQLiteStore(data_dir / 'state.db'), data_dir), ensure_ascii=False, indent=2))
        return 0
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
