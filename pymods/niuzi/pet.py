# -*- coding: utf-8 -*-
"""Pet 数据类。

完全对齐 spark.niuzi/models/Pet.js 的字段与序列化方式，便于把原 JS 数据
（如果将来要兼容迁移）直接 loadFromJson。gender: 0=女, 1=男。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict


@dataclass
class Pet:
    ownerId: str
    petName: str
    petId: str
    health: int
    gender: int
    battleTimestamp: int = 0          # 上次比划时间戳（毫秒）
    recoveryTimestamp: int = 0        # 贴贴恢复时间戳（毫秒）
    spouseId: str = ""                # 对象 petId；空字符串等价 null

    def load_from_json(self, data: Dict[str, Any]) -> None:
        """从 JSON 字典装载字段（兼容 null/spouseId 不存在）。"""
        self.ownerId = str(data.get("ownerId", ""))
        self.petName = str(data.get("petName", ""))
        self.petId = str(data.get("petId", ""))
        self.health = int(data.get("health", 0) or 0)
        self.battleTimestamp = int(data.get("battleTimestamp", 0) or 0)
        self.recoveryTimestamp = int(data.get("recoveryTimestamp", 0) or 0)
        self.gender = int(data.get("gender", 0) or 0)
        self.spouseId = str(data.get("spouseId") or "") or ""

    def save_as_json(self) -> Dict[str, Any]:
        """序列化为 JSON 字典（保留原 JS 字段命名，便于阅读）。"""
        return {
            "ownerId": self.ownerId,
            "petName": self.petName,
            "petId": self.petId,
            "health": self.health,
            "battleTimestamp": self.battleTimestamp,
            "recoveryTimestamp": self.recoveryTimestamp,
            "gender": self.gender,
            "spouseId": self.spouseId,
        }