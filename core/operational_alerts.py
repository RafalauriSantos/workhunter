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


def update_source_alerts(store, statuses, sender):
    states = store.state.setdefault('source_alerts', {})
    non_failure_statuses = {'THROTTLED', 'COOLDOWN'}
    for source, status in statuses.items():
        if status == 'NOT_CONFIGURED' or status in non_failure_statuses:
            continue
        state = states.setdefault(source, {'failures': 0, 'alerted': False})
        label = {'linkedin': 'LinkedIn'}.get(source, source)
        if status != 'OK':
            state['failures'] += 1
            if state['failures'] >= 2 and not state['alerted']:
                if status == 'PARTIAL':
                    message = (
                        f'Job Finder: {label} degraded in consecutive cycles. '
                        'Some searches were rate-limited (HTTP 429); collected results were preserved '
                        'and the remaining searches will rotate in the next cycle. Check local diagnostics.'
                    )
                else:
                    message = f'Job Finder: {label} failed in consecutive cycles. Check local diagnostics.'
                state['alerted'] = bool(sender(message))
        else:
            state['failures'] = 0
            if state['alerted'] and sender(f'Job Finder: {label} recovered; collection is back to normal.'):
                state['alerted'] = False
    store.save()
