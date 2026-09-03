# monitoring-kit 核心范围与开发总计划

> 状态：当前工作树的范围与本地验收基线（Issue #1～#5 已完成本地实现与真实 MySQL 契约验收）
>
> 更新时间：2026-09-03
> 适用仓库：`packages/monitoring-kit`

这份文档是 `monitoring-kit` 的总计划，负责回答“哪些事情属于核心库、按什么顺序做、做到什么程度才算完成”。Issue 级别的技术细节继续放在对应的分项计划中。

当前工作树已经包含 Issue #1～#5 的首轮实现、审计修复、真实 MySQL 契约测试和代码卫生检查。这里的“已完成”只表示实现和本地验收完成，不表示已经提交、发布或关闭 GitHub Issue。

## 1. 项目定位

`monitoring-kit` 是一个可嵌入、也可独立运行的外部信息持续观察核心库。它负责把一次采集运行可靠地变成可追溯的内容事实和稳定事件：

```text
Run → Observation → Document → Snapshot → ChangeEvent
```

核心库要沉淀的是跨场景都成立的能力：运行状态、幂等、恢复、历史版本、变化识别、范围隔离、可靠事件投递和运行时资源分配。

它不是平台产品，也不拥有用户、组织、套餐、计费、页面、审批或具体监测业务模型。

## 2. 范围结论

### 纳入本项目的能力

- **Issue #1：RunStateStore 多 Worker 并发保护**
  - 归属：`collection/` 端口与运行模型、`adapters/persistence/` 实现。
  - 内容：状态版本 CAS、租约过期后的 fencing、旧 Worker 不得覆盖新状态。
  - 当前状态：实现已在工作树，并已通过真实 MySQL 多连接 Run fencing 验收。
  - 分项计划：[v0.2 可靠性修复与代码收敛计划](14-v0.2可靠性修复与代码收敛计划.md)。

- **Issue #2：ContentHistory 并发一致性**
  - 归属：`history/` 内部写入命令与关系型 `HistoryStore`。
  - 内容：计算依据校验、有限冲突重算、历史事实单事务提交、revision/sequence 不重复。
  - 当前状态：实现已在工作树，并已通过真实 MySQL History 首次并发建档验收。
  - 分项计划：与 Issue #1 共用 [v0.2 可靠性修复与代码收敛计划](14-v0.2可靠性修复与代码收敛计划.md)。

- **Issue #3：事务型 Outbox 与可靠投递**
  - 归属：`runtime/` 投递运行时、`history/` 提交意图、持久化适配器。
  - 内容：历史事实和 Outbox 同事务写入，独立 Dispatcher 负责领取、重试、退避、租约恢复和 fencing。
  - 当前状态：内存、SQLite 和真实 MySQL 的 Outbox/Dispatcher 契约及故障窗口验收已完成。
  - 分项计划：[v0.3 事务型 Outbox 开发计划](15-v0.3事务型Outbox开发计划.md)。

- **Issue #4：`scope_key` 全链路隔离**
  - 归属：核心公共查询、History/Run 端口、所有持久化适配器和契约测试。
  - 内容：读、写、取消、幂等、Document、Snapshot、ChangeEvent 和分页均不能跨 scope；scope 不是授权模型。
  - 当前状态：实现和 memory/SQLite/真实 MySQL 隔离契约测试已在工作树完成。
  - 分项计划：[scope 隔离契约强化开发计划](16-scope隔离契约强化开发计划.md)。

- **Issue #5：公平调度与并发配额**
  - 归属：`runtime/` 的 `WorkAllocator` 契约、RunStateStore 的共享实现和运行策略。
  - 内容：全局、scope、gateway 并发限制，scope 轮转公平，租约过期恢复；无策略时保持简单运行。
  - 当前状态：首轮可选实现和边界修复已在工作树；高级权重、速率限制、熔断和背压不属于本轮。
  - 分项计划：[运行时公平调度与并发配额开发计划](17-运行时公平调度与并发配额开发计划.md)。

统一采集 API 网关、假 API 联调测试、内存适配器和 SQLite/MySQL 关系型适配层是上述能力的基础设施与验收手段，也属于本仓库；但它们不能把 HTTP、数据库或测试服务模型提升为核心领域模型。

### 明确不纳入本轮核心开发

- **Issue #6：站点监测 MVP**。它定义 URL 身份、页面发现和页面内容模型，是 `extensions/site` 或具体 `apps` 的业务扩展，不属于共享核心。
- GEO 效果监测、舆情监控和站点监测的业务字段、分析规则、报告页面和告警模板。
- 数据源资产目录、任务中心、Finding/Case、报告、预警等可选扩展。它们可以以后接入核心，但不能成为核心的反向依赖。
- 用户、组织、工作空间、RBAC、套餐、计费、订单、额度购买和平台 UI。
- Kafka、RabbitMQ、Celery、Redis、通用工作流 DSL、插件市场和动态热加载。
- 对象存储自动迁移、跨数据库数据搬迁和尚未被真实需求证明的第三种数据库。

## 3. 复杂度目标与设计选择

### 要降低的复杂度

