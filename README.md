# QQMCBridgeplus —— Python 网关（下游魔改版）

QQ 官方机器人 ⇄ Minecraft（BDS / LSE）群服互通插件的 **Python 网关端**。

本项目是上游 **QQMCBridge** 的下游魔改分支（下称 **QQMCBridgeplus**）：
在沿用上游「LSE 插件 + Python 网关」整体架构的基础上，重构并扩展了
**多服（多后端）架构、免公网发图、液态玻璃卡片、管理面板与桌面控制台、群管审批**
等能力，同时把 Python 子插件（pymods）加载器对齐到上游最新 v3 标准。

> 子插件开发规范见 [`pymods/README.md`](pymods/README.md)。

## 目录速览

```
python-adapter/
├── qq_mc_gateway.py     # 网关主体：QQ WebSocket / 多后端轮询 / 命令分发 / pymods 加载
├── config.json          # 主配置（后端列表、群 OpenID、Web 端口等）
├── card_render.py       # 图片卡片渲染（Playwright + 系统 Edge）+ 液态玻璃外壳
├── pymods/              # Python 子插件目录（插件规范见其内 README）
├── webui/               # 内置 Web 管理面板前端
├── desktop_panel/       # 桌面控制台（PyQt5 + qfluentwidgets）
├── requirements.txt     # 依赖
└── README.md            # 本文件
```

运行：`pip install -r requirements.txt` 后 `python qq_mc_gateway.py`。

---

## 相对上游：下游仍然存在的区别与优势

### 一、架构级差异

| 能力 | 上游 QQMCBridge | 下游 QQMCBridgeplus |
|---|---|---|
| 服务器接入 | 单个 LSE（`lse_url`） | **多后端 `backends[]`**：可同时挂多个 BDS/LSE，支持多服在线汇总、指定服务器执行指令、分服开关命令响应 |
| 管理后台 | 独立 Flask `admin_web.py`（端口 1230） | **内置双端口 Web 面板**（公网 18080 强制登录 / 本地 12708 免密）+ **桌面控制台** |
| 桌面 GUI | 无 | **有**：PyQt5 + qfluentwidgets（仿 Class-Widgets 风格），含概览 / 互通日志 / Python 子插件 / 后端子插件 / 配置编辑器 / 执行指令 |
| 发图地址依赖 | 需公网 URL（`media_public_base_url`）或走分片上传 | **双通道**：base64 直传（免公网，图片卡片）+ 上游同款分片直传 COS / URL 直传（可配 `media_public_base_url`） |
| 卡片风格 | CardRenderer 模板引擎（help/online/player 等固定模板） | **两者兼得**：上游 `__QQMC_HTML_CARD__` 模板卡协议已兼容（`TemplateCardRenderer` 完整移植）+ 自家 `glass_wrap` 液态玻璃卡片 |
| 语音条 | 协议层预留（`file_type=3`），无便捷入口 | **已打通**：`MEDIA_FILE_TYPE["voice"]` + `gateway.send_voice(path)` 一步发送 |
| AI 能力 | v3 已下线（`__QQMC_AI_PLUGIN__` 仅忽略） | **保留**：仍可处理 BDS 侧 AI 协议请求 |
| 日志 | 标准 logging | **双路合一**：互通消息日志 + 标准 logging 统一进面板「互通日志」页 |

### 二、群服互通增强

- **多服在线汇总**：一次查询汇总所有后端在线人数 / TPS / 玩家列表，渲染成图卡。
- **指定服务器执行指令**：`执行 <服务器名> <指令>`，避免广播到全部后端。
- **分服命令响应控制**：单个后端可设 `respond_to_commands=false` 不回群命令。
- **群组互通补全**：正确汇报了玩家死亡时的提示。

### 三、群管能力（下游独立实现）

- **入群审批**：收到 `GROUP_JOIN_REQUEST` 后在群内发审批卡片 + 内联键盘（同意 / 拒绝 / 拉黑），
  机器人自动调官方审批接口。
- **退群自动拉黑**：可开关，退群即写入黑名单，再次申请自动拒绝。
- **管理员绑定**：`绑定管理员 <验证码>` + 后台生成验证码，绑定后可用 `ctx.is_admin` 做权限控制。
- **指令菜单面板**：`菜单` 打开内联键盘面板，复刻全部常用功能，点击即执行。

