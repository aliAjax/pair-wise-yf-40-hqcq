from datetime import datetime, timedelta, timezone

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)

LAB_RESULTS = ("positive", "negative")


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _validate_consignment(actor, data, lookup):
    if data.get("origin") == data.get("destination"):
        raise ValidationError("origin and destination must differ")


def _validate_quarantine(actor, entity, data, lookup):
    if not data.get("pest_found"):
        raise ValidationError("pest_found must be true for quarantine")
    return {"quarantined_by": actor.user_id}


def _lab_reports_for(lookup, consignment_id):
    if lookup is None or not consignment_id:
        return []
    return lookup("lab_report", "consignment_id", consignment_id) or []


def _active_duplicate(lookup, consignment_id, sample_id, exclude_id=None):
    for report in _lab_reports_for(lookup, consignment_id):
        if exclude_id and report.get("id") == exclude_id:
            continue
        if report.get("status") not in ("submitted", "reviewed"):
            continue
        if report.get("data", {}).get("sample_id") == sample_id:
            return report
    return None


def _reviewed_report(lookup, consignment_id):
    reviewed = [
        report
        for report in _lab_reports_for(lookup, consignment_id)
        if report.get("status") == "reviewed"
    ]
    return reviewed[-1] if reviewed else None


def _duplicate_reason(consignment_id, sample_id):
    return "duplicate sample_id %s for consignment %s" % (sample_id, consignment_id)


def _validate_lab_report(actor, data, lookup):
    consignment = _find_one(lookup, "consignment", "id", data.get("consignment_id"))
    if not consignment:
        raise ValidationError("consignment not found: " + str(data.get("consignment_id")))
    if consignment.get("status") not in ("inspected", "quarantined"):
        raise ValidationError("consignment must be inspected or quarantined before lab registration")
    if data.get("result") not in LAB_RESULTS:
        raise ValidationError("result must be one of: " + ", ".join(LAB_RESULTS))
    now = _now_iso()
    history = [{
        "event": "submitted",
        "by": actor.user_id,
        "at": now,
        "sample_id": data.get("sample_id"),
        "result": data.get("result"),
    }]
    patch = {
        "tested_by": actor.user_id,
        "submitted_at": now,
        "review_invalidated": False,
        "history": history,
    }
    if _active_duplicate(lookup, data.get("consignment_id"), data.get("sample_id")):
        reason = _duplicate_reason(data.get("consignment_id"), data.get("sample_id"))
        patch["return_reason"] = reason
        history.append({"event": "returned", "by": "system", "at": now, "reason": reason})
    return patch


def _lab_report_create_status(actor, data, lookup):
    return "returned" if data.get("return_reason") else "submitted"


def _validate_lab_review(actor, entity, data, lookup):
    info = entity.get("data", {})
    now = _now_iso()
    history = list(info.get("history", []))
    tested_by = info.get("tested_by")
    if tested_by and actor.user_id == tested_by:
        reason = "reviewer must differ from tested_by: " + str(tested_by)
        history.append({"event": "returned", "by": actor.user_id, "at": now, "reason": reason})
        return {"next_status": "returned", "return_reason": reason, "history": history}
    history.append({"event": "reviewed", "by": actor.user_id, "at": now})
    return {
        "next_status": "reviewed",
        "reviewed_by": actor.user_id,
        "reviewed_at": now,
        "return_reason": None,
        "review_invalidated": False,
        "history": history,
    }


