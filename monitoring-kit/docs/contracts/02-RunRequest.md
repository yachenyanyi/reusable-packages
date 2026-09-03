# RunRequest 契约

## 目的

表达“启动一次什么监测”，不暴露这次运行如何排队、轮询、重试、回退和保存。

## 稳定结构

```text
RunRequest {
  contract_version
  collection                 TypedEnvelope
  source_ref?                宿主或扩展定义的稳定来源引用
  requested_window?          要观察的业务时间范围
  correlation_refs?         非敏感的外部关联引用
}
```

调用时另传 `ExecutionContext`：

```text
ExecutionContext {
  scope_key                  数据隔离范围
  actor_ref                  发起者引用
  idempotency_key            同一逻辑提交保持不变
  limits?                    宿主已计算的执行限制
  trace_ref?
}
```

`scope_key` 和 `actor_ref` 不放进场景 payload，防止扩展伪造隔离边界。独立运行时可使用固定的本地值。

## collection 扩展示例

```text
type_key: org.example.site.scan
schema_version: 1.0
data: SiteScanSpec

type_key: org.example.geo.query-batch
schema_version: 1.0
data: GeoQueryBatchSpec

type_key: org.example.opinion.search
schema_version: 1.0
data: OpinionSearchSpec
```

这些类型由可选场景模块拥有。核心只验证信封和处理器是否已注册，再委托对应 `CollectionAdapter` 验证业务字段、生成上游任务规格并映射该任务的结果。

## 不变量

- 同一 `scope_key + idempotency_key` 的重复提交返回同一个内部 Run。
- `type_key + schema_version` 必须有且只有一个已注册处理器。
- 处理器验证通过后才创建上游任务。
- 请求创建后冻结原始契约与处理器版本；后续修改 Task 不改变既有 Run。
- 请求不能携带队列名、轮询间隔、HTTP 客户端、数据库事务和供应商 SDK 对象。

## 公开结果

```text
RunRef {
  run_id
  accepted_at
  status
}
```

`upstream_job_id` 只用于内部追踪，不作为普通业务结果返回。需要排障时可在受控的运行详情或审计视图中查看。

## 稳定错误

- `INVALID_REQUEST`：固定字段不合法。
- `UNSUPPORTED_COLLECTION_TYPE`：没有注册对应类型或版本。
- `INVALID_COLLECTION_SPEC`：场景处理器拒绝扩展内容。
- `SCOPE_MISMATCH`：引用对象不属于当前范围。
- `EXECUTION_LIMIT_REJECTED`：宿主给出的限制不允许本次运行。

上游限流、暂时故障和游标失效是运行过程结果，不直接变成提交接口的供应商异常。

## 非目标

不表达长期周期任务、套餐名称、用户权限、内部 Attempt、回退顺序和最终分析方案。
