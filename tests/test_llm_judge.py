import os
import sys
from unittest.mock import patch, MagicMock
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from models.job import Job
from core import llm_judge
from core.scoring import evaluate_job


class TestLlmJudgeUnits:
    def setup_method(self):
        llm_judge._PROVIDER_UNAVAILABLE.clear()

    def test_should_invoke_judge_threshold(self):
        assert llm_judge.should_invoke_judge(30) is True
        assert llm_judge.should_invoke_judge(29) is False
        assert llm_judge.should_invoke_judge(0) is False
        assert llm_judge.should_invoke_judge(85) is True

    def test_get_profile_loads_data(self):
        profile = llm_judge.get_profile()
        assert isinstance(profile, dict)
        assert "stack_core" in profile
        assert "React" in profile["stack_core"]

    def test_judge_returns_none_when_no_api_key(self):
        with patch.dict(os.environ, {}, clear=True):
            result = llm_judge.judge("Dev", "Corp", "Desc")
            assert result is None

    def test_judge_calls_gemini_when_gemini_key_present(self):
        fake_response = {
            "is_real_job_opportunity": True,
            "cv_compatibility_score": 85,
            "reasoning": "Vaga React com match",
            "recommendation": "APPLY_NOW"
        }
        with patch.dict(os.environ, {"GEMINI_API_KEY": "fake_gemini_key"}, clear=True):
            with patch("core.llm_judge._call_gemini", return_value=fake_response) as mock_gemini:
                result = llm_judge.judge("Dev React", "Goomer", "React, Node.js")
                assert mock_gemini.called
                assert result["cv_compatibility_score"] == 85

    def test_judge_emits_sanitized_provider_health_outcome(self):
        outcomes = []
        llm_judge.set_health_observer(lambda provider, model, status:
                                      outcomes.append((provider, model, status)))
        fake_response = {
            "is_real_job_opportunity": True,
            "cv_compatibility_score": 70,
        }
        try:
            with patch.dict(os.environ, {"GEMINI_API_KEY": "secret-test-key"}, clear=True):
                with patch("core.llm_judge._call_gemini", return_value=fake_response):
                    result = llm_judge.judge("Dev", "Corp", "Description")
            assert result["llm_provider"] == "gemini"
            assert outcomes == [("gemini", "gemini-3.5-flash-lite", "OK")]
            assert all("secret" not in str(item) for item in outcomes)
        finally:
            llm_judge.set_health_observer(None)

    def test_judge_handles_api_exception_gracefully(self):
        with patch.dict(os.environ, {"GEMINI_API_KEY": "fake_gemini_key"}, clear=True):
            with patch("core.llm_judge._call_gemini", side_effect=Exception("API down")):
                result = llm_judge.judge("Dev", "Corp", "Desc")
                assert result is None

    def test_gemini_request_uses_api_key_header_and_structured_json(self):
        response = MagicMock(status_code=200)
        response.json.return_value = {
            "candidates": [{
                "content": {"parts": [{"text": '{"is_real_job_opportunity": true, "cv_compatibility_score": 70}'}]}
            }]
        }
        with patch("core.llm_judge._enforce_pacing"):
            with patch("requests.post", return_value=response) as mock_post:
                result = llm_judge._call_gemini("fake_gemini_key", "gemini-2.5-flash-lite", "prompt")

        assert result["cv_compatibility_score"] == 70
        args, kwargs = mock_post.call_args
        assert args[0].endswith("/models/gemini-2.5-flash-lite:generateContent")
        assert kwargs["headers"]["x-goog-api-key"] == "fake_gemini_key"
        assert kwargs["json"]["generationConfig"]["responseMimeType"] == "application/json"

    def test_openrouter_request_uses_openai_compatible_contract(self):
        response = MagicMock(status_code=200)
        response.json.return_value = {
            "choices": [{
                "message": {"content": '{"is_real_job_opportunity": true, "cv_compatibility_score": 65}'}
            }]
        }
        with patch("core.llm_judge._enforce_pacing"):
            with patch("requests.post", return_value=response) as mock_post:
                result = llm_judge._call_openrouter("fake_openrouter_key", "google/gemini-2.5-flash-lite", "prompt")

        assert result["cv_compatibility_score"] == 65
        args, kwargs = mock_post.call_args
        assert args[0] == "https://openrouter.ai/api/v1/chat/completions"
        assert kwargs["headers"]["Authorization"] == "Bearer fake_openrouter_key"
        assert kwargs["json"]["model"] == "google/gemini-2.5-flash-lite"

    def test_http_error_returns_none_for_provider_fallback(self):
        response = MagicMock(status_code=503, text="temporarily unavailable")
        with patch("core.llm_judge._enforce_pacing"):
            with patch("requests.post", return_value=response):
                assert llm_judge._call_openrouter("fake_openrouter_key", "model", "prompt") is None

    def test_provider_config_selects_openrouter_and_model_override(self):
        with patch.dict(os.environ, {
            "LLM_PROVIDER": "openrouter",
            "LLM_MODEL": "openai/gpt-oss-20b",
            "OPENROUTER_API_KEY": "fake_openrouter_key",
        }, clear=True):
            assert llm_judge._provider_config() == (
                "openrouter", "fake_openrouter_key", "openai/gpt-oss-20b"
            )

    def test_provider_chain_prefers_primary_and_uses_free_fallback(self):
        with patch.dict(os.environ, {
            "LLM_PRIMARY_PROVIDER": "gemini",
            "LLM_PRIMARY_MODEL": "gemini-test",
            "LLM_FALLBACK_PROVIDER": "openrouter",
            "LLM_FALLBACK_MODEL": "openrouter/free",
            "GEMINI_API_KEY": "gemini-key",
            "OPENROUTER_API_KEY": "router-key",
        }, clear=True):
            assert llm_judge._provider_chain() == [
                ("gemini", "gemini-key", "gemini-test"),
                ("openrouter", "router-key", "openrouter/free"),
            ]

    def test_judge_uses_fallback_provider_after_primary_failure(self):
        result = {
            "is_real_job_opportunity": True,
            "cv_compatibility_score": 70,
        }
        with patch.dict(os.environ, {
            "LLM_PRIMARY_PROVIDER": "gemini",
            "LLM_FALLBACK_PROVIDER": "openrouter",
            "GEMINI_API_KEY": "gemini-key",
            "OPENROUTER_API_KEY": "router-key",
        }, clear=True):
            with patch.object(llm_judge, "_call_gemini", return_value=None):
                with patch.object(llm_judge, "_call_openrouter", return_value=result) as fallback:
                    llm_judge.reset_cycle_stats()
                    output = llm_judge.judge("Dev", "Corp", "Desc")
        assert output["llm_provider"] == "openrouter"
        assert fallback.called
        assert llm_judge.usage_stats()["calls"] == 2

    def test_judge_stops_at_cycle_call_limit(self):
        with patch.dict(os.environ, {
            "LLM_PRIMARY_PROVIDER": "gemini",
            "GEMINI_API_KEY": "gemini-key",
            "LLM_MAX_CALLS_PER_CYCLE": "1",
        }, clear=True):
            llm_judge.reset_cycle_stats()
            with patch.object(llm_judge, "_call_gemini", return_value=None) as caller:
                assert llm_judge.judge("Dev", "Corp", "Desc") is None
                assert llm_judge.judge("Dev", "Corp", "Desc") is None
        assert caller.call_count == 1
        assert llm_judge.usage_stats()["calls"] == 1

    def test_timeout_opens_provider_circuit_breaker(self):
        with patch("core.llm_judge._enforce_pacing"):
            with patch("requests.post", side_effect=TimeoutError("read timeout")) as mock_post:
                assert llm_judge._call_gemini("fake_key", "gemini-2.5-flash-lite", "prompt") is None
                assert llm_judge._call_gemini("fake_key", "gemini-2.5-flash-lite", "prompt") is None
                assert mock_post.call_count == 1

    def test_pacing_enforced(self):
        with patch("time.sleep") as mock_sleep:
            llm_judge.PACING_SECONDS = 4.5
            llm_judge._LAST_CALL_TIMESTAMP = 100.0
            with patch("time.time", return_value=101.0):
                llm_judge._enforce_pacing()
                assert mock_sleep.called
                # Sleep must be around 3.5s (4.5 - 1.0 + jitter)
                sleep_arg = mock_sleep.call_args[0][0]
                assert 3.4 <= sleep_arg <= 4.1

    def test_429_retry_and_backoff(self):
        resp_429 = MagicMock()
        resp_429.status_code = 429
        resp_429.headers = {"Retry-After": "5"}

        resp_200 = MagicMock()
        resp_200.status_code = 200
        resp_200.json.return_value = {
            "candidates": [{
                "content": {
                    "parts": [{
                        "text": '{"is_real_job_opportunity": true, "cv_compatibility_score": 90, "reasoning": "ok", "recommendation": "APPLY_NOW"}'
                    }]
                }
            }]
        }

        with patch("time.sleep") as mock_sleep:
            with patch("core.llm_judge._enforce_pacing"):
                with patch("requests.post", side_effect=[resp_429, resp_200]) as mock_post:
                    res = llm_judge._call_gemini("fake_key", "gemini-2.5-flash-lite", "prompt")
                    assert res is not None
                    assert res["cv_compatibility_score"] == 90
                    assert mock_sleep.called
                    # The sleep argument was the Retry-After header: 5.0s
                    assert mock_sleep.call_args_list[0][0][0] == 5.0

    def test_rpd_tracking_in_state_store(self, tmp_path):
        from storage.state_store import StateStore
        test_file = str(tmp_path / "test_seen.json")
        store = StateStore(test_file)
        assert store.get_llm_usage()["calls"] == 0
        store.record_llm_call()
        store.record_llm_call()
        usage = store.get_llm_usage()
        assert usage["calls"] == 2


