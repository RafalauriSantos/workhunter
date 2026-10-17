from typing import Any, Dict


def summarize_cycle(
    raw_count: int,
    unique_count: int,
    notified_count: int,
    discarded_counts: Dict[str, int],
    source_statuses: Dict[str, str],
    seen_count: int = 0,
) -> Dict[str, Any]:
    """Produz métricas honestas do ciclo sem inferir precisão ou recall."""
    duplicate_count = max(0, raw_count - unique_count)
    duplicate_rate = duplicate_count / raw_count if raw_count else 0.0
    accounted_count = seen_count + notified_count + sum(discarded_counts.values())
    return {
        "raw_count": raw_count,
        "unique_count": unique_count,
        "duplicate_count": duplicate_count,
        "duplicate_rate": duplicate_rate,
        "notified_count": notified_count,
        "seen_count": seen_count,
        "discarded_counts": dict(discarded_counts),
        "accounted_count": accounted_count,
        "unaccounted_count": max(0, unique_count - accounted_count),
        "overcount_count": max(0, accounted_count - unique_count),
        "source_failures": [
            source for source, status in source_statuses.items()
            if str(status).startswith("FALHA") or status in {'FAILED', 'PARTIAL'}
        ],
        "precision_estimate": None,
        "recall_estimate": None,
        "precision_recall_note": "Feedback humano ainda não registrado",
    }


def build_funnel_report(
    raw_count: int,
    unique_count: int,
    seen_count: int,
    discarded_counts: Dict[str, int],
    notified_count: int,
) -> Dict[str, Any]:
    """Return the complete operator-facing funnel, including reconciliation."""
    metrics = summarize_cycle(
        raw_count, unique_count, notified_count, discarded_counts, {}, seen_count
    )
    return {
        "raw": raw_count,
        "unique": unique_count,
        "duplicates": metrics["duplicate_count"],
        "seen": seen_count,
        "discarded": dict(discarded_counts),
        "notified": notified_count,
        "unaccounted": metrics["unaccounted_count"],
        "overcount": metrics["overcount_count"],
    }
