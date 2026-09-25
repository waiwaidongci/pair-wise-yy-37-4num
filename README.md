# 空气污染源许可与合规检查

管理设施、排放口、治理设备、现场检查、整改和许可续期。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限和关闭不变量。
- `src/repository.py`：SQLite建表、事务、版本控制和审计链。
- `src/service.py`：权限检查、用例编排、并发控制和审计。
- `src/http_api.py`：JSON路由和统一错误响应。
- `src/audit.py`：UTC时间和SHA-256审计事件。
- `static/index.html`：最小演示页。
- `tests/`：完整流程、规则和失败测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8314
```

默认端口为`8314`，首次启动自动建库。使用`X-Actor`和`X-Role`请求头传递身份。

## 主要接口

- `GET /health`
- `GET /api/items`
- `POST /api/items`
- `GET /api/items/{id}`
- `POST /api/items/{id}/records`
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `GET /api/audit`

允许角色：applicant, inspector, compliance_manager, viewer。申报量超过许可量或检查发现高严重度问题时提高优先级；存在未关闭整改时不能批准。

## 停运报备

治理设备停运必须先报备，不再依赖现场口头安排。报备登记治理设备、停运时段、受影响排放口（按申报量汇总）、替代装置及其能力。

- `POST /api/shutdown-reports`（applicant/inspector）：登记报备，初始为`pending`。同一治理设备或同一替代装置在重叠时段只保留一笔待审/已确认报备，重复登记返回409；已退回（`returned`）不占槽位。半开区间比较，时段首尾相接不算重叠。
- `POST /api/shutdown-reports/{id}/review`（compliance_manager）：按受影响申报量核算`capacity_margin = 替代能力 - 受影响申报量`，余量不足自动退回（`returned`）并记录退回原因，足够则确认（`confirmed`）。必须提交`expected_version`。
- `POST /api/shutdown-reports/{id}/amend`（applicant/inspector）：停运延期、提前复产、替代能力或申报量变化均通过修订重算，报备回到`pending`重新审核，并写入审计链。
- `GET /api/shutdown-reports?status=&item_id=`、`GET /api/shutdown-reports/{id}`：查看报备与判定结果。

许可单进入`inspection`（复查）前，其关联的停运报备必须全部已确认，否则转换被拒绝。报备数据由仓储层承担，能力/时段冲突判定由规则层承担，请求由独立路由承担。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
