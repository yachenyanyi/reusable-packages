# monitoring-kit

一个可嵌入、也可独立运行的外部信息持续观察代码库。

当前版本已经实现稳定契约、可靠运行、内容历史、扩展注册、统一采集 API 网关和 CLI 验收闭环；不包含任何具体监测业务。核心目标是隐藏任务执行、上游 API、幂等、恢复、内容历史和变化识别细节，让站点、GEO、舆情等能力以扩展方式接入。

## 目录

```text
src/monitoring_kit/
├─ contracts/      稳定公共契约
├─ collection/     可靠运行一次监测
├─ history/        Document、Snapshot、ChangeEvent
├─ runtime/        唤醒、恢复和显式装配支持
├─ extensions/     可选场景与交付能力
├─ adapters/       上游 API、存储和基础设施实现
└─ integrations/   宿主平台绑定
```

架构文档位于 [docs](docs/README.md)。

## 设计原则

- 核心不依赖具体 Web 框架、ORM、队列、数据库或供应商 SDK。
- 业务使用公共 API；适配器实现扩展 SPI。
- 扩展载荷使用带类型和版本的 `TypedEnvelope`。
- 重试、回退、游标、Attempt 和事务顺序不泄漏给调用方。
- 默认显式装配，不自动扫描或热加载插件。

## 安装与验收

```powershell
python -m pip install -e ".[test]"
monitoring-kit demo
python -m pytest -q
```

CLI 的 `demo` 只使用内存网关、内存存储和一个通用示例策略，用来验证核心链路，不代表任何客户业务。

## 最小装配方式

宿主需要显式注册一个 `CollectionAdapter` 和与内容类型对应的 `ContentPolicy`，再注入 `RunStateStore`、`HistoryStore`、`UpstreamJobGateway`：

```python
registry = ExtensionRegistry()
registry.register_collection_adapter(collection_adapter)
registry.register_content_policy(content_policy)

history = ContentHistory(history_store, registry)
engine = CollectionEngine(run_store, history, registry, upstream_gateway)

ref = engine.submit_run(request, context)
engine.wake()
summary = engine.get_run(ref.run_id, context.scope_key)
```

`submit_run` 只接受运行请求；`wake` 才推进上游任务。轮询、重试、回退、游标和结果核对不需要宿主协调。

公共契约和扩展 SPI 见 [文档索引](docs/README.md)。
