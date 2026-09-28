"""边缘设备升级系统（第一版：虚拟设备验证）。

模块组成：
- package:  测试升级包的生成、读取与完整性校验
- device:   虚拟设备出厂初始化
- slots:    A/B 双槽与分段写入/按真实内容恢复
- bootctl:  虚拟 bootloader：试启动、健康检查、确认与回退
- updater:  升级编排（掉电恢复、去重、兼容性判定）
"""

from .models import InstallResult, ResultCode
from .package import build_package, PackageError, PackageReader
from .updater import UpgradeEngine, SimulatedPowerCut

__all__ = [
    "InstallResult",
    "ResultCode",
    "build_package",
    "PackageError",
    "PackageReader",
    "UpgradeEngine",
    "SimulatedPowerCut",
]
