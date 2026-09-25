from __future__ import annotations
from .domain import ConflictError, ValidationError
TITLE='空气污染源许可与合规检查'; ENTITY='排污许可'; ID_PREFIX='AQ'; SHUTDOWN_ENTITY='停运报备'
SEVERITIES=['low', 'medium', 'high', 'critical']; STATES=['draft', 'submitted', 'inspection', 'correction', 'approved']; TRANSITIONS={'draft': ['submitted'], 'submitted': ['inspection'], 'inspection': ['correction'], 'correction': ['approved'], 'approved': []}; TRANSITION_ROLES={'submitted': ['applicant'], 'inspection': ['inspector'], 'correction': ['inspector'], 'approved': ['compliance_manager']}
CREATE_ROLES=set(['applicant']); RECORD_ROLES=set(['applicant', 'inspector']); AUDIT_ROLES=set(['compliance_manager', 'viewer']); VIEW_ROLES=set(['applicant', 'inspector', 'compliance_manager', 'viewer'])
SHUTDOWN_STATUSES=['pending', 'confirmed', 'returned']; SHUTDOWN_CREATE_ROLES=set(['applicant', 'inspector']); SHUTDOWN_REVIEW_ROLES=set(['compliance_manager']); SHUTDOWN_AMEND_ROLES=set(['applicant', 'inspector']); SHUTDOWN_VIEW_ROLES=VIEW_ROLES
ACTIVE_SHUTDOWN_STATUSES=set(['pending', 'confirmed'])
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
def periods_overlap(start_a,end_a,start_b,end_b): return start_a < end_b and start_b < end_a
def same_resource(a,b): return bool(a) and a == b
def shutdown_conflicts(reports,equipment_id,backup_device_id,period_start,period_end,exclude_id=None):
    conflicts=[]
    for report in reports:
        if exclude_id is not None and report["id"] == exclude_id: continue
        if report["status"] not in ACTIVE_SHUTDOWN_STATUSES: continue
        if not periods_overlap(period_start,period_end,report["period_start"],report["period_end"]): continue
        same_equipment=same_resource(equipment_id,report["equipment_id"])
        same_backup=backup_device_id is not None and same_resource(backup_device_id,report["backup_device_id"])
        if same_equipment or same_backup: conflicts.append({"id":report["id"],"status":report["status"],"reason":"equipment" if same_equipment else "backup_device"})
    return conflicts
def capacity_margin(backup_capacity,affected_quantity): return round(float(backup_capacity)-float(affected_quantity),6)
def capacity_sufficient(backup_capacity,affected_quantity): return capacity_margin(backup_capacity,affected_quantity) >= 0
def inspection_blockers(item_id,unconfirmed_reports):
    if not unconfirmed_reports: return []
    return [f"许可{item_id}关联的停运报备{','.join(str(r) for r in unconfirmed_reports)}尚未确认，不能进入复查"]
