# 公共 API 与扩展 SPI

## 为什么分成两套接口

公共 API 面向使用代码库的业务和宿主，应该长期稳定并表达监测意图。扩展 SPI 面向适配器作者，允许统一采集 API、存储和场景映射独立替换。二者混在一起，会让游标、供应商错误和数据库细节泄漏给业务调用者。

## 公共 API：宿主使用

```text
submitRun(RunRequest, ExecutionContext) → RunRef
cancelRun(run_id, ExecutionContext)     → CancellationResult
getRun(run_id, scope_key)               → RunSummary
queryChanges(ChangeQuery, scope_key)    → ChangePage
```

`RunRequest` 表达要观察什么和使用哪种有版本的场景规格；不包含轮询间隔、队列名、HTTP 参数和数据库事务。

`ExecutionContext` 只传 `scope_key`、`actor_ref`、幂等键、追踪引用和可选的已计算执行限制。

公共错误只保留调用者能处理的情况，例如请求无效、范围不匹配、执行限制拒绝和运行不存在。供应商状态码与 SDK 异常不能直接透出。

## 运行接口：装配层使用

```text
wake(limit)              → WorkSummary
recoverInterruptedRuns() → RecoverySummary
```

这是运行宿主使用的运维接口，不是普通业务 API。单机运行器可以定时调用，队列适配器也可以在消息到达时调用。它隐藏具体 Run 的内部推进顺序，避免宿主依次调用“查状态、拉一页、写游标、再重试”。

## 扩展 SPI：适配器实现

### UpstreamJobGateway

```text
submit(request, idempotency_key) → UpstreamJobRef
getStatus(job_ref)                → UpstreamStatus
readBatch(job_ref, checkpoint?)  → UpstreamBatch
cancel(job_ref)                   → UpstreamCancellation
```

它隐藏鉴权、HTTP、Webhook、供应商 DTO、分页字段和错误码。`checkpoint` 是核心内部状态，不进入公共 API。

### CollectionAdapter

```text
supports(collection_type, schema_version) → boolean
validate(spec)                            → ValidationResult
buildUpstreamRequest(spec)                → UpstreamJobRequest
mapRecord(record, mapping_context)         → ObservedRecordDraft
```

它拥有某一采集类型从请求到结果的完整翻译知识，避免宿主必须正确配对“请求构造器”和“结果映射器”。映射失败产生可审计的记录级错误，不直接破坏整批结果。

### ContentPolicy

```text
identify(draft)                    → SubjectRef
prepareRevision(content)           → RevisionMaterial
compare(previous, current)         → RevisionDecision
```

它按内容类型生成稳定的对象身份与规范化版本材料，并解释新旧快照差异。它与 CollectionAdapter 分开，因为同一种内容可能来自多个上游采集方式，而身份和内容比较规则不应随供应商切换。

策略同时声明 `content_type_key`、`subject_namespace` 和 `policy_ref`。内容类型与持续观察对象的身份命名空间可以相同，也可以不同；这允许同一类内容采用独立的长期身份规则。

### StateStore 与 ArtifactStore

核心需要原子保存运行推进与检查点，并保存或引用不可变证据。公共接口应按核心需要的原子能力设计，不能机械照搬每张数据库表的 CRUD；详细操作要在实现前结合事务不变量确定。

### EventSink

核心发布 `RunCompleted`、`ObservationRecorded` 和 `ContentChanged`。进程内订阅、Outbox、消息队列由适配器决定；核心事件语义不随中间件变化。

## 接口稳定性

- 公共 API 采用兼容演进：新增可选字段、增加新查询，不随供应商版本变化。
- SPI 可以按主版本升级，但一个适配器必须明确声明支持的契约版本。
- 内部模型 `Item`、`Attempt`、checkpoint、锁和 ORM 实体不承诺兼容性。
- 扩展通过装配注册，不提前建设动态插件发现、插件市场或脚本运行沙箱。

## 回退机制如何加入

回退是运行装配中的内部策略。`GatewayRouter` 可以把主、备用 `UpstreamJobGateway` 组合成一个网关；明确允许回退的提交失败才切换，提交结果不确定时仍使用同一候选和幂等键重试。公共 `submitRun`、`RunRequest` 和历史模块都无需改变。只有当业务必须显式选择“禁止回退”时，才把它提升为有名称的领域策略，而不是随手增加布尔参数。
