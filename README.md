# 空气污染源许可与合规检查

管理设施、排放口、治理设备、现场检查、整改、许可续期和治理设备停运报备。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限、关闭不变量，以及停运时段重叠、替代能力余量和复查闸门判定（纯函数）。
- `src/repository.py`：SQLite建表、事务、版本控制和审计链；承担报备数据存储。
- `src/service.py`：权限检查、用例编排、并发控制和审计；承担能力判定与余量核算。
- `src/http_api.py`：JSON路由和统一错误响应；承担请求入口。
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

治理设备停运必须先报备，登记**治理设备、停运时段（start_time/end_time）、受影响排放口、替代装置及能力快照、受影响申报量**。

规则：

- 同一治理设备，或同一替代装置，在重叠时段只允许存在一笔`pending`待审报备（半开区间，首尾相接不算重叠），重复报备返回409。
- 合规管理员按「替代装置能力之和 − 受影响申报量」核算余量；余量不足不能确认，只能`return`退回；退回的报备仍会阻断复查。
- 许可单只有在关联停运报备全部`confirmed`后才能从`submitted`进入`inspection`复查。
- 停运延期、提前复产（resumed_at落在原时段内，生效结束时间提前）、申报量变化都会重算余量、重置为待审重新确认，并写入审计链留档；版本号递增，需提交`expected_version`。

接口：

- `POST /api/equipment`，`GET /api/equipment?kind=control|substitute`，`GET /api/equipment/{id}`：设备台账（applicant/inspector/compliance_manager可登记，其余只读）。
- `POST /api/outages`：停运报备（applicant/inspector）。
- `POST /api/outages/{id}/review`：管理员确认或退回（compliance_manager），提交`decision`和`expected_version`。
- `POST /api/outages/{id}/extend`：停运延期，提交`new_end_time`。
- `POST /api/outages/{id}/resume`：提前复产，提交`resumed_at`。
- `POST /api/outages/{id}/quantity`：受影响申报量变化，提交新的`affected_quantity`。
- `GET /api/outages?status=&item_id=&equipment_id=`，`GET /api/outages/{id}`：报备查询。

报备数据存储、能力判定、请求入口分别由repository、service(rules)、http_api承担。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
