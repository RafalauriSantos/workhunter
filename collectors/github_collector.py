import os
import re
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any, Optional
import requests
from datetime import datetime, timezone
from collectors.base import BaseCollector
from models.job import Job
from core.normalizer import normalize_title, normalize_workplace


DEFAULT_GITHUB_REPOS = [
    "frontendbr/vagas",
    "backend-br/vagas",
    "react-brasil/vagas",
]


class GithubIssuesCollector(BaseCollector):
    """
    Coletor de vagas via GitHub Issues (ecossistema aberto de vagas dev no Brasil).
    Repositórios monitorados por padrão: frontendbr/vagas, backend-br/vagas, react-brasil/vagas.
    Utiliza GITHUB_TOKEN se presente para cota de 5.000 req/h (ou 60 req/h anônimo).
    """

    def __init__(
        self,
        http_session: requests.Session,
        repos: Optional[List[str]] = None,
        keywords: Optional[List[str]] = None,
        exclude_keywords: Optional[List[str]] = None,
        max_pages: int = 3,
        lookback_days: int = 1,
        fallback_lookback_days: int = 3,
        strict_freshness: bool = True,
    ):
        self.http = http_session
        self.repos = repos or DEFAULT_GITHUB_REPOS
        self.keywords = [k.lower() for k in (keywords or [])]
        self.exclude_keywords = [k.lower() for k in (exclude_keywords or [])]
        self.max_pages = max(1, max_pages)
        self.lookback_days = max(0, lookback_days)
        self.fallback_lookback_days = max(self.lookback_days, fallback_lookback_days)
        self.strict_freshness = strict_freshness
        self.stats: Dict[str, Any] = {
            "repos": {},
            "pages": 0,
            "issues_seen": 0,
            "pull_requests_skipped": 0,
            "stale_skipped": 0,
            "filtered_skipped": 0,
            "jobs": 0,
            "primary_jobs": 0,
            "fallback_used": False,
        }
        self.collection_attempts = []

    def _get_headers(self) -> Dict[str, str]:
        headers = {
            "User-Agent": "job-finder-bot/1.0",
            "Accept": "application/vnd.github.v3+json",
        }
        token = os.getenv("GITHUB_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def _extract_company_from_title(self, title: str, repo: str) -> str:
        """Tenta extrair o nome da empresa do título padrão de issues de vagas."""
        patterns = [
            r"(?:na|no|@)\s+([A-Za-z0-9\.\-\_\s]{2,30}?)(?:\s+\[|\s+\(|\s+[\-\|]|$)",
            r"[\-\|]\s+([A-Za-z0-9\.\-\_\s]{2,30}?)(?:\s+\[|\s+\(|$)",
        ]
        for pat in patterns:
            m = re.search(pat, title, re.IGNORECASE)
            if m:
                cand = m.group(1).strip()
                if cand.lower() not in ["remoto", "hibrido", "híbrido", "presencial", "clt", "pj", "junior", "júnior", "pleno", "senior", "sênior"]:
                    return cand

        repo_clean = repo.split("/")[-1].replace("-", " ").replace("vagas", "").strip().title()
        return f"GitHub ({repo_clean or repo})"

    def _determine_workplace_type(self, labels: List[str], title: str) -> str:
        lbls_lower = [l.lower() for l in labels]
        if any("remoto" in l or "remote" in l for l in lbls_lower):
            return "remote"
        if any("hibrid" in l or "híbrid" in l for l in lbls_lower):
            return "hybrid"
        if any("presencial" in l or "alocado" in l or "on-site" in l for l in lbls_lower):
            return "on-site"
        return normalize_workplace("", "", title)

    def _determine_seniority(self, labels: List[str], title: str) -> str:
        lbls_lower = [l.lower() for l in labels]
        title_lower = title.lower()
        if any("junior" in l or "júnior" in l or "estagio" in l or "estágio" in l for l in lbls_lower):
            return "junior"
        if any("senior" in l or "sênior" in l or "lead" in l or "especialista" in l for l in lbls_lower):
            return "senior"
        if any("pleno" in l for l in lbls_lower):
            return "pleno"

        if any(k in title_lower for k in ["junior", "júnior", "estagio", "estágio"]):
            return "junior"
        if any(k in title_lower for k in ["senior", "sênior", "lead", "especialista"]):
            return "senior"
        return "unknown"

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
        headers = self._get_headers()
        now = datetime.now(timezone.utc)
        primary_cutoff = now - timedelta(days=self.lookback_days)
        fallback_cutoff = now - timedelta(days=self.fallback_lookback_days)
        fallback_jobs: List[Job] = []
        primary_jobs: List[Job] = []

        for repo in self.repos:
            repo_stats = {"pages": 0, "issues_seen": 0, "jobs": 0, "stale": 0, "filtered": 0}
            self.stats["repos"][repo] = repo_stats
            try:
                for page in range(1, self.max_pages + 1):
                    started = datetime.now(timezone.utc).isoformat()
                    url = f"https://api.github.com/repos/{repo}/issues"
                    resp = self.http.get(
                        url,
                        headers=headers,
                        params={"state": "open", "sort": "updated", "direction": "desc", "per_page": 30, "page": page},
                        timeout=15,
                    )
                    self.stats["pages"] += 1
                    repo_stats["pages"] += 1
                    if resp.status_code != 200:
                        self.record_attempt('github', 'issues', started, query={'repo': repo}, page=page,
                                            http_status=resp.status_code, error_type=f'HTTP_{resp.status_code}')
                        self.report_issue(f'HTTP_{resp.status_code}')
                        print(f"[ALERTA GithubCollector] Repo {repo} retornou status {resp.status_code}")
                        break

                    issues = resp.json()
                    self.record_attempt('github', 'issues', started, query={'repo': repo}, page=page,
                                        http_status=resp.status_code, result_count=len(issues) if isinstance(issues, list) else 0,
                                        native_ids=[str(issue.get('id')) for issue in issues] if isinstance(issues, list) else [],
                                        error_type=None if isinstance(issues, list) else 'INVALID_RESPONSE')
                    if not isinstance(issues, list):
                        self.report_issue('INVALID_RESPONSE')
                        break
                    if not issues:
                        break

                    for issue in issues:
                        self.stats["issues_seen"] += 1
                        repo_stats["issues_seen"] += 1
                        if "pull_request" in issue:
                            self.stats["pull_requests_skipped"] += 1
                            self.record_candidate_rejection(
                                'github', issue.get('id'), issue.get('title', ''), 'source_filter',
                                'Item é um pull request, não uma publicação de vaga',
                                query={'repo': repo}, url=issue.get('html_url', ''),
                                published_at=issue.get('created_at', ''),
                            )
                            continue

                        created_at = issue.get("created_at")
                        updated_at = issue.get("updated_at") or created_at
                        created = updated = None
                        try:
                            if created_at:
                                created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                            if updated_at:
                                updated = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
                        except ValueError:
                            created = None
                        # GitHub sorts issues by update time, but update time is
                        # not publication time. Never resurrect an old vacancy
                        # because someone edited or commented on its issue.
                        if self.strict_freshness and created is None:
                            self.stats["stale_skipped"] += 1
                            repo_stats["stale"] += 1
                            self.record_candidate_rejection(
                                'github', issue.get('id'), issue.get('title', ''), 'freshness_filter',
                                'Data original de criação ausente ou inválida; atualização recente não prova que a vaga é nova',
                                query={'repo': repo}, url=issue.get('html_url', ''),
                                published_at=created_at or '',
                            )
                            continue
                        if created is None:
                            created = fallback_cutoff
                        if created < primary_cutoff:
                            self.stats["stale_skipped"] += 1
                            repo_stats["stale"] += 1
                            self.record_candidate_rejection(
                                'github', issue.get('id'), issue.get('title', ''), 'freshness_filter',
                                f"Publicação anterior à janela de {self.lookback_days} dias",
                                query={'repo': repo}, url=issue.get('html_url', ''),
                                published_at=created.isoformat(),
                            )
                            continue

                        raw_title = issue.get("title", "")
                        labels = [
                            l.get("name", "") for l in issue.get("labels", [])
                            if isinstance(l, dict) and "name" in l
                        ]
                        body = issue.get("body", "") or ""

                        search_context = f"{raw_title} {' '.join(labels)} {body[:12000]}"
                        if not self._matches_filters(search_context):
                            self.stats["filtered_skipped"] += 1
                            repo_stats["filtered"] += 1
                            excluded = [word for word in self.exclude_keywords if word in search_context.lower()]
                            matched = [word for word in self.keywords if word in search_context.lower()]
                            reason = (f"Publicação contém termo excluído: {', '.join(excluded)}" if excluded
                                      else 'Publicação não corresponde às palavras-chave configuradas')
                            self.record_candidate_rejection(
                                'github', issue.get('id'), raw_title, 'source_filter', reason,
                                query={'repo': repo, 'keywords': self.keywords,
                                       'exclude_keywords': self.exclude_keywords, 'matched_keywords': matched},
                                url=issue.get('html_url', ''), published_at=created.isoformat() if created else '',
                            )
                            continue

                        title_clean = normalize_title(raw_title)
                        company = self._extract_company_from_title(title_clean, repo)
                        workplace_type = self._determine_workplace_type(labels, f"{title_clean} {body[:4000]}")
                        seniority = self._determine_seniority(labels, f"{title_clean} {body[:12000]}")

                        job = Job(
                            title=title_clean,
                            company=company,
                            workplace_type=workplace_type,
                            location="Remoto" if workplace_type == "remote" else "Brasil",
                            description=body[:4000],
                            seniority=seniority,
                            job_type="Vaga GitHub",
                            salary="Não informado",
                        )
                        job.add_source("github", str(issue.get("id")), issue.get("html_url", ""))
                        job.published_at = created.isoformat() if created else ""
                        primary_jobs.append(job)
                        self.stats["jobs"] += 1
                        repo_stats["jobs"] += 1

                    if len(issues) < 30:
                        break

            except Exception as e:
                self.report_issue(type(e).__name__)
                print(f"[ERRO GithubCollector] Falha ao coletar de {repo}: {e}")

        discovered = primary_jobs
        self.stats["primary_jobs"] = len(primary_jobs)
        self.stats["jobs"] = len(discovered)
        return discovered
