import os
import sys
from unittest.mock import patch, MagicMock
import pytest
from datetime import datetime, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from collectors.github_collector import GithubIssuesCollector


class TestGithubIssuesCollector:
    @pytest.fixture
    def sample_issues(self):
        issues = [
            {
                "id": 101,
                "title": "[Remoto] Desenvolvedor Front-end React Jr na Goomer",
                "html_url": "https://github.com/frontendbr/vagas/issues/101",
                "labels": [{"name": "Remoto"}, {"name": "Júnior"}, {"name": "React"}],
                "body": "Buscamos dev júnior com React e Tailwind."
            },
            {
                "id": 102,
                "title": "Pull Request de Teste",
                "html_url": "https://github.com/frontendbr/vagas/pull/102",
                "pull_request": {"url": "https://api.github.com/..."},
                "labels": [{"name": "Remoto"}],
                "body": "PR ignore"
            },
            {
                "id": 103,
                "title": "[Híbrido/SP] Arquiteto de Software Sênior",
                "html_url": "https://github.com/frontendbr/vagas/issues/103",
                "labels": [{"name": "Sênior"}, {"name": "Híbrido"}],
                "body": "Vaga sênior com 10 anos de experiência."
            },
            {
                "id": 104,
                "title": "[Curitiba] Desenvolvedor Fullstack Node/React @ Startup",
                "html_url": "https://github.com/backend-br/vagas/issues/104",
                "labels": [{"name": "CLT"}, {"name": "Presencial"}],
                "body": "Trabalho presencial em Curitiba."
            }
        ]
        created = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        for issue in issues:
            issue["created_at"] = created
            issue["updated_at"] = created
        return issues

    def test_collect_and_parsing(self, sample_issues):
        mock_http = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = sample_issues
        mock_http.get.return_value = mock_resp

        collector = GithubIssuesCollector(
            http_session=mock_http,
            repos=["frontendbr/vagas"],
            keywords=["react", "desenvolvedor"],
            exclude_keywords=["arquiteto", "sênior", "senior"]
        )

        jobs = collector.collect()
        # 102 is PR (skipped), 103 has exclude keyword 'arquiteto' (skipped)
        # 101 and 104 remain
        assert len(jobs) == 2

        job_goomer = next((j for j in jobs if "Goomer" in j.company or "goomer" in j.title.lower()), None)
        assert job_goomer is not None
        assert job_goomer.workplace_type == "remote"
        assert job_goomer.seniority == "junior"
        assert job_goomer.company == "Goomer"
        assert "github" in job_goomer.sources
        assert job_goomer.sources["github"].url == "https://github.com/frontendbr/vagas/issues/101"
        rejected_ids = {item['source_job_id'] for item in collector.rejected_candidates}
        assert {'102', '103'} <= rejected_ids
        rejection = next(item for item in collector.rejected_candidates if item['source_job_id'] == '102')
        assert rejection['reason'] == 'Item é um pull request, não uma publicação de vaga'

    def test_auth_headers_with_github_token(self):
        mock_http = MagicMock()
        with patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_secret_token_123"}):
            collector = GithubIssuesCollector(mock_http, repos=["frontendbr/vagas"])
            headers = collector._get_headers()
            assert headers["Authorization"] == "Bearer ghp_secret_token_123"

    def test_auth_headers_without_github_token(self):
        mock_http = MagicMock()
        with patch.dict(os.environ, {}, clear=True):
            collector = GithubIssuesCollector(mock_http, repos=["frontendbr/vagas"])
            headers = collector._get_headers()
            assert "Authorization" not in headers

    def test_old_issue_is_not_resurrected_by_recent_update(self):
        mock_http = MagicMock()
        response = MagicMock(status_code=200)
        response.json.return_value = [{
            "id": 501,
            "title": "[Remoto] Desenvolvedor React Junior",
            "html_url": "https://github.com/frontendbr/vagas/issues/501",
            "labels": [{"name": "Remoto"}],
            "body": "Vaga antiga editada recentemente.",
            "created_at": "2025-01-01T00:00:00Z",
            "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }]
        mock_http.get.return_value = response
        collector = GithubIssuesCollector(mock_http, repos=["frontendbr/vagas"], lookback_days=1)
        assert collector.collect() == []

    def test_issue_without_creation_date_is_not_treated_as_recent(self):
        mock_http = MagicMock()
        response = MagicMock(status_code=200)
        response.json.return_value = [{
            "id": 502,
            "title": "[Remoto] Desenvolvedor React Junior",
            "html_url": "https://github.com/frontendbr/vagas/issues/502",
            "labels": [{"name": "Remoto"}],
            "body": "Sem data de criação.",
        }]
        mock_http.get.return_value = response
        collector = GithubIssuesCollector(mock_http, repos=["frontendbr/vagas"], lookback_days=1)
        assert collector.collect() == []
