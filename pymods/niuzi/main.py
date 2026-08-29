# -*- coding: utf-8 -*-
"""Python 子插件：我的牛子（QQMCBridge 版，复刻 spark.niuzi by lition）。

指令全集（与原 spark.niuzi 完全对齐，保留所有特性和已知 bug）：
  领养牛子                新领养一只牛子
  改牛子名 <新名>         改名（≤10 字）
  比划比划 @某人 / 🔒     两人比划，按 win/los/dorp 概率转移长度
  我的牛子                查看自己的牛子
  看看你的 [@某人|QQ号]   查看他人的牛子（可省略参数，默认查看自己）
  丢弃牛子                丢弃（带冷却；有对象时不能丢）
  牛子菜单                发送菜单图（menu.png）
  牛子榜                  牛子排行榜（前 20）
  牛子变性                切性别，付出长度代价
  搞对象 @某人            异性才能发起，pending 状态等对方「处理请求」
  处理请求 <搞对象|分手> <同意|拒绝>
  我要分手                主动发起分手
  我的对象                查看对象信息
  贴贴 / 贴贴！           双方对象一起增加长度（共享冷却）

目标识别：
  pymod 的 ctx.content 仍保留 <@member_openid> 形式的成员 @ 标记
  （与 group-mute 等插件一致），用 <@!?([A-Za-z0-9_]+)> 正则提取第一个被 @ 的 openid。

卡片渲染：
  - 渲染器可用：把卡片内部 HTML 经 ctx.gateway.glass_wrap 套上液态玻璃外壳再 send_card；
    成功发图后返回 ctx.IMAGE_SENT，网关不再重复发文本
  - 渲染器不可用：返回纯文本（带牛图仅是装饰，没有也能跑）
"""

from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional

import cards
import service

# 模块级 help（在 manifest 之外补全一份，方便单测）
help = (
    "我的牛子: 领养牛子 / 改牛子名 <新名> / 比划比划 @某人 / 我的牛子 / "
    "看看你的 [@某人|QQ号] / 丢弃牛子 / 牛子菜单 / 牛子榜 / 牛子变性 / "
    "搞对象 @某人 / 处理请求 <搞对象|分手> <同意|拒绝> / 我要分手 / 我的对象 / 贴贴"
)

# QQ 官方机器人 @-mention 形态：<@!openid> 或 <@openid>
MENTION_PATTERN = re.compile(r"<@!?([A-Za-z0-9_]+)>")


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------
def _now_ms() -> int:
    return int(time.time() * 1000)


def _remaining_minutes(future_ts_ms: int) -> str:
    """距离未来时间戳还剩多少分钟（保留 1 位小数）。"""
    if not future_ts_ms or future_ts_ms <= _now_ms():
        return "0.0"
    return f"{(future_ts_ms - _now_ms()) / 60000:.1f}"


def _cooldown_ready(past_ts_ms: int, minutes: int) -> bool:
    if not past_ts_ms:
        return True
    return (_now_ms() - past_ts_ms) / 60000 > minutes


def _parse_target_id(content: str) -> Optional[str]:
    """从消息文本中解析 @-target openid；找不到返回 None。"""
    m = MENTION_PATTERN.search(content)
    if m:
        return m.group(1)
    return None


def _owner_label(ctx) -> str:
    """展示用的「主人」标识：优先昵称，回退 openid 末 4 位。"""
    if ctx.sender_name:
        return ctx.sender_name
    return f"用户{(ctx.sender_openid or '')[-4:]}"


# ---------------------------------------------------------------------------
# 异步发图封装（玻璃卡片）
# ---------------------------------------------------------------------------
async def _send_card(ctx, inner_html: str, width: int = 560):
    """渲染并发送玻璃卡片。成功返回 True，渲染器不可用/失败返回 False。"""
    if getattr(ctx.gateway, "card_renderer", None) is None:
        return False
    try:
        html = ctx.gateway.glass_wrap(inner_html, width=width)
        return await ctx.gateway.send_card(html, ctx.msg_id)
    except Exception as err:  # noqa: BLE001  -- 网关已记录日志，这里只兜底
        print(f"[niuzi] 卡片发送失败，回退文本：{err}", flush=True)
        return False


