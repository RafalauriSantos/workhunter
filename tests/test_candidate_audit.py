from core.candidate_audit import persist_rejections
from storage.sqlite_store import SQLiteStore


def test_persists_source_filter_rejections_with_cycle_id(tmp_path):
    store = SQLiteStore(tmp_path / 'state.db')

    class Collector:
        rejected_candidates = [{
            'source': 'rss', 'source_job_id': 'item-1', 'title': 'Senior Developer',
            'query': {'feed': 'Indeed'}, 'stage': 'source_filter',
            'reason': 'Título contém termo excluído: senior',
            'url': 'https://jobs.example/1', 'published_at': '2026-10-17T08:00:00Z',
        }]

    persist_rejections([Collector()], store, 'cycle-42')

    with store.connect() as db:
        row = db.execute('SELECT cycle_id,source,source_job_id,stage,reason FROM candidate_rejections').fetchone()
    assert row == ('cycle-42', 'rss', 'item-1', 'source_filter', 'Título contém termo excluído: senior')
