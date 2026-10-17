import os
import sys
import pytest
import requests

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from collectors.trampos_collector import TramposCollector


def test_trampos_collector_maps_api_response_to_job(monkeypatch):
    mock_payload = {
        "opportunities": [
            {
                "id": 1001,
                "name": "Desenvolvedor Front-end Júnior (React)",
                "company_name": "Agência Web 3",
                "city": "São Paulo",
                "state": "SP",
                "salary": "R$ 3.500 a R$ 4.500",
                "workplace_type": "remoto",
                "opportunity_type": "CLT",
                "description": "Vaga para atuar no desenvolvimento de interfaces com React e TypeScript.",
                "published_at": "2026-09-13T10:00:00Z"
            },
            {
                "id": 1002,
                "name": "Arquiteto de Software Sênior",
                "company_name": "Enterprise SA",
                "city": "Campinas",
                "state": "SP",
                "salary": "A combinar",
                "workplace_type": "presencial",
                "opportunity_type": "PJ",
                "description": "Liderança técnica e arquitetura de microsserviços.",
                "published_at": "2026-09-13T10:00:00Z"
            }
        ]
    }

    class MockResponse:
        status_code = 200

        def json(self):
            return mock_payload

    session = requests.Session()
    monkeypatch.setattr(session, "get", lambda *args, **kwargs: MockResponse())

    collector = TramposCollector(
        http_session=session,
        keywords=["front-end", "react", "junior"],
        exclude_keywords=["senior", "sênior", "arquiteto"]
    )

    jobs = collector.collect()
    assert len(jobs) == 1
    job = jobs[0]

    assert job.title == "Desenvolvedor Front-end Junior (React)"
    assert job.company == "Agência Web 3"
    assert job.salary == "R$ 3.500 a R$ 4.500"
    assert job.workplace_type == "remote"
    assert "trampos" in job.sources
    assert job.canonical_url == "https://trampos.co/oportunidades/1001"
    assert job.evidence_level == "HIGH_EVIDENCE"
    rejection = next(item for item in collector.rejected_candidates if item['source_job_id'] == '1002')
    assert rejection['stage'] == 'source_filter'
    assert 'termo excluído' in rejection['reason']
