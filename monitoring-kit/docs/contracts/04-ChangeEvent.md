# ChangeEvent 契约

## 目的

表达内容历史已经确认的通用变化事实，供分析、报告、预警和宿主平台按需消费。

`ChangeEvent` 只能由内容历史产生。外部适配器提交的是 `Observation`，不能直接宣布对象已删除或内容已经变化。

## 稳定结构

```text
ChangeEvent {
  contract_version
  event_id
  scope_key
  document_id
  run_id
  sequence                   Document 内单调递增
  kind                       稳定的核心变化类型
  occurred_at                系统确认变化的时间
  effective_observed_at      支撑该判断的观察时间
  from_snapshot_id?
  to_snapshot_id?
  evidence_refs[]
  policy_ref                 使用的修订/缺失判断策略版本
  details?                   TypedEnvelope
}
```

## 核心变化类型

- `FIRST_SEEN`：首次确认对象存在并形成首个 Snapshot。
- `REVISED`：修订策略确认内容产生新版本。
- `MISSING_SUSPECTED`：出现有效缺失证据，但尚未满足确认条件。
- `MISSING_CONFIRMED`：满足版本化缺失策略，确认对象当前缺失。
- `RESTORED`：此前已确认缺失的对象再次被有效观察到。

没有变化时不创建 ChangeEvent，只记录 Observation 和最后观察时间。场景特有的“引用新增”“情感突变”“排名下降”应由分析扩展消费通用事件后产生自己的事件或 Finding，不能无限增加核心 `kind`。

## details 扩展

`details` 可以携带修订策略生成的有类型差异，例如网页字段变化、GEO 引用集合变化或帖子正文修订。它用于解释变化，不决定核心 `kind`。

消费者必须能够只读取固定字段完成幂等、过滤和回溯；不认识 details 类型时可以安全忽略，而不能导致整个事件无法消费。

## 不变量

- `event_id` 全局唯一；重复投递语义不变。
- 同一 Document 的 `sequence` 单调递增且不能复用。
- Snapshot 不可变；事件通过 ID 引用，不嵌入可修改快照。
- `REVISED` 必须同时引用前后 Snapshot。
- `FIRST_SEEN` 必须引用 `to_snapshot_id`，没有 `from_snapshot_id`。
- 缺失不能由网络超时直接产生，必须来自有效 Observation 和版本化策略。
- `scope_key` 必须与 Run、Document 和 Snapshot 一致。

## 交付语义

ChangeEvent 允许至少一次投递。推荐在保存历史状态的同一事务中写入 Outbox，再由具体事件适配器投递。消费者按 `event_id` 去重。

## 非目标

不承载通知接收人、报告模板、风险等级和 Case 状态；这些属于可选扩展。
