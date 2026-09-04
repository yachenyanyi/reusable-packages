# website-collection-kit

`website-collection-kit` 是一个可复用的网站检查与公开内容采集组件。它从一个或多个网站入口出发，在明确的范围和资源预算内发现页面、判断页面用途、提取标准内容、保留采集证据，并给出可解释的覆盖报告。

它面向“已知网站的持续监测”，不是全互联网爬虫平台，也不承诺证明已经找到网站的每一个页面。

## 对外能力

- 检查站点结构并生成 Site Profile 草案；
- 采集一个网站范围内的公开页面；
- 对指定 URL 进行定点复查；
- 统一处理 URL 规范化、范围控制、去重、抓取回退、内容提取、页面分类和部分失败；
- 输出版本化结果、原始证据引用和覆盖信息。

项目关键词、AI 判断、网页历史、变化识别、问题整改和报告不属于本组件。

## 文档入口

- [文档索引](docs/README.md)
- [设计简报](docs/foundation/01-设计简报.md)
- [范围与非目标](docs/foundation/02-范围与非目标.md)
- [领域语言](docs/foundation/03-领域语言.md)
- [模块边界与依赖](docs/architecture/01-模块边界与依赖.md)
- [检查与采集流程](docs/architecture/02-检查与采集流程.md)
- [Site Profile](docs/architecture/03-Site-Profile.md)
- [公共契约](docs/contracts/01-公共契约.md)
- [架构决策](docs/decisions/0001-初始架构决策.md)
- [开发路线与验收](docs/development/01-开发路线与验收.md)
- [使用示例](docs/development/02-使用示例.md)

首版实现使用 Python 标准库、HTTPX 和 Playwright/CDP 适配器；Crawlee 仍保留为未来的可替换适配器候选。这些技术选择不改变本文档定义的公共语义。
