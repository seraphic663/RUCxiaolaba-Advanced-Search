# 本地抓包辅助

这些文件只用于在本人设备上观察小程序正常请求、确认接口格式或取得本人会话 cookie，不进入 Web、crawler 或 Railway 运行路径。

双击或在 PowerShell 中运行下面的批处理即可。它会为当前用户安装缺失的 mitmproxy，保存原来的 Windows 代理设置，临时使用 `127.0.0.1:8899`，关闭抓包后自动恢复：

当前 Windows 机器已将 mitmproxy 安装在 `D:\application\mitmproxy-env`，启动脚本会优先使用该隔离环境。

```powershell
.\tools\capture\start_proxy.bat
```

目标小程序请求出现后，过滤器会把当前设备捕获到的 `ys7_ysxy_session` 自动保存到 `data/config_small.txt`；只在本机使用，不要把文件内容发到聊天或提交到仓库。

如果 Windows 代理在脚本异常终止后没有自动恢复，重新运行一次批处理即可触发残留备份恢复。不要在抓包期间手动修改系统代理。

`mitm_filter.py` 会把筛选后的请求记录到同目录的 `captured_requests.jsonl`；请求头中的 Cookie、Authorization 等敏感字段会被遮蔽，但请求/响应正文仍可能包含个人信息。该文件已由 `*.jsonl` 忽略规则排除，使用后应及时删除，不得提交或分享。