def _validate_lab_amend(actor, entity, data, lookup):
    info = entity.get("data", {})
    if not data.get("result") and not data.get("sample_id"):
        raise ValidationError("amend requires a new result or sample_id")
    new_result = data.get("result") or info.get("result")
    if new_result not in LAB_RESULTS:
        raise ValidationError("result must be one of: " + ", ".join(LAB_RESULTS))
    new_sample = data.get("sample_id") or info.get("sample_id")
    now = _now_iso()
    history = list(info.get("history", []))
    patch = {
        "result": new_result,
        "sample_id": new_sample,
        "tested_by": actor.user_id,
        "submitted_at": now,
    }
    if entity.get("status") == "reviewed":
        history.append({
            "event": "review_invalidated",
            "by": actor.user_id,
            "at": now,
            "previous_reviewed_by": info.get("reviewed_by"),
        })
        patch["reviewed_by"] = None
        patch["reviewed_at"] = None
        patch["review_invalidated"] = True
    history.append({
        "event": "amended",
        "by": actor.user_id,
        "at": now,
        "sample_id": new_sample,
        "result": new_result,
    })
    if _active_duplicate(lookup, info.get("consignment_id"), new_sample, exclude_id=entity.get("id")):
        reason = _duplicate_reason(info.get("consignment_id"), new_sample)
        history.append({"event": "returned", "by": "system", "at": now, "reason": reason})
        patch.update({"next_status": "returned", "return_reason": reason, "history": history})
        return patch
    patch.update({"next_status": "submitted", "return_reason": None, "history": history})
    return patch


def _validate_lab_resubmit(actor, entity, data, lookup):
    info = entity.get("data", {})
    now = _now_iso()
    history = list(info.get("history", []))
    if _active_duplicate(lookup, info.get("consignment_id"), info.get("sample_id"), exclude_id=entity.get("id")):
        reason = _duplicate_reason(info.get("consignment_id"), info.get("sample_id"))
        history.append({"event": "returned", "by": "system", "at": now, "reason": reason})
        return {"next_status": "returned", "return_reason": reason, "history": history}
    history.append({"event": "resubmitted", "by": actor.user_id, "at": now})
    return {"next_status": "submitted", "return_reason": None, "submitted_at": now, "history": history}


def _validate_release(actor, entity, data, lookup):
    if data.get("treatment") not in ("none", "completed", "certified"):
        raise ValidationError("release requires a valid treatment state")
    report = _reviewed_report(lookup, entity.get("id"))
    if not report:
        raise ValidationError("release requires a reviewed lab conclusion")
    result = report.get("data", {}).get("result")
    if result != "negative":
        raise ValidationError("lab conclusion is %s; release requires negative" % result)
    return {"released_by": actor.user_id, "lab_report_id": report.get("id"), "lab_result": result}


def _validate_destroy(actor, entity, data, lookup):
    report = _reviewed_report(lookup, entity.get("id"))
    if not report:
        raise ValidationError("destroy requires a reviewed lab conclusion")
    result = report.get("data", {}).get("result")
    if result != "positive":
        raise ValidationError("lab conclusion is %s; destroy requires positive" % result)
    return {"destroyed_by": actor.user_id, "lab_report_id": report.get("id"), "lab_result": result}


def trace_downstream(consignments, start_id):
    pending = [start_id]
    visited = set()
    result = []
    while pending:
        current = pending.pop(0)
        if current in visited:
            continue
        visited.add(current)
        result.append(current)
        for item in consignments:
            if item.get("parent_id") == current:
                pending.append(item.get("id"))
    return result


CUSTOM_CREATE = {'consignment': _validate_consignment, 'lab_report': _validate_lab_report}
CUSTOM_TRANSITIONS = {('consignment', 'quarantine'): _validate_quarantine, ('consignment', 'release'): _validate_release, ('consignment', 'destroy'): _validate_destroy, ('lab_report', 'review'): _validate_lab_review, ('lab_report', 'amend'): _validate_lab_amend, ('lab_report', 'resubmit'): _validate_lab_resubmit}
CREATE_STATUS = {'lab_report': _lab_report_create_status}


