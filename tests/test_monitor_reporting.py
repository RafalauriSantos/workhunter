import monitor


def test_linkedin_report_accepts_throttled_query_without_full_search_fields():
    line = monitor.format_linkedin_query_stats({
        "keywords": "*",
        "status": "THROTTLED",
        "reason": "SOURCE_POLL_INTERVAL",
    })

    assert "status=THROTTLED" in line
    assert "páginas=0" in line
    assert "janela=n/a" in line
