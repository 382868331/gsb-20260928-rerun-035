from __future__ import annotations

from pathlib import Path


SLOT_NAMES = ("A", "B")


class Layout:
    """设备本地目录布局，全部状态都落盘在 root 下。"""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.state_dir = self.root / "state"
        self.slots_dir = self.root / "slots"
        self.device_file = self.root / "device.json"
        self.journal_file = self.state_dir / "updater_state.json"
        self.boot_file = self.state_dir / "bootctrl.json"

    def slot_dir(self, slot: str) -> Path:
        return self.slots_dir / f"slot_{slot.lower()}"

    def slot_image(self, slot: str) -> Path:
        return self.slot_dir(slot) / "image.bin"

    def slot_meta(self, slot: str) -> Path:
        return self.slot_dir(slot) / "slot_meta.json"

    def ensure(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.slots_dir.mkdir(parents=True, exist_ok=True)
        for slot in SLOT_NAMES:
            self.slot_dir(slot).mkdir(parents=True, exist_ok=True)
