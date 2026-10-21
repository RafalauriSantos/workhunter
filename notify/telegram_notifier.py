import os
import requests
from models.job import Job
from notify.job_card import build_card

WORKPLACE_EMOJIS = {
    "remote": "Remoto 🌐",
    "hybrid": "Híbrido 🏢/🏠",
    "on-site": "Presencial 🏢",
    "unknown": "A confirmar 📍",
}


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str, http_session: requests.Session):
        self.token = bot_token
        self.chat_id = chat_id
        self.http = http_session

    def send_job_alert(self, job: Job) -> bool:
        self.last_status = 'FAILED'
        if not self.token or not self.chat_id:
            return False

        msg, url = build_card(job)
        buttons = [[{"text": "Ver vaga", "url": url}]] if url else []
        reply_markup = {"inline_keyboard": buttons}
        api_url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": msg,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "reply_markup": reply_markup,
        }

        try:
            r = self.http.post(api_url, json=payload, timeout=10)
            self.last_status = 'DELIVERED' if r.status_code == 200 else ('UNKNOWN' if r.status_code >= 500 else 'FAILED')
            return r.status_code == 200
        except Exception as e:
            self.last_status = 'UNKNOWN'
            print(f"[ERRO TelegramNotifier] {type(e).__name__}")
            return False

    def send_heartbeat(self, monitors_count: int, jobs_tracked: int, llm_health: str = '') -> bool:
        if not self.token or not self.chat_id:
            return False

        msg = (
            f"🩺 <b>WorkHunter · resumo semanal</b>\n\n"
            f"• Operação: ativa\n"
            f"• Fontes monitoradas: {monitors_count} · vagas acompanhadas: {jobs_tracked}\n"
            + (f"• {llm_health}\n" if llm_health else "")
            + "Este resumo confirma a execução do serviço; consulte /status para a saúde das fontes."
        )

        api_url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": msg,
            "parse_mode": "HTML",
            "disable_notification": True,
        }

        try:
            r = self.http.post(api_url, json=payload, timeout=10)
            return r.status_code == 200
        except Exception as e:
            print(f"[ERRO TelegramNotifier Heartbeat] {type(e).__name__}")
            return False
