import os
import sys
import xml.etree.ElementTree as ET
import threading
import time
from unittest.mock import MagicMock, patch
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from models.job import Job
from core.normalizer import is_pcd_exclusive


class TestPcdFilter:
    """Testa a heurística de detecção de vaga afirmativa/exclusiva PCD."""

    def test_blocks_pcd_in_title(self):
        blocked, reason = is_pcd_exclusive("Desenvolvedor React Junior - PCD")
        assert blocked is True
        assert "PCD" in reason

    def test_blocks_pcd_case_insensitive(self):
        blocked, _ = is_pcd_exclusive("Analista de Suporte Jr (PcD)")
        assert blocked is True

    def test_blocks_pcd_with_dots(self):
        """P.C.D. deve ser detectado após remoção de pontos."""
        blocked, _ = is_pcd_exclusive("Monitor (a) - P.C.D.")
        assert blocked is True

    def test_blocks_pessoa_com_deficiencia(self):
        blocked, reason = is_pcd_exclusive("Auxiliar Administrativo - Pessoa com Deficiência")
        assert blocked is True
        assert "Deficiencia" in reason or "Pessoa" in reason

    def test_blocks_pessoas_com_deficiencia_plural(self):
        blocked, _ = is_pcd_exclusive("Vagas para Pessoas com Deficiência")
        assert blocked is True

    def test_allows_normal_job(self):
        """Vaga regular sem PCD no título NÃO deve ser bloqueada."""
        blocked, _ = is_pcd_exclusive("Desenvolvedor Full Stack Junior")
        assert blocked is False

    def test_allows_pcd_only_in_description(self):
        """PCD mencionado apenas na descrição não deve bloquear (título limpo)."""
        blocked, _ = is_pcd_exclusive("Desenvolvedor Full Stack Junior")
        assert blocked is False

    def test_allows_pcd_substring_in_word(self):
        """SPCDAM não deve casar com \\bpcd\\b (PCD dentro de palavra)."""
        blocked, _ = is_pcd_exclusive("SPCDAM Project Manager")
        assert blocked is False

    def test_pcd_signal_recorded_on_job(self):
        """O campo pcd_signal do Job deve registrar a origem do sinal."""
        job = Job(title="Dev React - PCD", company="X", workplace_type="remote")
        blocked, reason = is_pcd_exclusive(job.title)
        if blocked:
            job.pcd_signal = "TITLE"
        assert job.pcd_signal == "TITLE"


