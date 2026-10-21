from typing import List

from models.job import Job
from storage.state_store import StateStore
from core.deduplicator import delivery_fingerprint


def finalize_delivery(
    store: StateStore,
    job: Job,
    source_ids: List[str],
    sent: bool,
    score: int,
    reason: str,
) -> str:
    """Persiste o resultado do alerta e só confirma vagas entregues."""
    job_id = source_ids[0] if source_ids else "unknown"
    primary_source = next(iter(job.sources.keys()), "unknown")
    status = "DELIVERED" if sent else "DELIVERY_FAILED"
    decision_reason = reason if sent else "Falha ao enviar alerta pelo Telegram"

    store.record_decision(
        job_id,
        primary_source,
        job.identity_fingerprint,
        job.content_hash,
        status,
        decision_reason,
        final_score=score,
        raw_url=job.raw_url,
        canonical_url=job.canonical_url,
        evidence_level=job.evidence_level,
        category=job.compatibility_category,
        potential_score=job.potential_score,
        operational_seniority=job.operational_seniority,
        score_breakdown=job.score_breakdown,
        match_reasons=job.match_reasons,
    )
    fingerprint = delivery_fingerprint(job)
    store.record_delivery(fingerprint, source_ids, delivered=sent)
    if not sent:
        store.release_delivery_claim(fingerprint)
    return status
