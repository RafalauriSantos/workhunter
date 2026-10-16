from core.telegram_inbox import extract_job_urls, resolve_linkedin_share, poll_manual_urls
from core.telegram_inbox import notify_finished_manual_analyses
from storage.sqlite_store import SQLiteStore
import json


def test_extract_job_urls_from_plain_message():
    assert extract_job_urls('olha https://www.linkedin.com/jobs/view/123/?trk=x.') == [
        'https://www.linkedin.com/jobs/view/123/?trk=x'
    ]


def test_poll_uses_configured_long_poll_timeout(tmp_path):
    store = SQLiteStore(tmp_path / 'state.db')
    calls = []

    class Response:
        status_code = 200

        def json(self):
            return {'result': []}

    class Http:
        def get(self, url, **kwargs):
            calls.append(kwargs)
            return Response()

    assert poll_manual_urls(store, Http(), 'token', '42', poll_timeout=45, request_timeout=60) == 0
    assert calls[0]['params']['timeout'] == 45
    assert calls[0]['timeout'] == 60


def test_poll_details_distinguish_command_from_url(tmp_path):
    store = SQLiteStore(tmp_path / 'state.db')

    class Response:
        status_code = 200

        def json(self):
            return {'result': [{'update_id': 1, 'message': {
                'chat': {'id': '42'}, 'from': {'id': '99'}, 'text': '/ajuda'
            }}]}

    class Http:
        def get(self, url, **kwargs):
            return Response()

        def post(self, *args, **kwargs):
            return Response()

    result = poll_manual_urls(store, Http(), 'token', '42', '99', return_details=True)
    assert result == {'handled': 1, 'urls_registered': 0}


def test_poll_details_returns_consistent_empty_result_for_http_error(tmp_path):
    store = SQLiteStore(tmp_path / 'state.db')

    class Response:
        status_code = 502

    class Http:
        def get(self, url, **kwargs):
            return Response()

    assert poll_manual_urls(store, Http(), 'token', '42', return_details=True) == {
        'handled': 0, 'urls_registered': 0,
    }


def test_generic_linkedin_page_is_not_queued(tmp_path):
    store = SQLiteStore(tmp_path / 'state.db')
    sent = []

    class Response:
        status_code = 200
        url = 'https://www.linkedin.com/feed/'

        def json(self):
            return {'result': [{'update_id': 5, 'message': {
                'chat': {'id': '42'}, 'from': {'id': '99'},
                'text': 'https://www.linkedin.com/feed/',
            }}]}

    class Http:
        def get(self, *args, **kwargs):
            return Response()

        def post(self, *args, **kwargs):
            sent.append(args)
            return Response()

    assert poll_manual_urls(store, Http(), 'token', '42', '99', return_details=True) == {
        'handled': 0, 'urls_registered': 0,
    }
    with store.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM manual_cases').fetchone()[0] == 0
    assert not sent


def test_plain_start_is_replied_to_without_entering_manual_queue(tmp_path):
    store = SQLiteStore(tmp_path / 'state.db')
    sent = []

    class Response:
        status_code = 200

        def json(self):
            return {'result': [{'update_id': 2, 'message': {
                'chat': {'id': '42'}, 'from': {'id': '99'}, 'text': 'Start'
            }}]}

    class Http:
        def get(self, url, **kwargs):
            return Response()

        def post(self, url, **kwargs):
            sent.append(kwargs['json'])
            return Response()

    assert poll_manual_urls(store, Http(), 'token', '42', '99') == 1
    assert 'Olá! Eu sou o WorkHunter' in sent[0]['text']


def test_command_send_falls_back_to_plain_text_on_telegram_error(tmp_path):
    store = SQLiteStore(tmp_path / 'state.db')
    sent = []

    class Response:
        def __init__(self, status_code):
            self.status_code = status_code

        def json(self):
            return {'result': []}

    class Http:
        def get(self, url, **kwargs):
            response = Response(200)
            response.json = lambda: {'result': [{'update_id': 1, 'message': {
                'chat': {'id': '42'}, 'from': {'id': '99'}, 'text': '/ajuda'
            }}]}
            return response

        def post(self, url, **kwargs):
            sent.append(kwargs['json'])
            return Response(400 if len(sent) == 1 else 200)

    assert poll_manual_urls(store, Http(), 'token', '42', '99') == 1
    assert len(sent) == 2
    assert 'parse_mode' not in sent[1]


def test_linkedin_safety_wrapper_uses_nested_share_url():
    class Response:
        url = 'https://www.linkedin.com/jobs/view/123456789/'
    class Http:
        def get(self, url, **kwargs):
            assert url == 'https://lnkd.in/example'
            return Response()
    wrapped = 'https://www.linkedin.com/safety/go/?url=https%3A%2F%2Flnkd.in%2Fexample'
    assert resolve_linkedin_share(wrapped, Http()) == Response.url


def test_linkedin_short_url_failure_is_not_enqueued():
    class Http:
        def get(self, *args, **kwargs):
            raise TimeoutError('redirect lookup timed out')

    assert resolve_linkedin_share('https://lnkd.in/example', Http()) == ''


def test_linkedin_resolver_rejects_non_linkedin_destination():
    assert resolve_linkedin_share('https://example.com/?url=https%3A%2F%2Fevil.example%2F', object()) == 'https://example.com/?url=https%3A%2F%2Fevil.example%2F'


def test_geekhunter_or_other_redirect_is_not_accepted():
    from collectors.geekhunter_collector import GeekHunterCollector
    assert not GeekHunterCollector._is_allowed_url('https://127.0.0.1/admin')
    assert not GeekHunterCollector._is_allowed_url('https://evil.example/jobs/1')


def test_finished_manual_message_matches_prefixed_source_id(tmp_path):
    store = SQLiteStore(tmp_path / 'state.db')
    with store.connect() as db:
        db.execute("insert into manual_cases(url,normalized_url,source,native_id,identity_status,manual_found_at) values (?,?,?,?,?,?)",
                   ('u','u','linkedin','123','identity_resolved','now'))
        case_id = db.execute('select id from manual_cases').fetchone()[0]
        db.execute("insert into manual_analysis_queue(manual_case_id,status,result,created_at,telegram_chat_id,telegram_message_id) values (?,?,?,?,?,?)",
                   (case_id,'DONE','fetched','now','1',10))
        db.execute("insert into events(kind,payload) values ('decision',?)",
                   (json.dumps({'job_id':'linkedin:123','source':'linkedin','decision':'DISCARD_LOW_SCORE','final_score':20,'decision_reason':'score baixo'}),))
    class Http:
        def post(self, *args, **kwargs):
            return type('Response', (), {'status_code': 200})()
    assert notify_finished_manual_analyses(store, Http(), 'token') == 1
