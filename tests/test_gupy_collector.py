import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from collectors.gupy_collector import GupyCollector


class MockResponse:
    status_code = 200
    encoding = "utf-8"

    def __init__(self):
        self.text = "data: " + json.dumps({
            "result": {
                "content": [{
                    "text": json.dumps({
                        "data": {"data": [{
                            "id": 42,
                            "name": "Desenvolvedor Java",
                            "careerPageName": "Empresa Tech",
                            "city": "Sorocaba",
                            "state": "SP",
                            "jobUrl": "https://empresa.gupy.io/jobs/42",
                            "description": "Java e Spring",
                        }]}
                    })
                }]
            }
        })


class MockSession:
    def __init__(self):
        self.calls = []

    def post(self, *args, **kwargs):
        self.calls.append(kwargs)
        return MockResponse()


def test_gupy_collector_records_query_parameters_and_result_count():
    collector = GupyCollector(MockSession(), [{"term": "Java", "limit": 20}])

    jobs = collector.collect()

    assert len(jobs) == 1
    assert collector.query_stats == [{
        "parameters": {"term": "Java", "limit": 20},
        "results": 1,
        "status": "OK",
    }]


def test_gupy_collector_distinguishes_http_failure_from_empty_result():
    class FailedSession:
        def post(self, *args, **kwargs):
            response = MockResponse()
            response.status_code = 503
            return response

    collector = GupyCollector(FailedSession(), [{"term": "Java"}])

    assert collector.collect() == []
    assert collector.query_stats[0]["status"] == "HTTP_503"


def test_gupy_collector_forwards_seniority_and_recent_window():
    session = MockSession()
    collector = GupyCollector(session, [{
        "term": "developer",
        "seniority": "mid",
        "published_within_hours": 24,
        "query_id": "internal-id",
    }])

    collector.collect()

    arguments = session.calls[0]["json"]["params"]["arguments"]
    assert arguments["seniority"] == "mid"
    assert arguments["published_within_hours"] == 24
    assert "query_id" not in arguments


def test_gupy_collector_enriches_missing_description_with_job_detail():
    class DetailSession(MockSession):
        def post(self, *args, **kwargs):
            self.calls.append(kwargs)
            if kwargs["json"]["params"]["name"] == "get_job_by_id":
                response = MockResponse()
                response.text = "data: " + json.dumps({
                    "result": {"content": [{"text": json.dumps({
                        "data": {"job": {"description": "React, TypeScript e Node.js"}}
                    })}]}
                })
                return response
            response = MockResponse()
            response.text = response.text.replace('Java e Spring', '')
            return response

    collector = GupyCollector(DetailSession(), [{"term": "React"}], detail_limit=1)
    jobs = collector.collect()

    assert jobs[0].description == "React, TypeScript e Node.js"


def test_gupy_detail_request_sends_numeric_job_id_to_mcp():
    session = MockSession()
    collector = GupyCollector(session, [], detail_limit=1)

    collector._get_detail('10939233')

    arguments = session.calls[0]['json']['params']['arguments']
    assert arguments['id'] == 10939233
    assert isinstance(arguments['id'], int)


def test_gupy_detail_response_is_decoded_as_utf8():
    class Response:
        status_code = 200
        encoding = 'ISO-8859-1'

        def __init__(self):
            payload = {'result': {'content': [{'text': json.dumps({
                'data': {'id': 12601484, 'name': 'Desenvolvedor Júnior'}
            }, ensure_ascii=False)}]}}
            self.content = ('data: ' + json.dumps(payload, ensure_ascii=False)).encode('utf-8')

        @property
        def text(self):
            return self.content.decode(self.encoding)

    class Session:
        def post(self, *args, **kwargs):
            return Response()

    collector = GupyCollector(Session(), [], detail_limit=1)

    detail = collector._get_detail('12601484')

    assert detail['name'] == 'Desenvolvedor Júnior'


