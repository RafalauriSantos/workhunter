from collectors.manual_collector import ManualCollector
from storage.sqlite_store import SQLiteStore


class Response:
    status_code = 200
    url = 'https://www.linkedin.com/jobs/view/123'
    text = '<title>Backend Junior | LinkedIn</title><meta name="description" content="Python e APIs. ' + 'x' * 80 + '">'


class Session:
    def get(self, *args, **kwargs):
        return Response()


def test_manual_collector_reuses_public_description_enrichment(tmp_path):
    store = SQLiteStore(tmp_path / 'state.db')
    store.record_manual_case('https://www.linkedin.com/jobs/view/123')
    jobs = ManualCollector(Session(), store).collect()
    assert jobs[0].description
    with store.connect() as db:
        assert db.execute('select result from manual_analysis_queue').fetchone()[0] == 'FULL_EVIDENCE'


def test_manual_collector_does_not_turn_linkedin_login_page_into_a_job(tmp_path):
    store = SQLiteStore(tmp_path / 'state.db')
    store.record_manual_case('https://www.linkedin.com/jobs/view/456')

    class LoginSession:
        def get(self, *args, **kwargs):
            return type('LoginResponse', (), {
                'status_code': 200,
                'url': 'https://www.linkedin.com/uas/login',
                'text': '<title>LinkedIn Login, Sign in</title>',
            })()

    assert ManualCollector(LoginSession(), store).collect() == []
    with store.connect() as db:
        assert db.execute('select status,result from manual_analysis_queue').fetchone() == (
            'DONE', 'SOURCE_LOGIN_REQUIRED'
        )
