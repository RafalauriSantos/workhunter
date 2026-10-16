import monitor


def test_global_http_session_does_not_retry_rate_limits():
    session = monitor.get_http_session()
    retry = session.get_adapter("https://").max_retries

    assert 429 not in retry.status_forcelist
