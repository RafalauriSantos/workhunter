"""Windowless Task Scheduler entry point; logs stay outside the checkout."""
import logging
from logging.handlers import RotatingFileHandler
import json
import os
from pathlib import Path
import re
import sqlite3
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
NETWORK_PROBE_HOSTS = ('candidates.mcp.api.gupy.io', 'api.telegram.org')


def network_is_ready():
    """Check DNS and outbound HTTPS reachability without making an API call."""
    for host in NETWORK_PROBE_HOSTS:
        try:
            with socket.create_connection((host, 443), timeout=3):
                return True
        except OSError:
            continue
    return False


def mark_watchdog_timeout(database, timeout_seconds):
    """Close the child-owned cycle immediately when the watchdog kills it.

    Without this, the cycle remains RUNNING until the next five-minute check
    notices it as stale. That creates a misleading gap in reports and delays
    recovery by another scheduler tick.
    """
    try:
        db = sqlite3.connect(database, timeout=5)
        try:
            row = db.execute(
                "SELECT id, started FROM cycles WHERE status='RUNNING' ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if not row:
                return False
            progress = {}
            progress_file = Path(database).parent / 'cycle-progress' / f'{row[0]}.json'
            # The progress filename uses the application cycle id, while the
            # database row has the numeric id. Find the newest checkpoint as a
            # best-effort recovery artifact for this still-running cycle.
            candidates = sorted(
                (Path(database).parent / 'cycle-progress').glob('*.json'),
                key=lambda item: item.stat().st_mtime,
                reverse=True,
            ) if (Path(database).parent / 'cycle-progress').exists() else []
            if candidates:
                try:
                    progress = json.loads(candidates[0].read_text(encoding='utf-8'))
                except (OSError, ValueError):
                    progress = {}
            report = {
                "status": "TIMEOUT",
                "duration_seconds": timeout_seconds,
                "failure": {"kind": "watchdog_timeout", "timeout_seconds": timeout_seconds},
                "sources": progress.get('sources', {}),
                "funnel": progress.get('funnel', {
                    "raw": 0, "unique": 0, "seen": 0, "discarded": {}, "notified": 0
                }),
                "progress": progress,
            }
            db.execute(
                "UPDATE cycles SET status='INTERRUPTED', finished=?, report=? WHERE id=?",
                (datetime.now(timezone.utc).isoformat(), json.dumps(report), row[0]),
            )
            db.commit()
            return True
        finally:
            db.close()
    except (OSError, sqlite3.Error) as exc:
        logging.getLogger('local-runner').error(
            'Could not persist watchdog timeout: %s', type(exc).__name__
        )
        return False


def main():
    if '--dry-run' in sys.argv:
        os.environ['JOB_FINDER_SIMULATION'] = '1'
    from core.local_runtime import data_directory
    directory = data_directory()
    directory.mkdir(parents=True, exist_ok=True)
    log_path = Path(os.environ.get('JOB_FINDER_RUNTIME_LOG', str(directory / 'runtime.log')))
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(log_path, maxBytes=2_000_000,
                                  backupCount=7, encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
    logger = logging.getLogger('local-runner')
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    logger.propagate = False
    from dotenv import load_dotenv
    load_dotenv(directory / 'secrets.env', override=True)
    secrets = [value for key, value in os.environ.items()
               if any(part in key for part in ('TOKEN', 'KEY', 'PASSWORD')) and len(value) > 6]

    class LogStream:
        def write(self, text):
            for secret in secrets:
                text = text.replace(secret, '[REDACTED]')
            text = re.sub(r'bot\d+:[\w-]+', 'bot[REDACTED]', text)
            for line in text.splitlines():
                if line.strip():
                    logger.info(line)
        def flush(self):
            handler.flush()

    # Keep diagnostics visible to the caller. The windowless runner redirects
    # cycle output to the private log, but swallowing --diagnose output makes
    # recovery impossible from an interactive terminal.
    if '--diagnose' not in sys.argv:
        sys.stdout = sys.stderr = LogStream()
    os.chdir(ROOT)
    os.environ['JOB_FINDER_LOCAL_RUNTIME'] = '1'
    try:
        import monitor
        from core.local_runtime import main as run
        arguments = sys.argv[1:] or ['--due']
        if '--loop' in arguments:
            arguments = [arg for arg in arguments if arg != '--loop'] or ['--due']
            timeout_seconds = int(os.environ.get('JOB_FINDER_CYCLE_TIMEOUT_SECONDS', '240'))
            logger.info('Local runner started; checking every 5 minutes; cycle timeout=%ss', timeout_seconds)
            while True:
                started = time.monotonic()
                try:
                    if not network_is_ready():
                        logger.warning('Network/DNS is not ready; delaying collection cycle')
                        time.sleep(30)
                        continue
                    # Isolate each collection so a hung network call cannot freeze
                    # the watchdog permanently. The child owns the cycle lock.
                    command = [sys.executable, str(Path(__file__).resolve()), *arguments]
                    creationflags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
                    completed = subprocess.run(
                        command,
                        cwd=ROOT,
                        env=os.environ.copy(),
                        timeout=timeout_seconds,
                        creationflags=creationflags,
                    )
                    logger.info('Cycle process finished: exit=%s duration=%.1fs',
                                completed.returncode, time.monotonic() - started)
                    if completed.returncode:
                        logger.error('Cycle process failed: exit=%s', completed.returncode)
                except subprocess.TimeoutExpired:
                    mark_watchdog_timeout(directory / 'state.db', timeout_seconds)
                    logger.error('Cycle process timed out after %ss; next check will recover', timeout_seconds)
                except Exception as exc:
                    # The watchdog must stay alive after one bad cycle; the next
                    # five-minute check can recover transient source/runtime errors.
                    logger.error('Cycle failed: %s; retrying on next check', type(exc).__name__)
                time.sleep(300)
        else:
            run(monitor, arguments)
    except Exception as exc:
        logger.exception('Cycle failed: %s (use local diagnostic; secrets omitted)', type(exc).__name__)
        import sqlite3
        if isinstance(exc, (sqlite3.DatabaseError, OSError)):
            import requests
            from core.operational_alerts import send_operational
            with requests.Session() as http:
                send_operational(http, 'WorkHunter: critical local storage/runtime failure. Collection stopped; check local diagnostics.')
        return 1
    finally:
        handler.flush()
        logger.removeHandler(handler)
        handler.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
