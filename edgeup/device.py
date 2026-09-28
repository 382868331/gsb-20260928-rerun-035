from __future__ import annotations

from .layout import Layout
from .persistence import atomic_write_json, read_json


def provision(
    root: str,
    *,
    model: str = "edge-x1",
    serial: str = "VIRT-0001",
    factory_version: str = "1.0.0",
    factory_image: bytes | None = None,
) -> None:
    """虚拟设备出厂：写入设备信息，把出厂固件完整写入 A 槽并设为已确认。"""
    from .package import build_firmware
    from .slots import SlotManager
    from .bootctl import BootControl

    layout = Layout(root)
    layout.ensure()
    atomic_write_json(layout.device_file, {
        "model": model,
        "serial": serial,
    })

    image = factory_image if factory_image is not None else build_firmware(factory_version)
    slots = SlotManager(layout)
    slots.write_whole("A", factory_version, image)

    boot = BootControl(layout)
    boot.state.update({
        "active": "A",
        "boot_count": 0,
        "slots": {
            "A": {"status": "confirmed", "boot_attempts": 0},
            "B": {"status": "empty", "boot_attempts": 0},
        },
    })
    boot.save()


def load_device_info(root: str) -> dict:
    layout = Layout(root)
    info = read_json(layout.device_file)
    if not info:
        raise FileNotFoundError(f"设备未出厂初始化: {layout.device_file}")
    return info
