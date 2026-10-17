def persist_rejections(collectors, store, cycle_id):
    pending = []
    for collector in collectors:
        for rejection in getattr(collector, 'rejected_candidates', []):
            payload = dict(rejection)
            payload['cycle_id'] = cycle_id
            pending.append(payload)
    if not pending:
        return 0
    batch_writer = getattr(store, 'record_candidate_rejections', None)
    if callable(batch_writer):
        batch_writer(pending)
    else:
        for rejection in pending:
            store.record_candidate_rejection(rejection)
    return len(pending)
