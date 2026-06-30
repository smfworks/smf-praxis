"""Cron scheduling — schedule parsing, store lifecycle, and daemon tick firing."""
from datetime import datetime

from hybridagent import config as cfg
from hybridagent.cron import compute_next_run, normalize_mode


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))


# --------------------------------------------------------------- schedule parser
def test_interval_schedules():
    base = datetime(2026, 7, 1, 8, 30, 0).timestamp()
    assert compute_next_run("30m", after=base).ts == base + 1800
    assert compute_next_run("2h", after=base).ts == base + 7200
    assert compute_next_run("90s", after=base).ts == base + 90
    assert compute_next_run("every 15m", after=base).ts == base + 900
    assert compute_next_run("1d", after=base).ts == base + 86400


def test_keyword_schedules():
    base = datetime(2026, 7, 1, 8, 30, 0).timestamp()
    assert compute_next_run("hourly", after=base).ts == base + 3600
    assert compute_next_run("daily", after=base).ts == base + 86400


def test_daily_at_time():
    base = datetime(2026, 7, 1, 8, 30, 0).timestamp()
    nr = compute_next_run("daily@09:00", after=base)
    assert datetime.fromtimestamp(nr.ts).strftime("%H:%M") == "09:00"
    # A time already past today rolls to tomorrow.
    nr2 = compute_next_run("@08:00", after=base)
    assert datetime.fromtimestamp(nr2.ts).day == 2


def test_cron5_day_of_week():
    base = datetime(2026, 7, 1, 8, 30, 0).timestamp()  # Wed
    nr = compute_next_run("0 9 * * 1", after=base)  # next Monday 09:00
    dt = datetime.fromtimestamp(nr.ts)
    assert dt.weekday() == 0 and dt.hour == 9 and dt.minute == 0


def test_invalid_schedule_returns_none():
    assert compute_next_run("garbage").ts is None
    assert compute_next_run("").ts is None


def test_normalize_mode():
    assert normalize_mode("research") == "research"
    assert normalize_mode("nonsense") == "do"
    assert normalize_mode("") == "do"


# ------------------------------------------------------------- store lifecycle
def test_cron_store_lifecycle(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from hybridagent.cron import CronScheduler
    from hybridagent.persistence import Store
    sched = CronScheduler(Store.open())
    job = sched.create("do the thing", "30m", name="t", mode="do")
    assert "error" not in job
    jid = job["job_id"]
    assert len(sched.list()) == 1
    # pause clears scheduling, resume re-arms it
    assert sched.set_enabled(jid, False)
    assert sched.set_enabled(jid, True)
    assert sched.store.get_cron_job(jid)["next_run_ts"] is not None
    assert sched.delete(jid)
    assert sched.list() == []


def test_cron_create_rejects_bad_schedule(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from hybridagent.cron import CronScheduler
    from hybridagent.persistence import Store
    res = CronScheduler(Store.open()).create("goal", "not-a-schedule")
    assert "error" in res


# --------------------------------------------------------------- daemon firing
def test_daemon_cron_tick_fires_due_job(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    import time

    from hybridagent.daemon import Daemon
    d = Daemon.from_env()
    d._ensure_agent()
    res = d.cron_create("summarize status", "1s", mode="do")
    jid = res["job_id"]
    time.sleep(1.1)
    assert len(d.store.due_cron_jobs()) == 1
    d._cron_tick()
    job = d.store.get_cron_job(jid)
    assert job["runs"] == 1
    assert job["last_status"] == "ok"
    assert job["next_run_ts"] > time.time()   # rescheduled forward
    assert len(d.list_tasks()) >= 1            # 'do' mode queued a task


def test_cron_create_via_daemon_validates(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from hybridagent.daemon import Daemon
    d = Daemon.from_env()
    assert "error" in d.cron_create("", "30m")
    assert "error" in d.cron_create("goal", "")