- 调用方不需要协调轮询、重试、回退、游标、事务、租约和历史版本顺序。
- 更换统一采集 API、数据库或消息传输方式时，不改核心领域契约。
- 多 Worker 的旧状态、重复结果和跨 scope 访问不能静默破坏新事实。
- 新增一个业务场景时，主要增加适配器和策略，而不是修改核心状态机。

### 方案比较

**方案 A：按 Issue 各自堆功能。** 每个问题新增一组 Store 方法、Worker 分支和配置开关。初期改动看起来少，但会把 CAS、scope、Outbox、配额和重试分散到多个调用方，最终形成难以验证的调用顺序。

**方案 B：按隐藏知识组织少数深模块。** `CollectionEngine` 拥有运行推进，`ContentHistory` 拥有历史事实，`OutboxDispatcher` 拥有事件投递，`WorkAllocator` 拥有工作分配；稳定端口隔离上游、存储和传输实现。

选择方案 B。它让模块内部承担复杂决策，公共接口保持小而表达意图。当前不再额外创建只转发调用的 Scheduler、Repository、Manager 或 Service。

## 4. 目标模块关系

```text
宿主平台 / 独立 CLI
        │
        ├── CollectionEngine ── ContentHistory
        │          │                    │
        │          ├── WorkAllocator    ├── HistoryStore
        │          ├── RunStateStore    └── EventDeliveryStore
        │          └── UpstreamJobGateway
        │
        └── OutboxDispatcher ── EventPublisher

稳定核心契约与端口
        │
        └── memory / unified API / relational / future adapters
```

边界规则：

- `contracts/`、`collection/`、`history/` 和 `runtime/` 不导入 `adapters/`、`extensions/` 或 `apps/`。
- HTTP 响应、供应商 DTO、SQLAlchemy 行、事务、锁、数据库异常和消息格式只能停留在适配器边界。
- `scope_key` 是不透明隔离命名空间；宿主负责把用户或工作空间映射到它并完成授权。
- 当前首轮实现让关系型 `RunStateStore` 同时承担 `WorkAllocator` 端口，避免增加一个只转发的调度服务；只有出现独立调度后端或真实扩展压力，才重新评估拆分。

## 5. 分阶段开发计划

### 阶段 0：冻结范围与契约

1. 以本文件确认 Issue #1～#5 属于核心，Issue #6 及业务场景不进入本轮。
2. 固定 `RunRequest`、`Observation`、`Document`、`Snapshot`、`ChangeEvent`、`scope_key` 和 `TypedEnvelope` 的稳定语义。
3. 固定宿主最小调用面：提交、取消、查询、变化读取；运行唤醒和事件投递属于装配/运维入口。
4. 固定统一采集 API 的任务 ID、状态、结果分页和幂等前提；游标只作为内部断点与事件流辅助能力。

阶段门槛：调用方不需要知道内部表、HTTP 状态码、供应商游标、重试计数或锁对象。

### 阶段 1：v0.1 最小核心闭环

完成稳定契约、`CollectionEngine`、`ContentHistory`、内存参考适配器、统一 API 网关边界、扩展注册和 CLI 验收入口。

阶段门槛：至少能在独立进程内完成 `Run → Observation → Snapshot → ChangeEvent`，并能安全重放同一结果。

状态：已完成，作为本轮后续工作的基线。

### 阶段 2：v0.2 关系型存储与并发可靠性（Issue #1、#2）

1. 用同一套关系型 Store 语义支持 SQLite 和 MySQL；数据库类型只由 `database_url` 决定。
2. 为 Run 保存状态版本并使用 CAS；为 History 写入保存计算依据并在冲突时有限重算。
3. 用单事务保证历史事实、幂等记录和当前指针的一致性。
4. 对大 JSON、schema 初始化、分页下推、损坏载荷和敏感连接串建立边界测试。
5. 分配器先锁定共享公平游标，再读取占用计数；严格 MySQL 分组、饱和 gateway 扫描和首个 Document 唯一键竞争都必须有明确语义。

阶段门槛：memory、真实 SQLite 文件库和真实 MySQL 运行同一套高价值契约测试；旧 Worker 不能覆盖新状态。

状态：实现和真实 MySQL 多连接验收已完成；接入新部署环境时按同一契约复跑。

### 阶段 3：全链路 scope 隔离（Issue #4）

1. 所有面向资源的读写都带 `scope_key`，错误 scope 对外按不存在处理。
2. 将 scope 纳入幂等、Subject、ingest 和查询索引语义，并校验载荷与索引投影一致。
3. 明确 Worker 的跨 scope 领取是可信运行时能力，不把它暴露成普通资源查询。
4. memory、SQLite 和 MySQL 复用同一隔离契约测试。

阶段门槛：两个 scope 可以使用相同业务键但互不串读、串写、串取消或串幂等；scope 不替代宿主授权。

状态：实现和真实 MySQL 隔离验收已完成；scope 仍不替代宿主授权。

### 阶段 4：事务型 Outbox（Issue #3）

