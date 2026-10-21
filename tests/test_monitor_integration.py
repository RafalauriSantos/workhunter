import json

import monitor
from models.job import Job
from storage.state_store import StateStore


class EmptyCollector:
    def __init__(self, *args, **kwargs):
        self.query_stats = []

    def collect(self):
        return []


class FakeGupyCollector:
    def __init__(self, *args, **kwargs):
        self.query_stats = [{"parameters": {"term": "React"}, "results": 1, "status": "OK"}]

    def collect(self):
        job = Job(
            title="Desenvolvedor React Junior",
            company="Goomer",
            workplace_type="remote",
            location="Brasil",
            description="React, TypeScript, Node.js, PostgreSQL, testes e APIs.",
            technologies=["React", "TypeScript", "Node.js", "PostgreSQL"],
            evidence_level="HIGH_EVIDENCE",
        )
        job.add_source("gupy", "gupy-1", "https://goomer.gupy.io/jobs/1")
        return [job]


class FakeNotifier:
    def __init__(self, *args, **kwargs):
        self.alerts = []

    def send_job_alert(self, job):
        self.alerts.append(job)
        return True

    def send_heartbeat(self, *args, **kwargs):
        return False


class FailedNotifier(FakeNotifier):
    def send_job_alert(self, job):
        self.alerts.append(job)
        return False


class FakeEmailNotifier:
    def __init__(self, *args, **kwargs):
        self.alerts = []

    def send_job_alert(self, job):
        self.alerts.append(job)
        return True


def test_run_check_processes_and_persists_one_job_without_network(monkeypatch, tmp_path):
    state_file = tmp_path / "seen.json"
    config = {
        "min_match_score": 50,
        "heartbeat": {"enabled": False},
        "monitors": [
            {"type": "gupy", "term": "React", "limit": 1},
            {"type": "linkedin", "keywords_search": "React"},
            {"type": "rss", "url": "https://feed.example/rss"},
            {"type": "github_issues", "repos": []},
            {"type": "trampos", "keywords": ["react"]},
        ],
    }
    notifier = FakeNotifier()

    monkeypatch.setattr(monitor, "load_config", lambda: config)
    monkeypatch.setattr(monitor, "STATE_FILE", str(state_file))
    monkeypatch.setattr(monitor, "GupyCollector", FakeGupyCollector)
    monkeypatch.setattr(monitor, "LinkedInCollector", EmptyCollector)
    monkeypatch.setattr(monitor, "RssCollector", EmptyCollector)
    monkeypatch.setattr(monitor, "GithubIssuesCollector", EmptyCollector)
    monkeypatch.setattr(monitor, "TramposCollector", EmptyCollector)
    monkeypatch.setattr(monitor, "TelegramNotifier", lambda *args, **kwargs: notifier)

    monitor.run_check()

    saved = StateStore(str(state_file))
    assert len(notifier.alerts) == 1
    assert saved.state["recent_decisions"][-1]["decision"] == "DELIVERED"
    assert saved.is_seen(notifier.alerts[0].fingerprint, "gupy-1") is True


def test_run_check_plans_recent_seniority_queries_and_survives_source_failure(monkeypatch, tmp_path):
    state_file = tmp_path / "seen.json"
    calls = {}
    config = {
        "min_match_score": 50,
        "heartbeat": {"enabled": False},
        "monitors": [
            {
                "type": "gupy",
                "term": "developer",
                "query_variants": ["developer", "desenvolvedor"],
                "seniority_variants": ["junior", "mid"],
                "recent_window_hours": 24,
            },
            {"type": "linkedin", "keywords_search": "developer"},
        ],
    }

    class RecordingGupy(FakeGupyCollector):
        def __init__(self, *args, **kwargs):
            calls["gupy"] = args[1]
            super().__init__(*args, **kwargs)

    class FailedLinkedIn:
        def __init__(self, *args, **kwargs):
            calls["linkedin"] = kwargs
            self.query_stats = []

        def collect(self):
            raise RuntimeError("simulated LinkedIn outage")

    notifier = FakeNotifier()
    monkeypatch.setattr(monitor, "load_config", lambda: config)
    monkeypatch.setattr(monitor, "STATE_FILE", str(state_file))
    monkeypatch.setattr(monitor, "GupyCollector", RecordingGupy)
    monkeypatch.setattr(monitor, "LinkedInCollector", FailedLinkedIn)
    monkeypatch.setattr(monitor, "RssCollector", EmptyCollector)
    monkeypatch.setattr(monitor, "GithubIssuesCollector", EmptyCollector)
    monkeypatch.setattr(monitor, "TramposCollector", EmptyCollector)
    monkeypatch.setattr(monitor, "TelegramNotifier", lambda *args, **kwargs: notifier)

    monitor.run_check()

    assert len(calls["gupy"]) == 4
    assert {query["seniority"] for query in calls["gupy"]} == {"junior", "mid"}
    assert {query["term"] for query in calls["gupy"]} == {"developer", "desenvolvedor"}
    assert {query["published_within_hours"] for query in calls["gupy"]} == {24}
    assert len(notifier.alerts) == 1


