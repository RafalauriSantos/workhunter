import json
from pathlib import Path

from core.query_planner import (
    plan_searches, unique_searches, rotate_searches,
    rotate_searches_after, select_linkedin_probe, plan_linkedin_batch,
)


def test_query_planner_expands_variants_without_mutating_monitor():
    monitor = {
        "type": "linkedin",
        "keywords_search": "desenvolvedor",
        "query_variants": ["dev", "developer"],
        "time_range": "r3600",
    }

    planned = plan_searches([monitor], "linkedin")

    assert [query["query"] for query in planned] == ["dev", "developer"]
    assert len({query["query_id"] for query in planned}) == 2
    assert "query" not in monitor


def test_query_planner_preserves_legacy_single_query():
    planned = plan_searches([{"type": "gupy", "term": "Java", "limit": 20}], "gupy")

    assert len(planned) == 1
    assert planned[0]["query"] == "Java"
    assert planned[0]["limit"] == 20


def test_linkedin_query_variants_are_not_deduplicated_by_monitor_description():
    planned = plan_searches([{
        "type": "linkedin",
        "description": "LinkedIn React / Frontend",
        "query_variants": ["React Junior", "React Pleno", "frontend React"],
        "time_range": "r7200",
        "geo_id": "brasil",
    }], "linkedin")

    selected = unique_searches(planned)

    assert [item["query"] for item in selected] == ["React Junior", "React Pleno", "frontend React"]


def test_linkedin_duplicate_query_from_different_monitors_runs_once():
    planned = plan_searches([
        {"type": "linkedin", "description": "first", "query_variants": ["React Junior"], "time_range": "r7200"},
        {"type": "linkedin", "description": "second", "query_variants": ["React Junior"], "time_range": "r7200"},
    ], "linkedin")

    assert len(unique_searches(planned)) == 1


def test_limited_linkedin_searches_rotate_without_starving_later_queries():
    searches = [{"query": f"query-{index}"} for index in range(22)]

    first = rotate_searches(searches, limit=8, slot=0)
    second = rotate_searches(searches, limit=8, slot=1)
    third = rotate_searches(searches, limit=8, slot=2)

    assert len(first) == len(second) == len(third) == 8
    assert len({item["query"] for item in first + second + third}) == 22


def test_rotation_advances_after_the_last_actual_query_not_scheduler_ticks():
    searches = [{'query': f'query-{index}'} for index in range(32)]
    last_query = None
    visited = []
    for _ in range(32):
        selected = rotate_searches_after(searches, limit=1, last_query=last_query)
        visited.append(selected[0]['query'])
        last_query = {'keywords': selected[0]['query']}
    assert visited == [f'query-{index}' for index in range(32)]


def test_rotation_matches_last_query_id_before_query_text():
    searches = [
        {'query': 'reordered', 'query_id': 'current-id'},
        {'query': 'last query', 'query_id': 'new-id'},
        {'query': 'next query', 'query_id': 'next-id'},
    ]
    selected = rotate_searches_after(
        searches, 1, {'query_id': 'new-id', 'keywords': 'old query text'}
    )
    assert selected[0]['query'] == 'next query'


def test_linkedin_probe_selects_existing_broad_query_without_increasing_request_count():
    searches = [
        {'query': 'role-specific', 'query_id': 'specific', 'max_pages': 1},
        {'query': 'developer OR software', 'query_id': 'planned-broad', 'max_pages': 1},
    ]
    probe = select_linkedin_probe(searches, {
        'enabled': True, 'id': 'profile-broad-v1', 'query': 'developer OR software',
    })
    assert len(probe) == 1
    assert probe[0]['query_id'] == 'profile-broad-v1'
    assert probe[0]['max_pages'] == 1


def test_linkedin_probe_is_ignored_when_disabled_or_query_not_in_plan():
    searches = [{'query': 'developer'}]
    assert select_linkedin_probe(searches, {'enabled': False, 'query': 'developer'}) == []
    assert select_linkedin_probe(searches, {'enabled': True, 'query': 'software'}) == []


def test_linkedin_probe_is_one_shot_then_rotation_continues_from_it():
    searches = [
        {'query': 'first', 'query_id': 'first-id'},
        {'query': 'broad', 'query_id': 'broad-id'},
        {'query': 'third', 'query_id': 'third-id'},
    ]
    probe = {'enabled': True, 'id': 'probe-1', 'query': 'broad'}
    selected, probe_id = plan_linkedin_batch(searches, 1, None, probe)
    assert selected[0]['query'] == 'broad'
    assert probe_id == 'probe-1'

    selected, probe_id = plan_linkedin_batch(
        searches, 1, {'query_id': 'probe-1', 'keywords': 'broad'},
        probe, probe_consumed=True,
    )
    assert selected[0]['query'] == 'third'
    assert probe_id is None


def test_configured_linkedin_broad_probe_matches_real_plan_and_keeps_one_request():
    project_root = Path(__file__).resolve().parents[1]
    config = json.loads((project_root / 'config.json').read_text(encoding='utf-8'))
    configured = [
        search
        for monitor in config['monitors']
        for search in plan_searches([monitor], 'linkedin')
    ]
    probe = config['linkedin_broad_query_probe']

    selected, probe_id = plan_linkedin_batch(
        unique_searches(configured), config['linkedin_max_queries'], None,
        probe=probe,
    )

    assert len(selected) == 1
    assert selected[0]['query'] == probe['query']
    assert probe_id == probe['id']
    assert min(selected[0].get('max_pages', 3), config['linkedin_max_pages_per_query']) == 1
