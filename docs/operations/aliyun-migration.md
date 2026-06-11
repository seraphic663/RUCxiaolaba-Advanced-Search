# 阿里云 ECS 迁移指南

> 状态: **未实施**。当 Railway 5GB Volume 不够用或成本不再合理时参考。

从 Railway Volume 迁移到阿里云 ECS（云服务器），目标：更低的月成本、更大的存储空间、更灵活的控制。

## 为什么迁移

| 维度 | Railway | 阿里云 ECS |
|------|---------|-----------|
| 月成本 | ~$20（5GB Volume + Web + Cron） | ~¥183（2核4G 经济型 e 按量付费） |
| 存储 | 5GB 固定 | 40GB 系统盘（可扩容） |
| DB 上限 | 1.8GB → 余量 ~3.2GB | 1.8GB → 还剩 ~38GB |
| 爬虫 | 需要独立 Cron 服务 | 一个 crontab 搞定 |
| 备份 | Volume 内备份空间拮据 | 可挂载额外的数据盘 / OSS |

## 规格选择

推荐配置（性价比最优）：

```text
规格:    ecs.e-c2m2.large（2核 4GiB 经济型 e）
系统:    Ubuntu 24.04 LTS
磁盘:    ESSD Entry 40GiB（默认系统盘）
地域:    北京（华北2）或杭州（华东1）
带宽:    按使用流量计费
```

### 为什么 4GiB 不是 2GiB

SQLite 不自己申请大内存，但依赖 Linux 内核的文件缓存。1.8GB 的 DB：

- **2GB 内存**：系统占用 ~500MB，仅剩 ~1.5GB 做缓存。短词 LIKE 搜索、admin 聚合查询会频繁触发磁盘 IO，体验明显慢。
- **4GB 内存**：剩余 ~3.5GB 缓存，接近 DB 大小。大部分人访问的热数据在内存里，感知体验和本地无异。

差价 ¥0.131/小时，3 个月试用期内多花约 ¥94，值得。

### 为什么 Ubuntu 不是 Alibaba Cloud Linux

Python 生态在 Ubuntu 上最无痛，pip 安装无兼容问题。Alibaba Cloud Linux 基于 CentOS，偶有依赖路径差异。

## 不推荐的预装应用

创建 ECS 时不要勾选以下任何预装应用：

```text
❌ 宝塔面板   = 多一套 PHP+Nginx，白占内存
❌ WordPress  = 博客系统，与本项目无关
❌ Docker     = 单进程 Python 项目不需要
❌ LNMP       = Linux+Nginx+MySQL+PHP，全用不上
```

选**空白系统镜像**，进去后手动安装。

## 初始化步骤

### 1. 登录并更新系统

```bash
ssh root@<公网IP>
apt update && apt upgrade -y
apt install python3-pip python3-venv -y
```

### 2. 创建应用目录

```bash
mkdir -p /app/data
```

### 3. 上传项目文件

本地打包后上传：

```powershell
# 本地（PowerShell）
git archive --format=tar.gz -o ruc-xlb.tar.gz HEAD
scp ruc-xlb.tar.gz root@<公网IP>:/app/
```

```bash
# 服务器
cd /app
tar xzf ruc-xlb.tar.gz
```

### 4. 安装依赖

```bash
cd /app
pip install -r requirements.txt --break-system-packages
```

（Ubuntu 24.04 用 `--break-system-packages` 标志跳过 PEP 668 限制，或用 venv）

### 5. 上传数据文件

**DB 文件较大（1.8GB），不要用 scp 直接传，容易中断。** 推荐 rsync：

```powershell
# 本地（需要安装 rsync 或 WSL）
rsync -avz --progress data/posts.db root@<公网IP>:/app/data/
```

或先上传到 OSS Bucket，再从服务器下载：

```bash
# 服务器端
wget -O /app/data/posts.db "https://<bucket>.<region>.aliyuncs.com/posts.db"
```

### 6. 上传配置文件

```bash
# 服务器上手动创建
echo "ys7_ysxy_session=<你的cookie>" > /app/data/config.txt
echo "<admin密码>" > /app/data/admin_password.txt
```

### 7. 验证 DB

