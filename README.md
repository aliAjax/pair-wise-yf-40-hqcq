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
- `static/index.html`：实验室结论台演示页面。
- `tests/`：完整流程、规则和失败场景测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8306
```

服务启动时会自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。

## 核心对象

- `consignment`：检疫批次；`facility`：温室、苗圃或下游种植点。
- `lab_report`：实验室结论，按批次登记样本编号、检测人和检测结果。

## 实验室结论台

- 创建`lab_report`需批次处于`inspected`或`quarantined`状态，必填`consignment_id`、`sample_id`、`result`（`positive`/`negative`）；检测人自动记录为提交人。
- 提交后状态为`submitted`，须由另一名实验员（`lab`角色）执行`review`，生效后为`reviewed`。
- 同批次样本编号重复、或复核人与检测人为同一人时，记录转为`returned`并写入`return_reason`；可用`amend`修改结果或编号、`resubmit`重新提交。
- `release`要求该批次存在`reviewed`且结果为`negative`的结论；`destroy`要求`reviewed`且结果为`positive`的结论，结论编号会写入批次数据作为依据。
- 复核生效后再用`amend`修改检测结果，原复核失效（状态回到`submitted`，需重新复核后才能放行或销毁）。
- 每次提交、退回、复核、修改都写入`data.history`和审计日志，首页（实验室结论台）可查看。

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
