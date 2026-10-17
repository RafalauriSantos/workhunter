import json
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any
import requests
from datetime import datetime, timezone
from collectors.base import BaseCollector
from models.job import Job
from core.normalizer import normalize_title, normalize_workplace, extract_technologies

GUPY_MCP_URL = "https://candidates.mcp.api.gupy.io/mcp"

JOB_TYPE_TRANSLATIONS = {
    "vacancy_type_effective": "Efetivo (CLT)",
    "vacancy_type_internship": "Estágio",
    "vacancy_type_trainee": "Trainee",
    "vacancy_type_apprentice": "Jovem Aprendiz",
    "vacancy_legal_entity": "Pessoa Jurídica (PJ)",
    "vacancy_type_temporary": "Temporário",
    "vacancy_type_freelancer": "Freelancer",
    "vacancy_type_outsource": "Terceirizado",
    "vacancy_type_talent_pool": "Banco de Talentos",
}


class GupyCollector(BaseCollector):
    DETAIL_CACHE_TTL = timedelta(days=7)
    DETAIL_CACHE_MAX_ENTRIES = 500

    def __init__(self, http_session: requests.Session, queries: List[Dict[str, Any]], detail_limit: int = 5, max_age_hours: int = 0, require_publication_date: bool = False, detail_cache: Dict[str, Any] = None):
        self.http = http_session
        self.queries = queries
        self.detail_limit = max(0, detail_limit)
        self.max_age_hours = max(0, int(max_age_hours or 0))
        self.require_publication_date = bool(require_publication_date)
        self.detail_attempts = 0
        self._last_detail_success = False
        self.query_stats: List[Dict[str, Any]] = []
        self._last_query_status = "UNKNOWN"
        self.collection_attempts = []
        self.detail_cache = detail_cache if isinstance(detail_cache, dict) else {}

    def _query_api(self, args: Dict[str, Any]) -> List[Dict[str, Any]]:
        started = datetime.now(timezone.utc).isoformat()
        api_args = {key: value for key, value in args.items() if key != "query_id"}
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "search_jobs",
                "arguments": api_args,
            },
        }
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        try:
            resp = self.http.post(GUPY_MCP_URL, json=body, headers=headers, timeout=15)
            if resp.status_code != 200:
                self._last_query_status = f"HTTP_{resp.status_code}"
                self.record_attempt('gupy', 'search', started, query=args, http_status=resp.status_code,
                                    error_type=self._last_query_status, reason='HTTP response')
                return []

            resp.encoding = "utf-8"
            for line in resp.text.splitlines():
                if line.startswith("data:"):
                    payload = json.loads(line[5:].strip())
                    content_text = payload.get("result", {}).get("content", [{}])[0].get("text", "{}")
                    parsed = json.loads(content_text)
                    jobs = parsed.get("data", {}).get("data")
                    if not isinstance(jobs, list):
                        self._last_query_status = 'INVALID_RESPONSE'
                        return []
                    self._last_query_status = "OK"
                    self.record_attempt('gupy', 'search', started, query=args, http_status=resp.status_code,
                                        result_count=len(jobs), native_ids=[str(item.get('id')) for item in jobs])
                    return jobs
        except Exception as e:
            self._last_query_status = "ERROR"
            self.record_attempt('gupy', 'search', started, query=args,
                                error_type=type(e).__name__, timed_out=isinstance(e, requests.Timeout), reason=str(e))
            print(f"[ERRO GupyCollector] {e}")
            return []
        self._last_query_status = "INVALID_RESPONSE"
        self.record_attempt('gupy', 'search', started, query=args, error_type='INVALID_RESPONSE')
        return []

    def _get_detail(self, job_id: str) -> Dict[str, Any]:
        self._last_detail_success = False
        if not job_id or self.detail_attempts >= self.detail_limit:
            return {}
        try:
            numeric_job_id = int(job_id)
        except (TypeError, ValueError):
            self.record_attempt('gupy', 'detail', datetime.now(timezone.utc).isoformat(),
                                query={'job_id': str(job_id)}, error_type='INVALID_JOB_ID',
                                reason='Gupy MCP expects a numeric job ID')
            return {}
        self.detail_attempts += 1
        started = datetime.now(timezone.utc).isoformat()
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "get_job_by_id", "arguments": {"id": numeric_job_id}},
        }
        try:
            response = self.http.post(
                GUPY_MCP_URL,
                json=body,
                headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
                timeout=15,
            )
            if response.status_code != 200:
                self.record_attempt('gupy', 'detail', started, query={'job_id': job_id},
                                    http_status=response.status_code, error_type=f'HTTP_{response.status_code}')
                return {}
            response.encoding = 'utf-8'
            for line in response.text.splitlines():
                if line.startswith("data:"):
                    payload = json.loads(line[5:].strip())
                    content = payload.get("result", {}).get("content", [{}])[0].get("text", "{}")
                    parsed = json.loads(content)
                    data = parsed.get("data", {})
                    if not isinstance(data, dict):
                        return {}
                    self._last_detail_success = True
                    self.record_attempt('gupy', 'detail', started, query={'job_id': job_id},
                                        http_status=response.status_code, result_count=1 if data else 0,
                                        native_ids=[job_id] if data else [])
                    detail = data.get('job') or data.get('data')
                    if isinstance(detail, dict):
                        return detail
                    return data if data.get('id') else {}
        except (requests.RequestException, ValueError, TypeError, KeyError):
            self.record_attempt('gupy', 'detail', started, query={'job_id': job_id},
                                error_type='ERROR', reason='detail request or parsing failed')
            return {}
        self.record_attempt('gupy', 'detail', started, query={'job_id': job_id}, error_type='INVALID_RESPONSE')
        return {}

    def _cached_detail(self, job_id: str):
        entry = self.detail_cache.get(job_id)
        if not isinstance(entry, dict):
            return None
        try:
            checked_at = datetime.fromisoformat(str(entry.get('checked_at', '')).replace('Z', '+00:00'))
            if checked_at.tzinfo is None:
                checked_at = checked_at.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) - checked_at.astimezone(timezone.utc) > self.DETAIL_CACHE_TTL:
                return None
        except ValueError:
            return None
        detail = entry.get('detail')
        return detail if isinstance(detail, dict) else None

    def _remember_detail(self, job_id: str, detail: Dict[str, Any]):
        # Keep only fields used by collection/scoring; do not persist arbitrary MCP payloads.
        allowed = ('publishedAt', 'publishedDate', 'createdAt', 'description')
        compact = {key: detail[key] for key in allowed if detail.get(key)}
        if isinstance(compact.get('description'), str):
            compact['description'] = compact['description'][:6000]
        self.detail_cache[job_id] = {
            'checked_at': datetime.now(timezone.utc).isoformat(),
            'detail': compact,
        }
        while len(self.detail_cache) > self.DETAIL_CACHE_MAX_ENTRIES:
            self.detail_cache.pop(next(iter(self.detail_cache)))

    def collect(self) -> List[Job]:
        discovered_jobs: List[Job] = []

        for q_args in self.queries:
            raw_jobs = self._query_api(q_args)
            self.query_stats.append({
                "parameters": dict(q_args),
                "results": len(raw_jobs),
                "status": self._last_query_status,
            })
            for raw in raw_jobs:
                job_id = str(raw.get("id"))
                has_publication_date = any(raw.get(field) for field in (
                    'publishedAt', 'publishedDate', 'createdAt'
                ))
                if not raw.get("description") or not has_publication_date:
                    cached_detail = self._cached_detail(job_id)
                    if cached_detail is not None:
                        raw = {**raw, **cached_detail}
                    elif self.detail_attempts < self.detail_limit:
                        attempts_before = self.detail_attempts
                        detail = self._get_detail(job_id)
                        if self.detail_attempts > attempts_before:
                            raw = {**raw, **detail}
                            if self._last_detail_success:
                                self._remember_detail(job_id, detail)
                title = normalize_title(raw.get("name", ""))
                company = raw.get("careerPageName", "").strip() or "Empresa Gupy"
                city = raw.get("city", "")
                state = raw.get("state", "")
                loc = f"{city}, {state}".strip(", ")
                raw_workplace = raw.get("workplaceType") or ""
                workplace = normalize_workplace(raw_workplace, location_text=loc, title_text=title)
                raw_type = raw.get("type", "")
                job_type = JOB_TYPE_TRANSLATIONS.get(raw_type, raw_type or "CLT")
                salary_label = raw.get("salary", {}).get("label", "Não informado")
                description = raw.get("description", "")

                published_at = (raw.get("publishedAt") or raw.get("publishedDate")
                                or raw.get("createdAt") or "")
                if self.max_age_hours and self.require_publication_date and not published_at:
                    self.query_stats[-1].setdefault("warnings", []).append("MISSING_PUBLICATION_DATE")
                    self.record_candidate_rejection(
                        'gupy', job_id, title, 'freshness_filter',
                        'A API não forneceu data de publicação e a política exige uma data verificável',
                        query=q_args, url=raw.get('jobUrl', ''),
                    )
                    continue
                if self.max_age_hours and published_at:
                    try:
                        parsed = datetime.fromisoformat(str(published_at).replace("Z", "+00:00"))
                        if parsed.tzinfo is None:
                            parsed = parsed.replace(tzinfo=timezone.utc)
                        if datetime.now(timezone.utc) - parsed.astimezone(timezone.utc) > timedelta(hours=self.max_age_hours):
                            self.record_candidate_rejection(
                                'gupy', job_id, title, 'freshness_filter',
                                f"Anúncio fora da janela de {self.max_age_hours} horas",
                                query=q_args, url=raw.get('jobUrl', ''), published_at=published_at,
                            )
                            continue
                    except ValueError:
                        self.query_stats[-1].setdefault("warnings", []).append("INVALID_PUBLICATION_DATE")
                        if self.require_publication_date:
                            self.record_candidate_rejection(
                                'gupy', job_id, title, 'freshness_filter',
                                'Data de publicação inválida; não foi possível confirmar a recência',
                                query=q_args, url=raw.get('jobUrl', ''), published_at=published_at,
                            )
                            continue
                
                # URL canônica
                career_page = raw.get("careerPageName", "").strip().lower()
                canonical_url = raw.get("jobUrl")
                if not canonical_url:
                    canonical_url = f"https://{career_page}.gupy.io/jobs/{job_id}" if career_page else "https://gupy.io"

                techs = extract_technologies(f"{title} {description}")

                job = Job(
                    title=title,
                    company=company,
                    workplace_type=workplace,
                    location=loc,
                    description=description,
                    job_type=job_type,
                    salary=salary_label,
                    technologies=techs,
                    published_at=published_at,
                )
                from core.evidence import profile_job_evidence
                profile_job_evidence(job)
                job.add_source("gupy", job_id, canonical_url)
                discovered_jobs.append(job)

        return discovered_jobs
