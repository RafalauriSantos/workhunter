import os
import sys
import pytest
import requests
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from collectors.linkedin_collector import LinkedInCollector


SAMPLE_LINKEDIN_HTML = """
<ul class="jobs-search__results-list">
    <li>
        <div class="base-card" data-entity-urn="urn:li:jobPosting:3910293841">
            <a class="base-card__full-link" href="https://br.linkedin.com/jobs/view/desenvolvedor-junior-at-empresa-tech-3910293841?utm_campaign=google"></a>
            <div class="base-search-card__info">
                <h3 class="base-search-card__title">
                    Desenvolvedor Frontend Júnior (React)
                </h3>
                <h4 class="base-search-card__subtitle">
                    <a href="https://br.linkedin.com/company/empresa-tech">
                        Empresa Tech Inovadora
                    </a>
                </h4>
                <span class="job-search-card__location">
                    Sorocaba, SP
                </span>
                <time class="job-search-card__listdate" datetime="2026-10-17T10:00:00Z">há 6 horas</time>
            </div>
        </div>
    </li>
    <li>
        <!-- Card incompleto sem link nem titulo para testar resiliência -->
        <div class="base-card">
            <span class="job-search-card__location">São Paulo, SP</span>
        </div>
    </li>
    <li>
        <div class="base-card" data-entity-urn="urn:li:jobPosting:9920192831">
            <a class="base-card__full-link" href="https://br.linkedin.com/jobs/view/desenvolvedor-backend-node-9920192831"></a>
            <div class="base-search-card__info">
                <h3 class="base-search-card__title">
                    Desenvolvedor Backend Node.js
                </h3>
                <h4 class="base-search-card__subtitle">
                    <a href="#">Startup X</a>
                </h4>
                <span class="job-search-card__location">
                    Remoto
                </span>
            </div>
        </div>
    </li>
</ul>
"""


class MockResponse:
    def __init__(self, text, status_code=200):
        self.text = text
        self.status_code = status_code
        self.encoding = "utf-8"


def test_linkedin_collector_parses_html_successfully(monkeypatch):
    session = requests.Session()
    monkeypatch.setattr(session, "get", lambda *args, **kwargs: MockResponse(SAMPLE_LINKEDIN_HTML, 200))

    collector = LinkedInCollector(session, [{"keywords": "Desenvolvedor Junior"}])
    jobs = collector.collect()

    assert len(jobs) == 2
    
    j1 = jobs[0]
    assert "React" in j1.title or "react" in j1.technologies
    assert j1.company == "Empresa Tech Inovadora"
    assert j1.location == "Sorocaba, SP"
    assert j1.published_at == "2026-10-17T10:00:00Z"
    assert "linkedin" in j1.sources
    assert j1.sources["linkedin"].source_job_id == "3910293841"

    j2 = jobs[1]
    assert "Node" in j2.title or "node.js" in j2.technologies
    assert j2.company == "Startup X"
    assert j2.workplace_type == "remote"


def test_linkedin_search_attempt_records_native_job_ids_and_query_id(monkeypatch):
    session = requests.Session()
    monkeypatch.setattr(session, "get", lambda *args, **kwargs: MockResponse(SAMPLE_LINKEDIN_HTML, 200))

    collector = LinkedInCollector(session, [{
        "query": "developer OR software",
        "query_id": "broad-search-v1",
    }])
    collector.collect()

    attempt = collector.collection_attempts[0]
    assert attempt["native_ids"] == ["3910293841", "9920192831"]
    assert attempt["query"]["query_id"] == "broad-search-v1"
    parse_rejection = next(item for item in collector.rejected_candidates if item['source_job_id'] == 'card-0-1')
    assert parse_rejection['stage'] == 'source_parse'
    assert 'Card sem título' in parse_rejection['reason']


def test_linkedin_search_attempt_recovers_ids_from_job_urls_without_urn(monkeypatch):
    session = requests.Session()
    html = '''<ul>
    <li><a class="base-card__full-link" href="https://www.linkedin.com/jobs/view/4473616899/"></a>
        <h3 class="base-search-card__title">Junior Frontend Developer</h3></li>
    <li><a class="base-card__full-link" href="https://www.linkedin.com/jobs/view/frontend-engineer-at-company-4473616900?trackingId=x"></a>
        <h3 class="base-search-card__title">Frontend Engineer</h3></li>
    </ul>'''
    monkeypatch.setattr(session, 'get', lambda *args, **kwargs: MockResponse(html, 200))

    collector = LinkedInCollector(session, [{
        'query': 'frontend developer', 'query_id': 'front-end-wide',
    }])
    jobs = collector.collect()

    assert [job.sources['linkedin'].source_job_id for job in jobs] == ['4473616899', '4473616900']
    attempt = collector.collection_attempts[0]
    assert attempt['native_ids'] == ['4473616899', '4473616900']
    assert attempt['query']['query_id'] == 'front-end-wide'


def test_linkedin_collector_handles_http_errors_gracefully(monkeypatch):
    session = requests.Session()
    # Simula status 429 Too Many Requests ou 500 do LinkedIn
    monkeypatch.setattr(session, "get", lambda *args, **kwargs: MockResponse("", 429))

    collector = LinkedInCollector(session, [{"keywords": "Python Junior"}])
    jobs = collector.collect()

    assert jobs == []


