# 假统一采集 API 联调测试计划

> 状态：第一至第三层测试已落地；关系型持久化提升验收仍按第四层计划执行。
>
> 更新时间：2026-09-03

## 目的

建立一个只用于测试的、可控的“假统一采集 API”。它通过真实 HTTP 与 `UnifiedApiGateway` 通信，模拟任务提交、状态查询、结果分页和取消语义，用来验证 `monitoring-kit` 对统一采集 API 的接入与恢复行为，并为关系型持久化提升验收提供可重复场景。

它不是数据采集产品、演示服务或第二套上游系统。它的价值是让我们可以稳定重现上游在真实项目中一定会出现的重复、延迟、限流、部分结果和响应丢失，而不依赖网络、供应商账户或时间碰巧合适。

## 先明确：它补哪一层，不替换哪一层

现有 `ScriptedGateway` 直接实现 `UpstreamJobGateway`，适合快速、确定地测试 `CollectionEngine` 的状态机；现有 `urlopen` monkeypatch 适合测试 `UnifiedApiGateway` 如何翻译单次 HTTP 响应。

两者都应保留。但它们之间仍缺少一层验证：真实 `UnifiedApiGateway` 是否能通过 HTTP 正确接入一套符合契约的 API，并与采集引擎一起完成重试、游标推进和恢复。

假 API 只补这一层。它使用正式的统一采集 API 路径和 JSON 语义，但不进入 `src/monitoring_kit/`，不成为公共 API，也不替代真实统一采集服务的契约验收。

## 复杂度目标

- 一个测试用例只描述“上游发生了什么”，无需在用例里拼装 HTTP 响应、端口、线程、请求计数和等待顺序。
- 假 API 自己拥有 HTTP 细节、请求记录、幂等映射、游标语义与故障注入；测试只读取有意义的观测结果。
- 协议测试与采集引擎测试各自保持独立，避免把所有测试堆进一个大型端到端脚本。
- 将来真实 API 的接口调整时，修改假 API 场景和契约测试即可定位影响，不需要重写核心状态机测试。

## 候选设计

### 方案 A：继续只 monkeypatch `urlopen`

优点是运行最快，已有基础。缺点是请求 URL、方法、编码、认证、游标 query、HTTP 状态和响应体之间的组合关系没有经过真实传输边界，无法发现“网关和 API 分别看起来没问题、合起来不工作”的错误。

### 方案 B：为所有测试启动一个完整 Web 框架服务

优点是接近真实部署。缺点是给核心测试引入额外依赖、启动成本和框架生命周期；大部分引擎单元测试根本不需要它。

### 方案 C：测试目录中的可脚本化最小 HTTP 服务

使用 Python 标准库在测试时绑定随机本机端口，按统一 API 契约处理少量必要端点，并由测试 fixture 管理启动、关闭和场景装载。

选择方案 C。它比 monkeypatch 多验证一层真实边界，又不让生产包依赖 Web 框架或把测试服务误做成产品能力。

## 测试替身的边界

### 对 `UnifiedApiGateway` 暴露的接口

只实现 [统一采集 API 接入前提](../contracts/统一采集API接入前提.md) 中 v0.1 需要的语义：

```text
POST /jobs
GET  /jobs/{job_id}
GET  /jobs/{job_id}/results?cursor=...
POST /jobs/{job_id}/cancel
```

它应校验 JSON、`Authorization` 和 `idempotency_key` 的基本契约，返回稳定的 `job_id`、状态、结果外壳和错误结构。当前核心不消费 Webhook，因此第一阶段不实现 Webhook、事件流、实际采集、账户系统、额度结算或浏览器自动化。

### 对测试代码暴露的控制接口

测试不能通过额外 HTTP 后门控制假 API。fixture 创建一个仅限测试进程使用的控制对象，用于：

- 在服务启动前装载一个不可变 `ApiScenario`；
- 读取收到的规范化请求记录和已创建的 job 数量；
- 在需要时获取明确的场景完成断言。

这条边界很重要：正式网关只看见真实协议；测试才可以看见脚本和观测记录。这样不会把测试便利接口误加入统一采集 API 契约。

## 场景模型

`ApiScenario` 是测试替身最深的模块。测试用例描述业务可理解的上游行为，场景负责把它解释成逐次 HTTP 响应、服务器内部 job 状态和结果页。

建议包含以下概念，而不是散落的 `fail_once=True`、`duplicate=True` 一类布尔参数：

```text
ApiScenario
├─ SubmissionPlan       提交幂等与提交后的可见结果
├─ StatusTimeline       每次状态查询返回的状态序列
├─ ResultStream         有序记录、分页大小、游标和重放规则
├─ CancellationPlan     取消接受/拒绝及最终状态
└─ ResponseScript       端点级响应、延迟可见和可恢复错误
```

`ResponseScript` 可以表达“先创建 job，再让客户端收到超时/5xx”这种很关键的情况。服务端副作用与客户端收到的响应必须分开建模，否则测不出幂等提交是否真的安全。

场景推进以请求次数和明确脚本为准，不依赖 `sleep` 或真实时间。需要时间的断言仍由核心现有的 `ManualClock` 控制，保证 CI 稳定。

## 分层测试计划

### 第一层：核心单元测试（保留）

继续使用 `ScriptedGateway` 测试采集引擎的 Run 状态机、重试、回退、游标推进、重复记录和恢复。它不启动 HTTP 服务，失败定位快。