# ---------------------------------------------------------------------------
# 指令处理函数
# ---------------------------------------------------------------------------
async def _handle_adopt(ctx, cfg):
    sid = ctx.sender_openid
    if not sid:
        return "无法识别你的 OpenID"
    if service.has_pet(sid):
        return "你已经有一只牛子了!"
    cd_end = service.runtime_data.reget_cooldowns.get(sid)
    if cd_end and _now_ms() < cd_end:
        return f"正在冷却，请在 {_remaining_minutes(cd_end)} 分钟后再来"

    # 用配置覆盖默认 init_cm
    init_cm = int(cfg.get("init_cm", service.DEFAULT_CONFIG["init_cm"]))
    pet = service.create_pet(sid, init_cm)
    gender_text = "女" if pet.gender == 0 else "男"
    # 卡片（带图）优先
    inner = cards.adopt_pet_card(pet.petName, pet.gender, service.convert_length(pet.health))
    if await _send_card(ctx, inner, width=520):
        return ctx.IMAGE_SENT
    return (
        f"---=牛子系统=---\n您获取到了一只牛子\n长度：{service.convert_length(pet.health)}\n"
        f"性别：{gender_text}"
    )


async def _handle_change_name(ctx, args, cfg):
    sid = ctx.sender_openid
    if not service.has_pet(sid):
        return "你还没有一只牛子!"
    new_name = args[0] if args else ""
    result = service.change_name(sid, new_name)
    return result["message"]


async def _handle_battle(ctx, cfg):
    sid = ctx.sender_openid
    if not service.has_pet(sid):
        return "你还没有一只牛子!"
    target_id = _parse_target_id(ctx.content)
    if not target_id:
        return "请选择要比划的目标"
    if sid == target_id:
        return "不能对自己🦌棍子!"
    if not service.has_pet(target_id):
        return "对方还没有一只牛子!"

    pk_cd = int(cfg.get("pkCD", service.DEFAULT_CONFIG["pkCD"]))
    probs = {
        "win_p": float(cfg.get("win_p", service.DEFAULT_CONFIG["win_p"])),
        "los_p": float(cfg.get("los_p", service.DEFAULT_CONFIG["los_p"])),
        "dorp_p": float(cfg.get("dorp_p", service.DEFAULT_CONFIG["dorp_p"])),
    }
    # 快照 PK 前的数据，渲染用
    pet_a = service.get_pet(sid)
    pet_b = service.get_pet(target_id)
    a_name, a_gender, a_len = pet_a.petName, pet_a.gender, pet_a.health
    b_name, b_gender, b_len = pet_b.petName, pet_b.gender, pet_b.health
    result = service.battle(sid, target_id, pk_cd, probs)
    if not result["success"]:
        return result["message"]

    # PK 后重新取最新长度展示
    pet_a = service.get_pet(sid)
    pet_b = service.get_pet(target_id)
    inner = cards.battle_card(
        a_name, a_gender, a_len,
        b_name, b_gender, b_len,
        result_text=result["message"].split("\n", 1)[-1],
    )
    if await _send_card(ctx, inner, width=620):
        return ctx.IMAGE_SENT
    return result["message"]


async def _handle_show_my(ctx, cfg):
    sid = ctx.sender_openid
    pet = service.get_pet(sid)
    if not pet:
        return "你还没有一只牛子!"
    pk_cd = int(cfg.get("pkCD", service.DEFAULT_CONFIG["pkCD"]))
    is_ready = _cooldown_ready(pet.battleTimestamp, pk_cd)
    cd_end = pet.battleTimestamp + pk_cd * 60000 if pet.battleTimestamp else 0
    status = "积极向上" if is_ready else f"红肿(剩余{_remaining_minutes(cd_end)}分钟)"
    inner = cards.show_my_pet_card(pet.petName, pet.gender,
                                   service.convert_length(pet.health), status)
    if await _send_card(ctx, inner, width=620):
        return ctx.IMAGE_SENT
    gender_text = "女" if pet.gender == 0 else "男"
    return (
        f"---=牛子系统=---\n您的牛子：{pet.petName}\n性别：{gender_text}\n"
        f"长度：{service.convert_length(pet.health)}\n状态：{status}"
    )


async def _handle_remove(ctx, cfg):
    sid = ctx.sender_openid
    if not service.has_pet(sid):
        return "你还没有一只牛子!"
    reget_cd = int(cfg.get("reget_cd", service.DEFAULT_CONFIG["reget_cd"]))
    result = service.remove_pet(sid, reget_cd)
    return result["message"]


