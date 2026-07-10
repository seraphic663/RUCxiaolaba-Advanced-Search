# 工具目录

`tools/` 保存不进入 Web 请求主线和 scheduler 自动调度的辅助命令。

| 路径 | 类型 | 稳定性与边界 |
|---|---|---|
| `operations/` | 运维命令 | 备份、Symbol 重建和数据库维护；可能读取敏感数据或替换数据库，按文档验证 |
| `migrations/` | 迁移与恢复 | 不进入 runtime；具体生命周期见 `migrations/README.md` |
| `audits/` | 专项审计 | 只为明确审计任务运行，结果不自动进入服务 |
| `capture/` | 本地抓包 | 只观察本人设备流量，输出可能含敏感信息，不进入生产路径 |
| `benchmarks/` | 性能基准 | 只手动运行，不属于普通测试；数据前提见该目录 README |
| `demo/` | 合成演示数据 | 可重复生成，不读取真实主库 |

常用入口：

```powershell
python -m tools.audits.probe_upstream
python -m tools.operations.build_symbol_index --posts-db data\posts.db --output data\symbol_index.db
python -m tools.operations.backup_runtime --data-dir data
```

新增工具时先判断：日常可重复运维进入 `operations/`，一次性 schema/data 变换进入 `migrations/`，验证假设进入 `audits/`，性能测量进入 `benchmarks/`。
