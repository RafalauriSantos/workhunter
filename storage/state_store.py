import json
import os
import time
import uuid
from typing import Dict, Any, List


class StateStore:
    """
    Persistência do estado do monitor (fingerprints vistas, datas e heartbeat).
    """
    def __init__(self, filepath: str = "seen_jobs.json"):
        self.filepath = filepath
        self.state: Dict[str, Any] = self._load()

    def _load(self) -> Dict[str, Any]:
        default_state = {"seen_ids": [], "seen_fingerprints": [], "last_heartbeat": ""}
        if not os.path.exists(self.filepath):
            return default_state

        try:
            with open(self.filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    result = {"seen_ids": data, "seen_fingerprints": [], "last_heartbeat": ""}
                elif isinstance(data, dict):
                    result = {
                        "seen_ids": data.get("seen_ids", []),
                        "seen_fingerprints": data.get("seen_fingerprints", []),
                        "last_heartbeat": data.get("last_heartbeat", ""),
                        "recent_decisions": data.get("recent_decisions", []),
                        "deliveries": data.get("deliveries", {}),
                        "source_health": data.get("source_health", []),
                        "source_cooldowns": data.get("source_cooldowns", {}),
                        "delivery_claims": data.get("delivery_claims", {}),
                        "feedback": data.get("feedback", []),
                        "delivery_audit": data.get("delivery_audit", []),
                        "candidate_rejections": data.get("candidate_rejections", []),
                        "gupy_detail_cache": data.get("gupy_detail_cache", {}),
                    }
                else:
                    return default_state

                result["seen_ids"] = [str(x) for x in result.get("seen_ids", [])]
                result["seen_ids"] = list(dict.fromkeys(result["seen_ids"]))
                return result
        except Exception:
            return default_state

    def _acquire_lock(self):
        lock_path = f"{self.filepath}.lock"
        for _ in range(100):
            try:
                fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                return fd, lock_path
            except FileExistsError:
                time.sleep(0.01)
        raise TimeoutError(f"Não foi possível bloquear o estado: {self.filepath}")

    @staticmethod
    def _release_lock(fd: int, lock_path: str):
        os.close(fd)
        try:
            os.remove(lock_path)
        except FileNotFoundError:
            pass

    def is_seen(self, fingerprint: str, source_id: str = "") -> bool:
        """Verifica se a vaga já foi vista por fingerprint ou por ID específico."""
        if fingerprint in self.state.get("seen_fingerprints", []):
            return True
        if source_id and source_id in self.state.get("seen_ids", []):
            return True
        return False

    def mark_seen(self, fingerprint: str, source_ids: List[str]):
        if "seen_fingerprints" not in self.state:
            self.state["seen_fingerprints"] = []
        if "seen_ids" not in self.state:
            self.state["seen_ids"] = []

        if fingerprint not in self.state["seen_fingerprints"]:
            self.state["seen_fingerprints"].append(fingerprint)

        for sid in source_ids:
            sid_str = str(sid)
            if sid_str not in self.state["seen_ids"]:
                self.state["seen_ids"].append(sid_str)

    def record_delivery(self, fingerprint: str, source_ids: List[str], delivered: bool):
        """Registra o resultado sem perder alertas cuja entrega falhou."""
        import datetime
        deliveries = self.state.setdefault("deliveries", {})
        previous = deliveries.get(fingerprint, {})
        attempts = previous.get("attempts", 0) + 1
        deliveries[fingerprint] = {
            "status": "DELIVERED" if delivered else "DELIVERY_FAILED",
            "attempts": attempts,
            "source_ids": [str(source_id) for source_id in source_ids],
        }
        if not delivered:
            next_retry = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
                minutes=2 ** attempts
            )
            deliveries[fingerprint]["next_retry_at"] = next_retry.isoformat()
        if delivered:
            self.mark_seen(fingerprint, source_ids)

    def begin_delivery_attempt(self, fingerprint: str, source_ids: List[str],
                               channel: str = "telegram", cycle_id: str = "") -> str:
        """Persist an attempt before the external side effect."""
        attempt_id = uuid.uuid4().hex
        audit = self.state.setdefault("delivery_audit", [])
        audit.append({"attempt_id": attempt_id, "cycle_id": cycle_id,
                      "fingerprint": fingerprint, "source_ids": [str(x) for x in source_ids],
                      "channel": channel, "status": "SENDING",
                      "timestamp": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()})
        self.state["delivery_audit"] = audit[-1000:]
        self.save()
        return attempt_id

    def finish_delivery_attempt(self, attempt_id: str, status: str, cycle_id: str = ""):
        audit = self.state.setdefault("delivery_audit", [])
        for entry in reversed(audit):
            if entry.get("attempt_id") == attempt_id:
                entry["status"] = status
                entry["finished_at"] = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()
                break
        self.state["delivery_audit"] = audit[-1000:]
        self.save()

    def delivered_count_for_cycle(self, cycle_id: str) -> int:
        return sum(1 for item in self.state.get("delivery_audit", [])
                   if item.get("cycle_id") == cycle_id and item.get("status") == "DELIVERED")

    def get_delivery(self, fingerprint: str) -> Dict[str, Any]:
        """Retorna o último estado de entrega da vaga, se houver."""
        return self.state.get("deliveries", {}).get(fingerprint, {})

    def claim_delivery(self, fingerprint: str) -> bool:
        """Reivindica uma vaga de forma exclusiva entre processos."""
        fd, lock_path = self._acquire_lock()
        try:
            current = self._load()
            claims = current.setdefault("delivery_claims", {})
            if fingerprint in claims or fingerprint in current.get("seen_fingerprints", []):
                self.state = current
                return False
            claims[fingerprint] = {"claimed_at": time.time()}
            with open(self.filepath, "w", encoding="utf-8") as handle:
                json.dump(current, handle, indent=2, ensure_ascii=False)
            self.state = current
            return True
        finally:
            self._release_lock(fd, lock_path)

    def release_delivery_claim(self, fingerprint: str):
        fd, lock_path = self._acquire_lock()
        try:
            current = self._load()
            if self.state.get("deliveries"):
                current["deliveries"] = self.state["deliveries"]
            if self.state.get("recent_decisions"):
                current["recent_decisions"] = self.state["recent_decisions"]
            current.setdefault("delivery_claims", {}).pop(fingerprint, None)
            with open(self.filepath, "w", encoding="utf-8") as handle:
                json.dump(current, handle, indent=2, ensure_ascii=False)
            self.state = current
        finally:
            self._release_lock(fd, lock_path)

    def record_source_health(self, source: str, status: str, discovered: int, details=None):
        """Guarda uma janela curta de saúde operacional por fonte."""
        import datetime
        history = self.state.setdefault("source_health", [])
        history.append({
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "source": source,
            "status": status,
            "discovered": discovered,
            "details": details or {},
        })
        self.state["source_health"] = history[-200:]

    def get_last_source_poll(self, source: str):
        """Return the last recorded cycle that actually attempted a source."""
        skipped = {'THROTTLED', 'COOLDOWN', 'NOT_CONFIGURED'}
        for item in reversed(self.state.get("source_health", [])):
            if item.get("source") == source and item.get("status") not in skipped:
                return item.get("timestamp")
        return None

    def get_source_cooldown(self, source: str, now=None):
        """Return an active source cooldown, if one exists."""
        import datetime
        now = now or datetime.datetime.now(datetime.timezone.utc)
        cooldown = self.state.get("source_cooldowns", {}).get(source)
        if not cooldown:
            return None
        try:
            until = datetime.datetime.fromisoformat(cooldown["until"])
        except (KeyError, TypeError, ValueError):
            return None
        if until <= now:
            return None
        return cooldown

    def register_source_rate_limit(self, source: str, now=None, base_seconds: int = 1800,
                                   max_seconds: int = 21600):
        """Persist an exponential cooldown after a source rate limit."""
        import datetime
        now = now or datetime.datetime.now(datetime.timezone.utc)
        cooldowns = self.state.setdefault("source_cooldowns", {})
        previous = cooldowns.get(source, {})
        consecutive = int(previous.get("consecutive_failures", 0)) + 1
        seconds = min(max_seconds, base_seconds * (2 ** (consecutive - 1)))
        cooldown = {
            "source": source,
            "consecutive_failures": consecutive,
            "cooldown_seconds": seconds,
            "started_at": now.isoformat(),
            "until": (now + datetime.timedelta(seconds=seconds)).isoformat(),
            "reason": "HTTP_429",
        }
        cooldowns[source] = cooldown
        self.save()
        return cooldown

    def clear_source_cooldown(self, source: str):
        """Clear a source cooldown after a successful recovery."""
        cooldowns = self.state.setdefault("source_cooldowns", {})
        if source in cooldowns:
            cooldowns.pop(source, None)
            self.save()

    def delivery_retry_allowed(
        self, fingerprint: str, max_attempts: int = 3, ignore_backoff: bool = False
    ) -> bool:
        """Decide se uma entrega falha pode ser tentada novamente."""
        import datetime
        delivery = self.get_delivery(fingerprint)
        if not delivery:
            return True
        if delivery.get("status") == "DELIVERED":
            return False
        if delivery.get("attempts", 0) >= max_attempts:
            return False
        if ignore_backoff:
            return True
        next_retry_at = delivery.get("next_retry_at")
        if not next_retry_at:
            return True
        try:
            next_retry = datetime.datetime.fromisoformat(next_retry_at)
            return datetime.datetime.now(datetime.timezone.utc) >= next_retry
        except ValueError:
            return True

    def get_last_heartbeat(self) -> str:
        return self.state.get("last_heartbeat", "")

    def set_last_heartbeat(self, date_str: str):
        self.state["last_heartbeat"] = date_str

    def record_llm_call(self) -> int:
        """
        Registra uma chamada ao LLM no contador diário (RPD tracking).
        NOTA DE ARQUITETURA (Gatilho: ~700 chamadas/dia):
        O Google AI Studio reseta a cota diária à meia-noite do Horário do Pacífico (PT / UTC-8).
        Atualmente a margem é > 99% (< 20 calls/dia).
        TODO: Quando o volume diário se aproximar de 700 calls/dia, migrar para:
              from zoneinfo import ZoneInfo
              today_str = datetime.datetime.now(ZoneInfo("America/Los_Angeles")).date().isoformat()
        """
        import datetime
        today_str = datetime.date.today().isoformat()
        usage = self.state.get("llm_usage", {})
        if usage.get("date") != today_str:
            usage = {"date": today_str, "calls": 0}

        usage["calls"] = usage.get("calls", 0) + 1
        self.state["llm_usage"] = usage
        return usage["calls"]

    def get_llm_usage(self) -> Dict[str, Any]:
        """Retorna o uso diário de chamadas ao LLM."""
        import datetime
        today_str = datetime.date.today().isoformat()
        usage = self.state.get("llm_usage", {})
        if usage.get("date") != today_str:
            return {"date": today_str, "calls": 0}
        return usage

    def record_decision(
        self,
        job_id: str,
        source: str,
        identity_fingerprint: str,
        content_hash: str,
        decision: str,
        decision_reason: str,
        heuristic_score: int = 0,
        llm_score: Any = None,
        final_score: int = 0,
        raw_url: str = "",
        canonical_url: str = "",
        evidence_level: str = "",
        category: str = "",
        potential_score: int = 0,
        operational_seniority: str = "",
        score_breakdown: Dict[str, Any] = None,
        match_reasons: List[str] = None,
        max_history: int = 150,
    ):
        """Registra a trilha de auditoria para responder por que cada vaga foi aceita ou rejeitada (SPEC-008)."""
        import datetime
        entry = {
            "timestamp": datetime.datetime.now().isoformat(),
            "job_id": str(job_id),
            "source": source,
            "raw_url": raw_url,
            "canonical_url": canonical_url,
            "identity_fingerprint": identity_fingerprint,
            "content_hash": content_hash,
            "evidence_level": evidence_level,
            "decision": decision,
            "decision_reason": decision_reason,
            "heuristic_score": heuristic_score,
            "llm_score": llm_score,
            "final_score": final_score,
            "category": category,
            "potential_score": potential_score,
            "operational_seniority": operational_seniority,
            "score_breakdown": dict(score_breakdown or {}),
            "match_reasons": list(match_reasons or []),
        }
        if "recent_decisions" not in self.state:
            self.state["recent_decisions"] = []
        self.state["recent_decisions"].append(entry)
        if len(self.state["recent_decisions"]) > max_history:
            self.state["recent_decisions"] = self.state["recent_decisions"][-max_history:]

    def record_candidate_rejection(self, rejection):
        self.record_candidate_rejections([rejection])

    def record_candidate_rejections(self, rejections):
        if not rejections:
            return
        if "candidate_rejections" not in self.state:
            self.state["candidate_rejections"] = []
        self.state["candidate_rejections"].extend(dict(item) for item in rejections)
        self.state["candidate_rejections"] = self.state["candidate_rejections"][-1000:]

    def record_feedback(self, fingerprint: str, feedback: str, note: str = "", source: str = "human"):
        """Registra o retorno humano usado na recalibracao posterior."""
        import datetime
        allowed = {"applied", "not_applied", "interview", "rejected", "irrelevant", "hired"}
        if feedback not in allowed:
            raise ValueError(f"feedback invalido: {feedback}")
        entries = self.state.setdefault("feedback", [])
        entries.append({
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "fingerprint": fingerprint,
            "feedback": feedback,
            "note": note,
            "source": source,
        })
        self.state["feedback"] = entries[-500:]

    def feedback_summary(self) -> Dict[str, Any]:
        """Resume sinais humanos sem alterar pesos automaticamente."""
        entries = self.state.get("feedback", [])
        counts: Dict[str, int] = {}
        for entry in entries:
            key = entry.get("feedback", "unknown")
            counts[key] = counts.get(key, 0) + 1
        return {"total": len(entries), "by_feedback": counts}

    def prune(self, max_seen_ids: int = 2000, max_fingerprints: int = 2000, max_decisions: int = 100):
        """
        Mantém o tamanho do arquivo de estado sob controle estrito (ADR-001).
        Evita crescimento descontrolado no repositório Git descartando identificadores antigos (FIFO).
        """
        seen_ids = self.state.get("seen_ids", [])
        if len(seen_ids) > max_seen_ids:
            self.state["seen_ids"] = seen_ids[-max_seen_ids:]

        fps = self.state.get("seen_fingerprints", [])
        if len(fps) > max_fingerprints:
            self.state["seen_fingerprints"] = fps[-max_fingerprints:]

        decisions = self.state.get("recent_decisions", [])
        if len(decisions) > max_decisions:
            self.state["recent_decisions"] = decisions[-max_decisions:]

    def save(self):
        self.prune()
        with open(self.filepath, "w", encoding="utf-8") as f:
            json.dump(self.state, f, indent=2, ensure_ascii=False)
