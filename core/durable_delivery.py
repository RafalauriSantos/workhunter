import time

from core.delivery_workflow import finalize_delivery
from core.deduplicator import delivery_fingerprint
from notify.adapters import DeliveryResult, LegacyNotifierAdapter


def deliver(store, job, source_ids, telegram, email, adapters=None):
    fingerprint = delivery_fingerprint(job)
    store.enqueue(job)
    with store.connect() as db:
        changed = db.execute("UPDATE outbox SET status='SENDING',attempts=attempts+1 "
                             "WHERE fingerprint=? AND status IN ('PENDING','FAILED') "
                             'AND attempts < 3 AND retry_at <= ?', (fingerprint, time.time())).rowcount
    if not changed:
        return False
    sent = False
    outcome = 'FAILED'
    adapters = adapters or [
        LegacyNotifierAdapter(telegram, 'telegram'),
        LegacyNotifierAdapter(email, 'email'),
    ]
    for adapter in adapters:
        if getattr(adapter, 'configured', True) is False:
            continue
        channel = adapter.channel
        # Persist uncertainty BEFORE the external side effect.
        attempt_id = store.begin_delivery_attempt(
            fingerprint, source_ids, channel, store.state.get("active_cycle_id", "")
        )
        with store.connect() as db:
            attempt = db.execute('INSERT INTO channel_attempts(fingerprint,channel,status,created) '
                                 "VALUES (?,?,'UNKNOWN',datetime('now'))", (fingerprint, channel)).lastrowid
        try:
            result = adapter.send_job_alert(job)
            sent = result.status == 'DELIVERED'
            outcome = result.status
        except Exception:
            outcome = 'UNKNOWN'
        with store.connect() as db:
            db.execute('UPDATE channel_attempts SET status=? WHERE id=?', (outcome, attempt))
        store.finish_delivery_attempt(attempt_id, outcome)
        if sent or outcome == 'UNKNOWN':
            break
    with store.connect() as db:
        db.execute('UPDATE outbox SET status=?,retry_at=? WHERE fingerprint=?',
                   (outcome, time.time() + 300, fingerprint))
    if outcome != 'UNKNOWN':
        finalize_delivery(store, job, source_ids, sent, job.match_score,
                          job.match_reasons[0] if job.match_reasons else 'Approved')
    return sent
