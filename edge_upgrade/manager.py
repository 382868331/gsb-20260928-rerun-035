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
import json
from dataclasses import dataclass
from pathlib import Path

from .device import BootResult, DeviceError, VirtualDevice
from .migration import migrate_dataset, migration_path_exists, MigrationError
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
    MIGRATION_FAILED = "migration_failed"    # 数据迁移失败，事务已回滚


class ActivateResult(str, enum.Enum):
    COMMITTED = "committed"                          # 试启动+健康通过，已提交
    TRIAL_BOOT_FAILED_ROLLED_BACK = "boot_failed"   # 启动失败，已回退
    HEALTH_CHECK_FAILED_ROLLED_BACK = "unhealthy"   # 启动了但不健康，已回退
    UNCONFIRMED_ROLLED_BACK = "unconfirmed_reboot"  # 重启前未确认，已回退
    LOST_CONTACT_ROLLED_BACK = "lost_contact"      # 试启动后失联，超时回退
    MIGRATION_FAILED = "migration_failed"          # 数据迁移失败，已回退
    AWAITING_CONFIRM = "awaiting_confirm"           # 健康检查通过，等待确认
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
            "active_data_format": self.device.data_format(self.device.active_slot),
            "pending_package": self.state.package_id,
            "message": self.state.result,
        }

    def consistency_check(self) -> dict[str, object]:
        """核对版本、数据与升级事务三者是否一致。

        检查项：

        - 活动槽固件版本与 info.json、设备上报版本一致；
        - 活动槽数据可读、格式版本与数据 info.json 一致；
        - 试启动事务存在时，候选槽固件版本/数据格式必须与事务登记一致；
        - 已确认事务的版本/格式必须等于活动槽实际内容；
        - 不允许“程序与数据分居不同槽”的错配（固件槽与数据槽同名配对）。

        返回 ``{"ok": bool, "issues": [...]}``。
        """
        issues: list[str] = []
        active = self.device.active_slot
        try:
            active_version = self.device.current_version
        except DeviceError as exc:
            active_version = None
            issues.append(f"活动槽固件不可读: {exc}")
        active_fmt = self.device.data_format(active)
        if active_fmt is None:
            issues.append(f"活动槽 {active} 缺少配套数据")
        else:
            try:
                self.device.read_data(active)
            except (DeviceError, OSError, ValueError) as exc:
                issues.append(f"活动槽 {active} 数据无法读取: {exc}")

        trial = self.device.trial()
        if self.state.stage == Stage.PENDING_TRIAL:
            if trial is None:
                issues.append("状态为待确认但磁盘上没有试启动标记")
            else:
                candidate = trial.get("slot")
                slot_ver = self.device.slot_version(candidate) if candidate else None
                if slot_ver != trial.get("version"):
                    issues.append(
                        f"试启动槽 {candidate} 固件版本 {slot_ver} 与事务登记 "
                        f"{trial.get('version')} 不一致"
                    )
                cand_fmt = self.device.data_format(candidate) if candidate else None
                if cand_fmt != trial.get("data_format"):
                    issues.append(
                        f"试启动槽 {candidate} 数据格式 {cand_fmt} 与事务要求 "
                        f"{trial.get('data_format')} 不一致"
                    )
        elif trial is not None:
            issues.append(
                f"状态 {self.state.stage.value} 却残留试启动标记"
            )

        if self.state.stage == Stage.CONFIRMED:
            if active_version != self.state.version:
                issues.append(
                    f"已确认版本 {self.state.version} 与活动槽实际版本 "
                    f"{active_version} 不一致"
                )
            if active_fmt != self.state.data_format:
                issues.append(
                    f"已确认数据格式 {self.state.data_format} 与活动槽实际格式 "
                    f"{active_fmt} 不一致"
                )
        return {"ok": not issues, "issues": issues,
                "active_slot": active,
                "active_version": active_version,
                "active_data_format": active_fmt}

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
        # 数据格式同样必须有可用迁移路径，否则也在动盘前拒绝
        current_fmt = self.device.data_format(self.device.active_slot) or 1
        if not migration_path_exists(current_fmt, pkg.manifest.data_format):
            return ReceiveOutcome(
                ReceiveResult.INCOMPATIBLE,
                message=(f"数据格式 {current_fmt} -> "
                         f"{pkg.manifest.data_format} 无可用迁移路径"),
            )

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
            data_format=pkg.manifest.data_format,
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
        # 1) 按真实暂存内容校验大小+sha256，再原子写入非活动槽（旧槽不动）
        target = self.device.verify_and_install()

        # 2) 在隔离副本上迁移数据。活动槽数据从不原地修改；任何失败都整体回滚：
        #    清掉目标槽固件/数据、清暂存，旧程序+旧格式数据继续一起在线。
        active = self.device.active_slot
        current_fmt = self.device.data_format(active) or 1
        try:
            self.device.clone_data(active, target)
            migrated = migrate_dataset(
                self.device.read_data(target), current_fmt, self.state.data_format
            )
            # 固件可以声明“迁移阶段必失败”，用于模拟迁移故障：在隔离副本上引爆，
            # 旧数据此前只读、一个字节都没被改
            spec = self.package_firmware_spec()
            if not spec.get("migration_ok", True):
                raise MigrationError("固件声明其数据迁移步骤失败（模拟迁移故障）")
            self.device.migrate_data_in_slot(target, migrated, self.state.data_format)
        except (DeviceError, MigrationError, PackageError) as exc:
            outcome = self._migration_rollback(f"数据迁移失败，事务整体回滚：{exc}")
            return ReceiveOutcome(
                ReceiveResult.MIGRATION_FAILED, 0, self.state.payload_size, outcome.message
            )

        # 3) 迁移完成才登记试启动事务：固件槽与数据槽同名配对
        self.state.stage = Stage.PENDING_TRIAL
        self.state.received = self.state.payload_size
        self.state.data_migrated = True
        self.store.save(self.state)
        self.device.begin_trial(target, self.state.version or "", self.state.data_format)
        return ReceiveOutcome(
            ReceiveResult.RESUMED_SAME,
            self.state.payload_size,
            self.state.payload_size,
            (f"固件已装入槽 {target}，数据已迁移到格式 "
             f"{self.state.data_format}，等待试启动确认"),
        )

    def package_firmware_spec(self) -> dict[str, object]:
        """读目标槽固件行为描述（迁移完成后固件已落盘）。"""
        if self.state.target_slot is None:
            raise DeviceError("没有目标槽")
        return json.loads(
            self.device.read_slot_firmware(self.state.target_slot).decode("utf-8")
        )

    def _migration_rollback(self, message: str) -> ActivationOutcome:
        """迁移失败回滚：清掉目标槽本次写入的固件+隔离数据，保留旧槽一切内容。"""
        target = self.state.target_slot
        old_slot = self.device.active_slot
        self.device.clear_trial()
        self.device.discard_staging()
        if target is not None:
            self.device.discard_slot(target)
        self.state.stage = Stage.ROLLED_BACK
        self.state.data_migrated = False
        self.state.result = message
        self.store.save(self.state)
        return ActivationOutcome(
            ActivateResult.MIGRATION_FAILED, None, old_slot,
            self.device.slot_version(old_slot), message,
        )

    # ---------- 阶段四：试启动 -> 健康确认 -> 提交（失联可回退） ----------

    def boot_trial(self) -> ActivationOutcome:
        """从候选槽试启动并做健康检查；通过则停在“等待确认”，失败立即回退。"""
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
        self.state.result = (
            f"版本 {boot.version} 试启动与健康检查通过，等待外部确认"
        )
        self.store.save(self.state)
        return ActivationOutcome(
            ActivateResult.AWAITING_CONFIRM, boot, boot.slot, boot.version,
            self.state.result,
        )

    def confirm(self) -> ActivationOutcome:
        """外部健康确认到达后提交事务。重复确认是幂等的，不重复提交。"""
        # 已提交后确认重复到达：幂等返回当前事实，状态/磁盘都不再变化
        if self.state.stage == Stage.CONFIRMED:
            slot = self.device.active_slot
            return ActivationOutcome(
                ActivateResult.COMMITTED, None, slot,
                self.device.current_version, "确认重复到达：该版本此前已确认提交",
            )
        if self.state.stage != Stage.PENDING_TRIAL:
            return ActivationOutcome(
                ActivateResult.NOTHING, active_slot=self.device.active_slot,
                active_version=self.device.current_version,
                message="当前没有待确认的升级",
            )
        trial = self.device.trial()
        if trial is None or not trial.get("verified"):
            return self._rollback(
                ActivateResult.TRIAL_BOOT_FAILED_ROLLED_BACK,
                "健康检查尚未通过，确认被拒绝，已回退到旧版本",
            )
        slot = self.device.commit_trial()
        self.state.stage = Stage.CONFIRMED
        self.state.data_migrated = True
        self.state.result = (
            f"版本 {self.state.version}（数据格式 {self.state.data_format}）"
            f"在槽 {slot} 已确认提交"
        )
        self.store.save(self.state)
        return ActivationOutcome(
            ActivateResult.COMMITTED, None, slot,
            self.device.slot_version(slot), self.state.result,
        )

    def mark_lost_contact(self) -> ActivationOutcome:
        """试启动后设备失联（确认超时未达）：回退到旧程序+旧格式数据。

        已确认（CONFIRMED）的设备不受影响，返回 NOTHING。
        """
        if self.state.stage != Stage.PENDING_TRIAL:
            return ActivationOutcome(
                ActivateResult.NOTHING, active_slot=self.device.active_slot,
                active_version=self.device.current_version,
                message="没有待确认的试启动，无需失联回退",
            )
        return self._rollback(
            ActivateResult.LOST_CONTACT_ROLLED_BACK,
            "试启动后确认超时未到达（设备失联），已回退到旧程序与旧格式数据",
        )

    def boot_and_confirm(self) -> ActivationOutcome:
        """便捷流程：试启动+健康检查通过后立即确认（第一版兼容入口）。"""
        trial_result = self.boot_trial()
        if trial_result.result is not ActivateResult.AWAITING_CONFIRM:
            return trial_result
        return self.confirm()

    def _rollback(self, kind: ActivateResult, message: str,
                  boot: BootResult | None = None) -> ActivationOutcome:
        target = self.state.target_slot
        old_slot = self.device.rollback_trial()  # 旧槽从未被覆盖，指针回旧槽
        self.device.discard_staging()
        if target is not None and self.device.data_format(target) is not None:
            # 清掉隔离副本和候选固件，杜绝残留新格式数据
            self.device.discard_slot(target)
        if self.state.stage in (Stage.PENDING_TRIAL, Stage.DOWNLOADING):
            self.state.stage = Stage.ROLLED_BACK
        self.state.data_migrated = False
        self.state.result = message
        self.store.save(self.state)
        return ActivationOutcome(
            kind, boot, old_slot, self.device.slot_version(old_slot), message
        )
