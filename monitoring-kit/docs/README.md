# 可复用内容监测系统文档

这套文档服务于一类系统，而不是某一个客户项目：系统持续观察外部数据源，保留原始证据和历史版本，识别变化，进行规则或 AI 分析，并把发现转化为报告、预警或处置闭环。

站点监测、GEO 效果监测和舆情监控是当前验证场景，不是核心架构的边界。

## 文档结构

```text
docs/
├─ foundation/   架构目标、领域语言、总览、边界、运行形态和演进决策
├─ architecture/ 一模块一文档，定义模块契约和隐藏知识
├─ contracts/    与外部系统协作的稳定接入契约
└─ projects/     具体客户或交付项目的事实、约束和计划
```

## 推荐阅读顺序

1. [愿景与边界](foundation/01-愿景与边界.md)
2. [领域语言](foundation/02-领域语言.md)
3. [架构总览](foundation/03-架构总览.md)
4. [模块边界与依赖](foundation/04-模块边界与依赖.md)
5. [模块文档索引](architecture/README.md)
6. [运行与部署形态](foundation/05-运行与部署形态.md)
7. [演进路线](foundation/06-演进路线.md)
8. [架构决策](foundation/07-架构决策.md)
9. [核心数据契约](contracts/README.md)
10. [统一采集 API 接入前提](contracts/统一采集API接入前提.md)
11. [代码库扩展策略](foundation/08-代码库扩展策略.md)
12. [公共 API 与扩展 SPI](foundation/09-公共API与扩展SPI.md)
13. [代码目录与扩展布局](foundation/10-代码目录与扩展布局.md)
14. [v0.1 核心开发简报](foundation/11-v0.1核心开发简报.md)
15. [v0.2 关系型持久化适配层开发简报](foundation/12-v0.2关系型持久化适配层开发简报.md)
16. [假统一采集 API 联调测试计划](foundation/13-假统一采集API联调测试计划.md)
17. [v0.2 可靠性修复与代码收敛计划](foundation/14-v0.2可靠性修复与代码收敛计划.md)
18. [v0.3 事务型 Outbox 开发计划](foundation/15-v0.3事务型Outbox开发计划.md)
19. [scope 隔离契约强化开发计划](foundation/16-scope隔离契约强化开发计划.md)
20. [运行时公平调度与并发配额开发计划](foundation/17-运行时公平调度与并发配额开发计划.md)
21. [核心范围与开发总计划](foundation/18-monitoring-kit核心范围与开发总计划.md)

## 项目档案

具体项目档案属于工作区的 `apps/<project>/`，不放入这个公共组件库。

## 维护规则

- 通用原则只写在 `foundation/`。
- 每个可复用模块在 `architecture/` 中只有一份定义文档。
- 外部系统必须满足的协议、状态和可靠性要求写入 `contracts/`。
- 客户名称、真实站点、供应商约束和交付计划只写在 `projects/`。
- 项目发现的通用模式先记录为候选；至少经过第二个场景验证后，再提升为共享模块能力。
- 文档描述领域接口，不把 ORM、HTTP 客户端、队列消息或供应商 DTO 当成公共模型。
