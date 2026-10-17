import json
import os
import sys
import time
from pathlib import Path
from datetime import datetime, timedelta, timezone
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from dotenv import load_dotenv

from models.job import Job
from core.normalizer import is_location_allowed, is_pcd_exclusive
from core.scoring import evaluate_job
from core.deduplicator import Deduplicator, delivery_fingerprint
from collectors.gupy_collector import GupyCollector
from collectors.linkedin_collector import LinkedInCollector
from collectors.rss_collector import RssCollector
from collectors.github_collector import GithubIssuesCollector
from collectors.trampos_collector import TramposCollector
from collectors.manual_collector import ManualCollector
from collectors.geekhunter_collector import GeekHunterCollector
from notify.telegram_notifier import TelegramNotifier
from notify.email_notifier import ResendEmailNotifier
from notify.adapters import LegacyNotifierAdapter, WPPConnectAdapter, WhatsAppCloudAdapter
from storage.state_store import StateStore
from core.query_planner import plan_searches, unique_searches, rotate_searches
from core.delivery_workflow import finalize_delivery
from core.eligibility import classify_score, classify_evidence, classify_location
from core.scope_analyzer import analyze_scope
from core.metrics import summarize_cycle
from core.candidate_audit import persist_rejections
from core.ranking import order_for_alerts
from core import llm_judge
from collectors.base import collect_result

if os.getenv('JOB_FINDER_LOCAL_RUNTIME') != '1':
    load_dotenv()

# Garante suporte a UTF-8 no console do Windows
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

CONFIG_FILE = "config.json"
STATE_FILE = "seen_jobs.json"
HEALTH_REPORT_FILE = "cycle_health.json"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
RESEND_API_KEY = os.getenv("RESEND_API_KEY")
ALERT_EMAIL_FROM = os.getenv("ALERT_EMAIL_FROM")
ALERT_EMAIL_TO = os.getenv("ALERT_EMAIL_TO")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")
WHATSAPP_ACCESS_TOKEN = os.getenv("WHATSAPP_ACCESS_TOKEN")
WHATSAPP_PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID")
WHATSAPP_RECIPIENT = os.getenv("WHATSAPP_RECIPIENT")
WHATSAPP_TEMPLATE_NAME = os.getenv("WHATSAPP_TEMPLATE_NAME")
WHATSAPP_TEMPLATE_LANGUAGE = os.getenv("WHATSAPP_TEMPLATE_LANGUAGE", "pt_BR")
WPPCONNECT_API_URL = os.getenv("WPPCONNECT_API_URL")
WPPCONNECT_TOKEN = os.getenv("WPPCONNECT_TOKEN")
WPPCONNECT_SESSION = os.getenv("WPPCONNECT_SESSION")
WPPCONNECT_RECIPIENT = os.getenv("WPPCONNECT_RECIPIENT")


def get_http_session() -> requests.Session:
    """Retorna sessão requests com uma retentativa curta e observável.

    Cada collector já possui timeout e tratamento de erro próprios. Três
    retries globais com backoff podiam esconder uma falha por dezenas de
    segundos e empurrar o ciclo para o watchdog.
    """
    session = requests.Session()
    retries = Retry(
        total=1,
        connect=1,
        read=1,
        status=1,
        backoff_factor=0.5,
        # A 429 is an explicit provider cooldown signal. Retrying it here
        # defeats the source-level circuit breaker and adds more pressure.
        status_forcelist=[500, 502, 503, 504],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retries)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


HTTP = get_http_session()


def format_linkedin_query_stats(query):
    """Format a LinkedIn query record without assuming a live search ran."""
    return (
        f"│  └─ busca='{query.get('keywords', '*')}' janela={query.get('time_range', 'n/a')} "
        f"páginas={query.get('pages', 0)} cartões={query.get('cards', 0)} "
        f"válidas={query.get('parsed_jobs', 0)} enriquecidas={query.get('enrichment_successes', 0)}/"
        f"{query.get('enrichment_attempts', 0)} status={query.get('status', 'UNKNOWN')}"
    )


def load_config() -> dict:
    if not os.path.exists(CONFIG_FILE):
        return {"check_interval_minutes": 60, "monitors": []}
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def write_cycle_progress(cycle_id, phase, sources=None, funnel=None, error=None):
    """Persist a small, replaceable checkpoint for the current cycle.

    The watchdog may terminate the worker before ``cycle_health.json`` is
    written. This checkpoint deliberately contains only operational counters,
    so it is safe to recover into a timeout report without duplicating jobs or
    decisions.
    """
    try:
        directory = Path(HEALTH_REPORT_FILE).parent / 'cycle-progress'
        directory.mkdir(parents=True, exist_ok=True)
        payload = {
            'cycle_id': cycle_id,
            'updated_at': datetime.now().isoformat(timespec='seconds'),
            'phase': phase,
            'sources': sources or {},
            'funnel': funnel or {},
        }
        if error:
            payload['error'] = str(error)[:300]
        temporary = directory / f'{cycle_id}.tmp'
        target = directory / f'{cycle_id}.json'
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
        temporary.replace(target)
    except OSError as exc:
        # A diagnostic checkpoint must never make a healthy collection fail.
        print(f'Checkpoint de ciclo indisponível: {type(exc).__name__}')