class RuleEngine:
    ALIASES = {'consignments': 'consignment', 'facilities': 'facility', 'lab_reports': 'lab_report'}
    INITIAL_STATUS = {'consignment': 'declared', 'facility': 'registered', 'lab_report': 'submitted'}
    TRANSITIONS = {'consignment': {'inspect': (('declared',), 'inspected'), 'quarantine': (('inspected',), 'quarantined'), 'release': (('inspected', 'quarantined'), 'released'), 'destroy': (('quarantined',), 'destroyed'), 'recheck': (('quarantined',), 'inspected')}, 'facility': {'trace': (('registered',), 'traced')}, 'lab_report': {'review': (('submitted',), None), 'amend': (('submitted', 'reviewed', 'returned'), None), 'resubmit': (('returned',), None)}}
    CREATE_REQUIRED = {'consignment': ('code', 'origin', 'destination'), 'facility': ('name', 'address'), 'lab_report': ('consignment_id', 'sample_id', 'result')}
    ACTION_REQUIRED = {('consignment', 'inspect'): ('inspector', 'inspection_result'), ('consignment', 'quarantine'): ('pest_found', 'sample_id'), ('consignment', 'release'): ('treatment',), ('consignment', 'destroy'): ('method', 'witnessed_by'), ('consignment', 'recheck'): ('sample_id',), ('facility', 'trace'): ('consignment_ids',)}
    CREATE_ROLES = {'consignment': ('admin', 'inspector'), 'facility': ('admin', 'quarantine'), 'lab_report': ('admin', 'lab')}
    ROLE_ACTIONS = {'inspect': ('admin', 'inspector'), 'quarantine': ('admin', 'quarantine'), 'release': ('admin', 'quarantine'), 'destroy': ('admin', 'quarantine'), 'recheck': ('admin', 'inspector'), 'trace': ('admin', 'quarantine'), 'review': ('admin', 'lab'), 'amend': ('admin', 'lab'), 'resubmit': ('admin', 'lab')}

    def normalize_kind(self, kind):
        return self.ALIASES.get(kind, kind)

    def initial_status(self, kind):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        return self.INITIAL_STATUS[kind]

    def create_status(self, actor, kind, data, lookup=None):
        kind = self.normalize_kind(kind)
        decider = CREATE_STATUS.get(kind)
        if decider:
            return decider(actor, data, lookup)
        return self.initial_status(kind)

    @staticmethod
    def _ensure_role(actor, allowed):
        if "*" not in allowed and actor.role not in allowed:
            raise PermissionDenied("role %s is not allowed here" % actor.role)

    @staticmethod
    def _require(data, fields):
        for field in fields:
            value = data.get(field)
            if value is None or value == "" or value == [] or value == {}:
                raise ValidationError("missing required field: " + field)

    def validate_create(self, actor, kind, data, lookup=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        self._ensure_role(actor, self.CREATE_ROLES.get(kind, ("admin",)))
        self._require(data, self.CREATE_REQUIRED.get(kind, ()))
        merged = dict(data)
        custom = CUSTOM_CREATE.get(kind)
        if custom:
            extra = custom(actor, merged, lookup)
            if extra:
                merged.update(extra)
        return merged

    def validate_transition(self, actor, entity, action, data, lookup=None):
        kind = self.normalize_kind(entity["kind"])
        transition = self.TRANSITIONS.get(kind, {}).get(action)
        if not transition:
            raise InvalidTransition("unknown action %s for %s" % (action, kind))
        allowed_statuses, next_status = transition
        if entity["status"] not in allowed_statuses:
            raise InvalidTransition(
                "cannot %s from status %s" % (action, entity["status"])
            )
        allowed_roles = self.ROLE_ACTIONS.get(
            (kind, action), self.ROLE_ACTIONS.get(action, ("admin",))
        )
        self._ensure_role(actor, allowed_roles)
        self._require(data, self.ACTION_REQUIRED.get((kind, action), ()))
        custom = CUSTOM_TRANSITIONS.get((kind, action))
        extra = custom(actor, entity, data, lookup) if custom else {}
        patch = dict(data)
        if extra:
            patch.update(extra)
        # custom validators may decide the target status via reserved key
        override = patch.pop("next_status", None)
        if override:
            next_status = override
        if not next_status:
            raise InvalidTransition("action %s for %s has no target status" % (action, kind))
        return next_status, patch


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()
