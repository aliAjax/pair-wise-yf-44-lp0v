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
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(actor_id, idem_key)
                );
                CREATE TABLE IF NOT EXISTS freezes (
                    entity_id TEXT PRIMARY KEY,
                    unit_id TEXT NOT NULL,
                    active INTEGER NOT NULL,
                    born_frozen INTEGER NOT NULL DEFAULT 0,
                    reason TEXT NOT NULL,
                    frozen_version INTEGER NOT NULL,
                    frozen_status TEXT NOT NULL,
                    frozen_owner TEXT,
                    released_by TEXT,
                    released_at TEXT,
                    release_note TEXT,
                    resumed_at TEXT,
                    reblock_reason TEXT,
                    frozen_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_freezes_unit
                    ON freezes(unit_id, active);
            """)
            # Migrations for databases created before shutdown freeze support.
            columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(idempotency)")
            }
            if "action" not in columns:
                connection.execute("ALTER TABLE idempotency ADD COLUMN action TEXT")
            if "fingerprint" not in columns:
                connection.execute("ALTER TABLE idempotency ADD COLUMN fingerprint TEXT")

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

    @staticmethod
    def _freeze_from_row(row):
        return {
            "entity_id": row["entity_id"],
            "unit_id": row["unit_id"],
            "active": bool(row["active"]),
            "born_frozen": bool(row["born_frozen"]),
            "reason": row["reason"],
            "frozen_version": int(row["frozen_version"]),
            "frozen_status": row["frozen_status"],
            "frozen_owner": row["frozen_owner"],
            "released_by": row["released_by"],
            "released_at": row["released_at"],
            "release_note": row["release_note"],
            "resumed_at": row["resumed_at"],
            "reblock_reason": row["reblock_reason"],
            "frozen_at": row["frozen_at"],
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

    # -- Transactional helpers ------------------------------------------------

    def begin(self):
        connection = self._connect()
        connection.execute("BEGIN IMMEDIATE")
        return connection

    def lock_entity(self, connection, entity_id):
        row = connection.execute(
            "SELECT * FROM entities WHERE id = ?", (entity_id,)
        ).fetchone()
        return self._entity_from_row(row) if row else None

    def insert_entity(self, connection, entity_id, kind, status, data, actor_id, now):
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        connection.execute(
            "INSERT INTO entities(id, kind, status, version, data, created_by, created_at, updated_at) "
            "VALUES (?, ?, ?, 1, ?, ?, ?, ?)",
            (entity_id, kind, status, payload, actor_id, now, now),
        )

    def write_entity(self, connection, entity_id, status, data, now):
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        connection.execute(
            "UPDATE entities SET status = ?, version = version + 1, data = ?, updated_at = ? "
            "WHERE id = ?",
            (status, payload, now, entity_id),
        )

    def audit_with(self, connection, entity_id, actor_id, actor_role, action,
                   from_status, to_status, detail, now):
        connection.execute(
            "INSERT INTO audit_log(entity_id, actor_id, actor_role, action, from_status, to_status, detail, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                entity_id, actor_id, actor_role, action, from_status, to_status,
                json.dumps(detail, ensure_ascii=False, sort_keys=True), now,
            ),
        )

    def get_idempotency_row(self, actor_id, idem_key, connection=None):
        sql = (
            "SELECT entity_id, action, fingerprint FROM idempotency "
            "WHERE actor_id = ? AND idem_key = ?"
        )
        params = (actor_id, idem_key)
        if connection is None:
            with self._connect() as conn:
                row = conn.execute(sql, params).fetchone()
        else:
            row = connection.execute(sql, params).fetchone()
        return (
            {"entity_id": row["entity_id"], "action": row["action"], "fingerprint": row["fingerprint"]}
            if row
            else None
        )

    def save_idempotency_with(self, connection, actor_id, idem_key, entity_id,
                              action, fingerprint, now):
        connection.execute(
            "INSERT INTO idempotency(actor_id, idem_key, entity_id, action, fingerprint, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (actor_id, idem_key, entity_id, action, fingerprint, now),
        )

    # -- Freeze overlay --------------------------------------------------------

    def unit_freeze_scope(self, connection, unit_id):
        """Changes in flight plus every action item belonging to those changes."""
        changes = [
            self._entity_from_row(row)
            for row in connection.execute(
                "SELECT * FROM entities WHERE kind = 'change' AND status != 'closed' "
                "ORDER BY created_at, id"
            ).fetchall()
            if json.loads(row["data"]).get("unit_id") == unit_id
        ]
        change_ids = {item["id"] for item in changes}
        items = []
        if change_ids:
            placeholders = ",".join("?" for _ in change_ids)
            rows = connection.execute(
                "SELECT * FROM entities WHERE kind = 'action_item' AND data IS NOT NULL",
            ).fetchall()
            for row in rows:
                entity = self._entity_from_row(row)
                if entity["data"].get("change_id") in change_ids:
                    items.append(entity)
        return changes, items

    def insert_freeze(self, connection, entity_id, unit_id, reason, entity,
                      now, born_frozen=False):
        owner = None
        if entity["kind"] == "action_item":
            owner = entity["data"].get("owner")
        connection.execute(
            "INSERT INTO freezes(entity_id, unit_id, active, born_frozen, reason, "
            "frozen_version, frozen_status, frozen_owner, frozen_at, updated_at) "
            "VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(entity_id) DO UPDATE SET "
            "unit_id = excluded.unit_id, active = 1, born_frozen = excluded.born_frozen, "
            "reason = excluded.reason, frozen_version = excluded.frozen_version, "
            "frozen_status = excluded.frozen_status, frozen_owner = excluded.frozen_owner, "
            "released_by = NULL, released_at = NULL, release_note = NULL, "
            "resumed_at = NULL, reblock_reason = NULL, updated_at = excluded.updated_at",
            (
                entity_id, unit_id, 1 if born_frozen else 0, reason,
                entity["version"], entity["status"], owner, now, now,
            ),
        )
        return connection.total_changes

    def get_freeze(self, entity_id, connection=None):
        sql = "SELECT * FROM freezes WHERE entity_id = ?"
        if connection is None:
            with self._connect() as conn:
                row = conn.execute(sql, (entity_id,)).fetchone()
        else:
            row = connection.execute(sql, (entity_id,)).fetchone()
        return self._freeze_from_row(row) if row else None

    def list_freezes(self, connection, unit_id):
        rows = connection.execute(
            "SELECT * FROM freezes WHERE unit_id = ? ORDER BY entity_id", (unit_id,)
        ).fetchall()
        return [self._freeze_from_row(row) for row in rows]

    def release_freeze(self, connection, entity_id, actor_id, note, now):
        connection.execute(
            "UPDATE freezes SET active = 0, released_by = ?, released_at = ?, "
            "release_note = ?, updated_at = ? WHERE entity_id = ? AND active = 1",
            (actor_id, now, note, now, entity_id),
        )

    def resume_freeze(self, connection, entity_id, now, reblock_reason=None):
        """Deactivate a freeze on startup; reblock_reason keeps it active with a cause."""
        if reblock_reason:
            connection.execute(
                "UPDATE freezes SET reblock_reason = ?, updated_at = ? "
                "WHERE entity_id = ? AND active = 1",
                (reblock_reason, now, entity_id),
            )
        else:
            connection.execute(
                "UPDATE freezes SET active = 0, resumed_at = ?, reblock_reason = NULL, "
                "updated_at = ? WHERE entity_id = ? AND active = 1",
                (now, now, entity_id),
            )

    def reblock_freeze(self, connection, entity_id, reason, now):
        """Reactivate a safety-released freeze after startup finds drift."""
        connection.execute(
            "UPDATE freezes SET active = 1, reblock_reason = ?, updated_at = ? "
            "WHERE entity_id = ?",
            (reason, now, entity_id),
        )

    # -- Legacy single-statement operations ------------------------------------

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
        row = self.get_idempotency_row(actor_id, idem_key)
        return row["entity_id"] if row else None

    def save_idempotency(self, actor_id, idem_key, entity_id):
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO idempotency(actor_id, idem_key, entity_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                (actor_id, idem_key, entity_id, utcnow()),
            )

    def ping(self):
        with self._connect() as connection:
            connection.execute("SELECT 1").fetchone()
        return True
