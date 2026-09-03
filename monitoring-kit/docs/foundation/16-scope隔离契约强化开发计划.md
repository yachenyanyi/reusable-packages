# monitoring-kit scope 隔离契约强化开发计划

对应 GitHub Issue：[#4 建立 scope_key 全链路隔离不变量与契约测试](https://github.com/yachenyanyi/reusable-packages/issues/4)。

## 1. 任务与边界

把 `scope_key` 从“多数对象已有的字段”提升为可自动验证的核心隔离不变量，使共享部署不能因为某个查询或更新遗漏 scope 条件而发生跨范围读写。

这里的 scope 是不透明命名空间，不是 Tenant、Workspace、用户或套餐模型。宿主仍负责身份认证和授权；`monitoring-kit` 只保证已经传入的 scope 不会与其它 scope 的数据混用。

## 2. 当前基线

- `RunRequest`、Observation、Document、Snapshot 和 ChangeEvent 均携带 scope。
- Run、ingest 和 Subject 的幂等/身份索引均按 scope 隔离；关系型适配器同时保存 scope 哈希和原始 scope 投影。
- HistoryStore 与 RunStateStore 的资源读取、取消和保存均显式带 scope；错误 scope 对普通调用方按不存在处理。
- Worker 的 `allocate()`、`list_incomplete()` 等跨 scope 操作被视为可信运行时能力，不作为宿主普通资源 API 暴露。
- memory、SQLite 和真实 MySQL 均运行共享隔离契约测试；MySQL 测试使用显式配置的测试数据库并在测试前后清理业务数据。

首轮实现已经收紧了隔离边界，并已通过真实 MySQL 的串读、串写、串取消、幂等和分页游标回归；接入其它部署环境时仍应保留攻击式隔离测试。

## 3. 复杂度目标

- 让面向某个 scope 的读取和修改在端口层就难以误用。
- 明确区分“租户范围操作”和“可信 Worker 的全局运行操作”。
- 把 scope 谓词、唯一约束和不可变校验压入 Store，避免多处补丁式检查。
- 不引入 RBAC、用户表、套餐表或通用授权框架。
- 不为了形式统一把所有技术 ID 机械改成复合主键。

## 4. 方案比较

### 方案 A：继续由 CollectionEngine 读取后检查

优点是接口改动少。缺点是每增加一个查询、取消或恢复入口，都必须记住再次检查；数据库也会先读取错误 scope 的数据。隔离正确性依赖分散的调用顺序。

### 方案 B：scope 下沉到有范围的 Store 操作

所有面向调用方的读取和修改在 Store 查询条件中同时使用 `scope_key + resource_id`；Worker 的跨 scope 领取和恢复操作单独标明为系统内部能力。业务唯一性约束包含 scope，Store 同时校验载荷中的 scope 不可改变。

选择方案 B。它把隔离复杂度放进真正拥有数据选择与更新知识的模块，同时保留 Worker 跨 scope 工作的合法路径。

## 5. 接口设计

### RunStateStore

有范围的操作调整为表达完整意图：

```text
get(scope_key, run_id)
find_by_idempotency(scope_key, idempotency_key)
request_cancel(scope_key, run_id)       # 如果以后从 save 中拆出
```

`save(record)` 可以继续从 RunRecord 获取 scope，但更新语句必须同时校验 `run_id`、scope 和 `state_version`，且不能修改已有记录的 scope。

以下是可信运行时能力，可以跨 scope，但不能作为宿主的普通查询 API 暴露：

```text
allocate(...)
list_incomplete()
```

### HistoryStore

现有读取操作已经显式携带 scope。强化重点是：

- `commit(HistoryWrite)` 验证 Observation、Document、Snapshot、ChangeEvent 的 scope 完全一致；
- 对 `document_id` 的读取同时应用 scope 条件；
- 幂等重放不能从另一个 scope 返回结果；
- 任何基于哈希的快速查询命中后，都必须再核对原始 scope 和业务键。

### 标识与数据库约束

- `(scope_key, idempotency_key)`、`(scope_key, ingest_key)`、`(scope_key, SubjectRef)` 属于业务唯一性，必须包含 scope。
- `run_id`、`snapshot_id`、`event_id` 等随机技术 ID 可以继续使用全局主键，但所有有范围的读取与更新仍必须附带 scope 谓词。
- 不为了满足“所有键都复合化”的表面规则重写全部外键；只有真实隔离不变量和查询路径决定索引。
- scope 一经创建不可变；不支持通过修改字段完成跨 scope 搬迁。

## 6. 分阶段实施

### 阶段 1：建立共享隔离契约套件

先编写可同时运行于 memory、SQLite 和 MySQL Store 的测试：

- 两个 scope 使用相同幂等键，分别创建不同 Run。
- 两个 scope 使用相同 SubjectRef，分别创建不同 Document。
- scope A 使用 scope B 的 run/document/event ID 查询时返回不存在。
- scope A 不能取消或覆盖 scope B 的 Run。
- 同一 ingest key 在不同 scope 独立幂等，在同一 scope 内只提交一次。
- 伪造 scope 不一致的 HistoryWrite 必须在写入前失败。
- 哈希索引命中后，原始键不一致必须按持久化不变量损坏处理。

### 阶段 2：收紧 Run 端口

- 已将 `RunStateStore.get(run_id)` 收紧为 scope-aware 查询。
- 修改 CollectionEngine 的获取、取消和恢复边界，删除重复的事后检查或将其保留为防御性断言。
- `save` 和 CAS 条件加入 scope，拒绝修改 scope 的旧副本。
- 同步内存和关系型适配器，不新增第二套 Store。

### 阶段 3：审计 History 全链路

- 逐一检查 ingest、Document、Snapshot、ChangeEvent 的读取、写入、唯一约束和索引。
- 收敛跨对象 scope 一致性校验，避免各适配器复制不同规则。
- 检查分页游标不能跨 scope 复用后读出其它范围的数据。

### 阶段 4：关系型 schema 与迁移

- 根据查询计划确认现有 `scope_hash` 索引是否足够。
- 只对缺少真实唯一性保障的约束做 schema 调整。
- 若 schema 改变，提升版本并提供前向迁移或明确的重新初始化要求。
- 真实 MySQL 运行多连接串读、串写和并发幂等测试。

### 阶段 5：公共接口与文档

- 公共示例始终显式传入 scope。
- 文档说明固定本地 scope 是合法独立运行模式。
- 文档说明 scope 不是授权机制，宿主不得因为核心有 scope 就跳过鉴权。

## 7. 错误语义

- 错误 scope 查询与资源不存在对普通调用方保持相同结果，避免形成资源枚举侧信道。
- 同一次内部写入包含多个 scope 属于不变量错误，不自动修正。
- 数据库载荷与索引 scope 不一致属于持久化损坏，不返回原始行或数据库异常。
- 授权失败由宿主处理，不在核心增加 `PermissionDenied` 业务模型。

## 8. 非目标

- 不实现用户、组织、工作空间、角色和权限管理。
- 不实现套餐、计费或按用户统计。
- 不提供跨 scope 数据搬迁和合并。
- 不使用数据库 Row Level Security 作为唯一保障；未来可叠加，但不替代应用契约测试。
- 不把 `scope_key` 改名为 `tenant_id`。

## 9. 完成标准

- 所有普通资源查询和修改都不能省略 scope。
- Worker 的全局操作与普通 scoped 操作在端口和文档中清楚区分。
- memory、SQLite、MySQL 共用同一套隔离契约测试。
- 同名幂等键、Subject 和外部记录在不同 scope 中互不冲突。
- 错误 scope 不可读、不可写、不可取消，也不能通过错误差异枚举资源。
- 核心仍不拥有用户、套餐、权限或宿主平台模型。

## 10. 执行记录（2026-09-03）

- 已完成 Run、History、Document、Snapshot、ChangeEvent、ingest 和查询分页的 scope 校验与隔离索引审查。
- 已加入 memory/SQLite 共享隔离契约，覆盖同名幂等键、Subject、资源 ID、取消和损坏载荷投影。
- 已通过 memory、SQLite 和真实 MySQL 的隔离契约；真实 MySQL 还覆盖了多连接资源写入与错误 scope 访问。
- 配置真实 MySQL 8.0.36 时，`python -m pytest -q` 为 114 passed；未配置时为 94 passed、20 skipped。
