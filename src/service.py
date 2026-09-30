from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .rules import RuleEngine


# Progress actions that engineers/verifiers must not advance while an entity
# is frozen by a unit shutdown.  Submitting any of these on a frozen entity
# returns the latest version together with the conflict items.
BLOCKED_PROGRESS_ACTIONS = {
    "change": ("implement", "commission"),
    "action_item": ("complete", "verify"),
}


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing["entity_id"])
                if entity:
                    return entity
        self.rules.validate_create(actor, kind, payload, self._lookup)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id, "create")
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None, idempotency_key=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)

        # Idempotency: a network retry of the same transition must not
        # re-execute it (no re-freeze, no duplicate audit).  Return the
        # current entity state when the same actor re-issues the same
        # entity+action request.
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                if existing["entity_id"] == entity_id and existing["action"] == action:
                    return self.repository.get_entity(entity_id)
                raise ConflictError(
                    "idempotency key already used for a different request: " + idempotency_key
                )

        # Freeze guard: progress actions on frozen entities are rejected
        # with the latest version and the conflict list.
        if action in BLOCKED_PROGRESS_ACTIONS.get(entity["kind"], ()) and self.repository.is_frozen(entity_id):
            conflicts = self._freeze_conflicts(entity)
            raise ConflictError(
                "entity %s is frozen by unit shutdown; progress actions are blocked" % entity_id,
                details={"entity": entity, "conflicts": conflicts},
            )

        # Unfreeze is a freeze-record operation, not a status transition.
        if action == "unfreeze":
            updated = self._unfreeze(actor, entity, data)
            if idempotency_key:
                self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id, "unfreeze")
            return updated

        # Startup recalculation: refuse to start while frozen items remain.
        if entity["kind"] == "unit" and action == "startup":
            self._check_startup(actor, entity)

        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )

        # Shutdown freezes every change and action item under the unit.
        if entity["kind"] == "unit" and action == "shutdown":
            self._freeze_unit_items(actor, entity_id)

        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id, action)
        return updated

    # -- freeze / unfreeze -------------------------------------------------

    def _freeze_conflicts(self, entity):
        record = self.repository.get_freeze(entity["id"])
        reason = "entity is frozen by unit shutdown"
        if record:
            reason = "entity is frozen by unit shutdown %s" % record["unit_id"]
        return [{"code": "frozen", "reason": reason}]

    def _freeze_unit_items(self, actor, unit_id):
        """Freeze all changes under ``unit_id`` and their action items."""
        changes = self.repository.find_entities("change", "unit_id", unit_id)
        for change in changes:
            self._freeze_entity(actor, change, unit_id)
            items = self.repository.find_entities("action_item", "change_id", change["id"])
            for item in items:
                self._freeze_entity(actor, item, unit_id)

    def _freeze_entity(self, actor, entity, unit_id):
        existing = self.repository.get_freeze(entity["id"])
        if existing and existing["unfrozen_at"] is None:
            return  # already actively frozen; idempotent retry
        owner = entity["data"].get("owner") if entity["kind"] == "action_item" else None
        self.repository.freeze_entity(
            entity["id"], unit_id, entity["kind"], entity["version"], entity["status"], owner
        )
        self.audit.record(
            entity["id"],
            actor,
            "freeze",
            entity["status"],
            entity["status"],
            {"unit_id": unit_id, "reason": "unit shutdown"},
        )

    def _unfreeze(self, actor, entity, data):
        if actor.role != "safety":
            raise PermissionDenied("only safety can unfreeze frozen items")
        record = self.repository.get_freeze(entity["id"])
        if not record or record["unfrozen_at"] is not None:
            raise ValidationError("entity is not frozen: " + entity["id"])
        note = (data or {}).get("note")
        self.repository.unfreeze_entity(entity["id"], actor.user_id, note)
        self.audit.record(
            entity["id"],
            actor,
            "unfreeze",
            entity["status"],
            entity["status"],
            {"note": note},
        )
        return entity

    def _check_startup(self, actor, unit):
        """Recalculate freeze state before startup.

        Items that drifted during the shutdown (rolled-back changes,
        reopened action items, or owner-changed action items) stay frozen
        with the drift reasons listed.  Startup is blocked while any frozen
        items remain.
        """
        frozen = self.repository.list_frozen(unit_id=unit["id"], active_only=True)
        if not frozen:
            return
        items = []
        for record in frozen:
            entity = self.repository.get_entity(record["entity_id"])
            if not entity:
                continue
            drift_reasons = self._detect_drift(record, entity)
            items.append(
                {
                    "entity_id": entity["id"],
                    "kind": entity["kind"],
                    "status": entity["status"],
                    "version": entity["version"],
                    "drift_reasons": drift_reasons,
                }
            )
        raise ConflictError(
            "cannot startup: %d item(s) still frozen under unit %s" % (len(items), unit["id"]),
            details={"frozen_items": items},
        )

    def _detect_drift(self, record, entity):
        """Compare the current entity against its freeze snapshot."""
        reasons = []
        if entity["kind"] == "change":
            if entity["status"] == "rolled_back" and record["frozen_status"] != "rolled_back":
                reasons.append("change was rolled back during shutdown")
            elif entity["status"] != record["frozen_status"]:
                reasons.append(
                    "change status changed from %s to %s during shutdown"
                    % (record["frozen_status"], entity["status"])
                )
        elif entity["kind"] == "action_item":
            if entity["status"] == "open" and record["frozen_status"] != "open":
                reasons.append("action item was reopened during shutdown")
            elif entity["status"] != record["frozen_status"]:
                reasons.append(
                    "action item status changed from %s to %s during shutdown"
                    % (record["frozen_status"], entity["status"])
                )
            current_owner = entity["data"].get("owner")
            if current_owner != record["frozen_owner"]:
                reasons.append(
                    "owner changed from %s to %s during shutdown"
                    % (record["frozen_owner"], current_owner)
                )
        return reasons

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
