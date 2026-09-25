from datetime import datetime, timedelta

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)


LAB_RESULT_NEGATIVE = "negative"
LAB_RESULT_POSITIVE = "positive"
LAB_RESULTS = (LAB_RESULT_NEGATIVE, LAB_RESULT_POSITIVE)

ACTIVE_CONCLUSION_STATUSES = ("pending", "approved")


def _validate_consignment(actor, data, lookup):
    if data.get("origin") == data.get("destination"):
        raise ValidationError("origin and destination must differ")


def _validate_quarantine(actor, entity, data, lookup):
    if not data.get("pest_found"):
        raise ValidationError("pest_found must be true for quarantine")
    return {"quarantined_by": actor.user_id}


def _find_consignment(lookup, consignment_id):
    if lookup is None:
        return None
    rows = lookup("consignment", "id", consignment_id) or []
    return rows[0] if rows else None


def _active_conclusions(lookup, consignment_id):
    if lookup is None:
        return []
    return [
        item
        for item in lookup("lab_conclusion", "consignment_id", consignment_id) or []
        if item["status"] in ACTIVE_CONCLUSION_STATUSES
    ]


def _effective_conclusion(lookup, consignment_id):
    """隔离批次唯一可作依据的结论：状态 approved 的生效结论。"""
    approved = [
        item
        for item in _active_conclusions(lookup, consignment_id)
        if item["status"] == "approved"
    ]
    if not approved:
        return None
    return sorted(
        approved, key=lambda item: (item["version"], item["id"])
    )[-1]


def _validate_release(actor, entity, data, lookup):
    # 非隔离路径（初检后直接放行）沿用原有判定。
    if entity["status"] != "quarantined":
        if data.get("pest_found"):
            raise ValidationError("pest-positive consignment cannot be released")
        if data.get("treatment") not in ("none", "completed", "certified"):
            raise ValidationError("release requires a valid treatment state")
        return {"released_by": actor.user_id}
    # 隔离后放行必须有已复核生效、结果为阴性的实验室结论作为依据。
    conclusion = _effective_conclusion(lookup, entity["id"])
    if not conclusion:
        raise ValidationError(
            "隔离批次放行需要已复核生效的实验室结论，当前没有可依据的结论"
        )
    if conclusion["data"].get("result") != LAB_RESULT_NEGATIVE:
        raise ValidationError(
            "实验室结论为阳性（检出有害生物），不能放行，应按结论销毁"
        )
    return {
        "released_by": actor.user_id,
        "basis_conclusion_id": conclusion["id"],
        "basis_sample_id": conclusion["data"].get("sample_id"),
        "basis_result": conclusion["data"].get("result"),
    }


def _validate_destroy(actor, entity, data, lookup):
    if entity["status"] == "quarantined":
        # 隔离后销毁同样必须引用已复核生效的实验室结论。
        conclusion = _effective_conclusion(lookup, entity["id"])
        if not conclusion:
            raise ValidationError(
                "销毁隔离批次需要已复核生效的实验室结论作为依据，当前没有可依据的结论"
            )
        if conclusion["data"].get("result") != LAB_RESULT_POSITIVE:
            raise ValidationError(
                "实验室结论为阴性（未检出），不能按阳性结论销毁"
            )
        basis = {
            "basis_conclusion_id": conclusion["id"],
            "basis_sample_id": conclusion["data"].get("sample_id"),
            "basis_result": conclusion["data"].get("result"),
        }
    else:
        basis = {}
    basis["destroyed_by"] = actor.user_id
    return basis


def _validate_lab_conclusion(actor, data, lookup):
    consignment_id = data.get("consignment_id")
    consignment = _find_consignment(lookup, consignment_id)
    if not consignment:
        raise ValidationError("consignment_id 对应的检疫批次不存在")
    if consignment["status"] != "quarantined":
        raise ValidationError(
            "只有隔离区中的批次才能登记实验室结论，当前批次状态为 "
            + consignment["status"]
        )
    result = data.get("result")
    if result not in LAB_RESULTS:
        raise ValidationError("result 必须为 negative 或 positive")
    sample_id = data.get("sample_id")
    duplicates = lookup("lab_conclusion", "sample_id", sample_id) or []
    if duplicates:
        other = duplicates[0]
        raise ConflictError(
            "样本编号 %s 已登记在结论 %s（批次 %s），不能重复登记"
            % (sample_id, other["id"], other["data"].get("consignment_id"))
        )
    active = _active_conclusions(lookup, consignment_id)
    if active:
        raise ConflictError(
            "该批次已有未完结的实验室结论 %s（%s），请复核或改判，不要重复登记"
            % (active[0]["id"], active[0]["status"])
        )
    return {
        "registered_by": actor.user_id,
        "revision": 1,
        "reviewed_by": None,
        "effective_revision": None,
        "review_history": [],
    }


def _validate_lab_review(actor, entity, data, lookup):
    tester = entity["data"].get("tester")
    if tester == actor.user_id:
        raise PermissionDenied(
            "复核人（%s）与检测人（%s）为同一人，结论退回：必须由另一名实验员复核"
            % (actor.user_id, tester)
        )
    return {
        "reviewed_by": actor.user_id,
        "effective_revision": entity["data"].get("revision", 1),
    }


