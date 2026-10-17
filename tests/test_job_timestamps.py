from datetime import datetime

from models.job import Job


def test_job_and_source_first_seen_timestamps_are_timezone_aware_utc():
    job = Job(title='Developer', company='Acme', workplace_type='remote')
    job.add_source('linkedin', '123', 'https://example.test/jobs/123')

    job_seen = datetime.fromisoformat(job.first_seen)
    source_seen = datetime.fromisoformat(job.sources['linkedin'].first_seen)
    assert job_seen.utcoffset().total_seconds() == 0
    assert source_seen.utcoffset().total_seconds() == 0
