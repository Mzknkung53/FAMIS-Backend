from datetime import datetime, timezone


job_store: dict[str, dict] = {}
completed_unconfirmed_tasks: dict[str, dict] = {}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