1. `HistoryStore.commit()` 在历史事务中同时写入已承诺事件的投递意图。
2. `OutboxDispatcher` 隐藏领取、发送、重试、退避、阻塞、租约恢复和旧 Worker fencing。
3. 保证至少一次投递，不承诺恰好一次；消费者按稳定 `event_id` 去重。
4. 保持 `DeliveryGuarantee.NONE` 的轻量独立运行路径，不把 Outbox 强行变成所有部署的前置服务。

阶段门槛：commit 成功后即使 Dispatcher 未运行或在任意投递窗口崩溃，事件仍可恢复；历史事实不因传输失败回滚。

状态：内存、SQLite、真实 MySQL 和崩溃窗口验收已完成；不承诺恰好一次投递。

### 阶段 5：公平调度与并发配额（Issue #5）

1. 以 `RuntimePolicy` 表达通用运行约束，不引入套餐、VIP 或余额概念。
2. 由唯一的 `WorkAllocator.allocate()` 入口执行全局、scope 和 gateway 并发限制。
3. 在有资格的 scope 之间使用共享轮转；同一 scope 内保持稳定顺序。
4. 租约过期后容量自动恢复，旧 Worker 继续受到状态版本围栏保护。
5. 配置 gateway 配额时，排队 Run 必须已有可确定的 `gateway_hint`；无法确定时在提交阶段拒绝，不能静默绕过配额。
6. 无策略时保持独立运行的简单默认路径；本轮不加入加权公平、速率限制、熔断和背压。

阶段门槛：多 Worker 下不超过共享上限，忙 scope 不长期饿死其它 scope，饱和 gateway 不阻塞其它 gateway。

状态：核心公平与配额语义已完成 memory/SQLite/真实 MySQL 多连接验收；更高阶策略按真实压力后置。

### 阶段 6：发布前收敛与验收

1. 清理测试中的未使用导入和仅为测试方便而暴露的内部细节。
2. 为不同 Observation 并发写入同一 Document 补齐统一契约测试，检查最终事实而不只检查异常。
3. 为公平游标锁、同 Worker 续租、饱和 gateway 和未知 gateway 配置补齐回归测试。
4. 配置 MySQL 8+、InnoDB、`utf8mb4` 实例，运行多连接、多 Worker、Outbox 和大载荷验收。
5. 运行完整测试、编译检查、导入边界检查和 `git diff --check`。
6. 审查顶层公共 API，确认没有把内部模型、数据库实现或测试工具误承诺为稳定接口。
7. 验收通过后再提交代码、同步分项计划状态，并据证据决定是否关闭对应 Issue。

## 6. 测试与发布门槛

### 快速契约层

- memory Store：状态版本、历史幂等、scope 隔离、Outbox 状态机、公平轮转和同 Worker 续租。
- `ScriptedGateway`：运行状态、重试、回退、游标推进和恢复。
- 假统一采集 API：真实 loopback HTTP、任务提交幂等、状态映射、结果分页和取消。

### SQLite 层

- 使用真实文件数据库，不使用单连接内存数据库冒充并发测试。
- 覆盖 schema 初始化竞争、历史事务回滚、分页下推、租约过期和 Outbox 恢复。

### MySQL 层

- 通过 `MONITORING_KIT_TEST_MYSQL_URL` 显式启用真实实例测试。
- 覆盖多连接领取、旧 Worker fencing、History 并发、scope 隔离、Outbox 领取和 `LONGTEXT` 大载荷往返。
- 未配置实例时允许明确跳过，但不能把方言编译测试描述成真实 MySQL 通过。

当前工作树验收基线：配置真实 MySQL 8.0.36 时，`python -m pytest -q` 为 114 passed；未配置时为 94 passed、20 skipped，跳过项均是需要真实 MySQL 的测试。`compileall`、导入边界检查和 `git diff --check` 已通过；发布或接入其它环境时应复跑同一矩阵。

## 7. 完成定义

本轮核心开发完成，需要同时满足：

- Issue #1～#5 的核心语义都有实现、文档和可重复测试。
- memory、SQLite 和真实 MySQL 的高价值行为一致，数据库差异只停留在适配器内部。
- 历史事实、幂等、scope、租约、Outbox 和公平分配没有已知的静默覆盖或跨范围漏洞。
- 独立运行只需内存或一个数据库 URL；接入宿主平台时不需要搬入用户、套餐或权限模型。
- 顶层公共 API 小而稳定，内部游标、重试、事务、租约、锁和载荷格式没有泄漏。
- 没有新增通用垃圾桶、第二套数据库 Store、只转发的服务层或未经需求证明的基础设施。

## 8. 后续触发条件

以下能力不提前开发，只有出现证据才创建新的分项计划：

- 真实上游需要 Webhook/事件流时，再补事件接入和去重契约。
- 内容体量或访问模式证明需要大对象存储时，再设计 `ArtifactStore` 策略。
- 真实数据库有不可逆 schema 演进压力时，再评估迁移工具。
- 多场景出现共同业务规律后，再把场景能力提升为扩展，而不是直接塞进核心。
- 站点、GEO、舆情需要落地时，在 `extensions/` 或 `apps/` 创建各自计划，保持核心契约不变。