class TestEvaluateJobIntegration:
    def test_skips_judge_when_heuristic_score_below_floor(self):
        """Vaga com score < 30 nem bate na API do Gemini."""
        job = Job(
            title="Analista Administrativo",
            company="Empresa Genérica",
            workplace_type="remote",
        )
        with patch("core.llm_judge.judge") as mock_judge:
            score, reasons = evaluate_job(job)
            assert not mock_judge.called
            assert score < 30

    def test_fallback_to_heuristic_when_judge_fails(self):
        """Se o LLM falhar, a vaga preserva seu score heurístico intacto e marca fallback."""
        job = Job(
            title="Desenvolvedor React Junior",
            company="Startup",
            workplace_type="remote",
        )
        with patch("core.llm_judge.judge", return_value=None):
            score, reasons = evaluate_job(job)
            assert score >= 50
            assert any("react" in r.lower() or "júnior" in r.lower() or "junior" in r.lower() for r in reasons)
            assert any("Fallback Heurístico Ativado" in r for r in reasons)

    def test_429_exhaustion_falls_back_cleanly(self):
        """
        Cenário de pico real: 429 persistente em todas as retentativas.
        Deve retornar None sem crash, acionar o fallback heurístico em evaluate_job
        e marcar a ativação do fallback nas razões.
        """
        resp_429 = MagicMock()
        resp_429.status_code = 429
        resp_429.headers = {"Retry-After": "1"}

        job = Job(
            title="Desenvolvedor React Junior",
            company="Goomer",
            workplace_type="remote",
        )

        with patch("time.sleep"):
            with patch("core.llm_judge._enforce_pacing"):
                with patch.dict(os.environ, {"GEMINI_API_KEY": "fake_gemini_key"}, clear=True):
                    with patch("requests.post", return_value=resp_429):
                        score, reasons = evaluate_job(job)
                        assert score > 50
                        assert any("Fallback Heurístico Ativado" in r for r in reasons)

    def test_llm_veto_zeros_score(self):
        """Veto do LLM (ex: notícia ou desabafo) zera o score e adiciona razão."""
        job = Job(
            title="Desenvolvedor React Junior",
            company="Goomer",
            workplace_type="remote",
        )
        fake_llm_result = {
            "is_real_job_opportunity": False,
            "cv_compatibility_score": 0,
            "reasoning": "Texto é um post de despedida de funcionário, não uma vaga",
            "recommendation": "SKIP"
        }
        with patch("core.llm_judge.judge", return_value=fake_llm_result):
            score, reasons = evaluate_job(job)
            assert score == 0
            assert any("não é vaga real" in r for r in reasons)

    def test_combined_score_weighting(self):
        """Score combinado: 30% heurístico + 70% LLM."""
        job = Job(
            title="Desenvolvedor React Junior",
            company="Goomer",
            workplace_type="remote",
        )
        # Heurístico vai dar ~75
        fake_llm_result = {
            "is_real_job_opportunity": True,
            "cv_compatibility_score": 90,
            "reasoning": "Vaga Júnior com fit total na stack",
            "recommendation": "APPLY_NOW"
        }
        with patch("core.llm_judge.judge", return_value=fake_llm_result):
            score, reasons = evaluate_job(job)
            assert score > 70
            assert any("LLM Judge:" in r for r in reasons)
