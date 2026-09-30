"""升级状态机的本地文件持久化。

状态文件是 JSON，采用“临时文件 + 原子 rename + fsync”写入。
状态只记录“我们认为进行到哪一步”，重启恢复时必须拿设备上的真实内容
（暂存区真实长度、槽内固件摘要、试启动标记）来校正，不能仅凭标记下结论。
"""

from __future__ import annotations

import enum
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .device import atomic_write_bytes, read_json


class Stage(str, enum.Enum):
    IDLE = "idle"                    # 没有进行中的升级
    DOWNLOADING = "downloading"      # 分段写入暂存区中
    PENDING_TRIAL = "pending_trial"  # 已装入非活动槽，等待试启动确认
    CONFIRMED = "confirmed"          # 新版本确认可用（终态）
    ROLLED_BACK = "rolled_back"      # 升级失败并已回退（终态）


TERMINAL_STAGES = frozenset({Stage.CONFIRMED, Stage.ROLLED_BACK})


@dataclass
class UpgradeState:
    stage: Stage = Stage.IDLE
    package_id: str | None = None
    version: str | None = None
    target_slot: str | None = None
    payload_size: int = 0
    payload_sha256: str | None = None
    data_format: int = 1         # 目标固件要求的数据格式版本
    data_migrated: bool = False  # 隔离副本是否已完成迁移
    received: int = 0            # 仅作缓存；恢复时以磁盘真实长度覆盖
    result: str | None = None    # 终态结论说明
    attempts: int = 0
    updated_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["stage"] = self.stage.value
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "UpgradeState":
        try:
            stage = Stage(data["stage"])
        except (KeyError, ValueError) as exc:
            raise ValueError(f"状态文件中的阶段非法: {data.get('stage')!r}") from exc
        fields = {f for f in cls.__dataclass_fields__ if f != "stage"}
        return cls(stage=stage, **{k: data.get(k) for k in fields})

    def touch(self) -> None:
        self.updated_at = time.time()


class StateStore:
    """管理 root/state/upgrade_state.json 的原子读写。"""

    def __init__(self, root: str | Path):
        self.path = Path(root) / "state" / "upgrade_state.json"

    def exists(self) -> bool:
        return self.path.exists()

    def load(self) -> UpgradeState:
        if not self.path.exists():
            return UpgradeState()
        try:
            data = read_json(self.path)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"状态文件损坏: {self.path}: {exc}") from exc
        return UpgradeState.from_dict(data)

    def save(self, state: UpgradeState) -> None:
        state.touch()
        payload = json.dumps(state.to_dict(), ensure_ascii=False, indent=2).encode("utf-8")
        atomic_write_bytes(self.path, payload)
