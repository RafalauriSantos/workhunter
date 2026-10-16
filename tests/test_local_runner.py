from deployment import run_local


def test_network_is_ready_when_a_probe_can_open_https(monkeypatch):
    calls = []

    class Probe:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def connect(address, timeout):
        calls.append((address, timeout))
        if address[0] == run_local.NETWORK_PROBE_HOSTS[0]:
            raise OSError('DNS not ready')
        return Probe()

    monkeypatch.setattr(run_local.socket, 'create_connection', connect)

    assert run_local.network_is_ready() is True
    assert [call[0][0] for call in calls] == list(run_local.NETWORK_PROBE_HOSTS)


def test_network_is_not_ready_when_all_probes_fail(monkeypatch):
    def fail(_address, timeout):
        raise OSError('network unavailable')

    monkeypatch.setattr(run_local.socket, 'create_connection', fail)

    assert run_local.network_is_ready() is False
