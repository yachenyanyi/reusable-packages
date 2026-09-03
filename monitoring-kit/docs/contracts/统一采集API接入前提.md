# 统一采集 API 接入前提

## 1. 文档目的

本文面向统一采集 API 的设计者，定义监测系统能够可靠接入该 API 所需的最小契约。

主要交互模式为：客户端提交采集任务，API 返回稳定任务 ID；客户端按任务 ID 查询状态并获取结果。事件流/Webhook 用于降低延迟，游标用于增量结果和断点续传，但二者不作为唯一事实来源。

文中的 MUST 表示接入必需，SHOULD 表示强烈建议。

## 2. 边界与职责

统一采集 API 负责：

- 从网站、AI 平台或其它来源实际采集数据。
- 隐藏浏览器、代理、验证码处理边界、平台调用和供应商差异。
- 管理上游采集任务的执行与内部重试。
- 提供可恢复、可分页、可核对的结果。

监测代码库负责：

- 内部 Run、结果消费状态和内容历史。
- 将 API 结果转换为 Observation。
- 幂等入库、Snapshot、ChangeEvent、分析、Case、报告和预警。

用户、Workspace、套餐和权限由接入代码库的宿主平台负责；独立运行时不要求实现这些平台概念。

## 3. 任务身份

以下身份必须分开：

```text
task_id          监测系统中的长期任务
run_id           任务的一次内部运行
upstream_job_id  统一采集 API 创建的任务
```

API MUST 返回稳定且全局唯一，或在调用方账号范围内唯一的 `upstream_job_id`。任务创建后该 ID 不得改变或复用。

## 4. 最小语义接口

具体 URL 命名可以调整，但 MUST 提供以下语义能力：

```text
创建任务       POST /jobs
查询任务       GET  /jobs/{job_id}
分页获取结果   GET  /jobs/{job_id}/results?cursor=...
取消任务       POST /jobs/{job_id}/cancel       SHOULD
事件/Webhook   job.progress / result.available / job.finished  SHOULD
```

客户端不能只依赖事件流；查询任务和最终结果接口必须始终可用。

## 5. 创建任务

创建请求 MUST 支持：

```text
idempotency_key  客户端生成的幂等键，建议直接使用 run_id
source_type      website / geo / opinion / 其它版本化类型
task_spec        与 source_type 对应的有类型任务参数
schema_version   请求结构版本
client_metadata  可选的非敏感关联信息
callback         可选的事件回调配置
```

API MUST 保证：同一调用方使用相同 `idempotency_key` 重复提交时，返回同一个 `upstream_job_id`，不重复创建任务。

幂等键保留期限 MUST 明确，并且不短于任务最长运行时间与客户端可能重试的窗口。

创建响应至少包含：

```text
job_id
status
accepted_at
request_schema_version
```

## 6. 任务状态

API MUST 将内部供应商状态归一化为稳定状态：

```text
queued
running
completed
completed_with_errors
failed
cancelled
```

终态一旦返回不得回退到运行态。`completed` 表示结果已经持久化且可被结果接口完整读取，而不只是采集进程已经结束。

状态查询 SHOULD 返回：

```text
job_id
status
progress                    可选，必须说明口径
produced_count
failed_count
created_at
started_at
updated_at
finished_at
last_event_sequence
latest_cursor
error                       失败时提供稳定错误
usage                       当前或最终用量
```

## 7. 结果分页与游标

结果接口 MUST：

- 支持任务运行中读取已经产生的部分结果。
- 支持通过不透明 `cursor` 继续读取。
- 返回 `next_cursor` 和 `has_more`，不能要求客户端解析游标内容。
- 明确游标作用域；游标只能用于其所属 job 和结果 schema。
- 在声明的结果保留期内保证旧游标可继续使用，或返回明确的游标失效错误。
- 允许客户端安全重复读取同一游标，不得产生外部副作用。

客户端会在结果成功持久化后才提交本地 cursor。API 应允许结果重复返回；客户端将按 `record_id` 幂等去重。

## 8. 结果记录统一外壳

每条结果 MUST 包含：

```text
record_id       在该 API 中稳定唯一，重复读取保持不变
job_id
source_type
external_id     外部对象稳定身份；无法提供时说明生成规则
observed_at     上游实际观察或采集时间
schema_version
payload         与 source_type 对应的有类型内容
provenance      来源、平台、模型或采集证据信息
```

以下字段 SHOULD 提供：

```text
published_at    内容自身发布时间
deleted         删除或失效标记
raw_ref         原始证据引用及其访问期限
sequence        任务内稳定序号
upstream_hash   上游内容指纹，仅作参考
```

监测系统会自行规范化并计算内部 `content_hash`，不会把 `upstream_hash` 当作唯一变化依据。