async def _handle_inspect(ctx, args, cfg):
    target_info = _parse_target_id(ctx.content)
    target_id = target_info if target_info else (args[0] if args else None)
    if not target_id:
        return "请指定要查看的目标。"
    pet = service.get_pet(target_id)
    if not pet:
        return "对方还没有一只牛子!"
    pk_cd = int(cfg.get("pkCD", service.DEFAULT_CONFIG["pkCD"]))
    is_ready = _cooldown_ready(pet.battleTimestamp, pk_cd)
    cd_end = pet.battleTimestamp + pk_cd * 60000 if pet.battleTimestamp else 0
    status = "积极向上" if is_ready else f"红肿(剩余{_remaining_minutes(cd_end)}分钟)"
    inner = cards.inspect_pet_card(pet.petName, pet.gender,
                                   service.convert_length(pet.health), status,
                                   owner_label=f"{(pet.ownerId or '')[-4:]}")
    if await _send_card(ctx, inner, width=620):
        return ctx.IMAGE_SENT
    gender_text = "女" if pet.gender == 0 else "男"
    return (
        f"---=牛子系统=---\n牛子：{pet.petName}\n性别：{gender_text}\n"
        f"长度：{service.convert_length(pet.health)}\n状态：{status}"
    )


async def _handle_menu(ctx, cfg=None):
    """菜单：直接发图。发图失败回退纯文本说明。"""
    import os
    if os.path.isfile(cards.MENU_IMAGE_PATH):
        try:
            await ctx.reply({"type": "image", "url": cards.MENU_IMAGE_PATH})
            return ctx.IMAGE_SENT
        except Exception as err:  # noqa: BLE001
            print(f"[niuzi] 菜单图发送失败：{err}", flush=True)
    return (
        "---=牛子系统菜单=---\n"
        "领养牛子 / 改牛子名 <新名> / 比划比划 @某人 / 我的牛子\n"
        "看看你的 [@某人|QQ号] / 丢弃牛子 / 牛子榜 / 牛子变性\n"
        "搞对象 @某人 / 处理请求 <搞对象|分手> <同意|拒绝>\n"
        "我要分手 / 我的对象 / 贴贴"
    )


async def _handle_ranking(ctx, cfg):
    rows_data = service.get_ranking()
    if not rows_data:
        return "当前群里还没有任何牛子，快去领养一只吧！"
    rows = []
    for idx, item in enumerate(rows_data[:20], start=1):
        rows.append({
            "rank": idx,
            "petName": item["petName"],
            "health_text": service.convert_length(item["health"]),
        })
    inner = cards.ranking_card(rows)
    if await _send_card(ctx, inner, width=520):
        return ctx.IMAGE_SENT
    return cards.ranking_text(rows)


async def _handle_transgender(ctx, cfg):
    sid = ctx.sender_openid
    if not service.has_pet(sid):
        return "你还没有一只牛子!"
    pet = service.get_pet(sid)
    if pet is None:
        return "你还没有一只牛子!"
    # 变性：service.transgender 直接操作 pet，保留 bug 行为
    service.transgender(sid)
    return "你的牛子变性了，并为此付出了代价！"


async def _handle_propose(ctx, cfg):
    sid = ctx.sender_openid
    if not service.has_pet(sid):
        return "你还没有一只牛子!"
    target_id = _parse_target_id(ctx.content)
    if not target_id:
        return "请选择要搞对象的目标"
    if sid == target_id:
        return "不能和自己搞!"
    if not service.has_pet(target_id):
        return "对方还没有一只牛子!"
    result = service.propose_marriage(sid, target_id)
    return result["message"]


async def _handle_process(ctx, args, cfg):
    sid = ctx.sender_openid
    if not service.has_pet(sid):
        return "你还没有一只牛子!"
    if len(args) < 2:
        return "参数不足或有误，正确格式：处理请求 [搞对象/分手] [同意/拒绝]"
    ptype, decision = args[0], args[1]
    if ptype not in ("搞对象", "分手") or decision not in ("同意", "拒绝"):
        return "参数不足或有误，正确格式：处理请求 [搞对象/分手] [同意/拒绝]"
    result = service.handle_proposal(sid, ptype, decision)
    return result["message"]


async def _handle_breakup(ctx, cfg):
    sid = ctx.sender_openid
    if not service.has_pet(sid):
        return "你还没有一只牛子!"
    result = service.initiate_breakup(sid)
    return result["message"]


