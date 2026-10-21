"""Small operational messages; never include exception strings or credentials."""
import os


def send_operational(http, message):
    if os.getenv('JOB_FINDER_SIMULATION') == '1':
        return False
    token, chat = os.getenv('TELEGRAM_BOT_TOKEN'), os.getenv('TELEGRAM_CHAT_ID')
    if token and chat:
        try:
            response = http.post(f'https://api.telegram.org/bot{token}/sendMessage',
                                 json={'chat_id': chat, 'text': message}, timeout=10)
            if response.status_code == 200:
                return True
            if response.status_code >= 500:
                return False
        except Exception:
            return False
    key, sender, recipient = (os.getenv(name) for name in
                              ('RESEND_API_KEY', 'ALERT_EMAIL_FROM', 'ALERT_EMAIL_TO'))
    if key and sender and recipient:
        try:
            response = http.post('https://api.resend.com/emails',
                                 headers={'Authorization': f'Bearer {key}'},
                                 json={'from': sender, 'to': [recipient],
                                       'subject': 'Job Finder: operational alert', 'text': message}, timeout=10)
            return response.status_code in (200, 201)
        except Exception:
            pass
    return False


def update_source_alerts(store, statuses, sender, diagnostics=None):
    states = store.state.setdefault('source_alerts', {})
    non_failure_statuses = {'THROTTLED', 'COOLDOWN'}
    diagnostics = diagnostics or {}
    for source, status in statuses.items():
        if status == 'NOT_CONFIGURED' or status in non_failure_statuses:
            continue
        state = states.setdefault(source, {'failures': 0, 'alerted': False})
        label = {'linkedin': 'LinkedIn'}.get(source, source)
        if status != 'OK':
            state['failures'] += 1
            if state['failures'] >= 2 and not state['alerted']:
                summary = format_source_diagnostic(diagnostics.get(source, {}))
                source_label = {
                    'gupy': 'Gupy', 'linkedin': 'LinkedIn', 'rss': 'RSS',
                    'github': 'GitHub', 'trampos': 'Trampos.co',
                }.get(source, source.capitalize())
                if status == 'PARTIAL':
                    message = (f'⚠️ WorkHunter · coleta parcial\n'
                               f'Fonte: {source_label} · falha em ciclos consecutivos\n'
                               'Os resultados coletados foram preservados.')
                else:
                    message = (f'⚠️ WorkHunter · coleta com falha\n'
                               f'Fonte: {source_label} · falha em ciclos consecutivos')
                if summary:
                    message += f'\nDetalhe: {summary}.'
                message += '\nAção: nova tentativa no próximo ciclo.'
                state['alerted'] = bool(sender(message))
        else:
            state['failures'] = 0
            source_label = {
                'gupy': 'Gupy', 'linkedin': 'LinkedIn', 'rss': 'RSS',
                'github': 'GitHub', 'trampos': 'Trampos.co',
            }.get(source, source.capitalize())
            message = (f'✅ WorkHunter · coleta normalizada\n'
                       f'Fonte: {source_label} voltou a responder.')
            if state['alerted'] and sender(message):
                state['alerted'] = False
    store.save()


def format_source_diagnostic(counts):
    """Describe only failure classes backed by recorded request attempts."""
    parts = []
    if counts.get('http_429'):
        parts.append(f"limite 429: {int(counts['http_429'])}")
    if counts.get('timeouts'):
        parts.append(f"timeouts: {int(counts['timeouts'])}")
    if counts.get('other_http_errors'):
        parts.append(f"outros erros HTTP: {int(counts['other_http_errors'])}")
    if counts.get('other_errors'):
        parts.append(f"outros erros: {int(counts['other_errors'])}")
    return ' · '.join(parts)


def summarize_collection_attempts(attempts):
    """Count observed failures by source without inferring causes from status labels."""
    summary = {}
    for attempt in attempts:
        source = str(attempt.get('source') or '')
        if not source:
            continue
        counts = summary.setdefault(source, {
            'http_429': 0, 'timeouts': 0,
            'other_http_errors': 0, 'other_errors': 0,
        })
        status_code = attempt.get('http_status')
        try:
            status_code = int(status_code) if status_code is not None else None
        except (TypeError, ValueError):
            status_code = None
        if status_code == 429:
            counts['http_429'] += 1
        elif status_code is not None and status_code >= 400:
            counts['other_http_errors'] += 1
        elif attempt.get('timed_out'):
            counts['timeouts'] += 1
        elif attempt.get('error_type'):
            counts['other_errors'] += 1
    return summary
