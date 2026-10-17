from core.metrics import build_funnel_report


def test_cycle_metrics_account_for_every_unique_job_and_keep_scope_discards():
    result = build_funnel_report(
        raw_count=15, unique_count=11, notified_count=0, seen_count=8,
        discarded_counts={
            'pcd': 0, 'location': 0, 'seniority': 0, 'score': 0, 'scope': 3,
        },
    )

    assert result['discarded']['scope'] == 3
    assert result['seen'] == 8
    assert result['unaccounted'] == 0
    assert result['duplicates'] == 4


def test_cycle_metrics_expose_unaccounted_candidates():
    result = build_funnel_report(
        raw_count=10, unique_count=7, notified_count=1,
        discarded_counts={'scope': 2}, seen_count=3,
    )

    assert result['unaccounted'] == 1
    assert result['overcount'] == 0
