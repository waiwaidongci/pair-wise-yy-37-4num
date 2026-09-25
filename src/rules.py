from __future__ import annotations
from .domain import ConflictError, ValidationError
TITLE='空气污染源许可与合规检查'; ENTITY='排污许可'; ID_PREFIX='AQ'
EQUIPMENT_ENTITY='治理设备'; OUTAGE_ENTITY='停运报备'
SEVERITIES=['low', 'medium', 'high', 'critical']; STATES=['draft', 'submitted', 'inspection', 'correction', 'approved']; TRANSITIONS={'draft': ['submitted'], 'submitted': ['inspection'], 'inspection': ['correction'], 'correction': ['approved'], 'approved': []}; TRANSITION_ROLES={'submitted': ['applicant'], 'inspection': ['inspector'], 'correction': ['inspector'], 'approved': ['compliance_manager']}
CREATE_ROLES=set(['applicant']); RECORD_ROLES=set(['applicant', 'inspector']); AUDIT_ROLES=set(['compliance_manager', 'viewer']); VIEW_ROLES=set(['applicant', 'inspector', 'compliance_manager', 'viewer'])
EQUIPMENT_ROLES=set(['applicant', 'inspector', 'compliance_manager'])
OUTAGE_CREATE_ROLES=set(['applicant', 'inspector'])
OUTAGE_REVIEW_ROLES=set(['compliance_manager'])
SEVERITY_WEIGHT={'low': 1.0, 'medium': 3.0, 'high': 6.0, 'critical': 9.0}; DEADLINE_HOURS={'low': 72, 'medium': 24, 'high': 8, 'critical': 4}; TERMINAL_STATES=set(['approved'])
def priority_score(severity,quantity=0.0,threshold=1.0,open_records=0):
    if severity not in SEVERITY_WEIGHT: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(0,min(10,int(round(SEVERITY_WEIGHT[severity]+min(4.0,ratio*4.0)+min(3.0,float(open_records))))))
def response_deadline_hours(severity,quantity=0.0,threshold=1.0):
    if severity not in DEADLINE_HOURS: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(1,int(DEADLINE_HOURS[severity]/max(1.0,ratio)))
def escalation_required(severity,quantity=0.0,threshold=1.0):
    return severity==SEVERITIES[-1] or (threshold>0 and quantity>=threshold)
def can_transition(current,target): return target in TRANSITIONS.get(current,[])
def validate_transition(current,target):
    if current not in STATES or target not in STATES: raise ValidationError("未知状态")
    if not can_transition(current,target): raise ConflictError(f"不能从{current}转换到{target}")
def completion_blockers(target,open_records): return ["仍有未关闭事项"] if target in TERMINAL_STATES and open_records>0 else []
def role_for_transition(target): return set(TRANSITION_ROLES.get(target,[]))

def windows_overlap(start_a, end_a, start_b, end_b):
    """半开区间[start,end)重叠判定：首尾相接（a结束=b开始）不算重叠。"""
    return start_a < end_b and start_b < end_a

def outage_conflict(equipment_id, substitute_ids, start, end, candidates):
    """从待审报备候选中找出资源与时段都重叠的记录，返回冲突描述列表。

    主设备相同，或替代装置集合有交集，且停运时段重叠，即构成冲突。
    candidates: 由repository提供的待审报备，含 id/equipment_id/substitutes。
    """
    substitutes = set(substitute_ids)
    conflicts = []
    for candidate in candidates:
        if not windows_overlap(start, end, candidate["window_start"], candidate["window_end"]):
            continue
        shared = []
        if candidate["equipment_id"] == equipment_id:
            shared.append(f"治理设备#{equipment_id}")
        overlap_ids = substitutes.intersection(candidate.get("substitute_ids", ()))
        shared.extend(f"替代装置#{sid}" for sid in sorted(overlap_ids))
        if shared:
            conflicts.append({
                "outage_id": candidate["id"],
                "resources": shared,
                "start_time": candidate["window_start"].isoformat(),
                "end_time": candidate["window_end"].isoformat(),
            })
    return conflicts

def capacity_margin(affected_quantity, substitutes):
    """替代能力余量 = 替代装置能力之和 - 受影响申报量。"""
    provided = sum(float(item["rated_capacity"]) for item in substitutes)
    return provided, provided - float(affected_quantity)

def capacity_sufficient(margin):
    return margin >= 0.0

def review_blockers(margin):
    return ["替代装置能力余量不足"] if not capacity_sufficient(margin) else []

def unconfirmed_outage_blockers(count):
    """报备确认后许可单才进复查：存在未确认（待审/退回）停运报备即阻断。"""
    return ["存在未确认的停运报备，许可单暂不能进入复查"] if count > 0 else []
