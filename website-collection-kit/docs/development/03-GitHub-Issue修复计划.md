# GitHub Issue 修复计划

- 状态：已实施并完成本地回归验证
- 制定日期：2026-09-04
- 适用版本：`website-collection-kit` v0.1 公共契约冻结前
- 范围：GitHub Issues #7–#12

## 目标

在统一采集 API 正式依赖本组件前，修复范围收窄、URL 身份、网络安全、资源预算、robots/sitemap 以及公共契约一致性问题。修复后，调用方仍只需要表达“检查站点”“采集站点”或“定点复查”，不需要理解内部 URL 队列、DNS、robots 分组、计数器或浏览器拦截顺序。

本轮不是增加业务功能，而是把已经承诺的边界变成可验证的不变量。

## Issue 归属

### 完全由本组件负责

- [#8 分离请求 URL 与 canonical identity](https://github.com/yachenyanyi/reusable-packages/issues/8)
- [#9 收紧资源预算与 Coverage/Usage 计数语义](https://github.com/yachenyanyi/reusable-packages/issues/9)
- [#10 修正 robots.txt 与 sitemap 探测语义](https://github.com/yachenyanyi/reusable-packages/issues/10)
- [#11 在 v0.1 冻结前收敛公共契约与实现漂移](https://github.com/yachenyanyi/reusable-packages/issues/11)
- [#12 修复 Scope query 交集反向放宽](https://github.com/yachenyanyi/reusable-packages/issues/12)

### 共同负责，但本轮只实现组件侧边界

[Issue #7](https://github.com/yachenyanyi/reusable-packages/issues/7) 的以下内容属于本组件：

- 页面采集范围与浏览器子资源网络范围分离；
- 默认拒绝 loopback、私网、link-local、metadata 等非公开地址；
- scheme、origin、端口和重定向边界；
- HTTP 与 Playwright/CDP 适配器使用同一网络安全语义；
- 浏览器默认阻止有副作用的方法、service worker、越界 popup、WebSocket 和下载旁路。

以下内容不下沉到本组件：

- 统一 API 的用户、租户、RBAC、站点授权和审计；
- CDP 节点池、代理池、配额和跨节点调度；
- 防 DNS rebinding 的部署级受控 DNS、网络命名空间、防火墙或出口代理。

组件需要明确声明部署级保证的前置条件，但不伪装成完整的基础设施沙箱。

### 不属于本轮

- #1–#5 属于 `monitoring-kit` 的运行时、历史、投递和租户隔离；
- #6 已关闭；
- 长三角项目的站点清单、检测规则和 AI 判断属于应用层。

## 复杂度目标

- 把 query 三态、请求地址与身份地址、网络访问安全、预算计数和 robots 标准语义各自放到唯一所有者中。
- 保持 `WebsiteCollectionKit` 的三个意图操作不变，不向调用方暴露内部执行步骤。
- 让 HTTP 与 Playwright/CDP 共用安全规则，但不互相依赖。
- 在 v0.1 冻结前删除无运行语义的公共字段，避免统一 API 依赖占位契约。
- 让每个 Coverage/Usage 数字都能由一次确定的状态转换解释和测试。

## 方案比较

### 方案 A：每个 Issue 局部打补丁

在现有条件分支中分别修正 query、robots、计数和浏览器路由。

- 优点：单次改动小。
- 缺点：同一个不变量会继续散落在 `contracts.py`、`url_policy.py`、`collection.py` 和两个适配器中；修好一个入口仍可能漏掉另一个入口。
- 结论：不采用。

### 方案 B：一次性重写采集引擎并重排目录

同时拆分 frontier、fetcher、parser、classifier、budget manager 和 security manager。

- 优点：目录看起来更细。
- 缺点：改动面过大，容易产生同形透传层；难以判断行为变化来自修复还是重写。
- 结论：不采用。

### 方案 C：按不变量分批加深现有模块

保留三个公共意图操作，按依赖顺序完成 Scope、URL 身份、网络策略、运行计量、robots/sitemap 和最终契约冻结。只有当一组知识有独立规范和变化原因时才拆出内部模块。

- 最深模块仍是 `WebsiteCollectionKit`，负责一次采集的完整结果。
- `UrlPolicy` 独占请求地址解析、身份规范化和 Scope 判断。
- 新增的网络策略由 HTTP 与浏览器适配器共同使用，但不决定 crawl frontier。
- robots 规则拥有 RFC 语义和 sitemap 来源，不再用空规则隐式表示所有状态。
- 运行计量仍由 collection 核心拥有，不建立对外 `BudgetManager`。
- 结论：采用。

## 目标模块边界

### `contracts.py`

负责公共值对象和序列化语义：

- query 范围三态；
- page origin/scope；
- `Budget`、`CoverageReport`、`Usage` 的稳定字段；
- requested/final/canonical 的公共含义；
- 删除未实现的公共占位字段。

它不负责 DNS、HTTP、浏览器或 robots 解析。

### `url_policy.py`

负责把一个原始发现地址转换为两个不同结果：

- `request_url`：实际请求地址，尽量保留 query 顺序、重复 key、编码和必要参数；
- `canonical_url`：仅用于一次采集内的范围判断和去重身份。

它不发起网络请求，也不决定公网/私网地址。

### `network.py`（新增内部策略模块）

负责 scheme、origin、端口、IP 分类和只读 method 判断。HTTPX 与 Playwright/CDP 适配器依赖它，核心采集流程不依赖具体浏览器对象。

DNS 解析与连接仍由适配器执行；部署级出口隔离由宿主保证。

### `robots.py`（新增内部策略模块）

负责：

- RFC 9309 User-Agent group 选择和规则合并；
- Allow/Disallow 最长匹配；
- robots 获取状态；
- robots 声明的 sitemap 及来源。

它不负责网络获取、长期缓存或 sitemap XML 遍历。

### `collection.py`

继续负责 frontier、预算、重试、辅助发现、证据、部分失败和最终结果。运行计量作为 collection 内部协作者存在；除非实现后仍有明显独立不变量，否则不创建公开 `AccountingService` 或 `BudgetManager`。

### `adapters/httpx.py` 与 `adapters/playwright.py`

负责把同一网络策略落实到各自技术：

- HTTP 连接前的地址检查、响应大小和异常转换；
- 浏览器 context 级路由、只读 method、service worker、popup、WebSocket 和下载约束；
- 不改变 Scope，不决定业务状态。

## 实施顺序

### 阶段 0：建立修复基线

目标：避免后续修复掩盖已有行为。

- 固定当前 107 个测试为基线；
- 为 #7–#12 的每个已确认问题先增加失败测试；
- 保存公共 dataclass 和 `to_payload()` 的基线快照；
- 每个阶段保持包可导入、可构建、已有测试可运行。

完成门槛：每个 Issue 至少有一个能在修复前稳定失败的测试。

### 阶段 1：修复 #12 的 query 交集不变量

选择显式三态，而不是增加隐藏布尔标志：

```text
allowed_query_keys = None  -> 不限制 query key
allowed_query_keys = ()    -> 不允许任何 query key
allowed_query_keys = (...) -> 只允许声明的 key
```

改动：

- 将 `Scope.allowed_query_keys` 改为 `tuple[str, ...] | None`；
- `UrlPolicy` 在值不是 `None` 时执行白名单判断，空 tuple 因而只接受无 query 的 URL；
- `restrict_with()` 实现 unrestricted、restricted 和空交集的完整集合运算；
- 更新嵌套序列化和 Scope 文档。

测试：

- unrestricted 与 restricted 的交集；
- 两个白名单有交集和无交集；
- 空交集不能接受任意 query；
- host/path/scheme/query 组合测试证明结果不会扩大任一输入 Scope。

这是阻塞任何统一 API 接入的 P0 修复。

### 阶段 2：实现 #8 的请求 URL 与身份 URL 分离

改动：

- `UrlDecision` 同时返回 `request_url` 与 `canonical_url`；
- `_FrontierItem` 分别保存实际请求地址和 canonical key；
- 同一 canonical key 的后续原始地址只合并 alias 与 discovery source，不重复产生主页面；
- 明确 `PageCandidate.url`、`WebsitePage.url`、`final_url` 和 `canonical_url` 的含义；
- 重定向后重新执行 Scope 和网络策略判断。

测试：

- query 顺序、重复 key、key-only、空值和 tracking 参数；
- Unicode path、已编码 path、`%2F`、`%25` 和非法 percent escape；
- query-sensitive/signed URL 不被 canonicalization 改写后再请求；
- 两个 raw URL 对应一个 canonical 时只产生一个主结果且来源可追溯。

hash route 不做通用猜测；出现真实站点需求后再通过具名策略扩展。

### 阶段 3：实现 #7 的组件侧网络安全边界

先稳定页面范围，再约束适配器网络访问：

- 将现有 `Scope` 明确定义为“可进入 frontier 的页面范围”；
- 为 seed 保存明确 origin，默认不因 host 相同而开放其它 scheme/port；
- 新增公共网络默认策略，拒绝 loopback、私网、link-local、multicast、unspecified 和已知 metadata 地址；
- IP literal 和 DNS 解析结果都执行检查；
- 页面导航必须满足 Scope，浏览器子资源可以使用独立的公开网络策略，但不会进入 frontier；
- Playwright 使用 context 级拦截并默认阻止有副作用的 method、service worker、越界 popup、WebSocket 和下载；
- HTTP 与浏览器重定向都不能绕过 origin/IP 检查。

测试：

- IPv4、IPv6、localhost、私网、link-local 和 metadata；
- 同 host 不同端口、HTTP/HTTPS 切换和外部重定向；
- 浏览器 POST/PUT/PATCH/DELETE、popup、service worker、WebSocket 和下载；
- 公共 CDN 资源可以按资源策略加载，但不会成为待采集页面。

边界说明：组件的解析前检查不能单独消除 DNS rebinding 的连接时竞争；生产统一 API 必须配合受控 DNS/出口网络。该责任写入端口契约和部署文档。

### 阶段 4：实现 #9 的预算和计数语义

统一所有 AcquisitionPort 调用入口，使页面、robots、sitemap、feed、重定向和 retry 都经过同一计量点。

公共预算评估增加：

- `max_requests`：全部获取尝试；
- `max_total_bytes`：全部已接收响应体；
- `max_rendered_pages`：浏览器成本需要组件约束时启用。

最终字段以契约评审结果为准，但不能保留无界辅助请求。

计数定义：

- `candidate_count`：已接受的唯一 canonical 候选数；
- `visited_count`：实际进入页面 fetch 的唯一页面数；
- `page_count`：成功形成 `WebsitePage` 的数量；
- `failed_count` / `failed_pages`：唯一页面级失败数，不等于 issue 条数；
- `request_count`：包含辅助请求和 retry 的全部适配器调用；
- `bytes_received`：全部响应体字节；
- `excluded_count`：fetch 前被 Scope、深度、robots 或安全策略排除的数量。

测试：

- robots 禁止和超深度候选不消耗 page budget；
- retry 增加 request count，但不重复增加唯一失败页面；
- 同页多个 issue 不重复计数；
- sitemap index 不能绕过 request/time/candidate/byte 预算；
- `MemoryEvidenceStore` 的内存增长受总字节预算约束；
- 预算耗尽仍保留已完成页面并返回明确 stop reason。

默认预算值在代表站点基准测试后确定，不以当前适配器单响应上限乘页面数直接推导。

### 阶段 5：实现 #10 的 robots 与 sitemap 语义

改动：

- 固定 crawler product token，并使默认 User-Agent 与它一致；
- 支持连续多个 User-Agent、专用 group、多个匹配 group 合并和 `*` 回退；
- 4xx robots 表示 unavailable，可以继续；
- 5xx、timeout 和网络不可达表示 unreachable，在 `respect_robots=True` 时不默认为 allow-all；
- `respect_robots=False` 仍是调用方明确选择的跳过策略；
- robots 声明、Profile 声明和 conventional 猜测的 sitemap 分开保存来源与失败语义；
- conventional sitemap 探测失败只记录 probe miss/blind spot，不把正常采集错误标记为 partial。

测试：

- RFC group 选择、合并、最长匹配和同长度 Allow 优先；
- robots 404、500、timeout 和显式跳过；
- authoritative sitemap 无效产生 issue；
- conventional sitemap 返回 HTML/404 不产生假失败；
- sitemap 来源可在 Coverage 中解释；
- 所有 robots/sitemap 请求进入阶段 4 的统一预算和计量。

### 阶段 6：完成 #11 的 v0.1 契约收敛

#11 贯穿前述阶段，最后统一关闭。

确定以下契约选择：

- Python `InspectionSpec` / `CollectionSpec` 不增加 `schema_version`；Python 类型版本由包版本管理，wire-level 版本由统一 API DTO 拥有；公共文档删除不存在的输入字段；
- 保留输出 `schema_type`，因为输出 payload 会跨进程传输；
- 删除没有真实运行语义的 `registered_strategy_refs`，等出现第二个真实策略后再按显式 registry 设计；
- AcquisitionPort 的取消语义明确为 asyncio 结构化任务取消，不增加浅层 `cancel()` 透传方法；
- Site Profile v0.1 只承诺附件扩展名策略；响应大小和总字节属于适配器/预算约束；
- 审计 `__all__`，删除无调用场景或无明确 SPI 用途的公开异常和类型。

验收：

- 所有公共 dataclass 有字段与序列化快照测试；
- 文档字段与 Python 实现逐项一致；
- 公共配置不存在“可以传入但完全不生效”的字段；
- 生成 v0.1 接入说明，明确统一 API 需要做的 DTO 转换和部署安全前置条件。

## 交付拆分

建议按以下小批次提交，每批都可独立审查和回归：

1. `fix(scope): preserve empty query intersections` — #12；
2. `refactor(url): separate request URL from canonical identity` — #8；
3. `feat(network): enforce public read-only acquisition policy` — #7 组件侧；
4. `fix(accounting): bound requests and align coverage usage` — #9；
5. `fix(discovery): implement robots and sitemap provenance semantics` — #10；
6. `docs(contracts): freeze website collection v0.1 contract` — #11。

不要把六个 Issue 合成一个不可审查的大提交。每批完成后更新对应 GitHub Issue 的测试证据和提交链接，再关闭该 Issue。

## 总体验收门槛

- 全部原有测试通过，并新增针对 #7–#12 的回归测试；
- 包可在不安装 HTTP/Playwright extras 时导入；
- HTTP 和 Playwright/CDP 适配器通过共同的安全与获取契约测试；
- `git diff --check`、wheel 构建和锁文件检查通过；
- 公共 API、文档和序列化快照一致；
- 任何被有效 Scope 接受的 URL 都不会由 Profile 反向放宽；
- 任意采集来源都不能绕过请求数、字节数、时长和页面预算；
- 统一 API 在接入文档中能明确区分：组件保证、宿主保证和基础设施保证。

## 暂缓项

以下内容只有出现真实需求后再设计：

- 动态策略插件系统；
- 长期 robots 缓存；
- hash-route 通用执行平台；
- 浏览器池、CDP 分流和跨节点调度；
- 长期 Document identity、历史变化与业务风险分类；
- 为了缩短文件行数而机械拆分 collection 或 interpretation。

## 实施结果

本轮保留方案 C 的模块边界，完成 #7–#12 的组件侧修复：

- `Scope` 使用 query 三态和明确 origin；请求 URL 与 canonical identity 分离，并保留 alias；
- HTTPX 与 Playwright/CDP 共用公开只读网络策略，浏览器 context 级路由阻断写方法、service worker、WebSocket、下载和 popup 旁路；
- `Budget` 增加请求、总字节和渲染页上限，Coverage/Usage 统一统计辅助请求、重试和唯一页面；
- robots group、fail-closed 状态及声明/猜测 sitemap provenance 已分开；
- 删除 `registered_strategy_refs`、`OperationConflictError` 等没有运行语义的 v0.1 占位；
- 补充 Issue 回归测试，组件全量测试为 136 passed。

验证命令和结果：

```text
python -m pytest -q                         136 passed
python -m compileall -q src                  passed
python -m pytest -q ../monitoring-kit/tests  94 passed, 20 skipped
```

构建、导入边界、`git diff --check` 和锁文件检查在提交前继续执行；真实网站和部署级 DNS/出口隔离仍按“暂缓项”和宿主责任处理。