### 四、Python 子插件（pymods）加载器

已对齐上游最新 v3 加载器（`PyMod` 类 + `manifest.json`/`main.py` 固定结构），
并保留了下游的**增强项**：

- **热重载更彻底**：每次重载除了用「加载代数」命名新模块（`qqmc_pymod_<id>_<代数>`），
  还会清理 `sys.modules` 与 `sys.path` 残留，**包括插件目录里被裸 `import` 的子模块**
  （如 `niuzi` 的 `import cards / service`）。上游仅靠代数命名，不清子模块缓存，
  存在「改完 bug reload 后仍跑旧代码」的隐患。
- **帮助清单推送到所有后端**：上游只推单个 LSE，下游遍历 `backends[]` 逐一推送，
  BDS 重启后自动重推。
- **`ctx.IMAGE_SENT` 哨兵**：子插件自行发图后返回它，网关不再重复发文本。
- **`load_pymods()` 返回加载数量**：供 Web 面板 `/api/pymods/reload` 展示。
- **面板管理接口**：`GET /api/pymods`、`POST /api/pymods`（单插件启停）、
  `POST /api/pymods/reload`（热重载）。

上游 `pymods/` 下的插件可**直接复制使用**（已验证 9 个官方插件全部可加载）；
其中依赖公网配置的插件（如 `music` 点歌器依赖 `media_public_base_url`）已提供
**下游适配版**（`pymods/music/`）：改走本地 base64 直传，无需任何公网配置。

### 五、与上游的兼容情况（已对齐项）

以下上游能力**已移植**，上游插件可直接加载运行：

1. **富媒体分片上传**：`upload_media` 已与上游 v3 对齐 —— 本地 `media/` 文件
   （URL 含 `/media/` 标记）直接**分片直传 COS**（`_local_media_path` +
   `_upload_chunked`，预上传→逐片 PUT→确认→合并），无需公网地址；配置了
   `media_public_base_url` 时图片走 URL 直传、失败自动降级分片；瞬时错误
   （40034002/40034003/40093007/超时/下载失败）自动重试。下游原有的
   **base64 直传**（`upload_group_file`，图片卡片通道）保持不变，两套通道并存。
2. **`__QQMC_HTML_CARD__` 模板卡协议**：已兼容。`_send_result` 识别该协议串，
   由 `card_render.TemplateCardRenderer`（上游 CardRenderer 完整移植）渲染
   help / online / player / history / player-stats / checkin / server-status
   模板（缺省回退 status），走 `render_html_image`（Playwright/Edge 截图）发图，
   可选挂载键盘。上游依赖模板卡的插件无需改写。

### 六、尚未对齐上游的部分（有意不移植）

1. **上游 Flask 管理后台的服务器文件管理**（`admin_web.py` 的 `D:\ylcs` 文件浏览）：
   下游未移植，改用桌面控制台 + Web 面板覆盖常规运维。
2. **pymods 在线配置编辑**：上游管理后台可直接编辑插件 `config.json` 的 `settings`；
   下游需在面板「配置」页或手动编辑插件目录的 `config.json`。
3. **配置键大小写**：上游用大写键（`APP_ID` / `APP_SECRET`），下游 `_apply_config`
   大小写均兼容，两边 config.json 可互通（该点本就兼容，非差异）。


---

## 许可证

本项目（**QQMCBridgeplus**）采用 **GNU General Public License v3.0（GPL-3.0）** 授权。

- 你可以自由地运行、研究、修改和重新分发本程序（包括商业用途）；
- 若你分发本程序的修改版本，**必须以 GPLv3 同一许可证开源全部修改后的源代码**，
  并保留原始版权与许可证声明；
- 本程序「按原样」提供，**不提供任何明示或暗示的担保**，详见 GPLv3 第 15、16 条。

本项目衍生自上游 QQMCBridge 项目（LSE 插件 + Python 网关），在其架构基础上进行
下游魔改与功能扩展，故同样以 GPLv3 发布，以遵守其 Copyleft 要求。

- 完整许可证条款：<https://www.gnu.org/licenses/gpl-3.0.html>
- 中文非官方译本（仅供参考，以英文原版为准）：
  <https://www.gnu.org/licenses/translations.html>

> 分发本仓库时请将 GPLv3 全文保存为仓库根目录的 `LICENSE` 文件。
