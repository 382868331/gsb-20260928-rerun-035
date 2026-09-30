"""升级编排器：升级包、虚拟设备、数据迁移与持久化状态的事务编排。

第二版在第一版（A/B 槽、分段续传、试启动回退）之上增加：

* **带数据格式变化的升级**：新固件声明 ``data_format``，试启动前在
  ``data/trial`` 隔离副本上跑迁移，活动数据 ``data/current`` 原地不变；
* **程序与数据同一事务提交**：健康确认后两阶段切换（先落提交标记，再切
  槽指针，再切数据），任何一步断电都能对账——要么整套新版，要么整套旧版；
* **试启动/确认分离**：失联时保留待确认现场，重复确认幂等。

典型流程::

    manager = UpgradeManager.open(root)
    manager.receive_package(pkg)            # 检查 + 登记（兼容/重复/冲突分流）
    for chunk in chunks:                     # 分段写入（可重复、可断电续传）
        manager.write_segment(offset, chunk)
    manager.finish_download()                # 真实内容校验后装入非活动槽
    manager.start_trial()                    # 隔离迁移 + 候选槽试启动/健康检查
    manager.confirm()                        # 健康确认：程序与数据原子切换

断电恢复::

    manager = UpgradeManager.open(root)      # 自动按真实磁盘内容对账
    manager.recovery_report()  # 下载中->续传；未确认->整套回退；半提交->补齐
"""

from __future__ import annotations

import enum
import json
from dataclasses import dataclass
from pathlib import Path

from .device import BootResult, DeviceError, DeviceOfflineError, VirtualDevice
from .migrations import DataStore, MigrationError
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
    COMMITTED = "committed"                          # 健康确认通过，程序+数据已提交
    TRIAL_STARTED = "trial_started"                  # 试启动健康，等待显式确认
    ALREADY_CONFIRMED = "already_confirmed"          # 确认重复到达（幂等忽略）
    MIGRATION_FAILED_ROLLED_BACK = "migration_failed"  # 数据迁移失败，已回退
    TRIAL_BOOT_FAILED_ROLLED_BACK = "boot_failed"   # 启动失败，已回退
    HEALTH_CHECK_FAILED_ROLLED_BACK = "unhealthy"   # 启动了但不健康，已回退
    LOST_CONTACT = "lost_contact"                    # 试启动后失联，现场保留待恢复
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
    data_format: int | None = None
    message: str = ""


