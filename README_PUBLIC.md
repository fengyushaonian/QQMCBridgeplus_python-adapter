# QQMCBridge 腾讯官方群服互通插件 - 公开发布版

## 1. 插件结构

QQMCBridge-Public/
├── QQMCBridge/                 # LSE 插件目录
│   ├── QQMCBridge.js
│   ├── config.json
│   └── README.md
├── qq_mc_gateway.py            # Python 网关
├── LICENSE
├── README_PUBLIC.md
└── 部署说明.md

## 2. 隐私信息处理说明

- **所有密钥**（AppID、AppSecret、群 OpenID、access_token 等）均已删除
- 仅保留**通用模板**，你需要手动填写自己的真实信息
- 配置文件和源代码中所有敏感数据已注释或移除

## 3. 部署步骤

### 3.1 准备工作
1. 在你的腾讯机器人后台开启**群消息事件**权限
2. 复制本文件夹到你的 BDS 插件目录

### 3.2 Python 网关
```powershell
cd QQMCBridge-Public
python -m pip install requests websockets
python qq_mc_gateway.py
```

### 3.3 LSE 插件
将整个 `QQMCBridge/` 目录复制到你的 BDS 插件目录：
```
<你的BDS目录>\plugins\QQMCBridge\
```

### 3.4 手动配置

#### 配置 LSE 插件
修改 `QQMCBridge/config.json`：

```json
{
  "group_id": 1001220957,
  "group_openid": "你的QQ群group_openid",
  "local_host": "127.0.0.1",
  "local_port": 10724,
  "local_token": "QQMCBridgeLocalToken",
  "mc_to_qq": true,
  "qq_to_mc": true,
  "join_to_qq": true,
  "leave_to_qq": true,
  "command_server": "查服",
  "command_set_name": "设置名称",
  "command_personal": "个人信息",
  "command_tps": "查TPS",
  "qq_to_mc_format": "§7[QQ群] §f%s§7：§r%s",
  "mc_to_qq_format": "[游戏] %s：%s",
  "join_format": "[游戏] %s 加入服务器",
  "leave_format": "[游戏] %s 离开服务器",
  "online_empty_format": "[游戏] 当前没有玩家在线",
  "online_format": "[游戏] 当前在线（%s人）：%s",
  "personal_not_found_format": "[游戏] 找不到玩家：%s",
  "personal_format": "[游戏] %s | 生命：%s/%s | 坐标：%s, %s, %s | 游戏模式：%s",
  "tps_format": "[游戏] 当前 TPS：%s"
}
```

#### 配置 Python 网关
修改网关目录的 `config.json`（已与 LSE 配置合并到同一文件，网关相关键如下；不再需要改 `qq_mc_gateway.py`）：

```json
{
  "APP_ID": "你的AppID",
  "APP_SECRET": "你的AppSecret",
  "GROUP_OPENID": "你的群 OpenID",
  "POLL_INTERVAL": 0.5,
  "RECONNECT_SECONDS": 5,
  "web_port": 8080
}
```

> 注意：`config.json` 同时被网关与 LSE 读取，但二者路径不同——网关读的是**网关目录**下这份，LSE 读的是各服 `plugins/QQMCBridge/config.json`。修改网关相关键后重启网关生效（部分键支持 Web 面板热重载，见 §9）。

## 4. 功能列表

- 玩家进服通知 QQ 群
- 玩家退服通知 QQ 群
- MC 聊天发送到 QQ 群
- QQ 群消息发送到 MC
- QQ 群命令：
  - `查服`：查看所有互通服务器的在线玩家（网关聚合，按服务器分行展示）
  - `设置名称 玩家名`：设置 QQ 昵称
  - `个人信息` 或 `个人信息 玩家名`：查看玩家信息（生命、坐标、模式）
  - `查TPS`：查看服务器 TPS
  - `我的openid`：获取你自己的 QQ OpenID（用于加入远程执行白名单）
  - `执行 <MC指令>`：以控制台权限远程执行 MC 指令（仅白名单内 OpenID 可用，危险指令已封禁）
  - `猜数`：开始一局猜数字游戏（1-100）
  - `猜 数字`：猜一个数字，机器人提示太大/太小
  - `布吉岛 <玩家名>`：查询布吉岛（BuGLand）玩家基础信息（空岛/起床等阶、公会、VIP、布吉岛等级）
  - `布吉岛战绩 <玩家名> [游戏类型]`：查询指定游戏模式的详细战绩（默认 `bedwars`，可选 bedwars/skywars/vdefense/arenapvp/anqu/kitbattle/anni/achievement/survivalgame/parkour）
  - `布吉岛对局 <玩家名> [页码]`：查询最近对局记录

