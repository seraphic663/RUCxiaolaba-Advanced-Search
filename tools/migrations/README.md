# 迁移工具生命周期

迁移脚本不进入 Web、crawler 或 scheduler 的正常运行路径。运行前必须备份并在副本上验证。注意：当前 `tools.operations.compact_runtime_db` 仍按旧版精简 schema 生成替换库，尚未证明会保留当前 crawler queue、ID 台账、观察日志、删除记录和其他运行状态；在完成全量 schema/state 对照前，Railway 主库只能使用它的 `plan --quick-check` 做只读检查，不得执行 `migrate`、`verify` 或 `swap`。

| 工具 | 生命周期 | 当前用途 |
|---|---|---|
| `build_slim_sqlite.py` | recovery | 从旧全量库或现有瘦身库生成替换库 |
| `rebuild_sqlite_search_index.py` | recovery | SQLite FTS 缺行或损坏时重建 |

任何未来的替换库流程都必须在副本上逐项核对：表和列、posts/comments 行数、queue/ledger/run history、FTS 行数、Bigram/Symbol `source_rows`、代表性搜索结果、服务重启后的可读性，以及旧库回滚路径。

已被当前 runtime schema 和 compact 流程覆盖的一次性迁移不再保留在主分支，需要核查历史处理时从 Git history 恢复。
