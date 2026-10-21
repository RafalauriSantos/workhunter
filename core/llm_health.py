"""Persisted, non-invasive view of the last real LLM provider attempts."""
import json
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo


PROVIDERS = {
    'gemini': ('Gemini', 'GEMINI_API_KEY'),
    'openrouter': ('OpenRouter', 'OPENROUTER_API_KEY'),
}
FRESH_FOR = timedelta(hours=24)
LOCAL_ZONE = ZoneInfo('America/Sao_Paulo')


def record_attempt(store, provider, model, status, at=None):
    """Store provider outcome metadata only; never persist credentials or prompts."""
    event = {
        'provider': provider,
        'model': model,
        'status': status,
        'timestamp': (at or datetime.now(timezone.utc)).isoformat(),
    }
    if hasattr(store, 'event'):
        store.event('llm_health', event)
    return event


def record_configuration(store, at=None):
    """Persist configured provider names so the separate inbox process can report them."""
    providers = [name for name, (_, key) in PROVIDERS.items() if os.getenv(key)]
    if hasattr(store, 'event'):
        store.event('llm_configuration', {
            'providers': providers,
            'timestamp': (at or datetime.now(timezone.utc)).isoformat(),
        })
    return providers


def latest_provider_attempts(store):
    try:
        with store.connect() as db:
            rows = db.execute(
                "SELECT payload FROM events WHERE kind='llm_health' ORDER BY id DESC LIMIT 50"
            ).fetchall()
    except Exception:
        return {}
    latest = {}
    for (raw,) in rows:
        try:
            event = json.loads(raw)
            provider = event.get('provider')
            if provider in PROVIDERS and provider not in latest:
                latest[provider] = event
        except (TypeError, ValueError):
            continue
    return latest


def configured_providers(store):
    try:
        with store.connect() as db:
            row = db.execute(
                "SELECT payload FROM events WHERE kind='llm_configuration' ORDER BY id DESC LIMIT 1"
            ).fetchone()
        if row:
            payload = json.loads(row[0])
            return [name for name in payload.get('providers', []) if name in PROVIDERS]
    except Exception:
        pass
    return [name for name, (_, key) in PROVIDERS.items() if os.getenv(key)]


def _legacy_cycle_summary(store, now):
    """Use existing counters until provider-specific telemetry is available."""
    try:
        with store.connect() as db:
            rows = db.execute(
                'SELECT started,report FROM cycles ORDER BY id DESC LIMIT 1000'
            ).fetchall()
    except Exception:
        return None
    today = now.astimezone(LOCAL_ZONE).date()
    calls = 0
    latest_fallbacks = None
    has_metrics = False
    for started, raw_report in rows:
        try:
            timestamp = datetime.fromisoformat(started)
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)
            if timestamp.astimezone(LOCAL_ZONE).date() != today:
                continue
            report = json.loads(raw_report or '{}')
        except (TypeError, ValueError):
            continue
        has_metrics = has_metrics or 'llm_calls_today' in report or 'llm_fallbacks' in report
        calls = max(calls, int(report.get('llm_calls_today') or 0))
        if latest_fallbacks is None:
            latest_fallbacks = int(report.get('llm_fallbacks') or 0)
    if not has_metrics:
        return None
    return calls, latest_fallbacks or 0


def format_health(store, now=None):
    """Report only recent real call outcomes; never makes a probe/API call."""
    now = now or datetime.now(timezone.utc)
    configured = configured_providers(store)
    if not configured:
        return 'IA: análise heurística ativa; nenhum provedor configurado'

    latest = latest_provider_attempts(store)
    if not latest:
        legacy = _legacy_cycle_summary(store, now)
        if legacy:
            calls, fallbacks = legacy
            return (f'IA: provedor não individualizado · {calls} análises bem-sucedidas hoje · '
                    f'{fallbacks} fallback(s) no último ciclo')
        return 'IA: aguardando uma chamada real (' + ' · '.join(
            PROVIDERS[name][0] for name in configured
        ) + ')'
    parts = []
    for provider in configured:
        label = PROVIDERS[provider][0]
        event = latest.get(provider)
        if not event:
            parts.append(f'{label} sem chamada individual registrada')
            continue
        try:
            timestamp = datetime.fromisoformat(event['timestamp'])
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)
        except (KeyError, TypeError, ValueError):
            parts.append(f'{label} com estado desconhecido')
            continue
        age = now - timestamp.astimezone(timezone.utc)
        if age < timedelta(0) or age > FRESH_FOR:
            parts.append(f'{label} sem chamada recente')
        elif event.get('status') == 'OK':
            parts.append(f'{label} OK há {_age_text(age)}')
        else:
            parts.append(f'{label} falhou há {_age_text(age)}')
    return 'IA: ' + ' · '.join(parts)


def _age_text(age):
    seconds = max(0, int(age.total_seconds()))
    if seconds < 60:
        return 'menos de 1 min'
    minutes = seconds // 60
    if minutes < 60:
        return f'{minutes} min'
    hours = minutes // 60
    if hours < 24:
        return f'{hours} h'
    return f'{hours // 24} d'
