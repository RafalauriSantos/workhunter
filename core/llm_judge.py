"""
LLM Judge: camada semântica que roda em cima do scoring heurístico.
Só é chamado para vagas que já passaram no heurístico com um piso mínimo,
pra não gastar chamada de API em lixo óbvio (sênior, vaga não-dev, etc).

Usa Gemini por padrão, permite OpenRouter como alternativa e mantém o fallback
heurístico quando nenhuma chave estiver configurada ou quando a chamada falhar.
"""
import json
import logging
import os
import random
import time
from pathlib import Path
from typing import Optional, Dict, Any

import requests

logger = logging.getLogger(__name__)

_PROFILE_PATH = Path(__file__).parent.parent / "profile.json"
_HEURISTIC_FLOOR = 30  # abaixo disso, nem chama o LLM

# Pacing preventivo: 15 RPM = 1 chamada a cada 4.0s. Usamos 4.5s + jitter para garantir folga
PACING_SECONDS: float = 4.5
_LAST_CALL_TIMESTAMP: float = 0.0
_PROVIDER_UNAVAILABLE = set()
_PROVIDER_UNAVAILABLE_UNTIL = {}
_PROVIDER_COOLDOWN_SECONDS = 300
_CYCLE_CALLS = 0
_HEALTH_OBSERVER = None
_REQUEST_TIMEOUT_SECONDS = float(os.getenv("LLM_REQUEST_TIMEOUT_SECONDS", "12"))
_MAX_RATE_LIMIT_RETRIES = 1

_PROFILE_CACHE = None


def get_profile() -> dict:
    global _PROFILE_CACHE
    if _PROFILE_CACHE is None:
        if _PROFILE_PATH.exists():
            with open(_PROFILE_PATH, encoding="utf-8") as f:
                _PROFILE_CACHE = json.load(f)
        else:
            _PROFILE_CACHE = {}
    return _PROFILE_CACHE


JUDGE_PROMPT = """Você é um triador técnico de vagas para um candidato específico.
Analise se o texto abaixo é uma vaga real de desenvolvedor compatível com o perfil.

PERFIL DO CANDIDATO:
{profile_json}

TÍTULO: {title}
EMPRESA: {company}
DESCRIÇÃO:
{description}

Regras de julgamento:
- Se NÃO for uma oportunidade de vaga real (despedida, desabafo, notícia institucional, vaga não-dev), marque is_real_job_opportunity=false.
- Conte competências demonstradas em projetos próprios como evidência prática de aderência técnica.
- Não converta projetos próprios em anos de experiência profissional nem em senioridade formal.
- Considere CI/CD, cloud, testes e deploy competências reais quando houver evidência de implementação no perfil.
- Se exigir graduação completa OBRIGATÓRIA (não apenas preferencial), marque cv_compatibility_score <= 20.
- Se for vaga PCD exclusiva/afirmativa, marque is_real_job_opportunity=false.
- Se for sênior/lead/gerente/especialista, marque cv_compatibility_score <= 10.
- Não trate o título como prova suficiente: estime também o nível operacional pelo escopo das responsabilidades.
- Separe requisitos obrigatórios de diferenciais e identifique lacunas treináveis.
- Uma vaga Pleno com acompanhamento, correções simples e tarefas de entrada pode ser uma oportunidade de progressão.

Responda APENAS com o JSON abaixo, sem markdown, sem texto extra:
{{"is_real_job_opportunity": bool, "cv_compatibility_score": int, "potential_score": int, "declared_level": "junior|mid|senior|unknown", "operational_level": "junior|junior_to_mid|mid|senior|unknown", "mandatory_requirements": ["string"], "desirable_requirements": ["string"], "hard_barriers": ["string"], "matched_evidence": ["string"], "trainable_gaps": ["string"], "category": "COMPATIVEL|POTENCIALMENTE_COMPATIVEL|DESAFIADORA_VALIDA|INCOMPATIVEL", "reasoning": "string curta", "recommendation": "APPLY_NOW|MAYBE|SKIP"}}"""


