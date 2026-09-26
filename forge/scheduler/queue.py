import queue
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from forge.core.errors import ConcurrencyError, EntityNotFoundError
from forge.core.models import Deployment, DeploymentStatus, EventKind, utc_now
from forge.deployments.service import DeploymentService
from forge.storage.db import Database
from forge.storage.repository import (
    ApplicationRepository,
    DeploymentRepository,
    EventRepository,
)


@dataclass
class DeploymentJob:
    deployment_id: str
    app_id: str
    context_path: Path
    health_check_path: str = "/health"
    health_check_timeout: float = 15.0
    health_check_interval: float = 0.5
    job_id: str = field(default_factory=lambda: f"job-{uuid.uuid4().hex[:8]}")
    created_at: str = field(default_factory=utc_now)


class DeploymentQueue:
    """In-process background job queue for async deployments.
    Provides concurrency protection per application and asynchronous worker execution.
    """

    def __init__(
        self,
        db: Database,
        deployment_service: DeploymentService,
        max_workers: int = 1,
    ) -> None:
        self.db = db
        self.deployment_service = deployment_service
        self.max_workers = max_workers
        self._queue: queue.Queue[DeploymentJob | None] = queue.Queue()
        self._active_jobs: dict[str, DeploymentJob] = {}  # app_id -> job
        self._active_mutex = threading.Lock()
        self._stop_event = threading.Event()
        self._workers: list[threading.Thread] = []

        self.dep_repo = DeploymentRepository(db)
        self.event_repo = EventRepository(db)
        self.app_repo = ApplicationRepository(db)

    def enqueue(self, job: DeploymentJob) -> str:
        """Enqueue an already-created deployment job for async execution."""
        with self._active_mutex:
            if job.app_id in self._active_jobs:
                raise ConcurrencyError(
                    f"Deployment already queued or active for application '{job.app_id}'"
                )

            in_progress = self.dep_repo.get_in_progress_deployment(job.app_id)
            if in_progress is not None and in_progress.id != job.deployment_id:
                raise ConcurrencyError(
                    f"Deployment already in progress for application '{job.app_id}'"
                )

            self._active_jobs[job.app_id] = job
            self._queue.put(job)
            return job.job_id

    def submit_deployment(
        self,
        app_id: str,
        context_path: Path,
        health_check_path: str = "/health",
        health_check_timeout: float = 15.0,
        health_check_interval: float = 0.5,
    ) -> tuple[Deployment, str]:
        """Atomically create a PENDING deployment and enqueue it for background execution."""
        with self._active_mutex:
            if app_id in self._active_jobs:
                raise ConcurrencyError(
                    f"Deployment already queued or active for application '{app_id}'"
                )

            dep = self.deployment_service.create_deployment(app_id)
            job = DeploymentJob(
                deployment_id=dep.id,
                app_id=app_id,
                context_path=context_path,
                health_check_path=health_check_path,
                health_check_timeout=health_check_timeout,
                health_check_interval=health_check_interval,
            )
            self._active_jobs[app_id] = job
            self._queue.put(job)
            return dep, job.job_id

    def is_app_busy(self, app_id: str) -> bool:
        with self._active_mutex:
            return app_id in self._active_jobs

    def get_active_job(self, app_id: str) -> DeploymentJob | None:
        with self._active_mutex:
            return self._active_jobs.get(app_id)

    def get_in_flight_deployment_ids(self) -> set[str]:
        with self._active_mutex:
            return {job.deployment_id for job in self._active_jobs.values()}

    def get_in_flight_app_ids(self) -> set[str]:
        with self._active_mutex:
            return set(self._active_jobs.keys())

    def start(self) -> None:
        if self._workers:
            return
        self._stop_event.clear()
        for i in range(self.max_workers):
            t = threading.Thread(
                target=self._worker_loop,
                daemon=True,
                name=f"ForgeQueueWorker-{i+1}",
            )
            t.start()
            self._workers.append(t)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        for _ in self._workers:
            self._queue.put(None)
        for t in self._workers:
            if t.is_alive():
                t.join(timeout=timeout)
        self._workers.clear()

    @property
    def is_running(self) -> bool:
        return any(t.is_alive() for t in self._workers)

    def wait_empty(self, timeout: float = 10.0) -> bool:
        """Wait until all queued jobs have finished processing."""
        return self._queue.all_tasks_done.wait(timeout=timeout) if hasattr(self._queue, "all_tasks_done") else True

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                job = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue

            if job is None:
                self._queue.task_done()
                break

            try:
                self.deployment_service.execute_deployment(
                    deployment_id=job.deployment_id,
                    context_path=job.context_path,
                    health_check_path=job.health_check_path,
                    health_check_timeout=job.health_check_timeout,
                    health_check_interval=job.health_check_interval,
                )
            except Exception as exc:
                try:
                    dep = self.dep_repo.get_by_id(job.deployment_id)
                    if dep and dep.status not in (
                        DeploymentStatus.ACTIVE,
                        DeploymentStatus.FAILED,
                        DeploymentStatus.STOPPED,
                    ):
                        self.dep_repo.update_status(
                            job.deployment_id,
                            DeploymentStatus.FAILED,
                            error_message=f"Queue execution failed: {exc}",
                        )
                        self.event_repo.record(
                            app_id=job.app_id,
                            event_kind=EventKind.DEPLOYMENT_FAILED,
                            payload={"error": str(exc)},
                            deployment_id=job.deployment_id,
                        )
                except Exception:
                    pass
            finally:
                with self._active_mutex:
                    self._active_jobs.pop(job.app_id, None)
                self._queue.task_done()
