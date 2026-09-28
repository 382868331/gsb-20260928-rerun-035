"""边缘设备升级系统（第一版，纯本地、虚拟设备）。

模块组成::

    package    升级包的生成、读取、校验与版本兼容性
    device     虚拟设备：A/B 分区槽、分段写入、启动与健康检查
    state      升级状态机的本地持久化（原子写 JSON）
    manager    升级编排：检查 -> 分段写入 -> 试启动 -> 确认/回退
"""

from .package import (
    FirmwarePackage,
    build_package,
    check_compatibility,
)
from .device import VirtualDevice, BootResult
from .state import UpgradeState, Stage
from .manager import UpgradeManager, ReceiveResult, ActivateResult

__all__ = [
    "FirmwarePackage",
    "build_package",
    "check_compatibility",
    "VirtualDevice",
    "BootResult",
    "UpgradeState",
    "Stage",
    "UpgradeManager",
    "ReceiveResult",
    "ActivateResult",
]

__version__ = "0.1.0"
