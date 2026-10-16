"""Minimal inbound Telegram adapter for manual job URLs."""
import json
import hmac
import logging
import os
import re
import time
import unicodedata
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote

URL_RE = re.compile(r'https?://[^\s<>]+', re.IGNORECASE)
LINKEDIN_HOSTS = {'linkedin.com', 'www.linkedin.com', 'lnkd.in'}
# Short links often lead to the ATS that owns the vacancy.  Keep this list
# explicit: accepting arbitrary redirects would turn manual intake into an
# SSRF/phishing surface.
TRUSTED_JOB_HOSTS = {
    'gupy.io', 'greenhouse.io', 'lever.co', 'trampos.co',
}
logger = logging.getLogger(__name__)


def _enroll_promotion_recipient(message, http, token):
    """Consume a one-time private deep link to register the grocery-alert chat.

    This runs before WorkHunter's existing single-chat authorization filter so
    a separately authorized recipient can connect without sharing numeric IDs.
    The random capability is local, one-time, expires after seven days, and is
    never written to logs or returned in responses.
    """
    text = (message.get('text') or '').strip().split()
    if len(text) != 2 or text[0].split('@', 1)[0] != '/start':
        return False
    payload = text[1]
    if not payload.startswith('promo_'):
        return False
    chat = message.get('chat', {})
    if chat.get('type') != 'private' or not chat.get('id'):
        return False

    config_dir = Path.home() / '.config' / 'lista-compras-alertas'
    invitation_file = config_dir / 'recipient-invitation.json'
    config_file = config_dir / 'secrets.env'
    try:
        invitation = json.loads(invitation_file.read_text(encoding='utf-8'))
        code = invitation.get('code', '')
        created_at = float(invitation.get('created_at', 0))
        if not code or time.time() - created_at > 7 * 86400 or not hmac.compare_digest(payload, code):
            return False

        chat_id = str(chat['id'])
        previous = config_file.read_text(encoding='utf-8') if config_file.exists() else ''
        lines = [line for line in previous.splitlines()
                 if not line.strip().startswith('PROMOTIONS_TELEGRAM_CHAT_ID=')]
        lines.append(f'PROMOTIONS_TELEGRAM_CHAT_ID={chat_id}')
        config_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = config_file.with_suffix('.env.tmp')
        temporary.write_text('\n'.join(lines).rstrip() + '\n', encoding='utf-8')
        os.chmod(temporary, 0o600)
        os.replace(temporary, config_file)
        invitation_file.unlink()
    except (OSError, ValueError, TypeError):
        logger.warning('Promotion recipient enrollment could not be saved')
        return False

    _send_command_response(http=http, token=token, chat_id=chat_id,
                           text='✅ Telegram conectado. Você receberá aqui o resumo diário de ofertas pela manhã.')
    logger.info('A grocery-promotion recipient connected through a one-time invite')
    return True


def _send_command_response(http, token, chat_id, text):
    """Send a command response with a plain-text fallback for Telegram errors."""
    endpoint = f'https://api.telegram.org/bot{token}/sendMessage'
    try:
        started = time.perf_counter()
        response = http.post(endpoint, json={
            'chat_id': chat_id, 'text': text, 'parse_mode': 'HTML'
        }, timeout=10)
        logger.info('Telegram response API latency=%.3fs status=%s',
                    time.perf_counter() - started, response.status_code)
        if response.status_code == 200:
            return True
        logger.warning('Telegram command response failed with HTTP %s', response.status_code)
        plain = re.sub(r'<[^>]+>', '', text)
        fallback = http.post(endpoint, json={'chat_id': chat_id, 'text': plain}, timeout=10)
        return fallback.status_code == 200
    except Exception as exc:
        logger.warning('Telegram command response failed: %s', exc)
        return False


def _safe_linkedin_url(url):
    parsed = urlparse(url or '')
    host = (parsed.hostname or '').lower().rstrip('.')
    return (parsed.scheme.lower() in {'http', 'https'} and
            (host in LINKEDIN_HOSTS or host.endswith('.linkedin.com')) and
            host not in {'localhost', '127.0.0.1', '::1', '0.0.0.0'})


def _safe_job_destination(url):
    parsed = urlparse(url or '')
    host = (parsed.hostname or '').lower().rstrip('.')
    return (parsed.scheme.lower() in {'http', 'https'} and
            (host in TRUSTED_JOB_HOSTS or
             any(host.endswith('.' + suffix) for suffix in TRUSTED_JOB_HOSTS)))


