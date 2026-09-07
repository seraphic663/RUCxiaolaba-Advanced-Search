# 本地抓包辅助

这些文件只用于在本人设备上观察小程序正常请求、确认接口格式或取得本人会话 cookie，不进入 Web、crawler 或 Railway 运行路径。

双击或在 PowerShell 中运行下面的批处理即可。它会为当前用户安装缺失的 mitmproxy，保存原来的 Windows 代理设置，临时使用 `127.0.0.1:8899`，关闭抓包后自动恢复：

当前 Windows 机器已将 mitmproxy 安装在 `D:\application\mitmproxy-env`，启动脚本会优先使用该隔离环境。

```powershell
.\tools\capture\start_proxy.bat
```

目标小程序请求出现后，过滤器会先保存一个临时候选；当同一请求收到正常 API 成功响应后，才自动把会话保存到 `data/config_small.txt`。它兼容当前观察到的 `ys_ysxy_sess` 和历史名称 `ys7_ysxy_session`，并保留实际捕获到的名称。因此不会因为失败请求把旧配置覆盖掉。只在本机使用，不要把文件内容发到聊天或提交到仓库。

双击 `start_proxy.bat` 时不会打开抓包面板：启动后只需打开微信小程序并刷新或浏览任意页面。默认会在捕获并验证成功后自动关闭 mitmweb 并恢复原代理；5 分钟内没有成功响应则退出并保留原来的配置。

默认是 `new` 模式，写入 `data/config_small.txt`。同一个脚本也支持 `old` 模式：

```powershell
.\tools\capture\start_proxy.bat -Mode old
```

`old` 模式写入 `data/config.txt`，两种模式都会在 `data/capture_status.json` 中记录对应标签；状态文件不包含 cookie 值。

需要手动观察面板时可以直接运行 PowerShell 脚本；面板使用独立的 Chrome 临时配置，不会关闭你原来打开的标签页：

```powershell
.\tools\capture\start_proxy.ps1 -KeepOpen -TimeoutSeconds 0
```

如果不需要打开面板，可以加 `-NoPanel`；抓包仍会继续运行并自动验证会话。

如果 Windows 代理在脚本异常终止后没有自动恢复，重新运行一次批处理即可触发残留备份恢复。不要在抓包期间手动修改系统代理。

`mitm_filter.py` 只记录目标域名的请求到同目录的 `captured_requests.jsonl`；请求头中的 Cookie、Authorization 等敏感字段会被遮蔽，但请求/响应正文仍可能包含个人信息。该文件已由 `*.jsonl` 忽略规则排除，使用后应及时删除，不得提交或分享。自动流程另写入 `data/capture_status.json`，只包含状态、接口路径和 HTTP 状态码，不包含会话值。
