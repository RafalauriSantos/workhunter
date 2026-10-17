"""Small operator CLI for WorkHunter investigation data."""
import argparse
import json
import os
import sqlite3
from pathlib import Path

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
        attempt_rows = db.execute('''SELECT cycle_id,operation,query,started,finished,
            result_count,native_ids,http_status,error_type,timed_out,reason
            FROM collection_attempts WHERE source=? AND operation='search' ORDER BY id''', (source,)).fetchall()
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

    source_info = (payload or {}).get('sources', {}).get(source, {})
    return {
        'source': source, 'native_id': native_id, 'classification': classification,
        'title': (payload or {}).get('title') or (rejections[-1]['title'] if rejections else None),
        'published_at': (payload or {}).get('published_at') or (rejections[-1]['published_at'] if rejections else None),
        'first_seen': source_info.get('first_seen') or (payload or {}).get('first_seen'),
        'search_attempts': search_attempts, 'source_rejections': rejections,
        'pipeline_skips': pipeline_skips,
        'legacy_seen_state': legacy_seen_state,
        'stored_job': payload,
        'decision': decision_summary,
        'outbox': matched_outbox, 'channel_attempts': channels,
        'delivery_history': delivery_history,
        'deduplication': {
            'source_count': len((payload or {}).get('sources') or {}),
            'duplicate_listings': ((payload or {}).get('analysis') or {}).get('duplicate_listings', []),
        },
    }


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
            if result.get('decision'):
                print(f"Decisão: {result['decision']['decision']} — {result['decision']['reason'] or 'sem motivo'}")
            print(f"Outbox: {len(result['outbox'])} | Tentativas por canal: {len(result['channel_attempts'])}")
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
