# Python 子插件标准（pymods · 与上游 v3 对齐）

QQMCBridge 网关（python-adapter）支持在 **Python 端** 加载子插件，扩展群消息处理能力，
无需改动 `qq_mc_gateway.py` 主体。子插件运行在与 QQ 网关相同的进程 / 事件循环内，
通过统一 `ctx` 上下文直接调用网关能力（QQ 官方 API 全量封装、双通道图片渲染等）。

> 本加载器已按上游 `QQMCBridge-releasenew` 最新版（v3 架构）完整适配：
> 固定插件结构 `manifest.json + main.py`、`PyMod` 生命周期（on_load / handle_message /
> background_loop / 事件钩子）、按 priority 升序分发、控制台清单表。
> 下游额外保留了多后端（BDS backends[]）帮助清单回推、`ctx.IMAGE_SENT` 发图哨兵、
> Web/桌面面板热重载与启停接口。

> 注意区分两套子插件体系：
> - `mods/`：运行在 **BDS 端（LSE）** 的 JS 子插件；
> - `python-adapter/pymods/`：运行在 **Python 网关端** 的 Python 子插件（本文档）。

## 目录结构

每个子插件是一个独立子目录，内含 **manifest 文件** 与 **脚本**：

```
python-adapter/
└── pymods/
    ├── README.md            # 本说明
    └── <plugin_id>/         # 一个子插件（目录名任意，建议与 manifest 的 id 一致）
        ├── manifest.json    # 清单（必填）
        ├── main.py          # 入口脚本（文件名在 manifest.entry 指定，默认 main.py）
        ├── config.json      # 可选：插件配置（覆盖根 config.json 同名字段）
        ├── data.json        # 可选：插件运行数据
        └── <other>.py       # 其他模块（入口脚本可 import 同目录文件）
```

网关启动时扫描 `pymods/` 下每个含 `manifest.json` 的子目录并加载。

## manifest.json 字段

```json
{
  "id": "roll-dice",
  "name": "掷骰子",
  "version": "1.0.0",
  "author": "QQMCBridge",
  "description": "一句话描述",
  "entry": "main.py",
  "enabled": true,
  "priority": 100,
  "help": "掷骰子 / rd <最大值>：随机掷一个 1~N 的数字",
  "commands": ["掷骰子", "rd"]
}
```

- `id` / `name` 建议填写，`id` 与目录名一致最佳；`enabled: false` 跳过加载。
- `priority`：多个子插件按 priority 升序调用，先返回的插件阻断后续（默认 100）。
- `help`：写入「帮助」列表 Python 分区；模块内也可定义模块级 `help` 变量，二选一。
- `commands`（可选）：插件关键词列表，目前仅作元数据供管理界面展示。

## main.py 接口

入口脚本**必须**实现 `handle_message(ctx)`，以下均为可选：

```python
help = "掷骰子 / rd <最大值>：随机掷一个 1~N 的数字"


def handle_message(ctx):
    """必需。同步或 async 均可；返回非空字符串/消息对象即回复并阻断，None 继续流程。"""
    text = ctx.content.strip()
    if text == "掷骰子":
        import random
        return f"🎲 {ctx.sender_name} 掷出了 {random.randint(1, 6)}"
    return None


def on_load(gateway):
    """可选。加载时同步回调，用于初始化（读取配置、建立连接等）。"""


async def background_loop(gateway):
    """可选。后台轮询任务：网关 run() 自动启动，异常自动记录并 30 秒后重启。"""
    import asyncio
    while True:
        await asyncio.sleep(60)


# 事件钩子（可选，async/sync 均可），签名与上游一致
async def on_group_message(gateway, ctx):
    """每条群消息（pymods 处理前）都会触发。"""


async def on_member_joined(gateway, member_openid, username, qq_number, avatar_url):
    """有新成员入群时触发（GROUP_MEMBER_ADD 事件）。"""


async def on_member_approved(gateway, member_openid):
    """入群申请被批准后触发（group-approve 等插件可派发）。"""


async def on_member_left(gateway, member_openid, username, qq_number, avatar_url):
    """有成员退群时触发（GROUP_MEMBER_REMOVE 事件）。"""
```

