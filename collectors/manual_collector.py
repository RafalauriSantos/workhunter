import re
import hashlib
import html as html_lib
import json
from datetime import datetime, timezone
from urllib.parse import urlparse

from collectors.base import BaseCollector
from models.job import Job
from core.normalizer import normalize_title, normalize_workplace, extract_technologies, has_explicit_remote_signal
from collectors.linkedin_collector import LinkedInCollector


class ManualCollector(BaseCollector):
    """Fetches URLs sent by the owner so they use the normal decision pipeline."""
    def __init__(self, http_session, store):
        self.http = http_session
        self.store = store
        self.collection_attempts = []

    def collect(self):
        jobs = []
        with self.store.connect() as db:
            cases = db.execute("""SELECT q.id,c.normalized_url,c.source,c.native_id,c.raw_text,c.author
                FROM manual_analysis_queue q JOIN manual_cases c ON c.id=q.manual_case_id
                WHERE q.status='PENDING' ORDER BY q.created_at""").fetchall()
        for queue_id, url, source, native_id, raw_text, author in cases:
            with self.store.connect() as db:
                db.execute("UPDATE manual_analysis_queue SET status='RUNNING',started_at=?,attempts=attempts+1 WHERE id=?",
                           (datetime.now(timezone.utc).isoformat(), queue_id))
            if source not in {'linkedin', 'gupy'} or (not native_id and not raw_text):
                with self.store.connect() as db:
                    db.execute("UPDATE manual_analysis_queue SET status='DONE',finished_at=?,result=? WHERE id=?",
                               (datetime.now(timezone.utc).isoformat(), 'identity_unresolved', queue_id))
                continue
            started = datetime.now(timezone.utc).isoformat()
            try:
                response = None if raw_text else self.http.get(url, timeout=15, headers={'User-Agent': 'Mozilla/5.0'})
                response_status = 200 if raw_text else response.status_code
                if response_status != 200:
                    with self.store.connect() as db:
                        db.execute("UPDATE manual_analysis_queue SET status='FAILED',finished_at=?,error=? WHERE id=?",
                                   (datetime.now(timezone.utc).isoformat(), f'HTTP_{response_status}', queue_id))
                    self.record_attempt('manual', 'fetch', started, query={'url': url},
                                        http_status=response_status, error_type=f'HTTP_{response_status}')
                    continue
                html = '' if raw_text else response.text
                final_url = getattr(response, 'url', '') or url
                final_path = urlparse(final_url).path.rstrip('/').lower()
                title_match = re.search(r'<title[^>]*>\s*(.*?)\s*</title>', html, re.I | re.S)
                title = normalize_title(re.sub(r'<[^>]+>', ' ', title_match.group(1))) if title_match else ''
                title = re.sub(r'^Página da Vaga\s*\|\s*', '', title, flags=re.I).strip()
                normalized_title = title.casefold()
                login_page = (source == 'linkedin' and not raw_text and (
                    final_path in {'/uas/login', '/login', '/authwall', '/checkpoint/lg/login-submit'} or
                    'linkedin login' in normalized_title or 'sign in | linkedin' in normalized_title
                ))
                if login_page:
                    with self.store.connect() as db:
                        db.execute("UPDATE manual_analysis_queue SET status='DONE',finished_at=?,result=? WHERE id=?",
                                   (datetime.now(timezone.utc).isoformat(), 'SOURCE_LOGIN_REQUIRED', queue_id))
                    self.record_attempt('manual', 'fetch', started, query={'url': url},
                                        http_status=response_status, result_count=0,
                                        error_type='SOURCE_LOGIN_REQUIRED', reason='LinkedIn redirected to sign-in')
                    continue
                if not title:
                    title = 'Vaga do LinkedIn'
                if raw_text:
                    lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
                    title = next((line for line in lines if re.search(
                        r'\b(desenvolvedor(?:a)?|developer|engenheiro(?:a)? de software|software engineer)\b', line, re.I)
                        and len(line) <= 180), 'Oportunidade LinkedIn')
                company_match = re.search(r'"hiringOrganization"\s*:\s*\{.*?"name"\s*:\s*"([^"]+)', html, re.I | re.S)
                company = company_match.group(1) if company_match else (author or 'LinkedIn')
                location_match = re.search(r'"jobLocation".*?"addressLocality"\s*:\s*"([^"]+)', html, re.I | re.S)
                location = location_match.group(1) if location_match else 'Brasil'
                description = raw_text
                if source == 'gupy' and html:
                    match = re.search(r'"description":"(.*?)","responsibilities"', html, re.S)
                    if match:
                        try:
                            description = html_lib.unescape(json.loads('"' + match.group(1) + '"'))
                        except (ValueError, json.JSONDecodeError):
                            description = html_lib.unescape(match.group(1))
                        description = re.sub(r'<[^>]+>', ' ', description)
                        description = re.sub(r'\s+', ' ', description).strip()
                source_id = native_id or hashlib.sha256(raw_text.encode()).hexdigest()[:20]
                workplace = ('remote' if description and has_explicit_remote_signal(description)
                             else normalize_workplace('', location, title))
                job = Job(title=title, company=company, workplace_type=workplace,
                          location=location, description=description, raw_url=url, technologies=extract_technologies(description or title))
                job.add_source(source, source_id, url)
                # Reuse the normal public LinkedIn enrichment path. It extracts
                # the public description/meta description and updates evidence.
                enriched = False if raw_text or source != 'linkedin' else LinkedInCollector(self.http, [])._enrich_job(
                    job, {'User-Agent': 'Mozilla/5.0', 'Accept-Language': 'pt-BR,pt;q=0.9,en;q=0.8'})
                if enriched:
                    job.workplace_type = ('remote' if has_explicit_remote_signal(job.description)
                                          else normalize_workplace('', location_text=location,
                                                                   title_text=f'{title} {job.description}'))
                evidence_status = 'FULL_EVIDENCE' if enriched or raw_text or (source == 'gupy' and len(description) > 80) else (
                    'PARTIAL_EVIDENCE' if title and company != 'LinkedIn' else 'NO_DESCRIPTION')
                jobs.append(job)
                with self.store.connect() as db:
                    db.execute("UPDATE manual_analysis_queue SET status='DONE',finished_at=?,result=? WHERE id=?",
                               (datetime.now(timezone.utc).isoformat(), evidence_status, queue_id))
                self.record_attempt('manual', 'fetch', started, query={'url': url}, http_status=response_status,
                                    result_count=1, native_ids=[source_id])
            except Exception as exc:
                with self.store.connect() as db:
                    db.execute("UPDATE manual_analysis_queue SET status='FAILED',finished_at=?,error=? WHERE id=?",
                               (datetime.now(timezone.utc).isoformat(), str(exc), queue_id))
                self.record_attempt('manual', 'fetch', started, query={'url': url}, error_type=type(exc).__name__,
                                    timed_out='timeout' in type(exc).__name__.lower(), reason=str(exc))
        return jobs
