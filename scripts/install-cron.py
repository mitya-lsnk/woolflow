#!/opt/hermes/.venv/bin/python
"""Install cron/jobs.yaml into $HERMES_HOME/cron/jobs.json on every boot.

Hermes keeps scheduled jobs in cron/jobs.json under HERMES_HOME. On Free that
directory is wiped on every spin-down, so a job created from the chat lasts
until the next sleep and no longer. This re-creates them from the file baked
into the image.

It does NOT write jobs.json directly. The stored record carries a parsed
schedule, next_run_at, claim fences and monitor state — hand-rolling that would
break the first time upstream touched the schema. Instead it calls Hermes' own
``cron.jobs.create_job()`` with the declarative fields from the YAML, so the
record is always built by the code that owns it.

Delivery: jobs use deliver="telegram", which resolves to the Telegram home
channel that boot-config.py seeds from WOOLFLOW_TELEGRAM_CHAT_ID. Without that
chat id a job still runs, and its output is only saved locally.

Schedules follow HERMES_TIMEZONE, not the server clock — Render runs UTC.

    WOOLFLOW_CRON=0   skip entirely

Never fails the boot: a broken spec logs and exits 0, because a gateway with no
scheduled jobs is still a working gateway.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

SPEC = Path("/opt/woolflow/cron-jobs.yaml")


def warn(msg: str) -> None:
    print(f"[woolflow] {msg}", file=sys.stderr)


def main() -> int:
    if os.environ.get("WOOLFLOW_CRON", "1").strip().lower() in {"0", "false", "no"}:
        print("[woolflow] WOOLFLOW_CRON is off; not installing scheduled jobs")
        return 0
    if not SPEC.exists():
        return 0

    import yaml

    try:
        spec = yaml.safe_load(SPEC.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        warn(f"cannot read {SPEC} ({exc}); no jobs installed")
        return 0

    wanted = spec.get("jobs")
    if not isinstance(wanted, list) or not wanted:
        return 0

    # /opt/hermes is on the venv's path via the editable install; add it anyway
    # so this keeps working if that ever stops being true.
    sys.path.insert(0, "/opt/hermes")
    try:
        from cron.jobs import create_job, list_jobs
    except Exception as exc:  # noqa: BLE001 - any import failure is non-fatal
        warn(f"cannot import Hermes' cron module ({exc}); no jobs installed")
        return 0

    # Idempotent by name. On Free this always finds an empty store, but on a
    # mounted disk it keeps a boot from stacking duplicates every restart.
    try:
        existing = {str(j.get("name", "")).strip() for j in (list_jobs() or [])}
    except Exception as exc:  # noqa: BLE001
        warn(f"cannot read existing jobs ({exc}); assuming none")
        existing = set()

    created, skipped = [], []
    for entry in wanted:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name", "")).strip()
        prompt = entry.get("prompt")
        schedule = entry.get("schedule")
        if not (name and prompt and schedule):
            warn(f"job {name or '<unnamed>'!r} needs name, prompt and schedule; skipped")
            continue
        if name in existing:
            skipped.append(name)
            continue
        try:
            create_job(
                prompt=str(prompt).strip(),
                schedule=str(schedule).strip(),
                name=name,
                deliver=str(entry.get("deliver", "telegram")).strip(),
                repeat=entry.get("repeat"),
            )
            created.append(name)
        except Exception as exc:  # noqa: BLE001 - one bad job must not stop the rest
            warn(f"job {name!r} not created ({exc})")

    if created:
        print(f"[woolflow] cron jobs installed: {', '.join(created)}")
    if skipped:
        print(f"[woolflow] cron jobs already present: {', '.join(skipped)}")
    if not created and not skipped:
        print("[woolflow] no cron jobs installed")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - boot must survive anything here
        warn(f"unexpected failure installing cron jobs ({exc})")
        raise SystemExit(0)
