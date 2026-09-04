# Site Profile

## 目的

Site Profile 把网站稳定差异从代码中移到可版本化、可审查的数据中。它帮助通用引擎理解“这个逻辑网站的范围是什么、哪些路由有意义、哪些页面需要渲染、字段在哪里”，但不承担业务检测规则。

## 所有权

- Profile 的创建、审核、发布、回滚和项目归属由应用或统一 API 宿主管理。
- 本组件负责 schema、校验、运行时冻结、匹配和草案生成。
- 长三角项目的 Profile 实例放在 `apps/长三角监测/`，不能写入公共包源码。

## 第一版能力

```text
profile_id / version
site_ref
seed_urls
scope_rules
route_patterns
exclusion_rules
discovery_hints
rendering_hints
content_hints
attachment_policy
registered_strategy_refs
```

字段表达的应是站点事实和采集意图，不是供应商部署信息。例如可以声明某类路由“需要渲染后才能出现列表”，但不能填写 CDP 地址或浏览器用户目录。

## 规则边界

允许：

- host、path、query 的允许和排除规则；
- 列表、详情、分页和栏目路由模式；
- 声明式字段提示（当前支持受限 CSS 选择器：标签、`#id`、`.class`、`tag.class`、`[attr=value]`；JSON-LD 仅自动读取日期）；
- 已知 sitemap、RSS、栏目入口和公开接口提示；
- 内容类型、附件后缀和大小政策；
- 具名、版本化、启动时显式注册的策略引用。

禁止：

- 任意 JavaScript、Python、shell 或表达式求值；
- 凭证、Cookie、管理员账号、CDP 地址和代理密码；
- 关键词、AI prompt、风险规则和报告字段；
- 数据库查询、队列名、重试次数等运行基础设施配置；
- 未声明 schema 的自由格式 metadata。

## Profile 快照

每次检查或采集必须使用冻结的 Profile 版本。运行中发布新版本不得改变已经开始的 Collection。结果记录 `profile_ref`，以便解释当时为何包含、排除或分类某个页面。

## 通用规则与代码策略

默认通过声明式 Profile 解决差异。只有同时满足以下条件才增加代码策略：

1. 配置无法安全、清晰地表达该行为；
2. 行为包含稳定算法或协议，而非单个选择器；
3. 策略有具名版本、明确输入输出和独立测试；
4. 策略在启动时显式注册；
5. 缺失策略会产生清晰的 Profile 不兼容错误。

单站临时特例先留在项目应用中。第二个真实网站验证其复用价值后，才考虑提炼到公共策略。

## 草案与发布

`inspect_site` 只生成 `ProfileDraft`，不得自动覆盖已发布 Profile。草案应包含推断依据、置信度、未知项和与现有版本的差异，由宿主或人工审核后发布。
