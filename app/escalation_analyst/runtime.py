"""Module-owned services and workers; no dependency on existing report business logic."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import logging
import threading
from .repositories.database import Database
from .repositories.groups import Groups
from .repositories.clusters import Clusters
from .repositories.reports import Reports
from .repositories.jobs import Jobs
from .repositories.settings import Preferences
from .analysis.provider import Provider
from .analysis.reports import Analysis
from .whatsapp.client import Sidecar
from .errors import EscalationError

logger = logging.getLogger("escalation_analyst")


class Runtime:
    def __init__(self, settings):
        self.settings = settings
        self.db = Database(settings)
        self.groups = Groups(self.db)
        self.clusters = Clusters(self.db)
        self.reports = Reports(self.db)
        self.jobs = Jobs(self.db)
        self.preferences = Preferences(self.db)
        self.sidecar = Sidecar(settings)
        self.provider = Provider(settings, self.reports)
        self.analysis = Analysis(self)
        self.executor = ThreadPoolExecutor(
            max_workers=max(2, settings.concurrency), thread_name_prefix="escalation"
        )
        self.stop = threading.Event()
        self.scheduler = None
        self._heartbeats = []

    def scope_groups(self, scope: str = "all", scope_id: str = "") -> list[dict]:
        groups = self.groups.list()
        if scope == "group":
            return [self.groups.get(scope_id)]
        if scope == "cluster":
            self.clusters.get(scope_id, active=True)
            mapping = self.clusters.map()
            return [g for g in groups if scope_id == "all" or mapping.get(g["group_id"]) == scope_id]
        return groups

    def start(self, kind: str, lock_key: str, options: dict, work) -> tuple[dict, bool]:
        self.jobs.recover_expired()
        job, created = self.jobs.claim(kind, lock_key, options)
        if created:
            heartbeat_stop = threading.Event()
            self._heartbeats.append(heartbeat_stop)

            def heartbeat():
                while not heartbeat_stop.wait(10):
                    try:
                        self.jobs.heartbeat(job["id"])
                    except Exception:
                        logger.exception("Escalation heartbeat failed")

            threading.Thread(target=heartbeat, daemon=True, name="escalation-heartbeat").start()

            def execute():
                try:
                    result = work(job["id"])
                    self.jobs.update(job["id"], result or {}, "complete" if result is not None else "empty")
                except Exception as exc:
                    logger.exception("Escalation job %s failed", job["id"])
                    message = (
                        exc.message
                        if isinstance(exc, EscalationError)
                        else "Report generation failed; see server logs."
                    )
                    self.jobs.update(job["id"], {}, "error", message)
                finally:
                    heartbeat_stop.set()
                    self._heartbeats.remove(heartbeat_stop)

            try:
                self.executor.submit(execute)
            except RuntimeError:
                heartbeat_stop.set()
                self.jobs.update(job["id"], {}, "error", "Worker unavailable")
                raise EscalationError(503, "Report worker is unavailable")
        return job, created

    def close(self) -> None:
        self.stop.set()
        self.executor.shutdown(wait=True, cancel_futures=True)
        for event in self._heartbeats:
            event.set()

    def start_background(self) -> None:
        if self.settings.scheduler_enabled and self.scheduler is None:
            from .scheduler import run

            self.scheduler = threading.Thread(
                target=run, args=(self,), name="escalation-scheduler", daemon=True
            )
            self.scheduler.start()
