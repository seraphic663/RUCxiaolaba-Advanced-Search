# Crawler metrics ledger

`crawler_history.csv` 是数量历史的事实源，采用一行一个指标的长表结构。它记录当前自动快照，也保存旧报告导入后的结构化数据。

`crawler_sources.csv` 是来源清单，记录每个旧 JSON、HTML、PNG、SVG 或 Markdown 文件的时间范围、导入方法和质量。JSON 原文保存在 history ledger 的 `metric_name=raw_json` 行中；HTML/SVG 文本保存在来源清单的 `raw_content` 字段；PNG-only 文件在完成数字化前标记为 `visual_only`。

主要字段：

- `observed_at`：指标观察时间；
- `metric_group` / `metric_name`：指标类别和名称；
- `task_type` / `status`：队列任务路由和状态；
- `value` / `unit`：指标值和单位；
- `method`：`exact`、`backcast`、`legacy_import` 等；
- `quality`：`exact`、`estimated`、`structured_legacy`、`visual_only` 等；
- `source_id`：来源清单中的稳定标识；
- `extra_json`：该指标的补充字段或原始 JSON payload。

`reports/generated/` 是可删除的派生输出。删除后使用 `crawler_metrics.py render` 从本目录的 CSV 重新生成。