### 4.1 远程执行指令（安全说明）

- 白名单按 **OpenID**（不是 qq 号）验证：官方群消息事件不暴露明文 QQ 号，只能拿到 `member_openid`。
- 开启流程：群里发 `我的openid` → 复制返回的 OpenID → 填入 `config.json` 的 `admin_openids` 数组 → `ll reload QQMCBridge`。
- `admin_openids` 默认空，**默认任何人都不能执行**；必须手动加入自己的 OpenID。
- `exec_blocklist` 默认封禁 `stop/restart/save/op/deop/ban/ban-ip/pardon/pardon-ip/whitelist` 等危险指令；`exec_allowlist` 留空表示除黑名单外都放行，若填入则 only 放行列表内命令（最小权限模式）。
- 远程执行等同控制台权限，请妥善保管 `admin_openids` 与本地 `local_token`。

### 4.2 布吉岛（BuGLand）战绩查询

通过 Python 网关直接调用布吉岛官方 API（`https://api.mcbjd.net/v2`，Bearer 鉴权），在 QQ 群内查询第三方服务器玩家数据，**不影响 MC 端与现有群服互通**。

- 这些命令由 Python 网关拦截处理并直接回复 QQ 群，**不会转发到 MC**；与 LSE 内的 `查玩家` 等命令互不冲突（命名空间为 `布吉岛`）。
- 鉴权 Token 获取：布吉岛等级 > 20 可于大厅输入 `/openapi` 自助申领。
- 配置方式（二选一，环境变量优先）：
  - 在 `config.json` 填入 `"bugland_token": "你的Token"`；或
  - 设置环境变量 `BUGLAND_TOKEN=你的Token`（推荐，避免把 Token 写进仓库）。
- 其它可选配置（`config.json`）：`bugland_enabled`（总开关）、`bugland_command_player/stats/log`（命令词）、`bugland_default_gametype`（默认游戏类型）、`bugland_base`（API 域名，默认 `https://api.mcbjd.net/v2`）。
- 速率限制：普通 Token 30 次/分，开发者 Token 200 次/分；请勿滥用。
- 响应字段直接来自布吉岛接口，战绩类字段为接口原始字段名。`bugland_token` 属于敏感信息，发布前请勿提交。

## 5. 隐私信息删除记录

- `qq_bot_simple.py`：已删除（不使用）
- `LSE_API_REFERENCE.txt`：已删除（不使用）
- 所有密钥、token、OpenID：已删除
- 仅保留通用模板

## 6. 免责声明

本插件仅供学习参考，请确保在遵守腾讯机器人 API 条款的前提下使用。

## 7. 更新记录

- v1.0：初始公开发布版
- 增加设置名称、个人信息、查TPS 功能
- 增加布吉岛（BuGLand）战绩查询（QQ 群内 `布吉岛`/`布吉岛战绩`/`布吉岛对局`）
- 增加多服务器（主服+子服）互通：网关支持 `backends` 多后端，LSE 增加 `respond_to_commands` 开关
- 规范命名并移除源码写死的 LSE_URL/LSE_TOKEN：服务器地址完全由 config.json 的 `backends`（及 `local_host`/`local_port`/`local_token` 回退）驱动
- 查在线 → 查服：查询逻辑迁移到 Python 网关，聚合各服在线名单并统一格式（`【服务器】玩家列表：…`），修复只显示一个服的问题
- 连接端口改为 10724 起（主服 10724，子服 10725，即 10724+N）
- LSE 新增 `/qqmcbridge/online` 数据接口，供网关查询在线玩家
- 新增 Web 管理面板：网关内置本地 HTTP 服务器（端口 8080，仅 127.0.0.1），提供多服状态、远程执行、互通日志、配置编辑器（保存后热重载）
- 网关配置去硬编码：APP_ID/APP_SECRET/GROUP_OPENID/POLL_INTERVAL/RECONNECT_SECONDS 改为从 config.json 读取，并支持保存后热重载
- LSE 新增 `/qqmcbridge/command` 远程执行接口（与 QQ「执行」共用 blocklist/allowlist 校验），`/qqmcbridge/online` 增加 tps/version 字段
- Web 面板与配置编辑器均不含布吉岛相关功能（按需求移除）

## 8. 多服务器（主服 + 子服）互通部署

一个 Python 网关可同时桥接多个 BDS（主服 + 子服），让同一条 QQ 群消息发到所有服务器，多个服务器的聊天/进出服通知也汇聚回同一个 QQ 群，实现"全服互通"。

