# 文档地图

## architecture/ — 系统设计

| 文件 | 回答什么问题 |
|------|-------------|
| [overview](architecture/overview.md) | 项目是什么、数据怎么流、核心文件有哪些 |
| [data-model](architecture/data-model.md) | SQLite 表结构、索引、数据规模 |
| [api-reference](architecture/api-reference.md) | 所有已知 API 端点、参数、响应码 |
| [refactoring](architecture/refactoring.md) | 当前工程化目录、兼容范围、测试与开发规则 |

## operations/ — 如何运行

| 文件 | 回答什么问题 |
|------|-------------|
| [crawler](operations/crawler.md) | 爬虫一条龙：cookie→端点→子命令→写锁→Railway 调度 |
| [railway](operations/railway.md) | 部署、Volume、环境变量、健康检查 |
| [aliyun-migration](operations/aliyun-migration.md) | 备选方案：迁移到阿里云 ECS（未实施） |

## features/ — 功能模块

| 文件 | 回答什么问题 |
|------|-------------|
| [search-performance](features/search-performance.md) | 搜索性能实测数据与优化方向（草案） |

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