def extract_job_urls(text):
    return [url.rstrip('.,);]}>') for url in URL_RE.findall(text or '')
            if urlparse(url).scheme in {'http', 'https'}]


def resolve_linkedin_share(url, http):
    """Follow LinkedIn's safety wrapper/short link to expose the job URL."""
    parsed = urlparse(url)
    nested = parse_qs(parsed.query).get('url', [])
    candidate = unquote(nested[0]) if nested else url
    if not (_safe_linkedin_url(url) and _safe_linkedin_url(candidate)):
        return url
    candidate_parts = urlparse(candidate)
    if candidate_parts.hostname and candidate_parts.hostname.lower().rstrip('.') == 'lnkd.in':
        try:
            response = http.get(candidate, allow_redirects=True, timeout=10,
                                headers={'User-Agent': 'Mozilla/5.0'})
            resolved = getattr(response, 'url', '') or ''
            if _safe_linkedin_url(resolved) or _safe_job_destination(resolved):
                return resolved
            return ''
        except Exception:
            return ''
    try:
        response = http.get(candidate, allow_redirects=True, timeout=10,
                            headers={'User-Agent': 'Mozilla/5.0'})
        resolved = getattr(response, 'url', '') or candidate
        if not (_safe_linkedin_url(resolved) or _safe_job_destination(resolved)):
            return ''
        return resolved
    except Exception:
        return candidate


def _is_generic_linkedin_page(url):
    parsed = urlparse(url or '')
    host = (parsed.hostname or '').lower().rstrip('.')
    path = parsed.path.rstrip('/').lower() or '/'
    return (host == 'linkedin.com' or host.endswith('.linkedin.com')) and path in {
        '/', '/feed', '/notifications', '/mynetwork', '/messaging', '/jobs',
    }


