"""虚拟 bootloader：选择活动槽、模拟启动健康检查、确认与回退。

状态文件 bootctrl.json 模拟真实设备的 bootloader 控制分区：
- active: 当前启动槽
- slots[X].status: empty / confirmed / trial / booted / bad
- slots[X].boot_attempts: 试启动已用次数
"""
from __future__ import annotations

from dataclasses import dataclass

from .layout import Layout
from .persistence import atomic_write_json, read_json

MAX_BOOT_ATTEMPTS = 3


@dataclass
class BootReport:
    slot: str
    version: str | None
    healthy: bool
    status_after: str  # confirmed / trial / bad


class BootControl:
    def __init__(self, layout: Layout):
        self.layout = layout
        self.layout.ensure()
        state = read_json(layout.boot_file)
        self.state = state or {
            "active": None,
            "boot_count": 0,
            "slots": {
                "A": {"status": "empty", "boot_attempts": 0},
                "B": {"status": "empty", "boot_attempts": 0},
            },
        }

    def save(self) -> None:
        atomic_write_json(self.layout.boot_file, self.state)

    def slot_status(self, slot: str) -> dict:
        return self.state["slots"][slot]

    def mark_for_trial(self, slot: str) -> None:
        entry = self.state["slots"][slot]
        entry["status"] = "trial"
        entry["boot_attempts"] = 0
        self.state["active"] = slot
        self.save()

    @staticmethod
    def _inspect_firmware(image_path) -> tuple[bool, str | None]:
        """读取虚拟固件头部：EDGEFW-OK 表示健康，EDGEFW-BAD 表示启动失败。"""
        try:
            head = image_path.read_bytes()[:64]
        except OSError:
            return False, None
        healthy = head.startswith(b"EDGEFW-OK")
        version = None
        for line in head.split(b"\n"):
            if line.startswith(b"version="):
                version = line[len(b"version="):].decode("utf-8", "replace")
        return healthy, version

    def boot(self) -> BootReport:
        """模拟一次上电启动并返回启动报告，不修改 confirmed 状态。"""
        slot = self.state["active"]
        entry = self.state["slots"][slot]
        self.state["boot_count"] += 1
        image_path = self.layout.slot_image(slot)
        healthy, version = self._inspect_firmware(image_path)

        if entry["status"] == "trial":
            entry["boot_attempts"] += 1
            if healthy:
                entry["status"] = "booted"  # 已跑起来，等升级器确认
            elif entry["boot_attempts"] >= MAX_BOOT_ATTEMPTS:
                entry["status"] = "bad"
        elif not healthy:
            entry["status"] = "bad"
        self.save()
        return BootReport(slot, version, healthy, entry["status"])

    def commit(self, slot: str) -> None:
        self.state["slots"][slot]["status"] = "confirmed"
        self.state["active"] = slot
        self.save()

    def rollback(self, fallback: str) -> BootReport:
        """把失败槽标记为 bad，切回旧槽启动；旧槽内容始终保留。"""
        failed = self.state["active"]
        self.state["slots"][failed]["status"] = "bad"
        self.state["active"] = fallback
        self.save()
        return self.boot()

    def active_version(self) -> str | None:
        slot = self.state["active"]
        meta = read_json(self.layout.slot_meta(slot)) if slot else None
        return meta.get("version") if meta else None
