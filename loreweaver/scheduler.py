"""Serialized-drip scheduler.

Runs the pipeline in 'next_chapter' mode on a cadence so the series keeps
publishing autonomously after the demo. Uses APScheduler if available; otherwise
prints the cron line you can drop into a system crontab.
"""
from __future__ import annotations

from . import settings
from .graph import run_pipeline


def run_once(series_id: str) -> None:
    run_pipeline(series_id, mode="next_chapter")


def register_daily(series_id: str) -> None:
    cron = settings.SCHEDULE_CRON
    try:
        from apscheduler.schedulers.blocking import BlockingScheduler
        from apscheduler.triggers.cron import CronTrigger

        sched = BlockingScheduler()
        sched.add_job(run_once, CronTrigger.from_crontab(cron), args=[series_id],
                      id=f"loreweaver-{series_id}", replace_existing=True)
        print(f"Scheduled daily drip for '{series_id}' ({cron}). Ctrl-C to stop.")
        sched.start()
    except ImportError:
        print("APScheduler not installed. Add this to your crontab to drip daily:\n")
        m, h, *_ = cron.split()
        print(f"  {m} {h} * * *  cd {settings.ROOT} && "
              f"python -m loreweaver.main next --series {series_id}\n")
