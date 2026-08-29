# -*- coding: utf-8 -*-
"""查服图片卡片渲染：用系统自带的 Microsoft Edge（Chromium 内核）把 HTML 模板截图为 PNG。

为什么不用 Chrome / 不下载 Chromium？
    通过 Playwright 的 channel="msedge" 直接驱动系统已装的 Edge，
    渲染效果与 Chrome 完全一致，部署机只需 `pip install playwright`，无需 `playwright install chromium`。

液态玻璃效果（liquid glass）：
    卡片外层统一由 wrap_glass() 包裹：
      - 随机挑选 background/ 目录里的一张背景图铺满画面；
      - 卡片本身是半透明「玻璃」，叠加 backdrop-filter 的 模糊/饱和/亮度 营造磨砂质感；
      - 内联一段改造自 liquid-glass.js（https://github.com/shuding/liquid-glass）的脚本，
        在浏览器内按 .glass 真实像素尺寸生成一张 displacement map（SVG feDisplacementMap），
        让玻璃圆角边缘对背景产生「折射」扭曲 —— 即液态玻璃标志性的边缘透镜效果。
    该脚本在静态截图场景下去掉了鼠标交互，仅保留一次性折射贴图生成，确定性可复现。

依赖：
    pip install playwright        （系统需自带 Microsoft Edge，Win10/11 默认就有）
    【不需要】jinja2 / Pillow —— 本模块用纯 Python 拼 HTML，截图由 Playwright 内置能力完成。

线程安全：
    CardRenderer 使用 Playwright**Async API**（网关运行在插件宿主的事件循环中，
    Sync API 会报 "inside the asyncio loop" 错误）。内部用 asyncio.Lock 串行化截图调用；
    浏览器实例在网关进程内常驻复用，由 handle_server_query 通过 `await render_png` 调用。
"""

import asyncio
import base64
import html
import os
import random
import struct
import tempfile
import zlib
from typing import Any, Dict, List, Optional, Tuple

# --------------------------------------------------------------------------
# 内置字体（GenJyuuGothic，含完整中日韩字形，避免部署机缺字体时中文/假名显示为豆腐块）
#   字体文件需放在 card_render.py 同目录下（文件名见 _FONT_FILENAME）。
#   base64 只在首次渲染时读取一次并缓存到模块级，之后复用，避免每次查服重复读盘。
# --------------------------------------------------------------------------
_FONT_FILENAME = "misans.ttf"
_FONT_B64_CACHE: Optional[str] = None  # None=未探测；""=已探测但文件不存在；否则为 base64


def _get_font_face() -> str:
    """返回 <style> 里用的 @font-face 声明；字体文件不存在时返回空串（优雅降级）。"""
    global _FONT_B64_CACHE
    if _FONT_B64_CACHE is None:
        font_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), _FONT_FILENAME)
        if os.path.isfile(font_path):
            try:
                with open(font_path, "rb") as fh:
                    _FONT_B64_CACHE = base64.b64encode(fh.read()).decode("ascii")
            except Exception:
                _FONT_B64_CACHE = ""
        else:
            _FONT_B64_CACHE = ""
    if not _FONT_B64_CACHE:
        return ""
    return (
        "@font-face{font-family:'GenJyuuGothic';src:url(data:font/ttf;base64,"
        + _FONT_B64_CACHE
        + ") format('truetype');}"
    )


# --------------------------------------------------------------------------
# 背景图：随机挑选。位于 card_render.py 上一级目录的 background/ 下。
# 首次渲染时一次性读取全部图片并 base64 缓存（同一进程内复用，避免重复读盘）。
# --------------------------------------------------------------------------
_BACKGROUND_B64_CACHE: Optional[List[str]] = None  # None=未探测；否则为 data URL 列表

_MIME_BY_EXT = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
    "gif": "image/gif",
    "bmp": "image/bmp",
}


def _get_backgrounds() -> List[str]:
    """返回 background/ 目录下所有图片的 data URL 列表（模块级缓存）。

    背景目录按以下候选顺序查找（取第一个含有效图片的目录）：
      1. <python-adapter>/media/background   ← 部署首选：背景随 python-adapter 一起拷贝
      2. <python-adapter>/background        ← 直接放在 adapter 根
      3. <项目根>/background                ← 开发时放在 QQMCBridge/background
    这样无论把背景放在哪、只拷 python-adapter 与否都能找到。
    """
    global _BACKGROUND_B64_CACHE
    if _BACKGROUND_B64_CACHE is None:
        here = os.path.dirname(os.path.abspath(__file__))
        candidates = [
            os.path.join(here, "media", "background"),
            os.path.join(here, "background"),
            os.path.join(os.path.dirname(here), "background"),
        ]
        out: List[str] = []
        for bg_dir in candidates:
            if not os.path.isdir(bg_dir):
                continue
            found: List[str] = []
            for fn in sorted(os.listdir(bg_dir)):
                ext = fn.rsplit(".", 1)[-1].lower()
                mime = _MIME_BY_EXT.get(ext)
                if not mime:
                    continue
                try:
                    with open(os.path.join(bg_dir, fn), "rb") as fh:
                        found.append(
                            "data:" + mime + ";base64," + base64.b64encode(fh.read()).decode("ascii")
                        )
                except Exception:
                    pass
            if found:
                out = found
                break
        _BACKGROUND_B64_CACHE = out
    return _BACKGROUND_B64_CACHE


# --------------------------------------------------------------------------
# 背景亮度自适应：用纯标准库(zlib/struct)解析所选 PNG 背景图的平均相对亮度，
# 据此在「深色背景→浅色字」与「浅色背景→深色字」两套文字主题间切换，
# 保证查服卡片在任意背景下字体都足够醒目（不引入 Pillow 额外依赖）。
# --------------------------------------------------------------------------
_LUM_CACHE: Dict[str, Optional[float]] = {}  # data URL -> 平均亮度(0~1) 或 None(无法解析)


