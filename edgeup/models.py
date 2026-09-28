from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ResultCode(str, Enum):
    """升级流程的不同结果，掉电恢复不算独立结果。"""

    SUCCESS = "success"                       # 新版本试启动通过并已确认
    ALREADY_INSTALLED = "already_installed"   # 同一升级包已安装，直接幂等返回
    INCOMPATIBLE_VERSION = "incompatible"     # 型号不支持 / 基线版本不满足
    PACKAGE_INVALID = "package_invalid"       # 清单或分片哈希校验失败
    ROLLED_BACK = "rolled_back"              # 试启动失败，已回退旧版本
    TRIAL_PENDING = "trial_pending"          # 试启动尚未结束（需再次 reboot）
    INTERRUPTED = "interrupted"              # 升级被掉电打断，可恢复


@dataclass
class InstallResult:
    code: ResultCode
    message: str = ""
    from_version: str | None = None
    to_version: str | None = None
    slot: str | None = None
    resumed: bool = False
    boot_attempts: int = 0

    @property
    def ok(self) -> bool:
        return self.code is ResultCode.SUCCESS