def normalize_result(result: Dict[str, Any]) -> Dict[str, Any]:
    """Preenche o contrato novo sem quebrar respostas antigas do provedor."""
    normalized = dict(result or {})
    normalized.setdefault("potential_score", normalized.get("cv_compatibility_score", 0))
    normalized.setdefault("declared_level", "unknown")
    normalized.setdefault("operational_level", normalized.get("declared_level", "unknown"))
    for key in ("mandatory_requirements", "desirable_requirements", "hard_barriers", "matched_evidence", "trainable_gaps"):
        if not isinstance(normalized.get(key), list):
            normalized[key] = []
    score = int(normalized.get("cv_compatibility_score", 0))
    potential = int(normalized.get("potential_score", score))
    if normalized.get("hard_barriers"):
        normalized["category"] = "INCOMPATIVEL"
    else:
        normalized.setdefault(
            "category",
            "COMPATIVEL" if score >= 60 else "POTENCIALMENTE_COMPATIVEL" if potential >= 45 else "DESAFIADORA_VALIDA" if potential >= 35 else "INCOMPATIVEL",
        )
    return normalized


def should_invoke_judge(heuristic_score: int) -> bool:
    return heuristic_score >= _HEURISTIC_FLOOR


def _enforce_pacing():
    """Garante espaçamento de ~4.5s (+ jitter) entre chamadas sucessivas ao LLM (15 RPM)."""
    global _LAST_CALL_TIMESTAMP
    if PACING_SECONDS <= 0:
        _LAST_CALL_TIMESTAMP = time.time()
        return

    now = time.time()
    elapsed = now - _LAST_CALL_TIMESTAMP
    target_interval = PACING_SECONDS + random.uniform(0.0, 0.5)
    if elapsed < target_interval:
        sleep_needed = target_interval - elapsed
        time.sleep(sleep_needed)
    _LAST_CALL_TIMESTAMP = time.time()


def _parse_provider_response(data: Dict[str, Any], provider: str) -> Dict[str, Any]:
    """Extrai e interpreta o JSON dos formatos Gemini e OpenAI-compatível."""
    if provider == "gemini":
        text = data.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "")
    else:
        text = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    clean = text.strip().replace("```json", "").replace("```", "").strip()
    return json.loads(clean)


