"""Read-only operational health checks for WorkHunter."""
import shutil
import sqlite3
import time
from datetime import datetime, timezone


def health_snapshot(store, data_dir, cycle_max_age_seconds=5400, now=None):
    from core.llm_health import format_health
    now = now or datetime.now(timezone.utc)
    disk = shutil.disk_usage(data_dir)
    disk_free_percent = round((disk.free / disk.total) * 100, 1) if disk.total else 0.0
    result = {
        'status': 'HEALTHY',
        'disk_free_percent': disk_free_percent,
        'database': 'OK',
        'last_cycle_at': None,
        'last_cycle_status': None,
        'cycle_age_seconds': None,
        'pending_deliveries': 0,
        'uncertain_attempts': 0,
        'llm': format_health(store, now),
        'issues': [],
    }
    try:
        with store.connect() as db:
            db.execute('PRAGMA integrity_check').fetchone()
            cycle = db.execute('SELECT started,finished,status FROM cycles ORDER BY id DESC LIMIT 1').fetchone()
            result['pending_deliveries'] = db.execute("SELECT COUNT(*) FROM outbox WHERE status != 'DELIVERED'").fetchone()[0]
            result['uncertain_attempts'] = db.execute("SELECT COUNT(*) FROM channel_attempts WHERE status='UNKNOWN'").fetchone()[0]
        if cycle:
            result['last_cycle_at'] = cycle[1] or cycle[0]
            result['last_cycle_status'] = cycle[2]
            cycle_time = datetime.fromisoformat(result['last_cycle_at'])
            result['cycle_age_seconds'] = max(0, int((now - cycle_time).total_seconds()))
    except (sqlite3.DatabaseError, OSError) as exc:
        result['database'] = 'ERROR'
        result['issues'].append('database_unavailable')
    if disk_free_percent < 10:
        result['issues'].append('disk_low')
    if result['cycle_age_seconds'] is not None and result['cycle_age_seconds'] > cycle_max_age_seconds:
        result['issues'].append('cycle_stale')
    if result['pending_deliveries'] > 0:
        result['issues'].append('pending_deliveries')
    if result['uncertain_attempts'] > 0:
        result['issues'].append('uncertain_delivery')
    if result['database'] != 'OK' or disk_free_percent < 5:
        result['status'] = 'OFFLINE'
    elif result['issues']:
        result['status'] = 'DEGRADED'
    return result
