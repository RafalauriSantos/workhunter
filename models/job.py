from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional
import hashlib
import json
import unicodedata


@dataclass
class JobSource:
    source_name: str       # "gupy", "linkedin", "google_news", etc.
    source_job_id: str     # ID original na plataforma
    url: str               # Link direto para a candidatura
    first_seen: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass
class Job:
    title: str
    company: str
    workplace_type: str                  # "remote", "hybrid", "on-site", "unknown"
    location: str = ""
    description: str = ""
    job_type: str = "CLT"                # "CLT", "PJ", "Estágio", "Trainee"
    salary: str = "Não informado"
    technologies: List[str] = field(default_factory=list)
    seniority: str = "junior"            # "intern", "junior", "mid", "senior", "unknown"
    match_score: int = 0                 # 0 a 100
    learning_interest_score: int = 0     # Interesse de aprendizado, separado do match
    freshness_score: int = 0              # Frescor, separado da aderência profissional
    score_breakdown: Dict[str, int] = field(default_factory=dict)
    ranking_evidence: Dict[str, str] = field(default_factory=dict)
    match_reasons: List[str] = field(default_factory=list)
    pcd_signal: str = ""               # "", "TITLE" — origem do sinal PCD detectado
    raw_url: str = ""                  # URL bruta original do feed / agregador
    resolved_url: str = ""             # URL final após resolução de redirecionamentos HTTP
    canonical_url: str = ""            # URL canônica limpa (sem parâmetros de tracking)
    evidence_level: str = "HIGH_EVIDENCE"  # "LOW_EVIDENCE", "MEDIUM_EVIDENCE", "HIGH_EVIDENCE"
    analysis: Dict = field(default_factory=dict)
    compatibility_category: str = ""
    potential_score: int = 0
    operational_seniority: str = ""
    published_at: str = ""                # Timestamp ISO-8601 quando fornecido pela fonte
    sources: Dict[str, JobSource] = field(default_factory=dict)
    first_seen: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def add_source(self, source_name: str, source_job_id: str, url: str):
        """Associa uma nova fonte a esta vaga (suporte a vaga multi-fonte)."""
        if source_name not in self.sources:
            self.sources[source_name] = JobSource(
                source_name=source_name,
                source_job_id=str(source_job_id),
                url=url,
            )

    @property
    def primary_url(self) -> str:
        """Retorna o melhor link de candidatura disponível."""
        if self.canonical_url:
            return self.canonical_url
        if self.resolved_url:
            return self.resolved_url
        # Preferência: link oficial direto
        for src in ["gupy", "linkedin", "career_page"]:
            if src in self.sources and self.sources[src].url:
                return self.sources[src].url
        if self.sources:
            return next(iter(self.sources.values())).url
        return self.raw_url

    @property
    def identity_fingerprint(self) -> str:
        """
        Identidade Canônica da Vaga (Imutável contra edições de texto pelo RH).
        Combina: empresa normalizada + título normalizado + modalidade + localização.
        Garante que correções ortográficas ou pequenas edições no corpo da vaga não
        gerem uma falsa nova vaga duplicada.
        """
        norm_company = "".join(c for c in unicodedata.normalize("NFD", self.company.strip().lower()) if unicodedata.category(c) != "Mn")
        norm_title = "".join(c for c in unicodedata.normalize("NFD", self.title.strip().lower()) if unicodedata.category(c) != "Mn")
        norm_workplace = self.workplace_type.strip().lower()
        norm_location = "".join(c for c in unicodedata.normalize("NFD", (self.location or "").strip().lower()) if unicodedata.category(c) != "Mn")

        payload = f"{norm_company}|{norm_title}|{norm_workplace}|{norm_location}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @property
    def content_hash(self) -> str:
        """
        Hash do conteúdo textual da vaga. Permite auditar se a descrição
        sofreu alterações sem corromper a identidade canônica.
        """
        clean_desc = "".join(c for c in unicodedata.normalize("NFD", (self.description or "").lower()) if unicodedata.category(c) != "Mn")
        return hashlib.sha256(clean_desc.encode("utf-8")).hexdigest()

    @property
    def fingerprint(self) -> str:
        """Alias para identity_fingerprint garantindo compatibilidade com o pipeline existente."""
        return self.identity_fingerprint

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "company": self.company,
            "workplace_type": self.workplace_type,
            "location": self.location,
            "description": self.description,
            "job_type": self.job_type,
            "salary": self.salary,
            "technologies": self.technologies,
            "seniority": self.seniority,
            "match_score": self.match_score,
            "learning_interest_score": self.learning_interest_score,
            "freshness_score": self.freshness_score,
            "score_breakdown": self.score_breakdown,
            "ranking_evidence": self.ranking_evidence,
            "match_reasons": self.match_reasons,
            "pcd_signal": self.pcd_signal,
            "raw_url": self.raw_url,
            "resolved_url": self.resolved_url,
            "canonical_url": self.canonical_url,
            "evidence_level": self.evidence_level,
            "analysis": self.analysis,
            "compatibility_category": self.compatibility_category,
            "potential_score": self.potential_score,
            "operational_seniority": self.operational_seniority,
            "published_at": self.published_at,
            "primary_url": self.primary_url,
            "sources": {
                name: {
                    "source_name": s.source_name,
                    "source_job_id": s.source_job_id,
                    "url": s.url,
                    "first_seen": s.first_seen,
                }
                for name, s in self.sources.items()
            },
            "identity_fingerprint": self.identity_fingerprint,
            "content_hash": self.content_hash,
            "fingerprint": self.fingerprint,
            "first_seen": self.first_seen,
        }