def poll_manual_urls(store, http, token, chat_id, allowed_user_id=None,
                     poll_timeout=0, request_timeout=10, return_details=False):
    """Consume authorized messages, commands and URLs; returns items handled.

    ``poll_timeout`` enables Telegram long polling. A non-zero value keeps the
    connection open until a message arrives or the server-side wait expires,
    avoiding a tight loop when the inbox is quiet.
    """
    empty_result = {'handled': 0, 'urls_registered': 0} if return_details else 0
    if not token or not chat_id or http is None:
        return empty_result
    with store.connect() as db:
        row = db.execute("SELECT value FROM metadata WHERE key='telegram_update_offset'").fetchone()
        offset = int(row[0]) if row else 0
    response = http.get(f'https://api.telegram.org/bot{token}/getUpdates',
                        params={'offset': offset, 'timeout': poll_timeout,
                                'allowed_updates': '["message"]'},
                        timeout=request_timeout)
    if response.status_code != 200:
        logger.warning('Telegram update polling failed with HTTP %s', response.status_code)
        return empty_result
    updates = response.json().get('result', [])
    registered = 0
    urls_registered = 0
    next_offset = offset
    for update in updates:
        next_offset = max(next_offset, int(update.get('update_id', 0)) + 1)
        message = update.get('message', {})
        if _enroll_promotion_recipient(message, http, token):
            registered += 1
            continue
        sender_chat = str(message.get('chat', {}).get('id', ''))
        if sender_chat != str(chat_id):
            continue
        if allowed_user_id and str(message.get('from', {}).get('id', '')) != str(allowed_user_id):
            continue
        message_text = message.get('text', '') or ''
        received_at = time.time()
        telegram_at = message.get('date')
        if telegram_at:
            logger.info('Telegram message received transport_latency=%.3fs',
                        max(0.0, received_at - float(telegram_at)))
        command = message_text.strip().split()[0] if message_text.strip() else ''
        from core.telegram_reports import command_response
        report = command_response(command, store) if command else None
        if report:
            _send_command_response(http, token, chat_id, report)
            registered += 1
            continue
        normalized_command = ''.join(
            char for char in unicodedata.normalize('NFKD', command.lower())
            if not unicodedata.combining(char)
        )
        if command.startswith('/') or normalized_command in {
                'start', 'relatorio', 'status', 'fontes', 'ultimas', 'problemas', 'ajuda'}:
            _send_command_response(
                http, token, chat_id,
                '🤖 Não reconheci esse comando. Envie /ajuda para ver as opções.'
            )
            registered += 1
            continue
        for url in extract_job_urls(message_text):
            resolved_url = resolve_linkedin_share(url, http)
            if not resolved_url or _is_generic_linkedin_page(resolved_url):
                continue
            queue_id = store.record_manual_case(resolved_url)
            registered += 1
            urls_registered += 1
            response = http.post(f'https://api.telegram.org/bot{token}/sendMessage',
                      json={'chat_id': chat_id, 'text': '🔎 Link recebido. Identificando a fonte e iniciando a análise...'}, timeout=10)
            try:
                message_id = response.json()['result']['message_id']
                with store.connect() as db:
                    db.execute('UPDATE manual_analysis_queue SET telegram_chat_id=?,telegram_message_id=? WHERE id=?',
                               (str(chat_id), message_id, queue_id))
            except (ValueError, KeyError, TypeError):
                pass
    if next_offset != offset:
        with store.connect() as db:
            db.execute("INSERT INTO metadata(key,value) VALUES('telegram_update_offset',?) "
                       "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(next_offset),))
    if return_details:
        return {'handled': registered, 'urls_registered': urls_registered}
    return registered


def notify_finished_manual_analyses(store, http, token):
    if not token or http is None:
        return 0
    with store.connect() as db:
        rows = db.execute('''SELECT q.id,q.telegram_chat_id,q.telegram_message_id,q.result,q.error,
            c.source,c.native_id,c.normalized_url FROM manual_analysis_queue q
            JOIN manual_cases c ON c.id=q.manual_case_id
            WHERE q.status IN ('DONE','FAILED') AND q.notified_at IS NULL
            AND q.telegram_chat_id IS NOT NULL''').fetchall()
        decisions = [json.loads(row[0]) for row in db.execute("SELECT payload FROM events WHERE kind='decision'").fetchall()]
    sent = 0
    for qid, chat, message, result, error, source, native_id, url in rows:
        candidate_ids = {str(native_id), f'{source}:{native_id}'}
        matches = [d for d in decisions if str(d.get('job_id')) in candidate_ids and d.get('source') == source]
        decision = matches[-1] if matches else None
        if decision:
            label = decision.get('decision', 'UNKNOWN')
            reason = decision.get('decision_reason', 'não informado')
            if label == 'DISCARD_LOCATION':
                text = ("⚠️ Esta vaga foi descartada pelo filtro de localização.\n\n"
                        f"Motivo: {reason}\n\n"
                        "A modalidade remota não ficou confirmada ou a localidade está fora da região aceita.")
            elif label == 'DISCARD_LOW_EVIDENCE':
                text = ("⚠️ Não consegui confirmar informações suficientes sobre esta vaga.\n\n"
                        f"Motivo: {reason}\n\n"
                        "O LinkedIn não liberou detalhes públicos suficientes para uma avaliação segura.")
            else:
                text = f"{'✅' if label in {'DELIVERED','APPROVED'} else '❌'} Análise concluída.\n\nDecisão: {label}\nScore: {decision.get('final_score', 'N/D')}\nMotivo: {reason}"
        elif error:
            text = f"⚠️ Não consegui concluir a análise.\n\nEtapa: busca da vaga\nMotivo: {error}"
        else:
            seen_ids = {str(value) for value in store.state.get('seen_ids', [])}
            if str(native_id) in seen_ids or f'{source}:{native_id}' in seen_ids:
                text = ('ℹ️ Esta vaga já havia sido processada anteriormente.\n\n'
                        'Ela foi ignorada agora para evitar duplicidade.\n'
                        f'Evidência obtida: {result or "não registrada"}.')
            else:
                text = ('⚠️ A vaga foi buscada, mas a decisão final não foi registrada.\n\n'
                        f'Evidência obtida: {result or "não registrada"}.\n'
                        'Ela ficará disponível para nova tentativa.')
        response = http.post(f'https://api.telegram.org/bot{token}/editMessageText',
                             json={'chat_id': chat, 'message_id': message, 'text': text}, timeout=10)
        if response.status_code == 200:
            with store.connect() as db:
                db.execute('UPDATE manual_analysis_queue SET notified_at=datetime(?) WHERE id=?',
                           ('now', qid))
            sent += 1
    return sent
