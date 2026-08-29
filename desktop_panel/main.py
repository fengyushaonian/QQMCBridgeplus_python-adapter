#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""QQMCBridge 桌面控制面板（仿 Class-Widgets 的 qfluentwidgets / WinUI 风格）。

参考：
- Class-Widgets-main/plugin_plaza.py 的界面写法（FluentWindow + 卡片）
- PyQt-Fluent-Widgets 官方示例 examples/gallery/app/view/gallery_interface.py
  每个子页面都继承 ScrollArea，并在 __init__ 中 self.setObjectName(...)，
  否则 FluentWindow.addSubInterface 会抛出
  "The object name of `interface` can't be empty string."

功能对标 python-adapter/webui：连接与机器人启停、服务器概览、互通日志、
Python 子插件管理、后端子插件管理、配置编辑器、执行 MC 指令。
通过 HTTP API 与网关通信（与 webui 同一套接口），默认地址 127.0.0.1:8080。

依赖：pip install PyQt5 qfluentwidgets requests
"""

import json
import os
import subprocess
import sys


def _missing_dependency_exit(err: Exception) -> None:
    """依赖缺失时的友好提示：优先用 tkinter 弹窗，否则打印到控制台。"""
    msg = (
        "启动桌面面板失败：缺少必要依赖。\n\n"
        f"具体缺失：{err}\n\n"
        "请在本机执行：\n"
        "    pip install PyQt5 qfluentwidgets requests\n\n"
        "安装后即可自动启动 GUI 界面。"
    )
    print("=" * 60)
    print(msg)
    print("=" * 60)
    try:
        import tkinter as _tk
        from tkinter import messagebox as _mb
        _root = _tk.Tk()
        _root.withdraw()
        _mb.showerror("缺少依赖", msg)
    except Exception:
        pass
    sys.exit(1)


# 模块检测：仅当 PyQt5 + qfluentwidgets + requests 全部可用时才启动 GUI
try:
    import requests
    from PyQt5.QtCore import Qt, QTimer
    from PyQt5.QtGui import QFont, QIcon
    from PyQt5.QtWidgets import (
        QApplication, QHBoxLayout, QVBoxLayout, QWidget,
    )
    from qfluentwidgets import (
        BodyLabel, CaptionLabel, CardWidget, ComboBox, DoubleSpinBox,
        ExpandSettingCard, FluentIcon as FIF, FluentWindow, InfoBar,
        InfoBarPosition, LineEdit, NavigationItemPosition, PlainTextEdit,
        PrimaryPushButton, ScrollArea, setTheme, SpinBox, StrongBodyLabel,
        SubtitleLabel, SwitchButton, TextBrowser, Theme,
    )
except ImportError as _imp_err:
    _missing_dependency_exit(_imp_err)


# desktop_panel/ 的父目录即 python-adapter/（网关 qq_mc_gateway.py 所在）
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GATEWAY_SCRIPT = os.path.join(BASE_DIR, "qq_mc_gateway.py")
ICON_PATH = os.path.join(BASE_DIR, "icon.ico")


# ---------------------------------------------------------------------------
# 通用基类与工具
# ---------------------------------------------------------------------------
class BaseInterface(ScrollArea):
    """所有子页面的公共基类（仿官方 GalleryInterface）。

    关键点：每个页面都必须在 __init__ 里调用 self.setObjectName(...)，
    否则 FluentWindow.addSubInterface 会因 objectName 为空而报错。
    """

    def __init__(self, object_name: str, title: str, parent=None):
        super().__init__(parent=parent)
        self.setObjectName(object_name)
        # 是否参与「定时器自动刷新」。编辑类页面（配置/子插件设置）置 False，
        # 否则每 5 秒的自动刷新会 clear_layout 重建表单，冲掉用户正在输入的内容。
        self._auto_refresh = True

        # 关键：ScrollArea 和内部 view 必须透明，否则白色背景盖住深色主题
        # （参照官方 gallery_interface.qss：GalleryInterface, #view { background-color: transparent; }）
        self.setStyleSheet("QScrollArea { border: none; background-color: transparent; }")

        self.view = QWidget(self)
        self.view.setObjectName("view")
        self.view.setStyleSheet("background-color: transparent;")
        self.vBoxLayout = QVBoxLayout(self.view)
        self.vBoxLayout.setAlignment(Qt.AlignTop)
        self.vBoxLayout.setContentsMargins(36, 24, 36, 24)
        self.vBoxLayout.setSpacing(16)

        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setViewportMargins(0, 0, 0, 0)
        self.setWidget(self.view)
        self.setWidgetResizable(True)

        if title:
            self.vBoxLayout.addWidget(SubtitleLabel(title, self.view))

    def add_card(self, title: str = None, radius: int = 8):
        """在页面内新增一个 CardWidget，并加入布局，返回 (card, body_layout)。"""
        card, body = make_card(self.view, title, radius)
        self.vBoxLayout.addWidget(card)
        return card, body


def make_card(parent: QWidget = None, title: str = None, radius: int = 8):
    """构建一个 CardWidget，返回 (card, body_layout)。"""
    card = CardWidget(parent)
    card.setBorderRadius(radius)
    layout = QVBoxLayout(card)
    layout.setContentsMargins(20, 16, 20, 16)
    layout.setSpacing(12)
    if title:
        layout.addWidget(SubtitleLabel(title, card))
    body = QVBoxLayout()
    body.setSpacing(10)
    layout.addLayout(body)
    return card, body


def _coerce_scalar(text: str):
    """把单个字符串尽量还原成数字/布尔，否则保持字符串。"""
    text = text.strip()
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        pass
    low = text.lower()
    if low in ("true", "false"):
        return low == "true"
    return text


def _parse_scalar_list(text: str) -> list:
    """逗号分隔字符串 → 原始类型列表（供基本类型 list 的 LineEdit 使用）。"""
    return [_coerce_scalar(p) for p in text.split(",") if p.strip() != ""]


def _json_fallback_field(parent, key, value):
    """复杂结构（嵌套 list / 未知类型）兜底：用 JSON 文本框编辑。"""
    wrap = QWidget(parent)
    lay = QVBoxLayout(wrap)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(4)
    lay.addWidget(BodyLabel(f"{key}（复杂结构，JSON 编辑）", wrap))
    edit = PlainTextEdit(wrap)
    edit.setPlainText(json.dumps(value, ensure_ascii=False, indent=2))
    edit.setFixedHeight(120)
    lay.addWidget(edit)

    def get():
        try:
            return json.loads(edit.toPlainText())
        except Exception:
            return value  # 解析失败保持原值

    return wrap, get


def build_config_field(parent, key, value):
    """根据 value 类型构建图形化编辑行，返回 (widget, get_value 回调)。

    - bool    -> SwitchButton
    - int     -> SpinBox
    - float   -> DoubleSpinBox
    - str     -> LineEdit
    - list(基本类型) -> LineEdit（逗号分隔）
    - dict    -> 子分组卡片（递归）
    - 其他 / 复杂 list -> JSON 文本框兜底
    """
    if isinstance(value, dict):
        card, body = make_card(parent, str(key), radius=8)
        getters = {}
        for k, v in value.items():
            w, g = build_config_field(card, k, v)
            body.addWidget(w)
            getters[k] = g
        return card, (lambda: {k: g() for k, g in getters.items()})

    if isinstance(value, bool):
        row = QWidget(parent)
        lay = QHBoxLayout(row)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(BodyLabel(str(key), row))
        lay.addStretch(1)
        sw = SwitchButton(row)
        sw.setChecked(value)
        lay.addWidget(sw)
        return row, (lambda: sw.isChecked())

    if isinstance(value, int):
        row = QWidget(parent)
        lay = QHBoxLayout(row)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(BodyLabel(str(key), row))
        lay.addStretch(1)
        sp = SpinBox(row)
        sp.setRange(-2147483648, 2147483647)
        sp.setValue(value)
        lay.addWidget(sp)
        return row, (lambda: sp.value())

    if isinstance(value, float):
        row = QWidget(parent)
        lay = QHBoxLayout(row)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(BodyLabel(str(key), row))
        lay.addStretch(1)
        dp = DoubleSpinBox(row)
        dp.setRange(-1e9, 1e9)
        dp.setDecimals(6)
        dp.setValue(value)
        lay.addWidget(dp)
        return row, (lambda: dp.value())

    if isinstance(value, str):
        row = QWidget(parent)
        lay = QHBoxLayout(row)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(BodyLabel(str(key), row))
        le = LineEdit(row)
        le.setText(value)
        lay.addWidget(le, 1)
        return row, (lambda: le.text())

    if isinstance(value, list) and value and all(
        not isinstance(x, (dict, list)) for x in value
    ):
        row = QWidget(parent)
        lay = QHBoxLayout(row)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(BodyLabel(str(key), row))
        le = LineEdit(row)
        le.setText(", ".join(str(x) for x in value))
        lay.addWidget(le, 1)
        return row, (lambda: _parse_scalar_list(le.text()))

    return _json_fallback_field(parent, key, value)


def clear_layout(layout) -> None:
    """递归清空一个 layout 中持有的所有 widget。"""
    if layout is None:
        return
    while layout.count():
        item = layout.takeAt(0)
        w = item.widget()
        if w is not None:
            w.deleteLater()
        else:
            lay = item.layout()
            if lay is not None:
                clear_layout(lay)


def info(parent, kind: str, title: str, content: str, duration: int = 3000) -> None:
    """统一用 InfoBar 弹提示，替代 QMessageBox。"""
    fn = {
        "success": InfoBar.success,
        "warning": InfoBar.warning,
        "error": InfoBar.error,
        "info": InfoBar.info,
    }.get(kind, InfoBar.info)
    fn(
        title=title,
        content=content,
        orient=Qt.Horizontal,
        isClosable=True,
        position=InfoBarPosition.TOP,
        duration=duration,
        parent=parent,
    )


# ---------------------------------------------------------------------------
# 页面：概览（连接 + 机器人 + 服务器状态）
# ---------------------------------------------------------------------------
class OverviewPage(BaseInterface):
    def __init__(self, app):
        super().__init__("overview", "概览", parent=app)
        self.app = app

        # 连接与机器人卡片
        conn_card, conn_body = self.add_card("连接与机器人控制")
        row = QHBoxLayout()
        row.setSpacing(10)
        self.addr = LineEdit(conn_card)
        self.addr.setPlaceholderText("网关地址，例如 http://127.0.0.1:12708")
        self.addr.setText(app.base_url)
        self.addr.setMinimumWidth(260)
        self.addr.returnPressed.connect(self.apply_addr)
        row.addWidget(BodyLabel("网关地址", conn_card))
        row.addWidget(self.addr, 1)
        self.connect_btn = PrimaryPushButton("连接", conn_card)
        self.connect_btn.setIcon(FIF.LINK)
        self.connect_btn.clicked.connect(self.apply_addr)
        row.addWidget(self.connect_btn)
        self.bot_btn = PrimaryPushButton("▶ 启动机器人", conn_card)
        self.bot_btn.setIcon(FIF.PLAY)
        self.bot_btn.clicked.connect(app.toggle_bot)
        row.addWidget(self.bot_btn)
        conn_body.addLayout(row)

        # 主题切换行
        theme_row = QHBoxLayout()
        theme_row.setSpacing(10)
        theme_row.addStretch(1)
        theme_row.addWidget(BodyLabel("深色主题", conn_card))
        self.theme_switch = SwitchButton(conn_card)
        self.theme_switch.setChecked(app.theme_mode == "dark")
        self.theme_switch.checkedChanged.connect(
            lambda c: app.set_theme("dark" if c else "light")
        )
        theme_row.addWidget(self.theme_switch)
        conn_body.addLayout(theme_row)

        self.status_label = BodyLabel("连接状态：未知", conn_card)
        conn_body.addWidget(self.status_label)

        # 网关信息卡片
        gw_card, self.gw_body = self.add_card("🤖 网关运行状态")
        self.gw_info = BodyLabel("等待连接…", gw_card)
        self.gw_info.setWordWrap(True)
        self.gw_body.addWidget(self.gw_info)

        # 服务器状态容器
        sv_card, self.sv_body = self.add_card("🖥 服务器状态")
        self.backend_container = QWidget(sv_card)
        self.backend_layout = QVBoxLayout(self.backend_container)
        self.backend_layout.setContentsMargins(0, 0, 0, 0)
        self.backend_layout.setSpacing(12)
        self.sv_body.addWidget(self.backend_container)

    def apply_addr(self):
        self.app.set_base_url(self.addr.text().strip())

    def set_bot_state(self, running: bool):
        if running:
            self.bot_btn.setText("■ 关闭机器人")
            self.bot_btn.setIcon(FIF.PAUSE)
        else:
            self.bot_btn.setText("▶ 启动机器人")
            self.bot_btn.setIcon(FIF.PLAY)

    def refresh(self):
        ok = self.app.api_get("/api/status") is not None
        self.status_label.setText("连接状态：" + ("🟢 已连接" if ok else "🔴 无法连接"))
        if not ok:
            self.gw_info.setText("无法连接网关，请检查地址或先启动机器人。")
            self.set_bot_state(self.app.is_bot_running())
            clear_layout(self.backend_layout)
            return

        data = self.app.api_get("/api/status")
        gw = data.get("gateway", {})
        lines = [
            f"状态：{'🟢 在线' if gw.get('status') == 'online' else '🔴 离线'}",
            f"后端数量：{gw.get('backend_count', 0)}",
            f"轮询间隔：{gw.get('poll_interval', '-')} 秒",
            f"重连间隔：{gw.get('reconnect_seconds', '-')} 秒",
            f"指令服务器：{gw.get('command_server', '-')}",
            f"BugLand 指令：{'开启' if gw.get('bugland_enabled') else '关闭'}",
            f"Web 面板端口：{gw.get('web_port', '-')}",
        ]
        self.gw_info.setText("\n".join(lines))

        self.set_bot_state(self.app.is_bot_running())

        clear_layout(self.backend_layout)
        for b in data.get("backends", []):
            self.backend_layout.addWidget(self.build_backend_card(b))

    def build_backend_card(self, b):
        card, body = make_card(self.backend_container, None, radius=6)
        head = QHBoxLayout()
        head.setSpacing(8)
        online = b.get("status") == "online"
        dot = SubtitleLabel("●", card)
        dot.setStyleSheet(f"color: {'#27b960' if online else '#e23b3b'};")
        head.addWidget(dot)
        name = StrongBodyLabel(f"{b.get('name', '?')}  ({b.get('url', '')})", card)
        head.addWidget(name)
        head.addStretch(1)
        head.addWidget(BodyLabel("🟢 在线" if online else "🔴 离线", card))
        body.addLayout(head)

        if online:
            meta = BodyLabel(
                f"在线玩家：{b.get('online', 0)} 人　|　TPS：{b.get('tps', '-')}　|　版本：{b.get('version', '-')}",
                card,
            )
            body.addWidget(meta)
            players = b.get("players") or []
            if players:
                pnames = "，".join(str(p.get("name", "?")) for p in players)
                pl = CaptionLabel(f"玩家列表：{pnames}", card)
                pl.setWordWrap(True)
                body.addWidget(pl)
        else:
            err = BodyLabel(f"错误：{b.get('error', '未知')}", card)
            err.setStyleSheet("color: #e23b3b;")
            body.addWidget(err)
        return card


# ---------------------------------------------------------------------------
# 页面：互通日志
# ---------------------------------------------------------------------------
class LogPage(BaseInterface):
    def __init__(self, app):
        super().__init__("log", "互通日志", parent=app)
        self.app = app

        bar = QHBoxLayout()
        bar.setSpacing(10)
        bar.addWidget(BodyLabel("条数", self.view))
        self.limit = ComboBox(self.view)
        self.limit.addItems(["100", "200", "500", "1000"])
        self.limit.setCurrentText("200")
        self.limit.setFixedWidth(100)
        bar.addWidget(self.limit)
        bar.addStretch(1)
        self.vBoxLayout.addLayout(bar)

        self.text = TextBrowser(self.view)
        self.text.setLineWrapMode(TextBrowser.NoWrap)
        self.vBoxLayout.addWidget(self.text, 1)

    def refresh(self):
        limit = int(self.limit.currentText())
        data = self.app.api_get("/api/logs", {"limit": limit})
        if data is None:
            return
        self.text.clear()
        for entry in data.get("logs", []):
            if isinstance(entry, dict):
                # 网关 push_log 写入的字段是 time / type / text
                lvl = entry.get("type", entry.get("level", ""))
                ts = entry.get("time", "")
                msg = entry.get("text", entry.get("message", ""))
                color = {
                    "mc": "#27b960", "qq": "#4aa3ff", "warn": "#ff9f43",
                    "error": "#e23b3b", "info": "#c9d1d9",
                }.get(lvl, "#e8edf4")
                prefix = f"[{ts}] " if ts else ""
                self.text.append(f'{prefix}<span style="color:{color}">[{lvl}]</span> {msg}')
            else:
                self.text.append(str(entry))


# ---------------------------------------------------------------------------
# 页面：Python 子插件
# ---------------------------------------------------------------------------
class PyModPage(BaseInterface):
    def __init__(self, app):
        super().__init__("pymods", "Python 子插件", parent=app)
        self.app = app
        # 列表每 5 秒重建会打断点开关的操作，且会与手动「重新加载」重复：关闭定时刷新。
        self._auto_refresh = False

        bar = QHBoxLayout()
        bar.setSpacing(10)
        bar.addWidget(BodyLabel("🐍 已加载的 Python 子插件", self.view))
        bar.addStretch(1)
        self.reload_btn = PrimaryPushButton("🔄 重新加载", self.view)
        self.reload_btn.setIcon(FIF.SYNC)
        self.reload_btn.clicked.connect(self.reload)
        bar.addWidget(self.reload_btn)
        self.vBoxLayout.addLayout(bar)

        self.list_container = QWidget(self.view)
        self.list_layout = QVBoxLayout(self.list_container)
        self.list_layout.setContentsMargins(0, 0, 0, 0)
        self.list_layout.setSpacing(12)
        self.vBoxLayout.addWidget(self.list_container, 1)

    def refresh(self):
        data = self.app.api_get("/api/pymods")
        if data is None:
            return
        clear_layout(self.list_layout)
        for m in data.get("pymods", []):
            self.list_layout.addWidget(self.build_mod_card(m))

    def build_mod_card(self, m):
        enabled = bool(m.get("enabled", True))
        card, body = make_card(self.list_container, None, radius=8)
        head = QHBoxLayout()
        head.setSpacing(10)
        head.addWidget(SubtitleLabel("🐍", card))
        name = StrongBodyLabel(str(m.get("name", "?")), card)
        head.addWidget(name)
        head.addWidget(BodyLabel(f"v{m.get('version', '-')}", card))
        head.addWidget(BodyLabel(f"优先级 {m.get('priority', 0)}", card))
        head.addWidget(BodyLabel(f"作者：{m.get('author', '-')}", card))
        head.addStretch(1)
        switch = SwitchButton(card)
        switch.setChecked(enabled)
        switch.checkedChanged.connect(
            lambda c, mid=str(m.get("id", "")): self.toggle_enabled(mid, c)
        )
        head.addWidget(switch)
        body.addLayout(head)

        desc = BodyLabel(str(m.get("description", "")), card)
        desc.setWordWrap(True)
        body.addWidget(desc)

        help_text = str(m.get("help", ""))
        if help_text:
            h = CaptionLabel(f"指令：{help_text}", card)
            h.setWordWrap(True)
            body.addWidget(h)
        return card

    def toggle_enabled(self, mid: str, enabled: bool):
        data = self.app.api_post("/api/pymods", {"id": mid, "enabled": enabled})
        if data and data.get("ok"):
            info(self.app, "success", "成功", data.get("message", "已更新"))
        else:
            info(self.app, "error", "失败", str(data.get("error", "操作失败")) if data else "操作失败")
        self.refresh()

    def reload(self):
        data = self.app.api_post("/api/pymods/reload", {})
        if data and data.get("ok"):
            info(self.app, "success", "成功", data.get("message", "已重新加载"))
            self.refresh()
        else:
            info(self.app, "error", "失败", str(data.get("error", "重载失败")) if data else "重载失败")


# ---------------------------------------------------------------------------
# 页面：后端子插件
# ---------------------------------------------------------------------------
class ModPage(BaseInterface):
    def __init__(self, app):
        super().__init__("mods", "后端子插件", parent=app)
        self.app = app
        # 编辑页（展开卡片里可改 settings）：关闭定时自动刷新，避免输入被冲掉。
        self._auto_refresh = False

        bar = QHBoxLayout()
        bar.setSpacing(10)
        bar.addWidget(BodyLabel("🧩 后端子插件（LSE 端，点击卡片展开可自定义设置）", self.view))
        bar.addStretch(1)
        self.refresh_btn = PrimaryPushButton("🔄 刷新", self.view)
        self.refresh_btn.setIcon(FIF.UPDATE)
        self.refresh_btn.clicked.connect(self.refresh)
        bar.addWidget(self.refresh_btn)
        self.vBoxLayout.addLayout(bar)

        self.list_container = QWidget(self.view)
        self.list_layout = QVBoxLayout(self.list_container)
        self.list_layout.setContentsMargins(0, 0, 0, 0)
        self.list_layout.setSpacing(14)
        self.vBoxLayout.addWidget(self.list_container, 1)

    def refresh(self):
        data = self.app.api_get("/api/mods")
        if data is None:
            return
        clear_layout(self.list_layout)
        for b in data.get("backends", []):
            title = SubtitleLabel(
                f"🖥 {b.get('name', '?')}  ({b.get('url', '')})", self.list_container
            )
            self.list_layout.addWidget(title)
            for mod in b.get("mods", []):
                self.list_layout.addWidget(self.build_mod_card(b, mod))

    def build_mod_card(self, backend, mod):
        name = str(mod.get("name", "?"))
        enabled = bool(mod.get("enabled", False))
        # ExpandSettingCard 自带深色 qss：标题 + 描述 + 点击展开
        card = ExpandSettingCard(
            FIF.APPLICATION,
            name,
            "🟢 已启用" if enabled else "⚪ 已禁用",
            self.list_container,
        )
        switch = SwitchButton(card)
        switch.setChecked(enabled)
        switch.checkedChanged.connect(
            lambda c, url=str(backend.get("url", "")), n=name: self.toggle(url, n, c)
        )
        card.addWidget(switch)  # 头部右侧放开关

        # 展开区：图形化编辑该子插件的 settings（点击卡片展开）
        settings = mod.get("settings")
        if not isinstance(settings, dict):
            settings = {}
        form = QWidget(card.view)
        form_layout = QVBoxLayout(form)
        form_layout.setContentsMargins(0, 0, 0, 0)
        form_layout.setSpacing(8)
        getters = {}
        if settings:
            for k, v in settings.items():
                w, g = build_config_field(form, k, v)
                form_layout.addWidget(w)
                getters[k] = g
        else:
            form_layout.addWidget(BodyLabel("该插件暂无可用设置。", form))
        card.viewLayout.addWidget(form)

        save_btn = PrimaryPushButton("💾 保存设置", card.view)
        save_btn.setIcon(FIF.SAVE)
        save_btn.clicked.connect(
            lambda _checked=False, url=str(backend.get("url", "")), n=name, gs=getters: self.save_settings(url, n, gs)
        )
        card.viewLayout.addWidget(save_btn)
        return card

    def toggle(self, backend_url, name, enable):
        data = self.app.api_post(
            "/api/mods", {"backend": backend_url, "name": name, "enabled": enable}
        )
        if data and data.get("ok"):
            self.refresh()
        else:
            info(self.app, "error", "失败", str(data.get("error", "操作失败")) if data else "操作失败")

    def save_settings(self, backend_url, name, getters):
        settings = {k: g() for k, g in getters.items()}
        data = self.app.api_post(
            "/api/mods", {"backend": backend_url, "name": name, "settings": settings}
        )
        if data and data.get("ok"):
            info(self.app, "success", "成功", "设置已保存。")
        else:
            info(self.app, "error", "保存失败", str(data.get("error", "保存失败")) if data else "保存失败")


# ---------------------------------------------------------------------------
# 页面：配置编辑器
# ---------------------------------------------------------------------------
class ConfigPage(BaseInterface):
    def __init__(self, app):
        super().__init__("config", "配置编辑器", parent=app)
        self.app = app
        self._getters = {}
        # 配置是编辑页：关闭定时自动刷新，避免输入框被重建冲掉。
        # 仅在切换到本页（nav）或手动点「刷新」时重载。
        self._auto_refresh = False

        bar = QHBoxLayout()
        bar.setSpacing(10)
        bar.addWidget(BodyLabel("⚙️ 配置（图形化编辑，保存后热重载）", self.view))
        bar.addStretch(1)
        self.refresh_btn = PrimaryPushButton("🔄 刷新", self.view)
        self.refresh_btn.setIcon(FIF.UPDATE)
        self.refresh_btn.clicked.connect(self.refresh)
        bar.addWidget(self.refresh_btn)
        self.save_btn = PrimaryPushButton("💾 保存并重载", self.view)
        self.save_btn.setIcon(FIF.SAVE)
        self.save_btn.clicked.connect(self.save)
        bar.addWidget(self.save_btn)
        self.vBoxLayout.addLayout(bar)

        self.form_container = QWidget(self.view)
        self.form_layout = QVBoxLayout(self.form_container)
        self.form_layout.setContentsMargins(0, 0, 0, 0)
        self.form_layout.setSpacing(12)
        self.vBoxLayout.addWidget(self.form_container, 1)
        self.refresh()

    def refresh(self):
        data = self.app.api_get("/api/config")
        if data is None:
            return
        clear_layout(self.form_layout)
        self._getters = {}
        if not isinstance(data, dict):
            data = {}
        for k, v in data.items():
            w, g = build_config_field(self.form_container, k, v)
            self.form_layout.addWidget(w)
            self._getters[k] = g

    def save(self):
        cfg = {k: g() for k, g in self._getters.items()}
        data = self.app.api_put("/api/config", cfg)
        if data and data.get("ok"):
            info(self.app, "success", "成功", "配置已保存并热重载。")
            self.refresh()
        else:
            info(self.app, "error", "保存失败", str(data.get("error", "保存失败")) if data else "保存失败")


# ---------------------------------------------------------------------------
# 页面：执行指令
# ---------------------------------------------------------------------------
class ExecPage(BaseInterface):
    def __init__(self, app):
        super().__init__("exec", "执行指令", parent=app)
        self.app = app

        card, body = self.add_card("📟 执行 MC 指令（转发到后端 LSE）")
        row = QHBoxLayout()
        row.setSpacing(10)
        row.addWidget(BodyLabel("目标", card))
        self.target = ComboBox(card)
        self.target.setMinimumWidth(140)
        row.addWidget(self.target)
        self.cmd = LineEdit(card)
        self.cmd.setPlaceholderText("例如：list  或  say 你好")
        self.cmd.returnPressed.connect(self.run)
        row.addWidget(self.cmd, 1)
        self.run_btn = PrimaryPushButton("执行", card)
        self.run_btn.setIcon(FIF.SEND)
        self.run_btn.clicked.connect(self.run)
        row.addWidget(self.run_btn)
        body.addLayout(row)

        self.out = PlainTextEdit(card)
        self.out.setReadOnly(True)
        body.addWidget(self.out)

    def refresh(self):
        data = self.app.api_get("/api/status")
        if data is None:
            return
        current = self.target.currentText()
        self.target.clear()
        for b in data.get("backends", []):
            # 注意：qfluentwidgets 的 ComboBox.addItem(text, icon=None, userData=None)
            # 第二参数是 icon，不是 userData！必须用关键字传 userData，否则 currentData() 返回 None。
            self.target.addItem(b.get("name", "?"), userData=b.get("url", ""))
        if current:
            idx = self.target.findText(current)
            if idx >= 0:
                self.target.setCurrentIndex(idx)

    def run(self):
        target = self.target.currentData()
        if not target:
            info(self.app, "warning", "提示", "请先选择目标后端。")
            return
        data = self.app.api_post("/api/exec", {"command": self.cmd.text().strip(), "target": target})
        if data is None:
            self.out.appendPlainText("❌ 无法连接网关")
            return
        self.out.appendPlainText(f"> {self.cmd.text().strip()}")
        if data.get("ok"):
            self.out.appendPlainText(str(data.get("output", "")))
        else:
            self.out.appendPlainText(f"❌ {data.get('error', '执行失败')}")


# ---------------------------------------------------------------------------
# 主窗口
# ---------------------------------------------------------------------------
class ControlPanel(FluentWindow):
    def __init__(self):
        super().__init__()
        # GUI 专用本地端口（127.0.0.1 绑定，免密）——与公网端口完全隔离
        self.base_url = "http://127.0.0.1:12708"
        self.gateway_proc = None  # subprocess.Popen
        self.theme_mode = "dark"

        self.setWindowTitle("QQMCBridge 桌面控制台")
        self.resize(1080, 720)
        self.setMinimumSize(900, 600)
        self.setFont(QFont("Microsoft YaHei", 10))
        setTheme(Theme.DARK)
        self._apply_icon()

        # 构建各页面（均为 ScrollArea 子类，objectName 已设置）
        self.overview = OverviewPage(self)
        self.log = LogPage(self)
        self.pymods = PyModPage(self)
        self.mods = ModPage(self)
        self.config = ConfigPage(self)
        self.exec = ExecPage(self)

        self.addSubInterface(self.overview, FIF.HOME, "概览", isTransparent=True)
        self.addSubInterface(self.log, FIF.HISTORY, "互通日志", isTransparent=True)
        self.addSubInterface(self.pymods, FIF.CODE, "Python子插件", isTransparent=True)
        self.addSubInterface(self.mods, FIF.APPLICATION, "后端子插件", isTransparent=True)
        self.addSubInterface(
            self.config, FIF.SETTING, "配置",
            position=NavigationItemPosition.BOTTOM, isTransparent=True,
        )
        self.addSubInterface(
            self.exec, FIF.SEND, "执行指令",
            position=NavigationItemPosition.BOTTOM, isTransparent=True,
        )

        # 导航栏折叠/展开（WinUI 风格）：左上角菜单按钮点击切换，折叠后仅显示图标
        self.navigationInterface.setExpandWidth(300)
        self.navigationInterface.setMenuButtonVisible(True)
        self.navigationInterface.setCollapsible(True)

        # 导航切换时刷新
        self.stackedWidget.currentChanged.connect(self.on_nav_changed)

        # 自动刷新
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.auto_refresh)
        self.timer.start(5000)

        self.overview.refresh()
        self.show()

    # ---- 导航 ----
    def on_nav_changed(self, _index):
        self.refresh_current()

    def page_list(self):
        return [self.overview, self.log, self.pymods, self.mods, self.config, self.exec]

    def refresh_current(self):
        # 切换导航时总是刷新当前页（切回配置页会重新加载最新值，符合预期）。
        cur = self.stackedWidget.currentWidget()
        for p in self.page_list():
            if p is cur:
                p.refresh()
                break

    def auto_refresh(self):
        # 定时器刷新：只对「开启自动刷新」的页面生效（概览/日志）。
        # 配置、子插件等编辑页已关闭，避免输入框被重建冲掉。
        cur = self.stackedWidget.currentWidget()
        for p in self.page_list():
            if p is cur and getattr(p, "_auto_refresh", True):
                p.refresh()
                break

    # ---- API ----
    def api_get(self, path, params=None):
        try:
            r = requests.get(self.base_url + path, params=params, timeout=5)
            return r.json()
        except Exception:
            return None

    def api_post(self, path, body):
        try:
            r = requests.post(self.base_url + path, json=body, timeout=10)
            return r.json()
        except Exception:
            return None

    def api_put(self, path, body):
        try:
            r = requests.put(self.base_url + path, json=body, timeout=10)
            return r.json()
        except Exception:
            return None

    def set_base_url(self, url):
        url = url.strip().rstrip("/")
        if not url.startswith("http"):
            url = "http://" + url
        self.base_url = url
        info(self, "success", "已更新", f"连接地址：{url}")
        self.refresh_current()
        self.auto_refresh()

    def is_bot_running(self):
        return self.gateway_proc is not None and self.gateway_proc.poll() is None

    def _apply_icon(self):
        """加载 icon.ico 作为窗口/任务栏图标（不存在时静默跳过）。"""
        if os.path.isfile(ICON_PATH):
            icon = QIcon(ICON_PATH)
            self.setWindowIcon(icon)
            if hasattr(self, "titleBar") and self.titleBar is not None:
                try:
                    self.titleBar.setIcon(icon)
                except Exception:
                    pass

    # ---- 主题 ----
    def set_theme(self, mode: str):
        self.theme_mode = mode
        setTheme(Theme.DARK if mode == "dark" else Theme.LIGHT)

    # ---- 机器人启停 ----
    def toggle_bot(self):
        if self.is_bot_running():
            self.stop_bot()
        else:
            self.start_bot()

    def start_bot(self):
        if not os.path.isfile(GATEWAY_SCRIPT):
            info(self, "error", "错误", f"找不到网关脚本：\n{GATEWAY_SCRIPT}")
            return
        # 若网关已在运行（例如由网关自己反拉起的 GUI），无需重复启动
        if self.api_get("/api/status") is not None:
            info(self, "info", "已在运行", "网关已经启动，无需重复启动。")
            self.auto_refresh()
            return
        env = dict(os.environ)
        env["QQMC_NO_GUI"] = "1"  # 网关由本面板拉起，勿再反拉 GUI，避免循环
        try:
            self.gateway_proc = subprocess.Popen(
                [sys.executable, GATEWAY_SCRIPT],
                cwd=BASE_DIR,
                env=env,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
        except Exception as e:
            info(self, "error", "启动失败", str(e))
            return
        info(self, "success", "已启动", "网关进程已启动，5 秒后自动连接。")
        QTimer.singleShot(5000, self.auto_refresh)

    def stop_bot(self):
        if self.gateway_proc and self.gateway_proc.poll() is None:
            self.gateway_proc.terminate()
            try:
                self.gateway_proc.wait(timeout=8)
            except Exception:
                self.gateway_proc.kill()
        self.gateway_proc = None
        info(self, "info", "已关闭", "网关进程已结束。")
        self.refresh_current()


# ---------------------------------------------------------------------------
def main():
    app = QApplication(sys.argv)
    app.setFont(QFont("Microsoft YaHei", 10))
    if os.path.isfile(ICON_PATH):
        app.setWindowIcon(QIcon(ICON_PATH))
    win = ControlPanel()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
