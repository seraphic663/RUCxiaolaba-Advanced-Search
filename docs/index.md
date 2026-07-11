# 文档地图

## architecture/ — 系统设计

| 文件 | 回答什么问题 |
|------|-------------|
| [overview](architecture/overview.md) | 项目是什么、数据怎么流、核心文件有哪些 |
| [data-model](architecture/data-model.md) | SQLite 表结构、索引、数据规模 |
| [api-reference](architecture/api-reference.md) | 所有已知 API 端点、参数、响应码 |
| [refactoring](architecture/refactoring.md) | 当前目录职责、兼容入口、冗余判断和文件生命周期 |

## operations/ — 如何运行

| 文件 | 回答什么问题 |
|------|-------------|
| [crawler](operations/crawler.md) | 当前唯一爬虫运维事实源：命令、请求成本、队列、停止条件、配额和暂停 |
| [2026-07-11 爬取现状报告](operations/crawler-status-2026-07-11.md) | 当前线上覆盖、积压、配额效率、20:21 缺评论现象和证据边界的时间点快照 |
| [railway](operations/railway.md) | 部署、Volume、环境变量、健康检查 |

## features/ — 功能模块

| 文件 | 回答什么问题 |
|------|-------------|
| [search-performance](features/search-performance.md) | 历史搜索性能基准与优化记录；当前代码以 `app/` 和测试为准 |

## audits/ — 专项审计

| 文件 | 回答什么问题 |
|------|-------------|
| [is-publisher](audits/is-publisher.md) | 楼主标签是否可靠、根因、核查 SQL、修复方案 |
| [raw-json-slimming](audits/raw-json-slimming.md) | 为什么删冗余字段、删了什么、省了多少空间 |

## 数据使用与开发

| 文件 | 回答什么问题 |
|------|-------------|
| [legal-and-data](legal-and-data.md) | 项目如何抓取、是否获得授权、是否合法以及为何不公开真实数据库 |
| [benchmark README](../tools/benchmarks/README.md) | 如何运行搜索、游标和 Bigram 性能测试 |
| [tools README](../tools/README.md) | 工具分类、稳定性、权限边界和迁移生命周期 |
