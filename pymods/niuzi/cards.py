# -*- coding: utf-8 -*-
"""牛子系统卡片渲染。

策略：
- 我的牛子 / 看看你的 / 领养牛子 / 我的对象：用 glass_wrap 渲染「半透明卡片 + 牛图 + 文本」
- 比划比划 / 贴贴：双牛图并排 + 结果文本
- 牛子榜：标题 + 前 N 名文本列表
- 图片以 base64 内嵌（data: URI），网关的 CardRenderer/浏览器可直接显示
- 渲染器不可用时回退纯文本（与原版思路一致）

文件位置：
- muniu.png / gongniu.png / menu.png 与本文件同目录
"""

from __future__ import annotations

import base64
import os
from typing import Iterable, List, Optional

from service import convert_length

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))

# 图片加载（首次访问时缓存 base64，避免每次渲染重复读盘）
_IMG_CACHE: dict = {}


def _img_data_uri(filename: str) -> str:
    """读取 PNG/JPG，编码为 data: URI。失败返回空字符串，调用方回退纯文本。"""
    if filename in _IMG_CACHE:
        return _IMG_CACHE[filename]
    path = os.path.join(PLUGIN_DIR, filename)
    if not os.path.isfile(path):
        _IMG_CACHE[filename] = ""
        return ""
    try:
        with open(path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("ascii")
    except OSError:
        _IMG_CACHE[filename] = ""
        return ""
    # 根据扩展名推断 mime
    mime = "image/png"
    if filename.lower().endswith((".jpg", ".jpeg")):
        mime = "image/jpeg"
    data_uri = f"data:{mime};base64,{b64}"
    _IMG_CACHE[filename] = data_uri
    return data_uri


def _cow_for(gender: int) -> str:
    """gender 0/1 -> 母牛/公牛图文件名。"""
    return "muniu.png" if gender == 0 else "gongniu.png"


# ---------------------------------------------------------------------------
# 公共 CSS（与 roll-dice / gobang 风格保持一致）
# ---------------------------------------------------------------------------
_BASE_CSS = """
  * { margin:0; padding:0; box-sizing:border-box; font-family:'GenJyuuGothic','Microsoft YaHei','PingFang SC',sans-serif; }
  .card { background:rgba(255,255,255,0.6); border:1px solid rgba(255,255,255,0.6); border-radius:20px; width:100%; padding:22px; text-align:center; box-shadow:0 2px 10px rgba(0,0,0,0.1); }
  .title { font-size:26px; font-weight:bold; color:#6b3a18; margin-bottom:6px; }
  .subtitle { font-size:18px; color:#a35e2e; margin-bottom:12px; }
  .row { display:flex; align-items:center; justify-content:center; gap:18px; flex-wrap:wrap; }
  .row.single { gap:0; }
  .cow { width:200px; height:200px; object-fit:contain; }
  .cow.small { width:140px; height:140px; }
  .info { display:flex; flex-direction:column; align-items:flex-start; gap:6px; font-size:18px; color:#3a2a18; text-align:left; }
  .info b { color:#6b3a18; }
  .ranking { font-size:18px; color:#3a2a18; text-align:left; line-height:1.7; padding:6px 10px; }
  .ranking .top { font-weight:bold; color:#a35e2e; }
"""


def _wrap(inner: str) -> str:
    """把内部片段包成完整 .card 结构（含 <style>）。"""
    return f"<style>{_BASE_CSS}</style><div class=\"card\">{inner}</div>"


# ---------------------------------------------------------------------------
# 单牛卡片
# ---------------------------------------------------------------------------
def _single_pet_card(title: str, pet_name: str, gender: int, length_text: str,
                     status: str, owner_label: Optional[str] = None) -> str:
    cow_img = _img_data_uri(_cow_for(gender))
    gender_text = "女" if gender == 0 else "男"
    info_rows = [
        f'<div><b>名称：</b>{pet_name}</div>',
        f'<div><b>性别：</b>{gender_text}</div>',
        f'<div><b>长度：</b>{length_text}</div>',
        f'<div><b>状态：</b>{status}</div>',
    ]
    if owner_label:
        info_rows.append(f'<div><b>主人：</b>{owner_label}</div>')
    cow_html = (f'<img class="cow" src="{cow_img}">' if cow_img else '<div style="width:200px"></div>')
    inner = (
        f'<div class="title">{title}</div>'
        f'<div class="row">'
        f'  {cow_html}'
        f'  <div class="info">{"".join(info_rows)}</div>'
        f'</div>'
    )
    return _wrap(inner)


def show_my_pet_card(pet_name: str, gender: int, length_text: str,
                     status: str) -> str:
    return _single_pet_card("🐄 我的牛子", pet_name, gender, length_text, status)


def inspect_pet_card(pet_name: str, gender: int, length_text: str,
                     status: str, owner_label: str = "未知") -> str:
    return _single_pet_card("🐄 看看你的", pet_name, gender, length_text, status,
                            owner_label=owner_label)


def adopt_pet_card(pet_name: str, gender: int, length_text: str) -> str:
    cow_img = _img_data_uri(_cow_for(gender))
    gender_text = "女" if gender == 0 else "男"
    cow_html = (f'<img class="cow" src="{cow_img}">' if cow_img else '<div style="width:200px"></div>')
    inner = (
        '<div class="title">🐄 牛子系统</div>'
        '<div class="subtitle">恭喜！您获取到了一只牛子</div>'
        '<div class="row">'
        f'  {cow_html}'
        '  <div class="info">'
        f'    <div><b>名称：</b>{pet_name}</div>'
        f'    <div><b>性别：</b>{gender_text}</div>'
        f'    <div><b>长度：</b>{length_text}</div>'
        '  </div>'
        '</div>'
    )
    return _wrap(inner)


def show_spouse_card(pet_name: str, gender: int, length_text: str,
                     owner_label: str) -> str:
    return _single_pet_card("💞 我的对象", pet_name, gender, length_text,
                            status="配对中", owner_label=owner_label)


# ---------------------------------------------------------------------------
# 双牛卡片（比划 / 贴贴）
# ---------------------------------------------------------------------------
def battle_card(p1_name: str, p1_gender: int, p1_len: int,
                p2_name: str, p2_gender: int, p2_len: int,
                result_text: str) -> str:
    cow1 = _img_data_uri(_cow_for(p1_gender))
    cow2 = _img_data_uri(_cow_for(p2_gender))
    g1 = "女" if p1_gender == 0 else "男"
    g2 = "女" if p2_gender == 0 else "男"
    cow1_html = (f'<img class="cow small" src="{cow1}">' if cow1 else '<div style="width:140px"></div>')
    cow2_html = (f'<img class="cow small" src="{cow2}">' if cow2 else '<div style="width:140px"></div>')
    inner = (
        '<div class="title">⚔️ 牛子比划</div>'
        '<div class="row">'
        f'  <div style="display:flex;flex-direction:column;align-items:center;gap:4px;">'
        f'    {cow1_html}'
        f'    <div><b>{p1_name}</b>（{g1}）</div>'
        f'    <div>长度 {convert_len(p1_len)}</div>'
        '  </div>'
        '<div style="font-size:38px;color:#a35e2e;">VS</div>'
        f'  <div style="display:flex;flex-direction:column;align-items:center;gap:4px;">'
        f'    {cow2_html}'
        f'    <div><b>{p2_name}</b>（{g2}）</div>'
        f'    <div>长度 {convert_len(p2_len)}</div>'
        '  </div>'
        '</div>'
        f'<div style="margin-top:14px;font-size:20px;color:#3a2a18;">{result_text}</div>'
    )
    return _wrap(inner)


def cuddle_card(p1_name: str, p1_gender: int,
                p2_name: str, p2_gender: int,
                added: int, cooldown_minutes: int) -> str:
    cow1 = _img_data_uri(_cow_for(p1_gender))
    cow2 = _img_data_uri(_cow_for(p2_gender))
    cow1_html = (f'<img class="cow small" src="{cow1}">' if cow1 else '<div style="width:140px"></div>')
    cow2_html = (f'<img class="cow small" src="{cow2}">' if cow2 else '<div style="width:140px"></div>')
    inner = (
        '<div class="title">💕 贴贴成功！</div>'
        '<div class="row">'
        f'  <div style="display:flex;flex-direction:column;align-items:center;gap:4px;">'
        f'    {cow1_html}<div><b>{p1_name}</b></div>'
        '  </div>'
        '<div style="font-size:32px;">💞</div>'
        f'  <div style="display:flex;flex-direction:column;align-items:center;gap:4px;">'
        f'    {cow2_html}<div><b>{p2_name}</b></div>'
        '  </div>'
        '</div>'
        f'<div style="margin-top:14px;font-size:20px;color:#3a2a18;">'
        f'双方各增加 {added} cm，需要休息 {cooldown_minutes} 分钟</div>'
    )
    return _wrap(inner)


# ---------------------------------------------------------------------------
# 排行榜
# ---------------------------------------------------------------------------
def ranking_card(rows: List[dict]) -> str:
    """rows: [{rank, petName, health_text}, ...]"""
    if not rows:
        body = '<div class="ranking"><div>当前群里还没有任何牛子，快去领养一只吧！</div></div>'
    else:
        lines = []
        for r in rows:
            lines.append(
                f'<div><span class="top">Top {r["rank"]}</span> · '
                f'{r["petName"]} · <b>{r["health_text"]}</b></div>'
            )
        body = '<div class="ranking">' + "".join(lines) + '</div>'
    inner = '<div class="title">🏆 牛子光荣榜</div>' + body
    return _wrap(inner)


def ranking_text(rows: List[dict]) -> str:
    """排行榜纯文本兜底（与原版"普通文本"分支一致）。"""
    if not rows:
        return "当前群里还没有任何牛子，快去领养一只吧！"
    head = "--- 牛子光荣榜 ---\n"
    body = "\n".join(
        f"Top {r['rank']}: {r['petName']} - {r['health_text']}" for r in rows[:20]
    )
    return (head + body).strip()


# ---------------------------------------------------------------------------
# 单工具：把长度数字转成 convertLength 的字符串（service.convert_length 的本地别名）
# ---------------------------------------------------------------------------
def convert_len(n) -> str:
    return convert_length(n)


# ---------------------------------------------------------------------------
# 拼装「带玻璃外壳」的完整 HTML
# ---------------------------------------------------------------------------
def render_card_with_glass(inner_card_html: str, width: int = 560):
    """调用 ctx.gateway.glass_wrap(...)。返回完整 HTML。"""
    # 注意：此函数在 main.py 里调用 gateway.glass_wrap；
    # 这里只生成「内部片段」，方便 main.py 在可用时再套玻璃外壳。
    return inner_card_html


# 暴露给 main.py 的菜单图路径（直发图片用）
MENU_IMAGE_PATH = os.path.join(PLUGIN_DIR, "menu.png")