def _png_average_luminance(data_url: str) -> Optional[float]:
    """解析 PNG data URL 的平均相对亮度(0~1)，无法解析(非 PNG / 解码失败)返回 None。

    只支持 8-bit 的灰阶 / RGB / 灰阶+Alpha / RGBA（Minecraft 壁纸均为此类）。
    为控制开销，按约 200×200 网格采样像素，且仅一次性计算并缓存。
    """
    if not data_url or "," not in data_url:
        return None
    head, b64 = data_url.split(",", 1)
    if "png" not in head.lower():
        return None
    try:
        raw = base64.b64decode(b64)
    except Exception:
        return None
    if raw[:8] != b"\x89PNG\r\n\x1a\n":
        return None

    pos = 8
    width = height = bit_depth = color_type = None
    idat = bytearray()
    n = len(raw)
    while pos < n:
        if pos + 8 > n:
            break
        length = struct.unpack(">I", raw[pos : pos + 4])[0]
        ctype = raw[pos + 4 : pos + 8]
        data = raw[pos + 8 : pos + 8 + length]
        if ctype == b"IHDR":
            width, height, bit_depth, color_type = struct.unpack(">IIBB", data[:10])
        elif ctype == b"IDAT":
            idat += data
        elif ctype == b"IEND":
            break
        pos += 12 + length

    if width is None or height is None or bit_depth != 8:
        return None
    channels = {0: 1, 2: 3, 4: 2, 6: 4}.get(color_type)
    if channels is None:
        return None
    try:
        decompressed = zlib.decompress(idat)
    except Exception:
        return None

    stride = width * channels
    prev = bytearray(stride)
    total = 0.0
    count = 0
    step_x = max(1, width // 200)
    step_y = max(1, height // 200)
    for y in range(height):
        row_start = y * (stride + 1)
        if row_start + 1 > len(decompressed):
            break
        ftype = decompressed[row_start]
        row = bytearray(decompressed[row_start + 1 : row_start + 1 + stride])
        if ftype == 1:  # Sub
            for i in range(channels, stride):
                row[i] = (row[i] + row[i - channels]) & 0xFF
        elif ftype == 2:  # Up
            for i in range(stride):
                row[i] = (row[i] + prev[i]) & 0xFF
        elif ftype == 3:  # Average
            for i in range(stride):
                a = row[i - channels] if i >= channels else 0
                b = prev[i]
                row[i] = (row[i] + (a + b) // 2) & 0xFF
        elif ftype == 4:  # Paeth
            for i in range(stride):
                a = row[i - channels] if i >= channels else 0
                b = prev[i]
                c = prev[i - channels] if i >= channels else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                row[i] = (row[i] + pr) & 0xFF
        prev = row
        if y % step_y != 0:
            continue
        for x in range(0, width, step_x):
            idx = x * channels
            if idx + 2 >= len(row):
                continue
            r = row[idx]
            g = row[idx + 1]
            b = row[idx + 2] if channels >= 3 else r
            total += 0.2126 * r + 0.7152 * g + 0.0722 * b
            count += 1
    if count == 0:
        return None
    return total / count / 255.0


# 玻璃仅 18% 白叠加 + 轻微提亮(backdrop brightness 1.07)，背景偏暗时深色字不可读，
# 经换算当原图平均亮度 < 0.42 时应改用浅色字（theme-dark）。
_LUM_THRESHOLD = 0.42


def _pick_theme(bg_url: str) -> str:
    """根据所选背景图的平均亮度返回文字主题类。

    - 无背景图：bg-scrim 为纯深色，按深色背景处理 → theme-dark（浅色字）
    - 亮度未知(非 PNG 等)：沿用旧版深色字行为，避免引入新回归 → theme-light
    - 否则按阈值在 theme-dark / theme-light 间切换
    """
    if not bg_url:
        return "theme-dark"
    lum = _LUM_CACHE.get(bg_url)
    if lum is None:
        lum = _png_average_luminance(bg_url)
        _LUM_CACHE[bg_url] = lum
    if lum is None:
        return "theme-light"
    return "theme-dark" if lum < _LUM_THRESHOLD else "theme-light"


# --------------------------------------------------------------------------
# 液态玻璃脚本：改造自 liquid-glass.js。
# 在浏览器内按 .glass 真实像素尺寸生成 displacement map，并用 SVG feDisplacementMap
# 让玻璃圆角边缘对背景产生折射扭曲。无鼠标交互，加载后执行一次（叠加 setTimeout 兜底）。
# 该 JS 不含任何 Python 占位符，原样嵌入 HTML。
# --------------------------------------------------------------------------
_LIQUID_GLASS_JS = r"""
(function(){
  function smoothStep(a,b,t){t=Math.max(0,Math.min(1,(t-a)/(b-a)));return t*t*(3-2*t);}
  function rrect(x,y,w,h,r){
    var qx=Math.abs(x)-w+r, qy=Math.abs(y)-h+r;
    var m=Math.sqrt(Math.max(qx,0)*Math.max(qx,0)+Math.max(qy,0)*Math.max(qy,0));
    return Math.min(Math.max(qx,qy),0)+m-r;
  }
  function apply(){
    var glass=document.querySelector('.glass'); if(!glass) return;
    var rect=glass.getBoundingClientRect();
    var w=Math.max(1,Math.round(rect.width)), h=Math.max(1,Math.round(rect.height));
    var id='lg_'+Math.random().toString(36).slice(2,9);
    var r=Math.min(40,Math.min(w,h)/3), band=Math.min(w,h)*0.14;
    var canvas=document.createElement('canvas'); canvas.width=w; canvas.height=h;
    var cx=canvas.getContext('2d');
    var data=new Uint8ClampedArray(w*h*4); var raw=[]; var maxScale=1;
    for(var i=0;i<data.length;i+=4){
      var x=(i/4)%w, y=Math.floor(i/4/w);
      var ixp=(x/w-0.5)*w, iyp=(y/h-0.5)*h;
      var d=rrect(ixp,iyp,w/2,h/2,r);
      var disp=smoothStep(band,0,d);
      var scaled=smoothStep(0,1,disp);
      var dx=ixp*(scaled-1), dy=iyp*(scaled-1);
      if(Math.abs(dx)>maxScale)maxScale=Math.abs(dx);
      if(Math.abs(dy)>maxScale)maxScale=Math.abs(dy);
      raw.push(dx,dy);
    }
    var idx=0;
    for(var j=0;j<data.length;j+=4){
      data[j]=(raw[idx++]/maxScale+0.5)*255;
      data[j+1]=(raw[idx++]/maxScale+0.5)*255;
      data[j+2]=0; data[j+3]=255;
    }
    cx.putImageData(new ImageData(data,w,h),0,0);
    var svg=document.createElementNS('http://www.w3.org/2000/svg','svg');
    svg.setAttribute('width','0'); svg.setAttribute('height','0');
    svg.style.cssText='position:fixed;top:0;left:0;pointer-events:none;z-index:-1;';
    var defs=document.createElementNS('http://www.w3.org/2000/svg','defs');
    var filter=document.createElementNS('http://www.w3.org/2000/svg','filter');
    filter.setAttribute('id',id); filter.setAttribute('filterUnits','userSpaceOnUse');
    filter.setAttribute('colorInterpolationFilters','sRGB');
    filter.setAttribute('x','0'); filter.setAttribute('y','0');
    filter.setAttribute('width',String(w)); filter.setAttribute('height',String(h));
    var feImg=document.createElementNS('http://www.w3.org/2000/svg','feImage');
    feImg.setAttribute('id',id+'_map'); feImg.setAttribute('width',String(w)); feImg.setAttribute('height',String(h));
    feImg.setAttribute('href',canvas.toDataURL());
    feImg.setAttributeNS('http://www.w3.org/1999/xlink','href',canvas.toDataURL());
    var feDisp=document.createElementNS('http://www.w3.org/2000/svg','feDisplacementMap');
    feDisp.setAttribute('in','SourceGraphic'); feDisp.setAttribute('in2',id+'_map');
    feDisp.setAttribute('xChannelSelector','R'); feDisp.setAttribute('yChannelSelector','G');
    feDisp.setAttribute('scale',String(maxScale));
    filter.appendChild(feImg); filter.appendChild(feDisp); defs.appendChild(filter); svg.appendChild(defs);
    document.body.appendChild(svg);
    var bf='url(#'+id+') blur(3px) saturate(1.35) brightness(1.07) contrast(1.03)';
    glass.style.backdropFilter=bf; glass.style.webkitBackdropFilter=bf;
  }
  if(document.readyState==='complete'||document.readyState==='interactive'){apply();}
  else{window.addEventListener('load',apply);}
  setTimeout(apply,150);
})();
"""


# .glass 外层框架样式（半透明玻璃 + 圆角 + 阴影 + backdrop-filter 兜底）。
# __WIDTH__ 由 wrap_glass 替换为卡片宽度；__BG__ 由 wrap_glass 替换为随机背景图 data URL。
#
# 全幅背景与黑边修复：
#  - 背景图 + 半透明渐变遮罩统一承载在「根元素 html」上（见 _GLASS_FRAME_CSS 的 html 规则）。
#    根元素背景会被绘制到整张画布（含 full_page 截图的滚动区），任意高度都能完整铺满。
#  - 关键坑：之前用 .bg-scrim { position:absolute; inset:0 } 铺底，但 Chromium 在 full_page
#    截图时绝对定位层不会延伸到首屏之外，超出部分回退到 html/body 底色 #0c1018（近黑），
#    表现为「上下黑边」；再叠加 body 的 min-height:100vh 又把页面顶高，黑边更明显。
#  - 现改为背景挂 html + 去掉 min-height:100vh，黑边消除；.bg-scrim 节点保留但隐藏。
_GLASS_FRAME_CSS = """
  * { margin:0; padding:0; box-sizing:border-box; font-family:'GenJyuuGothic','Microsoft YaHei','PingFang SC',sans-serif; }
  /* 全幅背景挂根元素 html：Chromium full_page 截图时根元素背景能铺满整张画布
     （含首屏之外的滚动区），避免绝对定位铺底层在超出首屏处回退成近黑底色。 */
  html {
    background-color:#0c1018;
    background-image:
      linear-gradient(135deg, rgba(12,16,28,0.5), rgba(20,24,38,0.34)),
      url(__BG__);
    background-repeat:no-repeat, no-repeat;
    background-position:center, center;
    background-size:cover, cover;
  }
  html, body { min-height:100%; }
  body { position:relative; background:transparent; }
  /* .bg-scrim 不再承担铺底（避免 full_page 黑边），保留节点但隐藏 */
  .bg-scrim { display:none; }
  .glass {
    position:relative; z-index:1;
    width:__WIDTH__px; margin:22px auto; padding:26px;
    border-radius:40px;
    background:rgba(255,255,255,0.18);
    border:1px solid rgba(255,255,255,0.5);
    box-shadow:0 14px 45px rgba(0,0,0,0.4), inset 0 1px 2px rgba(255,255,255,0.6), inset 0 -14px 34px rgba(0,0,0,0.14);
    overflow:hidden;
    color:var(--c-text);
    -webkit-backdrop-filter:blur(3px) saturate(1.35) brightness(1.07) contrast(1.03);
    backdrop-filter:blur(3px) saturate(1.35) brightness(1.07) contrast(1.03);
  }
  /* 按背景明暗自适应的文字主题（由 wrap_glass 注入 theme-light / theme-dark）。
     默认按浅色背景（深色字）取值，保证即使未注入主题类也不会崩。 */
  .glass {
    --c-title:#16202b; --c-text:#2d3845; --c-weak:#5a6573; --c-strong:#16202b;
    --c-panel:rgba(255,255,255,0.66); --c-panel-border:rgba(255,255,255,0.6);
    --c-subpanel:rgba(255,255,255,0.6); --c-line:rgba(0,0,0,0.12);
    --c-float:transparent; --c-float-shadow:none;
    --c-accent:#c85a12;
    --c-online:#1f8a3b; --c-offline:#c02929;
    --c-footer:#3a4452; --c-footer-shadow:0 1px 3px rgba(255,255,255,0.45);
  }
  .glass.theme-dark {
    --c-title:#f3f7fc; --c-text:#e9eef6; --c-weak:#c7d0dd; --c-strong:#ffffff;
    --c-panel:rgba(16,22,34,0.6); --c-panel-border:rgba(255,255,255,0.18);
    --c-subpanel:rgba(16,22,34,0.55); --c-line:rgba(255,255,255,0.16);
    --c-float:rgba(8,12,20,0.42); --c-float-shadow:0 1px 4px rgba(0,0,0,0.7);
    --c-accent:#ffb066;
    --c-online:#5fd07a; --c-offline:#ff6b6b;
    --c-footer:#dfe7f2; --c-footer-shadow:0 1px 3px rgba(0,0,0,0.6);
  }
"""


def wrap_glass(inner_html: str, width: int = 760) -> str:
    """把卡片「内部 HTML（应含自身 <style> 与内容节点）」包裹进液态玻璃外壳。

    - 随机挑选 background/ 目录下一张背景图铺满；
    - 外层 .glass 为半透明玻璃容器，叠加 backdrop-filter 磨砂；
    - 内联液态玻璃脚本在浏览器内生成折射位移贴图（圆角边缘透镜效果）；
    - 自动注入中文字体 @font-face，子插件无需各自处理。

    返回可直接交给 render_png() 的完整 HTML 文档字符串。
    """
    font_face = _get_font_face()
    bgs = _get_backgrounds()
    bg_url = random.choice(bgs) if bgs else ""
    theme_class = _pick_theme(bg_url)
    css = _GLASS_FRAME_CSS.replace("__WIDTH__", str(width)).replace("__BG__", bg_url)
    return (
        "<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"UTF-8\">"
        "<style>" + font_face + css + "</style></head>"
        "<body>"
        "<div class=\"bg-scrim\"></div>"
        "<div class=\"glass " + theme_class + "\">" + inner_html + "</div>"
        "<script>" + _LIQUID_GLASS_JS + "</script>"
        "</body></html>"
    )


def _esc(value: Any) -> str:
    """HTML 转义，防止玩家名/服名里的 < & 等破坏结构。"""
    return html.escape(str(value))


def _game_mode_name(mode: Any) -> str:
    """把游戏模式数字转成中文；无法识别时返回空串。"""
    try:
        m = int(mode)
    except (TypeError, ValueError):
        return ""
    return {1: "创造", 2: "冒险", 3: "旁观"}.get(m, "生存")


def _fmt_duration(ms: Any) -> str:
    """毫秒时长格式化为 Xh Ym / Ym。"""
    try:
        sec = int(ms) // 1000
    except (TypeError, ValueError):
        return "0m"
    h = sec // 3600
    m = (sec % 3600) // 60
    if h > 0:
        return (f"{h}h{m:02d}m") if m else (f"{h}h")
    return f"{m}m"


def _tps_badge(tps: int) -> str:
    """TPS 颜色分级：≥18 绿 / ≥12 橙 / <12 红 / 0 或未知 灰。"""
    if tps > 0:
        if tps >= 18:
            return '<span class="tps tps-good">TPS ' + str(tps) + "</span>"
        if tps >= 12:
            return '<span class="tps tps-mid">TPS ' + str(tps) + "</span>"
        return '<span class="tps tps-bad">TPS ' + str(tps) + "</span>"
    return '<span class="tps tps-unknown">TPS --</span>'


def _server_block(s: Dict[str, Any]) -> str:
    """把单个后端的状态字典渲染成一张子卡 HTML。"""
    name = s.get("name", "?")
    status = s.get("status", "offline")
    online = s.get("online", 0) or 0
    players = s.get("players") or []
    tps = s.get("tps", 0) or 0
    version = s.get("version", "") or ""
    error = s.get("error", "") or ""

    badge = (
        '<span class="status-online">● 在线</span>'
        if status == "online"
        else '<span class="status-offline">● 离线</span>'
    )

    stat_row = '<div class="stat-row">' + _tps_badge(tps) + "</div>"

    if players:
        names = [_esc(p.get("name", p) if isinstance(p, dict) else p) for p in players]
        players_html = (
            '<div class="online-player-list">当前在线（'
            + str(len(players))
            + "）："
            + "、".join(names)
            + "</div>"
        )
    else:
        tip = _esc(error) if status != "online" else "暂无玩家在线"
        players_html = (
            '<div class="online-player-list empty-tip">当前在线：' + tip + "</div>"
        )

    return (
        '<div class="server-group">'
        + '<div class="server-title">' + _esc(name) + "</div>"
        + '<div class="server-item"><div class="server-left"><span>'
        + _esc(name)
        + "</span>"
        + badge
        + '</div><span>在线玩家：'
        + str(online)
        + "</span></div>"
        + stat_row
        + players_html
        + "</div>"
    )


def _build_server_inner(
    servers: List[Dict[str, Any]],
    generated_at: str,
    title: str,
) -> str:
    """生成查服卡片「内部 HTML」（不含玻璃外壳），半透明样式以透出背景与玻璃。"""
    blocks = "".join(_server_block(s) for s in servers)
    return (
        "<style>"
        ".container{width:100%;color:var(--c-text);}"
        ".header-bar{background:rgba(245,130,31,0.9);border-radius:16px;color:#fff;font-size:32px;font-weight:bold;padding:16px 22px;display:flex;align-items:center;gap:12px;box-shadow:0 3px 12px rgba(0,0,0,0.18);}"
        ".header-time{margin-left:auto;font-size:15px;font-weight:normal;color:#ffe2c4;}"
        ".server-group{margin-top:18px;}"
        ".server-title{font-size:26px;color:var(--c-title);font-weight:bold;margin-bottom:10px;padding:8px 12px;border-radius:12px;background:var(--c-float);border-bottom:1px solid var(--c-line);text-shadow:var(--c-float-shadow);}"
        ".server-item{display:flex;justify-content:space-between;align-items:center;gap:18px;padding:12px 14px;border-radius:12px;background:var(--c-panel);border:1px solid var(--c-panel-border);color:var(--c-text);font-size:22px;box-shadow:0 1px 6px rgba(0,0,0,0.08);}"
        ".server-left{display:flex;gap:14px;align-items:center;}"
        ".status-online{color:var(--c-online);font-weight:bold;}"
        ".status-offline{color:var(--c-offline);font-weight:bold;}"
        ".stat-row{display:flex;gap:14px;margin-top:10px;font-size:20px;color:var(--c-text);flex-wrap:wrap;align-items:center;padding:6px 10px;border-radius:10px;background:var(--c-float);text-shadow:var(--c-float-shadow);}"
        ".tps{padding:3px 12px;border-radius:20px;color:#fff;font-weight:bold;font-size:18px;}"
        ".tps-good{background:#67c23a;}.tps-mid{background:#e6a23c;}.tps-bad{background:#f56c6c;}.tps-unknown{background:#9aa3b2;}"
        ".online-player-list{font-size:19px;color:var(--c-text);margin:10px 0 0 4px;padding:8px 12px;border-radius:10px;background:var(--c-float);text-shadow:var(--c-float-shadow);}"
        ".player-row{display:flex;justify-content:space-between;align-items:center;gap:12px;padding:5px 4px;font-size:19px;border-bottom:1px dashed var(--c-line);}"
        ".player-row:last-child{border-bottom:none;}"
        ".player-name{color:var(--c-strong);font-weight:bold;word-break:break-all;}"
        ".player-attr{color:var(--c-weak);font-size:17px;white-space:nowrap;}"
        ".stats-block{margin-top:14px;background:var(--c-subpanel);border:1px solid var(--c-panel-border);border-radius:12px;padding:10px 14px;box-shadow:0 1px 6px rgba(0,0,0,0.06);}"
        ".stats-title{font-size:19px;color:var(--c-accent);font-weight:bold;margin-bottom:6px;}"
        ".stat-line{font-size:17px;color:var(--c-text);padding:3px 0;}"
        ".empty-tip{color:var(--c-weak);}"
        ".footer-text{margin-top:20px;text-align:center;color:var(--c-footer);font-size:15px;text-shadow:var(--c-footer-shadow);}"
        "</style>"
        '<div class="container">'
        '  <div class="header-bar"><span>📡 ' + _esc(title) + '</span><span class="header-time">' + _esc(generated_at) + "</span></div>"
        '  <div class="server-card">' + blocks + "</div>"
        '  <div class="footer-text">Powered by QQMCBridge+ · 更新时间 ' + _esc(generated_at) + "</div>"
        "</div>"
    )


def build_card_html(
    servers: List[Dict[str, Any]],
    generated_at: str,
    title: str = "服务器在线列表",
) -> str:
    """把所有后端状态拼成完整卡片 HTML（含随机背景 + 液态玻璃外壳）。

    servers 每项字段：name, status(online/offline), online, players[], tps, version, error
    """
    return wrap_glass(_build_server_inner(servers, generated_at, title), width=760)


# --------------------------------------------------------------------------
# 单人玩家名片（「查玩家 <名字>」命令用）
# --------------------------------------------------------------------------
def _activity_unit(key: str) -> str:
    """各统计分类的计量单位；移动距离按格计，其余按次计。

    注意：在线时长(onlineTime) 计的是累计在线「分钟数」，不走这里，
    由渲染处用 _fmt_minutes 格式化为 小时/分钟。
    """
    return " 格" if key == "moveDistance" else " 次"


def _fmt_minutes(minutes: Any) -> str:
    """把累计在线分钟数格式化为可读时长。"""
    try:
        minutes = int(minutes)
    except (TypeError, ValueError):
        minutes = 0
    if minutes <= 0:
        return "0 分钟"
    hours = minutes // 60
    mins = minutes % 60
    if hours > 0:
        return f"{hours} 小时 {mins} 分" if mins > 0 else f"{hours} 小时"
    return f"{mins} 分钟"


def _build_player_inner(player: Dict[str, Any], generated_at: str) -> str:
    """生成玩家名片「内部 HTML」（不含玻璃外壳），半透明样式。"""
    name = _esc(player.get("name", "?"))
    online = bool(player.get("online"))
    status_class = "pc-online" if online else "pc-offline"
    status_text = "● 在线" if online else "● 离线"
    attrs = player.get("attrs") or {}

    if online and attrs:
        realtime_rows = []
        hp = attrs.get("health")
        mhp = attrs.get("maxHealth")
        if hp is not None and mhp is not None:
            realtime_rows.append('<div class="pc-row">❤ 血量：' + _esc(hp) + " / " + _esc(mhp) + "</div>")
        mode = _game_mode_name(attrs.get("gameMode"))
        if mode:
            realtime_rows.append('<div class="pc-row">🎮 模式：' + _esc(mode) + "</div>")
        lvl = attrs.get("level")
        if lvl is not None:
            realtime_rows.append('<div class="pc-row">⭐ 等级：' + _esc(lvl) + "</div>")
        x = attrs.get("x")
        y = attrs.get("y")
        z = attrs.get("z")
        if x is not None and y is not None and z is not None:
            realtime_rows.append(
                '<div class="pc-row">📍 坐标：'
                + _esc(x) + ", " + _esc(y) + ", " + _esc(z)
                + "</div>"
            )
        realtime_html = "".join(realtime_rows) if realtime_rows else '<div class="pc-row">无实时数据</div>'
    else:
        realtime_html = '<div class="pc-row">玩家当前不在线，仅显示活跃度</div>'

    act = player.get("activity") or {}
    try:
        total = round(float(act.get("total", 0) or 0), 1)
    except (TypeError, ValueError):
        total = 0
    try:
        dynamic = round(float(act.get("dynamic", 0) or 0), 1)
    except (TypeError, ValueError):
        dynamic = 0
    stats = act.get("stats") or []
    stat_rows = [
        '<div class="pc-row">总活跃度：' + _esc(total) + "</div>",
        '<div class="pc-row">动态活跃度：' + _esc(dynamic) + "</div>",
    ]
    if stats:
        for st in stats:
            try:
                cnt = int(st.get("count", 0) or 0)
            except (TypeError, ValueError):
                cnt = 0
            key = str(st.get("key", ""))
            if key == "onlineTime":
                value_text = _fmt_minutes(cnt)
            else:
                value_text = str(cnt) + _activity_unit(key)
            stat_rows.append(
                '<div class="pc-row">· '
                + _esc(st.get("name", st.get("key", "?")))
                + "："
                + value_text
                + "</div>"
            )
    else:
        stat_rows.append('<div class="pc-row">暂无统计数据</div>')
    activity_html = "".join(stat_rows)

    return (
        "<style>"
        ".player-card{background:var(--c-panel);padding:20px;border-radius:18px;width:100%;border:1px solid var(--c-panel-border);box-shadow:0 2px 10px rgba(0,0,0,0.1);color:var(--c-text);}"
        ".pc-header{background:rgba(245,130,31,0.9);border-radius:14px;color:#fff;font-size:28px;font-weight:bold;padding:14px 18px;box-shadow:0 2px 8px rgba(0,0,0,0.15);}"
        ".pc-name{font-size:28px;color:var(--c-strong);font-weight:bold;margin:16px 4px 4px;display:flex;align-items:center;gap:12px;}"
        ".pc-online{color:var(--c-online);font-size:20px;font-weight:bold;}"
        ".pc-offline{color:var(--c-offline);font-size:20px;font-weight:bold;}"
        ".pc-section{margin-top:16px;background:var(--c-subpanel);border:1px solid var(--c-panel-border);border-radius:14px;padding:14px 16px;box-shadow:0 1px 6px rgba(0,0,0,0.06);}"
        ".pc-section-title{font-size:21px;color:var(--c-accent);font-weight:bold;margin-bottom:8px;border-bottom:1px solid var(--c-panel-border);padding-bottom:4px;}"
        ".pc-row{font-size:19px;color:var(--c-text);padding:4px 0;}"
        ".footer-text{margin-top:18px;text-align:center;color:var(--c-footer);font-size:15px;text-shadow:var(--c-footer-shadow);}"
        "</style>"
        '<div class="player-card">'
        '  <div class="pc-header">🧑 玩家名片</div>'
        '  <div class="pc-name">' + name + ' <span class="' + status_class + '">' + status_text + "</span></div>"
        '  <div class="pc-section">'
        '    <div class="pc-section-title">实时状态</div>'
        "    " + realtime_html +
        "  </div>"
        '  <div class="pc-section">'
        '    <div class="pc-section-title">📊 活跃度统计</div>'
        "    " + activity_html +
        "  </div>"
        '  <div class="footer-text">Powered by QQMCBridge+ · 更新时间 ' + _esc(generated_at) + "</div>"
        "</div>"
    )


def build_player_card_html(player: Dict[str, Any], generated_at: str) -> str:
    """生成单人玩家名片完整 HTML（含随机背景 + 液态玻璃外壳）。

    player 字段：name, online(bool), attrs({health,maxHealth,gameMode,level,x,y,z}|None),
                 activity({total,dynamic,stats:[{key,name,count}]})
    """
    return wrap_glass(_build_player_inner(player, generated_at), width=760)


class CardRenderer:
    """常驻一个 Edge(Chromium) 浏览器实例，复用截图。

    使用 Playwright **Async API**（网关运行在插件宿主的事件循环内，
    Sync API 会报 "inside the asyncio loop" 错误）。

    Playwright 的 import 放在 init() 内（懒加载），这样即使部署机没装 playwright，
    网关模块也能正常 import，只是 init_card_renderer() 会捕获异常并退回文本查服。
    """

    def __init__(self, width: int = 800) -> None:
        self._width = width
        self._pw = None
        self._browser = None
        self._lock = asyncio.Lock()

    @property
    def font_face(self) -> str:
        """@font-face 声明（base64 内嵌字体）；供 Python 子插件(pymods)拼卡片 HTML 复用。"""
        return _get_font_face()

    async def init(self) -> None:
        """启动浏览器。必须在事件循环内 await 调用。"""
        from playwright.async_api import async_playwright

        self._pw = await async_playwright().start()
        # channel="msedge" → 直接用系统自带 Edge，不下载 Chromium
        self._browser = await self._pw.chromium.launch(
            headless=True,
            channel="msedge",
            args=["--no-sandbox", "--disable-gpu"],
        )

    async def render_png(self, html_text: str, wait_ms: int = 400) -> str:
        """把 HTML 渲染成全页 PNG，返回临时文件路径（调用方用完需自行删除）。"""
        async with self._lock:
            page = await self._browser.new_page(
                viewport={"width": self._width, "height": 600}
            )
            try:
                await page.set_content(html_text, wait_until="load")
                await page.wait_for_timeout(wait_ms)
                fd, path = tempfile.mkstemp(suffix=".png", prefix="qqmc_card_")
                os.close(fd)
                await page.screenshot(path=path, full_page=True, type="png")
                return path
            finally:
                await page.close()

    async def close(self) -> None:
        try:
            if self._browser is not None:
                await self._browser.close()
            if self._pw is not None:
                await self._pw.stop()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# TemplateCardRenderer：上游 QQMCBridge-releasenew 的 CardRenderer 移植
# ---------------------------------------------------------------------------
# 用于兼容上游 __QQMC_HTML_CARD__ 模板卡协议（help/online/player/history/
# player-stats/checkin/server-status，缺省回退 status）。仅把「模板名 + 数据」
# 渲染为 (HTML markup, 宽, 高)，截图与发送由网关 render_html_image 完成。
# 与上方 CardRenderer（液态玻璃截图类）职责分离，二者互不影响。
# ---------------------------------------------------------------------------


class TemplateCardRenderer:
    """把模板名 + 数据渲染为 (HTML markup, 宽, 高)，交给网关截图。

    支持模板：help / online / player / history / player-stats / checkin /
    server-status（缺省回退 status）。围棋棋盘用 go_board()。
    """

    # 设计系统：深色服务器控制台风（青色数据高亮）
    _STYLE = """
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0a0f16;font-family:'Segoe UI Emoji','Microsoft YaHei','Segoe UI',sans-serif;color:#dbe7f0}
.page{width:__WIDTH__px;padding:24px}
.card{background:linear-gradient(180deg,#152537 0%,#0f1b29 100%);border:1px solid #234158;border-radius:20px;overflow:hidden;box-shadow:0 24px 60px rgba(0,0,0,.5)}
.hero{position:relative;padding:30px 34px 26px;background:linear-gradient(135deg,#0c2836 0%,#14536b 100%);border-bottom:1px solid #1d4a5e;overflow:hidden}
.hero:before{content:'';position:absolute;inset:0;background:repeating-linear-gradient(90deg,rgba(77,216,228,.05) 0 1px,transparent 1px 28px),repeating-linear-gradient(0deg,rgba(77,216,228,.05) 0 1px,transparent 1px 28px)}
.hero:after{content:'';position:absolute;width:300px;height:300px;right:-110px;top:-160px;border-radius:50%;background:radial-gradient(circle,rgba(77,216,228,.24),transparent 65%)}
.brand{position:relative;z-index:1;display:flex;align-items:center;gap:10px}
.brand .mark{width:10px;height:10px;border-radius:3px;background:#4dd8e4;box-shadow:0 0 12px rgba(77,216,228,.8)}
.eyebrow{position:relative;z-index:1;font-size:11px;letter-spacing:3px;color:#4dd8e4;font-weight:700}
.title{position:relative;z-index:1;font-size:30px;font-weight:800;color:#fff;margin-top:8px;letter-spacing:1px}
.subtitle{position:relative;z-index:1;font-size:13px;color:#9fc4d6;margin-top:7px;line-height:1.55}
.body{padding:22px 24px}
.section{border:1px solid #20344a;border-radius:14px;overflow:hidden;margin-bottom:14px;background:#0f1e30}
.section:last-child{margin-bottom:0}
.section-head{display:flex;align-items:center;gap:10px;padding:13px 16px;font-size:16px;font-weight:800;color:#eaf4fb;background:#132840;border-bottom:1px solid #20344a}
.section-head .dot{width:4px;height:16px;border-radius:3px;background:#4dd8e4;flex:0 0 4px}
.section-head .green{background:#3fd6a0}.section-head .amber{background:#f0b948}
.section-head .count{margin-left:auto;font-size:12px;color:#7ca4b8;font-weight:600}
.row{display:flex;align-items:center;gap:14px;padding:10px 16px;border-top:1px solid #16283c}
.row:first-child{border-top:0}
.cmd{flex:0 0 190px;display:inline-block;padding:6px 10px;border-radius:8px;background:#12314a;border:1px solid #1d4a63;color:#a8e4ec;font-family:'Cascadia Mono','Microsoft YaHei',monospace;font-size:13px;font-weight:700;overflow-wrap:anywhere}
.desc{flex:1;min-width:0;font-size:13px;color:#8fb2c4;line-height:1.5;overflow-wrap:anywhere}
.footer{display:flex;justify-content:space-between;gap:16px;flex-wrap:wrap;padding:15px 24px 19px;border-top:1px solid #1a2d40;color:#6f93a6;font-size:12px}
.stat-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:16px}
.stat{padding:14px 16px;border-radius:12px;background:#102133;border:1px solid #20344a}
.stat b{display:block;font-size:26px;color:#4dd8e4;font-weight:800;font-family:'Segoe UI','Microsoft YaHei',sans-serif}
.stat span{font-size:12px;color:#8fb2c4}
.metric{padding:16px;border:1px solid #20344a;border-radius:12px;background:#0f1e30;margin:12px 0}
.metric-top{display:flex;justify-content:space-between;align-items:center;font-size:15px;font-weight:800;color:#eaf4fb}
.metric-value{font-size:22px;color:#4dd8e4;font-weight:800}
.bar{height:10px;border-radius:6px;background:#0c1a2a;margin-top:12px;overflow:hidden;border:1px solid #1b3144}
.fill{height:100%;border-radius:6px;background:linear-gradient(90deg,#1fa9c4,#4dd8e4)}
.fill.warn{background:linear-gradient(90deg,#c8921f,#f0b948)}
.fill.danger{background:linear-gradient(90deg,#cf4a4a,#ff6b6b)}
.detail{font-size:12px;color:#8fb2c4;margin-top:9px}
.chips{display:flex;gap:8px;flex-wrap:wrap;padding:14px 16px;background:#0f1e30}
.chip{padding:6px 11px;border-radius:9px;background:#123d4d;border:1px solid #1f4a5e;color:#9fdfe6;font-size:13px}
.chip.offline{background:#1a2834;border-color:#26394a;color:#6d7f8c}
.avatar{width:46px;height:46px;border-radius:50%;background:#12314a;border:2px solid #1d4a63;color:#4dd8e4;display:flex;align-items:center;justify-content:center;font-size:22px;font-weight:800;flex:0 0 46px}
.badge{padding:3px 10px;border-radius:20px;font-size:11px;font-weight:700}
.badge.on{background:rgba(63,214,160,.15);color:#3fd6a0;border:1px solid rgba(63,214,160,.4)}
.badge.off{background:#1a2834;color:#6d7f8c;border:1px solid #26394a}
.empty{padding:34px;text-align:center;color:#6f93a6;font-size:14px}
.wd{height:30px;display:flex;align-items:center;justify-content:center;font-size:12px;color:#5d7f93}
</style>"""

    def __init__(self, width: int = 800) -> None:
        self.width = max(640, min(1000, int(width)))

    @staticmethod
    def _esc(value: Any) -> str:
        return html.escape(str(value if value is not None else ""))

    def _base(self, width: int) -> str:
        return self._STYLE.replace("__WIDTH__", str(int(width)))

    # ---------------- 模板入口 ----------------

    def render(self, template: str, data: Dict[str, Any]) -> Tuple[str, int, int]:
        template = str(template or "status")
        payload = data.get("data") if isinstance(data.get("data"), dict) else {}
        width = max(640, min(1000, int(data.get("width") or self.width)))
        builder = getattr(self, f"_tpl_{template.replace('-', '_')}", self._tpl_status)
        return builder(payload, width)

    # ---------------- 各模板 ----------------

    def _tpl_help(self, p: Dict[str, Any], width: int) -> Tuple[str, int, int]:
        sections_html = []
        row_count = 0
        for section in p.get("sections") or []:
            rows = section.get("rows") or []
            row_count += len(rows)
            tone = self._esc(section.get("tone") or "")
            rows_html = "".join(
                f"<div class='row'><span class='cmd'>{self._esc(row.get('command'))}</span>"
                f"<span class='desc'>{self._esc(row.get('description'))}</span></div>"
                for row in rows)
            sections_html.append(
                f"<div class='section'><div class='section-head'>"
                f"<i class='dot {tone}'></i>{self._esc(section.get('title'))}"
                f"<span class='count'>{len(rows)} 条</span></div>{rows_html}</div>")
        body = "".join(sections_html) or "<div class='empty'>暂无可用指令</div>"
        markup = (
            f"{self._base(width)}<div class='page'><div class='card'>"
            f"<div class='hero'><div class='brand'><i class='mark'></i>"
            f"<span class='eyebrow'>{self._esc(p.get('eyebrow') or 'COMMAND CENTER')}</span></div>"
            f"<div class='title'>{self._esc(p.get('title') or '指令中心')}</div>"
            f"<div class='subtitle'>{self._esc(p.get('subtitle'))}</div></div>"
            f"<div class='body'>{body}</div>"
            f"<div class='footer'><span>共 {int(p.get('total') or row_count)} 个指令</span>"
            f"<span>{self._esc(p.get('date'))}</span></div></div></div>")
        return markup, width, 320 + row_count * 72 + len(sections_html) * 64

    def _tpl_online(self, p: Dict[str, Any], width: int) -> Tuple[str, int, int]:
        cards = []
        for server in p.get("servers") or []:
            online = server.get("status") == "online"
            players = server.get("players") or []
            chips = "".join(
                f"<span class='chip{' offline' if not online else ''}'>{self._esc(name)}</span>"
                for name in players)
            body = chips or ("<span class='desc'>当前无人在线</span>" if online
                             else "<span class='desc'>无法连接，服务器可能已关闭</span>")
            cards.append(
                f"<div class='section'><div class='section-head'><i class='dot{' amber' if not online else ''}'></i>"
                f"{self._esc(server.get('name'))}"
                f"<span class='count'>{'在线 · ' + str(len(players)) + ' 人' if online else '离线'}</span></div>"
                f"<div class='chips'>{body}</div></div>")
        sections = "".join(cards) if cards else "<div class='empty'>暂无服务器数据</div>"
        markup = (
            f"{self._base(width)}<div class='page'><div class='card'>"
            f"<div class='hero'><div class='brand'><i class='mark'></i>"
            f"<span class='eyebrow'>LIVE SERVER MONITOR</span></div>"
            f"<div class='title'>服务器在线状态</div>"
            f"<div class='subtitle'>实时汇总 · {self._esc(p.get('updated_at'))}</div></div>"
            f"<div class='body'><div class='stat-grid'>"
            f"<div class='stat'><b>{int(p.get('total_players') or 0)}</b><span>在线玩家</span></div>"
            f"<div class='stat'><b>{int(p.get('online_servers') or 0)}</b><span>在线服务器</span></div>"
            f"<div class='stat'><b>{int(p.get('server_count') or 0)}</b><span>服务器总数</span></div>"
            f"</div>{sections}</div></div></div>")
        return markup, width, 380 + len(cards) * 150

    def _tpl_player(self, p: Dict[str, Any], width: int) -> Tuple[str, int, int]:
        ratio = max(0, min(100, float(p.get("ratio") or 0) * 100))
        markup = (
            f"{self._base(width)}<div class='page'><div class='card'>"
            f"<div class='hero'><div class='brand'><i class='mark'></i>"
            f"<span class='eyebrow'>PLAYER PROFILE</span></div>"
            f"<div class='title'>玩家信息</div>"
            f"<div class='subtitle'>实时状态 · {self._esc(p.get('updated'))}</div></div>"
            f"<div class='body'><div class='section'>"
            f"<div class='section-head'><i class='dot'></i>"
            f"<span class='avatar' style='width:34px;height:34px;flex:0 0 34px;font-size:16px'>{self._esc(p.get('initial'))}</span>"
            f"{self._esc(p.get('name'))}<span class='count'>{self._esc(p.get('mode'))}</span></div>"
            f"<div class='metric'><div class='metric-top'><span>生命值</span>"
            f"<span class='metric-value'>{self._esc(p.get('health'))} / {self._esc(p.get('maxHealth'))}</span></div>"
            f"<div class='bar'><div class='fill' style='width:{ratio:.0f}%'></div></div></div>"
            f"<div class='row'><span class='cmd'>坐标</span>"
            f"<span class='desc'>{self._esc(p.get('x'))}, {self._esc(p.get('y'))}, {self._esc(p.get('z'))}</span></div>"
            f"<div class='row'><span class='cmd'>世界</span><span class='desc'>{self._esc(p.get('world'))}</span></div>"
            f"</div></div></div></div>")
        return markup, width, 560

    def _tpl_history(self, p: Dict[str, Any], width: int) -> Tuple[str, int, int]:
        chips = "".join(
            f"<span class='chip{' offline' if not item.get('online') else ''}'>{self._esc(item.get('name'))}"
            f"<i style='font-style:normal;opacity:.55;margin-left:4px'>×{int(item.get('count') or 1)}</i></span>"
            for item in p.get("rows") or [])
        if p.get("hidden"):
            chips += f"<span class='chip offline'>+{int(p.get('hidden'))} 位</span>"
        body = chips or "<div class='empty'>暂无到访记录</div>"
        count = len(p.get("rows") or [])
        per_row = max(4, width // 130)
        page = int(p.get("page") or 1)
        total_pages = int(p.get("total_pages") or 1)
        nav = ""
        if total_pages > 1:
            nav = (f"<span>第 {page}/{total_pages} 页 · 发送「历史玩家 <b>{page + 1}</b>」翻页"
                   f"</span><span>{self._esc(p.get('updated'))}</span>")
        else:
            nav = f"<span>按最近到访时间排序</span><span>{self._esc(p.get('updated'))}</span>"
        height = 300 + ((count + 1) // per_row + 1) * 44
        markup = (
            f"{self._base(width)}<div class='page'><div class='card'>"
            f"<div class='hero'><div class='brand'><i class='mark'></i>"
            f"<span class='eyebrow'>PLAYER ARCHIVE</span></div>"
            f"<div class='title'>{self._esc(p.get('title') or '历史玩家')}</div>"
            f"<div class='subtitle'>累计到访 {int(p.get('total') or 0)} 位 · 当前在线 {int(p.get('online') or 0)} 人"
            + (f" · 第 {page}/{total_pages} 页" if total_pages > 1 else "") + "</div></div>"
            f"<div class='body'><div class='section'><div class='chips'>{body}</div></div></div>"
            f"<div class='footer'>{nav}</div>"
            f"</div></div>")
        return markup, width, height

    def _tpl_player_stats(self, p: Dict[str, Any], width: int) -> Tuple[str, int, int]:
        values = p.get("values") or []
        labels = p.get("labels") or []
        numeric = [float(v) for v in values if v is not None]
        max_value = max(numeric or [1]) or 1
        chart_w, chart_h = 700, 300
        left, right, top, bottom = 58, 20, 18, 52
        plot_w, plot_h = chart_w - left - right, chart_h - top - bottom
        count = max(1, len(values) - 1)

        def px(index: int) -> float:
            return left + index * plot_w / count

        def py(value: float) -> float:
            return top + plot_h - float(value) / max_value * plot_h

        points = " ".join(f"{px(i):.1f},{py(float(v)):.1f}"
                          for i, v in enumerate(values) if v is not None)
        y_ticks = []
        for tick in range(5):
            value = max_value * (4 - tick) / 4
            y = top + plot_h * tick / 4
            y_ticks.append(
                f"<line x1='{left}' y1='{y:.1f}' x2='{chart_w-right}' y2='{y:.1f}' stroke='#1b3144'/>"
                f"<text x='{left-10}' y='{y+5:.1f}' text-anchor='end' fill='#7ca4b8' font-size='13'>{value:.0f}</text>")
        x_ticks = []
        for index, label in enumerate(labels):
            if index % max(1, len(labels) // 6) != 0 and index != len(labels) - 1:
                continue
            x_ticks.append(
                f"<text x='{px(index):.1f}' y='{chart_h-16}' text-anchor='middle' "
                f"fill='#7ca4b8' font-size='12'>{self._esc(label)}</text>")
        dots = "".join(
            f"<circle cx='{px(i):.1f}' cy='{py(float(v)):.1f}' r='3.5' fill='#4dd8e4'/>"
            for i, v in enumerate(values) if v is not None)
        svg = (
            f"<svg width='100%' height='{chart_h}' viewBox='0 0 {chart_w} {chart_h}' preserveAspectRatio='none'>"
            f"<defs><linearGradient id='area' x1='0' y1='0' x2='0' y2='1'>"
            f"<stop offset='0' stop-color='#4dd8e4' stop-opacity='.30'/>"
            f"<stop offset='1' stop-color='#4dd8e4' stop-opacity='0'/></linearGradient></defs>"
            f"<polygon points='{left},{top+plot_h} {points} {left+plot_w},{top+plot_h}' fill='url(#area)'/>"
            f"{''.join(y_ticks)}"
            f"<line x1='{left}' y1='{top}' x2='{left}' y2='{top+plot_h}' stroke='#2a4a5e' stroke-width='1.5'/>"
            f"<line x1='{left}' y1='{top+plot_h}' x2='{chart_w-right}' y2='{top+plot_h}' stroke='#2a4a5e' stroke-width='1.5'/>"
            f"<polyline points='{points}' fill='none' stroke='#4dd8e4' stroke-width='4' "
            f"stroke-linecap='round' stroke-linejoin='round'/>{dots}{''.join(x_ticks)}</svg>")
        markup = (
            f"{self._base(width)}<div class='page'><div class='card'>"
            f"<div class='hero'><div class='brand'><i class='mark'></i>"
            f"<span class='eyebrow'>PLAYER ACTIVITY</span></div>"
            f"<div class='title'>{self._esc(p.get('title') or '玩家统计')}</div>"
            f"<div class='subtitle'>{self._esc(p.get('updated'))}</div></div>"
            f"<div class='body'><div class='stat-grid'>"
            f"<div class='stat'><b>{self._esc(p.get('current'))}</b><span>当前在线</span></div>"
            f"<div class='stat'><b>{self._esc(p.get('peak'))}</b><span>24h 峰值</span></div>"
            f"<div class='stat'><b>{self._esc(p.get('average'))}</b><span>平均人数</span></div>"
            f"</div><div class='section' style='padding:20px 14px 12px'>{svg}</div></div></div></div>")
        return markup, width, 640

    def _tpl_checkin(self, p: Dict[str, Any], width: int) -> Tuple[str, int, int]:
        weekdays = "".join(f"<div class='wd'>{day}</div>" for day in ("一", "二", "三", "四", "五", "六", "日"))
        cells = ""
        for item in p.get("calendar") or []:
            checked = bool(item.get("checked"))
            future = bool(item.get("future"))
            today = bool(item.get("today"))
            background = "#12304a" if checked else ("#0c1a2a" if future else "#0f2133")
            border = "#f0b948" if today and not checked else "#1f3a52"
            color = "#4dd8e4" if checked else "#7ca4b8"
            main = "🦌" if checked else self._esc(item.get("day"))
            sub = f"<small style='font-size:11px'>{self._esc(item.get('day'))}</small>" if checked else ""
            cells += (
                f"<div style='height:64px;border-radius:10px;background:{background};"
                f"border:2px solid {border};display:flex;flex-direction:column;"
                f"align-items:center;justify-content:center;color:{color}'>"
                f"<b style='font-size:{'30px' if checked else '17px'};line-height:1.2'>{main}</b>{sub}</div>")
        markup = (
            f"{self._base(width)}<div class='page'><div class='card'>"
            f"<div class='hero'><div class='brand'><i class='mark'></i>"
            f"<span class='eyebrow'>DAILY CHECK-IN</span></div>"
            f"<div class='title'>{self._esc(p.get('title') or '小鹿打卡')}</div>"
            f"<div class='subtitle'>{self._esc(p.get('month'))} · {self._esc(p.get('detail'))}</div></div>"
            f"<div class='body'><div class='stat-grid'>"
            f"<div class='stat'><b>{self._esc(p.get('total'))}</b><span>累计打卡</span></div>"
            f"<div class='stat'><b>{self._esc(p.get('streak'))}</b><span>连续天数</span></div>"
            f"<div class='stat'><b>24 / 7</b><span>坚持每一天</span></div></div>"
            f"<div class='section'><div class='chips' style='display:grid;"
            f"grid-template-columns:repeat(7,1fr);gap:8px'>{weekdays}{cells}</div></div>"
            f"</div></div></div>")
        return markup, width, 800

    def _tpl_server_status(self, p: Dict[str, Any], width: int) -> Tuple[str, int, int]:
        metrics_html = ""
        for metric in p.get("metrics") or []:
            value = max(0, min(100, float(metric.get("percent") or 0)))
            tone = "danger" if value >= 85 else ("warn" if value >= 60 else "")
            metrics_html += (
                f"<div class='metric'><div class='metric-top'><span>{self._esc(metric.get('label'))}</span>"
                f"<span class='metric-value'>{value:.0f}%</span></div>"
                f"<div class='bar'><div class='fill {tone}' style='width:{value:.0f}%'></div></div>"
                f"<div class='detail'>{self._esc(metric.get('detail'))}</div></div>")
        disks_html = "".join(
            f"<div class='metric' style='margin:10px 0'><div class='metric-top'>"
            f"<span>{self._esc(item.get('label'))}</span>"
            f"<span class='metric-value'>{float(item.get('percent') or 0):.0f}%</span></div>"
            f"<div class='bar'><div class='fill "
            f"{'danger' if float(item.get('percent') or 0) >= 85 else ('warn' if float(item.get('percent') or 0) >= 60 else '')}"
            f"' style='width:{max(0, min(100, float(item.get('percent') or 0))):.0f}%'></div></div>"
            f"<div class='detail'>{self._esc(item.get('detail'))}</div></div>"
            for item in p.get("disks") or [])
        content = metrics_html
        if disks_html:
            content += (f"<div class='section'><div class='section-head'>"
                        f"<i class='dot amber'></i>磁盘空间</div>{disks_html}</div>")
        if not content:
            content = "<div class='empty'>暂无监控数据</div>"
        markup = (
            f"{self._base(width)}<div class='page'><div class='card'>"
            f"<div class='hero'><div class='brand'><i class='mark'></i>"
            f"<span class='eyebrow'>HOST PERFORMANCE</span></div>"
            f"<div class='title'>{self._esc(p.get('title') or '服务器状态')}</div>"
            f"<div class='subtitle'>{self._esc(p.get('host'))} · {self._esc(p.get('sampled_at'))}</div></div>"
            f"<div class='body'>{content}</div>"
            f"<div class='footer'><span>系统运行 {self._esc(p.get('uptime'))}</span>"
            f"<span>网关运行 {self._esc(p.get('gateway_uptime'))}</span></div></div></div>")
        return markup, width, 420 + len(p.get("metrics") or []) * 150 + len(p.get("disks") or []) * 110

    def _tpl_status(self, p: Dict[str, Any], width: int) -> Tuple[str, int, int]:
        """通用信息卡：title/subtitle + rows[{label, value}] + footer。"""
        rows = "".join(
            f"<div class='row'><span class='cmd'>{self._esc(item.get('label'))}</span>"
            f"<span class='desc'>{self._esc(item.get('value'))}</span></div>"
            for item in p.get("rows") or [])
        if not rows:
            rows = "<div class='empty'>暂无内容</div>"
        markup = (
            f"{self._base(width)}<div class='page'><div class='card'>"
            f"<div class='hero'><div class='brand'><i class='mark'></i>"
            f"<span class='eyebrow'>{self._esc(p.get('eyebrow') or 'INFO')}</span></div>"
            f"<div class='title'>{self._esc(p.get('title') or '信息')}</div>"
            f"<div class='subtitle'>{self._esc(p.get('subtitle'))}</div></div>"
            f"<div class='body'><div class='section'>{rows}</div></div>"
            f"<div class='footer'><span>{self._esc(p.get('footer'))}</span></div></div></div>")
        return markup, width, 320 + len(p.get("rows") or []) * 60

    # ---------------- 围棋棋盘（专用渲染） ----------------

    def go_board(self, data: Dict[str, Any]) -> Tuple[str, int, int]:
        """9x9 围棋棋盘：木色底 + 完整 A-I / 1-9 坐标 + 星位 + 立体棋子。"""
        board = data.get("board") if isinstance(data.get("board"), list) else []
        width, height = 800, 820
        black = html.escape(str((data.get("black") or {}).get("name") or "等待"))
        white = html.escape(str((data.get("white") or {}).get("name") or "等待加入"))
        parts = [
            f"<div style=\"width:{width}px;min-height:{height}px;background:#f4e0a4;"
            f"padding:28px 38px;font-family:'Segoe UI Emoji','Microsoft YaHei',sans-serif;"
            f"color:#33240f;box-sizing:border-box\">"
            f"<div style='height:72px;display:flex;align-items:center;justify-content:space-between;"
            f"gap:20px;font-size:24px;font-weight:700;white-space:nowrap;overflow:hidden'>"
            f"<span style='max-width:300px;overflow:hidden;text-overflow:ellipsis'>● {black}</span>"
            f"<span>vs</span>"
            f"<span style='max-width:300px;overflow:hidden;text-overflow:ellipsis'>○ {white}</span></div>"
            f"<div style='position:relative;width:640px;height:610px;margin:0 auto'>"]
        # 顶部字母坐标 A-I
        for col in range(9):
            x = 34 + col * 64
            parts.append(f"<b style=\"position:absolute;left:{x}px;top:0;transform:translate(-50%,0);"
                         f"font-size:18px;line-height:24px;width:28px;text-align:center\">{chr(65 + col)}</b>")
        # 左侧数字坐标 1-9
        for row in range(9):
            y = 24 + row * 64
            parts.append(f"<b style=\"position:absolute;left:10px;top:{y}px;transform:translateY(-50%);"
                         f"font-size:18px;line-height:24px;width:18px;text-align:right\">{row + 1}</b>")
        # 网格
        parts.append("<div style='position:absolute;left:34px;top:24px;width:514px;height:514px;"
                     "box-sizing:border-box;background:#f4e0a4;overflow:visible'>")
        for offset in range(0, 513, 64):
            parts.append(f"<i style='position:absolute;left:0;top:{offset}px;width:513px;height:2px;"
                         f"background:#43301a;transform:translateY(-1px)'></i>")
            parts.append(f"<i style='position:absolute;left:{offset}px;top:0;width:2px;height:513px;"
                         f"background:#43301a;transform:translateX(-1px)'></i>")
        # 星位（9x9：四角 3-3 与天元）
        for star_row, star_col in ((2, 2), (2, 6), (4, 4), (6, 2), (6, 6)):
            star_x = 34 + star_col * 64
            star_y = 24 + star_row * 64
            parts.append(f"<i style='position:absolute;left:{star_x - 4}px;top:{star_y - 4}px;"
                         f"width:8px;height:8px;border-radius:50%;background:#43301a'></i>")
        # 棋子
        last = data.get("lastMove") or {}
        for row, values in enumerate(board[:9]):
            if not isinstance(values, list):
                continue
            for col, value in enumerate(values[:9]):
                if value not in {"black", "white"}:
                    continue
                left, top = col * 64 - 28, row * 64 - 28
                fill = "#20211f" if value == "black" else "#f7f6f1"
                stroke = "#0c0c0b" if value == "black" else "#c9c7bd"
                mark = ("box-shadow:0 0 0 4px #e04848 inset,0 3px 6px rgba(0,0,0,.35);"
                        if int(last.get("row", -1)) == row and int(last.get("col", -1)) == col
                        else "box-shadow:0 3px 6px rgba(0,0,0,.35);")
                parts.append(f"<i style='position:absolute;left:{left}px;top:{top}px;width:56px;"
                             f"height:56px;box-sizing:border-box;border-radius:50%;"
                             f"background:{fill};border:2px solid {stroke};{mark}'></i>")
        parts.append(f"</div><div style='position:absolute;left:34px;top:594px;"
                     f"font-size:18px;line-height:24px'>"
                     f"回合：{html.escape(str(data.get('turn') or '对局结束'))}</div></div>")
        return "".join(parts), width, height