def _validate_lab_amend(actor, entity, data, lookup):
    consignment = _find_consignment(lookup, entity["data"].get("consignment_id"))
    if consignment and consignment["status"] in ("released", "destroyed"):
        raise InvalidTransition(
            "批次已%s，结论已作为处置依据，不能再改判"
            % ("放行" if consignment["status"] == "released" else "销毁")
        )
    new_result = data.get("result")
    if new_result not in LAB_RESULTS:
        raise ValidationError("result 必须为 negative 或 positive")
    if new_result == entity["data"].get("result"):
        raise ValidationError("改判结果与当前结果相同，无需修改")
    note = data.get("note", "")
    patch = {"result": new_result}
    if entity["status"] == "approved":
        # 检测结果改动使原复核失效：存档原复核，结论退回待复核，版本号 +1。
        history = list(entity["data"].get("review_history", []))
        history.append(
            {
                "result": entity["data"].get("result"),
                "reviewed_by": entity["data"].get("reviewed_by"),
                "revision": entity["data"].get("revision", 1),
                "note": note,
            }
        )
        patch.update(
            {
                "revision": int(entity["data"].get("revision", 1)) + 1,
                "reviewed_by": None,
                "effective_revision": None,
                "review_history": history,
            }
        )
        return "pending", patch
    return patch


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


CUSTOM_CREATE = {
    'consignment': _validate_consignment,
    'lab_conclusion': _validate_lab_conclusion,
}
CUSTOM_TRANSITIONS = {
    ('consignment', 'quarantine'): _validate_quarantine,
    ('consignment', 'release'): _validate_release,
    ('consignment', 'destroy'): _validate_destroy,
    ('lab_conclusion', 'review'): _validate_lab_review,
    ('lab_conclusion', 'amend_result'): _validate_lab_amend,
}


class RuleEngine:
    ALIASES = {
        'consignments': 'consignment',
        'facilities': 'facility',
        'lab_conclusions': 'lab_conclusion',
    }
    INITIAL_STATUS = {
        'consignment': 'declared',
        'facility': 'registered',
        'lab_conclusion': 'pending',
    }
    TRANSITIONS = {
        'consignment': {
            'inspect': (('declared',), 'inspected'),
            'quarantine': (('inspected',), 'quarantined'),
            'release': (('inspected', 'quarantined'), 'released'),
            'destroy': (('quarantined',), 'destroyed'),
            'recheck': (('quarantined',), 'inspected'),
        },
        'facility': {'trace': (('registered',), 'traced')},
        'lab_conclusion': {
            # review 的下一状态固定为 approved；amend_result 在自定义校验中动态决定。
            'review': (('pending',), 'approved'),
            'amend_result': (('pending', 'approved'), 'pending'),
        },
    }
    CREATE_REQUIRED = {
        'consignment': ('code', 'origin', 'destination'),
        'facility': ('name', 'address'),
        'lab_conclusion': ('consignment_id', 'sample_id', 'tester', 'result'),
    }
    ACTION_REQUIRED = {
        ('consignment', 'inspect'): ('inspector', 'inspection_result'),
        ('consignment', 'quarantine'): ('pest_found', 'sample_id'),
        ('consignment', 'release'): ('pest_found', 'treatment'),
        ('consignment', 'destroy'): ('method', 'witnessed_by'),
        ('consignment', 'recheck'): ('sample_id',),
        ('facility', 'trace'): ('consignment_ids',),
        ('lab_conclusion', 'amend_result'): ('result',),
    }
    CREATE_ROLES = {
        'consignment': ('admin', 'inspector'),
        'facility': ('admin', 'quarantine'),
        'lab_conclusion': ('admin', 'lab'),
    }
    ROLE_ACTIONS = {
        'inspect': ('admin', 'inspector'),
        'quarantine': ('admin', 'quarantine'),
        'release': ('admin', 'quarantine'),
        'destroy': ('admin', 'quarantine'),
        'recheck': ('admin', 'inspector'),
        'trace': ('admin', 'quarantine'),
        ('lab_conclusion', 'review'): ('admin', 'lab'),
        ('lab_conclusion', 'amend_result'): ('admin', 'lab'),
    }

    def normalize_kind(self, kind):
        return self.ALIASES.get(kind, kind)

    def initial_status(self, kind):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        return self.INITIAL_STATUS[kind]

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
        custom = CUSTOM_CREATE.get(kind)
        extra = custom(actor, data, lookup) if custom else {}
        patch = dict(data)
        if extra:
            patch.update(extra)
        return patch

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
        # 自定义校验可以返回 (next_status, patch) 来动态决定目标状态
        # （例如改判生效结论时退回 pending），否则只返回补写字段。
        if isinstance(extra, tuple):
            next_status, custom_patch = extra
        else:
            custom_patch = extra
        patch = dict(data)
        if custom_patch:
            patch.update(custom_patch)
        return next_status, patch


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()
