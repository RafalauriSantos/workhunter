import re
import email.utils
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
import xml.etree.ElementTree as ET
from typing import List, Dict, Any, Optional, Tuple
import requests
from datetime import datetime, timezone
from html import unescape
from requests.adapters import HTTPAdapter
from collectors.base import BaseCollector
from models.job import Job
from core.normalizer import normalize_title
from core.url_resolver import resolve_url_tripartite, calculate_noise_score


def parse_feed_datetime(date_str: str) -> Optional[datetime]:
    """
    Faz parse robusto de timestamps de feeds (RFC-822/1123 e ISO-8601),
    convertendo sempre para UTC.
    """
    if not date_str:
        return None
    date_str = date_str.strip()

    # 1. Tenta RFC-822 / RFC-1123 (RSS 2.0 padrão)
    try:
        parsed_tuple = email.utils.parsedate_to_datetime(date_str)
        if parsed_tuple:
            return parsed_tuple.astimezone(timezone.utc)
    except Exception:
        pass

    # 2. Tenta ISO-8601 (Atom padrão)
    try:
        clean_iso = date_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(clean_iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        pass

    return None


def classify_freshness(dt: Optional[datetime], max_age_hours: int = 72) -> Tuple[str, str]:
    """
    Classifica recência da vaga sem assunções destrutivas.
    Retorna (status, reason): FRESH | STALE | UNKNOWN
    """
    if dt is None:
        return "UNKNOWN", "Data ausente ou em formato não reconhecido"

    now_utc = datetime.now(timezone.utc)
    age = now_utc - dt

    if age < timedelta(0):
        # Data no futuro por clock skew
        return "FRESH", "Timestamp recente (clock skew leve)"

    if age <= timedelta(hours=max_age_hours):
        return "FRESH", f"Vaga recente publicada há {int(age.total_seconds() // 3600)}h"

    return "STALE", f"Vaga com idade superior a {max_age_hours}h ({int(age.total_seconds() // 3600)}h)"


class RssCollector(BaseCollector):
    """Coletor de vagas via feeds RSS 2.0 e Atom com suporte à SPEC-008."""

    MAX_CONCURRENT_FEEDS = 4

    def __init__(self, http_session: requests.Session, configs: List[Dict[str, Any]], resolve_urls: bool = False, min_description_chars: int = 0):
        self.http = http_session
        self.configs = configs
        self.resolve_urls = resolve_urls
        self.min_description_chars = max(0, int(min_description_chars or 0))
        self.collection_attempts = []

    def _parse_feed(self, feed_url: str, http_session=None) -> List[Dict[str, Any]]:
        """Faz fetch e parse do XML do feed, retorna lista de {id, title, link, pub_date}."""
        http_session = http_session or self.http
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        try:
            started = datetime.now(timezone.utc).isoformat()
            resp = http_session.get(feed_url, headers=headers, timeout=15)
            if resp.status_code != 200:
                self.record_attempt('rss', 'feed', started, query={'url': feed_url},
                                    http_status=resp.status_code, error_type=f'HTTP_{resp.status_code}')
                self.report_issue(f'HTTP_{resp.status_code}')
                print(f"[ALERTA RssCollector] Feed retornou status {resp.status_code}: {feed_url[:80]}")
                return []

            root = ET.fromstring(resp.content)
            items = []

            # 1. Formato Atom (Google Alerts)
            ns = {"atom": "http://www.w3.org/2005/Atom"}
            entries = root.findall("atom:entry", ns)
            if entries:
                for entry in entries:
                    item_id = entry.findtext("atom:id", default="", namespaces=ns)
                    title = entry.findtext("atom:title", default="", namespaces=ns)
                    link_elem = entry.find("atom:link", ns)
                    link = link_elem.attrib.get("href", "") if link_elem is not None else ""
                    raw_date = entry.findtext("atom:updated", default="", namespaces=ns) or entry.findtext("atom:published", default="", namespaces=ns)
                    description = entry.findtext("atom:summary", default="", namespaces=ns) or entry.findtext("atom:content", default="", namespaces=ns)
                    title_clean = re.sub(r"<[^>]+>", "", title).strip()
                    items.append({
                        "id": item_id or link,
                        "title": title_clean,
                        "link": link,
                        "raw_date": raw_date,
                        "description": description,
                    })
                self.record_attempt('rss', 'feed', started, query={'url': feed_url},
                                    http_status=resp.status_code, result_count=len(items),
                                    native_ids=[str(item['id']) for item in items])
                return items

            # 2. Formato RSS 2.0 (Google News)
            channel = root.find("channel")
            if channel is not None:
                for item in channel.findall("item"):
                    guid = item.findtext("guid") or item.findtext("link") or ""
                    title = item.findtext("title", default="")
                    link = item.findtext("link", default="")
                    raw_date = item.findtext("pubDate", default="")
                    description = item.findtext("description", default="")
                    title_clean = re.sub(r"<[^>]+>", "", title).strip()
                    items.append({
                        "id": guid or link,
                        "title": title_clean,
                        "link": link,
                        "raw_date": raw_date,
                        "description": description,
                    })
                self.record_attempt('rss', 'feed', started, query={'url': feed_url},
                                    http_status=resp.status_code, result_count=len(items),
                                    native_ids=[str(item['id']) for item in items])
                return items

        except Exception as e:
            error_text = str(e)
            normalized_error = error_text.lower()
            timed_out = (
                isinstance(e, requests.Timeout)
                or "timed out" in normalized_error
                or "timeout" in normalized_error
            )
            retry_count = 1 if "max retries exceeded" in normalized_error else 0
            self.record_attempt('rss', 'feed', locals().get('started', datetime.now(timezone.utc).isoformat()),
                                query={'url': feed_url},
                                error_type='Timeout' if timed_out else type(e).__name__,
                                timed_out=timed_out, retry_count=retry_count, reason=error_text)
            self.report_issue('Timeout' if timed_out else type(e).__name__)
            print(f"[ERRO RssCollector] {e}")
        return []

    def _new_feed_session(self):
        """Create an isolated requests session while preserving the HTTP policy."""
        if not isinstance(self.http, requests.Session):
            return self.http, False

        session = requests.Session()
        session.headers.update(self.http.headers)
        session.cookies.update(self.http.cookies)
        session.auth = self.http.auth
        session.proxies.update(self.http.proxies)
        session.hooks = {name: list(callbacks) for name, callbacks in self.http.hooks.items()}
        session.params.update(self.http.params)
        session.verify = self.http.verify
        session.cert = self.http.cert
        session.trust_env = self.http.trust_env
        session.max_redirects = self.http.max_redirects
        for prefix, adapter in self.http.adapters.items():
            if isinstance(adapter, HTTPAdapter):
                session.mount(prefix, HTTPAdapter(
                    pool_connections=adapter._pool_connections,
                    pool_maxsize=adapter._pool_maxsize,
                    max_retries=adapter.max_retries,
                    pool_block=adapter._pool_block,
                ))
        return session, True

    def _fetch_feed(self, feed_url: str):
        session, owns_session = self._new_feed_session()
        try:
            return self._parse_feed(feed_url, session)
        finally:
            if owns_session:
                session.close()

    def _matches_filters(self, title_lower: str, config: Dict[str, Any]) -> bool:
        """Aplica keyword/exclude_keyword filters do config ao título do item RSS."""
        keywords = [k.lower() for k in config.get("keywords", [])]
        exclude = [k.lower() for k in config.get("exclude_keywords", [])]

        if exclude:
            if any(kw in title_lower for kw in exclude):
                return False

        if keywords:
            if not any(kw in title_lower for kw in keywords):
                return False

        return True

    def _enrich_resolved_page(self, job: Job) -> bool:
        """Promove RSS resolvido somente quando a página final traz evidência."""
        if not job.resolved_url or "news.google.com" in job.resolved_url:
            return False
        try:
            response = self.http.get(
                job.resolved_url,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
                timeout=8,
            )
            if response.status_code != 200:
                return False
            html = response.text
            match = re.search(
                r'<meta[^>]+(?:name|property)=["\'](?:description|og:description)["\'][^>]+content=["\']([^"\']+)',
                html,
                re.IGNORECASE,
            )
            if match:
                description = re.sub(r"\s+", " ", unescape(match.group(1))).strip()
            else:
                description = re.sub(r"<[^>]+>", " ", html)
                description = re.sub(r"\s+", " ", unescape(description)).strip()
            if len(description) < 80:
                return False
            job.description = description[:12000]
            job.evidence_level = "MEDIUM_EVIDENCE"
            return True
        except Exception:
            return False

    def collect(self) -> List[Job]:
        discovered: List[Job] = []
        feed_configs = [cfg for cfg in self.configs if cfg.get("url", "")]
        worker_count = min(self.MAX_CONCURRENT_FEEDS, len(feed_configs))
        if worker_count:
            # Feed servers are independent. A slow vendor feed must not delay
            # every later RSS query, while the small worker cap keeps request
            # pressure bounded.
            with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="rss-feed") as executor:
                feed_results = executor.map(
                    self._fetch_feed,
                    (cfg["url"] for cfg in feed_configs),
                )
                feed_items = list(feed_results)
        else:
            feed_items = []

        for cfg, raw_items in zip(feed_configs, feed_items):
            company = cfg.get("company", cfg.get("description", "RSS"))

            for item in raw_items:
                title_clean = normalize_title(item["title"])
                title_lower = title_clean.lower()

                # Filtro anti-ruído de páginas agregadoras (SPEC-008)
                noise_score, _ = calculate_noise_score(title_clean, item["link"])
                if noise_score >= 2:
                    self.record_candidate_rejection(
                        'rss', item.get('id'), title_clean, 'source_filter',
                        'Título/link identificado como página agregadora, não como vaga individual',
                        query={'feed': cfg.get('description', ''), 'url': cfg.get('url', '')},
                        url=item.get('link', ''), published_at=item.get('raw_date', ''),
                    )
                    continue

                if not self._matches_filters(title_lower, cfg):
                    excluded = [word for word in cfg.get('exclude_keywords', [])
                                if word.lower() in title_lower]
                    matched = [word for word in cfg.get('keywords', [])
                               if word.lower() in title_lower]
                    reason = (f"Título contém termo excluído: {', '.join(excluded)}" if excluded
                              else 'Título não corresponde às palavras-chave configuradas')
                    self.record_candidate_rejection(
                        'rss', item.get('id'), title_clean, 'source_filter', reason,
                        query={'feed': cfg.get('description', ''), 'url': cfg.get('url', ''),
                               'matched_keywords': matched},
                        url=item.get('link', ''), published_at=item.get('raw_date', ''),
                    )
                    continue

                # Parse temporal e filtro de recência (SPEC-008)
                dt = parse_feed_datetime(item.get("raw_date", ""))
                freshness, _ = classify_freshness(dt, max_age_hours=cfg.get("max_age_hours", 72))
                if freshness == "STALE":
                    self.record_candidate_rejection(
                        'rss', item.get('id'), title_clean, 'freshness_filter',
                        f"Anúncio fora da janela de {cfg.get('max_age_hours', 72)} horas",
                        query={'feed': cfg.get('description', ''), 'url': cfg.get('url', '')},
                        url=item.get('link', ''), published_at=dt.isoformat() if dt else '',
                    )
                    continue

                raw_link = item["link"]
                resolved_link = raw_link
                canonical_link = raw_link

                if self.resolve_urls and "news.google.com" in raw_link:
                    _, resolved_link, canonical_link, _ = resolve_url_tripartite(raw_link, self.http)

                job = Job(
                    title=title_clean,
                    company=company,
                    workplace_type="unknown",
                    location="",
                    description=re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", item.get("description", "")))).strip(),
                    job_type="Vaga Externa",
                    salary="Nao informado",
                    raw_url=raw_link,
                    resolved_url=resolved_link,
                    canonical_url=canonical_link,
                    evidence_level="MEDIUM_EVIDENCE" if len(item.get("description", "")) >= 80 else "LOW_EVIDENCE",
                    published_at=dt.isoformat() if dt else "",
                )
                job.add_source("rss", str(item["id"]), canonical_link or raw_link)
                if self.resolve_urls and resolved_link != raw_link:
                    self._enrich_resolved_page(job)
                if cfg.get("official") and len(job.description) >= 80:
                    job.evidence_level = "HIGH_EVIDENCE"
                if (
                    self.min_description_chars
                    and not cfg.get("official")
                    and len(job.description.strip()) < self.min_description_chars
                ):
                    self.record_candidate_rejection(
                        'rss', item.get('id'), title_clean, 'evidence_filter',
                        f"Descrição curta: {len(job.description.strip())} caracteres; mínimo {self.min_description_chars}",
                        query={'feed': cfg.get('description', ''), 'url': cfg.get('url', '')},
                        url=canonical_link or raw_link,
                        published_at=job.published_at,
                    )
                    continue
                discovered.append(job)

        return discovered