### 第二层：网关协议测试（扩展）

使用假 API + 真实 `UnifiedApiGateway`，验证：

- `POST /jobs` 的认证、任务载荷、schema 版本和幂等键；
- 状态字段、终态、错误结构与 `UpstreamStatus`/`UpstreamError` 的映射；
- cursor 的编码、空 cursor、省略 cursor、`next_cursor` 与 `has_more`；
- 取消请求和 `UpstreamCancellation` 映射；
- 限流、临时不可用、请求无效、任务不存在和游标失效的错误分类；
- 连接/响应异常中不泄露 token 或完整 URL 中的敏感信息。

已有 monkeypatch 用例继续保留为细粒度映射测试；这层只覆盖关键的真实 HTTP 组合，不追求替代所有单元测试。

### 第三层：核心联调测试（新增）

将真实 `UnifiedApiGateway`、假 API、`CollectionEngine`、测试场景适配器和内存 Store 组合，验证下面的完整链路：

```text
submit_run → wake → 提交上游任务 → 查询状态/读取结果
          → ContentHistory 原子处理 → 保存 cursor → Run 完成
```

这里的断言应面向领域结果：Run 状态、Document/Snapshot/ChangeEvent、cursor、上游 job 数量与请求记录，不断言 SQL、内部私有字段或 HTTP 实现细节。

### 第四层：持久化提升验收（后续）

关系型适配层完成后，从第三层场景中挑选少量高价值恢复场景，在 SQLite 和 MySQL 上重复运行。它验证持久化语义，不把全部 HTTP 场景乘以全部数据库矩阵，避免测试成本失控。

## 首批验收场景

1. **正常双页完成**：任务经历 `queued → running → completed`，两页结果全部写入，最终 `has_more=false` 后 Run 才完成。
2. **重复提交幂等**：同一 `idempotency_key` 重试只对应一个上游 job。
3. **提交已受理但响应丢失**：服务端先创建 job，再返回可恢复的未知提交结果；引擎以原幂等键重试，不能切换网关或创建第二个 job。
4. **运行中部分结果**：上游仍为 `running` 时可读到首批结果；历史成功后才保存本地 cursor。
5. **重复页重放**：上游重复返回首批记录；Document、Snapshot 和 ChangeEvent 不重复生成。
6. **限流后重试**：返回稳定 `retryable` 错误与 `retry_after`；引擎按可控时钟推进后恢复。
7. **终态后的最终核对**：上游已经 `completed`，但仍有末页可读；未拉到 `has_more=false` 前，Run 不可完成。
8. **不可恢复错误**：请求无效或明确失败使 Run 进入可解释失败，不进入无限重试。
9. **取消**：取消幂等，已产生结果是否继续可读完全按场景声明；核心状态与上游响应一致。
10. **中断恢复**：历史已提交、cursor 尚未保存时模拟停止；恢复后重复读取安全，最终事实只有一份。

## 目录与运行方式（当前实现）

测试支持代码已经落在测试目录中：

```text
tests/
├─ fixtures/
│  └─ unified_api/
│     ├─ scenario.py       # ApiScenario 及其计划对象
│     └─ server.py         # 测试期 HTTP 服务、生命周期与请求观测
├─ adapters/
│  └─ test_unified_api.py  # 网关协议测试
└─ integration/
   └─ test_unified_api_flow.py  # 真实 HTTP 的核心联调测试
```

fixture 应使用随机可用本机端口、只监听 loopback、测试结束后可靠关闭。测试入口仍是 `python -m pytest -q`；可再以标记区分 `unit`、`protocol`、`integration`，但不强制引入新的测试框架。

CLI 黑盒演示保留为核心的最小验收。假 API 的第一版不要求增加常驻 CLI 子命令，以免把测试工具误包装成产品功能；当需要人工排障演示时，再单独评估一个仅开发环境可用的命令。

## 执行记录（2026-09-03）

- 已实现标准库 loopback HTTP 服务、场景脚本、请求观测和服务端副作用与客户端响应分离。
- 已通过真实 `UnifiedApiGateway` 的双页完成、提交响应丢失后的幂等恢复、结果限流重试、结果重放、取消和协议错误联调测试。
- 关系型持久化提升验收仍复用少量高价值场景，不把全部 HTTP 场景乘以全部数据库矩阵。

## 通过标准

- 假 API 只在测试依赖树中，生产包无需导入或安装 Web 框架。
- 常规单元测试无需启动 HTTP 服务；协议与联调测试真实走过 loopback HTTP。
- 每个故障场景由有名称的 `ApiScenario` 表达，不使用等待时间或分散的布尔开关控制。
- 提交幂等、游标重放、终态核对、限流重试和中断恢复均有自动化断言。
- 假 API 与 [统一采集 API 接入前提](../contracts/统一采集API接入前提.md) 的重叠范围明确；新增真实 API 契约要求时，同步更新两份文档与相应场景。

## 非目标

- 不模拟真实网站、浏览器、反爬、验证码或 LLM 平台。
- 不以假 API 的实现细节替代供应商联调或生产环境验收。
- 不将测试脚本、控制接口或故障注入能力暴露为 `monitoring-kit` 的公共能力。
- 不在没有真实需求时建立 Docker、消息队列、Webhook 或多服务测试环境。