### 8.1 架构
- 网关读取 `config.json` 的 `backends` 列表，每个后端对应一个 BDS 的 LSE 本地 HTTP 接口。
- QQ 群消息 → 网关 → 同时 POST 到所有后端的 `/qqmcbridge/incoming` → 各服 LSE 广播到各自 MC 并各自响应命令。
- 各服 LSE 出站消息 → 网关轮询各后端 `/qqmcbridge/poll` → 打上 `[服务器名]` 标签后发到 QQ 群。
- 群内 `查服` 命令由网关统一处理：网关依次向各后端 `/qqmcbridge/online` 取在线名单，汇总成单条消息（`【服务器名】玩家列表：…`），实现"一个命令看全服"。
- 若 `config.json` 没有 `backends`，自动回退为单一后端（取 `local_host`/`local_port`/`local_token`），与旧版单服完全兼容。服务器地址完全由配置驱动，源码不再写死。

### 8.2 部署步骤（同机器，不同端口）
1. 主服、子服都安装本插件（各自的 `plugins/QQMCBridge/config.json`）。
2. 主服 `local_port` 设为 `10724`；子服设为 `10725`（`10724 + N`，各不相同即可）。
3. 在网关目录的 `config.json` 加入 `backends`：
   ```json
   "backends": [
     {"name": "主服", "url": "http://127.0.0.1:10724", "token": "QQMCBridgeLocalToken", "relay_mc_to_qq": true},
     {"name": "子服", "url": "http://127.0.0.1:10725", "token": "QQMCBridgeLocalToken", "relay_mc_to_qq": true}
   ]
   ```
   - `name`：服务器名，用于给发往 QQ 的消息加 `[主服]`/`[子服]` 前缀。
   - `url`：该服 LSE 本地地址（跨机器填局域网 IP，如 `http://192.168.1.20:8766`）。
   - `token`：该服 `local_token`（默认与全局一致，可留空沿用）。
   - `relay_mc_to_qq`：是否把该服 MC 聊天/通知转发到 QQ 群（默认 true）。
4. 重启网关 `python qq_mc_gateway.py`，启动日志会显示识别到的后端数量与名称。

### 8.3 命令由谁响应
- 默认两服都响应 QQ 群命令（各自回报本服状态，带服务器名标签）。
- `查服` 例外：它由网关统一聚合，不会在两服各回一条；只返回一条汇总消息（见 §8.1）。
- 若想避免 `执行` 指令或 `猜数` 在两服重复触发，可在**子服** `config.json` 设 `"respond_to_commands": false`，使其仅做聊天互通、不响应群命令（主服照常响应）。该开关在 LSE 端，默认 `true`。

### 8.4 注意事项
- 跨机器部署时，网关所在机器必须能同时访问主服、子服的 LSE HTTP 端口（防火墙/局域网放行）。
- 各服 `local_token` 建议保持一致；若不同，在 `backends` 里逐条填 `token`。
- 增删服务器只需改 `backends` 数组并重启网关，无需改代码。

## 9. Web 管理面板（本地）

网关内置一个**仅监听 127.0.0.1** 的本地 HTTP 服务器（默认端口 `web_port: 8080`），提供浏览器可视化管理，无需额外安装。启动网关后，浏览器打开 `http://127.0.0.1:8080/` 即可。

### 9.1 功能
- **服务器状态**：实时聚合各后端在线人数、TPS、插件版本、在线玩家名单；后端离线时显示「离线」并给出原因，不影响其它功能。
- **远程执行 MC 指令**：选择目标服务器并输入指令，转发到该服 LSE 的 `/qqmcbridge/command` 并返回输出；强制沿用 `exec_blocklist`/`exec_allowlist` 拦截危险指令（`stop/op/ban` 等）。
- **互通日志**：拉取网关缓存的 QQ↔MC 互通记录（QQ 收到 / MC→QQ 转发 / 查服 / Web 执行），支持自动轮询。
- **完整配置编辑器**：读取并可视化编辑 `config.json`（APP_ID/SECRET、群 OpenID、轮询参数、查服指令、多后端 `backends`），保存后网关**热重载**；也支持直接编辑原始 JSON。

### 9.2 安全说明
- Web 服务器**仅绑定本机回环地址 127.0.0.1**，外部网络不可达，仅本机浏览器可访问。
- `config.json` 内含 `APP_SECRET` 明文，**仅用于本机本地管理**；对外发布或部署到多用户机器前，请将密钥迁移到环境变量或专用密钥管理，并轮换已泄露的凭证。
- 远程执行等同控制台权限，且强制黑名单拦截；请勿把 `local_token` 暴露给不可信环境。
- 如确需跨机器访问面板，请在 127.0.0.1 之外自行加反向代理与鉴权（默认不提供）。

