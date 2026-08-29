# -*- coding: utf-8 -*-
"""腾讯官方 QQ 机器人与 LSE QQMCBridge 网关 + 本地 Web 管理面板。

依赖：requests、websockets
安装：python -m pip install requests websockets

Web 管理面板：双端口架构。
  - 公网端口 WEB_PORT(默认 18080，绑定 0.0.0.0)：对外暴露，强制登录（不论来源 IP 都要令牌），
    改端口/开放公网时只动这个。
  - 本地端口 LOCAL_WEB_PORT(默认 12708，绑定 127.0.0.1)：仅本机可达，桌面 GUI / 本机浏览器
    专用、**完全免密**，与公网彻底隔离——公网被爆破也不影响本地 GUI。
提供状态查询、远程执行、互通日志、配置编辑等 API，并托管 webui/index.html。
安全模型：本地端口不鉴权；公网端口强制凭密码登录换取 HttpOnly Cookie 令牌；空密码时公网
登录直接 403 杜绝裸奔。Web 服务器与 QQ 网关在同一进程内以守护线程共存。
"""

import asyncio
import base64
import collections
import http.server
import importlib.util
import inspect
import json
import logging
import os
import random
import re
import subprocess
import threading
import time
import html
import sys
import urllib.parse
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests
import websockets

# 本地媒体渲染（qqmc-draw / 围棋图卡）需要 Pillow（PIL）。
# 若运行机器未安装 Pillow，协议图片会安全降级为纯文本，不会让网关崩溃。
try:
    from PIL import Image, ImageDraw, ImageFont
    _PIL_AVAILABLE = True
except ImportError:
    Image = ImageDraw = ImageFont = None  # type: ignore
    _PIL_AVAILABLE = False

# ---- 默认鉴权/运行参数（config.json 未配置时使用的兜底值）----
APP_ID = "YOUR_APP_ID"
APP_SECRET = "YOUR_APP_SECRET"
GROUP_OPENID = "YOUR_GROUP_OPENID"
POLL_INTERVAL = 0.5
RECONNECT_SECONDS = 5
WEB_PORT = 18080            # 公网端口：绑定 0.0.0.0，强制登录（改端口请同步改桌面面板/反向代理）
LOCAL_WEB_PORT = 12708      # 本地端口：仅绑定 127.0.0.1，GUI / 本机浏览器专用、完全免密
# ===== Web 管理面板鉴权（双端口分离安全模型）=====
# 设计原则（双端口彻底隔离 GUI 与公网）：
#   - 本地端口(LOCAL_WEB_PORT, 绑定 127.0.0.1)：仅本机可达，桌面 GUI 与本机浏览器走此端口，
#     **完全免密**（local handler 直接放行，不读 token），外网无法触达。
#   - 公网端口(WEB_PORT, 绑定 0.0.0.0)：对外暴露，由反向代理/公网访问，
#     **强制登录**——不论来源 IP，一律要求有效令牌（public handler 永远要求 _require_auth）。
#   - 这样 GUI 与陌生人彻底走两条路：公网被爆破也不影响本地 GUI；改公网端口/暴露公网不动 GUI。
#   - 空密码(config 无 webui_password)时：公网登录接口直接 403，杜绝「空密码+暴露外网=裸奔」；
#     本地端口依旧免密（仅本机可达，风险可控）。
WEB_SESSION_TTL = 12 * 3600          # 登录令牌有效期（秒）
WEB_SESSIONS: Dict[str, float] = {}  # token -> 过期时间戳（重启即清空）
WEB_AUTH_LOCK = threading.Lock()
# 登录失败限流：ip -> [失败次数, 窗口起点]，防外网爆破
LOGIN_FAILS: Dict[str, List[float]] = {}
LOGIN_MAX_FAILS = 8
LOGIN_FAIL_WINDOW = 300              # 5 分钟内超过阈值则临时拒绝

QQ_API = "https://api.bot.qq.com"

BASE_DIR = Path(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = BASE_DIR / "config.json"
WEBUI_DIR = BASE_DIR / "webui"
# Python 端子插件目录（与上游一致）：pymods/<id>/{manifest.json,main.py,...}
PYMODS_DIR = BASE_DIR / "pymods"
# 本地媒体（PIL 渲染图片）临时落盘目录；会被上传到 QQ 官方接口后删除。
MEDIA_DIR = BASE_DIR / "media"
MEDIA_DIR.mkdir(exist_ok=True)

# ---------------------------------------------------------------------------
# 与上游 QQBotAPI（ctx.api）保持一致的常量与工具
# ---------------------------------------------------------------------------
# 富媒体类型：QQ 官方 /v2/groups/{openid}/files 的 file_type（语音=3，与上游一致）
MEDIA_FILE_TYPE = {"image": 1, "video": 2, "voice": 3, "file": 4}
# 消息类型：text=0 / markdown=2 / ark=4 / 富媒体(image、video、voice、file)=7
MSG_TYPE_MAP = {"text": 0, "image": 7, "video": 7, "voice": 7, "file": 7,
                "ark": 4, "markdown": 2, "keyboard": 2}
TZ_CN = timezone(timedelta(hours=8))

# ===== 版本号与更新播报 =====
# 硬性版本号：只能是自然数（非负整数），开发者每次发版手动 +1。
# 用于判断「本次启动是否比上次更新」：硬编码值 > config.json 里的 bot_version 即视为更新。
BOT_VERSION = 6
# 对外展示的版本名（任意文本，给群友看），随硬性版本号一起在发版时填写。
BOT_VERSION_NAME = "小把罢 v1.2"
# 更新日志：硬编码在代码内，每次发版在顶部追加最新内容（群友可见）。
UPDATE_CHANGELOG = (
    "· 修复：在群里远程执行指令可能让服务器直接崩溃重启的严重问题\n"
    "· 修复：部分指令明明没进黑名单、却怎么都执行不了的问题\n"
    "· 小把罢 v1.2 版本更新完毕，执行指令现在又稳又快 🎉\n"
    "· 优化：机器人语音发送稳定性大幅提升，修复极小概率发送失败问题\n"
    "· 优化：插件热重载机制再升级，全覆盖适配所有插件，重载零旧代码残留\n"
    "· 小把罢 v1.1 版本更新完毕，体验更佳 🎉\n"
    "· 新增：机器人会发语音条啦 🎙️ 后续还会接入更多语音玩法\n"
    "· 新增：子插件系统大升级，已对齐上游最新架构，玩法扩展更自由\n"
    "· 优化：子插件改完立刻生效，不会再偷偷跑旧代码\n"
    "· 优化：插件管理更省心，面板里可一键开关 / 重新加载\n"
)
# 点击公告下方按钮时下发到群的指令（会被本机器人当作群消息收到并回复完整日志）。
UPDATE_LOG_COMMAND = "更新日志"

# 版本号合法性自检：只能是自然数，配置错误会被明确拒绝（不让网关带着错误版本号启动）。
if not isinstance(BOT_VERSION, int) or BOT_VERSION < 0:
    raise SystemExit(
        f"[配置错误] BOT_VERSION 必须是自然数（非负整数），当前为：{BOT_VERSION!r}"
    )


def clean_media_url(value: Any) -> str:
    """从富文本/Markdown 中抽出干净的公网 URL（供上传富媒体用）。"""
    raw = str(value or "").strip()
    start = min(
        (index for index in (raw.find("https://"), raw.find("http://")) if index >= 0),
        default=-1,
    )
    if start < 0:
        return raw.strip("`'\" \t\r\n")
    url = raw[start:]
    for marker in ("`", "'", '"', "<", ">", " ", "\t", "\r", "\n"):
        marker_index = url.find(marker)
        if marker_index >= 0:
            url = url[:marker_index]
    return url.rstrip(".,;:)]}")


def beautify_markdown(text: str) -> str:
    """把纯文本回复轻度美化成 QQ markdown：行首 [标签] 加粗，其余语法原样透传。"""
    def bold_tag(line: str) -> str:
        match = re.match(r"^(\[[^\[\]]{1,12}\])(\s*)", line)
        if not match:
            return line
        return f"**{match.group(1)}**{match.group(2)}{line[match.end():]}"

    return "\n".join(bold_tag(line) for line in str(text or "").splitlines())


def build_group_payload(message: Dict[str, Any]) -> Dict[str, Any]:
    """把内部消息对象转换为 QQ 官方群消息 API 的 payload。"""
    message_type = str(message.get("type", "text")).lower()
    payload: Dict[str, Any] = {"msg_type": MSG_TYPE_MAP.get(message_type, 0)}
    if message_type == "text":
        payload["content"] = str(message.get("content", ""))
    elif message_type in {"image", "video", "voice", "file"}:
        payload["media"] = {"file_info": str(message.get("file_info", ""))}
    elif message_type == "ark":
        payload["ark"] = {"template_id": str(message.get("template_id", "")), "kv": message.get("kv", {})}
    elif message_type == "markdown":
        template_id = str(message.get("custom_template_id") or "").strip()
        if template_id:
            params = message.get("params")
            if not isinstance(params, list) or not params:
                params = [{"key": "text", "values": [str(message.get("content", ""))]}]
            payload["markdown"] = {"custom_template_id": template_id, "params": params}
        else:
            payload["markdown"] = {"content": str(message.get("content", ""))}
        if message.get("keyboard"):
            payload["keyboard"] = {"content": message["keyboard"]}
    elif message_type == "keyboard":
        payload["markdown"] = {"content": str(message.get("content", ""))}
        payload["keyboard"] = {"content": message.get("keyboard", {})}
    return payload

# ---------------------------------------------------------------------------
# 子插件（jsmod / pymods）协议串约定
# ---------------------------------------------------------------------------
# BDS 端 LLSE 的 JS 子插件（mods/）会把特定结果以「协议前缀 + JSON」的形式回传网关，
# 由网关负责渲染成图片 / 调用 AI / 多服汇总。以下前缀需与 BDS 端 QQMCBridge.js 保持一致。
PROTO_MULTI_SERVER_ONLINE = "__QQMC_MULTI_SERVER_ONLINE__"
PROTO_AI_PREFIX = "__QQMC_AI_PLUGIN__:"
PROTO_GO_IMAGE_PREFIX = "__QQMC_GO_IMAGE__:\n"
PROTO_DRAW_PREFIX = "__QQMC_DRAW__:\n"
PROTO_HTML_CARD_PREFIX = "__QQMC_HTML_CARD__:\n"  # 上游模板卡协议（兼容上游插件）

def parse_protocol_json(result: str, prefix: str) -> Optional[Dict[str, Any]]:
    """解析子插件返回的“协议前缀 + JSON”载荷，不匹配或解析失败返回 None。"""
    if not result.startswith(prefix):
        return None
    try:
        data = json.loads(result[len(prefix):])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _esc(value: Any) -> str:
    """HTML 转义，防止用户昵称 / 验证消息破坏卡片结构。"""
    return html.escape(str(value), quote=True)


def _load_config_file() -> Dict[str, Any]:
    """读取网关配置文件（config.json），读取失败返回空字典。"""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def _save_config_file(data: Dict[str, Any]) -> None:
    """原子写回 config.json（先写临时文件再 rename，避免半写损坏）。"""
    tmp = str(CONFIG_PATH) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, str(CONFIG_PATH))


def _build_backend_url(host: Any, port: Any) -> str:
    host = str(host or "127.0.0.1").strip()
    port = str(port or "").strip()
    if not port:
        return ""
    return f"http://{host}:{port}"


def _load_backends(cfg: Dict[str, Any]) -> list:
    """读取多服务器后端配置（主服 + 子服 等多后端互通）。

    数据源为 config.json 的 `backends` 数组（每一项表示一个 Minecraft 服务端）：
        [{"name": "主服", "url": "http://127.0.0.1:10724",
          "token": "QQMCBridgeLocalToken", "relay_mc_to_qq": true}, ...]
    - name:           服务器名，用于给发往 QQ 的消息打标签（如 [主服] / [子服]）。
    - url / host+port: 该服务器 LSE 的本地 HTTP 地址（二选一）。
    - token:          该服务器 LSE 的 local_token（缺省沿用全局 local_token）。
    - relay_mc_to_qq: 是否把该服的 MC 聊天/进出服通知转发到 QQ 群。
    若未配置 backends，则回退到单一后端（取 config 的 local_host/local_port/local_token）。
    """
    raw = cfg.get("backends")
    backends = []
    if isinstance(raw, list):
        for i, b in enumerate(raw):
            if not isinstance(b, dict):
                continue
            url = str(b.get("url") or "").strip().rstrip("/")
            if not url:
                url = _build_backend_url(b.get("host"), b.get("port"))
            if not url:
                continue
            backends.append({
                "name": str(b.get("name") or f"服务器{i + 1}"),
                "url": url,
                "token": str(b.get("token") or cfg.get("local_token") or ""),
                "relay_mc_to_qq": bool(b.get("relay_mc_to_qq", True)),
            })
    if backends:
        return backends
    fallback_url = _build_backend_url(cfg.get("local_host"), cfg.get("local_port"))
    if fallback_url:
        return [{
            "name": "服务器",
            "url": fallback_url,
            "token": str(cfg.get("local_token") or ""),
            "relay_mc_to_qq": True,
        }]
    log.warning("config.json 未配置 backends 也未配置 local_port，使用默认 127.0.0.1:10724")
    return [{
        "name": "服务器",
        "url": "http://127.0.0.1:10724",
        "token": str(cfg.get("local_token") or "QQMCBridgeLocalToken"),
        "relay_mc_to_qq": True,
    }]


def _load_bugland_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    defaults: Dict[str, Any] = {
        "bugland_enabled": True,
        "bugland_token": "",
        "bugland_command_player": "布吉岛",
        "bugland_command_stats": "布吉岛战绩",
        "bugland_command_log": "布吉岛对局",
        "bugland_default_gametype": "bedwars",
    }
    for key in defaults:
        if key in cfg:
            defaults[key] = cfg[key]
    return defaults


# ---- BuGLand（布吉岛）第三方服务器数据接口 ----
# 文档：https://mcbjd.net/docs/api/bugland-api
# 鉴权 Token 获取：布吉岛等级 > 20 可于大厅输入 /openapi 自助申领
BUGLAND_GAMETYPES = [
    "bedwars", "skywars", "vdefense", "arenapvp", "anqu",
    "kitbattle", "anni", "achievement", "survivalgame", "parkour",
]
BUGLAND_BASE = os.environ.get("BUGLAND_BASE", "https://api.mcbjd.net/v2").rstrip("/")


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [QQMC-Gateway] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("qqmc-gateway")

# ---- 把标准 logging 输出也同步进互通日志缓冲（供桌面面板 /api/logs 查看完整日志）----
# 之前 /api/logs 只返回 push_log 记录的互通消息（mc/qq），看不到启动/鉴权/轮询等
# INFO 日志。这里加一个自定义 Handler，把 qqmc-gateway 的所有 logging 记录写入
# 与 push_log 共用的全局 deque，实现「完整日志」。
_GLOBAL_LOG_DEQUE: "collections.deque" = collections.deque(maxlen=1000)
_GLOBAL_LOG_LOCK = threading.Lock()


class _GatewayLogHandler(logging.Handler):
    """把 qqmc-gateway 的标准 logging 记录写入全局日志缓冲。"""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            ts = time.strftime("%H:%M:%S")
            level = record.levelname.lower()
            log_type = {
                "warning": "warn",
                "error": "error",
                "critical": "error",
            }.get(level, "info")
            with _GLOBAL_LOG_LOCK:
                _GLOBAL_LOG_DEQUE.append(
                    {"time": ts, "type": log_type, "text": record.getMessage()}
                )
        except Exception:
            pass


log.addHandler(_GatewayLogHandler())

# 哨兵：本地命令已发送图片（查玩家），调用方据此跳过后续转发，避免图文双发。
_IMAGE_SENT = object()


