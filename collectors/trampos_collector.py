import re
from typing import List, Dict, Any, Optional
import requests
from datetime import datetime, timezone
from collectors.base import BaseCollector
from models.job import Job
from core.normalizer import normalize_title, normalize_workplace


class TramposCollector(BaseCollector):
    """
    Coletor de vagas via API pública REST da Trampos.co (SPEC-008).
    Consome endpoint JSON estruturado com salário, cidade, empresa e modalidade.
    """

    BASE_URL = "https://trampos.co/api/v2/opportunities"

    def __init__(
        self,
        http_session: requests.Session,
        keywords: Optional[List[str]] = None,
        exclude_keywords: Optional[List[str]] = None,
        max_pages: int = 1,
    ):
        self.http = http_session
        self.keywords = [k.lower() for k in (keywords or [])]
        self.exclude_keywords = [k.lower() for k in (exclude_keywords or [])]
        self.max_pages = max_pages
        self.collection_attempts = []

    def _matches_filters(self, text_to_check: str) -> bool:
        lower = text_to_check.lower()
        if self.exclude_keywords:
            if any(ex in lower for ex in self.exclude_keywords):
                return False
        if self.keywords:
            if not any(kw in lower for kw in self.keywords):
                return False
        return True

    def collect(self) -> List[Job]:
        discovered: List[Job] = []
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

        for page in range(1, self.max_pages + 1):
            started = datetime.now(timezone.utc).isoformat()
            url = f"{self.BASE_URL}?page={page}"
            try:
                resp = self.http.get(url, headers=headers, timeout=12)
                if resp.status_code != 200:
                    self.record_attempt('trampos', 'opportunities', started, page=page,
                                        http_status=resp.status_code, error_type=f'HTTP_{resp.status_code}')
                    self.report_issue(f'HTTP_{resp.status_code}')
                    print(f"[ALERTA TramposCollector] Status {resp.status_code} na página {page}")
                    continue

                data = resp.json()
                opportunities = data.get("opportunities", [])
                self.record_attempt('trampos', 'opportunities', started, page=page,
                                    http_status=resp.status_code, result_count=len(opportunities),
                                    native_ids=[str(item.get('id')) for item in opportunities])
                if not opportunities:
                    break

                for opp in opportunities:
                    title_raw = opp.get("name", "")
                    title_clean = normalize_title(title_raw)

                    # Concatena título e descrição para matching de filtros
                    desc = opp.get("description", "") or ""
                    full_text = f"{title_clean} {desc}"

                    if not self._matches_filters(full_text):
                        excluded = [word for word in self.exclude_keywords if word in full_text.lower()]
                        matched = [word for word in self.keywords if word in full_text.lower()]
                        reason = (f"Anúncio contém termo excluído: {', '.join(excluded)}" if excluded
                                  else 'Anúncio não corresponde às palavras-chave configuradas')
                        self.record_candidate_rejection(
                            'trampos', opp.get('id'), title_clean, 'source_filter', reason,
                            query={'page': page, 'keywords': self.keywords,
                                   'exclude_keywords': self.exclude_keywords, 'matched_keywords': matched},
                            url=opp.get('url', ''), published_at=opp.get('published_at', ''),
                        )
                        continue

                    company = opp.get("company_name") or "Empresa Confidencial"
                    city = opp.get("city", "") or ""
                    state = opp.get("state", "") or ""
                    location_str = f"{city}, {state}".strip(", ")

                    # Normaliza modalidade
                    raw_wp = opp.get("workplace_type") or ""
                    wp_type = normalize_workplace(raw_wp, location_str, title_clean)

                    salary = opp.get("salary") or "Não informado"
                    job_type = opp.get("opportunity_type") or "CLT"
                    opp_id = str(opp.get("id", ""))
                    opp_url = f"https://trampos.co/oportunidades/{opp_id}"

                    job = Job(
                        title=title_clean,
                        company=company,
                        workplace_type=wp_type,
                        location=location_str,
                        description=desc,
                        job_type=job_type,
                        salary=salary,
                        raw_url=opp_url,
                        resolved_url=opp_url,
                        canonical_url=opp_url,
                        published_at=opp.get("published_at") or "",
                    )
                    from core.evidence import profile_job_evidence
                    profile = profile_job_evidence(job)
                    job.evidence_level = profile["level"]

                    job.add_source("trampos", opp_id, opp_url)
                    discovered.append(job)

            except Exception as e:
                self.report_issue(type(e).__name__)
                print(f"[ERRO TramposCollector] Falha ao coletar página {page}: {e}")

        return discovered
