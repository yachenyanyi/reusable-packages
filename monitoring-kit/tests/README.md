# 测试目录

测试按契约、采集引擎、内容历史、恢复、扩展/适配器、真实 HTTP 联调和导入边界组织。

运行完整验收：

```powershell
python -m pytest -q
```

关系型适配器使用可选依赖：

```powershell
python -m pip install -e ".[relational,test]"
```

当前自动化测试会真实运行 SQLite 文件数据库和 loopback 假统一采集 API；MySQL 适配器的运行测试需要提供可测试的 MySQL 8+ 实例，不能用 SQLite 代替。设置 `MONITORING_KIT_TEST_MYSQL_URL` 后，公平调度、History 并发、Run fencing 和 Outbox 多连接测试会连接该测试库，并在每个测试前后清理 `monitoring_kit_*` 业务数据；不要把生产库作为这个变量的目标。

关系型回归测试还覆盖旧 Worker 写入围栏、History 并发指针与首次建档、SQLite 并发 schema 初始化、SQL 分页游标、MySQL DDL 类型与表选项、连接串脱敏和损坏载荷分类。运行时测试覆盖同 Worker 续租、饱和 gateway 候选扫描和未知 gateway 配额配置。没有 MySQL 实例时，MySQL 运行契约不会被伪装成通过，只执行方言编译检查。

测试中的场景适配器、内容策略和假统一采集 API 都是通用测试替身，不属于 `monitoring-kit` 的业务扩展。