def test_gupy_enriches_missing_publication_date_from_flat_detail_payload():
    from datetime import datetime, timedelta, timezone

    published_at = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()

    class Response:
        status_code = 200
        encoding = 'utf-8'

        def __init__(self, data):
            self.text = 'data: ' + json.dumps({
                'result': {'content': [{'text': json.dumps({'data': data})}]}
            })

    class Session:
        def __init__(self):
            self.calls = []

        def post(self, *args, **kwargs):
            self.calls.append(kwargs)
            method = kwargs['json']['params']['name']
            if method == 'search_jobs':
                return Response({'data': [{
                    'id': 10939233, 'name': 'Desenvolvedor React Junior',
                    'careerPageName': 'Empresa Tech', 'city': 'Remoto',
                    'jobUrl': 'https://empresa.gupy.io/jobs/10939233',
                    'description': 'React e TypeScript',
                }]})
            return Response({'id': 10939233, 'publishedDate': published_at})

    session = Session()
    collector = GupyCollector(
        session, [{'term': 'React'}], detail_limit=1,
        max_age_hours=72, require_publication_date=True,
    )

    jobs = collector.collect()

    assert len(jobs) == 1
    assert jobs[0].published_at == published_at
    detail_arguments = session.calls[1]['json']['params']['arguments']
    assert detail_arguments == {'id': 10939233}


def test_gupy_caches_detail_lookup_across_collector_cycles():
    class Response:
        status_code = 200
        encoding = 'utf-8'

        def __init__(self, data):
            self.text = 'data: ' + json.dumps({
                'result': {'content': [{'text': json.dumps({'data': data})}]}
            })

    class Session:
        def __init__(self):
            self.detail_calls = 0

        def post(self, *args, **kwargs):
            method = kwargs['json']['params']['name']
            if method == 'search_jobs':
                return Response({'data': [{
                    'id': 10939233, 'name': 'Desenvolvedor React Junior',
                    'careerPageName': 'Empresa Tech', 'city': 'Remoto',
                    'jobUrl': 'https://empresa.gupy.io/jobs/10939233',
                    'description': 'React e TypeScript',
                }]})
            self.detail_calls += 1
            return Response({'id': 10939233, 'publishedDate': '2020-01-01T00:00:00Z'})

    session = Session()
    state = {'gupy_detail_cache': {}}

    def collect_once():
        collector = GupyCollector(
            session, [{'term': 'React'}], detail_limit=1,
            max_age_hours=72, require_publication_date=True,
            detail_cache=state['gupy_detail_cache'],
        )
        assert collector.collect() == []
        return collector

    first = collect_once()
    second = collect_once()

    assert first.detail_attempts == 1
    assert second.detail_attempts == 0
    assert session.detail_calls == 1
    assert state['gupy_detail_cache']['10939233']['detail']['publishedDate'] == '2020-01-01T00:00:00Z'


def test_gupy_collector_discards_jobs_outside_configured_recency_window():
    class OldSession(MockSession):
        def post(self, *args, **kwargs):
            response = MockResponse()
            response.text = response.text.replace('\\"description\\": \\"Java e Spring\\"', '\\"description\\": \\"Java e Spring\\", \\"publishedAt\\": \\"2020-01-01T00:00:00Z\\"')
            return response

    collector = GupyCollector(OldSession(), [{"term": "Java"}], max_age_hours=72)

    assert collector.collect() == []


def test_gupy_collector_profiles_real_evidence_level():
    collector = GupyCollector(MockSession(), [{"term": "Java"}])

    jobs = collector.collect()

    assert jobs[0].evidence_level == "MEDIUM_EVIDENCE"


def test_gupy_collector_can_require_publication_date():
    collector = GupyCollector(
        MockSession(),
        [{"term": "Java"}],
        max_age_hours=72,
        require_publication_date=True,
    )

    assert collector.collect() == []
    assert "MISSING_PUBLICATION_DATE" in collector.query_stats[0]["warnings"]
    rejection = collector.rejected_candidates[0]
    assert rejection['source_job_id'] == '42'
    assert rejection['stage'] == 'freshness_filter'
    assert 'data de publicação' in rejection['reason']
