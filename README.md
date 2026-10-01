# 建筑抗震鉴定与加固排序

依据结构、用途、人员密度和历史缺陷生成鉴定与加固优先级。

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
python3 app.py --db ./data.db --port 8317
```

默认端口为`8317`，首次启动自动建库。使用`X-Actor`和`X-Role`请求头传递身份。

## 主要接口

- `GET /health`
- `GET /api/items`
- `POST /api/items`
- `GET /api/items/{id}`
- `POST /api/items/{id}/records`
- `POST /api/items/{id}/records/{record_id}/close`
- `POST /api/items/{id}/reassess`，必须提交`expected_version`
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `GET /api/schedule`
- `POST /api/schedule/batches`，必须提交`batch_no`、`expected_schedule_version`和`lines`
- `GET /api/work-orders`、`GET /api/work-orders/{id}`
- `POST /api/work-orders/{id}/start`
- `POST /api/work-orders/{id}/logs`
- `GET /api/audit`

允许角色：assessor, structural_engineer, review_board, viewer, dispatcher。风险分值和人员密度共同影响排序；审核通过前必须完成评估、设计和施工证据登记。

## 加固工单

调度员（dispatcher）按批次下发加固工单，每行可以是`issue`（按项目下发新工单并指派队伍）或`reassign`（为未开工工单改派队伍）。工单落单时快照排程依据：严重程度、未关闭记录数、优先级和响应期限。

- **风险变化重排**：鉴定项目严重程度变更（`reassess`）或未关闭记录增减（补录/关闭记录）时，未开工工单的排程依据立即失效并按当前风险重算（`basis_version`递增）；已开工工单保留原队伍和现场记录，不被改动。
- **批次并发**：批次提交携带`expected_schedule_version`，版本不一致返回409，后到者需读取`GET /api/schedule`按当前版本重新确认；重新提交时指向已开工或历史工单的改派行会被跳过（`skipped`），不覆盖已开工内容。
- **失败重试**：批次写入整体事务化，失败后按原`batch_no`重试；同批次号同内容重放返回原结果（`replayed: true`），已落下的工单不会重复；同批次号不同内容返回409。
- **旧数据升级**：启动时自动为升级前已存在的项目按当前风险回填排程依据，生成`LEGACY-MIGRATION`批次的历史工单（`status=historical`），不参与后续重排；迁移幂等，重复启动不重复回填。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
