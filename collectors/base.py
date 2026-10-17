from abc import ABC, abstractmethod
from typing import List
from datetime import datetime, timezone
import hashlib
import json
from models.job import Job
from dataclasses import dataclass, field


@dataclass
class CollectionResult:
    jobs: List[Job]
    status: str
    issues: list = field(default_factory=list)


def collect_result(collector):
    collector.collection_issues = []
    try:
        jobs = collector.collect()
    except Exception as exc:
        return CollectionResult([], 'FAILED', [type(exc).__name__])
    issues = list(collector.collection_issues)
    for query in getattr(collector, 'query_stats', []):
        status = query.get('status', 'UNKNOWN')
        if status not in ('OK', 'EMPTY', 'SUCCESS'):
            issues.append(status)
    return CollectionResult(jobs, ('PARTIAL' if jobs else 'FAILED') if issues else 'OK', issues)


class BaseCollector(ABC):
    def __init__(self):
        self.collection_attempts = []
        self.rejected_candidates = []

    def record_candidate_rejection(self, source, source_job_id, title, stage, reason,
                                   *, query=None, url="", published_at=""):
        if not hasattr(self, 'rejected_candidates'):
            self.rejected_candidates = []
        self.rejected_candidates.append({
            'source': str(source or ''),
            'source_job_id': str(source_job_id or ''),
            'title': str(title or ''),
            'stage': str(stage or ''),
            'reason': str(reason or ''),
            'query': query,
            'url': str(url or ''),
            'published_at': str(published_at or ''),
            'rejected_at': datetime.now(timezone.utc).isoformat(),
        })

    def record_attempt(self, source, operation, started, *, query=None, page=None,
                       cursor=None, http_status=None, result_count=0, native_ids=None,
                       error_type=None, timed_out=False, retry_count=0, reason=None):
        self.collection_attempts.append({
            'cycle_id': getattr(self, 'cycle_id', ''), 'source': source, 'operation': operation,
            'query': query, 'query_hash': hashlib.sha256(json.dumps(query, sort_keys=True, default=str).encode()).hexdigest() if query is not None else None,
            'page': page, 'cursor': cursor, 'started': started,
            'finished': datetime.now(timezone.utc).isoformat(), 'http_status': http_status,
            'result_count': result_count, 'native_ids': native_ids or [], 'error_type': error_type,
            'timed_out': timed_out, 'retry_count': retry_count, 'reason': reason,
        })
        item = self.collection_attempts[-1]
        item['duration_ms'] = max(0, int((datetime.fromisoformat(item['finished']) - datetime.fromisoformat(started)).total_seconds() * 1000))

    def report_issue(self, issue):
        if not hasattr(self, 'collection_issues'):
            self.collection_issues = []
        self.collection_issues.append(issue)

    """
    Interface abstrata para qualquer coletor de vagas (Gupy, LinkedIn, RSS, etc).
    Garante que a descoberta seja independente da classificação e envio.
    """
    @abstractmethod
    def collect(self) -> List[Job]:
        """Executa a coleta e retorna uma lista de instâncias de Job padronizadas."""
        pass
