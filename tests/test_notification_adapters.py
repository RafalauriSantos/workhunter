from types import SimpleNamespace

import requests

from notify.adapters import LegacyNotifierAdapter, WPPConnectAdapter, WhatsAppCloudAdapter
from models.job import Job


class FakeNotifier:
    def send_job_alert(self, job):
        return True

    def send_heartbeat(self, *args):
        return True


def test_legacy_notifier_adapter_normalizes_success():
    result = LegacyNotifierAdapter(FakeNotifier(), 'telegram').send_job_alert(SimpleNamespace())

    assert result.channel == 'telegram'
    assert result.status == 'DELIVERED'
    assert result.retryable is False


def test_whatsapp_adapter_is_disabled_without_official_configuration():
    adapter = WhatsAppCloudAdapter(None, None, None, None, 'pt_BR', requests.Session())

    result = adapter.send_job_alert(SimpleNamespace(title='Dev', company='Acme'))

    assert result.channel == 'whatsapp'
    assert result.status == 'DISABLED'
    assert result.retryable is False


def test_whatsapp_adapter_sends_approved_template(monkeypatch):
    calls = []

    class Response:
        status_code = 200
        content = b'{"messages":[{"id":"wamid.test"}]}'

        def json(self):
            return {'messages': [{'id': 'wamid.test'}]}

    class Http:
        def post(self, *args, **kwargs):
            calls.append((args, kwargs))
            return Response()

    adapter = WhatsAppCloudAdapter('token', 'phone-id', '5511999999999',
                                   'job_alert', 'pt_BR', Http())
    result = adapter.send_job_alert(SimpleNamespace(title='Dev React', company='Acme'))

    assert result.status == 'DELIVERED'
    assert result.provider_message_id == 'wamid.test'
    assert calls[0][1]['json']['type'] == 'template'


def test_wppconnect_adapter_sends_job_card():
    calls = []

    class Response:
        status_code = 201
        content = b'{"id":"wpp-message"}'

        def json(self):
            return {'id': 'wpp-message'}

    class Http:
        def post(self, *args, **kwargs):
            calls.append((args, kwargs))
            return Response()

    adapter = WPPConnectAdapter('http://127.0.0.1:21465', 'test-token',
                                'workhunter', '5511999999999', Http())
    job = Job(title='Dev', company='Acme', workplace_type='remote',
              raw_url='https://example.com/job')
    result = adapter.send_job_alert(job)

    assert result.status == 'DELIVERED'
    assert result.provider_message_id == 'wpp-message'
    assert calls[0][0][0].endswith('/api/workhunter/send-message')
    assert calls[0][1]['json']['phone'] == '5511999999999'
