"""Transactional compatibility adapter while the Python pipeline is migrated."""
import hashlib
import json
import sqlite3
import time
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from storage.state_store import StateStore


class SQLiteStore(StateStore):
    def __init__(self, filepath):
        self.filepath = str(filepath)
        Path(filepath).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise sqlite3.DatabaseError('State integrity check failed')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS claims(fingerprint TEXT PRIMARY KEY, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS cycles(id INTEGER PRIMARY KEY, started TEXT NOT NULL,
                    finished TEXT, status TEXT NOT NULL, revision TEXT, report TEXT, mode TEXT);
                CREATE TABLE IF NOT EXISTS channel_attempts(id INTEGER PRIMARY KEY,
                    fingerprint TEXT NOT NULL, channel TEXT NOT NULL, status TEXT NOT NULL,
                    created TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS outbox(fingerprint TEXT PRIMARY KEY, payload TEXT NOT NULL,
                    status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, retry_at REAL NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS jobs(source TEXT NOT NULL, external_id TEXT NOT NULL,
                    payload TEXT NOT NULL, PRIMARY KEY(source,external_id));
                CREATE TABLE IF NOT EXISTS collection_attempts(
                    id INTEGER PRIMARY KEY, cycle_id TEXT NOT NULL, source TEXT NOT NULL,
                    operation TEXT NOT NULL, query TEXT, query_hash TEXT, page INTEGER,
                    cursor TEXT, started TEXT NOT NULL, finished TEXT NOT NULL,
                    duration_ms INTEGER NOT NULL DEFAULT 0,
                    http_status INTEGER, result_count INTEGER NOT NULL DEFAULT 0,
                    native_ids TEXT NOT NULL DEFAULT '[]', error_type TEXT,
                    timed_out INTEGER NOT NULL DEFAULT 0, retry_count INTEGER NOT NULL DEFAULT 0,
                    reason TEXT);
                CREATE TABLE IF NOT EXISTS candidate_rejections(
                    id INTEGER PRIMARY KEY, cycle_id TEXT NOT NULL, source TEXT NOT NULL,
                    source_job_id TEXT NOT NULL, title TEXT NOT NULL, query TEXT,
                    url TEXT NOT NULL DEFAULT '', published_at TEXT NOT NULL DEFAULT '',
                    stage TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS idx_candidate_rejections_source_job
                    ON candidate_rejections(source,source_job_id,created_at);
                CREATE INDEX IF NOT EXISTS idx_candidate_rejections_created_at
                    ON candidate_rejections(created_at);
                CREATE TABLE IF NOT EXISTS manual_cases(
                    id INTEGER PRIMARY KEY, url TEXT NOT NULL, normalized_url TEXT NOT NULL,
                    source TEXT NOT NULL, native_id TEXT, identity_status TEXT NOT NULL,
                    manual_found_at TEXT NOT NULL, raw_text TEXT NOT NULL DEFAULT '',
                    author TEXT NOT NULL DEFAULT '', UNIQUE(source, native_id, normalized_url));
                CREATE TABLE IF NOT EXISTS manual_analysis_queue(
                    id INTEGER PRIMARY KEY, manual_case_id INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'PENDING', attempts INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT,
                    result TEXT, error TEXT, telegram_chat_id TEXT, telegram_message_id INTEGER,
                    notified_at TEXT);
            ''')
            db.execute("INSERT OR IGNORE INTO metadata VALUES ('state', ?)",
                       (json.dumps({'seen_ids': [], 'seen_fingerprints': [], 'last_heartbeat': ''}),))
            columns = {row[1] for row in db.execute('PRAGMA table_info(collection_attempts)')}
            if 'duration_ms' not in columns:
                db.execute('ALTER TABLE collection_attempts ADD COLUMN duration_ms INTEGER NOT NULL DEFAULT 0')
            cycle_columns = {row[1] for row in db.execute('PRAGMA table_info(cycles)')}
            if 'report' not in cycle_columns:
                db.execute('ALTER TABLE cycles ADD COLUMN report TEXT')
            if 'mode' not in cycle_columns:
                db.execute('ALTER TABLE cycles ADD COLUMN mode TEXT')
            queue_columns = {row[1] for row in db.execute('PRAGMA table_info(manual_analysis_queue)')}
            for column, definition in (('telegram_chat_id', 'TEXT'), ('telegram_message_id', 'INTEGER'), ('notified_at', 'TEXT')):
                if column not in queue_columns:
                    db.execute(f'ALTER TABLE manual_analysis_queue ADD COLUMN {column} {definition}')
            case_columns = {row[1] for row in db.execute('PRAGMA table_info(manual_cases)')}
            for column, definition in (('raw_text', "TEXT NOT NULL DEFAULT ''"), ('author', "TEXT NOT NULL DEFAULT ''")):
                if column not in case_columns:
                    db.execute(f'ALTER TABLE manual_cases ADD COLUMN {column} {definition}')
        self.state = self._load()

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.filepath, timeout=10)
        try:
            db.execute('PRAGMA synchronous=FULL')
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _load(self):
        with self.connect() as db:
            self._snapshot = db.execute("SELECT value FROM metadata WHERE key='state'").fetchone()[0]
        return json.loads(self._snapshot)

    def _persist(self, db):
        value = json.dumps(self.state, ensure_ascii=False)
        changed = db.execute("UPDATE metadata SET value=? WHERE key='state' AND value=?",
                             (value, self._snapshot)).rowcount
        if not changed:
            raise RuntimeError('State changed concurrently; refusing to overwrite')
        return value

    def save(self):
        with self.connect() as db:
            value = self._persist(db)
        self._snapshot = value

    def claim_delivery(self, fingerprint, now=None, lease_seconds=600):
        now = time.time() if now is None else now
        with self.connect() as db:
            db.execute('DELETE FROM claims WHERE expires <= ?', (now,))
            if fingerprint in self.state.get('seen_fingerprints', []):
                return False
            claimed = db.execute('INSERT OR IGNORE INTO claims VALUES (?, ?)',
                                 (fingerprint, now + lease_seconds)).rowcount
            if not claimed:
                return False
            value = self._persist(db)
        self._snapshot = value
        return True

    def release_delivery_claim(self, fingerprint):
        with self.connect() as db:
            value = self._persist(db)
            db.execute('DELETE FROM claims WHERE fingerprint=?', (fingerprint,))
        self._snapshot = value

    def event(self, kind, payload):
        with self.connect() as db:
            db.execute('INSERT INTO events(kind,payload) VALUES (?,?)',
                       (kind, json.dumps(payload, ensure_ascii=False)))

    def record_decision(self, *args, **kwargs):
        super().record_decision(*args, **kwargs)
        self.event('decision', self.state['recent_decisions'][-1])
        self.save()

    def record_source_health(self, *args, **kwargs):
        super().record_source_health(*args, **kwargs)
        self.event('source_health', self.state['source_health'][-1])
        self.save()

    def record_llm_call(self):
        result = super().record_llm_call()
        self.save()
        return result

    def record_delivery(self, fingerprint, source_ids, delivered):
        super().record_delivery(fingerprint, source_ids, delivered)
        self.release_delivery_claim(fingerprint)

    def record_channel(self, fingerprint, channel, status):
        with self.connect() as db:
            db.execute('INSERT INTO channel_attempts(fingerprint,channel,status,created) VALUES (?,?,?,?)',
                       (fingerprint, channel, status, datetime.now(timezone.utc).isoformat()))

    def record_collection_attempt(self, attempt):
        with self.connect() as db:
            db.execute('''INSERT INTO collection_attempts
                (cycle_id,source,operation,query,query_hash,page,cursor,started,finished,duration_ms,
                 http_status,result_count,native_ids,error_type,timed_out,retry_count,reason)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', (
                attempt.get('cycle_id', ''), attempt.get('source', ''), attempt.get('operation', ''),
                json.dumps(attempt.get('query'), ensure_ascii=False, sort_keys=True, default=str)
                if attempt.get('query') is not None else None,
                attempt.get('query_hash'), attempt.get('page'), attempt.get('cursor'),
                attempt.get('started', datetime.now(timezone.utc).isoformat()),
                attempt.get('finished', datetime.now(timezone.utc).isoformat()),
                int(attempt.get('duration_ms', 0)), attempt.get('http_status'),
                int(attempt.get('result_count', 0)), json.dumps(attempt.get('native_ids', [])),
                attempt.get('error_type'), int(bool(attempt.get('timed_out'))), int(attempt.get('retry_count', 0)),
                attempt.get('reason')))

    def get_last_collection_attempt(self, source, operation=None):
        """Return the latest persisted attempt for a source, decoded for planning."""
        sql = '''SELECT cycle_id,operation,query,started,finished,http_status,result_count
                 FROM collection_attempts WHERE source=?'''
        params = [str(source)]
        if operation:
            sql += ' AND operation=?'
            params.append(str(operation))
        sql += ' ORDER BY id DESC LIMIT 1'
        with self.connect() as db:
            row = db.execute(sql, params).fetchone()
        if not row:
            return None
        try:
            query = json.loads(row[2]) if row[2] else None
        except (TypeError, ValueError):
            query = row[2]
        return {
            'cycle_id': row[0], 'operation': row[1], 'query': query,
            'started': row[3], 'finished': row[4], 'http_status': row[5],
            'result_count': row[6],
        }

    def record_candidate_rejection(self, rejection):
        self.record_candidate_rejections([rejection])

    def record_candidate_rejections(self, rejections):
        if not rejections:
            return
        now = datetime.now(timezone.utc)
        rows = []
        for rejection in rejections:
            rows.append((
                rejection.get('cycle_id', ''), rejection.get('source', ''),
                rejection.get('source_job_id', ''), rejection.get('title', ''),
                json.dumps(rejection.get('query'), ensure_ascii=False, sort_keys=True, default=str)
                if rejection.get('query') is not None else None,
                rejection.get('url', ''), rejection.get('published_at', ''),
                rejection.get('stage', ''), rejection.get('reason', ''),
                rejection.get('rejected_at') or now.isoformat(),
            ))
        cutoff = (now - timedelta(days=90)).isoformat()
        with self.connect() as db:
            db.executemany('''INSERT INTO candidate_rejections
                (cycle_id,source,source_job_id,title,query,url,published_at,stage,reason,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)''', rows)
            db.execute('DELETE FROM candidate_rejections WHERE created_at < ?', (cutoff,))

    def record_manual_case(self, url, source=None, native_id=None, manual_found_at=None, raw_text='', author=''):
        from core.source_identity import extract_source_identity
        identity = extract_source_identity(url)
        with self.connect() as db:
            db.execute('''INSERT INTO manual_cases
                (url,normalized_url,source,native_id,identity_status,manual_found_at,raw_text,author)
                VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(source,native_id,normalized_url) DO UPDATE SET
                url=excluded.url, manual_found_at=excluded.manual_found_at,
                raw_text=excluded.raw_text, author=excluded.author''', (
                url, identity.normalized_url, source or identity.source, native_id or identity.native_id,
                identity.identity_status, manual_found_at or datetime.now(timezone.utc).isoformat(), raw_text, author))
            case = db.execute('SELECT id FROM manual_cases WHERE source=? AND native_id IS ? AND normalized_url=?',
                              (source or identity.source, native_id or identity.native_id, identity.normalized_url)).fetchone()
            queue = db.execute('INSERT INTO manual_analysis_queue(manual_case_id,created_at) VALUES (?,?)',
                       (case[0], datetime.now(timezone.utc).isoformat()))
            return queue.lastrowid

    def remember_job(self, job):
        from dataclasses import asdict
        payload = json.dumps(asdict(job), ensure_ascii=False)
        with self.connect() as db:
            payload_data = json.loads(payload)
            existing_payloads = []
            for source, item in job.sources.items():
                row = db.execute(
                    'SELECT payload FROM jobs WHERE source=? AND external_id=?',
                    (source, item.source_job_id),
                ).fetchone()
                if row:
                    try:
                        existing_payloads.append(json.loads(row[0]))
                    except (TypeError, ValueError):
                        pass

            def parse_seen(value):
                try:
                    parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
                    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
                except (TypeError, ValueError):
                    return None

            seen_values = [(parse_seen(payload_data.get('first_seen')), payload_data.get('first_seen'))]
            seen_values.extend(
                (parse_seen(value.get('first_seen')), value.get('first_seen'))
                for value in existing_payloads if value.get('first_seen')
            )
            seen_values = [(instant, value) for instant, value in seen_values if instant is not None]
            if seen_values:
                payload_data['first_seen'] = min(seen_values, key=lambda item: item[0])[1]

            for source, item in job.sources.items():
                previous = next((value for value in existing_payloads
                                 if source in (value.get('sources') or {})
                                 and str((value['sources'][source] or {}).get('source_job_id')) == str(item.source_job_id)), None)
                if previous:
                    previous_source = previous['sources'][source]
                    prior_seen = previous_source.get('first_seen')
                    new_seen = (payload_data.get('sources') or {}).get(source, {}).get('first_seen')
                    if parse_seen(prior_seen) and parse_seen(new_seen) and parse_seen(prior_seen) < parse_seen(new_seen):
                        payload_data['sources'][source]['first_seen'] = prior_seen
            payload = json.dumps(payload_data, ensure_ascii=False)
            for source, item in job.sources.items():
                db.execute('INSERT INTO jobs VALUES (?,?,?) ON CONFLICT(source,external_id) '
                           'DO UPDATE SET payload=excluded.payload', (source, item.source_job_id, payload))

    def enqueue(self, job):
        from dataclasses import asdict
        from core.deduplicator import delivery_fingerprint
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO outbox(fingerprint,payload,status) VALUES (?,?,'PENDING')",
                       (delivery_fingerprint(job), json.dumps(asdict(job), ensure_ascii=False)))

    def pending_jobs(self):
        from models.job import Job, JobSource
        with self.connect() as db:
            rows = db.execute("SELECT payload FROM outbox WHERE status IN ('PENDING','FAILED') "
                              'AND attempts < 3 AND retry_at <= ?', (time.time(),)).fetchall()
        jobs = []
        for row in rows:
            data = json.loads(row[0])
            data['sources'] = {key: JobSource(**value) for key, value in data['sources'].items()}
            jobs.append(Job(**data))
        return jobs

    def import_json(self, source):
        raw = Path(source).read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        data = json.loads(raw)
        if isinstance(data, list):
            data = {'seen_ids': data}
        if not isinstance(data, dict):
            raise ValueError('Unsupported legacy state')
        for key in ('seen_ids', 'seen_fingerprints'):
            if key in data and not isinstance(data[key], list):
                raise ValueError(f'Invalid legacy field: {key}')
        with self.connect() as db:
            imported = db.execute("SELECT value FROM metadata WHERE key='legacy_import'").fetchone()
            if imported:
                if imported[0] != digest:
                    raise ValueError('Different legacy state already imported')
                return {'imported': False}
            if self.state.get('seen_fingerprints') or self.state.get('deliveries'):
                raise ValueError('Migration requires an empty destination')
            backup = Path(self.filepath).with_suffix('.legacy.json')
            if backup.exists():
                if backup.read_bytes() != raw:
                    raise ValueError('Legacy backup differs; refusing to replace it')
            else:
                with backup.open('xb') as handle:
                    handle.write(raw)
            data['legacy_seen_ids'] = list(dict.fromkeys(str(x) for x in data.pop('seen_ids', [])))
            data['seen_ids'] = []
            data.pop('delivery_claims', None)
            self.state.update(data)
            value = self._persist(db)
            db.execute("INSERT INTO metadata VALUES ('legacy_import', ?)", (digest,))
        self._snapshot = value
        return {'imported': True, 'legacy_ids': len(data['legacy_seen_ids']),
                'fingerprints': len(data.get('seen_fingerprints', []))}

    def backup(self, directory):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"state-{datetime.now(timezone.utc):%Y-%m-%d}.db"
        if not target.exists():
            temporary = target.with_suffix('.tmp')
            with closing(sqlite3.connect(self.filepath)) as source, closing(sqlite3.connect(temporary)) as dest:
                source.backup(dest)
                if dest.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise sqlite3.DatabaseError('Backup integrity check failed')
            temporary.replace(target)
        with closing(sqlite3.connect(target)) as restored:
            if restored.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise sqlite3.DatabaseError('Existing backup integrity check failed')
        for old in sorted(directory.glob('state-*.db'), reverse=True)[7:]:
            old.unlink()
        return target