供应商 DTO、私有异常、浏览器对象和无法版本化的自由格式字段不得直接成为公共结果契约。

`external_id` 用于来源追溯，但监测代码库不会机械地把它当成 Document 身份。例如 GEO 每次回答可能拥有新的 external_id，而跨批次比较对象由“问题、模型、地区、语言”等稳定维度生成。

## 9. 事件流与 Webhook

事件是加速通道，不是任务与结果的唯一事实来源。

事件 SHOULD 包含：

```text
event_id
job_id
event_type
sequence
occurred_at
cursor              如果有新的可读取结果
schema_version
```

交付语义可采用至少一次。API MUST 明确事件可能重复、延迟或乱序；客户端会按 `event_id` 幂等处理，并根据 `sequence` 检测缺口。

Webhook SHOULD 支持签名、时间戳、防重放和失败重投。即使 Webhook 最终投递失败，客户端仍能通过状态与结果接口恢复。

## 10. 最终完成与核对

任务进入终态时，API MUST 提供稳定的最终统计：

```text
produced_count
failed_count
result_schema_version
finished_at
```

API SHOULD 提供结果清单版本或校验信息，便于客户端核对事件流和增量拉取得到的数据是否完整。

客户端会在收到终态后再次查询状态，并拉取到 `has_more=false`，再将内部 Run 标记为完成。事件中的 `job.finished` 不能单独作为完整性证明。

## 11. 错误与限流

错误响应 MUST 使用稳定结构：

```text
code          可编程处理的稳定错误码
message       面向维护者的说明
retryable     客户端是否可以重试
retry_after   可重试时的建议等待时间
request_id    排障关联 ID
```

必须能区分：请求无效、鉴权失败、额度不足、限流、暂时不可用、任务不存在、游标失效和任务内部失败。

HTTP 状态码可以作为传输语义，但不能要求客户端解析错误文本判断是否重试。API SHOULD 提供明确的限流头或 `retry_after`。

## 12. 取消语义

取消接口 SHOULD 幂等，并明确：

- queued 与 running 状态能否取消。
- 取消后已产生结果是否仍可读取。
- 上游无法立即停止时返回什么状态。
- 取消前已经产生的用量如何结算。

重复取消同一任务应返回当前结果，不应产生错误副作用。

## 13. 用量与套餐结算

API SHOULD 返回可审计的实际用量，而不是只返回价格：

```text
采集记录数
请求或页面数
模型调用次数
模型输入/输出 Token
浏览器运行时长
其它计费维度
```

用量数据应说明是估算、进行中还是最终值。客户端会在提交任务前预占额度，在任务终态后按最终用量结算。

## 14. 安全要求

- MUST 使用可撤销、可轮换的调用凭证。
- MUST 隔离不同调用方的任务和结果，不能仅依赖客户端传入的 tenant_id。
- MUST 对 Webhook 签名并支持防重放。
- MUST 在日志和错误中屏蔽 Token、Cookie、密码和敏感原文。
- SHOULD 支持调用方、来源类型和任务级访问审计。
- 原始证据访问链接 SHOULD 短期有效或需要鉴权。

## 15. 版本与兼容性

- 请求、任务状态、事件和结果都必须有明确 schema 版本。
- 同一主版本内新增字段应保持向后兼容。
- 删除或改变字段语义必须发布新主版本并提供迁移期。
- API 必须公布任务、结果、游标、事件和幂等键的保留期限。

## 16. API 设计者交付清单

接入前需要确认：

- [ ] 创建任务支持幂等键，并明确保留期限。
- [ ] 任务 ID 稳定且不会复用。
- [ ] 状态可查询，终态语义明确且不可回退。
- [ ] 运行中可以分页获取部分结果。
- [ ] 游标不透明、可恢复、作用域与失效错误明确。
- [ ] 每条结果有稳定 `record_id` 和 `schema_version`。
- [ ] 任务完成后可获取最终数量并核对完整性。
- [ ] 事件允许重复/乱序，具有 `event_id` 与 `sequence`。
- [ ] Webhook 有签名、防重放和重投策略。
- [ ] 错误结构包含稳定错误码、retryable 与 retry_after。
- [ ] 限流、任务/结果保留期和服务可用性目标已说明。
- [ ] 取消和取消后的结果、用量语义已说明。
- [ ] 最终用量能够按稳定维度审计。
- [ ] 调用方隔离、凭证轮换和敏感数据处理方案已说明。

## 17. 客户端接受的现实

客户端不会要求“恰好一次”投递。客户端接受事件和结果至少一次交付，并通过 `idempotency_key`、`record_id`、`event_id` 和本地事务实现重复安全。

客户端也不会把事件流当作最终事实，而会用任务查询与最终结果接口完成恢复和核对。