```bash
python3 - <<'PY'
import sqlite3
conn = sqlite3.connect("/app/data/posts.db")
row = conn.execute("select count(*), max(create_time) from posts").fetchone()
print(f"posts={row[0]:,} latest={row[1]}")
conn.close()
PY
```

### 8. 启动服务

```bash
cd /app
SQLITE_DB=/app/data/posts.db python3 server.py --port 8080 &
```

确认可访问：

```bash
curl http://localhost:8080/healthz
# {"ok": true}
```

### 9. 配置安全组

在阿里云控制台 → ECS → 安全组 → 添加入方向规则：

```text
端口: 8080
来源: 0.0.0.0/0
协议: TCP
```

### 10. 配置 systemd 自启动

```bash
cat > /etc/systemd/system/ruc-xlb.service << 'EOF'
[Unit]
Description=RUC小喇叭 搜索服务
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/app
Environment=SQLITE_DB=/app/data/posts.db
ExecStart=/usr/bin/python3 -u server.py --port 8080
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable ruc-xlb
systemctl start ruc-xlb
systemctl status ruc-xlb
```

## 定时爬虫

```bash
crontab -e
```

```cron
# 新帖：每 20 分钟
*/20 * * * * cd /app && python3 crawler_db.py sync-latest --db-path /app/data/posts.db --pages 500 --min-pages 20 --stop-unchanged 300 >> /var/log/crawler_new.log 2>&1

# 活跃帖刷新：每 40 分钟
3,43 * * * * cd /app && python3 crawler_db.py sync-active --db-path /app/data/posts.db --pages 500 --min-pages 20 --stop-unchanged 300 >> /var/log/crawler_refresh.log 2>&1

# 历史补全：每天凌晨 3 点
5 3 * * * cd /app && python3 crawler_db.py scan-history --endpoint lists --db-path /app/data/posts.db --start-page 200 --pages 500 --min-pages 20 --stop-unchanged 600 >> /var/log/crawler_backfill.log 2>&1
```

## 备份策略

### 小文件每日备份

```bash
# crontab 加一条
10 2 * * * cd /app && python3 -m jobs.backup --data-dir /app/data --keep 30
```

### DB 周备份到 OSS

阿里云 OSS Bucket 是廉价对象存储，适合放 DB 快照：

```bash
# 安装 ossutil
wget https://gosspublic.alicdn.com/ossutil/1.7.19/ossutil64
chmod +x ossutil64

# 配置（需要 AccessKey）
./ossutil64 config

# 每周备份
# crontab: 0 4 * * 0
python3 -m jobs.backup --data-dir /app/data --include-db --keep 2
./ossutil64 cp /app/data/backups/$(ls -t /app/data/backups/ | head -1)/posts.db oss://<bucket>/backups/
```

## 迁移后检查清单

- [ ] `http://<公网IP>:8080` 主页能打开
- [ ] 搜索功能正常
- [ ] 评论展开正常
- [ ] `http://<公网IP>:8080/admin` 可登录
- [ ] `http://<公网IP>:8080/healthz` 返回 `{"ok": true}`
- [ ] crontab 爬虫日志正常输出
- [ ] systemd 重启后服务自启动

## 成本估算

```text
ECS 按量:     ¥0.254/小时 × 730 小时 ≈ ¥185/月
公网流量:     20GB/月免费（CDT），超额 ¥0.80/GB
系统盘:       40GB ESSD Entry 已含在 ECS 费用
OSS 备份:     按实际存储量，月均 < ¥5

总计:         ≈ ¥190/月
```

对比 Railway Volume 方案，每月节省约 60-70%。

## 注意事项

1. **安全组只开 8080**，不要开 22 端口给 0.0.0.0（用 VPN 或跳板机 SSH）。
2. **cookie 定期更新**：微信小程序 session 会过期，需手动更新 `/app/data/config.txt`。
3. **磁盘监控**：1.8GB DB 每天增长有限，但建议每月检查磁盘使用率 `df -h`。
4. **不要在线上 VACUUM**：VACUUM 需要额外 ~1.8GB 临时空间，可能撑满磁盘。如需回收空间，在本地做然后重新上传。
