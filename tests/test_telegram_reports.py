import json
from datetime import datetime, timezone

from core.telegram_inbox import poll_manual_urls
from core.telegram_reports import command_response
from storage.sqlite_store import SQLiteStore


def _store(tmp_path):
    store = SQLiteStore(tmp_path / 'state.db')
    report = {
        'duration_seconds': 30.0,
        'sources': {
            'linkedin': {'status': 'SUCCESS', 'discovered': 4},
            'gupy': {'status': 'DEGRADED', 'discovered': 2},
        },
        'funnel': {
            'raw': 6, 'unique': 5, 'seen': 1,
            'discarded': {'location': 1, 'score': 1}, 'notified': 2,
        },
        'llm_calls_today': 2,
        'llm_fallbacks': 1,
    }
    with store.connect() as db:
        db.execute('INSERT INTO cycles(started,finished,status,report,mode) VALUES (?,?,?,?,?)',
                   ('2026-10-05T12:00:00+00:00', '2026-10-05T12:00:30+00:00',
                    'DEGRADED', json.dumps(report), 'scheduled'))
    return store


def test_daily_report_aggregates_persisted_cycle(tmp_path):
    report = command_response(
        '/relatorio', _store(tmp_path),
        now=datetime(2026, 10, 5, 13, 0, tzinfo=timezone.utc),
    )
    assert '1 ciclo(s) hoje' in report
    assert '6 vagas' in report
    assert '2 alertas enviados' in report
    assert 'Gupy' in report


def test_daily_report_separates_seen_from_rejections_and_reconciles_scope(tmp_path):
    store = _store(tmp_path)
    with store.connect() as db:
        db.execute("UPDATE cycles SET report=?", (json.dumps({
            'duration_seconds': 30,
            'sources': {
                'linkedin': {'status': 'THROTTLED', 'discovered': 0},
                'rss': {'status': 'PARTIAL', 'discovered': 2},
            },
            'funnel': {
                'raw': 4, 'unique': 4, 'seen': 2,
                'discarded': {'location': 1}, 'notified': 0,
            },
        }),))
        db.execute("INSERT INTO events(kind,payload) VALUES ('decision',?)", (json.dumps({
            'decision': 'DISCARD_SCOPE', 'timestamp': '2026-10-05T10:30:00',
        }),))
    report = command_response(
        '/relatorio', store,
        now=datetime(2026, 10, 5, 13, 0, tzinfo=timezone.utc),
    )

    assert '2 já conhecidas' in report
    assert '2 descartadas' in report
    assert 'LinkedIn' not in report.split('Fontes: ', 1)[1].split('\n', 1)[0]
    assert 'RSS' in report.split('Fontes: ', 1)[1].split('\n', 1)[0]


def test_daily_report_explains_gft_timeouts_by_query(tmp_path):
    store = _store(tmp_path)
    started = '2026-10-05T12:30:00+00:00'
    for query, reason, error_type, timed_out in (
        ('(java) AND locationSearch:(Brazil)', 'Read timed out', 'ConnectionError', False),
        ('(junior OR jr OR estagio) AND locationSearch:(Brazil)', 'ReadTimeoutError', 'ConnectionError', False),
        ('(developer OR backend) AND locationSearch:(Brazil)', '', None, False),
    ):
        store.record_collection_attempt({
            'cycle_id': 'cycle-1', 'source': 'rss', 'operation': 'feed',
            'query': {'url': 'https://jobs.gft.com/services/rss/job/?locale=en_US&keywords=' + query},
            'started': started, 'finished': started, 'error_type': error_type,
            'reason': reason, 'timed_out': timed_out,
        })

    report = command_response(
        '/relatorio', store,
        now=datetime(2026, 10, 5, 13, 0, tzinfo=timezone.utc),
    )

    assert 'RSS/GFT: 2 timeout(s)' in report
    assert 'Java' in report
    assert 'Júnior/estágio' in report
    assert 'ReadTimeout' not in report


def test_sources_and_problems_treat_throttle_as_waiting_not_failure(tmp_path):
    from core.telegram_reports import build_problems, build_sources

    store = _store(tmp_path)
    with store.connect() as db:
        db.execute("UPDATE cycles SET report=?", (json.dumps({
            'duration_seconds': 30,
            'sources': {
                'linkedin': {'status': 'THROTTLED', 'discovered': 0},
                'rss': {'status': 'OK', 'discovered': 3},
            },
            'funnel': {'raw': 3, 'unique': 3, 'seen': 0, 'discarded': {}, 'notified': 0},
        }),))
    now = datetime(2026, 10, 5, 13, 0, tzinfo=timezone.utc)

    assert 'aguardando intervalo' in build_sources(store, now)
    assert 'LinkedIn: Aguardando intervalo' not in build_problems(store, now)


def test_commands_have_expected_responses(tmp_path):
    store = _store(tmp_path)
    assert 'Olá! Eu sou o WorkHunter' in command_response('/start', store)
    assert 'Comandos do WorkHunter' in command_response('/ajuda', store)
    assert 'Última coleta' in command_response('/status', store)
    assert 'WorkHunter' in command_response('/status', store)
    assert 'LinkedIn' in command_response(
        '/fontes', store, now=datetime(2026, 10, 5, 13, 0, tzinfo=timezone.utc)
    )
    assert 'Últimas decisões' in command_response('/ultimas', store)
    assert 'Problemas' in command_response('/problemas', store)


def test_unknown_command_is_ignored(tmp_path):
    assert command_response('/desconhecido', _store(tmp_path)) is None


def test_plain_word_alias_is_supported(tmp_path):
    assert 'WorkHunter ·' in command_response('Relatorio', _store(tmp_path))
    assert 'WorkHunter ·' in command_response('Relatório', _store(tmp_path))
    assert 'WorkHunter ·' in command_response('/relatório@Radarvagas_Rafael_bot', _store(tmp_path))


def test_all_command_aliases_are_accent_and_case_tolerant(tmp_path):
    store = _store(tmp_path)
    commands = {
        'STATUS': 'Última coleta',
        'FÔNTES': 'Fontes',
        'ÚLTIMAS': 'Últimas decisões',
        'PROBLEMAS': 'Problemas',
        'AJÚDA': 'Comandos do WorkHunter',
    }
    for command, expected in commands.items():
        assert expected in command_response(command, store)


def test_telegram_command_is_replied_to_without_entering_manual_queue(tmp_path):
    store = _store(tmp_path)

    class Response:
        status_code = 200

        def json(self):
            return {'result': [{'update_id': 7, 'message': {
                'chat': {'id': '42'}, 'from': {'id': '99'}, 'text': '/ajuda'
            }}]}

    class Http:
        def __init__(self):
            self.posts = []

        def get(self, url, **kwargs):
            return Response()

        def post(self, url, **kwargs):
            self.posts.append(kwargs['json'])
            return Response()

    http = Http()
    assert poll_manual_urls(store, http, 'token', '42', '99') == 1
    assert 'Comandos do WorkHunter' in http.posts[0]['text']
    with store.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM manual_analysis_queue').fetchone()[0] == 0