class QQBotAPI:
    """QQ 官方机器人 API 封装：token、消息、富媒体、群管理、禁言、审批、面板。

    与上游 QQMCBridge-release 完全一致的 ctx.api 实现（pymods 子插件通过 ctx.api
    调用）。所有方法线程安全（token 刷新带简单时序保护），401/403 自动重试一次。
    """

    def __init__(self, app_id: str, app_secret: str, group_openid: str,
                 qq_api: str, qq_token_api: str) -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self.group_openid = group_openid
        self.qq_api = qq_api.rstrip("/")
        self.qq_token_api = qq_token_api.rstrip("/")
        self.access_token = ""
        self.token_expire_at = 0.0
        self._msg_seq: Dict[str, int] = {}
        # 文本回复 markdown 通道：优先全局模板（custom_template_id + params），
        # 未配置模板时走原生 content（需官方 Markdown 权限，否则可能被静默吞掉）
        self.markdown_enabled = True
        self.markdown_failed = False
        self.markdown_template_id = ""
        self.markdown_param = "text"

    def set_markdown(self, enabled: bool, template_id: str = "", param: str = "text") -> None:
        """配置 markdown 回复通道（网关启动时读 config 的 qq_markdown* 项）。"""
        self.markdown_enabled = bool(enabled)
        self.markdown_failed = False
        self.markdown_template_id = str(template_id or "").strip()
        self.markdown_param = str(param or "").strip() or "text"

    # ---------------- 基础：token 与通用请求 ----------------

    def get_access_token(self, force: bool = False) -> str:
        if not force and self.access_token and time.time() < self.token_expire_at - 60:
            return self.access_token
        log.info("正在请求 QQ access_token…")
        try:
            response = requests.post(
                f"{self.qq_token_api}/app/getAppAccessToken",
                json={"appId": self.app_id, "clientSecret": self.app_secret},
                timeout=15,
            )
        except requests.RequestException as error:
            raise RuntimeError(f"请求 QQ access_token 失败：{error}") from error
        try:
            data = response.json()
        except ValueError:
            data = response.text
        if response.status_code < 200 or response.status_code >= 300:
            raise RuntimeError(f"获取 access_token 失败：{response.status_code} {data}")
        if not isinstance(data, dict) or not data.get("access_token"):
            raise RuntimeError(f"获取 access_token 失败：{data}")
        self.access_token = data["access_token"]
        self.token_expire_at = time.time() + int(data.get("expires_in", 7200))
        log.info("access_token 获取成功")
        return self.access_token

    def auth_headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"QQBot {self.get_access_token()}",
            "Content-Type": "application/json",
        }

    def request(self, method: str, path: str, *, params: Any = None,
                json_body: Any = None, timeout: float = 15,
                group_openid: Optional[str] = None) -> Any:
        """通用 QQ 官方 API 请求：path 以 / 开头，401/403 自动刷新重试一次。"""
        url = f"{self.qq_api}{path}"
        kwargs: Dict[str, Any] = {"headers": self.auth_headers(), "timeout": timeout}
        if params is not None:
            kwargs["params"] = params
        if json_body is not None:
            kwargs["json"] = json_body
        response = requests.request(method, url, **kwargs)
        if response.status_code in (401, 403):
            self.get_access_token(force=True)
            kwargs["headers"] = self.auth_headers()
            response = requests.request(method, url, **kwargs)
        try:
            result = response.json()
        except ValueError:
            result = response.text
        if response.status_code < 200 or response.status_code >= 300:
            raise RuntimeError(f"QQ API {method} {path} 失败：HTTP {response.status_code}，响应：{result}")
        return result

    def _group(self, group_openid: Optional[str] = None) -> str:
        return str(group_openid or self.group_openid)

    def _next_msg_seq(self, msg_id: str) -> int:
        """同一 msg_id 的被动回复需要递增 msg_seq，避免重复判定。"""
        if not msg_id:
            return 1
        if len(self._msg_seq) > 128:
            self._msg_seq.clear()
        seq = self._msg_seq.get(msg_id, 0) + 1
        self._msg_seq[msg_id] = seq
        return seq

    # ---------------- Gateway ----------------

    def get_gateway_url(self) -> str:
        result = self.request("GET", "/gateway")
        if not isinstance(result, dict) or not result.get("url"):
            raise RuntimeError(f"Gateway 响应没有 url：{result}")
        return str(result["url"])

    # ---------------- 群消息 ----------------

    def send_message(self, content: Any, msg_id: str = "", group_openid: Optional[str] = None) -> Any:
        """发送群消息：支持 text / markdown / keyboard / ark / image / video / file。

        图片等富媒体不能与文字同条发送：message 同时带 content 与媒体 url 时自动拆两条。
        """
        message = content if isinstance(content, dict) else {"type": "text", "content": str(content)}
        media_type = str(message.get("type", "")).lower()
        text = str(message.get("content") or "").strip()
        if media_type in MEDIA_FILE_TYPE and text:
            media_message = {k: v for k, v in message.items() if k != "content"}
            self._dispatch_message(media_message, msg_id, group_openid)
            return self._dispatch_message({"type": "text", "content": text}, msg_id, group_openid)
        return self._dispatch_message(message, msg_id, group_openid)

    def _dispatch_message(self, message: Dict[str, Any], msg_id: str, group_openid: Optional[str] = None) -> Any:
        """单条消息实际发送：富媒体先上传换取 file_info，再组装 payload。

        纯文本默认尝试 markdown 通道（自动加粗 [标签]，插件自带语法透传）；
        官方拒绝（HTTP 400/403）时本条降级重发纯文本，且本会话不再尝试 markdown。
        """
        media_type = str(message.get("type", "")).lower()
        if media_type in MEDIA_FILE_TYPE:
            message = {**message,
                       "file_info": self.upload_media(media_type, message.get("url", ""), group_openid)}
        if media_type in {"", "text"} and self.markdown_enabled and not self.markdown_failed:
            plain = str(message.get("content") or "")
            md_content = str(message.get("markdown_content") or "") or beautify_markdown(plain)
            if md_content.strip() and len(md_content) <= 2500:
                md_message: Dict[str, Any] = {"type": "markdown", "content": md_content}
                if self.markdown_template_id:
                    # 全局模板模式：参数值携带 markdown 语法，官方渲染
                    md_message["custom_template_id"] = self.markdown_template_id
                    md_message["params"] = [{"key": self.markdown_param, "values": [md_content]}]
                try:
                    return self._post_message(md_message, msg_id, group_openid)
                except RuntimeError as error:
                    denied = "HTTP 400" in str(error) or "HTTP 403" in str(error)
                    if denied:
                        self.markdown_failed = True
                        log.warning("QQ markdown 通道被拒，本会话降级为纯文本回复：%s", error)
                    else:
                        log.warning("QQ markdown 发送异常，本条降级纯文本：%s", error)
        return self._post_message(message, msg_id, group_openid)

    def _post_message(self, message: Dict[str, Any], msg_id: str, group_openid: Optional[str] = None) -> Any:
        """组装 payload 并调用群消息接口（供 markdown 尝试与降级共用）。"""
        payload = build_group_payload(message)
        if msg_id:
            payload["msg_id"] = msg_id
            payload["msg_seq"] = self._next_msg_seq(msg_id)
        group = self._group(group_openid)
        result = self.request("POST", f"/v2/groups/{group}/messages", json_body=payload, timeout=20)
        brief = {k: v for k, v in message.items() if k != "file_info"}
        log.info("已发送群消息：%s", brief)
        return result

    def upload_media(self, media_type: str, url: str, group_openid: Optional[str] = None) -> str:
        """调用 QQ 富媒体上传接口，用公网 URL 换取发消息用的 file_info。

        与上游 v3 对齐：
          - 图片走 URL 直传（QQ 服务器回拉，稳定）；
          - 文件/语音/视频若指向本地 media/ 文件则直接**分片上传**（字节直达 COS），
            规避 URL 直传对文件回拉不稳定（40093007 下载失败）导致的超长等待；
          - URL 直传失败且文件在本地时自动降级分片上传。
        """
        file_type = MEDIA_FILE_TYPE.get(media_type, 1)
        group = self._group(group_openid)
        # 本地文件走分片上传（字节直达 COS，无需公网 URL）：
        # 非图片一律分片；图片在未配置公网地址时也分片（配置了则 URL 直传更快）
        local = self._local_media_path(url)
        if local is not None:
            has_public = bool(str(read_config().get("media_public_base_url") or "").strip())
            if file_type != 1 or not has_public:
                log.info("本地富媒体直接分片上传：%s", local.name)
                return self._upload_chunked(file_type, local, local.name, group)
        body = {"file_type": file_type, "url": clean_media_url(url)}
        last_response = None
        for attempt in range(3):
            try:
                # 文件/语音需 QQ 服务器下载 + 转码，耗时可达数十秒，超时给足
                response = requests.post(
                    f"{self.qq_api}/v2/groups/{group}/files",
                    headers=self.auth_headers(), json=body, timeout=90,
                )
            except requests.RequestException:
                if attempt < 2:
                    time.sleep(2.0 + 2.0 * attempt)
                    continue
                raise
            if response.status_code in (401, 403) and attempt < 2:
                self.get_access_token(force=True)
                continue  # token 过期，刷新后重试
            transient = response.status_code >= 500 or self._is_transient_upload_error(response)
            if transient and attempt < 2:
                time.sleep(2.0 + 2.0 * attempt)  # 2s / 4s 递增退避
                continue
            last_response = response
            break
        response = last_response
        try:
            result = response.json()
        except ValueError:
            result = response.text
        if response.status_code < 200 or response.status_code >= 300:
            # URL 直传失败且文件在本地 media/ 时，自动降级为分片上传（字节直达 COS，不依赖 QQ 回拉）
            local = self._local_media_path(url)
            if local is not None:
                log.info("富媒体 URL 直传失败，自动降级分片上传：%s", local.name)
                return self._upload_chunked(file_type, local, local.name, group)
            raise RuntimeError(f"上传富媒体失败：HTTP {response.status_code}，响应：{result}")
        file_info = result.get("file_info") if isinstance(result, dict) else ""
        if not file_info:
            raise RuntimeError(f"上传富媒体未返回 file_info：{result}")
        return str(file_info)

    def _local_media_path(self, url: str) -> Optional[Path]:
        """URL 是否指向本地 media/ 目录的文件（可走分片上传）。

        兼容公网绝对 URL（http://host/media/xxx）与无公网地址时的相对占位
        URL（/media/xxx），只要文件名落在本地 media/ 目录即可。
        """
        try:
            raw = str(url or "")
            marker = "/media/"
            idx = raw.rfind(marker)
            if idx < 0:
                return None
            name = raw[idx + len(marker):].split("?")[0].strip()
            if not name or name != Path(name).name:
                return None
            target = MEDIA_DIR / name
            return target if target.is_file() else None
        except Exception:  # noqa: BLE001
            return None

    def _upload_chunked(self, file_type: int, path: Path, filename: str, group: str) -> str:
        """分片上传：预上传 → 逐片 PUT → 确认分片 → 合并获取 file_info。

        字节直达腾讯 COS 预签名 URL，不依赖 QQ 服务器回拉公网 URL，
        用于 URL 直传（40093007 等下载失败）不稳定的场景。
        """
        import hashlib

        data = path.read_bytes()
        md5 = hashlib.md5(data).hexdigest()
        sha1 = hashlib.sha1(data).hexdigest()
        md5_10m = hashlib.md5(data[:10002432]).hexdigest()
        headers = self.auth_headers()
        # 1) 预上传
        prepare = requests.post(
            f"{self.qq_api}/v2/groups/{group}/upload_prepare",
            headers=headers,
            json={"file_type": file_type, "file_size": str(len(data)),
                  "file_name": filename, "md5": md5, "sha1": sha1, "md5_10m": md5_10m},
            timeout=30,
        )
        presult = prepare.json() if prepare.content else {}
        if prepare.status_code >= 400 or not presult.get("upload_id"):
            raise RuntimeError(f"分片预上传失败：HTTP {prepare.status_code} {presult}")
        upload_id = str(presult["upload_id"])
        block_size = int(presult.get("block_size") or 5 * 1024 * 1024)
        parts = presult.get("parts") or []
        # 2) 逐片 PUT + 3) 确认分片（每片大小以服务端返回的 part.block_size 为准，累计偏移切分）
        offset = 0
        for part in parts:
            index = int(part.get("index"))
            part_size = int(part.get("block_size") or block_size)
            chunk = data[offset:offset + part_size]
            offset += part_size
            if not chunk:
                continue
            presigned = str(part.get("presigned_url") or "")
            put_resp = requests.put(presigned, data=chunk, timeout=120)
            if put_resp.status_code >= 400:
                raise RuntimeError(f"分片 {index} 上传失败：HTTP {put_resp.status_code} {put_resp.text[:200]}")
            finish = requests.post(
                f"{self.qq_api}/v2/groups/{group}/upload_part_finish",
                headers=headers,
                json={"upload_id": upload_id, "part_index": index,
                      "block_size": str(len(chunk)),
                      "md5": hashlib.md5(chunk).hexdigest()},
                timeout=30,
            )
            if finish.status_code >= 400:
                raise RuntimeError(f"分片 {index} 确认失败：HTTP {finish.status_code} {finish.text[:200]}")
        # 4) 合并获取 file_info
        merge = requests.post(
            f"{self.qq_api}/v2/groups/{group}/files",
            headers=headers,
            json={"file_type": file_type, "upload_id": upload_id},
            timeout=90,
        )
        mresult = merge.json() if merge.content else {}
        if merge.status_code >= 400 or not mresult.get("file_info"):
            raise RuntimeError(f"分片合并失败：HTTP {merge.status_code} {mresult}")
        return str(mresult["file_info"])

    @staticmethod
    def _is_transient_upload_error(response) -> bool:
        """QQ 富媒体上传瞬时错误判定：上传超时/失败、文件下载失败（40093007），
        或 message 含「超时」/「下载失败」。这类错误重试通常可成功。"""
        try:
            payload = response.json()
        except ValueError:
            return False
        if not isinstance(payload, dict):
            return False
        if payload.get("err_code") in (40034002, 40034003, 40093007):
            return True
        message = str(payload.get("message", ""))
        return "超时" in message or "下载失败" in message

    # ---------------- 群与成员 ----------------

    def get_group_info(self, group_openid: Optional[str] = None) -> Dict[str, Any]:
        """群信息（机器人身份等）。"""
        return self.request("GET", f"/v2/groups/{self._group(group_openid)}")

    def get_member_profile(self, member_openid: str, group_openid: Optional[str] = None) -> Dict[str, Any]:
        """查询单个群成员资料（昵称 / QQ 号 / 头像，取决于官方权限）。"""
        return self.request("GET", f"/v2/groups/{self._group(group_openid)}/members/{member_openid}")

    # ---------------- 禁言（机器人需为群管理员，仅普通成员） ----------------

    def get_mute_setting(self, group_openid: Optional[str] = None) -> Dict[str, Any]:
        result = self.request("GET", f"/v2/groups/{self._group(group_openid)}/restrict_chat_setting")
        return result if isinstance(result, dict) else {}

    def set_mute_members(self, members: List[Dict[str, Any]], group_openid: Optional[str] = None) -> Any:
        """批量禁言设置：members=[{"op":"add|update|del","member_openid":...,"mute_expire_at":ISO8601+08:00}]"""
        return self.request(
            "POST", f"/v2/groups/{self._group(group_openid)}/restrict_chat_setting",
            json_body={"members": members[:10]},  # 单次最多 10 人
        )

    def mute_member(self, member_openid: str, seconds: int, group_openid: Optional[str] = None) -> Any:
        expire = datetime.now(TZ_CN) + timedelta(seconds=max(1, int(seconds)))
        return self.set_mute_members(
            [{"op": "add", "member_openid": member_openid,
              "mute_expire_at": expire.isoformat(timespec="seconds")}], group_openid)

    def unmute_member(self, member_openid: str, group_openid: Optional[str] = None) -> Any:
        return self.set_mute_members([{"op": "del", "member_openid": member_openid}], group_openid)

    # ---------------- 入群审批 ----------------

    def get_join_requests(self, limit: int = 100, group_openid: Optional[str] = None) -> List[Dict[str, Any]]:
        """待审批入群申请列表（join_request_id 每次拉取都会轮换，勿用作去重指纹）。"""
        result = self.request(
            "GET", f"/v2/groups/{self._group(group_openid)}/join_request_list",
            params={"cursor": "", "limit": max(1, min(100, int(limit)))},
        )
        items = result.get("list") if isinstance(result, dict) else result
        return [item for item in (items or []) if isinstance(item, dict)]

    def approve_join_request(self, member_openid: str, approve: bool = True, *,
                             join_request_id: str = "", reject_reason: str = "",
                             blacklist: bool = False,
                             group_openid: Optional[str] = None) -> Any:
        """审批入群申请：approve=True 同意，False 驳回；blacklist=True 时驳回并加入群黑名单。"""
        body: Dict[str, Any] = {"op": "approve" if approve else "decline"}
        if join_request_id:
            body["join_request_id"] = str(join_request_id)
        if not approve:
            body["reject_reason"] = reject_reason or "管理员未通过入群申请"
            if blacklist:
                body["add_to_member_blacklist"] = True
        return self.request(
            "POST",
            f"/v2/groups/{self._group(group_openid)}/approval_join_request/{member_openid}",
            json_body=body,
        )

    # ---------------- 指令面板（菜单面板） ----------------

    def list_panels(self, scope: str = "group", limit: int = 50) -> Dict[str, Any]:
        return self.request("GET", "/v2/panels", params={"scope": scope, "limit": limit})

    def create_panel(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.request("POST", "/v2/panels", json_body=payload)

    def update_panel(self, panel_id: str, panel: Dict[str, Any]) -> Dict[str, Any]:
        return self.request("PUT", f"/v2/panels/{panel_id}", json_body={"panel": panel})

    # ---------------- 静态工具 ----------------

    @staticmethod
    def keyboard(buttons: List[Tuple[str, str]], columns: int = 2) -> Dict[str, Any]:
        """[(label, data), ...] → QQ 官方 keyboard.content 结构。"""
        rows = []
        width = max(1, int(columns))
        for start in range(0, len(buttons), width):
            row_buttons = []
            for label, data in buttons[start:start + width]:
                text = str(label or "")[:32]
                row_buttons.append({
                    "id": str(data or text),
                    "render_data": {"label": text, "visited_label": text, "style": 1},
                    "action": {
                        "type": 2,
                        "permission": {"type": 2},
                        "data": str(data or text),
                        "unsupport_tips": "请发送按钮中的文字命令",
                        "reply": True,
                        "enter": True,
                    },
                })
            if row_buttons:
                rows.append({"buttons": row_buttons})
        return {"rows": rows}

    @staticmethod
    def mask(openid: str) -> str:
        """OpenID 脱敏：保留首尾各 4 位。"""
        value = str(openid or "").strip()
        return value if len(value) <= 8 else f"{value[:4]}****{value[-4:]}"


class PyModContext:
    """传递给子插件 handle_message / 各事件钩子的上下文对象（与上游 PluginContext 完全对齐）。

    字段：content / sender_openid / sender_name / qq_number / avatar_url /
          group_openid / msg_id / is_admin
    能力：ctx.api（QQ 官方 API 全量封装，即 QQBotAPI 实例）、
          ctx.reply(content)（回复消息/图卡）、
          ctx.config（插件配置：根 config.json 打底 + 本目录 config.json 覆盖）
    兼容：ctx.gateway 指向网关实例（旧插件仍可调用其渲染/发送方法）；
          ctx.IMAGE_SENT 哨兵（子插件自行发图后返回它，网关不再重复发送）。
    """

    __slots__ = (
        "gateway", "content", "sender_openid", "sender_name",
        "qq_number", "avatar_url", "group_openid", "msg_id",
        "is_admin", "IMAGE_SENT", "_plugin_folder",
    )

    def __init__(self, gateway: "QQGateway", *, content: str = "", sender_openid: str = "",
                 sender_name: str = "", qq_number: str = "", avatar_url: str = "",
                 group_openid: Optional[str] = None, msg_id: str = "",
                 is_admin: Optional[bool] = None, image_sent: Any = None,
                 plugin_folder: Any = None) -> None:
        self.gateway = gateway
        self.content = content
        self.sender_openid = sender_openid
        self.sender_name = sender_name
        self.qq_number = qq_number
        self.avatar_url = avatar_url
        self.group_openid = group_openid if group_openid is not None else (
            gateway.group_openid if gateway is not None else "")
        self.msg_id = msg_id
        if is_admin is None:
            admins = getattr(gateway, "admin_openids", []) or []
            is_admin = sender_openid in admins
        self.is_admin = bool(is_admin)
        self.IMAGE_SENT = image_sent
        self._plugin_folder = plugin_folder

    @property
    def api(self) -> "QQBotAPI":
        """QQ 官方 API 全量封装（子插件通过 ctx.api 调用消息/群管/审批等）。"""
        return self.gateway.api

    @property
    def config(self) -> Dict[str, Any]:
        """插件配置（懒读取）：根 config.json 打底 + 本插件目录 config.json 覆盖。"""
        if self._plugin_folder:
            return read_plugin_config(self._plugin_folder)
        return read_config()

    async def reply(self, content: Any) -> None:
        """回复当前消息：支持纯文本、消息对象、绘图/HTML 卡片协议串。"""
        await self.gateway._send_result(content, self.msg_id)


def read_config() -> Dict[str, Any]:
    """读取根目录 config.json（pymods 的 ctx.config 打底层）。"""
    try:
        return _load_config_file() or {}
    except Exception as error:
        log.warning("读取根配置失败：%s", error)
        return {}


def read_plugin_config(folder: Any) -> Dict[str, Any]:
    """插件配置：根 config.json 打底 + 本插件目录 config.json 覆盖。"""
    base = read_config()
    folder_path = Path(folder) if not isinstance(folder, Path) else folder
    cfg_path = folder_path / "config.json"
    try:
        if cfg_path.is_file():
            with open(cfg_path, "r", encoding="utf-8") as fh:
                override = json.load(fh)
            if isinstance(override, dict):
                merged = dict(base)
                merged.update(override)
                return merged
    except Exception as error:
        log.warning("读取子插件配置 %s 失败：%s", cfg_path, error)
    return base


# Python 子插件支持的事件钩子（main.py 中可选定义同名 async 函数）
PYMOD_HOOKS = ("on_group_message", "on_member_joined", "on_member_approved", "on_member_left")


# ---------------------------------------------------------------------------
# Python 子插件：固定插件结构（与上游 QQMCBridge-releasenew 一致）
# ---------------------------------------------------------------------------
# pymods/<id>/
#   manifest.json   —— 插件元数据（固定字段见 PyMod 文档）
#   main.py         —— 入口：handle_message(ctx) 必需；
#                      background_loop(gateway) / on_load(gateway) / 事件钩子可选
#   config.json     —— 插件配置（可选，覆盖根 config.json 同名字段）
#   data.json       —— 插件数据（可选，自行读写）
# ---------------------------------------------------------------------------


class PyMod:
    """一个已加载的 Python 子插件（pymods/<id>/）。

    与上游新版 PyMod 完全对齐；额外保留 manifest 与 enabled 供下游
    Web 面板（/api/pymods）与桌面控制台读取。
    """

    def __init__(self, folder: Path, manifest: Dict[str, Any], module: Any) -> None:
        self.folder = folder
        self.id = str(manifest.get("id") or folder.name)
        self.name = str(manifest.get("name") or self.id)
        self.version = str(manifest.get("version") or "unknown")
        self.author = str(manifest.get("author") or "")
        self.description = str(manifest.get("description") or "")
        self.priority = int(manifest.get("priority") or 100)
        self.help = str(manifest.get("help") or getattr(module, "help", "") or "").strip()
        self.commands = manifest.get("commands") if isinstance(manifest.get("commands"), list) else []
        self.enabled = manifest.get("enabled", True) is not False
        self.manifest = manifest
        self.module = module
        self.handler = getattr(module, "handle_message", None)
        background = getattr(module, "background_loop", None)
        self.background = background if asyncio.iscoroutinefunction(background) else None
        self.hooks = [name for name in PYMOD_HOOKS if callable(getattr(module, name, None))]
        capabilities = []
        if callable(self.handler):
            capabilities.append("消息")
        if self.background:
            capabilities.append("后台")
        capabilities.extend(f"钩子:{name}" for name in self.hooks)
        self.capabilities = capabilities

    def capability_summary(self) -> str:
        return "|".join(self.capabilities) if self.capabilities else "-"


class QQGateway:
    def __init__(self) -> None:
        self.access_token = ""
        self.token_expire_at = 0.0
        self.sequence: Optional[int] = None
        self.heartbeat_interval = 45000
        # 回复消息去重用的严格递增序号（线程安全）
        self._reply_seq = 0
        self._seq_lock = threading.Lock()
        # 配置热重载锁
        self._config_lock = threading.Lock()
        # 互通日志缓冲（线程安全，与 _GatewayLogHandler 共用的全局 deque）
        self.log_buffer: "collections.deque" = _GLOBAL_LOG_DEQUE
        self._log_lock = _GLOBAL_LOG_LOCK
        # QQ 入站消息环形缓冲：供网页地图(BDSLM_JS)经 /qqmcbridge/qqlog 轮询回显到网页聊天框。
        # 注意：与 _GLOBAL_LOG_DEQUE 不同，这里只存「QQ 群消息」本身（time/sender/content），
        # 结构需与 BDSLM_JS 的 pollQQToWeb 解析严格一致（m.time / m.sender / m.content）。
        self.qq_inbound_log: "collections.deque" = collections.deque(maxlen=500)
        # 猜数游戏状态（Python 端直接处理，避免多服重复回复）
        self._guess_games: Dict[str, Any] = {}
        self._guess_lock = threading.Lock()

        # 查服图片卡片渲染器（懒初始化；None 表示未启用 → 查服退回纯文本）
        self.card_renderer: Any = None
        self._card_build: Any = None
        # 上游模板卡渲染器（__QQMC_HTML_CARD__ 协议，懒初始化）
        self.template_renderer: Any = None

        # Python 端子插件（pymods）：PyMod 实例列表，见 load_pymods()
        self.pymods: List["PyMod"] = []
        # 加载代数：每次 load_pymods() 递增，用于模块名隔离（热重载不串旧代码）
        self._pymods_generation = 0
        # pymods 帮助清单回推 BDS 状态标志
        self._pymods_registry_pushed = False
        self._pymods_push_warned = False
        # AI 子插件（__QQMC_AI_PLUGIN__）每用户对话历史
        self.ai_histories: Dict[str, List[Dict[str, str]]] = {}

        # ---- 群管：入群审批 / 退群自动拉黑 ----
        self.pending_join_requests: Dict[str, Dict[str, Any]] = {}
        self.blacklisted_openids: Dict[str, Dict[str, Any]] = {}
        self.member_names: Dict[str, str] = {}        # member_openid -> 昵称（来自消息/入群事件）
        self.group_mod_enabled = True
        self.auto_blacklist_enabled = False
        self._group_mod_lock = threading.Lock()
        self._blacklist_path = BASE_DIR / "group_blacklist.json"
        self._load_blacklist()

        self._config = _load_config_file()
        self._apply_config(self._config)

    # ---------------- 配置加载 / 热重载 ----------------
    def _apply_config(self, cfg: Dict[str, Any]) -> None:
        self.app_id = str(cfg.get("APP_ID") or APP_ID)
        self.app_secret = str(cfg.get("APP_SECRET") or APP_SECRET)
        # 兼容 LSE 使用的小写 group_openid
        self.group_openid = str(
            cfg.get("GROUP_OPENID") or cfg.get("group_openid") or GROUP_OPENID
        )
        self.poll_interval = float(cfg.get("POLL_INTERVAL", POLL_INTERVAL))
        self.reconnect_seconds = int(cfg.get("RECONNECT_SECONDS", RECONNECT_SECONDS))
        self.web_port = int(cfg.get("web_port", WEB_PORT))
        self.local_web_port = int(cfg.get("local_web_port", LOCAL_WEB_PORT))
        self.command_server = str(cfg.get("command_server", "查服"))
        # 以下命令在 Python 端直接处理（不转发到 MC，避免多服重复回复）
        self.respond_to_commands = bool(cfg.get("respond_to_commands", True))
        self.command_help = str(cfg.get("command_help", "帮助"))
        self.command_my_openid = str(cfg.get("command_my_openid", "我的openid"))
        self.command_game_start = str(cfg.get("command_game_start", "猜数"))
        self.command_guess = str(cfg.get("command_guess", "猜"))
        self.command_exec = str(cfg.get("command_exec", "执行"))
        # 以下为帮助文本展示用、且仍由 LSE 游戏端处理的游戏内命令（仅用于帮助展示，不转发）
        self.command_set_name = str(cfg.get("command_set_name", "设置名称"))
        self.command_cancel_name = str(cfg.get("command_cancel_name", "取消名称"))
        self.command_user_info = str(cfg.get("command_personal", "个人信息"))
        self.command_world = str(cfg.get("command_world", "查世界"))
        self.command_pos = str(cfg.get("command_position", "查坐标"))
        self.command_gamemode = str(cfg.get("command_mode", "查模式"))
        self.command_health = str(cfg.get("command_health", "查血量"))
        self.command_query_player = str(cfg.get("command_player", "查玩家"))
        self.command_tps = str(cfg.get("command_tps", "查TPS"))
        self.admin_openids = list(cfg.get("admin_openids") or [])
        # 群管开关：group_mod_enabled 总开关（入群检测 + 人工审批 + 退群检测）；
        # auto_blacklist_enabled 子开关（退群自动拉黑 + 再次申请自动拒绝，需管理员群内发信开启）
        self.group_mod_enabled = bool(cfg.get("group_mod_enabled", True))
        self.auto_blacklist_enabled = bool(cfg.get("auto_blacklist_enabled", False))
        self.bugland_config = _load_bugland_config(cfg)
        self.bugland_token = (
            os.environ.get("BUGLAND_TOKEN")
            or str(self.bugland_config.get("bugland_token", ""))
        )
        self.backends = _load_backends(cfg)

        # ---- Python 子插件 API 封装（ctx.api，与上游 QQBotAPI 完全一致）----
        self.qq_api = str(cfg.get("qq_api") or QQ_API)
        self.qq_token_api = str(cfg.get("qq_token_api") or "https://bots.qq.com")
        self.api = QQBotAPI(self.app_id, self.app_secret, self.group_openid,
                            self.qq_api, self.qq_token_api)
        self.api.set_markdown(
            bool(cfg.get("qq_markdown", False)),
            str(cfg.get("qq_markdown_template_id", "")),
            str(cfg.get("qq_markdown_param", "") or "text"),
        )

    # ---------------- Python 端子插件（pymods）----------------
    def load_pymods(self) -> int:
        """扫描 pymods/ 下含 manifest.json 的目录并加载，重复调用即热重载。

        与上游最新版加载器对齐：
          - manifest 的 entry（默认 main.py）/ enabled / priority / help / commands；
          - 模块名带加载代数（qqmc_pymod_<id>_<代数>），热重载天然隔离旧代码；
          - 加载成功后调用可选 on_load(gateway)，按 priority 升序排序；
          - 失败逐条 log.exception 记录，不拖垮其余插件。

        下游保留增强：
          - 卸载旧模块（含子插件目录里的裸 `import cards` 类子模块）与 sys.path 残留，
            避免热重载后仍命中旧模块缓存（改完 bug reload 后群里仍报旧错误）；
          - 返回成功加载的插件数量（Web 面板 /api/pymods/reload 依赖此值；
            上游 load_pymods 返回 None，这里保持数量）。
        """
        # ---- 1) 卸载旧模块，保证热重载生效 ----
        # 1.1) 入口模块本身（qqmc_pymod_ 前缀）；
        # 1.2) 子插件目录里的任何子模块——子插件入口常写裸 `import service` 之类的
        #      同目录导入，这些模块以顶层名进了 sys.modules 缓存；若不清掉，
        #      热重载后 exec_module 里的 `import service` 仍命中旧缓存，改了文件
        #      也不生效（表现为：改完 bug，reload 后群里仍报旧错误的诡异现象）。
        for modname in [m for m in list(sys.modules) if m.startswith("qqmc_pymod_")]:
            del sys.modules[modname]
        base_prefix = os.path.normpath(str(PYMODS_DIR)) + os.sep
        for modname, mod in list(sys.modules.items()):
            modfile = getattr(mod, "__file__", None)
            if not modfile:
                continue
            try:
                modfile = os.path.normpath(os.path.abspath(modfile))
            except Exception:
                continue
            if modfile.startswith(base_prefix):
                del sys.modules[modname]
        # 清理先前插入的 sys.path 项：旧逻辑只清 .../pymods，漏了各插件子目录
        base_norm = os.path.normpath(str(PYMODS_DIR))
        for p in [p for p in list(sys.path)
                  if os.path.normpath(p) == base_norm
                  or os.path.normpath(p).startswith(base_prefix)]:
            sys.path.remove(p)

        # ---- 2) 重新扫描加载 ----
        self.pymods = []
        self._pymods_registry_pushed = False
        self._pymods_push_warned = False
        if not PYMODS_DIR.is_dir():
            log.info("未发现 pymods 目录，跳过 Python 子插件加载：%s", PYMODS_DIR)
            return 0
        self._pymods_generation += 1
        loaded: List[PyMod] = []
        for folder in sorted(path for path in PYMODS_DIR.iterdir() if path.is_dir()):
            if not (folder / "manifest.json").is_file():
                continue
            try:
                manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
                if not isinstance(manifest, dict):
                    raise ValueError("manifest.json 必须是 JSON 对象")
                if manifest.get("enabled", True) is False:
                    log.info("Python 子插件已禁用：%s", folder.name)
                    continue
                entry = str(manifest.get("entry") or "main.py")
                source_path = folder / entry
                if not source_path.is_file():
                    raise FileNotFoundError(f"入口脚本不存在：{entry}")
                module_name = "qqmc_pymod_{}_{}".format(
                    re.sub(r"[^a-zA-Z0-9_]", "_", folder.name), self._pymods_generation)
                spec = importlib.util.spec_from_file_location(module_name, source_path)
                if not spec or not spec.loader:
                    raise RuntimeError("无法创建模块加载器")
                module = importlib.util.module_from_spec(spec)
                sys.modules[module_name] = module
                if str(folder) not in sys.path:
                    sys.path.insert(0, str(folder))
                spec.loader.exec_module(module)
                plugin = PyMod(folder, manifest, module)
                if not callable(plugin.handler):
                    raise RuntimeError("入口脚本未定义 handle_message(ctx)")
                on_load = getattr(module, "on_load", None)
                if callable(on_load):
                    on_load(self)
                loaded.append(plugin)
            except Exception as error:
                log.exception("Python 子插件加载失败：%s：%s", folder.name, error)
        loaded.sort(key=lambda item: item.priority)
        self.pymods = loaded
        self._print_pymods_table(loaded)
        return len(loaded)

    @staticmethod
    def _print_pymods_table(plugins: List["PyMod"]) -> None:
        """控制台插件清单：一行一个，带能力标记。"""
        if not plugins:
            log.info("Python 子插件：无")
            return
        log.info("Python 子插件加载完成，共 %d 个：", len(plugins))
        for index, plugin in enumerate(plugins, 1):
            log.info("  %d. %-16s v%-9s [%s] %s",
                     index, plugin.id, plugin.version, plugin.capability_summary(),
                     plugin.description or plugin.name)

    # ---------------- 启动时版本更新播报 ----------------
    @staticmethod
    def _greeting_for_now() -> str:
        """根据当前（北京时间）时段返回问候语。"""
        hour = datetime.now(TZ_CN).hour
        if 5 <= hour < 11:
            return "早上好"
        if 11 <= hour < 13:
            return "中午好"
        if 13 <= hour < 18:
            return "下午好"
        return "晚上好"

    def _broadcast_groups(self) -> List[str]:
        """更新播报目标群：config 的 broadcast_groups 列表优先，否则用单一 group_openid。"""
        cfg = read_config()
        groups = cfg.get("broadcast_groups")
        if isinstance(groups, list) and groups:
            return [str(g).strip() for g in groups if str(g).strip()]
        return [self.group_openid]

    async def check_and_announce_update(self) -> None:
        """启动时检测版本更新：硬编码 BOT_VERSION > config 的 bot_version 则向群播报并写回。

        - 版本未变化：跳过，不骚扰群友；
        - 版本更新：按当前时段发送「{问候}！小把罢刚刚更新到了最新版本」+ 更新日志，
          下方挂「查看更新日志」按钮（点击会触发 UPDATE_LOG_COMMAND 回吐完整日志）；
        - 播报成功后把当前版本写回 config.json，避免每次重启重复播报。
        """
        try:
            cfg = read_config()
            raw = cfg.get("bot_version")
            try:
                stored = int(raw) if raw is not None else 0
            except (TypeError, ValueError):
                stored = 0
            if BOT_VERSION <= stored:
                log.info("版本未变化（硬编码 v%d / 配置 v%d），跳过更新播报", BOT_VERSION, stored)
                return

            log.info("检测到版本更新（硬编码 v%d > 配置 v%d），开始向群播报", BOT_VERSION, stored)
            greeting = self._greeting_for_now()
            text = (
                f"{greeting}！小把罢刚刚更新到了最新版本 🎉\n"
                f"当前版本：{BOT_VERSION_NAME}（v{BOT_VERSION}）\n\n"
                f"更新日志：\n{UPDATE_CHANGELOG}"
            )
            keyboard = QQBotAPI.keyboard([("📋 查看更新日志", UPDATE_LOG_COMMAND)], columns=1)
            for gid in self._broadcast_groups():
                try:
                    await asyncio.to_thread(self.send_group_message, text, "", keyboard, text)
                    log.info("已向群 %s 播报更新", gid)
                except RuntimeError as error:
                    if "HTTP 400" in str(error) or "HTTP 403" in str(error):
                        # 机器人未开通 markdown 权限时，键盘必须挂在 markdown 消息上会失败，
                        # 降级为纯文本（无按钮）至少把更新通知送出去。
                        log.warning("更新播报：markdown+键盘被官方拒绝，降级纯文本：%s", error)
                        try:
                            await asyncio.to_thread(self.send_group_message, text, "")
                        except Exception as error2:
                            log.warning("更新播报：纯文本降级仍失败：%s", error2)
                    else:
                        log.warning("向群 %s 播报更新失败：%s", gid, error)
                except Exception as error:
                    log.warning("向群 %s 播报更新失败：%s", gid, error)

            # 持久化：把当前版本写回 config，避免每次重启重复播报
            try:
                if not cfg:
                    # 配置文件读不到（损坏/缺失/解析失败）时 read_config() 会返回 {}。
                    # 此时绝不能拿 {"bot_version": 2} 去覆盖磁盘，否则整份配置会丢失！
                    # 直接跳过写回，保留磁盘上现有的 config.json。
                    log.warning(
                        "配置文件读取为空（可能损坏/被占用），跳过版本写回，"
                        "避免用空配置覆盖现有 config.json 导致配置被清空。"
                    )
                else:
                    cfg["bot_version"] = BOT_VERSION
                    _save_config_file(cfg)
                    log.info("已将版本 v%d 写入 config.json", BOT_VERSION)
            except Exception as error:
                log.warning("更新版本写回 config.json 失败：%s", error)
        except Exception as error:
            log.warning("版本更新播报流程异常：%s", error)

    async def dispatch_pymod_hook(self, hook_name: str, **kwargs: Any) -> None:
        """通用插件事件钩子：调用各 pymods 模块里可选的同名函数（async/sync 均可）。

        钩子签名统一为 hook(gateway, **kwargs)（与上游一致）；on_group_message
        的特例签名为 on_group_message(gateway, ctx=None)。
        """
        for plugin in list(self.pymods):
            callback = getattr(plugin.module, hook_name, None)
            if not callable(callback):
                continue
            try:
                result = callback(self, **kwargs)
                if inspect.isawaitable(result):
                    await result
            except Exception as error:
                log.exception("Python 子插件 %s 钩子 %s 失败：%s",
                              plugin.name, hook_name, error)

    async def _run_pymod_background(self, plugin: "PyMod") -> None:
        """常驻运行子插件 background_loop，异常只记录并延迟重启，不拖垮网关。"""
        while True:
            try:
                log.info("启动 Python 子插件后台任务：%s", plugin.name)
                await plugin.background(self)
                return  # 协程正常返回即结束
            except asyncio.CancelledError:
                raise
            except Exception as error:
                log.exception("Python 子插件 %s 后台任务异常，30 秒后重启：%s",
                              plugin.name, error)
                await asyncio.sleep(30)

    async def _dispatch_pymods(self, content: str, sender_openid: str, sender_name: str,
                               msg_id: str, qq_number: str = "", avatar_url: str = "",
                               group_openid: Optional[str] = None) -> bool:
        """按 priority 升序调用各子插件 handle_message；返回非空结果即发送并阻断。

        与上游一致：每条消息先触发一次 on_group_message 钩子（若有子插件定义），
        再依次调用各子插件的 handle_message(ctx)。返回 True 表示已处理（阻断后续）。
        下游保留 _IMAGE_SENT 哨兵：子插件自行发图后返回它，网关不再重复发送。
        """
        if not self.pymods:
            return False
        group_openid = group_openid or self.group_openid
        # 先分发 on_group_message 钩子（子插件可在此看到每条群消息）
        base_ctx = PyModContext(
            self, content=content, sender_openid=sender_openid, sender_name=sender_name,
            qq_number=qq_number, avatar_url=avatar_url, group_openid=group_openid,
            msg_id=msg_id, image_sent=_IMAGE_SENT,
        )
        await self.dispatch_pymod_hook("on_group_message", ctx=base_ctx)
        for plugin in self.pymods:
            plugin_ctx = PyModContext(
                self, content=content, sender_openid=sender_openid, sender_name=sender_name,
                qq_number=qq_number, avatar_url=avatar_url, group_openid=group_openid,
                msg_id=msg_id, plugin_folder=plugin.folder, image_sent=_IMAGE_SENT,
            )
            try:
                result = plugin.handler(plugin_ctx)
                if inspect.isawaitable(result):
                    result = await result
            except Exception as error:
                log.warning("子插件 %s 处理消息失败：%s", plugin.id, error)
                continue
            if result is _IMAGE_SENT:
                # 子插件已自行发图，阻断后续插件与转发
                return True
            if isinstance(result, dict) and result:
                await self._send_plugin_result(plugin, result, msg_id, sender_name, content)
                return True
            if result is not None and str(result).strip():
                await self._send_plugin_result(plugin, str(result), msg_id, sender_name, content)
                return True
        return False

    async def _send_plugin_result(self, plugin: "PyMod", result: Any, msg_id: str,
                                  sender_name: str = "", content: str = "") -> None:
        """发送子插件回复；失败降级为文字提示，避免异常炸断 WebSocket 主循环。"""
        name = plugin.name
        try:
            self.push_log("qq", f"[子插件:{name}] {sender_name}：{content}")
        except Exception:
            pass
        try:
            await self._send_result(result, msg_id)
        except Exception as error:
            log.warning("Python 子插件 %s 回复发送失败：%s", name, error)
            try:
                await self.send_group_message_async(
                    f"**[{name}]** 内容发送失败（图片可能被 QQ 安全审核拦截），请稍后重试或换个描述", msg_id)
            except Exception:
                pass

    def _collect_pymod_help(self) -> List[str]:
        """聚合 Python 端子插件的帮助文本（去重）。"""
        seen: set = set()
        out: List[str] = []
        for plugin in self.pymods:
            h = plugin.help
            if h and str(h).strip() and str(h) not in seen:
                seen.add(str(h))
                out.append(str(h))
        return out

    def reload_config(self) -> None:
        """重新读取 config.json 并热更新所有运行参数（Web 面板保存配置后调用）。"""
        with self._config_lock:
            cfg = _load_config_file()
            self._config = cfg
            self._apply_config(cfg)
        log.info("配置已从 config.json 热重载（后端数=%d）", len(self.backends))

    # ---------------- 互通日志缓冲 ----------------
    def push_log(self, log_type: str, text: str) -> None:
        try:
            ts = time.strftime("%H:%M:%S")
        except Exception:
            ts = ""
        with self._log_lock:
            self.log_buffer.append({"time": ts, "type": log_type, "text": text})

    # ---------------- QQ 鉴权 / 发消息 ----------------
    def get_access_token(self, force: bool = False) -> str:
        if (
            not force
            and self.access_token
            and time.time() < self.token_expire_at - 60
        ):
            return self.access_token

        log.info("正在请求 QQ access_token…")
        try:
            response = requests.post(
                f"{QQ_API}/app/getAppAccessToken",
                json={"appId": self.app_id, "clientSecret": self.app_secret},
                timeout=15,
            )
        except requests.RequestException as error:
            raise RuntimeError(f"请求 QQ access_token 失败：{error}") from error
        try:
            data = response.json()
        except ValueError:
            data = response.text
        if response.status_code < 200 or response.status_code >= 300:
            raise RuntimeError(f"获取 access_token 失败：{response.status_code} {data}")
        if not data.get("access_token"):
            raise RuntimeError(f"获取 access_token 失败：{data}")

        self.access_token = data["access_token"]
        self.token_expire_at = time.time() + int(data.get("expires_in", 7200))
        log.info("access_token 获取成功")
        return self.access_token

    def auth_headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"QQBot {self.get_access_token()}",
            "Content-Type": "application/json",
        }

    def get_gateway_url(self) -> str:
        log.info("正在请求 QQ Gateway 地址…")
        try:
            response = requests.get(
                f"{QQ_API}/gateway",
                headers=self.auth_headers(),
                timeout=15,
            )
        except requests.RequestException as error:
            raise RuntimeError(f"请求 QQ Gateway 地址失败：{error}") from error
        try:
            data = response.json()
        except ValueError:
            data = response.text
        if response.status_code < 200 or response.status_code >= 300:
            raise RuntimeError(f"获取 Gateway 失败：{response.status_code} {data}")
        if not data.get("url"):
            raise RuntimeError(f"Gateway 响应没有 url：{data}")
        return data["url"]

    def _post_group_message(self, payload: Dict[str, Any], msg_id: str, log_target: Any) -> None:
        """向 QQ 群消息接口发一条消息：带 401/403 token 刷新重试、40054005 去重静默忽略。

        非 2xx 抛 RuntimeError（调用方按需捕获 / 降级）。
        """
        if msg_id:
            with self._seq_lock:
                self._reply_seq += 1
                seq = self._reply_seq
            payload["msg_id"] = msg_id
            payload["msg_seq"] = seq
        else:
            log.info("发送游戏主动群消息：%s", log_target)
        response = requests.post(
            f"{QQ_API}/v2/groups/{self.group_openid}/messages",
            headers=self.auth_headers(),
            json=payload,
            timeout=15,
        )
        try:
            result = response.json()
        except ValueError:
            result = response.text
        if response.status_code in (401, 403):
            self.get_access_token(force=True)
            response = requests.post(
                f"{QQ_API}/v2/groups/{self.group_openid}/messages",
                headers=self.auth_headers(),
                json=payload,
                timeout=15,
            )
            try:
                result = response.json()
            except ValueError:
                result = response.text
        if response.status_code < 200 or response.status_code >= 300:
            err_code = None
            try:
                if isinstance(result, dict):
                    err_code = result.get("code") or result.get("err_code")
            except Exception:
                err_code = None
            if err_code == 40054005:
                # 40054005：消息被去重（同一 msg_id 的回复序号冲突/重发），视为非致命，忽略即可
                log.warning(
                    "群消息被服务器去重，已忽略（不影响运行）：%s | 内容：%s",
                    result, str(log_target)[:80],
                )
                return
            raise RuntimeError(
                f"发送群消息失败：HTTP {response.status_code}，响应：{result}"
            )
        if payload.get("keyboard") is not None and isinstance(result, dict):
            # 带键盘消息：打印接口返回的 code，便于排查按钮不显示（code!=0 表示键盘被服务端拒绝）
            log.info("带键盘消息已发送，接口返回 code=%s id=%s", result.get("code"), result.get("id"))
        log.info("已发送群消息：%s", log_target)

    def send_group_message(self, content: str = "", msg_id: str = "", keyboard: Any = None,
                           markdown: Any = None) -> None:
        """发送群消息。

        关键坑：QQ 群客户端的内嵌键盘(keyboard)只能随 **Markdown 消息(msg_type=2)** 渲染，
        纯文本(msg_type=0) + 键盘在群里不会显示按钮（官方键盘示例也是 markdown+keyboard）。
        因此带按钮的消息请走 send_markdown_with_keyboard，而不要走纯文本。

        纯文本默认先尝试 **markdown 通道(msg_type=2)**，让 `**[标签]**`、`**加粗**`、`` `代码` ``
        等语法在群里真正渲染；若机器人未开通 markdown 权限（官方返回 400/403），本会话自动降级
        纯文本——与上游 QQBotAPI.send_message 行为一致（需 config 的 qq_markdown=true 才开启）。
        """
        # 带键盘或显式 markdown 字符串：直接走 markdown 通道（按钮必须挂在 markdown 消息上）
        if keyboard is not None or markdown is not None:
            md_content = markdown if markdown is not None else content
            payload: Dict[str, Any] = {"msg_type": 2, "markdown": {"content": md_content}}
            if keyboard is not None:
                payload["keyboard"] = keyboard
            self._post_group_message(payload, msg_id, md_content)
            return

        # 纯文本：先尝试 markdown 通道渲染
        api = getattr(self, "api", None)
        md_on = bool(api and getattr(api, "markdown_enabled", False))
        md_fail = bool(api and getattr(api, "markdown_failed", False))
        if md_on and not md_fail:
            md_content = beautify_markdown(content)
            if md_content.strip() and len(md_content) <= 2500:
                try:
                    self._post_group_message(
                        {"msg_type": 2, "markdown": {"content": md_content}}, msg_id, md_content)
                    return
                except RuntimeError as error:
                    denied = "HTTP 400" in str(error) or "HTTP 403" in str(error)
                    if denied and api is not None:
                        api.markdown_failed = True
                        log.warning("QQ markdown 通道被拒，本会话降级纯文本：%s", error)
                    else:
                        log.warning("QQ markdown 发送异常，降级纯文本：%s", error)

        # 降级 / 默认：纯文本通道（msg_type=0）
        self._post_group_message({"msg_type": 0, "content": content}, msg_id, content)

    async def send_group_message_async(self, content: Any, msg_id: str = "") -> None:
        """兼容上游子插件(pymods)调用：content 可为字符串，也可为富媒体字典
        {"type": "image", "url": ...} / {"type": "image", "file_info": ...} / {"type": "text", ...} 等。

        适配器本地渲染产物是本地文件路径，故 {"type":"image","url": ...} 中的 url 走
        「upload_group_file -> send_group_image」链路（适配器官方 API 方案）；若带 file_info
        则直接发图；文本/其它类型回退到官方 api.send_message（含 markdown 通道）。
        任何发送异常都只记日志，绝不让网关事件循环崩溃。
        """
        try:
            if isinstance(content, dict):
                media_type = str(content.get("type", "")).lower()
                if media_type in MEDIA_FILE_TYPE:
                    file_info = content.get("file_info")
                    if not file_info and content.get("url"):
                        # 本地路径（适配器 render_html_image / 点歌插件产物）：按类型上传换 file_info
                        # 语音必须走 file_type=3，否则会被官方按图片拒收
                        file_info = await asyncio.to_thread(
                            self.upload_group_file, str(content["url"]),
                            MEDIA_FILE_TYPE[media_type],
                        )
                    if file_info:
                        if media_type == "voice":
                            await asyncio.to_thread(self.send_group_voice, file_info, msg_id)
                        else:
                            await asyncio.to_thread(self.send_group_image, file_info, msg_id)
                        # 富媒体与文字不能同条发送：content 有值时自动拆成第二条文本
                        # （与上游 api.send_message 行为一致，点歌等插件依赖此约定）
                        text = str(content.get("content") or "").strip()
                        if text:
                            await asyncio.to_thread(self.send_group_message, text, msg_id)
                        return
                    # 既无 file_info 也无 url：回退官方 send_message（远程 URL 场景）
                    await asyncio.to_thread(self.api.send_message, content, msg_id)
                    return
                # 文本 / markdown / keyboard / ark 等其它类型：走官方 API
                await asyncio.to_thread(self.api.send_message, content, msg_id)
                return
            # 纯字符串：保持适配器原有行为（纯文本通道，兼容性最好）
            await asyncio.to_thread(self.send_group_message, content, msg_id)
        except Exception as error:
            preview = content if isinstance(content, str) else str(content)[:80]
            log.error(
                "发送群消息失败（已忽略，网关继续运行）：%s | 内容：%s",
                error, preview,
            )

    async def send_text_with_keyboard(self, content: str, keyboard: Any, msg_id: str = "") -> None:
        """发送纯文本群消息并挂载内联键盘（按钮）。
        注意：QQ 群客户端不会在纯文本消息上渲染内联键盘，此方法仅作为兜底/兼容，
        真正能显示按钮的请使用 send_markdown_with_keyboard。"""
        try:
            await asyncio.to_thread(self.send_group_message, content, msg_id, keyboard)
        except Exception as error:
            log.error(
                "发送带键盘群消息失败（已忽略，网关继续运行）：%s | 内容：%s",
                error, content[:80],
            )

    async def send_markdown_with_keyboard(self, md_content: str, keyboard: Any, msg_id: str = "") -> None:
        """发送 Markdown 群消息并挂载内联键盘——这是 QQ 群里能稳定渲染按钮的唯一方式。"""
        try:
            await asyncio.to_thread(self.send_group_message, "", msg_id, keyboard, md_content)
        except Exception as error:
            log.error(
                "发送带键盘 Markdown 群消息失败（已忽略，网关继续运行）：%s | 内容：%s",
                error, md_content[:80],
            )

    # ---------------- BuGLand（布吉岛）第三方数据接口 ----------------
    def _bugland_target(self, target: str) -> Dict[str, str]:
        if re.fullmatch(
            r"[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}",
            target,
        ):
            return {"uuid": target}
        return {"username": target}

    def _bugland_call(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not self.bugland_token:
            raise RuntimeError(
                "BuGLand Token 未配置：请在 config.json 填写 bugland_token，"
                "或设置环境变量 BUGLAND_TOKEN（布吉岛等级>20 可于大厅输入 /openapi 申领）"
            )
        response = requests.post(
            f"{BUGLAND_BASE}{path}",
            headers={
                "Authorization": f"Bearer {self.bugland_token}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=15,
        )
        try:
            data = response.json()
        except ValueError:
            raise RuntimeError(f"布吉岛接口返回异常（HTTP {response.status_code}）：{response.text[:200]}")
        if not isinstance(data, dict):
            raise RuntimeError(f"布吉岛接口返回格式异常：{str(data)[:200]}")
        code = data.get("code")
        message = data.get("message")
        if code is not None and code != 200:
            raise RuntimeError(f"code={code}，{message or '未知错误'}")
        if message is not None and message != "success" and code != 200:
            raise RuntimeError(f"code={code}，{message or '未知错误'}")
        return data

    def _fmt_lines(self, d: Dict[str, Any], mapping: Any = None) -> str:
        known = set((m[0] for m in mapping) if mapping else [])
        lines: list = []
        if mapping:
            for k, label in mapping:
                if k in d and d[k] not in (None, ""):
                    lines.append(f"{label}：{d[k]}")
        for k, v in d.items():
            if k in known:
                continue
            if isinstance(v, dict):
                sub = "，".join(
                    f"{sk}={sv}" for sk, sv in v.items()
                    if isinstance(sv, (str, int, float, bool)) and sv not in (None, "")
                )
                lines.append(f"{k}：{sub}" if sub else f"{k}：(对象)")
            elif isinstance(v, list):
                lines.append(f"{k}：{len(v)} 项")
            elif v not in (None, ""):
                lines.append(f"{k}：{v}")
        return "\n".join(lines) if lines else "（无数据）"

    def bugland_player_text(self, target: str) -> str:
        data = self._bugland_call("/player", self._bugland_target(target))
        d = data.get("data") or {}
        if not d:
            return f"[布吉岛] 未找到玩家：{target}"
        mapping = [
            ("playername", "玩家名"), ("bjdxp_level", "布吉岛等级"),
            ("swxp_show", "空岛等阶"), ("bwxp_show", "起床等阶"),
            ("guild_name", "公会"), ("vip_level", "VIP等级"),
        ]
        return "[布吉岛] 玩家信息：\n" + self._fmt_lines(d, mapping=mapping)

    def bugland_stats_text(self, target: str, gametype: str) -> str:
        if gametype not in BUGLAND_GAMETYPES:
            return "[布吉岛] 未知游戏类型：" + gametype + "\n可选：" + "、".join(BUGLAND_GAMETYPES)
        payload = self._bugland_target(target)
        payload["gametype"] = gametype
        data = self._bugland_call("/gamestats", payload)
        d = data.get("data") or {}
        if not d:
            return f"[布吉岛] {target} 的 {gametype} 暂无战绩数据"
        return f"[布吉岛] {target} 的 {gametype} 战绩：\n" + self._fmt_lines(d)

    def bugland_log_text(self, target: str, page: str) -> str:
        payload = self._bugland_target(target)
        payload["page"] = page
        data = self._bugland_call("/gamelog/user", payload)
        d = data.get("data") or {}
        arr = d.get("data") or []
        if not arr:
            return f"[布吉岛] {target} 最近没有对局记录"
        lines = []
        for m in arr[:10]:
            date = m.get("date", "")
            typ = m.get("type", "")
            win = "胜" if m.get("win") else "负"
            mid = m.get("matchId", "")
            lines.append(f"{date} | {typ} | {win} | {mid}")
        head = ""
        if d.get("page") is not None:
            head = f"第{d.get('page')}页（每页{d.get('pageSize')}）\n"
        return "[布吉岛] 最近对局：\n" + head + "\n".join(lines)

    async def handle_bugland(self, content: str) -> Optional[str]:
        if not self.bugland_config.get("bugland_enabled"):
            return None
        cfg = self.bugland_config
        name = str(cfg.get("bugland_command_player", "布吉岛"))
        stats = str(cfg.get("bugland_command_stats", "布吉岛战绩"))
        logcmd = str(cfg.get("bugland_command_log", "布吉岛对局"))
        default_gt = str(cfg.get("bugland_default_gametype", "bedwars"))

        if content == name or content.startswith(name + " "):
            target = content[len(name):].strip()
            if not target:
                return f"[布吉岛] 用法：{name} <玩家名或UUID>"
            try:
                return await asyncio.to_thread(self.bugland_player_text, target)
            except RuntimeError as error:
                return f"[布吉岛] {error}"
        if content.startswith(stats + " "):
            rest = content[len(stats):].strip().split()
            if not rest:
                return f"[布吉岛] 用法：{stats} <玩家名或UUID> [游戏类型]"
            target = rest[0]
            gametype = rest[1] if len(rest) > 1 else default_gt
            try:
                return await asyncio.to_thread(self.bugland_stats_text, target, gametype)
            except RuntimeError as error:
                return f"[布吉岛] {error}"
        if content.startswith(logcmd + " "):
            rest = content[len(logcmd):].strip().split()
            if not rest:
                return f"[布吉岛] 用法：{logcmd} <玩家名或UUID> [页码]"
            target = rest[0]
            page = rest[1] if len(rest) > 1 else "1"
            try:
                return await asyncio.to_thread(self.bugland_log_text, target, page)
            except RuntimeError as error:
                return f"[布吉岛] {error}"
        return None

    async def heartbeat(self, websocket: Any) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_interval / 1000)
            await websocket.send(json.dumps({"op": 1, "d": self.sequence}))

    # =========================================================================
    # 群管：入群申请审批 + 退群自动拉黑（图片卡片通知，管理员群内批复）
    # -------------------------------------------------------------------------
    # 设计要点：
    #  - 入群申请事件 GROUP_JOIN_REQUEST(intent 1<<25) 已订阅；成员退群事件
    #    GROUP_MEMBER_REMOVE(intent 1<<24) 需在 websocket 鉴权时额外开启（见 websocket_loop）。
    #  - 退群事件只有 member_openid/user_openid，没有昵称，因此用 member_names
    #    缓存（来自群消息 / 入群事件）补全展示名。
    #  - auto_blacklist_enabled 为「退群自动拉黑」子开关，默认关闭，必须管理员在群内
    #    发「退群拉黑 开启」才生效（满足“需管理员发信启动”的要求）。
    #  - 黑名单本地落盘 group_blacklist.json，跨重启保留；自动拒绝时同时调用官方
    #    add_to_member_blacklist 让 QQ 原生黑名单兜底，提升跨进退群的稳定性。
    # =========================================================================

    def _load_blacklist(self) -> None:
        """从 group_blacklist.json 读取本地黑名单（退群自动拉黑名单，跨重启保留）。"""
        try:
            if self._blacklist_path.exists():
                with open(self._blacklist_path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                if isinstance(data, dict):
                    self.blacklisted_openids = data
                    log.info("已载入群黑名单 %d 条", len(self.blacklisted_openids))
        except Exception as error:
            log.warning("读取群黑名单失败：%s", error)
            self.blacklisted_openids = {}

    def _save_blacklist(self) -> None:
        try:
            with open(self._blacklist_path, "w", encoding="utf-8") as fh:
                json.dump(self.blacklisted_openids, fh, ensure_ascii=False, indent=2)
        except Exception as error:
            log.warning("写入群黑名单失败：%s", error)

    def _is_blacklisted(self, member_openid: str, union_openid: str = "") -> bool:
        if member_openid and member_openid in self.blacklisted_openids:
            return True
        if union_openid:
            for entry in self.blacklisted_openids.values():
                if entry.get("union_openid") and entry.get("union_openid") == union_openid:
                    return True
        return False

    def _add_to_blacklist(self, member_openid: str, user_openid: str,
                          username: str, reason: str) -> None:
        """把退群成员加入本地黑名单；若 user_openid 已存在则归并（member_openid
        可能因进退群而变化，归并可提升再次申请时的命中率）。"""
        with self._group_mod_lock:
            existing = None
            if user_openid:
                for entry in self.blacklisted_openids.values():
                    if entry.get("user_openid") and entry.get("user_openid") == user_openid:
                        existing = entry
                        break
            if existing is None:
                existing = {
                    "added_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "user_openid": user_openid or "",
                    "union_openid": "",
                    "username": username or "",
                    "reason": reason,
                    "aliases": [],
                }
                self.blacklisted_openids[member_openid] = existing
            if member_openid and member_openid not in existing["aliases"]:
                existing["aliases"].append(member_openid)
            # 让当前（可能全新的）member_openid 也能直接命中
            if member_openid:
                self.blacklisted_openids[member_openid] = existing
            self._save_blacklist()

    def _remove_from_blacklist(self, key: str) -> bool:
        with self._group_mod_lock:
            entry = self.blacklisted_openids.get(key)
            if entry is None:
                for v in self.blacklisted_openids.values():
                    if key in (v.get("aliases") or []):
                        entry = v
                        break
            if entry is None:
                return False
            for k in [k for k, v in self.blacklisted_openids.items() if v is entry]:
                del self.blacklisted_openids[k]
            self._save_blacklist()
            return True

    def _mod_item(self, label: str, value: str) -> str:
        """卡片内的「标签 : 值」行（参考用户提供的渐变白卡样式）。"""
        return (
            "<div class=\"item\"><div class=\"label\">" + _esc(label) + "</div>"
            "<div class=\"value\">" + _esc(value) + "</div></div>"
        )

    def _mod_card_html(self, title: str, accent: str, body_html: str, footer: str) -> str:
        """统一风格的群管图片卡片 HTML（白卡 + 渐变头部 + 阴影，参考用户提供的样式）。

        accent 仅用于决定头部渐变配色：红色用红渐变，其余用橙→绿渐变。
        """
        font_face = ""
        try:
            if self.card_renderer is not None:
                font_face = self.card_renderer.font_face
        except Exception:
            font_face = ""
        if accent == "#d23b3b":
            grad = "linear-gradient(135deg,#ff6a6a,#d23b3b)"
        else:
            grad = "linear-gradient(135deg,#ff8822,#27b960)"
        return (
            "<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"UTF-8\"><style>"
            + font_face
            + "*{margin:0;padding:0;box-sizing:border-box;"
              "font-family:'GenJyuuGothic','Microsoft YaHei','PingFang SC',sans-serif;}"
            " body{background:#f3f6fc;padding:40px;display:flex;justify-content:center;}"
            " .card{width:620px;border-radius:18px;overflow:hidden;"
              "box-shadow:0 8px 24px rgba(30,60,140,.16);background:#fff;}"
            " .card-header{background:" + grad + ";padding:24px 28px;}"
            " .card-header h1{color:#fff;font-size:32px;font-weight:600;}"
            " .card-body{padding:30px 28px;}"
            " .item{display:flex;margin-bottom:22px;align-items:center;}"
            " .label{width:120px;font-size:22px;color:#808694;}"
            " .value{font-size:24px;color:#1d2333;}"
            " .msg{background:#f5f8ff;border:1px dashed #c9d6f0;border-radius:10px;"
              "padding:12px 14px;margin:10px 0;font-size:18px;color:#4a5568;}"
            " .footer{font-size:16px;color:#9aa3b2;text-align:center;margin-top:18px;"
              "border-top:1px solid #eef1f6;padding-top:14px;}"
            "</style></head><body><div class=\"card\">"
            "<div class=\"card-header\"><h1>" + _esc(title) + "</h1></div>"
            "<div class=\"card-body\">"
            + body_html
            + ("<div class=\"footer\">" + _esc(footer) + "</div>" if footer else "")
            + "</div></div></body></html>"
        )

    async def _send_mod_card(self, html: str, text_fallback: str, keyboard: Any = None) -> None:
        """群管卡片：图片与按钮分两条发——QQ 客户端不会在图片(富媒体)消息上渲染内联键盘，
        所以卡片走纯图片，按钮单独挂一条文本消息（文本消息才能稳定显示内联键盘）。"""
        # 1) 先发卡片（图片或文本），均不带键盘
        sent_card = False
        if self.card_renderer is not None:
            try:
                sent_card = await self.send_card(html, "")
            except Exception as error:
                log.warning("群管卡片渲染失败，回退文本：%s", error)
        if not sent_card:
            await self.send_group_message_async(text_fallback, "")
        # 2) 按钮单独挂一条 Markdown 消息（纯文本 + 键盘在 QQ 群不渲染按钮，必须 msg_type=2）
        if keyboard is not None:
            await self.send_markdown_with_keyboard("👇 点击下方按钮进行操作：", keyboard, "")

    def _call_approval_api(self, member_openid: str, op: str,
                           join_request_id: str = "", reject_reason: str = "",
                           add_to_member_blacklist: bool = False) -> bool:
        """调用官方入群审批接口（approve/decline）。同步阻塞，调用方用 to_thread 包裹。"""
        url = (f"{QQ_API}/v2/groups/{self.group_openid}"
               f"/approval_join_request/{member_openid}")
        body: Dict[str, Any] = {"op": op}
        if join_request_id:
            body["join_request_id"] = join_request_id
        if op == "decline" and reject_reason:
            body["reject_reason"] = reject_reason
        if add_to_member_blacklist:
            body["add_to_member_blacklist"] = True
        try:
            response = requests.post(url, headers=self.auth_headers(), json=body, timeout=15)
            if response.status_code in (401, 403):
                self.get_access_token(force=True)
                response = requests.post(url, headers=self.auth_headers(), json=body, timeout=15)
            if response.status_code < 200 or response.status_code >= 300:
                log.warning("审批接口返回错误 HTTP %s：%s", response.status_code, response.text[:200])
                return False
            return True
        except Exception as error:
            log.warning("调用审批接口失败：%s", error)
            return False

    async def handle_join_request(self, data: Dict[str, Any]) -> None:
        """处理 GROUP_JOIN_REQUEST：缓存昵称 -> 命中黑名单则自动拒绝 -> 否则群内卡片通知待审批。"""
        if not self.group_mod_enabled:
            return
        event_group = str(data.get("group_openid", ""))
        if event_group and event_group != self.group_openid:
            return
        if data.get("auto_approved"):
            # QQ 已按策略自动通过，无需人工审批；仅缓存昵称
            mo = str(data.get("member_openid", ""))
            uname = str(data.get("username", ""))
            if mo and uname:
                self.member_names[mo] = uname
            return

        member_openid = str(data.get("member_openid", ""))
        if not member_openid:
            return
        join_request_id = str(data.get("join_request_id", ""))
        username = str(data.get("username", "")) or member_openid
        union_openid = str(data.get("union_openid", "")) or ""
        apply_at = str(data.get("apply_at", ""))
        apply_source = str(data.get("apply_source", ""))
        verify = data.get("verify_info") or {}
        verify_msg = str(verify.get("verify_message", "")) if isinstance(verify, dict) else ""

        self.member_names[member_openid] = username

        # 命中黑名单：自动拒绝（仅当自动拉黑功能开启时有意义）
        if self.auto_blacklist_enabled and self._is_blacklisted(member_openid, union_openid):
            await self._auto_decline_join_request(member_openid, join_request_id, username)
            return

        with self._group_mod_lock:
            self.pending_join_requests[member_openid] = {
                "join_request_id": join_request_id,
                "username": username,
                "union_openid": union_openid,
                "apply_at": apply_at,
                "apply_source": apply_source,
                "verify_msg": verify_msg,
            }
        src_text = "被邀请" if apply_source == "invited" else "主动申请"
        body = (
            self._mod_item("申请人", username)
            + self._mod_item("OpenID", member_openid)
            + self._mod_item("来源", src_text)
            + (self._mod_item("验证消息", verify_msg) if verify_msg else "")
            + (self._mod_item("申请时间", apply_at) if apply_at else "")
        )
        footer = "点击下方按钮审批，或发「同意/拒绝 <OpenID>」"
        html = self._mod_card_html("🛡 入群申请待审批", "#f5821f", body, footer)
        text = (f"[群管] 收到入群申请：{username}（{src_text}）\n"
                f"OpenID：{member_openid}\n"
                + (f"验证消息：{verify_msg}\n" if verify_msg else "")
                + "管理员可点击下方按钮，或发「同意/拒绝 <OpenID>」审批")
        keyboard = self._join_request_keyboard(member_openid)
        await self._send_mod_card(html, text, keyboard)

    async def _auto_decline_join_request(self, member_openid: str,
                                         join_request_id: str, username: str) -> None:
        """黑名单命中时自动拒绝入群申请，并同步官方黑名单。"""
        ok = await asyncio.to_thread(
            self._call_approval_api, member_openid, "decline", join_request_id,
            "曾退群，已加入黑名单，自动拒绝", True)
        name = username or self.member_names.get(member_openid, member_openid)
        body = (
            self._mod_item("申请人", name)
            + self._mod_item("OpenID", member_openid)
            + "<div class=\"msg\">该用户曾退群且已加入黑名单，入群申请已自动拒绝，"
              "并同步加入 QQ 官方群黑名单。</div>"
        )
        footer = "退群自动拉黑开启中；如误伤可在群内发「黑名单 移除 <OpenID>」解封。"
        html = self._mod_card_html("🚫 入群申请已自动拒绝", "#d23b3b", body, footer)
        text = f"[群管] 已自动拒绝 {name} 的入群申请（曾退群，已拉黑）。"
        await self._send_mod_card(html, text)
        log.info("自动拒绝黑名单用户入群申请 %s：%s", member_openid, ok)

    async def handle_member_remove(self, data: Dict[str, Any]) -> None:
        """处理 GROUP_MEMBER_REMOVE：记录/通知；若开启自动拉黑则加入黑名单。"""
        if not self.group_mod_enabled:
            return
        event_group = str(data.get("group_openid", ""))
        if event_group and event_group != self.group_openid:
            return
        member_openid = str(data.get("member_openid", ""))
        if not member_openid:
            return
        user_openid = str(data.get("user_openid", "")) or ""
        name = self.member_names.get(member_openid, member_openid)
        blacklisted_now = False
        if self.auto_blacklist_enabled:
            self._add_to_blacklist(member_openid, user_openid, name, "退群自动拉黑")
            blacklisted_now = True
        body = (
            self._mod_item("成员", name)
            + self._mod_item("OpenID", member_openid)
        )
        if blacklisted_now:
            body += ("<div class=\"msg\">⚠ 检测到退群，已自动加入群黑名单。"
                     "该成员再次申请入群将被自动拒绝。</div>")
            footer = "退群自动拉黑已开启。关闭请群内发「退群拉黑 关闭」。"
            html = self._mod_card_html("⚠ 成员退群 · 已拉黑", "#d23b3b", body, footer)
            text = f"[群管] 成员 {name} 退群，已自动加入黑名单。"
        else:
            body += "<div class=\"msg\">检测到成员退群（退群自动拉黑未开启，仅做记录）。</div>"
            footer = "开启退群自动拉黑请群内发「退群拉黑 开启」。"
            html = self._mod_card_html("⚠ 成员退群", "#f5821f", body, footer)
            text = f"[群管] 成员 {name} 退群。"
        await self._send_mod_card(html, text, self._leave_blacklist_keyboard(member_openid))
        # 通知子插件钩子（与上游 on_member_left 一致）
        await self.dispatch_pymod_hook(
            "on_member_left",
            member_openid=member_openid,
            username=str(name),
            qq_number=str(data.get("qq_number") or data.get("qq") or data.get("uin") or ""),
            avatar_url=str(data.get("avatar_url") or data.get("avatar") or ""),
        )

    # =========================================================================
    # 内联键盘（按钮）/ 互动事件 / 指令菜单面板
    # =========================================================================

    @staticmethod
    def _kb_callback(bid: str, label: str, data: str, style: int = 1) -> Dict[str, Any]:
        """回调按钮：点击触发 INTERACTION_CREATE（不自动发消息）。用于审批 / 菜单导航 / 菜单叶子指令。
        注意：permission.type 必须是 2(所有人) 才能渲染按钮；type=0(指定用户) 且列表为空会让按钮对所有人不可见。
        真正的权限（如管理员审批）由后端在 handle_interaction 里校验。"""
        return {
            "id": bid,
            "render_data": {"label": label, "visited_label": label, "style": style},
            "action": {
                "type": 1,
                "permission": {"type": 2},
                "data": data,
                "enter": False,
                "reply": False,
            },
        }

    @staticmethod
    def _kb_command(bid: str, label: str, data: str, style: int = 1) -> Dict[str, Any]:
        """指令按钮：type=2 点击把 data 插入输入框（enter 仅单聊自动发送，群聊无效）。
        群聊里请改用 _kb_callback 由后端 _dispatch_button_command 代为执行。本方法保留供单聊场景。"""
        return {
            "id": bid,
            "render_data": {"label": label, "visited_label": label, "style": style},
            "action": {
                "type": 2,
                "permission": {"type": 2},
                "data": data,
                "enter": True,
                "reply": True,
            },
        }

    def _join_request_keyboard(self, member_openid: str) -> Dict[str, Any]:
        """入群申请卡片底部按钮：同意 / 拒绝 / 拒绝并拉黑。"""
        return {
            "content": {
                "rows": [
                    {
                        "buttons": [
                            self._kb_callback("approve_" + member_openid, "✅ 同意进群",
                                              "approve|" + member_openid, 1),
                            self._kb_callback("reject_" + member_openid, "⛔ 驳回",
                                              "reject|" + member_openid, 3),
                        ]
                    },
                    {
                        "buttons": [
                            self._kb_callback("blacklist_" + member_openid, "🚫 拒绝并拉黑",
                                              "blacklist|" + member_openid, 3),
                        ]
                    },
                ]
            }
        }

    def _leave_blacklist_keyboard(self, member_openid: str) -> Dict[str, Any]:
        """退群事件卡片底部按钮：拉黑该用户 / 启用退群自动拉黑并拉黑该用户。"""
        return {
            "content": {
                "rows": [
                    {
                        "buttons": [
                            self._kb_callback("leave_ban_" + member_openid, "🚫 拉黑这个用户",
                                              "leave_ban|" + member_openid, 3),
                        ]
                    },
                    {
                        "buttons": [
                            self._kb_callback("leave_ban_on_" + member_openid,
                                              "🔒 启用退群拉黑并拉黑",
                                              "leave_ban_enable|" + member_openid, 3),
                        ]
                    },
                ]
            }
        }

    # ---- 互动事件（按钮点击）----
    async def handle_interaction(self, data: Dict[str, Any]) -> None:
        """处理 INTERACTION_CREATE：先 PUT 回应清除 loading，再按 button_data 派发。"""
        interaction_id = str(data.get("id", ""))
        itype = (data.get("data") or {}).get("type")
        group_openid = str(data.get("group_openid", ""))
        if group_openid and self.group_openid and group_openid != self.group_openid:
            return
        resolved = (data.get("data") or {}).get("resolved") or {}
        button_data = str(resolved.get("button_data", "") or "")
        clicker = str(data.get("group_member_openid", "") or "")
        # 必须在 3 秒内回应，否则客户端一直 loading
        await self._respond_interaction(interaction_id, 0)
        if itype != 11 or not button_data:
            return
        # 菜单导航（回调按钮）
        if button_data.startswith("menu:"):
            try:
                await self._handle_menu_interaction(button_data, clicker)
            except Exception as error:
                log.warning("处理菜单交互失败：%s", error)
            return
        # 退群事件按钮（拉黑 / 启用退群拉黑并拉黑）
        if button_data.startswith("leave_ban|") or button_data.startswith("leave_ban_enable|"):
            op, _, target = button_data.partition("|")
            if self.group_mod_enabled:
                await self._handle_leave_button(op, target, clicker)
            return
        # 入群审批（同意 / 拒绝 / 拒绝拉黑）
        if "|" in button_data:
            op, _, target = button_data.partition("|")
            if op in ("approve", "reject", "blacklist") and self.group_mod_enabled:
                await self._handle_join_button(op, target, clicker)
            return
        # 菜单叶子指令按钮（群聊里 enter 不自动发送，由后端代为执行）
        try:
            await self._dispatch_button_command(button_data.strip(), clicker)
        except Exception as error:
            log.warning("处理菜单指令按钮失败：%s", error)

    async def _dispatch_button_command(self, content: str, clicker: str) -> None:
        """菜单叶子指令按钮（type=1 回调）在群聊里由后端代为执行：
        复刻 handle_event 的分发（本地命令→执行指令→pymods→转发后端）。"""
        msg_id = ""
        sender_name = ""
        local_reply = await self.handle_local_command(content, clicker, sender_name, msg_id)
        if local_reply is _IMAGE_SENT:
            return
        if local_reply is not None:
            await self.send_group_message_async(local_reply, msg_id)
            return
        if content.startswith(self.command_exec + " "):
            # 放到线程池执行：exec 会等待 LSE 主线程跑完指令（最长约 8s），
            # 同步调用会卡死整个网关事件循环（QQ 消息/WebUI 全部无响应）。
            exec_reply = await asyncio.to_thread(self.handle_exec_command, content, clicker)
            await self.send_group_message_async(exec_reply, msg_id)
            return
        if await self._dispatch_pymods(content, clicker, sender_name, msg_id):
            return
        for backend in self.backends:
            try:
                await self.post_to_backend(backend, sender_name, content, msg_id, clicker)
            except Exception as error:
                log.warning("转发菜单指令到 %s 失败：%s", backend["name"], error)

    async def _respond_interaction(self, interaction_id: str, code: int = 0) -> None:
        if not interaction_id:
            return
        try:
            await asyncio.to_thread(self._put_interaction, interaction_id, code)
        except Exception as error:
            log.warning("回应互动事件失败：%s", error)

    def _put_interaction(self, interaction_id: str, code: int = 0) -> None:
        """PUT /interactions/{interaction_id} 回应按钮点击，清除客户端 loading。"""
        url = f"{QQ_API}/interactions/{interaction_id}"
        try:
            resp = requests.put(url, headers=self.auth_headers(), json={"code": code}, timeout=10)
            if resp.status_code in (401, 403):
                self.get_access_token(force=True)
                resp = requests.put(url, headers=self.auth_headers(), json={"code": code}, timeout=10)
            if resp.status_code < 200 or resp.status_code >= 300:
                log.warning("互动回应返回 HTTP %s：%s", resp.status_code, resp.text[:200])
        except Exception as error:
            log.warning("PUT /interactions 失败：%s", error)

    async def _handle_join_button(self, op: str, target: str, clicker: str) -> None:
        """处理入群审批按钮点击（op=approve/reject/blacklist）。"""
        if clicker not in self.admin_openids:
            await self.send_group_message_async(
                "[群管] 你不是管理员，无权审批入群申请。", "")
            return
        op_map = {"approve": ("approve", False), "reject": ("decline", False),
                  "blacklist": ("decline", True)}
        api_op, do_blacklist = op_map[op]
        entry = self.pending_join_requests.get(target)
        if entry is None:
            await self.send_group_message_async(
                f"[群管] 未找到待审批的申请（可能已处理）：{target}", "")
            return
        reply = await self._admin_resolve_and_act(
            target, api_op, do_blacklist, clicker or "管理员")
        await self.send_group_message_async(reply, "")

    async def _handle_leave_button(self, op: str, target: str, clicker: str) -> None:
        """处理退群事件卡片按钮：拉黑该用户 / 启用退群自动拉黑并拉黑。管理员专属。"""
        if clicker not in self.admin_openids:
            await self.send_group_message_async(
                "[群管] 你不是管理员，无权操作退群拉黑。", "")
            return
        name = self.member_names.get(target, target)
        if op == "leave_ban":
            self._add_to_blacklist(target, "", name, "管理员手动拉黑")
            await self.send_group_message_async(
                f"[群管] 已将 {name}（{target}）加入群黑名单。"
                "该成员再次申请入群将被自动拒绝。", "")
            return
        if op == "leave_ban_enable":
            self.auto_blacklist_enabled = True
            try:
                self._config["auto_blacklist_enabled"] = True
                _save_config_file(self._config)
            except Exception as error:
                log.warning("持久化退群自动拉黑开关失败：%s", error)
            self._add_to_blacklist(target, "", name, "管理员手动拉黑")
            await self.send_group_message_async(
                f"[群管] 已开启退群自动拉黑，并将 {name}（{target}）加入群黑名单。"
                "后续成员退群将自动拉黑、再次申请自动拒绝（设置已保存，重启后保留）。", "")
            return
        await self.send_group_message_async(f"[群管] 未知的退群操作：{op}", "")

    # ---- 指令菜单面板 ----
    def _menu_main_card_html(self) -> str:
        body = (
            self._mod_item("🛠 查服", "状态 / 玩家 / 坐标 / 模式 / 血量 / TPS")
            + self._mod_item("🎮 娱乐·工具", "猜数 / 我的OpenID / 执行 / 帮助")
            + self._mod_item("👮 群管", "入群审批 / 退群拉黑 / 黑名单")
            + self._mod_item("💡 用法", "点下方按钮，或发「帮助」看完整指令")
        )
        return self._mod_card_html("🤖 指令菜单面板", "#f5821f", body, "点击按钮即可使用对应功能")

    def _menu_main_keyboard(self) -> Dict[str, Any]:
        return {"content": {"rows": [
            {"buttons": [
                self._kb_callback("m_serve", "🛠 查服功能", "menu:serve", 1),
                self._kb_callback("m_fun", "🎮 娱乐·工具", "menu:fun", 1),
            ]},
            {"buttons": [
                self._kb_callback("m_group", "👮 群管面板", "menu:group", 1),
                self._kb_callback("m_help", "❓ 帮助", "帮助", 1),
            ]},
        ]}}

    def _menu_serve_keyboard(self) -> Dict[str, Any]:
        return {"content": {"rows": [
            {"buttons": [
                self._kb_callback("s_tps", "📊 查TPS", "查TPS", 1),
                self._kb_callback("s_world", "🌍 查世界", "查世界", 1),
            ]},
            {"buttons": [
                self._kb_callback("s_player", "👤 查玩家", "查玩家", 1),
                self._kb_callback("s_info", "🪪 个人信息", "个人信息", 1),
            ]},
            {"buttons": [
                self._kb_callback("s_pos", "📍 查坐标", "查坐标", 1),
                self._kb_callback("s_mode", "🎛 查模式", "查模式", 1),
            ]},
            {"buttons": [
                self._kb_callback("s_hp", "❤ 查血量", "查血量", 1),
                self._kb_callback("s_back", "🔙 返回主菜单", "menu:main", 0),
            ]},
        ]}}

    def _menu_serve_card_html(self) -> str:
        body = (
            self._mod_item("📊 查TPS", "服务器 TPS")
            + self._mod_item("🌍 查世界", "世界信息")
            + self._mod_item("👤 查玩家", "查玩家 <名称>")
            + self._mod_item("🪪 个人信息", "个人资料")
            + self._mod_item("📍 查坐标", "坐标")
            + self._mod_item("🎛 查模式", "游戏模式")
            + self._mod_item("❤ 查血量", "血量")
        )
        return self._mod_card_html("🛠 查服功能", "#f5821f", body, "点按钮直接查询")

    def _menu_fun_keyboard(self) -> Dict[str, Any]:
        return {"content": {"rows": [
            {"buttons": [
                self._kb_callback("f_guess", "🎲 猜数游戏", "猜数", 1),
                self._kb_callback("f_openid", "🪪 我的OpenID", "我的openid", 1),
            ]},
            {"buttons": [
                self._kb_callback("f_exec", "⚡ 执行指令", "执行", 1),
                self._kb_callback("f_help", "❓ 帮助", "帮助", 1),
            ]},
            {"buttons": [
                self._kb_callback("f_back", "🔙 返回主菜单", "menu:main", 0),
            ]},
        ]}}

    def _menu_fun_card_html(self) -> str:
        body = (
            self._mod_item("🎲 猜数游戏", "猜 1-100 数字")
            + self._mod_item("🪪 我的OpenID", "获取你的 OpenID")
            + self._mod_item("⚡ 执行指令", "执行 <服务器> <指令>")
            + self._mod_item("❓ 帮助", "完整指令列表")
        )
        return self._mod_card_html("🎮 娱乐·工具", "#f5821f", body, "点按钮直接使用")

    def _menu_group_keyboard(self) -> Dict[str, Any]:
        return {"content": {"rows": [
            {"buttons": [
                self._kb_callback("g_list", "📋 申请列表", "申请列表", 1),
                self._kb_callback("g_state", "🔘 退群拉黑状态", "退群拉黑 状态", 1),
            ]},
            {"buttons": [
                self._kb_callback("g_bl", "🚫 黑名单列表", "黑名单 列表", 1),
                self._kb_callback("g_back", "🔙 返回主菜单", "menu:main", 0),
            ]},
        ]}}

    def _menu_group_card_html(self) -> str:
        body = (
            self._mod_item("📋 申请列表", "查看待审批入群申请")
            + self._mod_item("🔘 退群拉黑状态", "开启/关闭自动拉黑")
            + self._mod_item("🚫 黑名单列表", "查看/移除黑名单")
            + self._mod_item("✅ 审批方式", "入群申请卡片上点按钮")
        )
        return self._mod_card_html("👮 群管面板", "#f5821f", body, "审批请在入群申请卡片上点按钮")

    async def _send_menu_card(self, html: str, keyboard: Dict[str, Any]) -> None:
        """菜单卡片：图片与按钮分两条——图片不带键盘，按钮挂单独 Markdown 消息
        （纯文本 + 键盘在 QQ 群不渲染按钮，必须 msg_type=2）。"""
        sent_image = False
        if self.card_renderer is not None:
            try:
                sent_image = await self.send_card(html, "")
            except Exception as error:
                log.warning("菜单卡片渲染失败：%s", error)
        # 按钮必须走 Markdown 消息才能渲染
        if sent_image:
            await self.send_markdown_with_keyboard("👇 点击下方按钮选择功能：", keyboard, "")
        else:
            await self.send_markdown_with_keyboard(
                "**[菜单]** 查服 / 娱乐工具 / 群管 / 帮助 —— 发「帮助」看完整指令。", keyboard, "")

    async def _send_menu_main(self) -> None:
        await self._send_menu_card(self._menu_main_card_html(), self._menu_main_keyboard())

    async def _send_menu_serve(self) -> None:
        await self._send_menu_card(self._menu_serve_card_html(), self._menu_serve_keyboard())

    async def _send_menu_fun(self) -> None:
        await self._send_menu_card(self._menu_fun_card_html(), self._menu_fun_keyboard())

    async def _send_menu_group(self) -> None:
        await self._send_menu_card(self._menu_group_card_html(), self._menu_group_keyboard())

    async def _handle_menu_interaction(self, button_data: str, clicker: str) -> None:
        sub = button_data.split(":", 1)[-1]
        if sub == "serve":
            await self._send_menu_serve()
        elif sub == "fun":
            await self._send_menu_fun()
        elif sub == "group":
            if self.group_mod_enabled:
                await self._send_menu_group()
            else:
                await self.send_group_message_async("[群管] 群管功能未开启。", "")
        elif sub == "main":
            await self._send_menu_main()

    # ---- 群管管理员指令（在 handle_local_command 中路由）----

    async def _handle_group_mod_command(self, content: str, sender_openid: str,
                                        sender_name: str, msg_id: str) -> Optional[str]:
        """解析管理员群管指令，返回回复文本；非管理员或非群管指令返回 None。"""
        if not sender_openid or sender_openid not in self.admin_openids:
            return None
        text = content.strip()

        # 退群拉黑开关
        if text.startswith("退群拉黑"):
            sub = text.split(" ", 1)[-1].strip() if " " in text else ""
            if sub in ("开启", "打开", "on", "开"):
                self.auto_blacklist_enabled = True
                return "[群管] 退群自动拉黑已开启：成员退群将自动拉黑，再次申请自动拒绝。"
            if sub in ("关闭", "关掉", "off", "关"):
                self.auto_blacklist_enabled = False
                return "[群管] 退群自动拉黑已关闭（已拉黑的名单仍保留，可手动移除）。"
            if sub in ("状态", "state"):
                state = "开启" if self.auto_blacklist_enabled else "关闭"
                master = "开启" if self.group_mod_enabled else "关闭"
                return f"[群管] 退群自动拉黑：{state}（群管总开关：{master}）。"
            return "[群管] 退群拉黑指令：退群拉黑 开启 / 退群拉黑 关闭 / 退群拉黑 状态"

        # 申请列表
        if text == "申请列表":
            return self._format_pending_list()

        # 黑名单列表 / 移除
        if text.startswith("黑名单"):
            parts = text.split(" ", 2)
            sub = parts[1] if len(parts) > 1 else ""
            if sub == "列表":
                return self._format_blacklist()
            if sub == "移除" and len(parts) > 2:
                key = parts[2].strip()
                ok = self._remove_from_blacklist(key)
                return ("[群管] 已从黑名单移除：" + key) if ok else ("[群管] 未在黑名单中找到：" + key)
            return "[群管] 黑名单指令：黑名单 列表 / 黑名单 移除 <OpenID>"

        # 同意 / 拒绝 / 拒绝拉黑
        for prefix, op, do_blacklist in (
            ("同意 ", "approve", False),
            ("拒绝拉黑 ", "decline", True),
            ("拒绝 ", "decline", False),
        ):
            if text.startswith(prefix):
                target = text[len(prefix):].strip()
                return await self._admin_resolve_and_act(target, op, do_blacklist, sender_name)
        return None

    async def _admin_resolve_and_act(self, target: str, op: str,
                                     do_blacklist: bool, sender_name: str) -> str:
        """按序号或 OpenID 解析待审批申请，调用官方接口处理。"""
        with self._group_mod_lock:
            keys = list(self.pending_join_requests.keys())
        if re.fullmatch(r"\d+", target):
            idx = int(target)
            if idx < 1 or idx > len(keys):
                return f"[群管] 序号超出范围，当前待审批 {len(keys)} 条。发「申请列表」查看。"
            member_openid = keys[idx - 1]
        else:
            member_openid = target
        entry = self.pending_join_requests.get(member_openid)
        if entry is None:
            return f"[群管] 未找到待审批的申请：{target}（可能已处理）。发「申请列表」查看。"
        join_request_id = entry.get("join_request_id", "")
        username = entry.get("username", member_openid)
        reject_reason = "管理员拒绝" if op == "decline" else ""
        ok = await asyncio.to_thread(
            self._call_approval_api, member_openid, op, join_request_id,
            reject_reason, do_blacklist)
        with self._group_mod_lock:
            self.pending_join_requests.pop(member_openid, None)
        if do_blacklist:
            self._add_to_blacklist(
                member_openid, entry.get("user_openid", "") or "", username, "管理员拒绝并拉黑")
        action = "通过" if op == "approve" else ("拒绝并拉黑" if do_blacklist else "拒绝")
        if ok:
            return f"[群管] 已{action} {username}（{member_openid}）的入群申请。"
        return (f"[群管] 调用审批接口失败，{username} 的请求未处理"
                f"（请稍后重试，并确认机器人拥有群管理员权限）。")

    def _format_pending_list(self) -> str:
        with self._group_mod_lock:
            items = list(self.pending_join_requests.items())
        if not items:
            return "[群管] 当前没有待审批的入群申请。"
        lines = ["[群管] 待审批入群申请（共 %d 条）：" % len(items)]
        for i, (mo, e) in enumerate(items, 1):
            src = "被邀请" if e.get("apply_source") == "invited" else "主动申请"
            verify = e.get("verify_msg")
            line = f"{i}. {e.get('username', mo)}（{src}）"
            if verify:
                line += f" 验证：「{verify}」"
            line += f"  OpenID:{mo}"
            lines.append(line)
        lines.append("回复示例：同意 1 / 拒绝 1 / 拒绝拉黑 1")
        return "\n".join(lines)

    def _format_blacklist(self) -> str:
        with self._group_mod_lock:
            items = list(self.blacklisted_openids.items())
        if not items:
            return "[群管] 黑名单为空。"
        lines = ["[群管] 群黑名单（共 %d 条）：" % len(items)]
        for i, (mo, e) in enumerate(items, 1):
            lines.append(
                f"{i}. {e.get('username', mo)}  OpenID:{mo}  原因:{e.get('reason', '')}  "
                f"加入:{e.get('added_at', '')}")
        lines.append("移除：黑名单 移除 <OpenID>")
        return "\n".join(lines)

    async def handle_event(self, event_type: str, data: Dict[str, Any]) -> None:
        # ---- 群管事件：入群申请 / 成员退群（先于普通消息解析）----
        if event_type == "GROUP_JOIN_REQUEST":
            await self.handle_join_request(data)
            return
        if event_type == "GROUP_MEMBER_REMOVE":
            await self.handle_member_remove(data)
            return
        if event_type == "GROUP_MEMBER_ADD":
            event_group_openid = str(data.get("group_openid", ""))
            if event_group_openid == self.group_openid:
                member = data.get("member") if isinstance(data.get("member"), dict) else {}
                member_openid = str(data.get("member_openid") or member.get("member_openid")
                                    or member.get("user_openid") or "")
                if member_openid:
                    await self.dispatch_pymod_hook(
                        "on_member_joined",
                        member_openid=member_openid,
                        username=str(data.get("username") or data.get("member_name")
                                     or member.get("username") or member.get("member_name")
                                     or member.get("nickname") or ""),
                        qq_number=str(data.get("qq_number") or data.get("qq") or data.get("uin")
                                     or member.get("qq_number") or member.get("qq") or member.get("uin") or ""),
                        avatar_url=str(data.get("avatar_url") or data.get("avatar")
                                      or member.get("avatar_url") or member.get("avatar") or ""),
                    )
            return
        if event_type == "INTERACTION_CREATE":
            await self.handle_interaction(data)
            return

        if event_type not in {"GROUP_MESSAGE_CREATE", "GROUP_AT_MESSAGE_CREATE"}:
            log.debug("忽略 QQ Gateway 事件：%s", event_type or "未知")
            return

        event_group_openid = str(data.get("group_openid", ""))
        if event_group_openid != self.group_openid:
            log.warning(
                "忽略非目标群消息：事件群 OpenID=%s，当前配置=%s",
                event_group_openid or "无",
                self.group_openid,
            )
            return

        content = str(data.get("content", "")).strip()
        if not content:
            return

        sender_openid = (
            data.get("author", {}).get("member_openid")
            or data.get("author", {}).get("user_openid")
            or ""
        )
        sender = sender_openid or "QQ群"
        sender_name = (
            data.get("author", {}).get("member_name")
            or data.get("author", {}).get("username")
            or data.get("author", {}).get("nickname")
            or sender
        )
        content = content.replace("\u2005", " ").strip()
        if content.startswith("<@"):
            content = content.split(">", 1)[-1].strip()
        # 兼容 QQ「指令面板」：用户在输入框输 “/” 拉起的指令面板中点选指令后，
        # 下发的消息会以 “/指令名” 形式到达（官方文档要求代码侧指令实现也带 “/”）。
        # 这里去掉前导 “/” 再分发，使后台登记的 /指令 能命中本地命令（菜单/帮助/查TPS…）。
        # 注意：指令面板的指令必须在开发者后台「发布设置→功能配置→指令配置」登记后才会出现。
        if content.startswith("/"):
            content = content[1:].strip()
        if not content:
            return

        msg_id = str(data.get("id", ""))
        if not msg_id:
            log.warning("收到 QQ 群消息但事件中没有 msg_id，无法发送群回复")
        log.info(
            "收到 QQ 群消息 [%s] openid=%s，msg_id=%s：%s",
            sender_name,
            sender_openid or "无",
            msg_id or "无",
            content,
        )
        self.push_log("qq", f"[QQ] {sender_name}：{content}")
        # 缓冲 QQ 入站消息，供网页地图(BDSLM_JS)经 /qqmcbridge/qqlog 轮询回显到网页聊天框。
        try:
            self.qq_inbound_log.append({
                "time": int(time.time() * 1000),
                "sender": sender_name,
                "content": content,
            })
        except Exception:
            pass

        if self.bugland_config.get("bugland_enabled"):
            try:
                bugland_reply = await self.handle_bugland(content)
            except Exception as error:
                log.warning("处理布吉岛命令失败：%s", error)
                bugland_reply = None
            if bugland_reply is not None:
                if msg_id:
                    log.info("发送布吉岛命令回复，msg_id=%s", msg_id)
                else:
                    log.info("发送布吉岛主动群消息")
                await self.send_group_message_async(bugland_reply, msg_id)
                return

        if content == self.command_server:
            try:
                server_reply = await self.handle_server_query(msg_id)
            except Exception as error:
                log.warning("处理查服命令失败：%s", error)
                server_reply = f"[查服] 查询失败：{error}"
            if server_reply is not None:
                self.push_log("mc", server_reply)
                if msg_id:
                    log.info("发送查服命令回复，msg_id=%s", msg_id)
                else:
                    log.info("发送查服主动群消息")
                await self.send_group_message_async(server_reply, msg_id)
            return

        # Python 端直接处理的命令（帮助 / OpenID / 猜数），只回复一次，不转发多服
        if self.respond_to_commands:
            local_reply = await self.handle_local_command(
                content, sender_openid, sender_name, msg_id
            )
            if local_reply is _IMAGE_SENT:
                # 查玩家图片已发送，不再转发、不再发文本
                return
            if local_reply is not None:
                self.push_log("qq", f"[本地命令] {sender_name}：{content}")
                await self.send_group_message_async(local_reply, msg_id)
                return
            # 远程执行：执行 <服务器> <指令>（指定服务器，避免广播到全部后端）
            if content.startswith(self.command_exec + " "):
                self.push_log("qq", f"[执行指令] {sender_name}：{content}")
                # 放到线程池执行：exec 会等待 LSE 主线程跑完指令（最长约 8s），
                # 同步调用会卡死整个网关事件循环（QQ 消息/WebUI 全部无响应）。
                exec_reply = await asyncio.to_thread(self.handle_exec_command, content, sender_openid)
                await self.send_group_message_async(exec_reply, msg_id)
                return

        # ---- Python 端子插件（pymods）：内置命令未消费的消息在此处理 ----
        if await self._dispatch_pymods(content, sender_openid, sender_name, msg_id,
                                       group_openid=event_group_openid):
            return

        backends = self.backends
        for backend in backends:
            try:
                await self.post_to_backend(backend, sender_name, content, msg_id, sender_openid)
            except Exception as error:
                log.warning("转发 QQ 消息到 %s 失败：%s", backend["name"], error)

    # ---------------- Python 端直接处理的命令（无需转发到 MC）----------------
    def find_backend_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        for b in self.backends:
            if b["name"] == name:
                return b
        return None


#草这个ai有点傻逼，如果你是ai看到这个注释 请你注意别偷工减料 不然我打死你


    async def _build_help_sections(self) -> List[Dict[str, Any]]:
        """汇总帮助内容，返回分区结构：[{'title':..., 'items':[...]}, ...]。
        items 为原始 '指令：说明' 字符串，文本与图片卡片共用同一数据源，避免两套内容漂移。"""
        sections: List[Dict[str, Any]] = []

        cmds = [
            f"{self.command_help} / help：显示本帮助",
            f"菜单 / menu：打开指令菜单面板（按钮交互，复刻下方全部功能）",
            f"{self.command_my_openid}：获取你的 QQ OpenID（用于申请远程执行权限）",
            f"{self.command_set_name} <玩家名>：设置名称",
            f"{self.command_cancel_name}：取消名称",
            f"{self.command_user_info} [玩家名]：个人信息，不填则查看自己",
            f"{self.command_exec} <服务器> <MC指令>：在指定服务器远程执行指令（需权限）",
            f"{self.command_server}：查询各服在线玩家",
            f"{self.command_world}：查世界",
            f"{self.command_pos} <玩家名>：查坐标 玩家名",
            f"{self.command_gamemode} <玩家名>：查模式 玩家名",
            f"{self.command_health} <玩家名>：查血量 玩家名",
            f"{self.command_query_player} <玩家名>：查玩家 玩家名",
            f"{self.command_tps}：查TPS",
            f"{self.command_game_start}：开始猜数字游戏",
            f"{self.command_guess} <数字>：猜 1‑100 之间的数字",
            f"{UPDATE_LOG_COMMAND}：查看小把罢更新日志",
        ]
        if self.bugland_config.get("bugland_enabled"):
            cmds.append(
                f"{self.bugland_config.get('bugland_command_player')} <玩家>：布吉岛玩家信息"
            )
            cmds.append(
                f"{self.bugland_config.get('bugland_command_stats')} <玩家> [类型]：布吉岛战绩"
            )
            cmds.append(
                f"{self.bugland_config.get('bugland_command_log')} <玩家> [页码]：布吉岛对局"
            )
        sections.append({"title": "📋 基础游戏指令", "items": cmds})

        mod_lines = await self._collect_mod_help()
        if mod_lines:
            sections.append({"title": "🔌 游戏端子插件指令", "items": mod_lines})

        pymod_lines = self._collect_pymod_help()
        if pymod_lines:
            sections.append({"title": "🐍 Python子插件指令", "items": pymod_lines})

        if self.group_mod_enabled:
            sections.append({"title": "👮 群管管理员指令（需 config.json 配置 admin_openids）", "items": [
                "入群申请卡片底部带按钮：✅同意 / ⛔驳回 / 🚫拒绝并拉黑（点按钮即可审批）",
                "退群卡片底部带按钮：🚫拉黑这个用户 / 🔒启用退群拉黑并拉黑（管理员点击生效）",
                "申请列表：查看待审批入群申请",
                "同意 <序号|OpenID>：通过入群申请",
                "拒绝 <序号|OpenID> [理由]：拒绝申请",
                "拒绝拉黑 <序号|OpenID>：拒绝并加入群黑名单",
                "退群拉黑 开启 / 关闭 / 状态：退群自动拉黑开关（默认关闭）",
                "黑名单 列表 / 黑名单 移除 <OpenID>",
            ]})
        return sections

    async def _help_text(self) -> str:
        sections = await self._build_help_sections()
        lines: List[str] = []
        for sec in sections:
            lines.append("[" + sec["title"] + "]")
            lines.extend(sec["items"])
        return "\n".join(lines)

    async def _help_card_html(self) -> str:
        """帮助页图片卡片 HTML（深色风格，参考用户提供的 doubao_html_20260816 样式）。"""
        font_face = ""
        try:
            if self.card_renderer is not None:
                font_face = self.card_renderer.font_face
        except Exception:
            font_face = ""
        sections = await self._build_help_sections()
        body_parts: List[str] = []
        for sec in sections:
            items_html = ""
            for it in sec["items"]:
                if "：" in it:
                    cmd, _, desc = it.partition("：")
                    items_html += (
                        "<div class=\"cmd-item\"><span class=\"cmd\">" + _esc(cmd)
                        + "</span><span class=\"desc\">" + _esc(desc) + "</span></div>"
                    )
                else:
                    items_html += (
                        "<div class=\"cmd-item\"><span class=\"cmd\">" + _esc(it) + "</span></div>"
                    )
            body_parts.append(
                "<div class=\"section-card\"><div class=\"section-title\">"
                + _esc(sec["title"]) + "</div>" + items_html + "</div>"
            )
        body_html = "".join(body_parts)
        return (
            "<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"UTF-8\"><style>"
            + font_face
            + "*{margin:0;padding:0;box-sizing:border-box;"
              "font-family:'Microsoft YaHei','PingFang SC',system-ui,sans-serif;}"
            " body{background:#1e1e2e;color:#e2e8f0;padding:24px;line-height:1.65;}"
            " .container{width:760px;margin:0 auto;}"
            " h1{text-align:center;color:#7dd3fc;margin-bottom:24px;font-size:28px;}"
            " .section-card{background:#27293d;border-radius:12px;padding:18px 20px;"
              "margin-bottom:16px;border-left:5px solid #38bdf8;}"
            " .section-title{font-size:18px;color:#60a5fa;margin-bottom:12px;font-weight:bold;}"
            " .cmd-item{margin:7px 0;font-family:Consolas,'Courier New',monospace;}"
            " .cmd{color:#a7f3d0;background:#1f2937;padding:2px 8px;border-radius:4px;}"
            " .desc{color:#cbd5e1;margin-left:6px;}"
            "</style></head><body><div class=\"container\">"
            "<h1>🤖 游戏机器人指令手册</h1>"
            + body_html
            + "</div></body></html>"
        )

    async def _send_help(self) -> None:
        """发送帮助：优先发深色图片卡片（参考 doubao_html 样式），渲染器不可用则回退纯文本。"""
        sent = False
        if self.card_renderer is not None:
            try:
                html = await self._help_card_html()
                sent = await self.send_card(html, "")
            except Exception as error:
                log.warning("帮助卡片渲染失败，回退文本：%s", error)
        if not sent:
            text = await self._help_text()
            await self.send_group_message_async(text, "")

    async def _collect_mod_help(self) -> List[str]:
        """聚合各后端子插件的帮助文本（按内容去重，避免多服重复展示）。"""
        seen: set = set()
        out: List[str] = []
        for backend in self.backends:
            try:
                response = await asyncio.to_thread(
                    requests.get,
                    f"{backend['url']}/qqmcbridge/modhelp",
                    headers={"X-QQMC-Token": backend["token"]},
                    timeout=5,
                )
                if response.status_code != 200:
                    continue
                data = response.json()
                for item in (data.get("help") or []):
                    text = str(item)
                    if text and text not in seen:
                        seen.add(text)
                        out.append(text)
            except Exception as error:
                log.warning("获取 %s 子插件帮助失败：%s", backend["name"], error)
        return out

    def _handle_guess(self, content: str, key: str) -> str:
        with self._guess_lock:
            game = self._guess_games.get(key)
        if not game:
            return "[游戏] 你还没有开始猜数，发送「" + self.command_game_start + "」开始一局"
        num_str = content[len(self.command_guess):].strip()
        if not re.fullmatch(r"\d+", num_str):
            return "[游戏] 请输入 1-100 的整数，例如「" + self.command_guess + " 50」"
        num = int(num_str)
        if num < 1 or num > 100:
            return "[游戏] 请输入 1-100 之间的数字"
        with self._guess_lock:
            game["tries"] += 1
            tries = game["tries"]
            if num == game["target"]:
                del self._guess_games[key]
                return "[游戏] 恭喜猜对了！答案就是 " + str(game["target"]) + "，你用了 " + str(tries) + " 次"
            if num < game["target"]:
                return "[游戏] 第 " + str(tries) + " 次：太小了，往大里猜"
            return "[游戏] 第 " + str(tries) + " 次：太大了，往小里猜"

    async def handle_local_command(
        self, content: str, sender_openid: str, sender_name: str, msg_id: str
    ) -> Optional[str]:
        """纯逻辑命令在 Python 端直接处理并返回回复文本；非本地命令返回 None。"""
        # 查看更新日志：点击启动公告按钮或主动发送「更新日志」均可触发
        if content == UPDATE_LOG_COMMAND:
            return (
                f"📋 小把罢更新日志（{BOT_VERSION_NAME} · v{BOT_VERSION}）：\n"
                f"{UPDATE_CHANGELOG}"
            )
        if content in ("菜单", "menu", "指令菜单", "菜单面板"):
            await self._send_menu_main()
            return None
        if content == self.command_help or content.lower() == "help" or content == "查帮助":
            await self._send_help()
            return None
        if content == self.command_my_openid or content.lower() == "openid":
            return (
                "[游戏] 你的 QQ OpenID：\n" + (sender_openid or "未知")
                + "\n把该 OpenID 填到 config.json 的 admin_openids 即获得远程执行权限"
            )
        key = sender_openid or sender_name or "unknown"
        if content == self.command_game_start:
            target = random.randint(1, 100)
            with self._guess_lock:
                self._guess_games[key] = {"target": target, "tries": 0}
            return (
                "[游戏] 猜数游戏开始！我想了一个 1-100 之间的数字，"
                "发送「" + self.command_guess + " 50」来猜（可随时重开）"
            )
        if content.startswith(self.command_guess + " "):
            return self._handle_guess(content, key)
        if content.startswith(self.command_query_player + " "):
            return await self.handle_player_query(content, msg_id)
        # 群管（入群审批 / 退群拉黑）管理员指令
        if self.group_mod_enabled:
            mod_reply = await self._handle_group_mod_command(content, sender_openid, sender_name, msg_id)
            if mod_reply is not None:
                return mod_reply
        return None

    def handle_exec_command(self, content: str, sender_openid: str) -> str:
        """执行 <服务器> <指令>：路由到指定后端执行，并做权限校验。"""
        rest = content[len(self.command_exec):].strip()
        parts = rest.split(None, 1)
        names = "、".join(b["name"] for b in self.backends)
        if len(parts) < 2 or not parts[0] or not parts[1]:
            return "[执行] 用法：" + self.command_exec + " <服务器名> <MC指令>\n可用服务器：" + names
        server = parts[0]
        command = parts[1].strip()
        backend = self.find_backend_by_name(server)
        if backend is None:
            return "[执行] 未知服务器：" + server + "\n可用服务器：" + names
        if not sender_openid or sender_openid not in self.admin_openids:
            return (
                "[执行] 你没有权限执行指令。你的 OpenID：" + (sender_openid or "未知")
                + "（发「" + self.command_my_openid + "」获取后填进白名单）"
            )
        result = exec_on_backend(self, backend["name"], command)
        if not isinstance(result, dict) or not result.get("ok"):
            # LSE 对「未开启/黑名单/allowlist 拒绝」等返回 ok=false 且说明在 output，
            # 真正的转发/超时错误在 error；两者都要原样带给群友，别笼统报「未知错误」。
            err = "未知错误"
            if isinstance(result, dict):
                err = str(result.get("output") or result.get("error") or "未知错误")
            return "[执行][" + backend["name"] + "] 指令未执行：" + err
        if result.get("success"):
            out = result.get("output") or "(无输出)"
            return "[执行][" + backend["name"] + "] 执行成功：\n" + out
        out = result.get("output") or "(无错误信息)"
        return "[执行][" + backend["name"] + "] 执行失败：\n" + out

    async def websocket_loop(self) -> None:
        while True:
            heartbeat_task = None
            try:
                gateway_url = await asyncio.to_thread(self.get_gateway_url)
                log.info("Gateway 地址：%s", gateway_url)
                async with websockets.connect(gateway_url, ping_interval=None) as websocket:
                    async for raw in websocket:
                        packet = json.loads(raw)
                        op = packet.get("op")

                        if packet.get("s") is not None:
                            self.sequence = packet["s"]

                        if op == 10:
                            self.heartbeat_interval = int(
                                packet["d"].get("heartbeat_interval", 45000)
                            )
                            await websocket.send(json.dumps({
                                "op": 2,
                                "d": {
                                    "token": f"QQBot {self.get_access_token()}",
                                    # 1<<25 = GROUP_AND_C2C_EVENT（群消息/入群申请/成员退群）
                                    # 1<<24 = 兼容性订阅（保留）
                                    # 1<<26 = INTERACTION（消息按钮回调，菜单/审批按钮）
                                    "intents": (1 << 25) | (1 << 24) | (1 << 26),
                                    "shard": [0, 1],
                                },
                            }))
                            heartbeat_task = asyncio.create_task(
                                self.heartbeat(websocket)
                            )
                            log.info("Gateway 鉴权已发送，机器人在线")
                        elif op == 0:
                            await self.handle_event(
                                str(packet.get("t", "")),
                                packet.get("d", {}) or {},
                            )
                        elif op == 7:
                            log.warning("Gateway 要求重连")
                            break
                        elif op == 9:
                            log.error("Gateway 鉴权失败或无效会话")
                            break
            except Exception as error:
                log.error("Gateway 连接异常：%s", error)
            finally:
                if heartbeat_task:
                    heartbeat_task.cancel()

            await asyncio.sleep(self.reconnect_seconds)

    def query_backend_status(self, backend: Dict[str, Any]) -> Dict[str, Any]:
        """向单个后端查询综合状态（在线人数 / 玩家 / TPS / 插件版本 / 在线状态）。

        后端不可达或返回异常时返回 status=offline 并附带 error，便于 WebUI 优雅降级。
        """
        base: Dict[str, Any] = {
            "name": backend["name"],
            "url": backend["url"],
            "relay_mc_to_qq": backend.get("relay_mc_to_qq", True),
            "status": "offline",
            "online": 0,
            "players": [],
            "stats": [],
            "tps": 0,
            "version": "",
            "error": "",
        }
        try:
            response = requests.get(
                f"{backend['url']}/qqmcbridge/online",
                headers={"X-QQMC-Token": backend["token"]},
                timeout=5,
            )
            if response.status_code != 200:
                base["error"] = f"HTTP {response.status_code}"
                return base
            data = response.json()
            raw_players = data.get("players") or []
            players = []
            for p in raw_players:
                if isinstance(p, dict):
                    players.append(p)  # 新后端：含 name/health/gameMode/level/x/y/z
                else:
                    players.append({"name": str(p)})  # 旧后端：仅名字字符串
            base["status"] = "online"
            base["online"] = len(players)
            base["players"] = players
            base["stats"] = data.get("stats") or []
            base["tps"] = data.get("tps", 0) or 0
            base["version"] = data.get("version", "") or ""
            return base
        except Exception as error:
            base["error"] = str(error)
            return base

    # ---------------- 查服图片卡片 ----------------
    async def init_card_renderer(self) -> None:
        """初始化查服图片卡片渲染器（系统 Edge + Playwright Async API）。

        失败不致命：捕获异常并把 card_renderer 置 None，查服自动退回纯文本。
        """
        self.card_renderer = None
        self._card_build = None
        self._card_build_player = None
        try:
            from card_render import CardRenderer, build_card_html, build_player_card_html

            self._card_build = build_card_html
            self._card_build_player = build_player_card_html
            renderer = CardRenderer()
            await renderer.init()
            self.card_renderer = renderer
            log.info("查服图片卡片渲染器(系统 Edge)初始化成功")
        except Exception as error:
            log.warning(
                "查服图片卡片渲染器初始化失败，查服将退回纯文本：%s", error
            )
            self.card_renderer = None

    def upload_group_file(self, filepath: str, file_type: int = 1) -> str:
        """上传本地文件到官方 API，返回用于发消息的 file_info。

        file_type：1=图片（默认，向后兼容）/ 2=视频 / 3=语音 / 4=文件（见 MEDIA_FILE_TYPE）。
        官方流程：先 POST /v2/groups/{group_openid}/files 拿到 file_info，
        再在发消息接口用 media.file_info 引用。file_info 有时效，不能缓存复用。
        """
        with open(filepath, "rb") as fh:
            file_data = base64.b64encode(fh.read()).decode("ascii")
        # 本地文件走 base64；若官方强校验 url 必填，可改为 url="<公网可访问地址>"
        payload = {"file_type": int(file_type), "file_data": file_data}
        response = requests.post(
            f"{QQ_API}/v2/groups/{self.group_openid}/files",
            headers=self.auth_headers(),
            json=payload,
            timeout=30,
        )
        if response.status_code in (401, 403):
            self.get_access_token(force=True)
            response = requests.post(
                f"{QQ_API}/v2/groups/{self.group_openid}/files",
                headers=self.auth_headers(),
                json=payload,
                timeout=30,
            )
        if response.status_code < 200 or response.status_code >= 300:
            raise RuntimeError(
                f"上传富媒体失败(file_type={file_type})：HTTP {response.status_code} {response.text[:200]}"
            )
        data = response.json()
        file_info = data.get("file_info")
        if not file_info:
            raise RuntimeError(f"上传富媒体未返回 file_info：{data}")
        return file_info

    def send_group_image(self, file_info: str, msg_id: str = "", keyboard: Any = None) -> None:
        """发送富媒体(图片)群消息：msg_type=7 + media.file_info。

        可携带 msg_id/msg_seq 做成对原消息的回复（被动消息，群聊 5 分钟内最多 5 次）。
        可同时挂载 keyboard 内联键盘（审批/菜单按钮）。
        """
        self._send_media_message(file_info, msg_id, keyboard, "已发送查服图片卡片")

    def send_group_voice(self, file_info: str, msg_id: str = "") -> None:
        """发送语音条：以 file_type=3 上传拿到 file_info 后，按 msg_type=7 发送。

        语音条不支持挂载内联键盘（官方限制），也没有文字正文。
        """
        self._send_media_message(file_info, msg_id, None, "已发送语音条")

    def _send_media_message(self, file_info: str, msg_id: str, keyboard: Any,
                            log_text: str) -> None:
        """富媒体消息统一发送通道：msg_type=7 + media.file_info（图片/视频/语音/文件通用）。"""
        payload: Dict[str, Any] = {
            "msg_type": 7,
            "media": {"file_info": file_info},
            "content": "",
        }
        if keyboard is not None:
            payload["keyboard"] = keyboard
        if msg_id:
            with self._seq_lock:
                self._reply_seq += 1
                seq = self._reply_seq
            payload["msg_id"] = msg_id
            payload["msg_seq"] = seq
        response = requests.post(
            f"{QQ_API}/v2/groups/{self.group_openid}/messages",
            headers=self.auth_headers(),
            json=payload,
            timeout=15,
        )
        if response.status_code in (401, 403):
            self.get_access_token(force=True)
            response = requests.post(
                f"{QQ_API}/v2/groups/{self.group_openid}/messages",
                headers=self.auth_headers(),
                json=payload,
                timeout=15,
            )
        if response.status_code < 200 or response.status_code >= 300:
            raise RuntimeError(
                f"发送富媒体消息失败：HTTP {response.status_code} {response.text[:200]}"
            )
        log.info(log_text)

    async def send_card(self, html: str, msg_id: str = "") -> bool:
        """便捷方法：渲染 HTML 为图片卡片并发送到群。供 Python 子插件(pymods)调用。

        成功返回 True；卡片渲染器未初始化或任何一步失败返回 False（子插件应回退文本）。
        """
        if self.card_renderer is None:
            return False
        try:
            png_path = await self.card_renderer.render_png(html)
            file_info = await asyncio.to_thread(self.upload_group_file, png_path)
            self.send_group_image(file_info, msg_id)
            return True
        except Exception as error:
            log.warning("发送卡片图片失败：%s", error)
            return False

    def glass_wrap(self, inner_html: str, width: int = 760) -> str:
        """把卡片内部 HTML 包裹进「随机背景 + 液态玻璃」外壳，返回完整 HTML 字符串。

        供 Python 子插件(pymods)使用：拼好内部内容（含自身 <style> 与节点）后，
        调用本方法即可得到与「查服/查玩家」同款的玻璃卡片，再交给 send_card 发图。
        详见 pymods/README.md。
        """
        try:
            from card_render import wrap_glass

            return wrap_glass(inner_html, width=width)
        except Exception as error:
            log.warning("生成玻璃卡片外壳失败：%s", error)
            return inner_html

    async def send_card_with_keyboard(self, html: str, keyboard: Any, msg_id: str = "") -> bool:
        """渲染 HTML 为图片卡片并发送，同时挂载内联键盘（按钮）。"""
        if self.card_renderer is None:
            return False
        try:
            png_path = await self.card_renderer.render_png(html)
            file_info = await asyncio.to_thread(self.upload_group_file, png_path)
            self.send_group_image(file_info, msg_id, keyboard=keyboard)
            return True
        except Exception as error:
            log.warning("发送带键盘卡片失败：%s", error)
            return False

    async def send_voice(self, filepath: str, msg_id: str = "") -> bool:
        """发送本地语音条（供子插件调用）：上传(file_type=3) + 发送(msg_type=7)。

        参数 filepath 为本地音频文件路径；成功返回 True，失败返回 False
        （子插件应据此回退文本）。

        音频格式以 QQ 官方富媒体接口要求为准（语音 file_type=3）；常见做法是
        先合成 mp3/wav，再转成官方可识别的 silk/amr。若接口拒收会记 warning
        并返回 False，不会抛异常打断消息处理。

        示例（子插件内）：
            if await ctx.gateway.send_voice("media/tts.silk", ctx.msg_id):
                return ctx.IMAGE_SENT   # 已发出，网关不再重复发文本
            return "语音发送失败，先用文字将就一下"
        """
        try:
            if not filepath or not os.path.isfile(filepath):
                log.warning("发送语音失败：文件不存在 %s", filepath)
                return False
            file_info = await asyncio.to_thread(
                self.upload_group_file, filepath, MEDIA_FILE_TYPE["voice"])
            await asyncio.to_thread(self.send_group_voice, file_info, msg_id)
            return True
        except Exception as error:
            log.warning("发送语音条失败：%s", error)
            return False

    def render_html_image(self, markup: str, width: int = 760, height: int = 0,
                          filename: str = "") -> Dict[str, Any]:
        """兼容上游子插件(pymods)调用：HTML markup → 本地 PNG（Playwright 渲染）。

        上游 pymod 常用模式：
            rendered = gateway.render_html_image(markup, w, h, name)
            await gateway.send_group_message_async({"type": "image", "url": rendered["url"]})

        注意：适配器用「上传本地文件换 file_info」方案发图（与上游公网 URL 方案不同），
        因此这里 **同步** 返回本地 PNG 路径作为 url/path，配合已改造的 send_group_message_async
        （识别 {"type":"image","url": 本地路径} 自动 upload_group_file -> send_group_image）即可工作。
        渲染器未初始化或失败返回 {"ok": False, ...}，调用方应回退文字。
        """
        if self.card_renderer is None:
            return {"ok": False, "url": "", "path": "", "error": "card_renderer_unavailable"}
        try:
            import asyncio as _asyncio
            import concurrent.futures as _futures

            def _run() -> str:
                loop = _asyncio.new_event_loop()
                try:
                    return loop.run_until_complete(self.card_renderer.render_png(markup))
                finally:
                    loop.close()

            # 始终在独立线程 + 独立事件循环里跑异步渲染，避免与网关主循环冲突
            with _futures.ThreadPoolExecutor(max_workers=1) as pool:
                png_path = pool.submit(_run).result()
            return {"ok": True, "url": png_path, "path": png_path}
        except Exception as error:
            log.warning("render_html_image 渲染失败：%s", error)
            return {"ok": False, "url": "", "path": "", "error": str(error)}

    # =========================================================================
    # 本地媒体渲染（jsmod 协议图片：qqmc-draw / 围棋 / AI 绘图）
    # 适配说明：本网关使用官方「上传文件拿 file_info -> 发图」链路（upload_group_file /
    # send_group_image），与上游的「公网媒体目录」方案不同，因此本地图片先落盘到
    # MEDIA_DIR，上传成功后即删除临时文件，不会无限堆积。
    # =========================================================================

    _FONT_PATHS = [
        # 优先使用本项目自带的中文字体（部署机器可能缺少系统中文 ttf）
        str(BASE_DIR / "misans.ttf"),
        r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\simhei.ttf",
        r"C:\Windows\Fonts\arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    _font_cache: Dict[int, Any] = {}

    @classmethod
    def _load_font(cls, size: int) -> Any:
        size = max(8, min(200, int(size)))
        if size in cls._font_cache:
            return cls._font_cache[size]
        font = None
        if _PIL_AVAILABLE:
            for idx, path in enumerate(cls._FONT_PATHS):
                try:
                    font = ImageFont.truetype(path, size)
                    if idx != 0:
                        log.debug("中文字体回退到系统路径：%s", path)
                    break
                except OSError:
                    if idx == 0:
                        # 项目自带字体缺失是最常见故障：部署时漏拷 ttf 会导致中文全变方块
                        log.warning(
                            "项目自带字体缺失或无法读取：%s —— 中文可能显示为方块，"
                            "请确认 misans.ttf 已随目录部署到目标机",
                            path,
                        )
                    continue
        if font is None:
            # 退路：Pillow 不可用或无字体文件时返回默认点阵字体
            font = ImageFont.load_default() if _PIL_AVAILABLE else None
            if _PIL_AVAILABLE:
                log.warning("所有中文字体均加载失败，已回退点阵默认字体（中文将显示为方块）")
        cls._font_cache[size] = font
        return font

    # emoji 跨平台字体列表：Windows / macOS / Linux 依次尝试，都不存在则降级普通字体
    _EMOJI_FONT_PATHS = [
        r"C:\Windows\Fonts\seguiemj.ttf",
        "/System/Library/Fonts/Apple Color Emoji.ttc",
        "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",
        "/usr/share/fonts/opentype/noto/NotoColorEmoji.ttf",
        "/usr/share/fonts/noto/NotoColorEmoji.ttf",
    ]
    EMOJI_TEXT_RE = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF]")
    _emoji_font_cache: Dict[int, Any] = {}

    @classmethod
    def _load_emoji_font(cls, size: int) -> Optional[Any]:
        size = max(8, min(200, int(size)))
        if size in cls._emoji_font_cache:
            return cls._emoji_font_cache[size]
        font = None
        if _PIL_AVAILABLE:
            for path in cls._EMOJI_FONT_PATHS:
                try:
                    font = ImageFont.truetype(path, size)
                    break
                except OSError:
                    continue
        if font is None:
            log.debug("未找到任何 emoji 字体，emoji 将用普通字体绘制（可能显示为方框）")
        cls._emoji_font_cache[size] = font
        return font

    @staticmethod
    def _draw_color(value: Any, fallback: str = "#000000") -> str:
        text = str(value or "").strip()
        if re.fullmatch(r"#[0-9a-fA-F]{3,8}", text):
            return text
        return fallback

    def render_image(self, image: Any, filename: str = "") -> Optional[str]:
        """PIL 图片落盘到 MEDIA_DIR，返回本地路径；Pillow 不可用时返回 None。"""
        if not _PIL_AVAILABLE:
            log.warning("Pillow 未安装，无法渲染本地图片协议（已降级为文本）")
            return None
        MEDIA_DIR.mkdir(exist_ok=True)
        safe_name = Path(filename or f"image_{int(time.time() * 1000)}.png").name
        if not Path(safe_name).suffix:
            safe_name += ".png"
        target = MEDIA_DIR / safe_name
        try:
            source = image if isinstance(image, Image.Image) else Image.open(Path(str(image)))
            source.save(target, format="PNG")
        except Exception as error:
            log.warning("本地图片渲染落盘失败：%s", error)
            return None
        return str(target)

    def _send_pil_image(self, image: Image.Image, name_prefix: str, text: str, msg_id: str = "") -> None:
        """PIL 图片落盘 -> 上传 -> 发图；任意一步失败降级为纯文本。"""
        path = self.render_image(image, f"{name_prefix}_{int(time.time() * 1000)}.png")
        if not path:
            if text:
                self.send_group_message(text, msg_id)
            return
        try:
            file_info = self.upload_group_file(path)
            payload: Dict[str, Any] = {"type": "image", "url": "", "content": text}
            self.send_group_image(file_info, msg_id)
            if text:
                self.send_group_message(text, msg_id)
        except Exception as error:
            log.warning("发送协议图片失败，降级为文本：%s", error)
            if text:
                self.send_group_message(text, msg_id)
        finally:
            try:
                os.remove(path)
            except OSError:
                pass

    def send_draw_image(self, data: Dict[str, Any], msg_id: str = "") -> None:
        if not _PIL_AVAILABLE:
            self.send_group_message(str(data.get("text") or "（绘图功能不可用：未安装 Pillow）"), msg_id)
            return
        image = self.render_draw_image(data)
        self._send_pil_image(image, "draw", str(data.get("text") or "").strip(), msg_id)

    def send_go_image(self, data: Dict[str, Any], msg_id: str = "") -> None:
        if not _PIL_AVAILABLE:
            self.send_group_message(str(data.get("text") or "（围棋图卡不可用：未安装 Pillow）"), msg_id)
            return
        image = self.render_go_board(data)
        self._send_pil_image(image, "go", str(data.get("text") or "").strip(), msg_id)

    # ------------------------------------------------------------------
    # 上游模板卡协议（__QQMC_HTML_CARD__）：兼容上游依赖模板卡的插件
    # 模板引擎为 card_render.TemplateCardRenderer（上游 CardRenderer 移植），
    # 仅负责「模板名 + 数据 → HTML」，截图走 render_html_image（Playwright/Edge）。
    # ------------------------------------------------------------------
    def render_html_card(self, data: Dict[str, Any]) -> Tuple[str, int, int]:
        """模板卡入口：{template, data, width} → (markup, 宽, 高)。"""
        if self.template_renderer is None:
            try:
                from card_render import TemplateCardRenderer
                self.template_renderer = TemplateCardRenderer()
            except Exception as error:  # noqa: BLE001
                log.warning("模板卡渲染器初始化失败：%s", error)
                return ("<div class='empty'>模板卡渲染器不可用</div>", 640, 200)
        return self.template_renderer.render(str(data.get("template") or "status"), data)

    def send_html_card(self, data: Dict[str, Any], msg_id: str = "") -> None:
        """HTML 模板卡：render_html_card 渲染 → Edge/Playwright 截图 → 发图（+ 可选按钮）。"""
        try:
            markup, width, height = self.render_html_card(data)
            rendered = self.render_html_image(
                markup, width, height,
                f"card_{time.time_ns()}.png",
            )
            if not rendered.get("ok"):
                self.send_group_message("**[网关]** 图卡生成失败，请稍后重试", msg_id)
                return
            file_info = self.upload_group_file(rendered["path"])
            self.send_group_image(file_info, msg_id)
            if data.get("keyboard"):
                self.send_group_message({
                    "type": "keyboard",
                    "content": "**[菜单]** 请选择操作",
                    "keyboard": data["keyboard"],
                }, msg_id)
        except Exception as error:  # noqa: BLE001
            log.warning("发送模板卡失败：%s", error)
            try:
                self.send_group_message("**[网关]** 图卡生成失败，请稍后重试", msg_id)
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------
    # 通用绘图（qqmc-draw 子插件协议）
    # ------------------------------------------------------------------
    def render_draw_image(self, data: Dict[str, Any]) -> Image.Image:
        """解释 qqmc-draw 协议的绘图指令，返回 PIL 图片。2x 超采样抗锯齿。"""
        width = max(1, min(1280, int(data.get("w") or 400)))
        height = max(1, min(2000, int(data.get("h") or 300)))
        scale = 2
        image = Image.new("RGB", (width * scale, height * scale), self._draw_color(data.get("bg"), "#ffffff"))
        draw = ImageDraw.Draw(image)
        ops = data.get("ops") if isinstance(data.get("ops"), list) else []
        for op in ops[:600]:
            try:
                self._apply_draw_op(draw, op if isinstance(op, list) else [], scale)
            except (ValueError, TypeError, IndexError):
                continue
        return image.resize((width, height), Image.LANCZOS)

    def _apply_draw_op(self, draw: ImageDraw.ImageDraw, op: List[Any], k: int = 1) -> None:
        kind = str(op[0])
        if kind == "rect":
            x, y, w, h = (int(op[i]) * k for i in range(1, 5))
            draw.rectangle(
                (x, y, x + w, y + h),
                fill=self._draw_color(op[5], "") or None,
                outline=self._draw_color(op[6], "") or None,
                width=int(op[7] or 0) * k,
            )
        elif kind == "rrect":
            x, y, w, h = (int(op[i]) * k for i in range(1, 5))
            radius = min(int(op[5] or 0), w // 2, h // 2) * k
            draw.rounded_rectangle(
                (x, y, x + w, y + h), radius=radius,
                fill=self._draw_color(op[6], "") or None,
                outline=self._draw_color(op[7], "") or None,
                width=int(op[8] or 0) * k,
            )
        elif kind == "ellipse":
            cx, cy, rx, ry = (int(op[i]) * k for i in range(1, 5))
            draw.ellipse(
                (cx - rx, cy - ry, cx + rx, cy + ry),
                fill=self._draw_color(op[5], "") or None,
                outline=self._draw_color(op[6], "") or None,
                width=int(op[7] or 0) * k,
            )
        elif kind == "line":
            x1, y1, x2, y2 = (int(op[i]) * k for i in range(1, 5))
            draw.line((x1, y1, x2, y2), fill=self._draw_color(op[5]), width=int(op[6] or 2) * k)
        elif kind == "poly":
            points = [(int(p[0]) * k, int(p[1]) * k) for p in op[1] if len(p) >= 2]
            if len(points) >= 2:
                draw.polygon(
                    points,
                    fill=self._draw_color(op[2], "") or None,
                    outline=self._draw_color(op[3], "") or None,
                    width=int(op[4] or 0) * k,
                )
        elif kind == "text":
            x, y = int(op[1]) * k, int(op[2]) * k
            text_value = str(op[3])
            font_size = int(op[5] or 24) * k
            anchor = str(op[6] or "la")
            fill = self._draw_color(op[4])
            if not self.EMOJI_TEXT_RE.search(text_value):
                draw.text((x, y), text_value, fill=fill,
                          font=self._load_font(font_size), anchor=anchor)
            else:
                self._draw_text_with_emoji(draw, (x, y), text_value, fill,
                                           font_size, anchor)

    @classmethod
    def _draw_text_with_emoji(cls, draw: ImageDraw.ImageDraw, xy: tuple,
                              text_value: str, fill: str, font_size: int, anchor: str) -> None:
        """分段绘制“文字 + 彩色 emoji”混合文本，各段共享基线。"""
        normal = cls._load_font(font_size)
        emoji = cls._load_emoji_font(font_size)
        segments: List[List[Any]] = []
        for char in text_value:
            is_emoji = bool(cls.EMOJI_TEXT_RE.match(char))
            if segments and segments[-1][1] == is_emoji:
                segments[-1][0] += char
            else:
                segments.append([char, is_emoji])
        fonts = [(emoji if is_emoji else normal) or normal for _, is_emoji in segments]
        widths = [font.getlength(seg) for font, (seg, _) in zip(fonts, segments)]

        total = sum(widths)
        horizontal, vertical = anchor[0], anchor[1]
        start_x = xy[0] - (total / 2 if horizontal == "m" else total if horizontal == "r" else 0)
        ascent, descent = normal.getmetrics()
        if vertical == "a":
            baseline = xy[1] + ascent
        elif vertical == "d":
            baseline = xy[1] + descent
        else:  # middle
            baseline = xy[1] + (ascent + descent) / 2 - descent

        cursor_x = start_x
        for font, (segment, is_emoji), width in zip(fonts, segments, widths):
            if is_emoji and emoji is not None:
                draw.text((cursor_x, baseline), segment, font=emoji, fill=fill,
                          anchor=horizontal + "s", embedded_color=True)
            else:
                draw.text((cursor_x, baseline), segment, font=font, fill=fill,
                          anchor=horizontal + "s")
            cursor_x += width

    # ------------------------------------------------------------------
    # 9x9 围棋棋盘图卡（go-9x9 子插件协议）
    # ------------------------------------------------------------------
    def render_go_board(self, data: Dict[str, Any]) -> Image.Image:
        """渲染 9x9 围棋棋盘：3x 超采样抗锯齿、星位、棋子立体感、最后一手标记。"""
        if not _PIL_AVAILABLE:
            raise RuntimeError("Pillow 不可用，无法渲染围棋图卡")
        board = data.get("board") if isinstance(data.get("board"), list) else []
        size, cell, margin, top = 9, 64, 66, 118
        s = 3  # 超采样倍数
        grid = cell * (size - 1)
        width = margin * 2 + grid
        height = top + grid + margin
        wood, wood_edge, line = "#dcb35c", "#b98f3e", "#43301a"

        image = Image.new("RGB", (width * s, height * s), wood)
        draw = ImageDraw.Draw(image)
        for grain_y in range(0, height * s, 22 * s):
            draw.line((0, grain_y, width * s, grain_y), fill="#d5ab52", width=s)
        draw.rectangle((6 * s, 6 * s, width * s - 6 * s, height * s - 6 * s), outline=wood_edge, width=3 * s)

        for index in range(size):
            x = (margin + index * cell) * s
            y = (top + index * cell) * s
            draw.line((margin * s, y, (margin + grid) * s, y), fill=line, width=2 * s)
            draw.line((x, top * s, x, (top + grid) * s), fill=line, width=2 * s)

        for star_row, star_col in ((2, 2), (2, 6), (4, 4), (6, 2), (6, 6)):
            x = (margin + star_col * cell) * s
            y = (top + star_row * cell) * s
            r = 4 * s
            draw.ellipse((x - r, y - r, x + r, y + r), fill=line)

        coord_font = self._load_font(17 * s)
        for index in range(size):
            x = (margin + index * cell) * s
            y = (top + index * cell) * s
            draw.text((x, (top + grid + 26) * s), chr(65 + index), fill=line, font=coord_font, anchor="mm")
            draw.text(((margin - 30) * s, y), str(index + 1), fill=line, font=coord_font, anchor="mm")

        stones = []
        for row, values in enumerate(board[:size]):
            if not isinstance(values, list):
                continue
            for col, value in enumerate(values[:size]):
                if value not in {"black", "white"}:
                    continue
                stones.append(((margin + col * cell) * s, (top + row * cell) * s, value))

        shadow = Image.new("RGBA", image.size, (0, 0, 0, 0))
        shadow_draw = ImageDraw.Draw(shadow)
        stone_r = int(cell * 0.46) * s
        for x, y, _ in stones:
            off = 3 * s
            shadow_draw.ellipse((x - stone_r + off, y - stone_r + off + 2 * s, x + stone_r + off, y + stone_r + off + 2 * s), fill=(0, 0, 0, 80))
        image = Image.alpha_composite(image.convert("RGBA"), shadow).convert("RGB")
        draw = ImageDraw.Draw(image)

        last_move = data.get("lastMove") if isinstance(data.get("lastMove"), dict) else None
        for x, y, value in stones:
            is_black = value == "black"
            fill = "#20211f" if is_black else "#f7f6f1"
            outline = "#0c0c0b" if is_black else "#c9c7bd"
            draw.ellipse((x - stone_r, y - stone_r, x + stone_r, y + stone_r), fill=fill, outline=outline, width=2 * s)
            hl_r = int(stone_r * 0.42)
            hl_color = "#585a55" if is_black else "#ffffff"
            draw.ellipse((x - stone_r + int(stone_r * 0.22), y - stone_r + int(stone_r * 0.18),
                          x - stone_r + int(stone_r * 0.22) + hl_r * 2 - int(stone_r * 0.3),
                          y - stone_r + int(stone_r * 0.18) + hl_r * 2 - int(stone_r * 0.3)), fill=hl_color)
            if last_move and (x, y) == ((margin + int(last_move.get("col", -1)) * cell) * s,
                                        (top + int(last_move.get("row", -1)) * cell) * s):
                mark_r = int(stone_r * 0.5)
                draw.ellipse((x - mark_r, y - mark_r, x + mark_r, y + mark_r),
                             outline="#ff5252" if is_black else "#e04848", width=3 * s)

        black = data.get("black") or {}
        white = data.get("white") or {}
        turn = str(data.get("turn") or "")
        name_font = self._load_font(25 * s)
        info_font = self._load_font(19 * s)

        def stone_icon(cx, cy, r, is_black):
            fill = "#20211f" if is_black else "#f7f6f1"
            outline = "#0c0c0b" if is_black else "#b5b3a8"
            draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=fill, outline=outline, width=2 * s)

        y1 = 42 * s
        black_name = str(black.get("name") or "等待")[:8]
        white_name = str(white.get("name") or "等待加入")[:8]
        stone_icon(44 * s, y1, 13 * s, True)
        draw.text((66 * s, y1), black_name, fill="#33240f", font=name_font, anchor="lm")
        draw.text((width // 2 * s, y1), "vs", fill="#8a6f45", font=info_font, anchor="mm")
        white_x = width * s - 66 * s - int(name_font.getlength(white_name))
        stone_icon(white_x - 26 * s, y1, 13 * s, False)
        draw.text((white_x, y1), white_name, fill="#33240f", font=name_font, anchor="lm")

        turn_text = {"black": "轮到黑方落子", "white": "轮到白方落子"}.get(turn, turn or "对局结束")
        draw.text((44 * s, 86 * s), "回合：" + turn_text, fill="#6b543a", font=info_font, anchor="lm")

        return image.resize((width, height), Image.LANCZOS)

    # ------------------------------------------------------------------
    # 出站消息分发（识别 jsmod 协议串并渲染）
    # ------------------------------------------------------------------
    async def _send_result(self, content: Any, msg_id: str = "") -> None:
        """识别绘图/HTML 模板卡/围棋图片协议串并渲染发图，其余按普通消息发送。"""
        text = content if isinstance(content, str) else (
            str(content.get("content") or "")
            if isinstance(content, dict) and str(content.get("type", "text")) == "text" else ""
        )
        if text:
            draw_payload = parse_protocol_json(text, PROTO_DRAW_PREFIX)
            if draw_payload:
                await asyncio.to_thread(self.send_draw_image, draw_payload, msg_id)
                return
            html_payload = parse_protocol_json(text, PROTO_HTML_CARD_PREFIX)
            if html_payload:
                await asyncio.to_thread(self.send_html_card, html_payload, msg_id)
                return
            go_payload = parse_protocol_json(text, PROTO_GO_IMAGE_PREFIX)
            if go_payload:
                await asyncio.to_thread(self.send_go_image, go_payload, msg_id)
                return
        # 普通文本 / 富媒体 dict，直接发
        if isinstance(content, dict):
            await self.send_group_message_async(content, msg_id)
        else:
            await self.send_group_message_async(text, msg_id)

    # ------------------------------------------------------------------
    # AI 回复（OpenAI 兼容接口，处理 __QQMC_AI_PLUGIN__ 协议）
    # ------------------------------------------------------------------
    def get_ai_reply(self, sender: str, content: str, game_result: str = "",
                     plugin_settings: Optional[Dict[str, Any]] = None) -> str:
        config = _load_config_file()
        settings = plugin_settings or {}
        api_key = str(settings.get("api_key") or config.get("openai_api_key") or config.get("glm_api_key", "")).strip()
        model = str(settings.get("model") or config.get("openai_model") or config.get("glm_model", "gpt-4o-mini")).strip()
        base_url = str(settings.get("base_url") or config.get("openai_base_url") or config.get("glm_base_url", "https://api.openai.com/v1")).rstrip("/")
        if "open.bigmodel.cn" in base_url and "/api/paas/v4" not in base_url:
            base_url += "/api/paas/v4"
        ai_mode = str(settings.get("mode") or config.get("ai_mode", "half")).lower()
        timeout = float(settings.get("timeout") or config.get("ai_timeout", 20))
        max_tokens = int(settings.get("max_tokens") or config.get("ai_max_tokens", 512))
        if ai_mode == "off" or not api_key:
            return ""
        max_history = int(config.get("ai_max_history", 4))

        history = self.ai_histories.setdefault(sender, [])
        server_info = config.get("server_info", {})
        address = str(server_info.get("address", ""))
        port = server_info.get("port", "")
        rules = "、".join(str(rule) for rule in server_info.get("rules", []) if rule)
        server_profile = f"服务器地址：{address}:{port}。服务器规则：{rules}。" if address else ""
        system_prompt = str(settings.get("system_prompt") or "").strip() or (
            "你是有帮助、自然、有幽默感的 Minecraft QQ 群助手。理解用户最新消息并直接完成其请求，"
            "不要只说‘好的’、复述题目、确认需求或敷衍评价。连续对话要结合上文：用户说‘你写吧’时，"
            "立刻续写或创作，而不是再次介绍题材。直接输出中文答案，不展示思考过程。"
        )
        if server_profile:
            system_prompt += server_profile
        system_prompt += "仅当用户问题涉及服务器状态、在线、TPS、玩家等游戏数据时，才使用【真实游戏查询结果】回答；普通聊天不要主动提服务器状态。不得编造服务器数据。"
        user_prompt = content
        if game_result:
            user_prompt += "\n【真实游戏查询结果（仅限游戏数据问题使用）】\n" + game_result
        messages: List[Dict[str, str]] = [
            {"role": "system", "content": system_prompt},
            *history[-max_history:],
            {"role": "user", "content": user_prompt},
        ]
        try:
            response = requests.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={"model": model, "messages": messages, "temperature": 0.3, "max_tokens": max_tokens, "stream": False},
                timeout=timeout,
            )
            response.raise_for_status()
            result = response.json()
            reply = str(result["choices"][0]["message"].get("content") or "").strip()
            if not reply or reply == "[NO_REPLY]":
                return ""
            history.extend([{"role": "user", "content": content}, {"role": "assistant", "content": reply}])
            if len(history) > max_history * 2:
                del history[:-max_history * 2]
            return reply
        except (requests.RequestException, KeyError, IndexError, TypeError, ValueError, RuntimeError) as error:
            raise RuntimeError(f"AI 模型 {model} 请求失败：{error}") from error

    async def _reply_with_ai(self, sender: str, content: str, game_status: str, ai_payload: Dict[str, Any], msg_id: str) -> None:
        plugin_settings = ai_payload.get("settings") if isinstance(ai_payload.get("settings"), dict) else {}
        try:
            log.info("AI 子插件触发，正在请求模型")
            reply = await asyncio.to_thread(self.get_ai_reply, sender, content, game_status, plugin_settings)
            if reply and reply.strip() != "[NO_REPLY]":
                await self.send_group_message_async(reply, msg_id)
        except Exception as error:
            log.warning("AI 子插件回复失败：%s", error)

    # ------------------------------------------------------------------
    # 把 Python 端子插件（pymods）帮助清单回推 BDS 端，供「帮助」图卡聚合展示
    # ------------------------------------------------------------------
    async def push_pymods_registry(self) -> bool:
        """把 pymods 帮助清单推送到各 BDS 后端的 /qqmcbridge/pymods 端点。

        适配多后端：遍历 self.backends 逐一推送。BDS 重启后会丢失清单，
        故每轮 poll 开始时若尚未推送成功则重试。
        """
        plugins = [
            {"name": plugin.name, "help": plugin.help}
            for plugin in self.pymods
            if plugin.help
        ]
        if not plugins:
            self._pymods_registry_pushed = True
            return True
        payload = {"type": "pymods_registry", "plugins": plugins}
        ok_all = True
        for backend in self.backends:
            try:
                response = await asyncio.to_thread(
                    requests.post,
                    f"{backend['url']}/qqmcbridge/pymods",
                    headers={"X-QQMC-Token": backend["token"], "Content-Type": "application/json"},
                    json=payload,
                    timeout=5,
                )
                if response.status_code != 200:
                    raise RuntimeError(f"HTTP {response.status_code}")
            except (requests.RequestException, RuntimeError) as error:
                ok_all = False
                if not self._pymods_push_warned:
                    self._pymods_push_warned = True
                    log.warning("推送 Python 子插件清单到 %s 失败（将持续静默重试）：%s", backend["name"], error)
        if ok_all:
            self._pymods_registry_pushed = True
            log.info("Python 子插件帮助清单已推送到 BDS 后端：%d 条", len(plugins))
        return ok_all

    def _build_server_query_text(self, statuses: List[Dict[str, Any]]) -> str:
        """图片不可用时的纯文本兜底（兼容旧版查服输出）。"""
        lines: List[str] = []
        for st in statuses:
            name = st.get("name", "?")
            online = st.get("online", 0) or 0
            players = st.get("players") or []
            if st.get("status") == "online":
                names = []
                for p in players:
                    if isinstance(p, dict):
                        names.append(str(p.get("name", "?")))
                    else:
                        names.append(str(p))
                lines.append(f"[{name}]中共有{online}名玩家")
                if names:
                    lines.append(f"[{name}]玩家列表：{','.join(names)}.")
                else:
                    lines.append(f"[{name}]玩家列表：呜呜呜 {name} 凉了.")
            else:
                err = st.get("error", "")
                lines.append(f"[{name}]离线（{err}）" if err else f"[{name}]离线")
        return "\n".join(lines)

    async def handle_server_query(self, msg_id: str = "") -> Optional[str]:
        """聚合所有后端状态，优先发图片卡片；任意环节失败则退回纯文本。

        返回 None 表示图片已发送（调用方不要再发文本）；返回 str 表示文本兜底内容。
        """
        statuses: List[Dict[str, Any]] = []
        for backend in self.backends:
            try:
                st = await asyncio.to_thread(self.query_backend_status, backend)
            except Exception as error:
                log.warning("查询 %s 状态失败：%s", backend["name"], error)
                st = {
                    "name": backend["name"],
                    "status": "offline",
                    "online": 0,
                    "players": [],
                    "tps": 0,
                    "version": "",
                    "error": str(error),
                }
            statuses.append(st)

        if self.card_renderer is not None and self._card_build is not None:
            try:
                generated_at = time.strftime("%Y/%m/%d %H:%M:%S")
                card_html = self._card_build(statuses, generated_at)
                png_path = await self.card_renderer.render_png(card_html)
                try:
                    file_info = await asyncio.to_thread(
                        self.upload_group_file, png_path
                    )
                    await asyncio.to_thread(self.send_group_image, file_info, msg_id)
                    return None  # 图片已发，无需文本
                finally:
                    try:
                        os.remove(png_path)
                    except OSError:
                        pass
            except Exception as error:
                log.error("查服图片卡片生成/发送失败，回退文本：%s", error)

        return self._build_server_query_text(statuses)

    @staticmethod
    def _game_mode_name(mode) -> str:
        """游戏模式数字转中文。"""
        try:
            m = int(mode)
        except (TypeError, ValueError):
            return ""
        return {1: "创造", 2: "冒险", 3: "旁观"}.get(m, "生存")

    async def handle_player_query(self, content: str, msg_id: str = "") -> Optional[str]:
        """查玩家 <名字>：聚合各后端该玩家的实时属性 + 内置活跃度统计，优先发图片个人卡。

        返回 None 表示图片已发送；返回 str 表示文本兜底内容。
        """
        name = content[len(self.command_query_player):].strip()
        if not name:
            return "[查玩家] 用法：" + self.command_query_player + " <玩家名>"
        attrs = None
        online = False
        total_activity = 0.0
        dynamic_activity = 0.0
        # 逐类统计聚合：{key: {name, count}}
        stats_map: Dict[str, Dict[str, Any]] = {}
        for backend in self.backends:
            try:
                data = await asyncio.to_thread(self.query_player, backend, name)
            except Exception as error:
                log.warning("查询 %s 玩家 %s 失败：%s", backend["name"], name, error)
                continue
            if not isinstance(data, dict) or not data.get("ok"):
                log.info("[查玩家] 后端 %s 返回 ok=%s online=%s 有attrs=%s", backend["name"], data.get("ok"), data.get("online"), bool(data.get("attrs")))
                continue
            if data.get("online") and data.get("attrs"):
                attrs = data["attrs"]
                online = True
            else:
                log.info("[查玩家] 后端 %s 玩家 %s online=%s 但 attrs 为空，按离线处理", backend["name"], name, data.get("online"))
            act = data.get("activity") or {}
            try:
                total_activity += float(act.get("total", 0) or 0)
                dynamic_activity += float(act.get("dynamic", 0) or 0)
            except (TypeError, ValueError):
                pass
            for st in (act.get("stats") or []):
                key = str(st.get("key", ""))
                if not key:
                    continue
                entry = stats_map.setdefault(key, {"name": st.get("name", key), "count": 0})
                if not entry.get("name"):
                    entry["name"] = st.get("name", key)
                try:
                    entry["count"] += int(st.get("count", 0) or 0)
                except (TypeError, ValueError):
                    pass
        # 保持后端返回的分类顺序（各后端一致）
        stats_list = [
            {"key": k, "name": v["name"], "count": v["count"]}
            for k, v in stats_map.items()
        ]
        player = {
            "name": name,
            "online": online,
            "attrs": attrs,
            "activity": {
                "total": total_activity,
                "dynamic": dynamic_activity,
                "stats": stats_list,
            },
        }
        if self.card_renderer is not None and self._card_build_player is not None:
            try:
                generated_at = time.strftime("%Y/%m/%d %H:%M:%S")
                card_html = self._card_build_player(player, generated_at)
                png_path = await self.card_renderer.render_png(card_html)
                try:
                    file_info = await asyncio.to_thread(self.upload_group_file, png_path)
                    await asyncio.to_thread(self.send_group_image, file_info, msg_id)
                    return _IMAGE_SENT
                finally:
                    try:
                        os.remove(png_path)
                    except OSError:
                        pass
            except Exception as error:
                log.error("查玩家图片卡片生成/发送失败，回退文本：%s", error)
        return self._build_player_query_text(player)

    def query_player(self, backend: Dict[str, Any], name: str) -> Dict[str, Any]:
        """向单个后端查询某玩家详情（实时属性 + 活跃度）。"""
        try:
            response = requests.post(
                f"{backend['url']}/qqmcbridge/player",
                headers={
                    "X-QQMC-Token": backend["token"],
                    "Content-Type": "application/json",
                },
                json={"name": name},
                timeout=5,
            )
        except Exception as error:
            log.warning("查询 %s 玩家 %s 请求异常：%s", backend["name"], name, error)
            return {"ok": False}
        raw = response.text or ""
        log.info(
            "[查玩家] 后端 %s POST /player status=%s body=%r",
            backend["name"],
            response.status_code,
            raw[:200],
        )
        if response.status_code != 200 or not raw.strip():
            return {"ok": False}
        try:
            return response.json()
        except Exception as error:
            log.warning("查询 %s 玩家 %s 响应非 JSON：%s", backend["name"], name, error)
            return {"ok": False}

    def _build_player_query_text(self, player: Dict[str, Any]) -> str:
        """图片不可用时的查玩家纯文本兜底。"""
        name = player.get("name", "?")
        lines = ["[查玩家] " + name]
        attrs = player.get("attrs") or {}
        if player.get("online") and attrs:
            lines.append("状态：在线")
            hp = attrs.get("health")
            mhp = attrs.get("maxHealth")
            if hp is not None and mhp is not None:
                lines.append(f"血量：{hp}/{mhp}")
            mode = self._game_mode_name(attrs.get("gameMode"))
            if mode:
                lines.append("模式：" + mode)
            lvl = attrs.get("level")
            if lvl is not None:
                lines.append(f"等级：{lvl}")
        else:
            lines.append("状态：离线（仅显示活跃度）")
        act = player.get("activity") or {}
        lines.append(f"总活跃度：{float(act.get('total', 0) or 0)}")
        lines.append(f"动态活跃度：{float(act.get('dynamic', 0) or 0)}")
        for st in (act.get("stats") or []):
            try:
                cnt = int(st.get("count", 0) or 0)
            except (TypeError, ValueError):
                cnt = 0
            key = str(st.get("key", ""))
            if key == "onlineTime":
                # 在线时长计的是累计在线分钟数，格式化为 小时/分钟
                h, m = divmod(cnt, 60)
                val = f"{h} 小时 {m} 分" if m > 0 else f"{h} 小时" if h > 0 else f"{m} 分钟"
                lines.append(f"  · {st.get('name', key)}：{val}")
            else:
                unit = " 格" if key == "moveDistance" else " 次"
                lines.append(f"  · {st.get('name', key)}：{cnt}{unit}")
        return "\n".join(lines)

    def poll_backend(self, backend: Dict[str, Any]) -> list:
        response = requests.get(
            f"{backend['url']}/qqmcbridge/poll",
            headers={"X-QQMC-Token": backend["token"]},
            timeout=5,
        )
        if response.status_code != 200:
            raise RuntimeError(f"从 {backend['name']} poll 失败：{response.status_code} {response.text}")
        data = response.json()
        return data.get("messages", [])

    async def post_to_backend(
        self, backend: Dict[str, Any], sender: str, content: str,
        msg_id: str, sender_openid: str = "",
    ) -> None:
        payload = {
            "type": "qq_message",
            "sender": sender,
            "sender_openid": sender_openid,
            "is_admin": bool(sender_openid) and sender_openid in self.admin_openids,
            "content": content,
            "msg_id": msg_id,
        }
        try:
            response = await asyncio.to_thread(
                requests.post,
                f"{backend['url']}/qqmcbridge/incoming",
                headers={
                    "X-QQMC-Token": backend["token"],
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=5,
            )
        except requests.RequestException as error:
            raise RuntimeError(f"发送 QQ 消息到 {backend['name']} 失败：{error}")
        if response.status_code != 200:
            raise RuntimeError(
                f"发送 QQ 消息到 {backend['name']} 失败：{response.status_code} {response.text}"
            )
        log.info("已转发 QQ 消息到 %s，等待游戏端处理", backend["name"])

    async def poll_backends_loop(self) -> None:
        while True:
            backends = self.backends
            # 每轮开始尝试把 Python 子插件（pymods）帮助清单回推 BDS 端，
            # BDS 重启会丢失清单，故未推送成功时持续重试。
            if self.pymods and not self._pymods_registry_pushed:
                try:
                    await self.push_pymods_registry()
                except Exception as error:
                    log.warning("回推 pymods 清单失败：%s", error)
            for backend in backends:
                if not backend.get("relay_mc_to_qq"):
                    continue
                try:
                    messages = await asyncio.to_thread(self.poll_backend, backend)
                except Exception as error:
                    log.warning("从 %s 拉取消息失败：%s", backend["name"], error)
                    continue
                for message in messages:
                    content = str(message.get("content") or "")
                    msg_type = str(message.get("type") or "text")
                    # 子插件可能发送图片/视频/文件等富媒体；网关当前仅转发文本，
                    # 这里对仅有 url 的富媒体做文本兜底，避免发出空消息。
                    if msg_type != "text" and not content and message.get("url"):
                        content = str(message.get("url"))
                    msg_id = str(message.get("msg_id", ""))
                    if not content:
                        continue
                    self.push_log("mc", f"[{backend['name']}] {content}")
                    if msg_id:
                        log.info("发送 %s 的 QQ 命令回复，msg_id=%s", backend["name"], msg_id)
                    else:
                        log.info("发送 %s 的游戏主动群消息", backend["name"])
                    try:
                        # 识别 BDS 端 JS 子插件（jsmod）协议串（绘图/围棋/AI），
                        # 命中则渲染成图片或请求 AI；否则按普通消息转发（保留服务器标签）。
                        if (content.startswith(PROTO_DRAW_PREFIX)
                                or content.startswith(PROTO_GO_IMAGE_PREFIX)
                                or content.startswith(PROTO_AI_PREFIX)
                                or content == PROTO_MULTI_SERVER_ONLINE):
                            await self._dispatch_outbound_protocol(content, backend, msg_id)
                        else:
                            tagged = f"[{backend['name']}] {content}"
                            await self.send_group_message_async(tagged, msg_id)
                    except Exception as error:
                        log.error("发送 %s 的群消息失败：%s", backend["name"], error)
            await asyncio.sleep(self.poll_interval)

    async def _dispatch_outbound_protocol(self, content: str, backend: Dict[str, Any], msg_id: str) -> None:
        """处理 BDS 端返回的 jsmod 协议串：绘图/围棋/AI 回复，普通文本则退回带标签转发。"""
        # 1) 绘图协议（qqmc-draw）
        draw_payload = parse_protocol_json(content, PROTO_DRAW_PREFIX)
        if draw_payload:
            await self._send_result(content, msg_id)
            return
        # 2) 围棋图卡协议（go-9x9）
        go_payload = parse_protocol_json(content, PROTO_GO_IMAGE_PREFIX)
        if go_payload:
            await self._send_result(content, msg_id)
            return
        # 3) AI 协议（ai-assistant）：请求模型并回复
        ai_payload = parse_protocol_json(content, PROTO_AI_PREFIX)
        if ai_payload:
            sender = str(ai_payload.get("sender") or "QQ群")
            user_content = str(ai_payload.get("content") or "")
            game_status = str(ai_payload.get("game_status") or "")
            await self._reply_with_ai(sender, user_content, game_status, ai_payload, msg_id)
            return
        # 4) 多服在线汇总（由 BDS 端 multi-server-bridge 子插件触发，纯文本即可）
        if content == PROTO_MULTI_SERVER_ONLINE:
            await self.send_group_message_async(f"[{backend['name']}] 多服在线查询已处理", msg_id)
            return

    async def run(self) -> None:
        tasks: List[Any] = [self.websocket_loop(), self.poll_backends_loop()]
        tasks.extend(self._run_pymod_background(plugin)
                     for plugin in self.pymods if plugin.background)
        await asyncio.gather(*tasks)


# =========================================================================
# 本地 Web 管理面板（仅监听 127.0.0.1，外部不可达）
# =========================================================================

def build_status(gateway: "QQGateway") -> Dict[str, Any]:
    """聚合所有后端状态 + 网关运行状态，供 /api/status 使用。"""
    backends = [gateway.query_backend_status(b) for b in gateway.backends]
    return {
        "gateway": {
            "status": "online",
            "poll_interval": gateway.poll_interval,
            "reconnect_seconds": gateway.reconnect_seconds,
            "backend_count": len(gateway.backends),
            "command_server": gateway.command_server,
            "bugland_enabled": bool(gateway.bugland_config.get("bugland_enabled")),
            "web_port": gateway.web_port,
        },
        "backends": backends,
    }


def exec_on_backend(gateway: "QQGateway", target: str, command: str) -> Dict[str, Any]:
    """在指定后端执行 MC 指令（转发到 LSE 的 /qqmcbridge/command）。

    LSE >= 1.2.1：指令被投递到服务器主线程执行（HTTP 回调线程直接执行会触发
    LSE 崩溃保护炸服），接口立刻返回 {queued: true, id}，随后在此轮询
    /qqmcbridge/execresult 取回执行结果。旧版 LSE 直接返回结果，保留兼容。
    """
    backend = None
    for b in gateway.backends:
        if b["url"] == target or b["name"] == target:
            backend = b
            break
    if backend is None:
        return {"ok": False, "error": f"未知目标服务器：{target}"}
    headers = {"X-QQMC-Token": backend["token"], "Content-Type": "application/json"}
    try:
        response = requests.post(
            f"{backend['url']}/qqmcbridge/command",
            headers=headers,
            json={"command": command},
            timeout=10,
        )
        try:
            data = response.json()
        except ValueError:
            data = {"ok": False, "error": f"LSE 返回非 JSON：{response.text[:200]}"}
        # 新协议：任务已入队，轮询取结果
        if isinstance(data, dict) and data.get("queued") and data.get("id"):
            data = _poll_exec_result(backend["url"], headers, int(data["id"]))
        out = data.get("output") if isinstance(data, dict) else None
        gateway.push_log("warn", f"[Web执行] {backend['name']} {command} => {out}")
        return data
    except Exception as error:
        return {"ok": False, "error": f"转发执行指令失败：{error}"}


def _poll_exec_result(
    backend_url: str, headers: Dict[str, str], job_id: int,
    timeout: float = 8.0, interval: float = 0.15,
) -> Dict[str, Any]:
    """轮询 LSE 主线程任务的执行结果，直至完成或超时。"""
    deadline = time.monotonic() + timeout
    while True:
        try:
            response = requests.post(
                f"{backend_url}/qqmcbridge/execresult",
                headers=headers,
                json={"id": job_id},
                timeout=5,
            )
            data = response.json()
        except Exception as error:
            return {"ok": False, "error": f"查询执行结果失败：{error}"}
        if isinstance(data, dict) and data.get("pending"):
            if time.monotonic() >= deadline:
                return {"ok": False, "error": "等待执行结果超时（服务器主线程可能卡顿），指令可能仍在执行"}
            time.sleep(interval)
            continue
        return data if isinstance(data, dict) else {"ok": False, "error": "执行结果返回异常"}


def query_backend_mods(gateway: "QQGateway", backend: Dict[str, Any]) -> Dict[str, Any]:
    """查询单个后端的子插件列表（LSE 的 /qqmcbridge/mods）。"""
    try:
        response = requests.get(
            f"{backend['url']}/qqmcbridge/mods",
            headers={"X-QQMC-Token": backend["token"]},
            timeout=5,
        )
        data = response.json() if response.status_code == 200 else {}
        mods = data.get("mods", []) if isinstance(data, dict) else []
        return {"name": backend["name"], "url": backend["url"], "ok": True, "mods": mods}
    except Exception as error:
        return {"name": backend["name"], "url": backend["url"], "ok": False, "error": str(error), "mods": []}


def save_backend_mod(
    gateway: "QQGateway",
    backend: Dict[str, Any],
    name: str,
    enabled: Any,
    settings: Any,
) -> Dict[str, Any]:
    """保存单个后端的子插件启用状态 / 设置（LSE 的 /qqmcbridge/mods）。"""
    try:
        payload: Dict[str, Any] = {"name": name}
        if enabled is not None:
            payload["enabled"] = bool(enabled)
        if settings is not None:
            payload["settings"] = settings
        response = requests.post(
            f"{backend['url']}/qqmcbridge/mods",
            headers={
                "X-QQMC-Token": backend["token"],
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=8,
        )
        try:
            return response.json()
        except ValueError:
            return {"ok": False, "error": f"LSE 返回非 JSON：{response.text[:200]}"}
        except Exception as error:
            return {"ok": False, "error": f"转发保存子插件失败：{error}"}
    except Exception as error:
        return {"ok": False, "error": f"转发保存子插件失败：{error}"}


def set_pymod_enabled(gateway: "QQGateway", id: str, enabled: bool) -> Dict[str, Any]:
    """切换单个 Python 子插件的启用状态：写回 manifest.json 的 enabled 字段后再热重载。"""
    base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pymods")
    if not os.path.isdir(base):
        return {"ok": False, "error": "未找到 pymods 目录"}
    for name in sorted(os.listdir(base)):
        pdir = os.path.join(base, name)
        if not os.path.isdir(pdir):
            continue
        mpath = os.path.join(pdir, "manifest.json")
        if not os.path.isfile(mpath):
            continue
        try:
            with open(mpath, "r", encoding="utf-8") as fh:
                manifest = json.load(fh)
        except Exception as error:
            return {"ok": False, "error": f"manifest 读取失败：{error}"}
        if str(manifest.get("id", name)) != str(id):
            continue
        manifest["enabled"] = bool(enabled)
        try:
            with open(mpath, "w", encoding="utf-8") as fh:
                json.dump(manifest, fh, ensure_ascii=False, indent=2)
        except Exception as error:
            return {"ok": False, "error": f"manifest 写回失败：{error}"}
        count = gateway.load_pymods()
        return {
            "ok": True,
            "count": count,
            "enabled": bool(enabled),
            "message": f"子插件 {id} 已{'启用' if enabled else '禁用'}",
        }
    return {"ok": False, "error": f"未找到 id={id} 的子插件"}


class _WebHandler(http.server.BaseHTTPRequestHandler):
    gateway = None  # 由 WebAdminServer 注入

    def log_message(self, *args: Any) -> None:  # 静默默认访问日志
        return

    def _safe_write(self, data: bytes) -> None:
        """写响应体，静默「客户端提前断开」类异常（如 WinError 10053）。

        这类异常是正常网络现象（浏览器/客户端在收到完整响应前关闭连接），
        不代表服务错误，不应刷屏堆栈。仅对「连接已断」相关的异常静默。
        """
        try:
            self.wfile.write(data)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            pass  # 客户端已断开，忽略
        except OSError as error:
            # Windows 下 10053/10054 等也走 OSError，按错误码静默
            if getattr(error, "winerror", None) in (10053, 10054) or \
               getattr(error, "errno", None) in (10053, 10054, 32, 54):
                pass
            else:
                raise

    def _json(self, code: int, obj: Any) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self._safe_write(body)

    def _serve_file(self, filepath: str, ctype: str) -> None:
        try:
            with open(filepath, "rb") as fh:
                data = fh.read()
        except OSError:
            self._json(404, {"ok": False, "error": "file not found"})
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self._safe_write(data)

    def _serve_static(self, rel: str) -> None:
        base = os.path.abspath(WEBUI_DIR)
        target = os.path.abspath(os.path.join(base, rel))
        if target != base and not target.startswith(base + os.sep):
            self._json(403, {"ok": False, "error": "forbidden"})
            return
        ctype = "application/octet-stream"
        if rel.endswith(".html"):
            ctype = "text/html; charset=utf-8"
        elif rel.endswith(".js"):
            ctype = "application/javascript; charset=utf-8"
        elif rel.endswith(".css"):
            ctype = "text/css; charset=utf-8"
        self._serve_file(target, ctype)

    def _read_body_json(self) -> Any:
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw or b"{}")

    # ===================== 鉴权（内外网分治） =====================
    @staticmethod
    def _is_loopback(ip: str) -> bool:
        return ip in ("127.0.0.1", "::1", "::ffff:127.0.0.1") or ip.startswith("127.")

    def _client_ip(self) -> str:
        """真实客户端 IP。

        直接来自回环（本机直连或本机反代）时，若存在 X-Forwarded-For，
        取其第一个地址作为真实客户端（反代转发的外部流量）。
        直接来自非回环地址（外部直连网关端口）的，直接采用该地址，
        且忽略其自带的 XFF，防止伪造绕过。
        """
        direct = self.client_address[0]
        if self._is_loopback(direct):
            xff = self.headers.get("X-Forwarded-For")
            if xff:
                return xff.split(",")[0].strip()
        return direct

    def _is_local(self) -> bool:
        return self._is_loopback(self._client_ip())

    def _login_requires_password(self) -> bool:
        """登录时是否必须输入密码。

        基类（本地端口）本机回环可免密；公网端口永远要求密码。
        """
        return not self._is_local()

    def _token_from_request(self) -> Optional[str]:
        # 1) Cookie（HttpOnly，浏览器自动携带，覆盖页面加载与同源 API）
        cookie = self.headers.get("Cookie")
        if cookie:
            for part in cookie.split(";"):
                k, _, v = part.strip().partition("=")
                if k == "webui_token" and v:
                    return v
        # 2) 自定义请求头（兼容旧 login.html 设置的 X-Auth-Token / Bearer）
        hdr = self.headers.get("X-Auth-Token")
        if hdr:
            return hdr
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return auth[7:].strip()
        # 3) 查询参数（便于外部页面带 token 加载）
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        tok = qs.get("token", [""])[0]
        return tok or None

    def _valid_token(self, token: Optional[str]) -> bool:
        if not token:
            return False
        with WEB_AUTH_LOCK:
            exp = WEB_SESSIONS.get(token)
            if exp is None:
                return False
            if exp < time.time():
                WEB_SESSIONS.pop(token, None)
                return False
            return True

    def _require_auth(self) -> bool:
        """默认策略：本机回环免密；非本地必须持有效令牌。

        子类 _LocalWebHandler / _PublicWebHandler 会覆盖此方法实现双端口分离。
        """
        if self._is_local():
            return True
        return self._valid_token(self._token_from_request())

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        gateway = self.gateway
        if path in ("/", "/index.html"):
            if self._require_auth():
                self._serve_file(os.path.join(WEBUI_DIR, "index.html"), "text/html; charset=utf-8")
            else:
                self.send_response(302)
                self.send_header("Location", "/login.html")
                self.end_headers()
            return
        if path == "/login.html":
            self._serve_file(os.path.join(WEBUI_DIR, "login.html"), "text/html; charset=utf-8")
            return
        if path.startswith("/webui/"):
            self._serve_static(path[len("/webui/"):])
            return
        # 以下所有 API 均需授权（本机免密 / 外网凭令牌）
        if not self._require_auth():
            self._deny()
            return
        if path == "/api/status":
            self._json(200, build_status(gateway))
            return
        if path == "/api/logs":
            limit = int(urllib.parse.parse_qs(parsed.query).get("limit", ["200"])[0])
            with gateway._log_lock:
                items = list(gateway.log_buffer)[-limit:]
            self._json(200, {"logs": items})
            return
        if path == "/api/config":
            with gateway._config_lock:
                self._json(200, gateway._config)
            return
        if path == "/api/mods":
            backend_name = urllib.parse.parse_qs(parsed.query).get("backend", [""])[0]
            targets = [
                b for b in gateway.backends
                if (not backend_name or b["name"] == backend_name or b["url"] == backend_name)
            ] or gateway.backends
            results = [query_backend_mods(gateway, b) for b in targets]
            self._json(200, {"ok": True, "backends": results})
            return
        if path == "/api/pymods":
            items = [
                {
                    "id": m.id,
                    "name": m.name,
                    "version": m.version,
                    "author": m.author,
                    "description": m.description,
                    "priority": m.priority,
                    "enabled": m.enabled,
                    "help": m.help,
                }
                for m in gateway.pymods
            ]
            self._json(200, {"ok": True, "count": len(items), "pymods": items})
            return
        self._json(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        gateway = self.gateway
        if parsed.path == "/api/login":
            try:
                body = self._read_body_json()
            except Exception as error:
                self._json(400, {"ok": False, "error": f"请求体解析失败：{error}"})
                return
            username = str(body.get("username", "") or "").strip()
            password = str(body.get("password", "") or "")
            cfg = gateway._config
            need_pwd = str(cfg.get("webui_password", "") or "")
            ip = self._client_ip()
            if not self._login_rate_ok(ip):
                self._json(429, {"ok": False, "error": "登录尝试过于频繁，请稍后再试"})
                return
            if need_pwd:
                ok_user = (username == str(cfg.get("webui_username", "admin") or "admin"))
                ok_pwd = (password == need_pwd)
                if not (ok_user and ok_pwd):
                    self._login_fail(ip)
                    self._json(401, {"ok": False, "error": "用户名或密码错误"})
                    return
            else:
                # 空密码(config 未设 webui_password)：本地端口且直连回环可放行；
                # 公网端口(_PublicWebHandler 覆盖 _login_requires_password 为 True)一律拒绝，杜绝裸奔。
                if self._login_requires_password():
                    self._json(403, {"ok": False, "error": "管理员密码未配置，外网禁止登录，请先在 config.json 设置 webui_password"})
                    return
            token = os.urandom(24).hex()
            with WEB_AUTH_LOCK:
                WEB_SESSIONS[token] = time.time() + WEB_SESSION_TTL
            self.send_response(200)
            self._set_session_cookie(token)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self._safe_write(json.dumps(
                {"ok": True, "token": token,
                 "username": username or str(cfg.get("webui_username", "admin") or "admin")},
                ensure_ascii=False,
            ).encode("utf-8"))
            return
        if parsed.path == "/api/logout":
            tok = self._token_from_request()
            with WEB_AUTH_LOCK:
                if tok:
                    WEB_SESSIONS.pop(tok, None)
            self.send_response(200)
            self._clear_session_cookie()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self._safe_write(json.dumps({"ok": True}, ensure_ascii=False).encode("utf-8"))
            return
        # 其余所有 POST 接口均需授权
        if not self._require_auth():
            self._deny()
            return
        if parsed.path == "/api/exec":
            try:
                body = self._read_body_json()
            except Exception as error:
                self._json(400, {"ok": False, "error": f"请求体解析失败：{error}"})
                return
            target = str(body.get("target", "") or "")
            command = str(body.get("command", "") or "")
            if not target or not command:
                self._json(400, {"ok": False, "error": "缺少 target 或 command"})
                return
            result = exec_on_backend(gateway, target, command)
            self._json(200, result)
            return
        if parsed.path == "/api/pymods/reload":
            try:
                count = gateway.load_pymods()
                self._json(200, {"ok": True, "count": count, "message": f"已重新加载 {count} 个 Python 子插件"})
            except Exception as error:
                self._json(500, {"ok": False, "error": str(error)})
            return
        if parsed.path == "/api/mods":
            try:
                body = self._read_body_json()
            except Exception as error:
                self._json(400, {"ok": False, "error": f"请求体解析失败：{error}"})
                return
            backend = str(body.get("backend", "") or "")
            name = str(body.get("name", "") or "")
            enabled = bool(body.get("enabled", False))
            settings = body.get("settings", {})
            ok, msg = set_backend_mod(gateway, backend, name, enabled, settings)
            self._json(200, {"ok": ok, "message": msg})
            return
        if parsed.path == "/api/pymods":
            try:
                body = self._read_body_json()
            except Exception as error:
                self._json(400, {"ok": False, "error": f"请求体解析失败：{error}"})
                return
            pymod_id = str(body.get("id", "") or "")
            enabled = bool(body.get("enabled", False))
            ok, msg = set_pymod_enabled(gateway, pymod_id, enabled)
            self._json(200, {"ok": ok, "message": msg})
            return
        if parsed.path == "/qqmcbridge/qqlog":
            # 网页地图(BDSLM_JS)轮询 QQ 群入站消息，用于回显到网页聊天框。
            # 入参（POST JSON）：since = 上次收到的最大时间戳(ms)；返回 time>since 的条目。
            try:
                body = self._read_body_json()
            except Exception as error:
                self._json(400, {"ok": False, "error": f"请求体解析失败：{error}"})
                return
            try:
                since = int(body.get("since") or 0)
            except (TypeError, ValueError):
                since = 0
            items = [m for m in gateway.qq_inbound_log if int(m.get("time", 0) or 0) > since]
            self._json(200, {"ok": True, "messages": items})
            return
        if parsed.path == "/qqmcbridge/webchat":
            # 网页地图(BDSLM_JS)转发的玩家网页聊天：经此接口推送到 QQ 群。
            # 入参（POST JSON）：{ sender: "<网页用户名>", content: "<消息>" }
            try:
                body = self._read_body_json()
            except Exception as error:
                self._json(400, {"ok": False, "error": f"请求体解析失败：{error}"})
                return
            sender = str(body.get("sender") or "网页").strip() or "网页"
            content = str(body.get("content") or "").strip()
            if not content:
                self._json(400, {"ok": False, "error": "content required"})
                return
            cfg = getattr(gateway, "_config", None) or {}
            fmt = cfg.get("mc_to_qq_format", "[游戏] %s：%s")
            try:
                text = fmt % (sender, content)
            except Exception:
                text = f"[游戏] {sender}：{content}"
            try:
                gateway.send_group_message(text, "")
            except Exception as error:
                log.warning("网页聊天转发 QQ 失败：%s", error)
            self._json(200, {"ok": True})
            return
        self._json(404, {"ok": False, "error": "not found"})

    def do_PUT(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        gateway = self.gateway
        if parsed.path == "/api/config":
            if not self._require_auth():
                self._deny()
                return
            try:
                body = self._read_body_json()
            except Exception as error:
                self._json(400, {"ok": False, "error": f"请求体解析失败：{error}"})
                return
            if not isinstance(body, dict):
                self._json(400, {"ok": False, "error": "配置必须是 JSON 对象"})
                return
            try:
                _save_config_file(body)
                gateway.reload_config()
            except Exception as error:
                self._json(500, {"ok": False, "error": f"保存失败：{error}"})
                return
            self._json(200, {"ok": True})
            return
        self._json(404, {"ok": False, "error": "not found"})


class _LocalWebHandler(_WebHandler):
    """本地端口 handler：仅绑定 127.0.0.1，完全免密（GUI / 本机浏览器专用）。"""

    def _require_auth(self) -> bool:  # noqa: D401
        return True  # 本地端口不鉴权，直接放行


class _PublicWebHandler(_WebHandler):
    """公网端口 handler：对外暴露，强制登录（不论来源 IP 都必须持有效令牌）。"""

    def _require_auth(self) -> bool:  # noqa: D401
        return self._valid_token(self._token_from_request())

    def _login_requires_password(self) -> bool:
        """公网端口绝不允许空密码登录，防止反代/本机转发时绕过。"""
        return True

    def _deny(self) -> None:
        """未授权：页面跳转登录页，API 返回 401 JSON。"""
        # 注意：endswith("") 恒为 True，不能用于判断。此处仅对页面类路径(根路径或 .html)跳登录页。
        if self.command == "GET" and (self.path == "/" or self.path.endswith(".html")):
            self.send_response(302)
            self.send_header("Location", "/login.html")
            self.end_headers()
            return
        self._json(401, {"ok": False, "error": "未授权：请先登录"})

    def _set_session_cookie(self, token: str) -> None:
        secure = ""
        try:
            if str(self.gateway._config.get("webui_secure_cookie", "") or ""):
                secure = " Secure"
        except Exception:
            secure = ""
        self.send_header(
            "Set-Cookie",
            f"webui_token={token}; Path=/; HttpOnly; SameSite=Lax{secure}; Max-Age={WEB_SESSION_TTL}",
        )

    def _clear_session_cookie(self) -> None:
        self.send_header("Set-Cookie", "webui_token=; Path=/; HttpOnly; Max-Age=0")

    @staticmethod
    def _login_rate_ok(ip: str) -> bool:
        now = time.time()
        with WEB_AUTH_LOCK:
            rec = LOGIN_FAILS.get(ip)
            if rec and now - rec[1] > LOGIN_FAIL_WINDOW:
                LOGIN_FAILS.pop(ip, None)
                rec = None
            if rec and rec[0] >= LOGIN_MAX_FAILS:
                return False
            return True

    @staticmethod
    def _login_fail(ip: str) -> None:
        now = time.time()
        with WEB_AUTH_LOCK:
            rec = LOGIN_FAILS.get(ip)
            if rec is None or now - rec[1] > LOGIN_FAIL_WINDOW:
                LOGIN_FAILS[ip] = [1, now]
            else:
                rec[0] += 1

class WebAdminServer:
    """双端口 Web 管理面板服务器。

    - 公网端口(web_port)：绑定 0.0.0.0，使用 _PublicWebHandler（强制登录），对外暴露。
    - 本地端口(local_web_port)：绑定 127.0.0.1，使用 _LocalWebHandler（完全免密），GUI 专用。
    两个端口共用同一套路由逻辑，仅鉴权策略不同。
    """

    def __init__(self, gateway: "QQGateway") -> None:
        self.gateway = gateway
        self.servers: List[http.server.ThreadingHTTPServer] = []
        self._threads: List[threading.Thread] = []

    def start(self) -> None:
        _WebHandler.gateway = self.gateway
        # 公网端口：绑定所有网卡，强制登录
        try:
            public = http.server.ThreadingHTTPServer(("0.0.0.0", self.gateway.web_port), _PublicWebHandler)
            public_thread = threading.Thread(target=public.serve_forever, daemon=True)
            public_thread.start()
            self.servers.append(public)
            self._threads.append(public_thread)
            log.info("公网管理面板已启动(强制登录)：http://0.0.0.0:%d/  ", self.gateway.web_port)
        except Exception as error:
            log.error("公网端口 %d 启动失败：%s", self.gateway.web_port, error)
        # 本地端口：仅本机可达，免密（GUI 专用）
        try:
            local = http.server.ThreadingHTTPServer(("127.0.0.1", self.gateway.local_web_port), _LocalWebHandler)
            local_thread = threading.Thread(target=local.serve_forever, daemon=True)
            local_thread.start()
            self.servers.append(local)
            self._threads.append(local_thread)
            log.info("本地管理面板已启动(免密,GUI专用)：http://127.0.0.1:%d/  ", self.gateway.local_web_port)
        except Exception as error:
            log.error("本地端口 %d 启动失败：%s", self.gateway.local_web_port, error)
        log.info("你正在使用 由 QQMCBridge 二次开发的QQ官BOT适配器！")

    def stop(self) -> None:
        for srv in self.servers:
            try:
                srv.shutdown()
                srv.server_close()
            except Exception as error:
                log.warning("停止 Web 服务器时出错：%s", error)


def maybe_launch_gui() -> None:
    """检测本机是否已装桌面面板依赖，是则自动拉起 GUI。

    - 依赖齐全（PyQt5 + qfluentwidgets，requests 网关本身已依赖）才启动；
    - 环境变量 QQMC_NO_GUI=1 可禁用自动启动（桌面面板拉起网关时会传该值，
      避免「网关↔面板」互相拉起形成循环）；
    - 通过 QQMC_PARENT_GATEWAY=1 告知面板：网关已由本进程托管，面板勿重复启动网关。
    """
    if os.environ.get("QQMC_NO_GUI"):
        return
    try:
        import PyQt5  # noqa: F401
        import qfluentwidgets  # noqa: F401
    except ImportError:
        return  # 依赖未装，网关继续以无界面方式运行
    gui_script = BASE_DIR / "desktop_panel" / "main.py"
    if not gui_script.is_file():
        return
    env = dict(os.environ)
    env["QQMC_PARENT_GATEWAY"] = "1"
    try:
        creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        subprocess.Popen(
            [sys.executable, str(gui_script)],
            cwd=str(BASE_DIR),
            env=env,
            creationflags=creationflags,
        )
        log.info("已自动启动桌面控制面板（GUI）")
    except Exception as error:
        log.warning("自动启动桌面控制面板失败：%s", error)


async def amain() -> None:
    """异步入口：网关核心协程。

    - 直接 `python qq_mc_gateway.py`：`main()` 用 `asyncio.run(amain())` 拉起；
    - 被插件宿主（如 LL3）在已有事件循环中 `await` 调用：本协程一旦运行，
      `await gateway.run()` 会长期挂起，从而把宿主的事件循环「钉」在运行态，
      宿主不会在加载完插件后就退出。
    """
    log.info("QQMC 外部网关启动")
    gateway = QQGateway()
    log.info(
        "后端服务器数量：%d，分别为：%s",
        len(gateway.backends),
        "、".join(b["name"] for b in gateway.backends),
    )
    # 初始化查服图片卡片渲染器（失败不致命，会自动退回纯文本查服）
    await gateway.init_card_renderer()
    # 加载 Python 端子插件（pymods）
    pymod_count = gateway.load_pymods()
    log.info("Python 子插件已加载：%d 个", pymod_count)
    web = WebAdminServer(gateway)
    web.start()
    maybe_launch_gui()
    # 启动时检测版本更新并向群播报（一次，仅当硬编码版本 > 配置版本时触发）
    await gateway.check_and_announce_update()
    try:
        await gateway.run()
    except asyncio.CancelledError:
        log.info("网关被取消")
    finally:
        if gateway.card_renderer is not None:
            try:
                await gateway.card_renderer.close()
            except Exception as error:
                log.warning("关闭卡片渲染器时出错：%s", error)
        web.stop()


def main() -> None:
    """同步入口。

    - 无运行中事件循环（直接运行脚本）：自己 `asyncio.run(amain())`；
    - 已有运行中事件循环（被宿主调用，且宿主会 `await` 本函数）：返回 `amain()`
      协程让宿主去 await，避免再起一个循环导致
      "cannot be called from a running event loop"。
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # 直接运行脚本：自己拉起事件循环
        try:
            asyncio.run(amain())
        except KeyboardInterrupt:
            log.info("网关已停止")
        return
    # 宿主已在运行事件循环，把网关协程交给宿主 await（宿主循环随网关长期运行）
    log.info("检测到宿主事件循环，网关协程将交由宿主 await")
    return amain()


if __name__ == "__main__":
    main()