返回值约定（handle_message）：

- 返回 **非空字符串或消息对象（dict）**：作为群回复发送，并 **阻断** 后续子插件与转发到 MC；
- 返回 `None` 或 `""`：表示不处理，流程继续（下一个子插件 / 转发到 MC）；
- 返回 `ctx.IMAGE_SENT`（下游扩展）：子插件已自行发图，网关不再重复发送任何内容。

## ctx（PyModContext）字段

| 属性 | 类型 | 说明 |
|---|---|---|
| `ctx.api` | `QQBotAPI` | **QQ 官方 API 全量封装**，见下表 |
| `ctx.content` | `str` | 消息正文（已去除 @ 与前后空白） |
| `ctx.sender_openid` | `str` | 发送者 QQ OpenID |
| `ctx.sender_name` | `str` | 发送者昵称 |
| `ctx.qq_number` / `ctx.avatar_url` | `str` | QQ 号与头像 URL（官方事件提供时） |
| `ctx.group_openid` | `str` | 群 OpenID |
| `ctx.msg_id` | `str` | 消息 ID（用于被动回复） |
| `ctx.is_admin` | `bool` | 发送者是否为配置的群管理员（`group_admins`） |
| `ctx.config` | `dict` | 插件配置：根 config.json 打底 + 插件目录 config.json 覆盖 |
| `ctx.reply(content)` | `async` | 回复当前消息（文本 / 消息对象 / 图卡协议串） |
| `ctx.gateway` | `QQGateway` | 网关实例（兼容旧插件，可调用渲染 / 发送方法） |
| `ctx.IMAGE_SENT` | 哨兵 | 自行发图成功后返回它，网关不再重复发送 |

## ctx.api（QQBotAPI）方法一览

| 方法 | 说明 |
|---|---|
| `api.send_message(content, msg_id)` | 发送群消息：text / markdown / keyboard / ark / image / video / file；图文混排自动拆两条 |
| `api.upload_media(media_type, url)` | 富媒体上传：本地文件自动分片（无需公网 IP），URL 直传 5xx 自动重试 |
| `api.get_group_info()` | 群信息（含机器人身份 member_role） |
| `api.get_member_profile(member_openid)` | 群成员资料（昵称 / 头像，取决于官方权限） |
| `api.get_mute_setting()` / `api.set_mute_members(members)` | 禁言查询 / 批量设置（单次 ≤10 人，机器人需群管理员） |
| `api.mute_member(openid, seconds)` / `api.unmute_member(openid)` | 单个成员禁言 / 解除（自动算 ISO8601+08:00 过期时间） |
| `api.get_join_requests()` | 待审批入群申请列表（join_request_id 每次轮换，勿作去重指纹） |
| `api.approve_join_request(openid, approve, ...)` | 审批入群申请（approve=False 时带拒绝理由） |
| `api.list_panels()` / `create_panel(payload)` / `update_panel(id, panel)` | QQ 指令面板（菜单面板）管理 |
| `api.keyboard(buttons, columns)` | 静态工具：`[(label, data), ...]` → 官方 keyboard.content 结构 |
| `api.mask(openid)` | 静态工具：OpenID 脱敏（保留首尾各 4 位） |
| `api.request(method, path, ...)` | 通用请求：直接调用任意 QQ 官方 API（path 以 / 开头） |

同步方法内部使用 requests，在 async 上下文调用时建议包一层 `asyncio.to_thread`：

```python
async def handle_message(ctx):
    if not ctx.is_admin:
        return "[禁言] 仅管理员可用"
    await asyncio.to_thread(ctx.api.mute_member, target_openid, 600)
    return "[禁言] 已禁言 10 分钟"
```

## 发图 / 图片卡片

三种方式任选：

### 1) 自绘图协议串（简单图形，无需前端资源）

返回 `__QQMC_DRAW__:\n{json}` 协议串（`{"w", "h", "bg", "ops": [...]}` 指令序列），
网关转 SVG 后由 Edge Headless 截图发群。适合棋盘、柱状图等简单图形。