async def _handle_show_spouse(ctx, cfg):
    sid = ctx.sender_openid
    pet = service.get_pet(sid)
    if not pet:
        return "你还没有一只牛子!"
    if not pet.spouseId:
        return "你还没有对象!"
    spouse = service.get_pet_by_id(pet.spouseId)
    if not spouse:
        # 冗余清理
        pet.spouseId = ""
        service.save_data()
        return "你的对象神秘失踪了，已自动恢复单身。"
    inner = cards.show_spouse_card(spouse.petName, spouse.gender,
                                   service.convert_length(spouse.health),
                                   owner_label=f"{(spouse.ownerId or '')[-4:]}")
    if await _send_card(ctx, inner, width=620):
        return ctx.IMAGE_SENT
    gender_text = "女" if spouse.gender == 0 else "男"
    return (
        f"---=我的对象=---\n牛子：{spouse.petName}\n性别：{gender_text}\n"
        f"长度：{service.convert_length(spouse.health)}\n主人：{spouse.ownerId}"
    )


async def _handle_cuddle(ctx, cfg):
    sid = ctx.sender_openid
    pet = service.get_pet(sid)
    if not pet:
        return "你还没有一只牛子!"
    if not pet.spouseId:
        return "你还没有对象，不能贴贴！"
    tt_cd = int(cfg.get("tt_cd", service.DEFAULT_CONFIG["tt_cd"]))
    tt_grow = int(cfg.get("tt_grow", service.DEFAULT_CONFIG["tt_grow"]))
    # 预估本次成长值与冷却用于渲染（与 service.cuddle 内同步生成同一随机数序列是巧合，
    # 这里为简化只展示增量区间，不再注入随机种子；最终消息以 service 返回为准）
    result = service.cuddle(sid, tt_cd, tt_grow)
    if not result["success"]:
        return result["message"]
    # 取出 added 与 cooldown 用于卡片
    added_match = re.search(r"增加了(\d+)\s*cm", result["message"])
    cd_match = re.search(r"休息(\d+)分钟", result["message"])
    added = int(added_match.group(1)) if added_match else 0
    cooldown_minutes = int(cd_match.group(1)) if cd_match else tt_cd

    spouse = service.get_pet_by_id(pet.spouseId)
    if not spouse:
        return result["message"]
    inner = cards.cuddle_card(pet.petName, pet.gender,
                             spouse.petName, spouse.gender,
                             added, cooldown_minutes)
    if await _send_card(ctx, inner, width=620):
        return ctx.IMAGE_SENT
    return result["message"]


# ---------------------------------------------------------------------------
# 指令注册表（与原 spark.niuzi 完全对齐）
# ---------------------------------------------------------------------------
COMMANDS = {
    "领养牛子": _handle_adopt,
    "改牛子名": _handle_change_name,
    "比划比划": _handle_battle,
    "🔒": _handle_battle,
    "我的牛子": _handle_show_my,
    "丢弃牛子": _handle_remove,
    "看看你的": _handle_inspect,
    "牛子菜单": _handle_menu,
    "牛子榜": _handle_ranking,
    "牛子变性": _handle_transgender,
    "搞对象": _handle_propose,
    "处理请求": _handle_process,
    "我要分手": _handle_breakup,
    "我的对象": _handle_show_spouse,
    "贴贴": _handle_cuddle,
    "贴贴！": _handle_cuddle,
}


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
async def handle_message(ctx):
    """每条群消息都进来一次；首 token 不在指令表 → 返回 None，不消费消息。"""
    text = (ctx.content or "").strip()
    if not text:
        return None
    parts = text.split()
    if not parts:
        return None
    cmd = parts[0]
    args = parts[1:]
    handler = COMMANDS.get(cmd)
    if handler is None:
        return None

    # 读取本插件目录下的 config.json（如果存在），与根 config.json 走网关的 ctx.config
    cfg = dict(service.DEFAULT_CONFIG)
    try:
        # ctx.config 是「根 config.json 打底 + 本插件目录 config.json 覆盖」
        # 但这里我们只关心牛子系统自己的几项配置；ctx.config 是 dict 类型
        if isinstance(ctx.config, dict):
            for key in service.DEFAULT_CONFIG.keys():
                if key in ctx.config:
                    cfg[key] = ctx.config[key]
    except Exception as err:  # noqa: BLE001
        print(f"[niuzi] 读取配置失败：{err}", flush=True)

    try:
        # 大多数 handler 接收 (ctx, args, cfg)；_handle_menu/_handle_battle 只接收 (ctx[, cfg])
        # 用 inspect 简单分发
        import inspect
        sig = inspect.signature(handler)
        params = list(sig.parameters.keys())
        if "args" in params:
            return await handler(ctx, args, cfg)
        return await handler(ctx, cfg)
    except Exception as err:  # noqa: BLE001
        print(f"[niuzi] 处理指令 [{cmd}] 时异常：{err}", flush=True)
        return f"呜，牛子系统好像出错了... ({err})"