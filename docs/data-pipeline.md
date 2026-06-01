# 数据更新流水线

`update_full.py` — 三阶段顺序执行，保持 RUC 小喇叭数据完整且评论最新。

## 数据现状

```
posts_final.csv:  60,876 帖
ID 范围:          4,000,013 ~ 4,999,230
时间跨度:         2025-12-28 ~ 2026-05-31
扫描覆盖:         4,000,000 ~ 5,000,000（100 万 ID，已全部扫描）
```

### ID 区间密度

| 区间 | RUC 帖数 | 密度 | 时间 |
|------|----------|------|------|
| 4.9M-5.0M | 5,929 | 5.9% | 2026-05 |
| 4.8M-4.9M | 5,786 | 5.8% | 2026-05 |
| 4.7M-4.8M | 5,628 | 5.6% | 2026-04~05 |
| 4.6M-4.7M | 5,820 | 5.8% | 2026-04 |
| 4.5M-4.6M | 6,036 | 6.0% | 2026-03~04 |
| 4.4M-4.5M | 5,877 | 5.9% | 2026-03 |
| 4.3M-4.4M | 6,855 | 6.9% | 2026-02~03 |
| 4.2M-4.3M | 6,460 | 6.5% | 2026-01~02 |
| 4.1M-4.2M | 6,257 | 6.3% | 2026-01 |
| 4.0M-4.1M | 6,228 | 6.2% | 2025-12 |

### 月份分布

| 月份 | 帖数 |
|------|------|
| 2025-12 | 1,830 |
| 2026-01 | 11,344 |
| 2026-02 | 5,180 |
| 2026-03 | 15,250 |
| 2026-04 | 13,047 |
| 2026-05 | 14,225 |

## 三阶段

### Phase 1 — 全量 ID 扫描

逐 ID 扫描 `/article/article/info`，判断 `community_id==4` 确定是否为 RUC 帖。多线程并发（10 workers），断点续传。

```
扫描区间: START_ID → END_ID
每帖: GET /article/article/info?id={id}
  ├─ code=0000 + community_id=4 → RUC 帖，存入 CSV
  ├─ code=0102 → 帖子不存在/已下架
  └─ code=1000 → Cookie 过期，保存断点退出
每 30 秒自动保存 checkpoint 到 .scan_checkpoint.json
Ctrl+C 安全中断
```

- **用途**: 获取全量历史数据
- **速度**: ~30 IDs/s（10 线程）
- **输出**: `posts_scan.csv`
- **断点**: `data/.scan_checkpoint.json`
- **停止条件**: 到达 END_ID

### Phase 2 — 高 ID 补扫

Phase 1 扫完后，5M 以上会不断产生新帖。Phase 2 从最新帖 ID 往下扫，直到触及已存在数据。

```
1. GET /article/article/lists?page=1 → 取最新帖 ID
2. 从最新 ID 往下逐 ID 扫描
3. 每条:
   ├─ ID 在 DB → consecutive_hit++
   │   连续 10 条 → 已触及重叠区，停止
   └─ ID 不在 DB → 查 info，新帖入库，consecutive_hit=0
```

- **用途**: 采集 Phase 1 完成后产生的新帖
- **速度**: 单线程，~3 IDs/s（有随机延迟）
- **输出**: 直接写入内存 `posts` dict（最终统一存 posts_final.csv）
- **停止条件**: 连续 10 条命中

### Phase 3 — lists2 评论更新

遍历 `/article/article/lists2`，对比每帖的 `comment_count`。变了就重抓详情覆盖旧评论。

```
lists2 page 1, 2, 3 … (逐页)
  逐帖:
  ├─ ID 不在 DB → 新帖！抓 info 追加
  ├─ comment_count 变了 → 重抓 info 覆盖
  └─ comment_count 不变 → unchanged++
  连续 10 条 unchanged → 停止（至少扫 3 页后）
```

- **用途**: 
  - 更新活跃帖的评论数据
  - 交叉覆盖 Phase 1/2 遗漏的帖（lists2 排序不同于 lists）
- **速度**: ~1 页/秒（有随机延迟）
- **输出**: 直接更新内存 `posts` dict
- **停止条件**: 连续 10 条无变化（至少扫 3 页后才生效）

## 运行

```bash
# 首次全量（Phase 1+2+3）
python update_full.py

# 日常增量（Phase 1 已完成，只跑 Phase 2+3，约 30 分钟）
python update_full.py

# 输出
# data/posts_final.csv — 最终合并文件
```

### 首次运行时序

```
Phase 1: ~6h    一次性，扫描历史
Phase 2: ~10m   补扫 5M 以上新帖
Phase 3: ~20m   更新评论
─────────────────
总计:   ~6.5h
```

### 日常增量时序

```
Phase 1: 跳过（已完成）
Phase 2: ~10m
Phase 3: ~20m
─────────────────
总计:   ~30m
```

## 数据流

```
posts_danger.csv ─────┐
  (lists 端点，2632 帖)│
                       ├──→ load_all_posts() 合并去重 → posts dict (内存)
posts_scan.csv ───────┘        │
  (Phase 1 产出，60K+ 帖)      ├── Phase 2 追加新帖
                               ├── Phase 3 更新评论
                               ↓
                          save_posts() → posts_final.csv
```

## 安全特性

- **断点续传**: Phase 1 每 30s 存 checkpoint，Ctrl+C / 关机不丢进度
- **Cookie 过期检测**: 检测到 code=1000 立即保存并退出
- **延迟控制**: Phase 2/3 有随机延迟 (0.3-0.8s)，降低 API 压力
- **防早停**: Phase 2/3 连续 10 条不变才停（非 1 条），避免误判
- **内存安全**: 60K 帖约占用 ~200MB 内存，远低于系统限制
