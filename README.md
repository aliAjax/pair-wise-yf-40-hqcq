# 植物病虫害检疫与传播追溯

这是一个只使用Python标准库和SQLite的模块化项目，默认端口为`8306`。所有业务规则集中在`src/rules.py`，`app.py`只负责组装依赖和启动服务。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、数据结构、领域异常和基础校验。
- `src/rules.py`：状态机、权限、领域计算、冲突和跨对象校验。
- `src/repository.py`：SQLite建表、查询、事务和乐观锁。
- `src/service.py`：用例编排、幂等处理、版本控制和审计写入。
- `src/http_api.py`：HTTP路由、请求解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `static/index.html`：实验室结论台页面（批次处置、结论登记、复核、改判与审计时间线）。
- `tests/`：完整流程、规则和失败场景测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8306
```

服务启动时会自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。

## 核心对象

- `consignment`：检疫批次；`facility`：温室、苗圃或下游种植点。
- `lab_conclusion`：实验室结论台记录，按隔离批次登记，状态为 `pending`（待复核）或 `approved`（复核生效）。

## 实验室结论台流程

1. 批次初检后由隔离区角色 `quarantine` 转入隔离（`quarantine`）。
2. 实验员角色 `lab` 按批次提交结论：`POST /api/lab_conclusions`，字段 `consignment_id`、`sample_id`、`tester`、`result`（`negative` / `positive`）。
   - 样本编号重复：返回 409 并指明已占用该编号的结论；同一批次已有未完结结论时同样退回。
3. 由**另一名实验员**执行 `review`：复核人不得与登记的 `tester` 为同一人，否则 403 退回。
4. 隔离后的 `release` 与 `destroy` 只认 `approved` 结论：阴性才能放行，阳性才能销毁，批次数据写入 `basis_conclusion_id` / `basis_sample_id` / `basis_result` 作为依据。
5. 生效后用 `amend_result` 改检测结果：原复核存档进 `review_history`，`reviewed_by` 清空、结论退回 `pending`、`revision` 加一，须重新复核；批次已放行/销毁后禁止改判。
6. 每次登记、复核、改判与批次处置都写入 `/api/audit`，页面底部展示完整时间线。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

植物检疫结论和传播链规则是流程演示，不替代法定检疫标准或实验室鉴定。