def test_run_check_preserves_distinct_linkedin_query_variants(monkeypatch, tmp_path):
    state_file = tmp_path / "seen.json"
    captured = {}

    class RecordingLinkedIn(EmptyCollector):
        def __init__(self, *args, **kwargs):
            captured["searches"] = args[1]
            super().__init__(*args, **kwargs)

    config = {
        "min_match_score": 50,
        "heartbeat": {"enabled": False},
        "linkedin_detail_enrichment_enabled": True,
        "linkedin_detail_enrichment_limit": 2,
        "monitors": [{
            "type": "linkedin",
            "description": "same monitor description",
            "query_variants": ["React Junior", "React Pleno", "Frontend React"],
            "time_range": "r7200",
            "enrich_details": False,
        }],
    }
    monkeypatch.setattr(monitor, "load_config", lambda: config)
    monkeypatch.setattr(monitor, "STATE_FILE", str(state_file))
    monkeypatch.setattr(monitor, "GupyCollector", EmptyCollector)
    monkeypatch.setattr(monitor, "LinkedInCollector", RecordingLinkedIn)
    monkeypatch.setattr(monitor, "RssCollector", EmptyCollector)
    monkeypatch.setattr(monitor, "GithubIssuesCollector", EmptyCollector)
    monkeypatch.setattr(monitor, "TramposCollector", EmptyCollector)
    monkeypatch.setattr(monitor, "TelegramNotifier", lambda *args, **kwargs: FakeNotifier())

    monitor.run_check()

    assert [query["query"] for query in captured["searches"]] == ["React Junior", "React Pleno", "Frontend React"]
    assert all(query["enrich_details"] is True for query in captured["searches"])
    assert sum(query["detail_enrichment_limit"] for query in captured["searches"]) == 2
    assert [query["detail_enrichment_limit"] for query in captured["searches"]] == [2, 0, 0]


def test_run_check_skips_linkedin_during_persisted_rate_limit(monkeypatch, tmp_path):
    state_file = tmp_path / "seen.json"
    captured = {"collector": 0}
    config = {
        "min_match_score": 50,
        "heartbeat": {"enabled": False},
        "monitors": [{"type": "linkedin", "keywords_search": "React"}],
    }

    class UnexpectedLinkedIn(EmptyCollector):
        def __init__(self, *args, **kwargs):
            captured["collector"] += 1
            super().__init__(*args, **kwargs)

    store = StateStore(str(state_file))
    store.register_source_rate_limit("linkedin")

    monkeypatch.setattr(monitor, "load_config", lambda: config)
    monkeypatch.setattr(monitor, "STATE_FILE", str(state_file))
    monkeypatch.setattr(monitor, "LinkedInCollector", UnexpectedLinkedIn)
    monkeypatch.setattr(monitor, "TelegramNotifier", lambda *args, **kwargs: FakeNotifier())

    monitor.run_check()

    assert captured["collector"] == 0


