# 化工装置变更与工艺安全管理

这是一个只使用Python标准库和SQLite的模块化项目，默认端口为`8310`。所有业务规则集中在`src/rules.py`，`app.py`只负责组装依赖和启动服务。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、数据结构、领域异常和基础校验。
- `src/rules.py`：状态机、权限、领域计算、冲突和跨对象校验。
- `src/repository.py`：SQLite建表、查询、事务和乐观锁。
- `src/service.py`：用例编排、幂等处理、版本控制和审计写入。
- `src/http_api.py`：HTTP路由、请求解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则和失败场景测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8310
```

服务启动时会自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。

## 核心对象

- `unit`：装置运行状态；`change`：变更申请；`action_item`：风险控制行动项。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。
动作请求可携带`Idempotency-Key`请求头；同一用户用相同键重试相同请求只执行一次（断网重试安全），记录持久化在SQLite中，服务重启后仍然有效。

## 停车冻结与恢复复核

装置临时停车（`unit`执行`shutdown`）后，该装置下所有在途变更（未`closed`）及其行动项被
**冻结覆盖层**一并冻结（对象自身状态不变）：

- 冻结期间工程师/复核人提交`implement`、`commission`、`complete`、`verify`等动作会被拒绝
  （HTTP 409，`FreezeBlocked`），响应中带`entity`（最新版本）和`conflicts`（该装置下仍冻结
  的全部对象及冻结原因）。
- 只有`safety`（或`admin`）清点后可以对**单个**对象执行`release`解除冻结，
  必须提供`inventory_note`；解除一项只放一项。
- 停车期间创建的变更/行动项自动带冻结（`born_frozen`）。
- `startup`前按停车期间捕获的版本快照逐项重算：
  - 变更被`rollback`、行动项被`reopen`、或行动项负责人（`owner`，通过`assign`动作修改）
    变更过的，**保持冻结**并在审计与响应中给出`reblock_reason`，需safety再次解除；
  - 其余对象自动恢复（审计记录`resume`）。响应包含`resumed`和`still_frozen`两个列表。
- `shutdown`/`startup`的冻结、审计、幂等记录在同一个SQLite事务中提交。

行动项负责人可用`assign`动作修改（数据需含`owner`），状态不变、版本递增。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

风险分级和投产规则用于流程演示，不替代HAZOP、LOPA、法定许可和现场安全审查。
