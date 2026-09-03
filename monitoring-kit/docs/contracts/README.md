# 数据契约索引

本目录定义跨模块、跨项目需要保持稳定的契约。契约不是数据库表，也不是统一采集 API 的原始 DTO。

## 设计顺序

1. [核心数据契约设计说明](01-核心数据契约设计说明.md)
2. [RunRequest](02-RunRequest.md)
3. [Observation](03-Observation.md)
4. [ChangeEvent](04-ChangeEvent.md)
5. [扩展类型信封](05-扩展类型信封.md)
6. [统一采集 API 接入前提](统一采集API接入前提.md)

## 可见性

- 公共契约：`RunRequest`、`RunRef`、`RunSummary`、`Observation`、`ChangeEvent`。
- 扩展契约：场景规格、内容载荷、变化详情及其处理器。
- 内部模型：`Item`、`Attempt`、checkpoint、锁、ORM 实体和供应商 DTO。

内部模型不承诺兼容性，也不能因为数据库或队列实现方便而升级成公共契约。