### 2) 上游模板卡协议（`__QQMC_HTML_CARD__`，上游插件原样可用）

返回 `__QQMC_HTML_CARD__:\n{json}` 协议串，`json` 形如
`{"template": "online", "data": {...}, "width": 800, "keyboard": {...}}`。
网关用移植自上游的 `TemplateCardRenderer` 渲染固定模板后截图发图：

- 可用模板：`help` / `online` / `player` / `history` / `player-stats` /
  `checkin` / `server-status`（缺省回退 `status`），另有围棋棋盘 `go_board()`；
- 上游依赖模板卡的插件（如 `group-mute` 等）**无需改写**即可运行；
- 也可在插件里直接调用 `gateway.render_html_card(data)` 取 `(markup, w, h)`。

### 3) 液态玻璃卡片（下游推荐，与「查服 / 查玩家」同款）

依赖 `ctx.gateway.glass_wrap(inner_html, width=760)` + `ctx.gateway.send_card(html, msg_id)`：

- `glass_wrap` 把「卡片内部 HTML」（应含自身 `<style>` 与内容节点，**不含**
  `<!DOCTYPE>/<html>/<body>`）包裹进统一外壳：随机背景图 + 半透明磨砂玻璃
  （backdrop-filter 液态玻璃透镜效果）+ 自动注入中文字体；
- `send_card` 为 **async** 方法：渲染 HTML→上传→发图，成功返回 `True`，
  失败 / 渲染器不可用（未装 Edge）返回 `False`；
- 发图成功后应返回 `ctx.IMAGE_SENT`，否则网关会把你返回的文本再发一遍（图文双发）。

完整流程示例（发图失败后回退文本）：

```python
async def handle_message(ctx):
    # _CARD_INNER 是「内部片段」：含 <style> 与 <div class="card">…</div>
    inner = _CARD_INNER.replace("{result}", str(result)).replace("{n}", str(n))
    html = ctx.gateway.glass_wrap(inner, width=480)
    ok = await ctx.gateway.send_card(html, ctx.msg_id)
    if ok:
        return ctx.IMAGE_SENT
    return f"纯文本兜底：结果是 {result}"
```

> `send_card` 内部已用 `asyncio.to_thread` 包裹，不会阻塞事件循环；
> 子插件若自带耗时同步操作，也应自行用 `asyncio.to_thread` 包裹。
>

## 发语音条

`await ctx.gateway.send_voice(本地音频路径, msg_id)` —— 上传（`file_type=3`）
并按 `msg_type=7` 发送，成功返回 `True`，失败返回 `False`（不抛异常，请据此回退文本）。

- 下游走 **base64 直传本地文件**，因此**不需要公网地址 / 图床**，本地合成即可发；
- 也可走标准富媒体字典：`await ctx.gateway.send_group_message_async(
  {"type": "voice", "url": 本地路径}, msg_id)`，网关会自动按 `file_type=3` 上传；
- 音频格式以 QQ 官方富媒体接口要求为准（语音 `file_type=3`；常见做法是合成 mp3/wav
  后再转 silk/amr）。格式被拒时 `send_voice` 记 warning 并返回 `False`；
- 语音条**不支持**挂载内联键盘，也没有文字正文。

```python
async def handle_message(ctx):
    silk = await asyncio.to_thread(tts_to_silk, "你好呀")
    if await ctx.gateway.send_voice(silk, ctx.msg_id):
        return ctx.IMAGE_SENT      # 已发出，网关不再重复发文本
    return "语音发送失败，先用文字将就一下：你好呀"
```

> `glass_wrap` 会兜底：若玻璃外壳渲染异常，直接返回原 inner HTML（丢失外壳但仍可渲染）。

## 加载与重载

- 网关启动时自动加载（`QQGateway.load_pymods()`），控制台输出插件清单表
  （每行：序号 / id / 版本 / 能力标记 / 描述），能力标记如 `消息|后台|钩子:on_member_joined`。
