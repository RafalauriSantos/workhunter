"""Expansão determinística de consultas configuradas por fonte."""

from copy import deepcopy
from hashlib import sha1
from typing import Any, Dict, List


def unique_searches(searches: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Deduplicate by the actual request parameters, preserving distinct queries."""
    unique: List[Dict[str, Any]] = []
    seen = set()
    for search in searches:
        key = (
            str(search.get("query") or search.get("keywords") or "").strip().casefold(),
            search.get("time_range") or search.get("published_within_hours"),
            str(search.get("geo_id") or "106057199"),
            tuple(sorted((name, str(search.get(name))) for name in (
                "experience", "workplace_type", "job_type", "function", "industry"
            ) if search.get(name))),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(search)
    return unique


def rotate_searches(searches: List[Dict[str, Any]], limit: int, slot: int) -> List[Dict[str, Any]]:
    """Spread a large query plan fairly across cycles without starving its tail."""
    if limit <= 0 or not searches:
        return []
    if len(searches) <= limit:
        return list(searches)
    start = (max(0, int(slot)) * limit) % len(searches)
    ordered = searches[start:] + searches[:start]
    return ordered[:limit]


def rotate_searches_after(searches: List[Dict[str, Any]], limit: int,
                          last_query: Dict[str, Any] | None) -> List[Dict[str, Any]]:
    """Continue after the last query actually attempted by this source.

    Scheduler slots are a poor cursor when a source is polled less often than
    the main job loop: interval multiples can repeatedly select the same
    subset. The persisted last query makes rotation independent of wall-clock
    cadence and configuration ordering when query IDs remain stable.
    """
    if limit <= 0 or not searches:
        return []
    if not last_query:
        return list(searches[:limit])

    last_id = str(last_query.get('query_id') or '')
    last_text = str(last_query.get('keywords') or last_query.get('query') or '').strip().casefold()
    index = next((i for i, item in enumerate(searches)
                  if last_id and str(item.get('query_id') or '') == last_id), None)
    if index is None and last_text:
        index = next((i for i, item in enumerate(searches)
                      if str(item.get('query') or item.get('keywords') or '').strip().casefold()
                      == last_text), None)
    if index is None:
        return list(searches[:limit])

    start = (index + 1) % len(searches)
    ordered = searches[start:] + searches[:start]
    return ordered[:limit]


def select_linkedin_probe(searches: List[Dict[str, Any]],
                          probe: Dict[str, Any] | None) -> List[Dict[str, Any]]:
    """Select one explicitly configured, existing query for a one-shot probe."""
    if not probe or not probe.get('enabled') or not probe.get('query'):
        return []
    requested = ' '.join(str(probe['query']).split()).casefold()
    selected = next((item for item in searches if
                     ' '.join(str(item.get('query') or item.get('keywords') or '').split()).casefold()
                     == requested), None)
    if not selected:
        return []
    result = dict(selected)
    result['query_id'] = str(probe.get('id') or result.get('query_id') or 'linkedin-probe')
    return [result]


def plan_linkedin_batch(searches: List[Dict[str, Any]], limit: int,
                        last_query: Dict[str, Any] | None,
                        probe: Dict[str, Any] | None = None,
                        probe_consumed: bool = False):
    """Plan one normal batch, substituting an armed probe exactly once."""
    if not probe_consumed:
        selected_probe = select_linkedin_probe(searches, probe)
        if selected_probe:
            return selected_probe, str(probe.get('id') or selected_probe[0]['query_id'])
    return rotate_searches_after(searches, limit, last_query), None


def plan_searches(monitors: List[Dict[str, Any]], source_type: str) -> List[Dict[str, Any]]:
    """Gera consultas sem alterar os monitores originais.

    Um monitor pode usar ``query_variants`` para testar termos equivalentes.
    Monitores antigos continuam produzindo exatamente uma consulta.
    """
    planned: List[Dict[str, Any]] = []
    for index, monitor in enumerate(monitors):
        if monitor.get("type") != source_type:
            continue

        variants = monitor.get("query_variants")
        if not variants:
            variants = [monitor.get("keywords_search") or monitor.get("term") or monitor.get("description", "")]
        elif isinstance(variants, str):
            variants = [variants]

        seniorities = monitor.get("seniority_variants") or [monitor.get("seniority")]
        seniorities = [value for value in seniorities if value]
        if not seniorities:
            seniorities = [None]

        for variant_index, variant in enumerate(variants):
            for seniority_index, seniority in enumerate(seniorities):
                query = deepcopy(monitor)
                query["query"] = str(variant).strip()
                if seniority is not None:
                    query["seniority"] = seniority
                if monitor.get("recent_window_hours") is not None:
                    query["published_within_hours"] = monitor["recent_window_hours"]
                query["query_id"] = sha1(
                    f"{source_type}:{index}:{variant_index}:{seniority_index}:{query['query']}:{seniority}".encode("utf-8")
                ).hexdigest()[:12]
                planned.append(query)
    return planned
