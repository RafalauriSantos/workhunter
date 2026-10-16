"""Common notification adapter contract and optional WhatsApp Cloud adapter."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import requests


@dataclass(frozen=True)
class DeliveryResult:
    channel: str
    status: str
    provider_message_id: str | None = None
    retryable: bool = False
    error: str | None = None


class NotificationAdapter(Protocol):
    channel: str

    def send_job_alert(self, job: Any) -> DeliveryResult:
        ...

    def send_heartbeat(self, monitors_count: int, jobs_tracked: int) -> DeliveryResult:
        ...


class LegacyNotifierAdapter:
    """Adapts the existing notifiers without changing their public behavior."""

    def __init__(self, notifier: Any, channel: str):
        self.notifier = notifier
        self.channel = channel
        self.last_status = 'FAILED'

    def send_job_alert(self, job: Any) -> DeliveryResult:
        try:
            sent = self.notifier.send_job_alert(job)
            status = 'DELIVERED' if sent else getattr(self.notifier, 'last_status', 'UNKNOWN')
        except Exception as exc:
            status = 'UNKNOWN'
            return DeliveryResult(self.channel, status, retryable=True, error=type(exc).__name__)
        self.last_status = status
        return DeliveryResult(self.channel, status, retryable=status in {'UNKNOWN', 'FAILED'})

    def send_heartbeat(self, monitors_count: int, jobs_tracked: int) -> DeliveryResult:
        try:
            sent = self.notifier.send_heartbeat(monitors_count, jobs_tracked)
            status = 'DELIVERED' if sent else getattr(self.notifier, 'last_status', 'FAILED')
        except Exception as exc:
            status = 'UNKNOWN'
            return DeliveryResult(self.channel, status, retryable=True, error=type(exc).__name__)
        return DeliveryResult(self.channel, status, retryable=status in {'UNKNOWN', 'FAILED'})


class WhatsAppCloudAdapter:
    """Optional official WhatsApp Cloud API adapter.

    Business initiated messages use an approved template. The adapter stays
    disabled when credentials or a template name are absent.
    """

    channel = 'whatsapp'
    endpoint = 'https://graph.facebook.com/v23.0/{phone_number_id}/messages'

    def __init__(self, token: str | None, phone_number_id: str | None,
                 recipient: str | None, template_name: str | None,
                 template_language: str, http_session: requests.Session):
        self.token = token
        self.phone_number_id = phone_number_id
        self.recipient = recipient
        self.template_name = template_name
        self.template_language = template_language or 'pt_BR'
        self.http = http_session
        self.last_status = 'FAILED'

    @property
    def configured(self) -> bool:
        return all((self.token, self.phone_number_id, self.recipient, self.template_name))

    def send_job_alert(self, job: Any) -> DeliveryResult:
        if not self.configured:
            return DeliveryResult(self.channel, 'DISABLED', retryable=False)
        payload = {
            'messaging_product': 'whatsapp',
            'to': self.recipient,
            'type': 'template',
            'template': {
                'name': self.template_name,
                'language': {'code': self.template_language},
                'components': [{'type': 'body', 'parameters': [
                    {'type': 'text', 'text': str(getattr(job, 'title', 'Nova vaga'))},
                    {'type': 'text', 'text': str(getattr(job, 'company', ''))},
                ]}],
            },
        }
        try:
            response = self.http.post(
                self.endpoint.format(phone_number_id=self.phone_number_id),
                headers={'Authorization': f'Bearer {self.token}'},
                json=payload,
                timeout=10,
            )
            if response.status_code in {200, 201}:
                data = response.json() if response.content else {}
                message_id = ((data.get('messages') or [{}])[0]).get('id')
                self.last_status = 'DELIVERED'
                return DeliveryResult(self.channel, 'DELIVERED', provider_message_id=message_id)
            status = 'UNKNOWN' if response.status_code >= 500 else 'FAILED'
            self.last_status = status
            return DeliveryResult(self.channel, status, retryable=status == 'UNKNOWN')
        except (requests.RequestException, ValueError) as exc:
            self.last_status = 'UNKNOWN'
            return DeliveryResult(self.channel, 'UNKNOWN', retryable=True, error=type(exc).__name__)

    def send_heartbeat(self, monitors_count: int, jobs_tracked: int) -> DeliveryResult:
        return DeliveryResult(self.channel, 'DISABLED', retryable=False)


class WPPConnectAdapter:
    """Experimental WhatsApp Web adapter through a self-hosted WPPConnect server."""

    channel = 'wppconnect'

    def __init__(self, base_url: str | None, token: str | None,
                 session: str | None, recipient: str | None,
                 http_session: requests.Session):
        self.base_url = (base_url or '').rstrip('/')
        self.token = token
        self.session = session
        self.recipient = recipient
        self.http = http_session
        self.last_status = 'FAILED'

    @property
    def configured(self) -> bool:
        return all((self.base_url, self.token, self.session, self.recipient))

    def send_job_alert(self, job: Any) -> DeliveryResult:
        if not self.configured:
            return DeliveryResult(self.channel, 'DISABLED', retryable=False)
        from notify.job_card import build_card

        text, _ = build_card(job)
        endpoint = f'{self.base_url}/api/{self.session}/send-message'
        try:
            response = self.http.post(
                endpoint,
                headers={'Authorization': f'Bearer {self.token}',
                         'Content-Type': 'application/json'},
                json={'phone': self.recipient, 'message': text},
                timeout=10,
            )
            if response.status_code in {200, 201}:
                data = response.json() if response.content else {}
                message_id = data.get('id') if isinstance(data, dict) else None
                self.last_status = 'DELIVERED'
                return DeliveryResult(self.channel, 'DELIVERED', provider_message_id=message_id)
            status = 'UNKNOWN' if response.status_code >= 500 else 'FAILED'
            self.last_status = status
            return DeliveryResult(self.channel, status, retryable=status == 'UNKNOWN')
        except (requests.RequestException, ValueError) as exc:
            self.last_status = 'UNKNOWN'
            return DeliveryResult(self.channel, 'UNKNOWN', retryable=True, error=type(exc).__name__)

    def send_heartbeat(self, monitors_count: int, jobs_tracked: int) -> DeliveryResult:
        return DeliveryResult(self.channel, 'DISABLED', retryable=False)