def check_heartbeat(config: dict, store: StateStore, notifier: TelegramNotifier):
    """Respeita a frequência configurada sem repetir mensagens de status."""
    hb_cfg = config.get("heartbeat", {})
    if not hb_cfg.get("enabled", False):
        return

    now = datetime.now()
    today_str = now.strftime("%Y-%m-%d")
    target_hour = hb_cfg.get("hour_start", 9)

    last_heartbeat = store.get_last_heartbeat()
    if last_heartbeat == today_str:
        return
    if hb_cfg.get("frequency") == "weekly" and last_heartbeat:
        try:
            if (now.date() - datetime.fromisoformat(last_heartbeat).date()).days < 7:
                return
        except ValueError:
            pass

    if now.hour >= target_hour:
        monitors_count = len(config.get("monitors", []))
        total_tracked = len(store.state.get("seen_fingerprints", []))
        if notifier.send_heartbeat(monitors_count, total_tracked):
            print(f"[{now.strftime('%H:%M:%S')}] Status do radar enviado ao Telegram.")
            store.set_last_heartbeat(today_str)
            store.save()


def run_check():
    config = load_config()
    llm_judge.reset_cycle_stats()
    manual_only = os.getenv('JOB_FINDER_MANUAL_ONLY') == '1'
    store = StateStore(STATE_FILE)
    cycle_id = datetime.now().strftime("%Y%m%dT%H%M%S") + "-" + __import__("uuid").uuid4().hex[:8]
    store.state["active_cycle_id"] = cycle_id
    notifier = TelegramNotifier(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, HTTP)
    email_notifier = ResendEmailNotifier(
        RESEND_API_KEY, ALERT_EMAIL_FROM, ALERT_EMAIL_TO, HTTP
    )
    whatsapp_adapter = WhatsAppCloudAdapter(
        WHATSAPP_ACCESS_TOKEN, WHATSAPP_PHONE_NUMBER_ID, WHATSAPP_RECIPIENT,
        WHATSAPP_TEMPLATE_NAME, WHATSAPP_TEMPLATE_LANGUAGE, HTTP
    )
    wppconnect_adapter = WPPConnectAdapter(
        WPPCONNECT_API_URL, WPPCONNECT_TOKEN, WPPCONNECT_SESSION,
        WPPCONNECT_RECIPIENT, HTTP
    )
    notification_adapters = [
        LegacyNotifierAdapter(notifier, 'telegram'),
        LegacyNotifierAdapter(email_notifier, 'email'),
        whatsapp_adapter,
        wppconnect_adapter,
    ]
    durable = hasattr(store, 'pending_jobs')
    recovered_deliveries = 0
    if durable:
        from core.durable_delivery import deliver
        for pending in store.pending_jobs():
            ids = [f'{name}:{item.source_job_id}' for name, item in pending.sources.items()]
            recovered_deliveries += int(deliver(
                store, pending, ids, notifier, email_notifier,
                adapters=notification_adapters,
            ))
    deduplicator = Deduplicator()
    min_score = config.get("min_match_score", 50)  # Padrão: 50 pts mínimos para alertar
    progression_min_score = config.get("progression_min_score", 35)

    monitors = config.get("monitors", [])
    start_time = time.time()
    now_str = datetime.now().strftime("%H:%M:%S")
    print(f"[{now_str}] Iniciando patrulha com {len(monitors)} monitor(es)...")
    write_cycle_progress(cycle_id, 'started')

    # 1. Agrupa configurações por tipo de coletor
    gupy_queries = []
    linkedin_searches = []
    rss_configs = []
    github_configs = []
    trampos_configs = []
    geekhunter_configs = []

    for m in monitors:
        m_type = m.get("type", "")
        if m_type == "gupy":
            for planned_query in plan_searches([m], "gupy"):
                query_args = {
                    k: v for k, v in planned_query.items()
                    if k not in ["type", "description", "only_remote", "strict_location", "keywords", "exclude_keywords", "query", "query_variants"]
                }
                # A configured term is the legacy default. Explicit variants
                # must replace it or every planned Gupy request repeats one term.
                if planned_query.get("query"):
                    query_args["term"] = planned_query["query"]
                if "limit" not in query_args:
                    query_args["limit"] = 20
                query_args["query_id"] = planned_query["query_id"]
                gupy_queries.append(query_args)
        elif m_type == "linkedin":
            for planned_query in plan_searches([m], "linkedin"):
                search = dict(planned_query)
                search["keywords"] = planned_query["query"]
                search["max_pages"] = min(
                    int(m.get("max_pages", 3)),
                    int(config.get("linkedin_max_pages_per_query", 1)),
                )
                search["enrich_details"] = bool(config.get("linkedin_detail_enrichment_enabled", False))
                search["detail_enrichment_limit"] = 0
                linkedin_searches.append(search)
        elif m_type == "rss":
            rss_configs.append(m)
        elif m_type == "github_issues":
            github_configs.append(m)
        elif m_type == "trampos":
            trampos_configs.append(m)
        elif m_type == "geekhunter":
            geekhunter_configs.append(m)

    # 2. Executa a Coleta (Fase de Descoberta / Recall Alto)
    discovered_gupy = []
    discovered_linkedin = []
    discovered_rss = []
    discovered_github = []
    discovered_trampos = []
    discovered_manual = []
    gupy_status = "OK"
    linkedin_status = "OK"
    linkedin_query_stats = []
    gupy_query_stats = []
    rss_status = "OK"
    github_status = "OK"
    trampos_status = "OK"
    geekhunter_status = "OK"
    candidate_audit_collectors = []

    if durable:
        try:
            manual_col = ManualCollector(HTTP, store)
            candidate_audit_collectors.append(manual_col)
            manual_result = collect_result(manual_col)
            discovered_manual, manual_status = manual_result.jobs, manual_result.status
            for attempt in manual_col.collection_attempts:
                attempt['cycle_id'] = cycle_id
                store.record_collection_attempt(attempt)
        except Exception as e:
            manual_status = f"FALHA ({e})"
        write_cycle_progress(cycle_id, 'manual', {'manual': {
            'status': manual_status, 'discovered': len(discovered_manual)
        }})
    else:
        manual_status = "NOT_CONFIGURED"

    if geekhunter_configs and not manual_only:
        for cfg in geekhunter_configs:
            gh_col = GeekHunterCollector(HTTP, cfg.get('keywords'), cfg.get('exclude_keywords'), cfg.get('max_jobs', 30))
            candidate_audit_collectors.append(gh_col)
            result = collect_result(gh_col)
            discovered_manual.extend(result.jobs)
            if result.status != 'OK':
                geekhunter_status = result.status
            for attempt in gh_col.collection_attempts:
                attempt['cycle_id'] = cycle_id
                if durable:
                    store.record_collection_attempt(attempt)
        write_cycle_progress(cycle_id, 'geekhunter', {'geekhunter': {
            'status': geekhunter_status, 'discovered': len(discovered_manual)
        }})

    if gupy_queries and not manual_only:
        try:
            gupy_col = GupyCollector(
                HTTP,
                gupy_queries,
                detail_limit=config.get("gupy_detail_enrichment_limit", 5),
                max_age_hours=config.get("gupy_max_age_hours", 72),
                require_publication_date=config.get("gupy_require_publication_date", True),
            )
            candidate_audit_collectors.append(gupy_col)
            result = collect_result(gupy_col)
            discovered_gupy, gupy_status = result.jobs, result.status
            gupy_query_stats = gupy_col.query_stats
            for attempt in gupy_col.collection_attempts:
                attempt['cycle_id'] = cycle_id
                if durable:
                    store.record_collection_attempt(attempt)
        except Exception as e:
            gupy_status = f"FALHA ({e})"
        write_cycle_progress(cycle_id, 'gupy', {'gupy': {
            'status': gupy_status, 'discovered': len(discovered_gupy)
        }} )

    if linkedin_searches and not manual_only:
        cooldown = store.get_source_cooldown("linkedin")
        if cooldown:
            linkedin_status = "COOLDOWN"
            linkedin_query_stats = [{
                "keywords": "*",
                "time_range": "cooldown",
                "pages": 0,
                "cards": 0,
                "parsed_jobs": 0,
                "enrichment_successes": 0,
                "enrichment_attempts": 0,
                "status": "COOLDOWN",
                "reason": "HTTP_429",
                "until": cooldown["until"],
                "consecutive_failures": cooldown["consecutive_failures"],
            }]
            print(f"[LinkedInCollector] Em cooldown até {cooldown['until']}; busca suspensa após HTTP 429.")
        else:
            poll_interval = max(1, int(config.get("linkedin_poll_interval_hours", 6))) * 3600
            last_poll = store.get_last_source_poll("linkedin")
            if last_poll:
                try:
                    last_poll_at = datetime.fromisoformat(last_poll)
                    if last_poll_at.tzinfo is None:
                        last_poll_at = last_poll_at.replace(tzinfo=timezone.utc)
                    elapsed = (datetime.now(timezone.utc) - last_poll_at).total_seconds()
                except (TypeError, ValueError):
                    elapsed = poll_interval
                if elapsed < poll_interval:
                    next_poll = last_poll_at + timedelta(seconds=poll_interval)
                    linkedin_status = "THROTTLED"
                    linkedin_query_stats = [{
                        "keywords": "*", "status": "THROTTLED", "pages": 0,
                        "reason": "SOURCE_POLL_INTERVAL",
                        "next_check_at": next_poll.isoformat(),
                    }]
                    print(f"[LinkedInCollector] Consulta adiada até {next_poll.isoformat()} para reduzir rate limit.")
            if linkedin_status != "THROTTLED":
                try:
                    max_linkedin_queries = int(config.get("linkedin_max_queries", 8))
                    linkedin_searches = rotate_searches(
                        unique_searches(linkedin_searches), max_linkedin_queries,
                        slot=int(time.time() // (max(1, int(config.get("check_interval_minutes", 60))) * 60)),
                    )
                    li_col = LinkedInCollector(HTTP, linkedin_searches)
                    candidate_audit_collectors.append(li_col)
                    result = collect_result(li_col)
                    discovered_linkedin, linkedin_status = result.jobs, result.status
                    linkedin_query_stats = li_col.query_stats
                    for attempt in getattr(li_col, "collection_attempts", []):
                        attempt['cycle_id'] = cycle_id
                        if durable:
                            store.record_collection_attempt(attempt)
                    if li_col.rate_limited:
                        cooldown = store.register_source_rate_limit("linkedin")
                        linkedin_status = "PARTIAL"
                        print(f"[LinkedInCollector] Cooldown persistente de {cooldown['cooldown_seconds']}s registrado.")
                    elif linkedin_status == "OK":
                        store.clear_source_cooldown("linkedin")
                except Exception as e:
                    linkedin_status = f"FALHA ({e})"
        write_cycle_progress(cycle_id, 'linkedin', {'linkedin': {
            'status': linkedin_status, 'discovered': len(discovered_linkedin)
        }} )

    if rss_configs and not manual_only:
        try:
            rss_col = RssCollector(
                HTTP,
                rss_configs,
                resolve_urls=config.get("resolve_rss_urls", False),
                min_description_chars=config.get("rss_min_description_chars", 0),
            )
            candidate_audit_collectors.append(rss_col)
            result = collect_result(rss_col)
            discovered_rss, rss_status = result.jobs, result.status
            for attempt in rss_col.collection_attempts:
                attempt['cycle_id'] = cycle_id
                if durable:
                    store.record_collection_attempt(attempt)
        except Exception as e:
            rss_status = f"FALHA ({e})"
        write_cycle_progress(cycle_id, 'rss', {'rss': {
            'status': rss_status, 'discovered': len(discovered_rss)
        }} )

    if github_configs and not manual_only:
        try:
            for cfg in github_configs:
                gh_col = GithubIssuesCollector(
                    HTTP,
                    repos=cfg.get("repos"),
                    keywords=cfg.get("keywords"),
                    exclude_keywords=cfg.get("exclude_keywords"),
                    max_pages=cfg.get("max_pages", 3),
                    lookback_days=cfg.get("lookback_days", 1),
                    fallback_lookback_days=cfg.get("fallback_lookback_days", 3),
                    strict_freshness=cfg.get("strict_freshness", True),
                )
                candidate_audit_collectors.append(gh_col)
                result = collect_result(gh_col)
                discovered_github.extend(result.jobs)
                if result.status != 'OK':
                    github_status = result.status
                for attempt in gh_col.collection_attempts:
                    attempt['cycle_id'] = cycle_id
                    if durable:
                        store.record_collection_attempt(attempt)
        except Exception as e:
            github_status = f"FALHA ({e})"
        write_cycle_progress(cycle_id, 'github', {'github': {
            'status': github_status, 'discovered': len(discovered_github)
        }} )

    if trampos_configs and not manual_only:
        try:
            for cfg in trampos_configs:
                t_col = TramposCollector(
                    HTTP,
                    keywords=cfg.get("keywords"),
                    exclude_keywords=cfg.get("exclude_keywords"),
                    max_pages=cfg.get("max_pages", 1)
                )
                candidate_audit_collectors.append(t_col)
                result = collect_result(t_col)
                discovered_trampos.extend(result.jobs)
                if result.status != 'OK':
                    trampos_status = result.status
                for attempt in t_col.collection_attempts:
                    attempt['cycle_id'] = cycle_id
                    if durable:
                        store.record_collection_attempt(attempt)
        except Exception as e:
            trampos_status = f"FALHA ({e})"
        write_cycle_progress(cycle_id, 'trampos', {'trampos': {
            'status': trampos_status, 'discovered': len(discovered_trampos)
        }} )

    persist_rejections(candidate_audit_collectors, store, cycle_id)
    discovered_jobs = discovered_manual + discovered_gupy + discovered_linkedin + discovered_rss + discovered_github + discovered_trampos

    # 3. Deduplicação e Fusão de Múltiplas Fontes
    unique_jobs = deduplicator.process(discovered_jobs)
    write_cycle_progress(cycle_id, 'deduplicated', {
        'gupy': {'status': gupy_status, 'discovered': len(discovered_gupy)},
        'linkedin': {'status': linkedin_status, 'discovered': len(discovered_linkedin)},
        'rss': {'status': rss_status, 'discovered': len(discovered_rss)},
        'github': {'status': github_status, 'discovered': len(discovered_github)},
        'trampos': {'status': trampos_status, 'discovered': len(discovered_trampos)},
    }, {'raw': len(discovered_jobs), 'unique': len(unique_jobs)})

    # 4. Decisão e Classificação (Scoring + Localidade Estrita + Filtro PCD)
    notified_count = recovered_deliveries
    discarded_seen = 0
    discarded_pcd = 0
    discarded_location = 0
    discarded_senior = 0
    discarded_score = 0
    discarded_scope = 0
    fallback_count = 0
    approved_jobs = []

    for idx, job in enumerate(unique_jobs, 1):
        fp = delivery_fingerprint(job)
        source_ids = [s.source_job_id for s in job.sources.values()]
        if durable:
            store.remember_job(job)
            source_ids = [f'{name}:{item.source_job_id}' for name, item in job.sources.items()]

        # Se já foi notificada anteriormente, ignora
        if store.is_seen(fp, source_ids[0] if source_ids else ""):
            discarded_seen += 1
            continue
        if not store.delivery_retry_allowed(fp):
            discarded_seen += 1
            continue

        # Auditoria individual da decisão
        print(f"\n--- [Auditoria Vaga #{idx}] ---")
        print(f"Empresa: {job.company} | Título: {job.title}")
        print(f"Modalidade: {job.workplace_type} | Local: {job.location or 'Não especificado'}")

        job_id = source_ids[0] if source_ids else "unknown"
        primary_source = next(iter(job.sources.keys()), "unknown")

        # Heurística PCD: bloqueia apenas com sinal forte no título
        is_pcd, pcd_reason = is_pcd_exclusive(job.title)
        if is_pcd:
            job.pcd_signal = "TITLE"
            print(f"✗ PCD: BLOQUEADA ({pcd_reason})")
            print(f"DECISÃO: DESCARTADA (Vaga Afirmativa PCD)")
            discarded_pcd += 1
            store.record_decision(
                job_id, primary_source, job.identity_fingerprint, job.content_hash,
                "DISCARD_PCD", pcd_reason,
                raw_url=job.raw_url, canonical_url=job.canonical_url, evidence_level=job.evidence_level
            )
            store.mark_seen(fp, source_ids)
            continue

        # Validação de Localidade Estrita (Tatuí / Sorocaba / Remoto)
        location_decision, loc_reason = classify_location(job)
        if location_decision == "LOCATION_REJECTED":
            print(f"✗ Localização: REJEITADA ({loc_reason})")
            print(f"DECISÃO: DESCARTADA (Filtro Regional)")
            discarded_location += 1
            store.record_decision(
                job_id, primary_source, job.identity_fingerprint, job.content_hash,
                "DISCARD_LOCATION", loc_reason,
                raw_url=job.raw_url, canonical_url=job.canonical_url, evidence_level=job.evidence_level
            )
            store.mark_seen(fp, source_ids)
            continue
        else:
            print(f"✓ Localização: APROVADA ({loc_reason})")

        # Google News/RSS normalmente entrega apenas um título e um link de notícia.
        # Sem descrição ou metadados suficientes, não há evidência para transformar
        # o item em alerta de candidatura; isso evita ruído no Telegram.
        if classify_evidence(job) == "LOW_EVIDENCE":
            reason = "RSS sem evidência suficiente para confirmar uma vaga real"
            print(f"✗ Evidência: INSUFICIENTE ({reason})")
            print("DECISÃO: DESCARTADA (RSS raso)")
            discarded_score += 1
            store.record_decision(
                job_id, primary_source, job.identity_fingerprint, job.content_hash,
                "DISCARD_LOW_EVIDENCE", reason,
                raw_url=job.raw_url, canonical_url=job.canonical_url,
                evidence_level=job.evidence_level
            )
            store.mark_seen(fp, source_ids)
            continue

        # A LinkedIn card without its public detail is only a lead, not enough
        # evidence for an alert. Keep it audited as seen and wait for a later
        # enriched observation instead of sending a misleading recommendation.
        if "linkedin" in job.sources and not (job.description or "").strip():
            reason = "LinkedIn sem descrição pública enriquecida; destino e requisitos não confirmados"
            print(f"✗ Evidência: INSUFICIENTE ({reason})")
            discarded_score += 1
            store.record_decision(
                job_id, primary_source, job.identity_fingerprint, job.content_hash,
                "DISCARD_LOW_EVIDENCE", reason,
                raw_url=job.raw_url, canonical_url=job.canonical_url,
                evidence_level=job.evidence_level
            )
            # Não marcar como visto: o detalhe público pode ficar disponível
            # no próximo ciclo, e a vaga deve poder ser enriquecida novamente.
            continue

        # Cálculo do Score: Juiz Semântico com Fallback Heurístico
        # A análise de escopo acontece antes da decisão final: o título anunciado
        # pode ser pleno, mas a rotina diária ainda ser compatível com júnior.
        scope = analyze_scope(job)
        for preserved_key in ("duplicate_listings", "semantic_fingerprint"):
            if job.analysis.get(preserved_key):
                scope[preserved_key] = job.analysis[preserved_key]
        job.analysis = scope
        job.compatibility_category = scope["category"]
        job.potential_score = scope["potential_score"]
        job.operational_seniority = scope["operational_level"]
        job.ranking_evidence["declared_seniority"] = scope["declared_level"]
        job.ranking_evidence["operational_seniority"] = scope["operational_level"]

        score, reasons = evaluate_job(job, is_rss=("rss" in job.sources))
        if any("LLM Judge" in r for r in reasons):
            store.record_llm_call()
        if any("Fallback Heurístico Ativado" in r for r in reasons):
            fallback_count += 1

        job.match_score = score
        gap_note = f"Lacunas treináveis: {', '.join(scope['trainable_gaps'][:3])}" if scope["trainable_gaps"] else ""
        job.match_reasons = reasons + [f"Categoria: {scope['category']}", f"Leitura do escopo: {scope['reasoning']}"]
        if gap_note:
            job.match_reasons.append(gap_note)

        # A classe explicável decide elegibilidade; score apenas ordena vagas.
        # A progressão inclui pleno com sinais de escopo acessível e lacunas limitadas.
        progression = (
            scope["category"] == "POTENCIALMENTE_COMPATIVEL"
            and scope["potential_score"] >= max(45, progression_min_score)
            and scope["declared_level"] == "mid"
            and scope["operational_level"] in {"junior", "junior_to_mid", "mid"}
            and len((job.description or "").strip()) >= 180
            and not scope["hard_barriers"]
        )

        direct_compatible = (
            scope["category"] == "COMPATIVEL"
            or (scope["category"] == "POTENCIALMENTE_COMPATIVEL" and scope["declared_level"] == "junior")
        ) and not scope["hard_barriers"]
        if scope["hard_barriers"] or scope["category"] == "INCOMPATIVEL":
            reason = "; ".join(scope["hard_barriers"] or scope["trainable_gaps"] or [scope["reasoning"]])
            print(f"✗ Escopo: DESCARTADO ({reason})")
            discarded_scope += 1
            store.record_decision(
                job_id, primary_source, job.identity_fingerprint, job.content_hash,
                "DISCARD_SCOPE", reason, final_score=score,
                raw_url=job.raw_url, canonical_url=job.canonical_url,
                evidence_level=job.evidence_level, category=scope["category"],
                potential_score=scope["potential_score"], operational_seniority=scope["operational_level"],
            )
            store.mark_seen(fp, source_ids)
            continue

        if not direct_compatible and not progression:
            reason = f"Categoria de escopo {scope['category']} não atende às regras de alerta"
            print(f"✗ Escopo: DESCARTADO ({reason})")
            discarded_scope += 1
            store.record_decision(
                job_id, primary_source, job.identity_fingerprint, job.content_hash,
                "DISCARD_SCOPE", reason, final_score=score,
                raw_url=job.raw_url, canonical_url=job.canonical_url,
                evidence_level=job.evidence_level, category=scope["category"],
                potential_score=scope["potential_score"], operational_seniority=scope["operational_level"],
            )
            store.mark_seen(fp, source_ids)
            continue

        decision = classify_score(score, min_score)
        if decision == "VETO":
            veto_reason = next((r for r in reasons if "LLM Judge" in r), None)
            label = "Vaga não-real (Veto LLM)" if veto_reason else "Senioridade/Relevância"
            print(f"✗ {label}: BLOQUEADA (Score <= 0)")
            for r in reasons:
                print(f"   • {r}")
            print(f"DECISÃO: DESCARTADA ({label})")
            discarded_senior += 1
            store.record_decision(
                job_id, primary_source, job.identity_fingerprint, job.content_hash,
                "DISCARD_SENIOR_OR_VETO", veto_reason or label, final_score=score,
                raw_url=job.raw_url, canonical_url=job.canonical_url, evidence_level=job.evidence_level
            )
            store.mark_seen(fp, source_ids)
            continue

        if decision == "LOW_SCORE" and not (direct_compatible or progression):
            print(f"✗ Match Score: {score}/100 (Abaixo do mínimo {min_score})")
            discarded_score += 1
            store.record_decision(
                job_id, primary_source, job.identity_fingerprint, job.content_hash,
                "DISCARD_LOW_SCORE", reasons[0] if reasons else "Score insuficiente", final_score=score,
                raw_url=job.raw_url, canonical_url=job.canonical_url, evidence_level=job.evidence_level,
                category=scope["category"], potential_score=scope["potential_score"],
                operational_seniority=scope["operational_level"],
            )
            store.mark_seen(fp, source_ids)
            continue

        # Aprovada em todos os critérios; a entrega ocorre após ordenar o lote.
        approved_jobs.append((job, source_ids, reasons))

    for job, source_ids, reasons in sorted(
        approved_jobs,
        key=lambda item: (
            item[0].freshness_score,
            item[0].match_score,
            item[0].learning_interest_score,
        ),
        reverse=True,
    ):
        fp = delivery_fingerprint(job)
        if durable:
            notified_count += int(deliver(
                store, job, source_ids, notifier, email_notifier,
                adapters=notification_adapters,
            ))
            continue
        if not store.claim_delivery(fp):
            discarded_seen += 1
            continue
        print(f"✓ Match Score: {job.match_score}/100 (Aprovado >= {min_score})")
        for reason in reasons:
            print(f"   • {reason}")
        print(f"🎯 DECISÃO: NOTIFICAR TELEGRAM")

        attempt_id = store.begin_delivery_attempt(fp, source_ids, "telegram", cycle_id)
        sent = notifier.send_job_alert(job)
        delivery_channel = "telegram"
        store.finish_delivery_attempt(attempt_id, "DELIVERED" if sent else "FAILED")
        if not sent:
            print("⚠️ Telegram falhou; tentando fallback por e-mail via Resend.")
            email_attempt = store.begin_delivery_attempt(fp, source_ids, "email", cycle_id)
            sent = email_notifier.send_job_alert(job)
            delivery_channel = "email" if sent else "none"
            store.finish_delivery_attempt(email_attempt, "DELIVERED" if sent else "FAILED")
        if sent:
            notified_count += 1
            print(f"✓ Alerta entregue pelo canal: {delivery_channel}")
        else:
            print("⚠️ Telegram e Resend falharam; vaga ficará disponível para retry no próximo ciclo.")
        finalize_delivery(store, job, source_ids, sent, job.match_score, reasons[0] if reasons else "Aprovada")

    elapsed = time.time() - start_time
    max_cycle_seconds = config.get("max_cycle_seconds", 180)
    llm_usage = store.get_llm_usage()
    today_llm_calls = llm_usage.get("calls", 0)

    # Relatório Estruturado do Funil e Saúde do Sistema
    print("\n" + "=" * 48)
    print(" 📊 FUNIL DE EXECUÇÃO E SAÚDE DO SISTEMA")
    print("=" * 48)
    print(f"├─ Gupy:     {gupy_status:<8} | {len(discovered_gupy)} vaga(s)")
    for query in gupy_query_stats:
        print(
            f"│  └─ parâmetros={list(query['parameters'].keys())} "
            f"resultados={query['results']} status={query['status']}"
        )
    print(f"├─ LinkedIn: {linkedin_status:<8} | {len(discovered_linkedin)} vaga(s)")
    for query in linkedin_query_stats:
        print(format_linkedin_query_stats(query))
    print(f"├─ RSS:      {rss_status:<8} | {len(discovered_rss)} vaga(s)")
    print(f"├─ GitHub:   {github_status:<8} | {len(discovered_github)} vaga(s)")
    print("├" + "─" * 46)
    print(f"├─ Descoberta Bruta:    {len(discovered_jobs)}")
    print(f"├─ Vagas Únicas:        {len(unique_jobs)}")
    print(f"├─ Já Vistas:           {discarded_seen}")
    print(f"├─ Rejeitadas PCD:      {discarded_pcd}")
    print(f"├─ Rejeitadas Região:   {discarded_location}")
    print(f"├─ Rejeitadas Nível:    {discarded_senior}")
    print(f"├─ Rejeitadas Score:    {discarded_score}")
    print(f"├─ Rejeitadas Escopo:   {discarded_scope}")
    # Count only durable, confirmed deliveries from this cycle.
    notified_count = store.delivered_count_for_cycle(cycle_id)
    print(f"├─ 🎯 Notificadas:       {notified_count}")
    cycle_metrics = summarize_cycle(
        len(discovered_jobs),
        len(unique_jobs),
        notified_count,
        {"pcd": discarded_pcd, "location": discarded_location, "seniority": discarded_senior, "score": discarded_score, "scope": discarded_scope},
        {"gupy": gupy_status, "linkedin": linkedin_status, "rss": rss_status, "github": github_status, "trampos": trampos_status},
    )
    health_report = {
        "finished_at": datetime.now().isoformat(timespec="seconds"),
        "status": "DEGRADED" if cycle_metrics['source_failures'] else "SUCCESS",
        "duration_seconds": round(elapsed, 1),
        "sources": {
            "gupy": {"status": gupy_status, "discovered": len(discovered_gupy), "queries": gupy_query_stats},
            "linkedin": {"status": linkedin_status, "discovered": len(discovered_linkedin), "queries": linkedin_query_stats},
            "rss": {"status": rss_status, "discovered": len(discovered_rss)},
            "github": {"status": github_status, "discovered": len(discovered_github)},
            "trampos": {"status": trampos_status, "discovered": len(discovered_trampos)},
        },
        "funnel": {
            "raw": len(discovered_jobs),
            "unique": len(unique_jobs),
            "seen": discarded_seen,
            "discarded": {
                "pcd": discarded_pcd,
                "location": discarded_location,
                "seniority": discarded_senior,
                "score": discarded_score,
            },
            "notified": notified_count,
        },
        "metrics": cycle_metrics,
        "llm_calls_today": today_llm_calls,
        "llm_fallbacks": fallback_count,
    }
    with open(HEALTH_REPORT_FILE, "w", encoding="utf-8") as report_file:
        json.dump(health_report, report_file, ensure_ascii=False, indent=2)
    try:
        (Path(HEALTH_REPORT_FILE).parent / 'cycle-progress' / f'{cycle_id}.json').unlink(missing_ok=True)
    except OSError:
        pass
    print(f"├─ Duplicatas:           {cycle_metrics['duplicate_count']} ({cycle_metrics['duplicate_rate']:.1%})")
    print(f"├─ Precisão/Recall:      N/D ({cycle_metrics['precision_recall_note']})")
    print(f"├─ Falhas de fonte:      {', '.join(cycle_metrics['source_failures']) or 'nenhuma'}")
    print("├" + "─" * 46)
    print(f"├─ LLM Calls Hoje:     {today_llm_calls}/1000 (RPD)")
    fallback_str = f"{fallback_count} vaga(s)" if fallback_count > 0 else "0 (LLM 100% ativo)"
    print(f"├─ Fallback Heurístico: {fallback_str}")
    print(f"├─ Duração:             {elapsed:.1f}s")
    if elapsed > max_cycle_seconds:
        print(f"⚠️ Limite operacional excedido: {elapsed:.1f}s > {max_cycle_seconds}s")
    print(f"└─ Status do Ciclo:     {health_report['status']}")
    print("=" * 48 + "\n")

    if fallback_count > 0:
        print(f"⚠️  ALERTA: O Fallback Heurístico foi acionado em {fallback_count} vaga(s) devido a indisponibilidade ou rate limit da IA!\n")

    # 5. Heartbeat e Persistência
    store.record_source_health("gupy", gupy_status, len(discovered_gupy), gupy_query_stats)
    store.record_source_health("linkedin", linkedin_status, len(discovered_linkedin), linkedin_query_stats)
    store.record_source_health("rss", rss_status, len(discovered_rss))
    store.record_source_health("github", github_status, len(discovered_github))
    store.record_source_health("trampos", trampos_status, len(discovered_trampos))
    if durable:
        from core.operational_alerts import update_source_alerts, send_operational
        update_source_alerts(store, {
            'gupy': gupy_status if gupy_queries else 'NOT_CONFIGURED',
            'linkedin': linkedin_status if linkedin_searches else 'NOT_CONFIGURED',
            'rss': rss_status if rss_configs else 'NOT_CONFIGURED',
            'github': github_status if github_configs else 'NOT_CONFIGURED',
            'trampos': trampos_status if trampos_configs else 'NOT_CONFIGURED',
        }, lambda message: send_operational(HTTP, message))
    check_heartbeat(config, store, notifier)
    if os.getenv('JOB_FINDER_MANUAL_ONLY') == '1':
        from core.telegram_inbox import notify_finished_manual_analyses
        notify_finished_manual_analyses(store, HTTP, TELEGRAM_BOT_TOKEN)
    store.save()


def main():
    if any(flag in sys.argv for flag in ('--due', '--diagnose', '--migrate', '--dry-run')) or ('--once' in sys.argv and os.getenv('JOB_FINDER_LOCAL_RUNTIME') == '1'):
        from core.local_runtime import main as local_main
        local_main(sys.modules[__name__], sys.argv[1:])
        return
    config = load_config()
    interval = config.get("check_interval_minutes", 60) * 60

    if "--once" in sys.argv:
        run_check()
        return

    if "--heartbeat" in sys.argv:
        store = StateStore(STATE_FILE)
        notifier = TelegramNotifier(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, HTTP)
        monitors_count = len(config.get("monitors", []))
        total_tracked = len(store.state.get("seen_fingerprints", []))
        ok = notifier.send_heartbeat(monitors_count, total_tracked)
        if ok:
            print("💚 Heartbeat de teste enviado com sucesso ao Telegram!")
        else:
            print("❌ Falha ao enviar Heartbeat.")
        return

    print("========================================")
    print("   JOB FINDER - RADAR PROFISSIONAL      ")
    print("========================================")
    print(f"Intervalo: {interval // 60} minuto(s)")
    print("Pressione Ctrl+C para parar.\n")

    while True:
        try:
            run_check()
            time.sleep(interval)
        except KeyboardInterrupt:
            print("\nMonitor encerrado pelo usuário.")
            break
        except Exception as e:
            print(f"Erro inesperado: {e}")
            time.sleep(30)


if __name__ == "__main__":
    main()