def _call_provider(provider: str, api_key: str, model: str, prompt: str) -> Optional[Dict[str, Any]]:
    """Executa uma chamada LLM com retry e parsing comum aos providers."""
    if provider in _PROVIDER_UNAVAILABLE and time.time() < _PROVIDER_UNAVAILABLE_UNTIL.get(provider, 0):
        return None
    _PROVIDER_UNAVAILABLE.discard(provider)

    if provider == "gemini":
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        payload = {
            "system_instruction": {"parts": [{"text": "Responda somente com JSON válido."}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json"},
        }
        headers = {"x-goog-api-key": api_key, "Content-Type": "application/json"}
    else:
        url = "https://openrouter.ai/api/v1/chat/completions"
        payload = {
            "model": model,
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": "Responda somente com JSON válido."},
                {"role": "user", "content": prompt},
            ],
        }
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    max_retries = _MAX_RATE_LIMIT_RETRIES
    for attempt in range(max_retries + 1):
        _enforce_pacing()
        try:
            resp = requests.post(url, headers=headers, json=payload,
                                 timeout=_REQUEST_TIMEOUT_SECONDS)
            if resp.status_code == 200:
                return _parse_provider_response(resp.json(), provider)
            if resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After")
                try:
                    wait_sec = float(retry_after) if retry_after else 4.0
                except (ValueError, TypeError):
                    wait_sec = 4.0
                wait_sec = min(max(wait_sec, 0.0), 8.0)
                logger.warning(
                    f"[LLM RateLimit 429] Limite do provider {provider}. "
                    f"Aguardando {wait_sec:.1f}s (tentativa {attempt + 1}/{max_retries + 1})..."
                )
                time.sleep(wait_sec)
                continue
            logger.warning(f"Provider {provider} retornou status HTTP {resp.status_code}: {resp.text[:100]}")
            if resp.status_code in {401, 403, 408, 500, 502, 503, 504}:
                _PROVIDER_UNAVAILABLE.add(provider)
                _PROVIDER_UNAVAILABLE_UNTIL[provider] = time.time() + _PROVIDER_COOLDOWN_SECONDS
            break
        except (requests.RequestException, TimeoutError) as e:
            logger.warning(f"Erro de conexão com provider {provider} (tentativa {attempt + 1}): {e}")
            _PROVIDER_UNAVAILABLE.add(provider)
            _PROVIDER_UNAVAILABLE_UNTIL[provider] = time.time() + _PROVIDER_COOLDOWN_SECONDS
            return None
    return None


def _call_gemini(api_key: str, model: str, prompt: str) -> Optional[Dict[str, Any]]:
    return _call_provider("gemini", api_key, model, prompt)


def _call_openrouter(api_key: str, model: str, prompt: str) -> Optional[Dict[str, Any]]:
    return _call_provider("openrouter", api_key, model, prompt)


def _provider_config():
    provider = os.getenv("LLM_PROVIDER", "gemini").strip().lower()
    if provider not in {"gemini", "openrouter"}:
        logger.warning("LLM_PROVIDER inválido: %s; usando fallback heurístico", provider)
        return None
    key_name = "GEMINI_API_KEY" if provider == "gemini" else "OPENROUTER_API_KEY"
    api_key = os.getenv(key_name)
    if not api_key:
        return None
    default_model = "gemini-2.5-flash-lite" if provider == "gemini" else "google/gemini-2.5-flash-lite"
    model = os.getenv("LLM_MODEL", "").strip() or default_model
    return provider, api_key, model


def _provider_chain():
    """Resolve a cadeia sem quebrar a configuração legada LLM_PROVIDER/LLM_MODEL."""
    primary = os.getenv("LLM_PRIMARY_PROVIDER", os.getenv("LLM_PROVIDER", "gemini")).strip().lower()
    fallback = os.getenv("LLM_FALLBACK_PROVIDER", "openrouter").strip().lower()
    allowed = {"gemini", "openrouter"}
    if primary not in allowed:
        logger.warning("LLM_PRIMARY_PROVIDER inválido: %s", primary)
        primary = ""
    if fallback not in allowed or fallback == primary:
        fallback = ""

    models = {
        "gemini": os.getenv("LLM_PRIMARY_MODEL", os.getenv("LLM_MODEL", "")).strip() or "gemini-3.5-flash-lite",
        "openrouter": os.getenv("LLM_FALLBACK_MODEL", "").strip() or "openrouter/free",
    }
    keys = {"gemini": os.getenv("GEMINI_API_KEY"), "openrouter": os.getenv("OPENROUTER_API_KEY")}
    chain = [(p, keys[p], models[p]) for p in (primary, fallback) if p and keys[p]]
    return chain


def reset_cycle_stats():
    global _CYCLE_CALLS
    _CYCLE_CALLS = 0


def usage_stats():
    return {"calls": _CYCLE_CALLS}


def set_health_observer(observer):
    """Install a callback for sanitized provider outcomes, if durable storage exists."""
    global _HEALTH_OBSERVER
    _HEALTH_OBSERVER = observer


def _record_provider_outcome(provider, model, status):
    if _HEALTH_OBSERVER is None:
        return
    try:
        _HEALTH_OBSERVER(provider, model, status)
    except Exception as exc:
        logger.warning("Não foi possível persistir a saúde do provedor LLM (%s)", type(exc).__name__)


def judge(title: str, company: str, description: str) -> Optional[Dict[str, Any]]:
    """
    Avalia a vaga usando o provider configurado.
    Retorna None em caso de ausência de chaves ou erro, ativando fallback heurístico.
    """
    global _CYCLE_CALLS
    max_calls = max(0, int(os.getenv("LLM_MAX_CALLS_PER_CYCLE", "20")))
    if _CYCLE_CALLS >= max_calls:
        logger.warning("Limite de chamadas LLM por ciclo atingido: %s", max_calls)
        return None
    chain = _provider_chain()
    if not chain:
        return None

    prompt = JUDGE_PROMPT.format(
        profile_json=json.dumps(get_profile(), ensure_ascii=False),
        title=title,
        company=company,
        description=(description or "")[:3000],
    )

    for provider, api_key, model in chain:
        _CYCLE_CALLS += 1
        try:
            caller = _call_gemini if provider == "gemini" else _call_openrouter
            result = caller(api_key, model, prompt)
            if result:
                assert isinstance(result.get("is_real_job_opportunity"), bool)
                assert isinstance(result.get("cv_compatibility_score"), int)
                normalized = normalize_result(result)
                normalized["llm_provider"] = provider
                normalized["llm_model"] = model
                _record_provider_outcome(provider, model, "OK")
                return normalized
        except Exception as e:
            logger.warning("LLM judge falhou no provider %s (%s: %s)", provider, type(e).__name__, e)
        _record_provider_outcome(provider, model, "FAILED")
        logger.warning("Tentando próximo provider LLM após falha em %s", provider)

    return None
