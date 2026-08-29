# -*- coding: utf-8 -*-
"""示例 Python 子插件：掷骰子（图片版，液态玻璃卡片）。

演示 pymods 子插件「发图 + 玻璃卡片」能力：
- 拼好卡片内部 HTML（含自身 <style> 与 .card 节点）；
- 用网关的 ctx.gateway.glass_wrap(inner, width=480) 套上「随机背景 + 液态玻璃」外壳，
  得到与「查服/查玩家」同款的毛玻璃卡片，并自动注入中文字体；
- 渲染器不可用时（如未安装 Edge）自动回退为纯文本。

发图接口见 pymods/README.md：
- await ctx.gateway.send_card(html, ctx.msg_id)  —— 渲染+上传+发图，成功返回 True
- 自行发图后返回 ctx.IMAGE_SENT  —— 告知网关不要再重复发文本
- ctx.gateway.glass_wrap(inner_html, width)  —— 生成液态玻璃卡片外壳（随机背景）
"""

import random
import re

help = "掷骰子 / 掷骰子 <数量> / rd <数量>：随机掷一个 1~N 的数字（带图片结果）"

# 注意：这是「卡片内部」片段 —— 不含 <!DOCTYPE>/<html>/<body>/<head>，
# 字体与玻璃外壳由 ctx.gateway.glass_wrap 注入。花括号用 replace 避免被 str.format 误解析。
_CARD_INNER = """<style>
  .card { background:rgba(255,255,255,0.6); border:1px solid rgba(255,255,255,0.6); border-radius:20px; width:100%; padding:26px; text-align:center; box-shadow:0 2px 10px rgba(0,0,0,0.1); }
  .title { font-size:26px; color:#1f5d33; font-weight:bold; }
  .dice { font-size:96px; margin:8px 0 4px; }
  .result { font-size:72px; font-weight:bold; color:#1f5d33; line-height:1; }
  .range { font-size:22px; color:#33473b; margin-top:10px; }
  .who { font-size:20px; color:#52615a; margin-top:8px; }
</style>
<div class="card">
  <div class="title">🎲 掷骰子</div>
  <div class="dice">🎲</div>
  <div class="result">{result}</div>
  <div class="range">范围 1 ~ {n}</div>
  <div class="who">由 {who} 掷出</div>
</div>"""


# 支持「掷骰子 6」「掷骰子 (6)」「rd 10」「roll 10」等带数量写法，括号可选
_DICE_RE = re.compile(r"^(?:掷骰子|roll|rd)\s*[(（]?\s*(\d+)\s*[)）]?")


def _parse(text: str):
    """解析指令，返回骰子面数 n（int）或 None（不处理）。
    支持：掷骰子 / roll / rd（默认 6 面），以及带数量 掷骰子 10 / rd 10 / 掷骰子(10)。"""
    t = text.strip()
    low = t.lower()
    if low == "掷骰子" or low == "roll" or low == "rd":
        return 6
    m = _DICE_RE.match(low)
    if m:
        n = int(m.group(1))
        if n < 1:
            n = 1
        return n
    return None


async def handle_message(ctx):
    n = _parse(ctx.content)
    if n is None:
        return None
    n = min(n, 100000)
    result = random.randint(1, n)

    # 渲染器可用 → 生成液态玻璃卡片并发图
    if ctx.gateway.card_renderer is not None:
        inner = (
            _CARD_INNER
            .replace("{result}", str(result))
            .replace("{n}", str(n))
            .replace("{who}", ctx.sender_name)
        )
        html = ctx.gateway.glass_wrap(inner, width=480)
        ok = await ctx.gateway.send_card(html, ctx.msg_id)
        if ok:
            return ctx.IMAGE_SENT

    # 回退：纯文本
    return f"🎲 {ctx.sender_name} 掷出了 {result}（范围 1~{n}）"
