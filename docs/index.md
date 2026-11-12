# 文档地图

## architecture/ — 系统设计

| 文件 | 回答什么问题 |
|------|-------------|
| [overview](architecture/overview.md) | 项目是什么、数据怎么流、核心文件有哪些 |
| [data-model](architecture/data-model.md) | SQLite 表结构、索引、数据规模 |
| [api-reference](architecture/api-reference.md) | 所有已知 API 端点、参数、响应码 |
| [refactoring](architecture/refactoring.md) | 当前目录职责、兼容入口、冗余判断和文件生命周期 |
| [module-map](architecture/module-map.md) | 核心 Python 文件的用途、调用方式、副作用和测试 |

## operations/ — 如何运行

| 文件 | 回答什么问题 |
|------|-------------|
| [crawler](operations/crawler.md) | 当前唯一爬虫运维事实源：命令、请求成本、队列、停止条件、配额和暂停 |
| [railway](operations/railway.md) | 部署、Volume、环境变量、健康检查 |

## 开发与工具

| 文件 | 回答什么问题 |
|------|-------------|
| [benchmark README](../tools/benchmarks/README.md) | 如何运行搜索、游标和 Bigram 性能测试 |
| [tools README](../tools/README.md) | 工具分类、稳定性、权限边界和迁移生命周期 |
