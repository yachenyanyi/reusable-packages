# Observation 契约

## 目的

表达“某个外部对象在某个时间被观察成什么样”，作为采集引擎与内容历史之间的稳定边界。

## 稳定结构

```text
Observation {
  contract_version
  observation_id            核心生成的内部 ID
  scope_key                  由运行上下文继承
  run_id
  ingest_key                 上游记录的幂等身份
  subject                    持续观察对象的稳定身份
  observed_at                外部实际观察时间
  presence                   PRESENT | ABSENT
  content?                   TypedEnvelope，PRESENT 时必需
  published_at?
  provenance                 Provenance
}
```

```text
ingest_key {
  gateway_key
  upstream_record_id
}

subject {
  namespace                  身份规则命名空间
  key                        在命名空间内稳定的比较对象键
  identity_version           生成规则版本
  canonical_uri?
}

provenance {
  source_ref?
  upstream_job_ref?
  upstream_external_id?      来源系统中的原生对象身份
  raw_artifact_ref?
  collector_ref?
  attempt_ref?             核心内部 Attempt 的受控追踪引用
}
```

## 三种不同的身份

- `ingest_key` 回答“这条上游结果是否处理过”，用于至少一次投递去重。
- `subject` 回答“哪些观察属于同一个持续比较序列”，用于关联 Document 和历史版本。
- `upstream_external_id` 回答“来源系统把这一次内容称作什么”，用于追溯，但不必等于 subject.key。

三者不能合并：同一个网页或帖子会在不同 Run 中产生不同上游记录；GEO 的每次回答也可能有新的原生 ID，但 `问题 × 模型 × 地区 × 语言` 仍应落入同一个比较序列。

`subject.key` 由对应内容类型的 `ContentPolicy` 生成，不能由上游记录或采集适配器直接指定。典型规则是：

- 站点页面：规范化 URL 或站点内稳定页面 ID。
- 舆情帖子：平台命名空间加平台帖子 ID。
- GEO 回答：QueryDefinition、模型渠道、地区和语言的稳定组合键。

## content 扩展载荷

`content` 使用带类型和版本的信封，例如：

```text
org.example.site.page@1.0
org.example.geo.answer@1.0
org.example.opinion.post@1.0
```

载荷由扩展模块定义并验证。核心通过对应 `ContentPolicy` 生成 subject；内容历史通过同一策略得到规范化版本材料、内容指纹和可选变化详情，而不是对任意 JSON 直接排序后计算哈希。

## 不变量

- `(scope_key, gateway_key, upstream_record_id)` 唯一，重复处理必须得到同一结果。
- `(scope_key, subject.namespace, subject.key, identity_version)` 稳定定位一个 Document。
- `PRESENT` 必须有 content；`ABSENT` 不得伪造空 content。
- 网络超时、限流和内部异常是 Attempt 失败，不是 `ABSENT` Observation。
- `observed_at` 表示外部观察时间；本地接收时间由系统另行记录。
- 原始证据不可被扩展 payload 内的任意路径替代，必须使用受控引用。

## 边界说明

互动量、模型回答、网页正文是否构成“新版本”，不能由核心固定字段决定。各内容类型的 `ContentPolicy` 必须明确：

- 参与内容修订的字段。
- 仅作为时序指标记录的字段。
- 需要忽略或规范化的易变字段。
- 旧 schema 如何升级或继续解释。

采集适配器先产生内部 `ObservedRecordDraft`；核心校验后调用 `ContentPolicy` 生成最终 Observation。这样供应商适配器无法随意改变既有 Document 身份。

## 非目标

Observation 不是 Finding，不包含情感结论或风险等级；也不是供应商原始响应、Attempt 日志或数据库行。