def test_linkedin_collector_defers_remaining_searches_after_rate_limit(monkeypatch):
    session = requests.Session()
    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        return MockResponse("", 429)

    monkeypatch.setattr(session, "get", fake_get)
    collector = LinkedInCollector(session, [
        {"keywords": "Desenvolvedor Junior"},
        {"keywords": "Frontend Junior"},
    ])

    assert collector.collect() == []
    assert len(calls) == 1
    assert collector.rate_limited is True
    assert len(collector.query_stats) == 1
    assert collector.query_stats[0]["status"] == "HTTP_429"


def test_detail_rate_limit_stops_additional_linkedin_requests(monkeypatch):
    session = requests.Session()
    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        if "seeMoreJobPostings" in url:
            return MockResponse(SAMPLE_LINKEDIN_HTML, 200)
        return MockResponse("", 429)

    monkeypatch.setattr(session, "get", fake_get)
    collector = LinkedInCollector(session, [{
        "keywords": "desenvolvedor",
        "max_pages": 1,
        "enrich_details": True,
        "detail_enrichment_limit": 8,
    }])

    jobs = collector.collect()

    assert len(jobs) == 2
    assert len(calls) == 2
    assert collector.rate_limited is True
    assert collector.query_stats[0]["enrichment_attempts"] == 1


def test_linkedin_collector_handles_malformed_html(monkeypatch):
    session = requests.Session()
    # HTML corrompido ou inesperado
    monkeypatch.setattr(session, "get", lambda *args, **kwargs: MockResponse("<div>Unexpected structure</div>", 200))

    collector = LinkedInCollector(session, [{"keywords": "Java Junior"}])
    jobs = collector.collect()

    assert jobs == []


def test_linkedin_collector_preserves_filters_and_paginates(monkeypatch):
    session = requests.Session()
    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        return MockResponse(SAMPLE_LINKEDIN_HTML if len(calls) == 1 else "<ul></ul>", 200)

    monkeypatch.setattr(session, "get", fake_get)
    collector = LinkedInCollector(session, [{
        "keywords": "desenvolvedor",
        "time_range": "r3600",
        "experience": "2",
        "workplace_type": "2",
        "max_pages": 3,
    }])

    jobs = collector.collect()

    assert len(jobs) == 2
    assert len(calls) == 2
    query = parse_qs(urlparse(calls[0]).query)
    assert query["f_TPR"] == ["r3600"]
    assert query["experience"] == ["2"]
    assert query["workplace_type"] == ["2"]
    assert parse_qs(urlparse(calls[1]).query)["start"] == ["25"]
    assert collector.query_stats[0]["pages"] == 2
    assert collector.query_stats[0]["cards"] == 3
    assert collector.query_stats[0]["parsed_jobs"] == 2
    assert collector.query_stats[0]["status"] == "OK"


def test_linkedin_collector_enriches_detail_description(monkeypatch):
    session = requests.Session()
    detail_html = '<div class="show-more-less-html__markup">React, Node.js e PostgreSQL para APIs, testes automatizados e integrações.</div>'

    def fake_get(url, **kwargs):
        if "seeMoreJobPostings" in url:
            return MockResponse(SAMPLE_LINKEDIN_HTML, 200)
        return MockResponse(detail_html, 200)

    monkeypatch.setattr(session, "get", fake_get)
    collector = LinkedInCollector(session, [{
        "keywords": "desenvolvedor",
        "max_pages": 1,
        "enrich_details": True,
        "detail_enrichment_limit": 1,
    }])

    jobs = collector.collect()

    assert jobs[0].description.startswith("React, Node.js")
    assert collector.query_stats[0]["enrichment_attempts"] == 1
    assert collector.query_stats[0]["enrichment_successes"] == 1


def test_linkedin_detail_budget_prioritizes_profile_stack_over_first_card(monkeypatch):
    session = requests.Session()
    cards = '''<ul>
    <li><div data-entity-urn="urn:li:jobPosting:1"><a class="base-card__full-link" href="https://www.linkedin.com/jobs/view/generic-1"></a><h3 class="base-search-card__title">Junior Developer</h3><h4 class="base-search-card__subtitle"><a>Company</a></h4><span class="job-search-card__location">Brasil</span></div></li>
    <li><div data-entity-urn="urn:li:jobPosting:2"><a class="base-card__full-link" href="https://www.linkedin.com/jobs/view/react-2"></a><h3 class="base-search-card__title">Junior React Developer</h3><h4 class="base-search-card__subtitle"><a>Company</a></h4><span class="job-search-card__location">Brasil</span></div></li>
    </ul>'''
    detail_urls = []

    def fake_get(url, **kwargs):
        if "seeMoreJobPostings" in url:
            return MockResponse(cards, 200)
        detail_urls.append(url)
        return MockResponse('<div class="show-more-less-html__markup">React is required for this frontend role with APIs and tests.</div>', 200)

    monkeypatch.setattr(session, "get", fake_get)
    collector = LinkedInCollector(session, [{
        "keywords": "developer", "max_pages": 1,
        "enrich_details": True, "detail_enrichment_limit": 1,
    }])

    jobs = collector.collect()

    assert len(jobs) == 2
    assert len(detail_urls) == 1
    assert detail_urls[0].endswith("react-2")


def test_linkedin_collector_records_planned_seniority_and_recent_window(monkeypatch):
    session = requests.Session()
    monkeypatch.setattr(session, "get", lambda *args, **kwargs: MockResponse("<ul></ul>", 200))

    collector = LinkedInCollector(session, [{
        "keywords": "developer",
        "seniority": "mid",
        "published_within_hours": 24,
    }])

    collector.collect()

    assert collector.query_stats[0]["seniority"] == "mid"
    assert collector.query_stats[0]["published_within_hours"] == 24
