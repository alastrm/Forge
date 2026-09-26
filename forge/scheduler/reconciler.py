import logging
import threading
from dataclasses import dataclass, field

from forge.core.models import DeploymentStatus, EventKind, utc_now
from forge.deployments.service import DeploymentService
from forge.runtime.base import Runtime
from forge.scheduler.queue import DeploymentQueue
from forge.storage.db import Database
from forge.storage.repository import (
    ApplicationRepository,
    DeploymentRepository,
    EventRepository,
)

logger = logging.getLogger("forge.reconciler")


@dataclass
class ReconciliationReport:
    interrupted_deployments_fixed: list[str] = field(default_factory=list)
    dead_containers_detected: list[str] = field(default_factory=list)
    orphans_removed: list[str] = field(default_factory=list)
    timestamp: str = field(default_factory=utc_now)

    @property
    def has_changes(self) -> bool:
        return bool(
            self.interrupted_deployments_fixed
            or self.dead_containers_detected
            or self.orphans_removed
        )


class Reconciler:
    """Reconciliation loop for Forge Control Plane.
    Continuously compares desired state (stored in SQLite) against actual state (in Runtime/Docker),
    correcting drift, detecting crashed containers, cleaning up orphaned candidates,
    and recovering after Forge process restarts.
    """

    def __init__(
        self,
        db: Database,
        runtime: Runtime,
        deployment_service: DeploymentService | None = None,
        job_queue: DeploymentQueue | None = None,
        interval: float = 10.0,
        auto_restart_active: bool = False,
    ) -> None:
        self.db = db
        self.runtime = runtime
        self.deployment_service = deployment_service
        self.job_queue = job_queue
        self.interval = interval
        self.auto_restart_active = auto_restart_active

        self.dep_repo = DeploymentRepository(db)
        self.event_repo = EventRepository(db)
        self.app_repo = ApplicationRepository(db)

        self.error_count: int = 0
        self.last_error: str | None = None
        self.started_at: str = utc_now()

        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def reconcile_once(self) -> ReconciliationReport:
        """Perform a single reconciliation pass across all applications and containers."""
        report = ReconciliationReport()

        self._reconcile_interrupted_deployments(report)
        self._reconcile_active_containers(report)
        self._reconcile_orphaned_containers(report)

        return report

    def _reconcile_interrupted_deployments(self, report: ReconciliationReport) -> None:
        """Detect deployments left in intermediate states (e.g. after Forge server crash/restart)
        and mark them as FAILED while cleaning up candidate containers.
        """
        in_flight_ids = set()
        if self.job_queue is not None:
            in_flight_ids.update(self.job_queue.get_in_flight_deployment_ids())
        if self.deployment_service is not None:
            in_flight_ids.update(self.deployment_service.get_in_flight_deployment_ids())

        in_progress = self.dep_repo.list_in_progress()
        for dep in in_progress:
            # Priority 3: If deployment is currently in-flight (queue worker or rollback), leave it alone
            if dep.id in in_flight_ids:
                continue

            # Candidate cleanup if created before interrupt
            if dep.candidate_container_id:
                try:
                    self.runtime.remove_container(dep.candidate_container_id, force=True)
                except Exception as exc:
                    logger.warning("Failed to remove candidate container %s: %s", dep.candidate_container_id, exc)

            err_msg = "Deployment interrupted by server restart or process termination"
            self.dep_repo.update_status(
                deployment_id=dep.id,
                target_status=DeploymentStatus.FAILED,
                error_message=err_msg,
            )
            self.event_repo.record(
                app_id=dep.app_id,
                event_kind=EventKind.DEPLOYMENT_FAILED,
                payload={"error": err_msg, "stage": "reconciliation_cleanup"},
                deployment_id=dep.id,
            )
            report.interrupted_deployments_fixed.append(dep.id)

    def _reconcile_active_containers(self, report: ReconciliationReport) -> None:
        """Verify that containers for ACTIVE deployments are actually running in the runtime.
        If a container has died or disappeared, detect the drift and mark deployment as FAILED.
        """
        active_deployments = self.dep_repo.list_all_active()
        for dep in active_deployments:
            if not dep.active_container_id:
                continue

            is_running = False
            exit_code = -1
            try:
                state = self.runtime.inspect_container(dep.active_container_id)
                is_running = state.running
                exit_code = state.exit_code
            except Exception:
                is_running = False

            if not is_running:
                # Active container died or was removed externally
                self.event_repo.record(
                    app_id=dep.app_id,
                    event_kind=EventKind.CONTAINER_CRASHED,
                    payload={
                        "container_id": dep.active_container_id,
                        "exit_code": exit_code,
                    },
                    deployment_id=dep.id,
                )

                if self.auto_restart_active:
                    try:
                        self.runtime.start_container(dep.active_container_id)
                        new_state = self.runtime.inspect_container(dep.active_container_id)
                        if new_state.running:
                            continue
                    except Exception:
                        pass

                err_msg = (
                    f"Active container '{dep.active_container_id}' crashed or stopped "
                    f"(exit code: {exit_code})"
                )
                self.dep_repo.update_status(
                    deployment_id=dep.id,
                    target_status=DeploymentStatus.FAILED,
                    error_message=err_msg,
                )
                self.event_repo.record(
                    app_id=dep.app_id,
                    event_kind=EventKind.DEPLOYMENT_FAILED,
                    payload={"error": err_msg, "stage": "runtime_reconciliation"},
                    deployment_id=dep.id,
                )
                report.dead_containers_detected.append(dep.active_container_id)

    def _reconcile_orphaned_containers(self, report: ReconciliationReport) -> None:
        """Detect and remove containers existing in Docker that Forge is not expecting
        (e.g. stale candidates from aborted deployments, or uncleaned stopped revisions).
        """
        try:
            managed_containers = self.runtime.list_containers(
                label_filters={"forge.managed": "true"}
            )
        except Exception:
            return

        # Determine the set of containers that SHOULD be alive
        expected_containers: set[str] = set()

        # 1. Active deployments
        for dep in self.dep_repo.list_all_active():
            if dep.active_container_id:
                expected_containers.add(dep.active_container_id)

        # 2. In-flight jobs
        if self.job_queue is not None:
            for dep_id in self.job_queue.get_in_flight_deployment_ids():
                d = self.dep_repo.get_by_id(dep_id)
                if d and d.candidate_container_id:
                    expected_containers.add(d.candidate_container_id)

        # Remove any container managed by Forge that is not expected
        apps = self.app_repo.list_all()
        for cont_name in managed_containers:
            if cont_name not in expected_containers:
                try:
                    self.runtime.remove_container(cont_name, force=True)
                    report.orphans_removed.append(cont_name)

                    # Try to associate with an application to record cleanup event
                    matched_app = None
                    for app in apps:
                        if cont_name == app.name or cont_name.startswith(f"{app.name}-"):
                            matched_app = app
                            break

                    if matched_app:
                        self.event_repo.record(
                            app_id=matched_app.id,
                            event_kind=EventKind.CONTAINER_ORPHAN_CLEANED,
                            payload={"cleaned_container": cont_name},
                        )
                except Exception:
                    pass

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop,
            daemon=True,
            name="ForgeReconciler",
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        self._thread = None

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _loop(self) -> None:
        while not self._stop_event.wait(self.interval):
            try:
                self.reconcile_once()
            except Exception as exc:
                # Priority 9: Stop silently swallowing reconciler exceptions; log/report them without killing the loop.
                self.error_count += 1
                self.last_error = str(exc)
                logger.exception("Reconciler loop encountered error during reconciliation pass: %s", exc)