- **热重载机制**：加载器每次重扫都会卸载旧模块（含子插件目录里的裸 `import cards` 类
  子模块，防止改完 bug reload 后仍跑旧代码），并以「加载代数」命名新模块
  （`qqmc_pymod_<id>_<代数>`），天然隔离新旧代码。
- 触发重载的三种方式：
  1. 重启网关进程；
  2. Web 面板 `POST /api/pymods/reload`；
  3. 桌面控制台「Python子插件」页的「重新加载」按钮。
- 在「帮助」命令中可看到所有已加载子插件的 `help` 文本（标注为 `[Python子插件]`）。

## 管理接口（Web 面板 / 桌面控制台）

| 接口 | 方法 | 说明 |
|---|---|---|
| `/api/pymods` | GET | 列出全部已加载子插件（id / name / version / author / description / priority / enabled / help） |
| `/api/pymods` | POST | 启停单个子插件：`{"id": "<id>", "enabled": true/false}`，写回 manifest.json 后热重载 |
| `/api/pymods/reload` | POST | 重新扫描加载全部子插件，返回 `{"count": N}` |

桌面控制台「Python子插件」页即基于上述接口：每张卡带启停开关与重新加载按钮。

## 示例

下游自带实现（均在 `pymods/` 下）：

- `roll-dice/`：掷骰子（液态玻璃卡片版）。演示 `handle_message` + `glass_wrap` + `send_card` + `IMAGE_SENT` 完整链路。
- `gobang/`：群内 15×15 五子棋（双人 / 人机四档难度），演示状态管理与棋盘图卡，渲染器不可用时回退 ASCII 棋盘。
- `niuzi/`：群聊虚拟宠物「牛子」小游戏（复刻 spark.niuzi），演示多文件结构（`cards.py` / `service.py` / 图片资源）与 `import` 同目录模块。
- `music/`：点歌器（**上游适配版**）。网易云搜索 + 下载 mp3 → 以语音条发送 + 歌名文字。
  上游版依赖根配置 `media_public_base_url`（公网 URL 直传），**下游版无需任何公网配置**：
  直接返回 `{"type": "voice", "url": 本地路径, "content": 歌名信息}`，网关自动 base64 直传
  本地文件并「先语音后文字」拆两条发送（与上游 api.send_message 行为一致）。

最小 `roll-dice/` 核心逻辑（纯文本版）：

```python
import random

help = "掷骰子 / rd <最大值>：随机掷一个 1~N 的数字"

def handle_message(ctx):
    text = ctx.content.strip()
    if text == "掷骰子" or text.lower() == "roll":
        n = 6
    elif text.lower().startswith("rd "):
        try:
            n = int(text[3:].strip())
        except ValueError:
            return None
        if n < 1:
            n = 1
    else:
        return None
    n = min(n, 100000)
    return f"🎲 {ctx.sender_name} 掷出了 {random.randint(1, n)}（范围 1~{n}）"
```

上游 `QQMCBridge-releasenew/pymods/` 的插件结构与本标准完全兼容，可直接复制使用：

- `py-demo/`：on_load + gateway 能力访问示例；
- `group-approve/`：ctx.is_admin + ctx.api 审批流 + on_member_approved 钩子派发；
- `group-mute/`：ctx.api 禁言 + 图卡渲染；
- `group-welcome/`：on_member_joined 钩子 + 头像获取 + 欢迎图卡；
- `ai-image-py/`：AI 生图（上游 v3 已下线，默认 `enabled: false`，仅供结构参考）。

## 约定与限制

- 子插件运行在主进程，请避免阻塞式长耗时操作；耗时任务用 `asyncio` 或 `asyncio.to_thread`。
- 子插件的标准错误会被网关捕获并记录日志，不会拖垮网关。
- 子插件之间按 `priority` 顺序执行，先返回非空结果者阻断后续。
- 子插件运行在网关进程权限内，只应加载可信代码；不要把 API Key、Token 或密码写入数据文件。
- 多后端（backends[]）部署时：子插件在网关端全局生效（所有后端共用一份），
  帮助清单由网关推送至各 BDS 后端的 `/qqmcbridge/pymods` 端点，重启 BDS 后自动重推。
