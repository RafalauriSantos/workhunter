"""Small operator CLI for WorkHunter investigation data."""
import argparse
import json
import os
from pathlib import Path

from storage.sqlite_store import SQLiteStore
from core.source_identity import extract_source_identity


def diagnose(store):
    """Classify manually found cases using durable collection and decision evidence."""
    with store.connect() as db:
        manual = db.execute('''SELECT url,normalized_url,source,native_id,identity_status,manual_found_at,raw_text
                               FROM manual_cases ORDER BY manual_found_at''').fetchall()
        jobs = db.execute('SELECT source,external_id,payload FROM jobs').fetchall()
        attempts = db.execute('SELECT source,native_ids,error_type,http_status,timed_out FROM collection_attempts').fetchall()
        queue_rows = db.execute('''SELECT c.url,c.source,c.native_id,q.status,q.result,q.error
            FROM manual_analysis_queue q JOIN manual_cases c ON c.id=q.manual_case_id
            ORDER BY q.id''').fetchall()
        decisions = [json.loads(row[0]) for row in db.execute(
            "SELECT payload FROM events WHERE kind='decision'").fetchall()]
        state_row = db.execute("SELECT value FROM metadata WHERE key='state'").fetchone()
        state = json.loads(state_row[0]) if state_row else {}
        analyses = state.get('recent_decisions', [])

    job_keys = {(str(source), str(external_id)) for source, external_id, _ in jobs}
    job_aliases = {}
    login_page_aliases = set()
    job_urls = set()
    for job_source, external_id, payload in jobs:
        try:
            data = json.loads(payload)
            canonical_key = (str(job_source), str(external_id))
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
    for source, native_ids, *_ in attempts:
        try:
            attempt_keys.update((str(source), str(value)) for value in json.loads(native_ids))
        except (TypeError, ValueError):
            pass

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
                       'decision': matching[-1].get('decision') if matching else None,
                       'decision_reason': matching[-1].get('decision_reason') if matching else None,
                       'audit': matching_analysis[-1] if matching_analysis else None})
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(prog='workhunter')
    sub = parser.add_subparsers(dest='command', required=True)
    add = sub.add_parser('add', help='register a manually found job URL')
    add.add_argument('url')
    sub.add_parser('diagnose', help='classify manually found URLs against collection evidence')
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
                print(f"{item['classification']}: {item['source']}:{item['native_id'] or '-'}{details} {item['url']}")
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
