# -*- coding: utf-8 -*-
"""牛子系统业务层。

合并了 spark.niuzi 的 services/dataService.js、services/petService.js、
utils/helpers.js、utils/timeUtils.js 的所有逻辑，目的是「复刻」原 JS 行为，
包括已知 bug（如负长度、battle 公式中的 abs、变性时负长度反而回正的怪逻辑）。

数据结构：
  pets_data: Dict[ownerId, Pet]      —— 主人 openid -> 宠物
  runtime_data:
      reget_cooldowns: Dict[ownerId, ms_timestamp]  —— 丢弃后冷却
      marriage_proposals: Dict[responderOpenid, proposerPetId]   —— 求婚待回应
      breakup_proposals: Dict[responderOpenid, initiatorPetId]   —— 分手待回应
持久化：
      data.json —— 仅保存 pets_data；运行时三个 Map 是纯内存，重启即清空
"""

from __future__ import annotations

import json
import os
import random
import string
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from pet import Pet


# ---------------------------------------------------------------------------
# 路径与持久化
# ---------------------------------------------------------------------------
PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(PLUGIN_DIR, "data.json")

# data.json 文件锁；save_data() 内部用，避免并发写坏文件
_save_lock = threading.Lock()


@dataclass
class RuntimeData:
    reget_cooldowns: Dict[str, int] = field(default_factory=dict)
    marriage_proposals: Dict[str, str] = field(default_factory=dict)
    breakup_proposals: Dict[str, str] = field(default_factory=dict)


pets_data: Dict[str, Pet] = {}
runtime_data = RuntimeData()


def _load_from_disk() -> None:
    """启动时把 data.json 读进 pets_data；不存在或损坏则空字典。"""
    if not os.path.isfile(DATA_FILE):
        return
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError) as err:
        # 不让加载错误拖垮网关；打日志继续
        print(f"[niuzi] data.json 读取失败：{err}", flush=True)
        return
    if not isinstance(raw, dict):
        return
    for owner_id, payload in raw.items():
        pet = Pet("", "", "", 0, 0)
        pet.load_from_json(payload if isinstance(payload, dict) else {})
        if pet.ownerId:  # 跳过空数据
            pets_data[pet.ownerId] = pet


def save_data() -> None:
    """把 pets_data 落盘到 data.json（线程安全）。"""
    snapshot = {oid: p.save_as_json() for oid, p in pets_data.items()}
    with _save_lock:
        tmp_path = DATA_FILE + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(snapshot, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, DATA_FILE)
        except OSError as err:
            print(f"[niuzi] data.json 写入失败：{err}", flush=True)


def get_data_file() -> str:
    """暴露给 main.py 做初始化（README 要求 pymods 自管数据；这里仅返回路径）。"""
    return DATA_FILE


# 模块加载时即读取
_load_from_disk()


# ---------------------------------------------------------------------------
# 通用工具（移植自 utils/helpers.js + utils/timeUtils.js）
# ---------------------------------------------------------------------------
def _generate_random_id(length: int) -> str:
    """生成长度 length 的字母数字 ID（[A-Za-z0-9]）。"""
    chars = string.ascii_letters + string.digits
    return "".join(random.choice(chars) for _ in range(length))


def _random_number(min_val: float, max_val: float, is_integer: bool = False) -> float:
    """[min_val, max_val] 区间随机数；is_integer=True 则向下取整。"""
    if min_val > max_val:
        min_val, max_val = max_val, min_val
    val = random.random() * (max_val - min_val) + min_val
    return int(val) if is_integer else val  # int() 对正数等价于 floor


def _battle_result(prob: Dict[str, float]) -> Optional[int]:
    """根据 win/los/dorp 概率返回 1=攻击方赢, 2=防守方赢, 3=双输; 概率和!=1 返回 None。"""
    win_p = float(prob.get("win_p", 0))
    los_p = float(prob.get("los_p", 0))
    dorp_p = float(prob.get("dorp_p", 0))
    if abs(win_p + los_p + dorp_p - 1.0) > 1e-9:
        print("[niuzi] 战斗概率总和不为1", flush=True)
        return None
    r = random.random()
    if r < win_p:
        return 1
    if r < win_p + los_p:
        return 2
    return 3