def test_detail_http_429_cools_down_details_without_cooling_search(monkeypatch, tmp_path):
    state_file = tmp_path / "seen.json"
    config = {
        "min_match_score": 50,
        "heartbeat": {"enabled": False},
        "linkedin_detail_enrichment_enabled": True,
        "linkedin_detail_enrichment_limit": 1,
        "monitors": [{"type": "linkedin", "keywords_search": "React"}],
    }

    class DetailRateLimitedLinkedIn:
        def __init__(self, *args, **kwargs):
            self.query_stats = []
            self.collection_attempts = [{
                "operation": "detail", "http_status": 429,
                "result_count": 0, "reason": "HTTP response",
            }]
            self.rate_limited = False
            self.detail_rate_limited = True

        def collect(self):
            return []

    monkeypatch.setattr(monitor, "load_config", lambda: config)
    monkeypatch.setattr(monitor, "STATE_FILE", str(state_file))
    monkeypatch.setattr(monitor, "LinkedInCollector", DetailRateLimitedLinkedIn)
    monkeypatch.setattr(monitor, "TelegramNotifier", lambda *args, **kwargs: FakeNotifier())

    monitor.run_check()

    saved = StateStore(str(state_file))
    assert "linkedin" not in saved.state["source_cooldowns"]
    assert saved.state["source_cooldowns"]["linkedin_detail"]["cooldown_seconds"] == 21600


def test_active_linkedin_detail_cooldown_skips_only_detail_requests(monkeypatch, tmp_path):
    state_file = tmp_path / "seen.json"
    config = {
        "min_match_score": 50,
        "heartbeat": {"enabled": False},
        "linkedin_detail_enrichment_enabled": True,
        "linkedin_detail_enrichment_limit": 2,
        "monitors": [{"type": "linkedin", "keywords_search": "React"}],
    }
    store = StateStore(str(state_file))
    store.register_source_rate_limit("linkedin_detail")
    captured = {}

    class RecordingLinkedIn:
        def __init__(self, http, searches):
            captured["searches"] = searches
            self.query_stats = []
            self.collection_attempts = []
            self.rate_limited = False
            self.detail_rate_limited = False

        def collect(self):
            return []

    monkeypatch.setattr(monitor, "load_config", lambda: config)
    monkeypatch.setattr(monitor, "STATE_FILE", str(state_file))
    monkeypatch.setattr(monitor, "LinkedInCollector", RecordingLinkedIn)
    monkeypatch.setattr(monitor, "TelegramNotifier", lambda *args, **kwargs: FakeNotifier())

    monitor.run_check()

    assert len(captured["searches"]) == 1
    assert captured["searches"][0]["detail_enrichment_limit"] == 0
    assert "detail_enrichment_skipped_reason" in captured["searches"][0]


def test_run_check_never_sends_scope_with_hard_barrier_even_if_score_is_high(monkeypatch, tmp_path):
    state_file = tmp_path / "seen.json"
    config = {"min_match_score": 1, "heartbeat": {"enabled": False}, "monitors": [{"type": "gupy", "term": "Java"}]}

    class BarrierGupy(FakeGupyCollector):
        def collect(self):
            job = Job(
                title="Desenvolvedor Java Junior", company="Empresa", workplace_type="remote",
                description="Requisitos obrigatórios: 1+ ano de experiência prática com Java e inglês avançado.",
            )
            job.add_source("gupy", "barrier-java", "https://empresa.gupy.io/jobs/2")
            return [job]

    notifier = FakeNotifier()
    monkeypatch.setattr(monitor, "load_config", lambda: config)
    monkeypatch.setattr(monitor, "STATE_FILE", str(state_file))
    monkeypatch.setattr(monitor, "GupyCollector", BarrierGupy)
    monkeypatch.setattr(monitor, "LinkedInCollector", EmptyCollector)
    monkeypatch.setattr(monitor, "RssCollector", EmptyCollector)
    monkeypatch.setattr(monitor, "GithubIssuesCollector", EmptyCollector)
    monkeypatch.setattr(monitor, "TramposCollector", EmptyCollector)
    monkeypatch.setattr(monitor, "TelegramNotifier", lambda *args, **kwargs: notifier)
    monkeypatch.setattr(monitor, "evaluate_job", lambda job, is_rss=False: (99, ["score artificialmente alto"]))

    monitor.run_check()

    assert notifier.alerts == []
    saved = StateStore(str(state_file))
    assert saved.state["recent_decisions"][-1]["decision"] == "DISCARD_SCOPE"


