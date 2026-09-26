import json
import sqlite3
import uuid
from typing import Any

from forge.core.errors import EntityNotFoundError, StorageError
from forge.core.models import (
    Application,
    Deployment,
    DeploymentRevision,
    DeploymentStatus,
    EnvironmentVariable,
    Event,
    EventKind,
    utc_now,
    validate_transition,
)
from forge.storage.db import Database


class ApplicationRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def create(self, name: str, domain: str, container_port: int = 8000) -> Application:
        app_id = f"app-{uuid.uuid4().hex[:8]}"
        app = Application(
            id=app_id,
            name=name,
            domain=domain,
            container_port=container_port,
        )
        conn = self.db.get_connection()
        try:
            conn.execute(
                """
                INSERT INTO applications (id, name, domain, container_port, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (app.id, app.name, app.domain, app.container_port, app.created_at, app.updated_at),
            )
            conn.commit()
            return app
        except sqlite3.IntegrityError as exc:
            raise StorageError(f"Application with name '{name}' already exists") from exc
        finally:
            if self.db.db_path != ":memory:":
                conn.close()

    def get_by_id(self, app_id: str) -> Application | None:
        conn = self.db.get_connection()
        try:
            cursor = conn.execute(
                "SELECT id, name, domain, container_port, created_at, updated_at FROM applications WHERE id = ?",
                (app_id,),
            )
            row = cursor.fetchone()
            if not row:
                return None
            return Application(
                id=row["id"],
                name=row["name"],
                domain=row["domain"],
                container_port=row["container_port"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
        finally:
            if self.db.db_path != ":memory:":
                conn.close()

    def get_by_name(self, name: str) -> Application | None:
        conn = self.db.get_connection()
        try:
            cursor = conn.execute(
                "SELECT id, name, domain, container_port, created_at, updated_at FROM applications WHERE name = ?",
                (name,),
            )
            row = cursor.fetchone()
            if not row:
                return None
            return Application(
                id=row["id"],
                name=row["name"],
                domain=row["domain"],
                container_port=row["container_port"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
        finally:
            if self.db.db_path != ":memory:":
                conn.close()

    def list_all(self) -> list[Application]:
        conn = self.db.get_connection()
        try:
            cursor = conn.execute(
                "SELECT id, name, domain, container_port, created_at, updated_at FROM applications ORDER BY name ASC"
            )
            rows = cursor.fetchall()
            return [
                Application(
                    id=row["id"],
                    name=row["name"],
                    domain=row["domain"],
                    container_port=row["container_port"],
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                )
                for row in rows
            ]
        finally:
            if self.db.db_path != ":memory:":
                conn.close()

    def delete(self, app_id: str) -> bool:
        conn = self.db.get_connection()
        try:
            cursor = conn.execute("DELETE FROM applications WHERE id = ?", (app_id,))
            conn.commit()
            return cursor.rowcount > 0
        finally:
            if self.db.db_path != ":memory:":
                conn.close()


class DeploymentRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def create(
        self,
        app_id: str,
        revision_id: str | None = None,
        status: DeploymentStatus = DeploymentStatus.PENDING,
    ) -> Deployment:
        dep_id = f"dep-{uuid.uuid4().hex[:8]}"
        deployment = Deployment(
            id=dep_id,
            app_id=app_id,
            revision_id=revision_id,
            status=status,
        )
        conn = self.db.get_connection()
        try:
            conn.execute(
                """
                INSERT INTO deployments (id, app_id, revision_id, status, error_message, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    deployment.id,
                    deployment.app_id,
                    deployment.revision_id,
                    deployment.status.value,
                    deployment.error_message,
                    deployment.created_at,
                ),
            )
            conn.commit()
            return deployment
        finally:
            if self.db.db_path != ":memory:":
                conn.close()

    def get_by_id(self, deployment_id: str) -> Deployment | None:
        conn = self.db.get_connection()
        try:
            cursor = conn.execute(
                """
                SELECT id, app_id, revision_id, status, candidate_container_id, active_container_id,
                       error_message, created_at, started_at, finished_at
                FROM deployments WHERE id = ?
                """,
                (deployment_id,),
            )
            row = cursor.fetchone()
            if not row:
                return None
            return Deployment(
                id=row["id"],
                app_id=row["app_id"],
                revision_id=row["revision_id"],
                status=DeploymentStatus(row["status"]),
                candidate_container_id=row["candidate_container_id"],
                active_container_id=row["active_container_id"],
                error_message=row["error_message"],
                created_at=row["created_at"],
                started_at=row["started_at"],
                finished_at=row["finished_at"],
            )
        finally:
            if self.db.db_path != ":memory:":
                conn.close()

    def list_by_app(self, app_id: str, limit: int = 50) -> list[Deployment]:
        conn = self.db.get_connection()
        try:
            cursor = conn.execute(
                """
                SELECT id, app_id, revision_id, status, candidate_container_id, active_container_id,
                       error_message, created_at, started_at, finished_at
                FROM deployments WHERE app_id = ?
                ORDER BY created_at DESC LIMIT ?
                """,
                (app_id, limit),
            )
            rows = cursor.fetchall()
            return [
                Deployment(
                    id=row["id"],
                    app_id=row["app_id"],
                    revision_id=row["revision_id"],
                    status=DeploymentStatus(row["status"]),
                    candidate_container_id=row["candidate_container_id"],
                    active_container_id=row["active_container_id"],
                    error_message=row["error_message"],
                    created_at=row["created_at"],
                    started_at=row["started_at"],
                    finished_at=row["finished_at"],
                )
                for row in rows
            ]
        finally:
            if self.db.db_path != ":memory:":
                conn.close()

    def get_active_deployment(self, app_id: str) -> Deployment | None:
        conn = self.db.get_connection()
        try:
            cursor = conn.execute(
                """
                SELECT id, app_id, revision_id, status, candidate_container_id, active_container_id,
                       error_message, created_at, started_at, finished_at
                FROM deployments WHERE app_id = ? AND status = ?
                ORDER BY created_at DESC LIMIT 1
                """,
                (app_id, DeploymentStatus.ACTIVE.value),
            )
            row = cursor.fetchone()
            if not row:
                return None
            return Deployment(
                id=row["id"],
                app_id=row["app_id"],
                revision_id=row["revision_id"],
                status=DeploymentStatus(row["status"]),
                candidate_container_id=row["candidate_container_id"],
                active_container_id=row["active_container_id"],
                error_message=row["error_message"],
                created_at=row["created_at"],
                started_at=row["started_at"],
                finished_at=row["finished_at"],
            )
        finally:
            if self.db.db_path != ":memory:":
                conn.close()

    def update_status(
        self,
        deployment_id: str,
        target_status: DeploymentStatus,
        error_message: str | None = None,
        candidate_container_id: str | None = None,
        active_container_id: str | None = None,
    ) -> Deployment:
        dep = self.get_by_id(deployment_id)
        if dep is None:
            raise EntityNotFoundError(f"Deployment '{deployment_id}' not found")

        # Validate transition using State Machine
        validate_transition(dep.status, target_status)

        started_at = dep.started_at
        finished_at = dep.finished_at
        now = utc_now()

        if target_status in (DeploymentStatus.BUILDING, DeploymentStatus.STARTING) and started_at is None:
            started_at = now
        elif target_status in (DeploymentStatus.ACTIVE, DeploymentStatus.FAILED, DeploymentStatus.STOPPED, DeploymentStatus.ROLLED_BACK):
            if finished_at is None:
                finished_at = now

        new_candidate = candidate_container_id if candidate_container_id is not None else dep.candidate_container_id
        new_active = active_container_id if active_container_id is not None else dep.active_container_id
        new_error = error_message if error_message is not None else dep.error_message

        conn = self.db.get_connection()
        try:
            conn.execute(
                """
                UPDATE deployments
                SET status = ?, candidate_container_id = ?, active_container_id = ?,
                    error_message = ?, started_at = ?, finished_at = ?
                WHERE id = ?
                """,
                (
                    target_status.value,
                    new_candidate,
                    new_active,
                    new_error,
                    started_at,
                    finished_at,
                    deployment_id,
                ),
            )
            conn.commit()
            return Deployment(
                id=dep.id,
                app_id=dep.app_id,
                revision_id=dep.revision_id,
                status=target_status,
                candidate_container_id=new_candidate,
                active_container_id=new_active,
                error_message=new_error,
                created_at=dep.created_at,
                started_at=started_at,
                finished_at=finished_at,
            )
        finally:
            if self.db.db_path != ":memory:":
                conn.close()


class EventRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def record(
        self,
        app_id: str,
        event_kind: EventKind,
        payload: dict[str, Any],
        deployment_id: str | None = None,
    ) -> Event:
        # Zero-leak: Event validation ensures raw env vars are forbidden
        event = Event(
            id=f"evt-{uuid.uuid4().hex[:8]}",
            app_id=app_id,
            deployment_id=deployment_id,
            event_kind=event_kind,
            payload=payload,
        )
        payload_json = json.dumps(event.payload)
        conn = self.db.get_connection()
        try:
            conn.execute(
                """
                INSERT INTO events (id, app_id, deployment_id, event_kind, payload, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (event.id, event.app_id, event.deployment_id, event.event_kind.value, payload_json, event.created_at),
            )
            conn.commit()
            return event
        finally:
            if self.db.db_path != ":memory:":
                conn.close()

    def list_by_app(self, app_id: str, limit: int = 100) -> list[Event]:
        conn = self.db.get_connection()
        try:
            cursor = conn.execute(
                """
                SELECT id, app_id, deployment_id, event_kind, payload, created_at
                FROM events WHERE app_id = ?
                ORDER BY created_at DESC, rowid DESC LIMIT ?
                """,
                (app_id, limit),
            )
            rows = cursor.fetchall()
            return [
                Event(
                    id=row["id"],
                    app_id=row["app_id"],
                    deployment_id=row["deployment_id"],
                    event_kind=EventKind(row["event_kind"]),
                    payload=json.loads(row["payload"]),
                    created_at=row["created_at"],
                )
                for row in rows
            ]
        finally:
            if self.db.db_path != ":memory:":
                conn.close()


class EnvironmentRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def set_var(self, app_id: str, key: str, value: str) -> None:
        now = utc_now()
        var_id = f"env-{uuid.uuid4().hex[:8]}"
        conn = self.db.get_connection()
        try:
            conn.execute(
                """
                INSERT INTO environment_variables (id, app_id, key, value, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(app_id, key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (var_id, app_id, key, value, now, now),
            )
            conn.commit()
        finally:
            if self.db.db_path != ":memory:":
                conn.close()

    def get_vars(self, app_id: str) -> dict[str, str]:
        conn = self.db.get_connection()
        try:
            cursor = conn.execute(
                "SELECT key, value FROM environment_variables WHERE app_id = ?",
                (app_id,),
            )
            return {row["key"]: row["value"] for row in cursor.fetchall()}
        finally:
            if self.db.db_path != ":memory:":
                conn.close()

    def get_masked_vars(self, app_id: str) -> list[dict[str, Any]]:
        # SEC-07: Never return raw secret values in standard metadata queries
        conn = self.db.get_connection()
        try:
            cursor = conn.execute(
                "SELECT key, updated_at FROM environment_variables WHERE app_id = ? ORDER BY key ASC",
                (app_id,),
            )
            return [
                {
                    "key": row["key"],
                    "is_set": True,
                    "updated_at": row["updated_at"],
                }
                for row in cursor.fetchall()
            ]
        finally:
            if self.db.db_path != ":memory:":
                conn.close()

    def delete_var(self, app_id: str, key: str) -> bool:
        conn = self.db.get_connection()
        try:
            cursor = conn.execute(
                "DELETE FROM environment_variables WHERE app_id = ? AND key = ?",
                (app_id, key),
            )
            conn.commit()
            return cursor.rowcount > 0
        finally:
            if self.db.db_path != ":memory:":
                conn.close()
