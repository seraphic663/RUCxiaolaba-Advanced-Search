# 迁移工具生命周期

迁移脚本不进入 Web、crawler 或 scheduler 的正常运行路径。运行前必须备份并在副本上验证；Railway 主库迁移优先使用 `tools.operations.compact_runtime_db` 的 plan/migrate/verify/swap 流程。

| 工具 | 生命周期 | 当前用途 |
|---|---|---|
| `build_slim_sqlite.py` | recovery | 从旧全量库或现有瘦身库生成替换库 |
| `rebuild_sqlite_search_index.py` | recovery | SQLite FTS 缺行或损坏时重建 |

已被当前 runtime schema 和 compact 流程覆盖的一次性迁移不再保留在主分支，需要核查历史处理时从 Git history 恢复。