def test_scope_compatible_job_is_not_lost_only_because_legacy_score_is_low(monkeypatch, tmp_path):
    state_file = tmp_path / "seen.json"
    config = {"min_match_score": 50, "heartbeat": {"enabled": False}, "monitors": [{"type": "gupy", "term": "React"}]}

    class CompatibleGupy(FakeGupyCollector):
        def collect(self):
            job = Job(
                title="Desenvolvedor React Junior", company="Startup", workplace_type="remote",
                description="Requisitos obrigatórios: React e Node.js. Trabalho sob orientação com APIs.",
            )
            job.add_source("gupy", "compatible-low-score", "https://startup.gupy.io/jobs/3")
            return [job]

    notifier = FakeNotifier()
    monkeypatch.setattr(monitor, "load_config", lambda: config)
    monkeypatch.setattr(monitor, "STATE_FILE", str(state_file))
    monkeypatch.setattr(monitor, "GupyCollector", CompatibleGupy)
    monkeypatch.setattr(monitor, "LinkedInCollector", EmptyCollector)
    monkeypatch.setattr(monitor, "RssCollector", EmptyCollector)
    monkeypatch.setattr(monitor, "GithubIssuesCollector", EmptyCollector)
    monkeypatch.setattr(monitor, "TramposCollector", EmptyCollector)
    monkeypatch.setattr(monitor, "TelegramNotifier", lambda *args, **kwargs: notifier)
    monkeypatch.setattr(monitor, "evaluate_job", lambda job, is_rss=False: (10, ["legacy score baixo"]))

    monitor.run_check()

    assert len(notifier.alerts) == 1
    assert notifier.alerts[0].compatibility_category == "COMPATIVEL"


def test_run_check_uses_email_when_telegram_delivery_fails(monkeypatch, tmp_path):
    state_file = tmp_path / "seen.json"
    config = {
        "min_match_score": 50,
        "heartbeat": {"enabled": False},
        "monitors": [{"type": "gupy", "term": "React", "limit": 1}],
    }
    telegram = FailedNotifier()
    email = FakeEmailNotifier()
    monkeypatch.setattr(monitor, "load_config", lambda: config)
    monkeypatch.setattr(monitor, "STATE_FILE", str(state_file))
    monkeypatch.setattr(monitor, "GupyCollector", FakeGupyCollector)
    monkeypatch.setattr(monitor, "LinkedInCollector", EmptyCollector)
    monkeypatch.setattr(monitor, "RssCollector", EmptyCollector)
    monkeypatch.setattr(monitor, "GithubIssuesCollector", EmptyCollector)
    monkeypatch.setattr(monitor, "TramposCollector", EmptyCollector)
    monkeypatch.setattr(monitor, "TelegramNotifier", lambda *args, **kwargs: telegram)
    monkeypatch.setattr(monitor, "ResendEmailNotifier", lambda *args, **kwargs: email)

    monitor.run_check()

    assert len(telegram.alerts) == 1
    assert len(email.alerts) == 1


def test_run_check_rejects_weak_progression_opportunity_below_legacy_score(monkeypatch, tmp_path):
    state_file = tmp_path / "seen.json"
    config = {
        "min_match_score": 50,
        "progression_min_score": 35,
        "heartbeat": {"enabled": False},
        "monitors": [{"type": "gupy", "term": "pleno", "limit": 1}],
    }

    class ProgressionGupy(FakeGupyCollector):
        def collect(self):
            job = Job(
                title="Desenvolvedor Pleno",
                company="Startup",
                workplace_type="remote",
                description="Apoiar o desenvolvimento, fazer correcoes simples e testes basicos sob orientacao. Desejavel conhecer Java.",
                evidence_level="HIGH_EVIDENCE",
            )
            job.add_source("gupy", "progression-1", "https://startup.gupy.io/jobs/1")
            return [job]

    notifier = FakeNotifier()
    monkeypatch.setattr(monitor, "load_config", lambda: config)
    monkeypatch.setattr(monitor, "STATE_FILE", str(state_file))
    monkeypatch.setattr(monitor, "GupyCollector", ProgressionGupy)
    monkeypatch.setattr(monitor, "LinkedInCollector", EmptyCollector)
    monkeypatch.setattr(monitor, "RssCollector", EmptyCollector)
    monkeypatch.setattr(monitor, "GithubIssuesCollector", EmptyCollector)
    monkeypatch.setattr(monitor, "TramposCollector", EmptyCollector)
    monkeypatch.setattr(monitor, "TelegramNotifier", lambda *args, **kwargs: notifier)
    monkeypatch.setattr(monitor, "evaluate_job", lambda job, is_rss=False: (40, ["heuristica controlada no teste"]))

    monitor.run_check()

    assert len(notifier.alerts) == 0
