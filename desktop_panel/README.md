# QQMCBridge 桌面控制台（qfluentwidgets / WinUI 风格）

仿 **Class-Widgets** 的 `qfluentwidgets` 界面（即 Windows 11 的 Fluent / WinUI 风格）：
左侧亚克力导航栏（`MSFluentWindow`）+ 圆角 `CardWidget` 卡片 + `FluentIcon` 图标 +
`InfoBar` 提示 + `PrimaryPushButton` 主按钮，视觉与 Class-Widgets 的插件广场一致。
功能对标 `webui/` 网页面板，并额外提供「机器人启停」。

通过 HTTP API 与网关通信（与 webui 同一套接口 `127.0.0.1:8080`）。

## 功能页面

| 页面 | 对应 webui | 说明 |
|---|---|---|
| 概览（HOME） | 服务器状态 | 网关运行状态 + 各后端在线人数 / TPS / 版本 / 玩家列表；顶部含「网关地址连接」与「启动/关闭机器人」按钮，每 5 秒自动刷新 |
| 互通日志（HISTORY） | 互通日志 | 网关日志，可选条数，自动刷新 |
| Python子插件（PYTHON） | Python子插件 | 列出 pymods 卡片，一键「重新加载」 |
| 后端子插件（PLUGIN） | 子插件管理 | 列出各后端 LSE 子插件，可启/停 |
| 配置（SETTING） | 完整配置编辑器 | 在线编辑 `config.json` 并保存热重载（导航底部） |
| 执行指令（SEND） | （新增） | 选择后端、转发 MC 指令（如 `list` / `say 你好`）（导航底部） |

## 启动方式

```bash
# 1. 安装依赖（qfluentwidgets + PyQt5 仅桌面面板需要，网关本身不需要）
pip install PyQt5 qfluentwidgets requests

# 2. 运行
python desktop_panel/main.py
```

> 概览页顶部「网关地址」默认 `http://127.0.0.1:8080`，与 `config.json` 里的 `web_port` 对应。
> 点「▶ 启动机器人」可直接拉起同目录的 `qq_mc_gateway.py`；再点「■ 关闭机器人」结束进程。
> 主题默认深色（Fluent 深色），如需浅色可在代码 `ControlPanel.__init__` 中将 `setTheme(Theme.DARK)` 改为 `setTheme(Theme.LIGHT)`。

## 说明

- 面板是**独立桌面进程**，通过 REST 与网关交互；网关仍是后台服务（无界面）。
- 机器人启停通过 `subprocess` 管理 `python-adapter/qq_mc_gateway.py`（启动时会以 `python-adapter/` 为工作目录）。
- 部署时只需把 `desktop_panel/` 随 `python-adapter/` 一起拷贝，并在本机 `pip install PyQt5 qfluentwidgets`。