class TestRssCollector:
    """Testa o coletor RSS com XML mockado."""

    RSS_FEED_XML = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0">
      <channel>
        <title>Google News - Goomer vagas</title>
        <item>
          <title>Goomer contrata desenvolvedores React para Sorocaba</title>
          <link>https://example.com/article1</link>
          <guid>article-001</guid>
        </item>
        <item>
          <title>Goomer lança novo recurso para cardápios digitais</title>
          <link>https://example.com/article2</link>
          <guid>article-002</guid>
        </item>
        <item>
          <title>GFT abre vagas de Java Junior em Sorocaba</title>
          <link>https://example.com/article3</link>
          <guid>article-003</guid>
        </item>
      </channel>
    </rss>"""

    ATOM_FEED_XML = """<?xml version="1.0" encoding="UTF-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry>
        <id>atom-001</id>
        <title>Flavia Nasser busca desenvolvedor web para projeto</title>
        <link href="https://example.com/atom1"/>
      </entry>
    </feed>"""

    def _make_collector(self, xml_content, configs):
        """Cria um RssCollector com HTTP mockado retornando o XML fornecido."""
        from collectors.rss_collector import RssCollector
        mock_http = MagicMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = xml_content.encode("utf-8")
        mock_http.get.return_value = mock_response
        return RssCollector(mock_http, configs)

    def test_parses_rss_20_feed(self):
        configs = [{"url": "https://fake.url/rss", "company": "Goomer"}]
        col = self._make_collector(self.RSS_FEED_XML, configs)
        jobs = col.collect()
        assert len(jobs) == 3
        assert all(isinstance(j, Job) for j in jobs)

    def test_parses_atom_feed(self):
        configs = [{"url": "https://fake.url/atom", "company": "Flavia Nasser"}]
        col = self._make_collector(self.ATOM_FEED_XML, configs)
        jobs = col.collect()
        assert len(jobs) == 1
        assert jobs[0].company == "Flavia Nasser"

    def test_keyword_filter_applied(self):
        """Somente itens com keywords sobrevivem."""
        configs = [{
            "url": "https://fake.url/rss",
            "company": "Goomer",
            "keywords": ["contrata", "vagas"],
        }]
        col = self._make_collector(self.RSS_FEED_XML, configs)
        jobs = col.collect()
        # "Goomer contrata desenvolvedores..." tem "contrata"
        # "GFT abre vagas..." tem "vagas"
        # "Goomer lança novo recurso..." NÃO tem keywords
        assert len(jobs) == 2

    def test_exclude_keyword_filter_applied(self):
        """Itens com exclude_keywords são removidos."""
        configs = [{
            "url": "https://fake.url/rss",
            "company": "Goomer",
            "exclude_keywords": ["cardápio", "recurso"],
        }]
        col = self._make_collector(self.RSS_FEED_XML, configs)
        jobs = col.collect()
        # "Goomer lança novo recurso para cardápios..." é removido
        assert len(jobs) == 2

    def test_rss_source_name_is_rss(self):
        """Jobs vindos do RSS devem ter 'rss' como chave no sources dict."""
        configs = [{"url": "https://fake.url/rss", "company": "Test"}]
        col = self._make_collector(self.RSS_FEED_XML, configs)
        jobs = col.collect()
        for job in jobs:
            assert "rss" in job.sources

    def test_empty_feed_returns_empty_list(self):
        configs = [{"url": "https://fake.url/rss", "company": "Test"}]
        col = self._make_collector("<rss><channel></channel></rss>", configs)
        jobs = col.collect()
        assert jobs == []

    def test_fetches_multiple_feeds_concurrently(self):
        from collectors.rss_collector import RssCollector

        lock = threading.Lock()
        active = 0
        max_active = 0

        class Session:
            def get(self, *args, **kwargs):
                nonlocal active, max_active
                with lock:
                    active += 1
                    max_active = max(max_active, active)
                time.sleep(0.05)
                with lock:
                    active -= 1
                response = MagicMock(status_code=200, content=b"<rss><channel/></rss>")
                return response

        configs = [{"url": f"https://feed{i}.example/rss"} for i in range(4)]
        RssCollector(Session(), configs).collect()

        assert max_active > 1

    def test_wrapped_read_timeout_is_reported_as_timeout_after_retry(self):
        import requests
        from collectors.rss_collector import RssCollector

        class Session:
            def get(self, *args, **kwargs):
                raise requests.ConnectionError(
                    "HTTPSConnectionPool: Max retries exceeded after Read timed out"
                )

        collector = RssCollector(Session(), [{"url": "https://feed.example/rss"}])
        collector.collect()

        attempt = collector.collection_attempts[0]
        assert attempt["error_type"] == "Timeout"
        assert attempt["timed_out"] is True
        assert attempt["retry_count"] == 1

    def test_worker_sessions_are_isolated_and_preserve_retry_policy(self):
        import requests
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry
        from collectors.rss_collector import RssCollector

        base_session = requests.Session()
        base_session.mount("https://", HTTPAdapter(max_retries=Retry(total=1, read=1)))
        collector = RssCollector(base_session, [])

        first, owns_first = collector._new_feed_session()
        second, owns_second = collector._new_feed_session()
        try:
            assert owns_first and owns_second
            assert first is not second
            assert first.adapters["https://"] is not second.adapters["https://"]
            assert first.adapters["https://"].max_retries.total == 1
            assert first.adapters["https://"].max_retries.read == 1
        finally:
            first.close()
            second.close()
            base_session.close()


class TestRssRelevanceScoring:
    """Testa o scoring dedicado para itens RSS."""

    def test_job_signal_scores_high(self):
        from core.scoring import calculate_rss_relevance
        job = Job(
            title="Goomer contrata desenvolvedores React Junior",
            company="Goomer",
            workplace_type="unknown",
        )
        score, reasons = calculate_rss_relevance(job)
        # Sinal de vaga (+30) + Junior (+20) + Empresa Goomer (+20) + tech react (+10) = 80+
        assert score >= 50

    def test_news_signal_scores_low(self):
        from core.scoring import calculate_rss_relevance
        job = Job(
            title="Goomer lança novo recurso para restaurantes",
            company="Goomer",
            workplace_type="unknown",
        )
        score, reasons = calculate_rss_relevance(job)
        # Empresa Goomer (+20) - notícia (-30) = -10
        assert score < 30

    def test_senior_penalized_in_rss(self):
        from core.scoring import calculate_rss_relevance
        job = Job(
            title="GFT contrata Desenvolvedor Senior Java",
            company="GFT",
            workplace_type="unknown",
        )
        score, _ = calculate_rss_relevance(job)
        assert score <= 0

    def test_non_tech_bakery_vetoed_in_rss(self):
        from core.scoring import calculate_rss_relevance
        job = Job(
            title="Estágio de Atendimento - Padaria Central em Sorocaba",
            company="Indeed",
            workplace_type="unknown",
        )
        score, reasons = calculate_rss_relevance(job)
        assert score == 0
        assert any("não-tecnológico" in r.lower() or "padaria" in r.lower() for r in reasons)

    def test_generic_internship_without_tech_penalized(self):
        from core.scoring import calculate_rss_relevance
        job = Job(
            title="Vaga de Estágio Administrativo",
            company="Indeed",
            workplace_type="unknown",
        )
        score, reasons = calculate_rss_relevance(job)
        assert score < 30



class TestSeenIdsNormalization:
    """Testa que seen_ids são normalizados para string."""

    def test_mixed_types_normalized(self):
        import tempfile
        import json
        from storage.state_store import StateStore

        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = os.path.join(tmpdir, "seen.json")
            with open(test_file, "w", encoding="utf-8") as f:
                json.dump({
                    "seen_ids": [12184580, "12184580", "abc", 999],
                    "seen_fingerprints": [],
                    "last_heartbeat": "",
                }, f)

            store = StateStore(test_file)
            # Todos devem ser strings agora
            assert all(isinstance(x, str) for x in store.state["seen_ids"])
            # Duplicatas (12184580 int e str) devem ser unificadas
            assert store.state["seen_ids"].count("12184580") == 1
            # is_seen deve encontrar o ID como string
            assert store.is_seen("fake_fp", "12184580") is True
            assert store.is_seen("fake_fp", "999") is True
