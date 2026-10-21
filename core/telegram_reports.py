"""Read-only Telegram reports built from durable WorkHunter cycle data."""
from __future__ import annotations

import json
import unicodedata
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo


LOCAL_ZONE = ZoneInfo("America/Sao_Paulo")
NON_FAILURE_SOURCE_STATUSES = {'SUCCESS', 'OK', 'NOT_CONFIGURED', 'THROTTLED', 'COOLDOWN'}


def _parse(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _rows(store, limit=20000):
    with store.connect() as db:
        return db.execute(
            "SELECT id,started,finished,status,report,mode FROM cycles ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()


def _reports_for_day(store, now=None):
    now = now or datetime.now(timezone.utc)
    local_now = now.astimezone(LOCAL_ZONE)
    start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    result = []
    for cycle_id, started, finished, status, raw_report, mode in _rows(store):
        parsed = _parse(started)
        if parsed and parsed.astimezone(LOCAL_ZONE).date() == start.date():
            try:
                report = json.loads(raw_report) if raw_report else {}
            except (TypeError, ValueError):
                report = {}
            # Older manual-only cycles predate the mode column. Their empty,
            # zero-duration reports are excluded from the collection report.
            legacy_empty_manual = (
                not mode and report.get('duration_seconds', 0) == 0
                and _number(report, 'funnel', 'raw') == 0
            )
            if legacy_empty_manual:
                continue
            result.append({
                'id': cycle_id, 'started': started, 'finished': finished,
                'status': status, 'report': report, 'mode': mode or 'scheduled',
            })
    return result


def _number(report, *path):
    value = report
    for key in path:
        if not isinstance(value, dict):
            return 0
        value = value.get(key, 0)
    return value if isinstance(value, (int, float)) else 0


def _friendly_datetime(value):
    parsed = _parse(value)
    if not parsed:
        return 'horário desconhecido'
    local = parsed.astimezone(LOCAL_ZONE)
    return local.strftime('%d/%m às %H:%M')


def _decision_time(value):
    # Decision timestamps are naive local wall-clock values, unlike cycle timestamps.
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return 'horário desconhecido'
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=LOCAL_ZONE)
    return parsed.astimezone(LOCAL_ZONE).strftime('%d/%m às %H:%M')


def _friendly_status(value):
    return {
        'COMPLETED': 'Concluído', 'DEGRADED': 'Concluído com alertas',
        'FAILED': 'Falhou', 'INTERRUPTED': 'Interrompido',
        'RUNNING': 'Em andamento', 'SUCCESS': 'Normal', 'OK': 'Normal',
        'NOT_CONFIGURED': 'Não configurada',
        'THROTTLED': 'Aguardando intervalo', 'COOLDOWN': 'Em intervalo de espera',
    }.get(value, str(value or 'Indisponível').replace('_', ' ').title())


def _source_failed(status):
    return bool(status) and str(status).upper() not in NON_FAILURE_SOURCE_STATUSES


def _scope_decisions_for_day(store, now):
    """Count persisted scope rejects, including older cycle reports without funnel counters."""
    local_date = now.astimezone(LOCAL_ZONE).date()
    with store.connect() as db:
        rows = db.execute("SELECT payload FROM events WHERE kind='decision'").fetchall()
    count = 0
    for (payload,) in rows:
        try:
            decision = json.loads(payload)
        except (TypeError, ValueError):
            continue
        if decision.get('decision') != 'DISCARD_SCOPE':
            continue
        timestamp = _parse(decision.get('timestamp'))
        # Decisions historically use datetime.now() without a timezone; those values
        # are local wall-clock timestamps, unlike cycle timestamps (which are UTC).
        if decision.get('timestamp') and not _has_timezone(decision['timestamp']):
            try:
                timestamp = datetime.fromisoformat(decision['timestamp']).replace(tzinfo=LOCAL_ZONE)
            except (TypeError, ValueError):
                timestamp = None
        if timestamp and timestamp.astimezone(LOCAL_ZONE).date() == local_date:
            count += 1
    return count


def _has_timezone(value):
    try:
        return datetime.fromisoformat(value).tzinfo is not None
    except (TypeError, ValueError):
        return False


def _rss_gft_timeout_summary(store, now):
    """Summarize today's failed GFT RSS queries without exposing raw exception text."""
    local_date = now.astimezone(LOCAL_ZONE).date()
    with store.connect() as db:
        rows = db.execute("""SELECT query,error_type,timed_out,reason,http_status,started
            FROM collection_attempts WHERE source='rss' AND operation='feed'""").fetchall()
    failed = 0
    query_labels = set()
    for query_raw, error_type, timed_out, reason, http_status, started in rows:
        timestamp = _parse(started)
        if not timestamp or timestamp.astimezone(LOCAL_ZONE).date() != local_date:
            continue
        reason_text = str(reason or '').lower()
        error_text = str(error_type or '').lower()
        is_timeout = bool(timed_out) or 'timeout' in error_text or 'timed out' in reason_text or 'timeout' in reason_text
        try:
            bad_http = http_status is not None and int(http_status) >= 400
        except (TypeError, ValueError):
            bad_http = False
        if not is_timeout:
            continue
        try:
            query = json.loads(query_raw) if query_raw else {}
        except (TypeError, ValueError):
            query = {}
        url = query.get('url', '') if isinstance(query, dict) else ''
        if urlparse(url).hostname != 'jobs.gft.com':
            continue
        failed += 1
        keywords = parse_qs(urlparse(url).query).get('keywords', [''])[0].lower()
        if 'java' in keywords:
            query_labels.add('Java')
        elif 'junior' in keywords or 'estagio' in keywords or 'trainee' in keywords:
            query_labels.add('Júnior/estágio')
        elif 'pleno' in keywords or 'mid' in keywords or 'intermediate' in keywords:
            query_labels.add('Pleno')
        elif 'developer' in keywords or 'desenvolvedor' in keywords or 'backend' in keywords:
            query_labels.add('Desenvolvimento')
        elif bad_http:
            query_labels.add('outra consulta')
    if not failed:
        return None
    queries = ', '.join(sorted(query_labels)) or 'consultas sem classificação'
    return f"RSS/GFT: {failed} timeout(s); consultas afetadas: {queries}"


def _friendly_source(value):
    return {
        'linkedin': 'LinkedIn', 'gupy': 'Gupy', 'github': 'GitHub',
        'geekhunter': 'GeekHunter', 'trampos': 'Trampos', 'rss': 'RSS',
    }.get(str(value).lower(), str(value).replace('_', ' ').title())


def _friendly_decision(value):
    return {
        'DISCARD_SCOPE': 'Descartada por compatibilidade',
        'DISCARD_LOCATION': 'Descartada por localização',
        'DISCARD_LOW_EVIDENCE': 'Descartada por pouca evidência',
        'DISCARD_LOW_SCORE': 'Descartada por pontuação',
        'DISCARD_PCD': 'Descartada pelo filtro PCD',
        'DISCARD_SENIOR_OR_VETO': 'Descartada por senioridade ou relevância',
        'DELIVERED': 'Alerta enviado', 'APPROVED': 'Aprovada',
    }.get(value, str(value or 'Decisão não identificada').replace('_', ' ').title())


def _day_summary(store, now):
    cycles = _reports_for_day(store, now)
    summary = {
        'cycles': len(cycles), 'raw': 0, 'notified': 0, 'already_known': 0, 'discarded': 0,
        'fallbacks': 0, 'degraded': 0, 'failed': 0, 'interrupted': 0,
        'source_failures': defaultdict(int), 'gft_timeout': _rss_gft_timeout_summary(store, now),
    }
    for cycle in cycles:
        report = cycle['report']
        summary['raw'] += _number(report, 'funnel', 'raw')
        summary['notified'] += _number(report, 'funnel', 'notified')
        funnel = report.get('funnel', {})
        summary['already_known'] += _number(funnel, 'seen')
        for value in (funnel.get('discarded') or {}).values():
            if isinstance(value, (int, float)):
                summary['discarded'] += value
        summary['fallbacks'] += _number(report, 'llm_fallbacks')
        if cycle['status'] == 'DEGRADED':
            summary['degraded'] += 1
        if cycle['status'] in {'FAILED', 'INTERRUPTED'}:
            summary['failed'] += 1
        if cycle['status'] == 'INTERRUPTED':
            summary['interrupted'] += 1
        for source, details in (report.get('sources') or {}).items():
            if isinstance(details, dict) and _source_failed(details.get('status')):
                summary['source_failures'][source] += 1
    recorded_scope_rejects = _scope_decisions_for_day(store, now)
    funnel_scope_rejects = sum(
        _number(cycle['report'], 'funnel', 'discarded', 'scope') for cycle in cycles
    )
    # Reconcile legacy daily reports that recorded DISCARD_SCOPE events but did not
    # persist the scope counter in the funnel. max avoids counting both copies.
    summary['discarded'] += max(0, recorded_scope_rejects - funnel_scope_rejects)
    return summary


def _problem_lines(summary):
    problems = []
    if summary['failed']:
        note = f" ({summary['interrupted']} interrompido(s))" if summary['interrupted'] else ''
        problems.append(f"{summary['failed']} ciclo(s) com falha{note}")
    if summary['degraded']:
        problems.append(f"{summary['degraded']} ciclo(s) degradado(s)")
    if summary['source_failures']:
        problems.append('Fontes: ' + ', '.join(
            _friendly_source(source) for source in sorted(summary['source_failures'])))
    if summary['gft_timeout']:
        problems.append(summary['gft_timeout'])
    if summary['fallbacks']:
        problems.append(f"IA: {summary['fallbacks']} fallback(s) heurístico(s)")
    return problems


def build_daily_report(store, now=None):
    now = now or datetime.now(timezone.utc)
    summary = _day_summary(store, now)
    header = (f"📊 <b>WorkHunter · {now.astimezone(LOCAL_ZONE).strftime('%d/%m às %H:%M')}</b>\n"
              f"{summary['raw']} vagas · {summary['notified']} alertas enviados · "
              f"{summary['already_known']} já conhecidas · {summary['discarded']} descartadas")
    problems = _problem_lines(summary)
    if not summary['cycles']:
        cycle_line = 'Nenhum ciclo registrado hoje'
    elif problems:
        cycle_line = f"{summary['cycles']} ciclo(s) hoje"
    else:
        cycle_line = f"{summary['cycles']} ciclo(s) sem falha"
    lines = [header, cycle_line]
    if problems:
        lines.extend(['', '⚠️ <b>Problemas</b>'])
        lines.extend(f"• {item}" for item in problems)
    return '\n'.join(lines)


def build_status(store):
    with store.connect() as db:
        cycle = db.execute(
            "SELECT started,finished,status,mode FROM cycles ORDER BY id DESC LIMIT 1"
        ).fetchone()
        pending = db.execute(
            "SELECT COUNT(*) FROM outbox WHERE status!='DELIVERED'"
        ).fetchone()[0]
        manual = db.execute(
            "SELECT COUNT(*) FROM manual_analysis_queue WHERE status='PENDING'"
        ).fetchone()[0]
    if not cycle:
        return '🩺 <b>WorkHunter</b>\n\nAguardando a primeira coleta.'
    started, _, status, _ = cycle
    attention = status in {'FAILED', 'INTERRUPTED', 'DEGRADED'} or pending or manual
    headline = '⚠️ <b>WorkHunter · atenção</b>' if attention else '✅ <b>WorkHunter · tudo certo</b>'
    lines = [headline, f"Última coleta: {_friendly_datetime(started)} · {_friendly_status(status)}"]
    if pending or manual:
        lines.append(f"Filas: {pending} entrega(s), {manual} análise(s) manual(is)")
    return '\n'.join(lines)


def build_sources(store, now=None):
    cycles = _reports_for_day(store, now)
    stats = defaultdict(lambda: {'cycles': 0, 'discovered': 0, 'failures': 0})
    for cycle in cycles:
        for source, details in (cycle['report'].get('sources') or {}).items():
            if not isinstance(details, dict):
                continue
            stats[source]['cycles'] += 1
            stats[source]['discovered'] += details.get('discovered', 0) or 0
            if _source_failed(details.get('status')):
                stats[source]['failures'] += 1
            elif details.get('status') in {'THROTTLED', 'COOLDOWN'}:
                stats[source]['waiting'] = stats[source].get('waiting', 0) + 1
    if not stats:
        return ('📡 <b>Fontes</b>\n\n'
                'Ainda não houve uma coleta programada registrada hoje.\n'
                'O monitor está aguardando o próximo horário.')
    total = sum(item['discovered'] for item in stats.values())
    lines = [f'📡 <b>Fontes · {total} vagas hoje</b>']
    for source in sorted(stats, key=lambda name: -stats[name]['discovered']):
        item = stats[source]
        line = f"• {_friendly_source(source)}: {item['discovered']}"
        if item['failures']:
            line += f" ⚠️ {item['failures']} ciclo(s) com falha"
        elif item.get('waiting'):
            line += ' · aguardando intervalo'
        lines.append(line)
    return '\n'.join(lines)


def build_latest(store):
    with store.connect() as db:
        rows = db.execute(
            "SELECT payload FROM events WHERE kind='decision' ORDER BY id DESC LIMIT 5"
        ).fetchall()
    if not rows:
        return '🧾 <b>Últimas decisões</b>\n\nNenhuma decisão registrada.'
    lines = ['🧾 <b>Últimas decisões</b>']
    for row in rows:
        try:
            item = json.loads(row[0])
        except (TypeError, ValueError):
            continue
        lines.append(f"• {_decision_time(item.get('timestamp'))} · {_friendly_decision(item.get('decision'))}")
        source = _friendly_source(item['source']) if item.get('source') else 'fonte desconhecida'
        reason = str(item.get('decision_reason') or '').strip()
        detail = f"  {source}"
        if reason:
            detail += f" · {reason[:120]}{'…' if len(reason) > 120 else ''}"
        lines.append(detail)
    return '\n'.join(lines)


def build_problems(store, now=None):
    now = now or datetime.now(timezone.utc)
    problems = _problem_lines(_day_summary(store, now))
    if not problems:
        return '✅ <b>Problemas</b>\n\nNenhum problema registrado hoje.'
    return '⚠️ <b>Problemas — hoje</b>\n\n' + '\n'.join(f'• {item}' for item in problems)


def command_response(command, store, now=None):
    command = command.strip().lower().split('@', 1)[0]
    command = ''.join(
        char for char in unicodedata.normalize('NFKD', command)
        if not unicodedata.combining(char)
    )
    if not command.startswith('/'):
        command = '/' + command
    responses = {
        '/start': lambda store: ('👋 <b>Olá! Eu sou o WorkHunter.</b>\n\n'
                                 'Posso monitorar vagas, analisar links e mostrar o estado do sistema.\n\n'
                                 + command_response('/ajuda', store, now)),
        '/relatorio': lambda current_store: build_daily_report(current_store, now),
        '/status': build_status,
        '/fontes': lambda current_store: build_sources(current_store, now),
        '/ultimas': build_latest,
        '/problemas': lambda current_store: build_problems(current_store, now),
    }
    if command == '/ajuda':
        return ('🤖 <b>Comandos do WorkHunter</b>\n\n'
                '• /relatorio — resumo do dia\n'
                '• /status — situação agora\n'
                '• /problemas — falhas de hoje\n'
                '• /ajuda — esta lista')
    builder = responses.get(command)
    return builder(store) if builder else None
