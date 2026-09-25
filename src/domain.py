from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional
class ErrorKind:
    VALIDATION="validation"; NOT_FOUND="not_found"; FORBIDDEN="forbidden"; CONFLICT="conflict"
class DomainError(Exception):
    kind=ErrorKind.VALIDATION
    def __init__(self,message): super().__init__(message); self.message=message
class ValidationError(DomainError): kind=ErrorKind.VALIDATION
class NotFoundError(DomainError): kind=ErrorKind.NOT_FOUND
class PermissionDenied(DomainError): kind=ErrorKind.FORBIDDEN
class ConflictError(DomainError): kind=ErrorKind.CONFLICT
SEVERITIES=['low', 'medium', 'high', 'critical']; STATES=['draft', 'submitted', 'inspection', 'correction', 'approved']; ROLES=['applicant', 'inspector', 'compliance_manager', 'viewer']
EQUIPMENT_KINDS=['control', 'substitute']; OUTAGE_STATES=['pending', 'confirmed', 'returned']
@dataclass(frozen=True)
class Item:
    id:int; title:str; description:str; severity:str; quantity:float; threshold:float; status:str; version:int; external_ref:Optional[str]; created_by:str; created_at:str; updated_at:str
@dataclass(frozen=True)
class Record:
    id:int; item_id:int; kind:str; detail:str; status:str; external_ref:Optional[str]; created_by:str; created_at:str
@dataclass(frozen=True)
class Equipment:
    id:int; name:str; kind:str; rated_capacity:float; status:str; external_ref:Optional[str]; created_by:str; created_at:str
@dataclass(frozen=True)
class OutageReport:
    id:int; equipment_id:int; item_id:int; start_time:str; end_time:str; resumed_at:Optional[str]; affected_quantity:float; capacity_provided:float; margin:float; status:str; version:int; reason:str; review_note:Optional[str]; created_by:str; created_at:str; updated_at:str
@dataclass(frozen=True)
class AuditEntry:
    id:int; action:str; entity_type:str; entity_id:int; actor:str; detail:Dict[str,Any]; previous_hash:str; entry_hash:str; created_at:str
def require_text(value,field,max_length=2000):
    if not isinstance(value,str) or not value.strip(): raise ValidationError(f"{field}不能为空")
    value=value.strip()
    if len(value)>max_length: raise ValidationError(f"{field}不能超过{max_length}个字符")
    return value
def normalize_severity(value):
    if value not in SEVERITIES: raise ValidationError("severity不在允许范围内")
    return value
def require_number(value,field,minimum=0.0):
    if isinstance(value,bool): raise ValidationError(f"{field}必须是数字")
    try: number=float(value)
    except (TypeError,ValueError): raise ValidationError(f"{field}必须是数字")
    if number<minimum: raise ValidationError(f"{field}不能小于{minimum}")
    return number
def require_int(value,field,minimum=1):
    if isinstance(value,bool) or not isinstance(value,int): raise ValidationError(f"{field}必须是整数")
    if value<minimum: raise ValidationError(f"{field}不能小于{minimum}")
    return value
def normalize_equipment_kind(value):
    if value not in EQUIPMENT_KINDS: raise ValidationError("kind不在允许范围内")
    return value
def require_code(value,field,max_length=50):
    value=require_text(value,field,max_length)
    return value
def parse_iso(value,field):
    if not isinstance(value,str) or not value.strip(): raise ValidationError(f"{field}必须是ISO时间字符串")
    text=value.strip()
    if text.endswith(('Z','z')): text=text[:-1]+'+00:00'
    try: dt=datetime.fromisoformat(text)
    except ValueError: raise ValidationError(f"{field}必须是ISO时间字符串")
    if dt.tzinfo is None: dt=dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)
def parse_window(start,end):
    start_dt=parse_iso(start,"start_time"); end_dt=parse_iso(end,"end_time")
    if end_dt<=start_dt: raise ValidationError("end_time必须晚于start_time")
    return start_dt,end_dt
def ensure_role(role,allowed):
    if role not in allowed: raise PermissionDenied("当前角色无权执行该操作")