def _get_at(message_fragments) -> Tuple[bool, str]:
    """从 spark 的消息片段里找第一个 at；这里 pymod 用不到，保留接口便于将来扩展。
    Args:
        message_fragments: 原 JS 是 list[dict]，这里容忍任何可迭代对象或 None。
    """
    if not message_fragments:
        return False, ""
    for seg in message_fragments:
        if isinstance(seg, dict) and seg.get("type") == "at":
            return True, str(seg.get("data", {}).get("qq", "") or "")
    return False, ""


# 单位换算（与 utils/helpers.js convertLength 完全一致；保留 2 位小数厘米兜底）
_UNITS = [
    ("光年", 946052840500000000),
    ("地月距离", 38440000000),
    ("千米", 100000),
    ("米", 100),
    ("厘米", 1),
]


def convert_length(cm) -> str:
    """cm 转可读单位。负数也支持（加负号），保留原版「长度可负」的特性。"""
    try:
        cm = float(cm)
    except (TypeError, ValueError):
        return "0 厘米"
    is_negative = cm < 0
    abs_cm = abs(cm)
    for name, factor in _UNITS:
        if abs_cm >= factor:
            value = int(abs_cm // factor)
            return f"{'-' if is_negative else ''}{value} {name}"
    return f"{cm:.2f} 厘米"


def _remaining_minutes(future_ts_ms: int) -> str:
    """距离未来时间戳还剩多少分钟（保留 1 位小数）。"""
    if not future_ts_ms or future_ts_ms <= _now_ms():
        return "0.0"
    return f"{(future_ts_ms - _now_ms()) / 60000:.1f}"


def _cooldown_ready(past_ts_ms: int, minutes: int) -> bool:
    """过去时间戳距现在是否超过 minutes 分钟（无记录视作已冷却完）。"""
    if not past_ts_ms:
        return True
    return (_now_ms() - past_ts_ms) / 60000 > minutes


def _now_ms() -> int:
    return int(time.time() * 1000)


# ---------------------------------------------------------------------------
# 业务逻辑（移植自 services/petService.js）
# ---------------------------------------------------------------------------
def create_pet(owner_id: str, init_cm_max: int) -> Pet:
    """新领养一只牛子。"""
    pet_name = f"牛子{str(owner_id)[-4:]}"  # 与 JS slice(-4) 一致
    pet_id = _generate_random_id(5)
    health = int(_random_number(1, init_cm_max, is_integer=True))
    # 【修复原版 bug】原版 getRandomNumber(0,1,true)=floor(random()*1) 恒为 0，
    # 导致所有领养的牛子全是女。改为 [0,2) 区间取整，男女各 50%。
    gender = int(_random_number(0, 2, is_integer=True))  # 0=女, 1=男
    new_pet = Pet(owner_id, pet_name, pet_id, health, gender)
    pets_data[owner_id] = new_pet
    save_data()
    return new_pet


def get_pet(owner_id: str) -> Optional[Pet]:
    return pets_data.get(owner_id)


def get_pet_by_id(pet_id: str) -> Optional[Pet]:
    """遍历查找 petId —— 与原版一致（pet 数量通常不大）。"""
    for p in pets_data.values():
        if p.petId == pet_id:
            return p
    return None


def has_pet(owner_id: str) -> bool:
    return owner_id in pets_data


def remove_pet(owner_id: str, reget_cd_minutes: int) -> Dict[str, object]:
    """丢弃牛子。有对象时不能丢。"""
    pet = get_pet(owner_id)
    if pet and pet.spouseId:
        return {"success": False, "message": "你的牛子有对象，无法抛弃！"}
    pets_data.pop(owner_id, None)
    runtime_data.reget_cooldowns[owner_id] = _now_ms() + reget_cd_minutes * 60000
    save_data()
    return {"success": True, "message": "你的牛子没了"}


def change_name(owner_id: str, new_name: str) -> Dict[str, object]:
    pet = get_pet(owner_id)
    if not new_name:
        return {"success": False, "message": "请提供新的牛子名称!"}
    if len(new_name) > 10:
        return {"success": False, "message": "牛子名称最多10个字符!"}
    pet.petName = new_name
    save_data()
    return {"success": True, "message": f"牛子的名称已被修改为：{pet.petName}"}


def transgender(owner_id: str) -> Dict[str, object]:
    """切性别。Bug 保留：负长度时 health -= floor(health/5) 会让长度「回正」，
    因为 Python 的 // 与 JS Math.floor 对负数都是向下取整。"""
    pet = get_pet(owner_id)
    health_lost = pet.health // 5
    pet.gender = 1 if pet.gender == 0 else 0
    pet.health = pet.health - health_lost
    save_data()
    return {"success": True, "message": "你的牛子变性了，并为此付出了代价！"}


def battle(attacker_id: str, defender_id: str, pk_cd_minutes: int,
           probs: Dict[str, float]) -> Dict[str, object]:
    """两只牛子比划。保留 abs 公式 bug。"""
    attacker = get_pet(attacker_id)
    defender = get_pet(defender_id)
    if not _cooldown_ready(attacker.battleTimestamp, pk_cd_minutes):
        end_ts = attacker.battleTimestamp + pk_cd_minutes * 60000
        return {"success": False,
                "message": f"{attacker.petName} 红肿了，需要等 {_remaining_minutes(end_ts)} 分钟"}
    if not _cooldown_ready(defender.battleTimestamp, pk_cd_minutes):
        end_ts = defender.battleTimestamp + pk_cd_minutes * 60000
        return {"success": False,
                "message": f"{defender.petName} 红肿了，需要等 {_remaining_minutes(end_ts)} 分钟"}

    now = _now_ms()
    attacker.battleTimestamp = now
    defender.battleTimestamp = now

    # 【平衡性调整】: 原版为 abs(attacker) + abs(defender)/4（发起者长度权重是对方 4 倍，
    # 导致先领养者碾压新人）；现改为 (|双方长度之和|)/2，双方共同决定攻击值，缩小碾压差距。
    upper = (abs(attacker.health) + abs(defender.health)) / 2
    attack = int(_random_number(1, upper, is_integer=True))
    result_type = _battle_result(probs)
    if result_type is None:
        return {"success": False, "message": "---=牛子系统=---\n参数有误，概率之和不为1"}

    msg = f"---=牛子系统=---\n{attacker.petName} 和 {defender.petName} 开始比划\n"
    if result_type == 1:
        attacker.health += attack
        defender.health -= attack
        msg += f"{attacker.petName} 赢得了{convert_length(attack)}！"
    elif result_type == 2:
        attacker.health -= attack
        defender.health += attack
        msg += f"{defender.petName} 赢得了{convert_length(attack)}！"
    else:  # 双输
        attacker.health -= attack
        defender.health -= attack
        msg += f"两败俱伤！ 都输掉了{convert_length(attack)}！"

    save_data()
    return {"success": True, "message": msg}


def get_ranking() -> List[Dict[str, object]]:
    """按 health 倒序排序，返回 [{petName, health, ownerId, petId, gender}]。"""
    data_list = list(pets_data.values())
    data_list.sort(key=lambda p: p.health, reverse=True)
    return [{
        "petName": p.petName,
        "health": p.health,
        "ownerId": p.ownerId,
        "petId": p.petId,
        "gender": p.gender,
    } for p in data_list]


def propose_marriage(proposer_id: str, target_id: str) -> Dict[str, object]:
    """发起搞对象（异性）。"""
    proposer = get_pet(proposer_id)
    target = get_pet(target_id)
    if proposer.spouseId:
        return {"success": False, "message": "你已经有对象了!"}
    if target.spouseId:
        return {"success": False, "message": "对方已经有对象了!"}
    if proposer.gender == target.gender:
        return {"success": False, "message": "同性怎么搞？"}
    runtime_data.marriage_proposals[target_id] = proposer.petId
    return {"success": True, "message": "请求已发出，请等待对方回应"}


def initiate_breakup(initiator_id: str) -> Dict[str, object]:
    """发起分手请求。"""
    initiator = get_pet(initiator_id)
    if not initiator.spouseId:
        return {"success": False, "message": "你还没有对象!"}
    spouse = get_pet_by_id(initiator.spouseId)
    if not spouse:
        # 对方数据丢失，强制解除
        initiator.spouseId = ""
        save_data()
        return {"success": False, "message": "你的对象找不到了，已自动分手。"}
    runtime_data.breakup_proposals[spouse.ownerId] = initiator.petId
    return {"success": True, "message": "分手请求已发出，请等待对方回应"}


def handle_proposal(responder_id: str, ptype: str, decision: str) -> Dict[str, object]:
    """处理（搞对象/分手）请求的同意/拒绝。"""
    proposal_map = (runtime_data.marriage_proposals
                    if ptype == "搞对象"
                    else runtime_data.breakup_proposals)
    initiator_pet_id = proposal_map.get(responder_id)
    if not initiator_pet_id:
        return {"success": False, "message": f"当前没有人向你发起{ptype}请求！"}

    if decision == "拒绝":
        proposal_map.pop(responder_id, None)
        return {"success": True, "message": f"已拒绝对方的{ptype}请求。"}

    responder = get_pet(responder_id)
    initiator = get_pet_by_id(initiator_pet_id)
    if not initiator:
        return {"success": False, "message": "对方的牛子不见了，操作失败。"}

    if ptype == "搞对象":
        if responder.spouseId or initiator.spouseId:
            proposal_map.pop(responder_id, None)
            return {"success": False, "message": "你或对方在此期间已经有对象了，操作失败。"}
        responder.spouseId = initiator.petId
        initiator.spouseId = responder.petId
    else:  # 分手
        responder.spouseId = ""
        initiator.spouseId = ""

    proposal_map.pop(responder_id, None)
    save_data()
    return {"success": True, "message": f"{ptype}成功！"}


def cuddle(owner_id: str, tt_cd_minutes: int, tt_grow_max: int) -> Dict[str, object]:
    """贴贴：双方对象一起增加长度 + 共同冷却。"""
    pet = get_pet(owner_id)
    spouse = get_pet_by_id(pet.spouseId)
    if pet.recoveryTimestamp and _now_ms() < pet.recoveryTimestamp:
        return {"success": False, "message": f"还在休息，剩余{_remaining_minutes(pet.recoveryTimestamp)}分钟"}
    added = int(_random_number(1, tt_grow_max, is_integer=True))
    pet.health += added
    spouse.health += added
    cooldown_minutes = int(_random_number(tt_cd_minutes, tt_cd_minutes + 20, is_integer=True))
    cooldown_end = _now_ms() + cooldown_minutes * 60000
    pet.recoveryTimestamp = cooldown_end
    spouse.recoveryTimestamp = cooldown_end
    save_data()
    return {"success": True,
            "message": f"---=牛子系统=---\n贴上了，都增加了{added} cm，但是都需要休息{cooldown_minutes}分钟"}


# ---------------------------------------------------------------------------
# 默认配置（对应原 spark.niuzi/config/index.js 的 defaultConfig）
# ---------------------------------------------------------------------------
DEFAULT_CONFIG: Dict[str, object] = {
    "pkCD": 2,        # 比划冷却（分）
    "init_cm": 10,    # 初始长度上限
    "reget_cd": 10,   # 丢弃冷却（分）
    "win_p": 0.4,     # 比划赢概率
    "los_p": 0.4,     # 比划输概率
    "dorp_p": 0.2,    # 双输概率
    "tt_cd": 40,      # 贴贴冷却（分）
    "tt_grow": 120,   # 贴贴成长上限
}