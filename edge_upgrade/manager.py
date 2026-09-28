"""升级编排器：把升级包、虚拟设备和持久化状态串成完整流程。

典型流程::

    manager = UpgradeManager.open(root)
    manager.receive_package(pkg)            # 检查 + 登记（兼容/重复/冲突分流）
    for chunk in chunks:                     # 分段写入（可重复、可断电续传）
        manager.write_segment(offset, chunk)
    manager.finish_download()                # 真实内容校验后装入非活动槽
    result = manager.boot_and_confirm()      # 试启动 + 健康检查 -> 提交/回退

断电恢复::

    manager = UpgradeManager.open(root)      # 自动按真实磁盘内容对账
    manager.recovery_report()                # 下载中 -> 续传；试启动未确认 -> 回退
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from pathlib import Path

from .device import BootResult, DeviceError, VirtualDevice
from .package import (
    FirmwarePackage,
    IncompatibleError,
    PackageError,
    check_compatibility,
    load_package,
)
from .state import Stage, StateStore, UpgradeState, TERMINAL_STAGES


class ReceiveResult(str, enum.Enum):
    ACCEPTED_NEW = "accepted_new"            # 新升级包，已开始接收
    SEGMENT_WRITTEN = "segment_written"      # 新分片已落盘
    RESUMED_SAME = "resumed_same"            # 同一包，断电/中断后续传
    DUPLICATE_SEGMENT = "duplicate_segment"  # 收到已写入的重复分片（内容一致）
    ALREADY_INSTALLED = "already_installed"  # 同一包已经确认安装
    PREVIOUS_ROLLED_BACK = "previous_rolled_back"  # 同包之前已回退，拒绝重试
    INCOMPATIBLE = "incompatible"            # 版本不兼容
    PACKAGE_INVALID = "package_invalid"      # 包损坏/格式错误
    CONFLICT = "conflict"                    # 有另一升级进行中，不能换包


class ActivateResult(str, enum.Enum):
    COMMITTED = "committed"                          # 试启动+健康通过，已提交
    TRIAL_BOOT_FAILED_ROLLED_BACK = "boot_failed"   # 启动失败，已回退
    HEALTH_CHECK_FAILED_ROLLED_BACK = "unhealthy"   # 启动了但不健康，已回退
    UNCONFIRMED_ROLLED_BACK = "unconfirmed_reboot"  # 重启前未确认，已回退
    INCOMPLETE = "incomplete"                        # 固件没收全，不能安装
    NOTHING = "nothing"                              # 没有待激活的升级


@dataclass
class ReceiveOutcome:
    result: ReceiveResult
    received: int = 0
    total: int = 0
    message: str = ""


@dataclass
class ActivationOutcome:
    result: ActivateResult
    boot: BootResult | None = None
    active_slot: str = ""
    active_version: str | None = None
    message: str = ""


class UpgradeManager:
    def __init__(self, device: VirtualDevice, store: StateStore):
        self.device = device
        self.store = store
        self.state: UpgradeState = store.load()
        self._reconcile_after_reboot()

    # ---------- 打开 / 恢复 ----------

    @classmethod
    def open(cls, root: str | Path) -> "UpgradeManager":
        root = Path(root)
        return cls(VirtualDevice(root), StateStore(root))

    @classmethod
    def initialize(cls, root: str | Path, factory_version: str = "1.0.0") -> "UpgradeManager":
        VirtualDevice.initialize(root, factory_version)
        manager = cls.open(root)
        manager.store.save(manager.state)
        return manager

    @classmethod
    def from_package_file(cls, root: str | Path, package_path: str | Path) -> "UpgradeManager":
        manager = cls.open(root)
        manager.receive_package(load_package(package_path))
        return manager

    def _reconcile_after_reboot(self) -> None:
        """拿真实磁盘内容校正内存状态——绝不只信成功标记。"""
        stage = self.state.stage
        if stage == Stage.DOWNLOADING:
            info = self.device.staging_info()
            if info is None or info.get("target_slot") != self.state.target_slot:
                # 暂存区根本不存在：断电发生在任何可靠落盘之前
                self._reset_idle("重启后未发现有效暂存区，丢弃未完成的下载")
            else:
                self.state.received = int(info["bytes_written"])
                self.store.save(self.state)
        elif stage == Stage.PENDING_TRIAL:
            # 重启发生在“试启动之后、确认之前”：新版本从未被确认，
            # 安全策略是回到旧版本（旧槽一直完好保留）。
            self._rollback(ActivateResult.UNCONFIRMED_ROLLED_BACK,
                           "重启前新版本未完成确认，已回退到旧版本")

    def _reset_idle(self, reason: str) -> None:
        self.state = UpgradeState()
        self.state.result = reason
        self.store.save(self.state)

    def recovery_report(self) -> dict[str, object]:
        """供上层查询：当前阶段、已真实写字节、活动槽版本。"""
        return {
            "stage": self.state.stage.value,
            "received": self.state.received,
            "total": self.state.payload_size,
            "active_slot": self.device.active_slot,
            "active_version": self.device.current_version,
            "pending_package": self.state.package_id,
            "message": self.state.result,
        }

    # ---------- 阶段一：收包（三种异常结果在此分流） ----------

    def receive_package(self, pkg: FirmwarePackage) -> ReceiveOutcome:
        stage = self.state.stage

        # 1) 同一包重复收到 —— 根据真实进度给不同结论
        if stage in (Stage.DOWNLOADING, Stage.PENDING_TRIAL) and pkg.package_id == self.state.package_id:
            if stage == Stage.PENDING_TRIAL:
                # 已经装进非活动槽，只等试启动确认；重复收包不产生副作用
                return ReceiveOutcome(
                    ReceiveResult.RESUMED_SAME,
                    self.state.payload_size,
                    self.state.payload_size,
                    "同一升级包已写入完成，等待试启动确认",
                )
            return ReceiveOutcome(
                ReceiveResult.RESUMED_SAME,
                self.state.received,
                self.state.payload_size,
                f"同一升级包续传：{self.state.received}/{self.state.payload_size} 字节",
            )

        if stage == Stage.CONFIRMED and pkg.package_id == self.state.package_id:
            return ReceiveOutcome(
                ReceiveResult.ALREADY_INSTALLED,
                self.state.payload_size,
                self.state.payload_size,
                f"版本 {pkg.version} 已安装并确认",
            )
        if stage == Stage.ROLLED_BACK and pkg.package_id == self.state.package_id:
            return ReceiveOutcome(
                ReceiveResult.PREVIOUS_ROLLED_BACK,
                self.state.received,
                self.state.payload_size,
                "该升级包此前试启动失败已回退，拒绝重复安装；请更换新版本包",
            )

        # 2) 另一个升级还在进行中，不允许悄悄换包
        if stage in (Stage.DOWNLOADING, Stage.PENDING_TRIAL):
            return ReceiveOutcome(
                ReceiveResult.CONFLICT,
                self.state.received,
                self.state.payload_size,
                f"升级 {self.state.package_id} 正在进行中（{stage.value}），请先中止或完成",
            )

        # 3) 版本兼容性检查（在动任何磁盘数据之前）
        try:
            check_compatibility(pkg, self.device.current_version)
        except IncompatibleError as exc:
            return ReceiveOutcome(ReceiveResult.INCOMPATIBLE, message=str(exc))

        # 4) 合法新包：登记并准备暂存
        target = self.device.inactive_slot
        self.device.prepare_staging(
            target, len(pkg.payload), pkg.manifest.payload_sha256
        )
        self.state = UpgradeState(
            stage=Stage.DOWNLOADING,
            package_id=pkg.package_id,
            version=pkg.version,
            target_slot=target,
            payload_size=len(pkg.payload),
            payload_sha256=pkg.manifest.payload_sha256,
            received=0,
        )
        self.store.save(self.state)
        return ReceiveOutcome(
            ReceiveResult.ACCEPTED_NEW,
            0,
            len(pkg.payload),
            f"已接受升级包 {pkg.package_id}（{pkg.version}），写入目标槽 {target}",
        )

    def abort_upgrade(self) -> None:
        """中止当前升级，清空暂存并回到 IDLE。"""
        self.device.discard_staging()
        self.device.clear_trial()
        self._reset_idle("升级已手动中止")

    # ---------- 阶段二：分段写入 ----------

    def write_segment(self, offset: int, data: bytes) -> ReceiveOutcome:
        if self.state.stage != Stage.DOWNLOADING:
            raise DeviceError(f"当前阶段 {self.state.stage.value} 不接受分片")
        total = self.state.payload_size
        written_before = self.state.received
        try:
            written_after = self.device.write_segment(offset, bytes(data))
        except DeviceError as exc:
            raise
        # 先落盘设备数据（write_segment 已 fsync），再更新状态
        self.state.received = written_after
        self.store.save(self.state)
        if written_after <= written_before:
            return ReceiveOutcome(
                ReceiveResult.DUPLICATE_SEGMENT,
                written_after,
                total,
                f"重复分片已忽略（offset={offset}, len={len(data)}），内容与已写一致",
            )
        return ReceiveOutcome(
            ReceiveResult.SEGMENT_WRITTEN,
            written_after,
            total,
            f"已写入 {written_after}/{total} 字节",
        )

    # ---------- 阶段三：校验并装入非活动槽 ----------

    def finish_download(self) -> ReceiveOutcome:
        if self.state.stage != Stage.DOWNLOADING:
            if self.state.stage == Stage.CONFIRMED:
                return ReceiveOutcome(ReceiveResult.ALREADY_INSTALLED,
                                      self.state.payload_size, self.state.payload_size,
                                      "该版本已安装")
            raise DeviceError(f"当前阶段 {self.state.stage.value} 无法完成下载")
        if self.state.received != self.state.payload_size:
            return ReceiveOutcome(
                ReceiveResult.RESUMED_SAME,
                self.state.received,
                self.state.payload_size,
                f"固件不完整 {self.state.received}/{self.state.payload_size}，请继续传剩余分片",
            )
        # 按真实暂存内容校验大小+sha256，再原子写入非活动槽（旧槽不动）
        target = self.device.verify_and_install()
        self.state.stage = Stage.PENDING_TRIAL
        self.state.received = self.state.payload_size
        self.store.save(self.state)
        self.device.begin_trial(target, self.state.version or "")
        return ReceiveOutcome(
            ReceiveResult.RESUMED_SAME,
            self.state.payload_size,
            self.state.payload_size,
            f"固件已装入槽 {target}，等待试启动确认",
        )

    # ---------- 阶段四：试启动 + 健康检查；失败即回退 ----------

    def boot_and_confirm(self) -> ActivationOutcome:
        if self.state.stage != Stage.PENDING_TRIAL:
            return ActivationOutcome(
                ActivateResult.NOTHING,
                active_slot=self.device.active_slot,
                active_version=self.device.current_version,
                message="当前没有待确认的升级",
            )
        boot = self.device.boot()
        self.state.attempts += 1
        self.store.save(self.state)
        if not boot.ok:
            return self._rollback(ActivateResult.TRIAL_BOOT_FAILED_ROLLED_BACK,
                                  boot.reason or "试启动失败", boot)
        if not boot.healthy:
            return self._rollback(ActivateResult.HEALTH_CHECK_FAILED_ROLLED_BACK,
                                  boot.reason or "健康检查失败", boot)
        slot = self.device.commit_trial()
        self.state.stage = Stage.CONFIRMED
        self.state.result = f"版本 {boot.version} 在槽 {slot} 试启动与健康检查通过，已提交"
        self.store.save(self.state)
        return ActivationOutcome(
            ActivateResult.COMMITTED, boot, slot, boot.version, self.state.result
        )

    def _rollback(self, kind: ActivateResult, message: str,
                  boot: BootResult | None = None) -> ActivationOutcome:
        old_slot = self.device.rollback_trial()  # 旧槽从未被覆盖，指针回旧槽
        self.device.discard_staging()
        if self.state.stage in (Stage.PENDING_TRIAL, Stage.DOWNLOADING):
            self.state.stage = Stage.ROLLED_BACK
        self.state.result = message
        self.store.save(self.state)
        return ActivationOutcome(
            kind, boot, old_slot, self.device.slot_version(old_slot), message
        )
