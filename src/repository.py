import json
import sqlite3
from datetime import datetime, timezone

from .domain import ConflictError, NotFoundError


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SQLiteRepository:
    def __init__(self, path):
        self.path = str(path)
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self):
        with self._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS entities (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    data TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_entities_kind_status
                    ON entities(kind, status);
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    actor_role TEXT NOT NULL,
                    action TEXT NOT NULL,
                    from_status TEXT,
                    to_status TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_audit_entity
                    ON audit_log(entity_id, id);
                CREATE TABLE IF NOT EXISTS idempotency (
                    actor_id TEXT NOT NULL,
                    idem_key TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    action TEXT NOT NULL DEFAULT 'create',
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(actor_id, idem_key)
                );
                CREATE TABLE IF NOT EXISTS freeze_records (
                    entity_id TEXT PRIMARY KEY,
                    unit_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    frozen_version INTEGER NOT NULL,
                    frozen_status TEXT NOT NULL,
                    frozen_owner TEXT,
                    frozen_at TEXT NOT NULL,
                    unfrozen_at TEXT,
                    unfrozen_by TEXT,
                    unfreeze_note TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_freeze_unit
                    ON freeze_records(unit_id);
            """)
            self._migrate_idempotency(connection)

    @staticmethod
    def _migrate_idempotency(connection):
        """Add the ``action`` column to pre-existing idempotency tables."""
        columns = [row[1] for row in connection.execute("PRAGMA table_info(idempotency)").fetchall()]
        if "action" not in columns:
            connection.execute(
                "ALTER TABLE idempotency ADD COLUMN action TEXT NOT NULL DEFAULT 'create'"
            )

    @staticmethod
    def _entity_from_row(row):
        return {
            "id": row["id"],
            "kind": row["kind"],
            "status": row["status"],
            "version": int(row["version"]),
            "data": json.loads(row["data"]),
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def create_entity(self, entity_id, kind, status, data, actor_id):
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO entities(id, kind, status, version, data, created_by, created_at, updated_at) "
                "VALUES (?, ?, ?, 1, ?, ?, ?, ?)",
                (entity_id, kind, status, payload, actor_id, now, now),
            )
        return self.get_entity(entity_id)

    def get_entity(self, entity_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
        return self._entity_from_row(row) if row else None

    def list_entities(self, kind=None, status=None):
        clauses = []
        params = []
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM entities" + where + " ORDER BY created_at, id", params
            ).fetchall()
        return [self._entity_from_row(row) for row in rows]

    def find_entities(self, kind, field, value):
        return [
            entity
            for entity in self.list_entities(kind=kind)
            if (entity["id"] == value if field == "id" else entity["data"].get(field) == value)
        ]

    def update_entity(self, entity_id, expected_version, status, data):
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT version FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
            if not row:
                raise NotFoundError("entity not found: " + entity_id)
            current_version = int(row["version"])
            if expected_version is not None and current_version != int(expected_version):
                raise ConflictError(
                    "version conflict: expected %s, found %s"
                    % (expected_version, current_version)
                )
            connection.execute(
                "UPDATE entities SET status = ?, version = version + 1, data = ?, updated_at = ? "
                "WHERE id = ? AND version = ?",
                (status, payload, now, entity_id, current_version),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_entity(entity_id)

    def append_audit(self, entity_id, actor_id, actor_role, action, from_status, to_status, detail):
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO audit_log(entity_id, actor_id, actor_role, action, from_status, to_status, detail, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    entity_id,
                    actor_id,
                    actor_role,
                    action,
                    from_status,
                    to_status,
                    json.dumps(detail, ensure_ascii=False, sort_keys=True),
                    utcnow(),
                ),
            )

    def list_audit(self, entity_id=None):
        with self._connect() as connection:
            if entity_id:
                rows = connection.execute(
                    "SELECT * FROM audit_log WHERE entity_id = ? ORDER BY id", (entity_id,)
                ).fetchall()
            else:
                rows = connection.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
        return [
            {
                "id": row["id"],
                "entity_id": row["entity_id"],
                "actor_id": row["actor_id"],
                "actor_role": row["actor_role"],
                "action": row["action"],
                "from_status": row["from_status"],
                "to_status": row["to_status"],
                "detail": json.loads(row["detail"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def get_idempotency(self, actor_id, idem_key):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT entity_id, action FROM idempotency WHERE actor_id = ? AND idem_key = ?",
                (actor_id, idem_key),
            ).fetchone()
        if not row:
            return None
        return {"entity_id": row["entity_id"], "action": row["action"]}

    def save_idempotency(self, actor_id, idem_key, entity_id, action="create"):
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO idempotency(actor_id, idem_key, entity_id, action, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (actor_id, idem_key, entity_id, action, utcnow()),
            )

    # -- freeze records ----------------------------------------------------

    def freeze_entity(self, entity_id, unit_id, kind, version, status, owner):
        """Record a freeze for ``entity_id``.

        An active freeze record is never overwritten, so re-issuing the
        shutdown request (network retry) cannot freeze the entity twice.
        """
        with self._connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO freeze_records"
                "(entity_id, unit_id, kind, frozen_version, frozen_status, frozen_owner, frozen_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (entity_id, unit_id, kind, version, status, owner, utcnow()),
            )

    def get_freeze(self, entity_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM freeze_records WHERE entity_id = ?", (entity_id,)
            ).fetchone()
        return self._freeze_from_row(row) if row else None

    def is_frozen(self, entity_id):
        record = self.get_freeze(entity_id)
        return record is not None and record["unfrozen_at"] is None

    def list_frozen(self, unit_id=None, active_only=True):
        clauses = []
        params = []
        if unit_id:
            clauses.append("unit_id = ?")
            params.append(unit_id)
        if active_only:
            clauses.append("unfrozen_at IS NULL")
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM freeze_records" + where + " ORDER BY frozen_at, entity_id",
                params,
            ).fetchall()
        return [self._freeze_from_row(row) for row in rows]

    def unfreeze_entity(self, entity_id, actor_id, note=None):
        with self._connect() as connection:
            connection.execute(
                "UPDATE freeze_records SET unfrozen_at = ?, unfrozen_by = ?, unfreeze_note = ? "
                "WHERE entity_id = ? AND unfrozen_at IS NULL",
                (utcnow(), actor_id, note, entity_id),
            )

    @staticmethod
    def _freeze_from_row(row):
        return {
            "entity_id": row["entity_id"],
            "unit_id": row["unit_id"],
            "kind": row["kind"],
            "frozen_version": int(row["frozen_version"]),
            "frozen_status": row["frozen_status"],
            "frozen_owner": row["frozen_owner"],
            "frozen_at": row["frozen_at"],
            "unfrozen_at": row["unfrozen_at"],
            "unfrozen_by": row["unfrozen_by"],
            "unfreeze_note": row["unfreeze_note"],
        }

    def ping(self):
        with self._connect() as connection:
            connection.execute("SELECT 1").fetchone()
        return True