class UpgradeManager:
    def __init__(self, device: VirtualDevice, store: StateStore):
        self.device = device
        self.store = store
        self.data = DataStore(device.root)
        self.state: UpgradeState = store.load()
        self._reconcile_after_reboot()

    # ---------- 打开 / 恢复 ----------

    @classmethod
    def open(cls, root: str | Path) -> "UpgradeManager":
        root = Path(root)
        return cls(VirtualDevice(root), StateStore(root))

    @classmethod
    def initialize(cls, root: str | Path, factory_version: str = "1.0.0",
                   factory_data: dict | None = None) -> "UpgradeManager":
        VirtualDevice.initialize(root, factory_version)
        DataStore(root).initialize(factory_data)
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
            self._reconcile_pending_trial()

    def _reconcile_pending_trial(self) -> None:
        """按磁盘现场对账待确认/半提交的升级。"""
        trial = self.device.trial()
        # 1) 两阶段提交做了一半：程序槽指针已切、数据尚未切 -> 补齐事务
        if self.state.committed and trial is not None:
            slot = trial["slot"]
            if self.device.active_slot == slot:
                self.data.commit_trial()
                self._finalize_commit(slot, "重启后检测到半完成提交，已补齐数据切换")
                return
        # 2) 试启动之后失联/未确认就重启：整套（程序+数据）回到旧版。
        #    旧槽固件从未被覆盖，current 数据从未被原地修改，回退是配套的。
        if self.state.trial_booted:
            self._rollback(ActivateResult.UNCONFIRMED_ROLLED_BACK,
                           "试启动后未在确认期内完成确认，已整套回退到旧程序+旧数据")
            return
        # 3) 尚未试启动（迁移前/迁移中重启）：同样回到旧版，等待重新投递
        self._rollback(ActivateResult.UNCONFIRMED_ROLLED_BACK,
                       "重启前尚未进入试启动，已丢弃隔离副本并回退")

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
            "data_format": self.data.current_format,
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
        self.data.rollback_trial()
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

    # ---------- 阶段四：隔离迁移 + 试启动（与显式确认分离） ----------

    def start_trial(self) -> ActivationOutcome:
        """在隔离数据副本上迁移，并从候选槽试启动 + 健康检查。

        成功只表示“候选版本健康”，活动槽与活动数据都不切换，等待
        ``confirm()`` 的显式健康确认。
        """
        if self.state.stage == Stage.CONFIRMED:
            return ActivationOutcome(
                ActivateResult.ALREADY_CONFIRMED,
                active_slot=self.device.active_slot,
                active_version=self.device.current_version,
                data_format=self.data.current_format,
                message="该升级已确认，重复启动试启动被忽略",
            )
        if self.state.stage != Stage.PENDING_TRIAL:
            return ActivationOutcome(
                ActivateResult.NOTHING,
                active_slot=self.device.active_slot,
                active_version=self.device.current_version,
                data_format=self.data.current_format,
                message="当前没有待试启动的升级",
            )

        trial = self.device.trial()
        if trial is None:
            return ActivationOutcome(
                ActivateResult.NOTHING,
                active_slot=self.device.active_slot,
                active_version=self.device.current_version,
                data_format=self.data.current_format,
                message="试启动标记缺失，无法启动候选版本",
            )

        # 1) 隔离迁移：只写 data/trial，data/current 一个字节都不改
        target_format = int(self.state.data_format or 1)
        try:
            spec = json.loads(
                self.device.read_slot_firmware(trial["slot"]).decode("utf-8")
            )
            if spec.get("migrate_ok", True) is False and target_format != self.data.current_format:
                raise MigrationError("新程序拒绝迁移当前数据（模拟迁移失败）")
            report = self.data.prepare_trial_copy(target_format)
        except MigrationError as exc:
            return self._rollback(ActivateResult.MIGRATION_FAILED_ROLLED_BACK,
                                  f"数据迁移失败，旧数据保持原样: {exc}")

        # 2) 候选槽试启动 + 健康检查（在迁移后的隔离副本上运行）
        self.state.trial_booted = True
        self.store.save(self.state)
        try:
            boot = self.device.boot()
        except DeviceOfflineError as exc:
            # 失联不回退也不提交：保留现场，等设备重新可达后确认或重启对账
            return ActivationOutcome(
                ActivateResult.LOST_CONTACT,
                active_slot=self.device.active_slot,
                active_version=self.device.current_version,
                data_format=self.data.current_format,
                message=f"试启动后设备失联，保留旧程序+旧数据在线: {exc}",
            )
        self.state.attempts += 1
        self.store.save(self.state)
        if not boot.ok:
            return self._rollback(ActivateResult.TRIAL_BOOT_FAILED_ROLLED_BACK,
                                  boot.reason or "试启动失败", boot)
        if not boot.healthy:
            return self._rollback(ActivateResult.HEALTH_CHECK_FAILED_ROLLED_BACK,
                                  boot.reason or "健康检查失败")

        migrated = "并完成数据迁移" if report.migrated else "数据格式无需迁移"
        return ActivationOutcome(
            ActivateResult.TRIAL_STARTED,
            boot,
            self.device.active_slot,
            self.device.current_version,
            self.data.current_format,
            f"候选版本 {boot.version} 试启动健康（{migrated}），等待健康确认",
        )

    # ---------- 阶段五：显式健康确认（程序 + 数据同一事务） ----------

    def confirm(self) -> ActivationOutcome:
        """健康确认：两阶段提交，保证程序版本与数据格式成套。"""
        if self.state.stage == Stage.CONFIRMED:
            # 确认重复到达：幂等，不做任何磁盘改动
            return ActivationOutcome(
                ActivateResult.ALREADY_CONFIRMED,
                active_slot=self.device.active_slot,
                active_version=self.device.current_version,
                data_format=self.data.current_format,
                message="确认重复到达，升级早已提交，本次忽略",
            )
        if self.state.stage != Stage.PENDING_TRIAL:
            return ActivationOutcome(
                ActivateResult.NOTHING,
                active_slot=self.device.active_slot,
                active_version=self.device.current_version,
                data_format=self.data.current_format,
                message="当前没有待确认的升级",
            )
        trial = self.device.trial()
        if trial is None:
            return self._rollback(ActivateResult.UNCONFIRMED_ROLLED_BACK,
                                  "试启动标记丢失，无法确认，已回退")
        slot = trial["slot"]
        if self.device.active_slot == slot:
            # 半提交恢复路径：程序已切，补切数据
            new_format = self.data.commit_trial()
            self._finalize_commit(slot, "补提交：程序已切换，数据已补齐")
            return self._committed_outcome(new_format)

        try:
            if not self.device.online:
                raise DeviceOfflineError("确认到达时设备仍失联")
        except DeviceOfflineError as exc:
            return ActivationOutcome(
                ActivateResult.LOST_CONTACT,
                active_slot=self.device.active_slot,
                active_version=self.device.current_version,
                data_format=self.data.current_format,
                message=f"设备失联，确认无法送达，旧程序+旧数据保持在线: {exc}",
            )

        # 两阶段提交：先落“已决定提交”标记（带 fsync），再切程序槽，
        # 最后切数据。任一步断电，重启对账时都能把事务补齐或整体回退。
        self.state.committed = True
        self.store.save(self.state)
        self.device.commit_trial()                 # 阶段一：活动槽 -> 新程序
        new_format = self.data.commit_trial()      # 阶段二：活动数据 -> 新格式
        self._finalize_commit(slot, f"版本与数据（格式 {new_format}）已成套提交")
        return self._committed_outcome(new_format)

    def _finalize_commit(self, slot: str, message: str) -> None:
        self.state.stage = Stage.CONFIRMED
        self.state.trial_booted = False
        self.state.committed = False
        self.state.result = message + f"，活动槽 {slot}"
        self.store.save(self.state)

    def _committed_outcome(self, new_format: int) -> ActivationOutcome:
        slot = self.device.active_slot
        return ActivationOutcome(
            ActivateResult.COMMITTED, None, slot,
            self.device.current_version, new_format, self.state.result,
        )

    def boot_and_confirm(self) -> ActivationOutcome:
        """便捷方法：试启动通过后立即确认（第一版 API 兼容）。

        试启动失联时不做确认，返回 ``LOST_CONTACT`` 保留现场。
        """
        trial_outcome = self.start_trial()
        if trial_outcome.result in (
            ActivateResult.TRIAL_STARTED,
        ):
            return self.confirm()
        return trial_outcome

    def _rollback(self, kind: ActivateResult, message: str,
                  boot: BootResult | None = None) -> ActivationOutcome:
        old_slot = self.device.active_slot
        self.device.rollback_trial()  # 旧槽从未被覆盖，指针维持旧槽
        self.device.discard_staging()
        self.data.rollback_trial()    # 丢弃隔离副本，旧格式数据原样保留
        if self.state.stage in (Stage.PENDING_TRIAL, Stage.DOWNLOADING):
            self.state.stage = Stage.ROLLED_BACK
        self.state.trial_booted = False
        self.state.committed = False
        self.state.result = message
        self.store.save(self.state)
        return ActivationOutcome(
            kind, boot, old_slot, self.device.slot_version(old_slot),
            self.data.current_format, message,
        )
