import json
from uuid import uuid4

from .audit import AuditTrail
from .domain import (
    ConflictError,
    FreezeBlocked,
    InvalidTransition,
    NotFoundError,
)
from .repository import utcnow
from .rules import RuleEngine


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    # -- helpers ---------------------------------------------------------------

    @staticmethod
    def _fingerprint(parts):
        return json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str)

    def _ensure_same_request(self, stored, fingerprint):
        if stored.get("fingerprint") is not None and stored["fingerprint"] != fingerprint:
            raise ConflictError("idempotency key was already used for a different request")

    def _replayed_idempotency(self, actor, idem_key, fingerprint, action):
        existing = self.repository.get_idempotency_row(actor.user_id, idem_key)
        if not existing:
            return None
        self._ensure_same_request(existing, fingerprint)
        entity = self.repository.get_entity(existing["entity_id"])
        if entity:
            return self._replay_result(action, entity)
        return None

    def _replay_result(self, action, entity, connection=None):
        if entity["kind"] == "unit":
            if action == "shutdown":
                return dict(entity, frozen_items=self._freeze_views(entity["id"], connection))
            if action == "startup":
                return self._startup_result(entity, connection)
        return entity

    def _idem_in_tx(self, connection, actor, idem_key, fingerprint, action):
        if not idem_key:
            return None
        row = self.repository.get_idempotency_row(actor.user_id, idem_key, connection)
        if not row:
            return None
        self._ensure_same_request(row, fingerprint)
        entity = self.repository.lock_entity(connection, row["entity_id"])
        if entity:
            return self._replay_result(action, entity, connection)
        return None

    def _save_idem(self, connection, actor, idem_key, entity_id, action, fingerprint, now):
        if idem_key:
            self.repository.save_idempotency_with(
                connection, actor.user_id, idem_key, entity_id, action, fingerprint, now
            )

    def _freeze_view(self, entity, freeze):
        return {
            "entity_id": entity["id"],
            "kind": entity["kind"],
            "status": entity["status"],
            "version": entity["version"],
            "frozen_version": freeze["frozen_version"],
            "frozen_status": freeze["frozen_status"],
            "unit_id": freeze["unit_id"],
            "reason": freeze["reblock_reason"] or freeze["reason"],
            "reblock_reason": freeze["reblock_reason"],
            "released_by": freeze["released_by"],
            "born_frozen": freeze["born_frozen"],
        }

    def _active_freezes(self, unit_id):
        rows = []
        for entity in self.repository.list_entities():
            freeze = self.repository.get_freeze(entity["id"])
            if freeze and freeze["active"] and freeze["unit_id"] == unit_id:
                rows.append((entity, freeze))
        rows.sort(key=lambda pair: pair[0]["id"])
        return [self._freeze_view(entity, freeze) for entity, freeze in rows]

    def _freeze_views(self, unit_id, connection=None):
        if connection is not None:
            views = []
            for freeze in self.repository.list_freezes(connection, unit_id):
                if not freeze["active"]:
                    continue
                entity = self.repository.lock_entity(connection, freeze["entity_id"])
                if entity:
                    views.append(self._freeze_view(entity, freeze))
            views.sort(key=lambda view: view["entity_id"])
            return views
        return self._active_freezes(unit_id)

    def _startup_result(self, unit, connection=None):
        resumed = []
        still_frozen = []
        if connection is not None:
            freezes = self.repository.list_freezes(connection, unit["id"])
            for freeze in freezes:
                entity = self.repository.lock_entity(connection, freeze["entity_id"])
                if not entity:
                    continue
                if freeze["active"]:
                    still_frozen.append(self._freeze_view(entity, freeze))
                elif freeze["resumed_at"]:
                    resumed.append(entity["id"])
        else:
            for entity in self.repository.list_entities():
                freeze = self.repository.get_freeze(entity["id"])
                if not freeze or freeze["unit_id"] != unit["id"]:
                    continue
                if freeze["active"]:
                    still_frozen.append(self._freeze_view(entity, freeze))
                elif freeze["resumed_at"]:
                    resumed.append(entity["id"])
        resumed.sort()
        still_frozen.sort(key=lambda view: view["entity_id"])
        return dict(unit, resumed=resumed, still_frozen=still_frozen)

    # -- create ----------------------------------------------------------------

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        fingerprint = self._fingerprint(("create", kind, payload))
        if idempotency_key:
            replayed = self._replayed_idempotency(actor, idempotency_key, fingerprint, "create")
            if replayed is not None:
                return replayed
        self.rules.validate_create(actor, kind, payload, self._lookup)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind)

        born_unit_id = None
        if kind == "change":
            born_unit_id = payload.get("unit_id")
        elif kind == "action_item":
            change = self.repository.get_entity(payload.get("change_id"))
            if change:
                born_unit_id = change["data"].get("unit_id")

        now = utcnow()
        connection = self.repository.begin()
        try:
            replayed = self._idem_in_tx(
                connection, actor, idempotency_key, fingerprint, "create"
            )
            if replayed is not None:
                connection.commit()
                return replayed
            if self.repository.lock_entity(connection, entity_id):
                raise ConflictError("entity already exists: " + entity_id)
            self.repository.insert_entity(
                connection, entity_id, kind, status, payload, actor.user_id, now
            )
            self.repository.audit_with(
                connection, entity_id, actor.user_id, actor.role,
                "create", None, status, {"kind": kind}, now,
            )
            if born_unit_id:
                unit = self.repository.lock_entity(connection, born_unit_id)
                if self.rules.is_shutdown(unit):
                    born_payload = dict(payload)
                    if kind == "change":
                        born_payload["unit_id"] = born_unit_id
                    view = {
                        "id": entity_id, "kind": kind, "status": status,
                        "version": 1, "data": born_payload,
                    }
                    self.repository.insert_freeze(
                        connection, entity_id, born_unit_id,
                        "created while unit shutdown", view, now, born_frozen=True,
                    )
                    self.repository.audit_with(
                        connection, entity_id, actor.user_id, actor.role,
                        "freeze", None, status,
                        {"unit_id": born_unit_id, "reason": "created while unit shutdown"},
                        now,
                    )
            self._save_idem(
                connection, actor, idempotency_key, entity_id,
                "create:" + kind, fingerprint, now,
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.repository.get_entity(entity_id)

    # -- transition ------------------------------------------------------------

    def transition(self, actor, entity_id, action, data=None, expected_version=None,
                   idempotency_key=None):
        payload = dict(data or {})
        fingerprint = self._fingerprint((action, payload, expected_version))
        if idempotency_key:
            replayed = self._replayed_idempotency(actor, idempotency_key, fingerprint, action)
            if replayed is not None:
                return replayed
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        kind = self.rules.normalize_kind(entity["kind"])
        if kind == "unit" and action in ("shutdown", "startup"):
            return self._unit_lifecycle(
                actor, entity, action, payload, expected_version,
                idempotency_key, fingerprint,
            )
        if action == "release":
            return self._release(actor, entity, payload, idempotency_key, fingerprint)
        return self._apply_transition(
            actor, entity, action, payload, expected_version,
            idempotency_key, fingerprint,
        )

    def _apply_transition(self, actor, entity, action, payload, expected_version,
                          idem_key, fingerprint):
        freeze = self.repository.get_freeze(entity["id"])
        if freeze and freeze["active"]:
            latest = self.repository.get_entity(entity["id"])
            raise FreezeBlocked(
                "entity %s is frozen under shutdown of unit %s"
                % (entity["id"], freeze["unit_id"]),
                latest,
                self._active_freezes(freeze["unit_id"]),
            )
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, payload, self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        now = utcnow()
        connection = self.repository.begin()
        try:
            replayed = self._idem_in_tx(connection, actor, idem_key, fingerprint, action)
            if replayed is not None:
                connection.commit()
                return replayed
            locked = self.repository.lock_entity(connection, entity["id"])
            if not locked:
                raise NotFoundError("entity not found: " + entity["id"])
            if locked["version"] != expected:
                raise ConflictError(
                    "version conflict: expected %s, found %s"
                    % (expected, locked["version"])
                )
            locked_freeze = self.repository.get_freeze(entity["id"], connection)
            if locked_freeze and locked_freeze["active"]:
                raise FreezeBlocked(
                    "entity %s is frozen under shutdown of unit %s"
                    % (entity["id"], locked_freeze["unit_id"]),
                    locked,
                    self._freeze_views(locked_freeze["unit_id"], connection),
                )
            self.repository.write_entity(connection, entity["id"], next_status, merged, now)
            self.repository.audit_with(
                connection, entity["id"], actor.user_id, actor.role,
                action, locked["status"], next_status, {"patch": patch}, now,
            )
            self._save_idem(
                connection, actor, idem_key, entity["id"], action, fingerprint, now
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.repository.get_entity(entity["id"])

    def _release(self, actor, entity, payload, idem_key, fingerprint):
        self.rules.validate_release(actor, entity, payload)
        now = utcnow()
        connection = self.repository.begin()
        try:
            replayed = self._idem_in_tx(connection, actor, idem_key, fingerprint, "release")
            if replayed is not None:
                connection.commit()
                return replayed
            locked = self.repository.lock_entity(connection, entity["id"])
            freeze = self.repository.get_freeze(entity["id"], connection)
            if not freeze or not freeze["active"]:
                raise InvalidTransition("entity %s is not frozen" % entity["id"])
            self.repository.release_freeze(
                connection, entity["id"], actor.user_id, payload["inventory_note"], now
            )
            self.repository.audit_with(
                connection, entity["id"], actor.user_id, actor.role,
                "release", locked["status"], locked["status"],
                {"unit_id": freeze["unit_id"], "inventory_note": payload["inventory_note"]},
                now,
            )
            self._save_idem(
                connection, actor, idem_key, entity["id"], "release", fingerprint, now
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.repository.get_entity(entity["id"])

    # -- shutdown / startup orchestration --------------------------------------

    def _unit_lifecycle(self, actor, unit, action, payload, expected_version,
                        idem_key, fingerprint):
        next_status, patch = self.rules.validate_transition(
            actor, unit, action, payload, self._lookup
        )
        expected = int(expected_version) if expected_version is not None else unit["version"]
        now = utcnow()
        connection = self.repository.begin()
        try:
            replayed = self._idem_in_tx(connection, actor, idem_key, fingerprint, action)
            if replayed is not None:
                connection.commit()
                return replayed
            locked_unit = self.repository.lock_entity(connection, unit["id"])
            if not locked_unit:
                raise NotFoundError("entity not found: " + unit["id"])
            if locked_unit["version"] != expected:
                raise ConflictError(
                    "version conflict: expected %s, found %s"
                    % (expected, locked_unit["version"])
                )
            merged = dict(locked_unit["data"])
            merged.update(patch)
            self.repository.write_entity(connection, unit["id"], next_status, merged, now)
            self.repository.audit_with(
                connection, unit["id"], actor.user_id, actor.role,
                action, locked_unit["status"], next_status, {"patch": patch}, now,
            )
            frozen_items = []
            if action == "shutdown":
                changes, items = self.repository.unit_freeze_scope(connection, unit["id"])
                for child in changes + items:
                    self.repository.insert_freeze(
                        connection, child["id"], unit["id"], "unit shutdown", child, now
                    )
                    self.repository.audit_with(
                        connection, child["id"], actor.user_id, actor.role,
                        "freeze", child["status"], child["status"],
                        {"unit_id": unit["id"], "reason": "unit shutdown",
                         "frozen_version": child["version"]},
                        now,
                    )
                    frozen_items.append({
                        "entity_id": child["id"],
                        "kind": child["kind"],
                        "status": child["status"],
                        "version": child["version"],
                        "frozen_version": child["version"],
                        "frozen_status": child["status"],
                        "unit_id": unit["id"],
                        "reason": "unit shutdown",
                        "reblock_reason": None,
                        "released_by": None,
                        "born_frozen": False,
                    })
            else:
                self._startup_review(connection, unit["id"], actor, now)
            self._save_idem(
                connection, actor, idem_key, unit["id"], action, fingerprint, now
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        updated = self.repository.get_entity(unit["id"])
        if action == "shutdown":
            return dict(updated, frozen_items=frozen_items)
        return self._startup_result(updated)

    def _startup_review(self, connection, unit_id, actor, now):
        """Recompute every freeze (including safety-released ones) against
        versions captured at shutdown; drifted items are re-frozen."""
        for freeze in self.repository.list_freezes(connection, unit_id):
            entity_id = freeze["entity_id"]
            entity = self.repository.lock_entity(connection, entity_id)
            if not entity:
                continue
            reason = self._drift_reason(entity, freeze)
            if reason:
                self.repository.reblock_freeze(connection, entity_id, reason, now)
                self.repository.audit_with(
                    connection, entity_id, actor.user_id, actor.role,
                    "freeze_reblock", entity["status"], entity["status"],
                    {"unit_id": unit_id, "reason": reason,
                     "frozen_version": freeze["frozen_version"],
                     "current_version": entity["version"]},
                    now,
                )
            elif freeze["active"]:
                self.repository.resume_freeze(connection, entity_id, now)
                self.repository.audit_with(
                    connection, entity_id, actor.user_id, actor.role,
                    "resume", entity["status"], entity["status"],
                    {"unit_id": unit_id, "frozen_version": freeze["frozen_version"]},
                    now,
                )

    @staticmethod
    def _drift_reason(entity, freeze):
        """Changes rolled back / items reopened / owners changed stay frozen."""
        if entity["version"] == freeze["frozen_version"]:
            return None
        if entity["kind"] == "change" and entity["status"] == "rolled_back":
            return "change rolled back during shutdown"
        if entity["kind"] == "action_item":
            if entity["status"] == "open" and freeze["frozen_status"] != "open":
                return "action item reopened during shutdown"
            owner = entity["data"].get("owner")
            if freeze["frozen_owner"] is not None and owner != freeze["frozen_owner"]:
                return "owner changed during shutdown: %s -> %s" % (
                    freeze["frozen_owner"], owner,
                )
        return None

    # -- reads -----------------------------------------------------------------

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
