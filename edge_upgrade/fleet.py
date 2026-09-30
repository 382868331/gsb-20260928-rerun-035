"""多设备分批升级编排。

每个设备各自有独立的虚拟磁盘与升级事务，编排器负责：

* 按批次（每批一组设备）推进同一份升级包；
* 一批内逐台执行“下载 -> 迁移 -> 试启动 -> 健康确认”整事务；
* 批内出现失败（下载/迁移/启动/健康/失联任一）则本批结束后**停止后续批次**，
  已经确认成功的设备状态各自独立保留，不被牵连回退；
* 进度落盘，控制端重启后可查看报告；对未完成设备可安全重投升级包，
  已成功设备返回幂等结论，绝不会因为重复投递再破坏一次旧数据。

失败注入通过每台设备的故障开关控制（下载中断、迁移失败、试启动后失联、
启动/健康失败），用于演练与核对。
"""

from __future__ import annotations

import enum
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .device import atomic_write_json, read_json
from .manager import ActivateResult, ReceiveResult, UpgradeManager
from .migrations import MigrationError
from .package import FirmwarePackage


class DeviceFault(str, enum.Enum):
    NONE = "none"
    DOWNLOAD_INTERRUPT = "download_interrupt"  # 下载中断（分片未传全）
    MIGRATION_FAIL = "migration_fail"          # 数据迁移失败
    LOST_AFTER_TRIAL = "lost_after_trial"      # 试启动后失联
    BOOT_FAIL = "boot_fail"                    # 候选固件启动失败
    HEALTH_FAIL = "health_fail"               # 健康检查失败


class DeviceStatus(str, enum.Enum):
    PENDING = "pending"        # 尚未轮到
    SUCCEEDED = "succeeded"    # 已健康确认，状态独立保留
    FAILED = "failed"          # 本台失败（已回退或失联），触发停止后续批次
    SKIPPED = "skipped"        # 因前面批次失败而未推进
    INTERRUPTED = "interrupted"  # 下载中断，等待续传


@dataclass
class DeviceRecord:
    device_id: str
    root: str
    batch: int
    status: DeviceStatus = DeviceStatus.PENDING
    version_before: str | None = None
    version_after: str | None = None
    data_format_before: int | None = None
    data_format_after: int | None = None
    outcome: str | None = None
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DeviceRecord":
        return cls(
            device_id=str(data["device_id"]),
            root=str(data["root"]),
            batch=int(data["batch"]),
            status=DeviceStatus(data.get("status", DeviceStatus.PENDING.value)),
            version_before=data.get("version_before"),
            version_after=data.get("version_after"),
            data_format_before=data.get("data_format_before"),
            data_format_after=data.get("data_format_after"),
            outcome=data.get("outcome"),
            detail=data.get("detail"),
        )


@dataclass
class BatchReport:
    package_id: str
    halted: bool = False
    records: dict[str, DeviceRecord] = field(default_factory=dict)

    def succeeded(self) -> list[DeviceRecord]:
        return [r for r in self.records.values() if r.status is DeviceStatus.SUCCEEDED]

    def failed(self) -> list[DeviceRecord]:
        return [r for r in self.records.values() if r.status is DeviceStatus.FAILED]

    def interrupted(self) -> list[DeviceRecord]:
        return [r for r in self.records.values() if r.status is DeviceStatus.INTERRUPTED]

    def to_dict(self) -> dict[str, Any]:
        return {
            "package_id": self.package_id,
            "halted": self.halted,
            "records": {k: v.to_dict() for k, v in self.records.items()},
        }


class BatchUpgradeOrchestrator:
    """把设备分组为批次，逐批推进同一升级包的编排器。"""

    def __init__(self, fleet_root: str | Path, factory_version: str = "1.0.0",
                 chunk_size: int = 64):
        self.fleet_root = Path(fleet_root)
        self.fleet_root.mkdir(parents=True, exist_ok=True)
        self.factory_version = factory_version
        self.chunk_size = chunk_size
        self.report_path = self.fleet_root / "fleet_report.json"
        self.faults: dict[str, DeviceFault] = {}

    # ---------- 设备登记与故障注入 ----------

    def register_device(self, device_id: str, batch: int,
                        factory_version: str | None = None) -> Path:
        root = self.fleet_root / device_id
        if not root.exists():
            UpgradeManager.initialize(root, factory_version or self.factory_version)
        return root

    def set_fault(self, device_id: str, fault: DeviceFault) -> None:
        self.faults[device_id] = fault

    def device_root(self, device_id: str) -> Path:
        return self.fleet_root / device_id

    # ---------- 批次推进 ----------

    def run_batches(self, pkg: FirmwarePackage,
                    batches: Iterable[Iterable[str]]) -> BatchReport:
        """按批次顺序升级；一批中任一设备失败，停止后续所有批次。"""
        report = self._load_report(pkg.package_id)
        halt = report.halted
        for batch_index, members in enumerate(batches):
            if halt:
                for device_id in members:
                    self._mark_skipped(report, device_id, batch_index,
                                       "前面批次已有设备失败，按策略停止推进")
                continue
            batch_failed = False
            for device_id in members:
                record = self._upgrade_one(report, device_id, batch_index, pkg)
                if record.status in (DeviceStatus.FAILED, DeviceStatus.INTERRUPTED):
                    batch_failed = True
            if batch_failed:
                halt = True
                report.halted = True
            self._save_report(report)
        report.halted = halt
        self._save_report(report)
        return report

    def resume_device(self, device_id: str, pkg: FirmwarePackage) -> DeviceRecord:
        """对下载中断的设备续传并走完事务（失败包重投的安全入口）。"""
        report = self._load_report(pkg.package_id)
        record = report.records[device_id]
        manager = UpgradeManager.open(self.device_root(device_id))
        outcome = manager.receive_package(pkg)
        if outcome.result is ReceiveResult.ALREADY_INSTALLED:
            self._fill_success(record, manager)
            self._save_report(report)
            return record
        if outcome.result is ReceiveResult.PREVIOUS_ROLLED_BACK:
            record.detail = outcome.message
            self._save_report(report)
            return record
        self._feed_payload(manager, pkg, interrupt=False)
        manager.finish_download()
        trial = manager.start_trial()
        if trial.result is ActivateResult.TRIAL_STARTED:
            manager.confirm()
        self._snapshot(record, manager)
        record.status = (
            DeviceStatus.SUCCEEDED
            if manager.state.stage.value == "confirmed"
            else DeviceStatus.FAILED
        )
        record.outcome = manager.state.stage.value
        self._save_report(report)
        return record

    # ---------- 单设备事务 ----------

    def _upgrade_one(self, report: BatchReport, device_id: str,
                     batch_index: int, pkg: FirmwarePackage) -> DeviceRecord:
        root = self.device_root(device_id)
        self.register_device(device_id, batch_index)
        manager = UpgradeManager.open(root)
        record = report.records.setdefault(
            device_id,
            DeviceRecord(device_id, str(root), batch_index,
                         version_before=manager.device.current_version,
                         data_format_before=manager.data.current_format),
        )
        if record.status is DeviceStatus.SUCCEEDED:
            # 控制端重投/重跑：已成功设备保持原状，幂等返回
            manager.receive_package(pkg)
            self._fill_success(record, manager)
            return record

        fault = self.faults.get(device_id, DeviceFault.NONE)
        record.version_before = manager.device.current_version
        record.data_format_before = manager.data.current_format

        received = manager.receive_package(pkg)
        if received.result not in (ReceiveResult.ACCEPTED_NEW, ReceiveResult.RESUMED_SAME):
            record.status = DeviceStatus.FAILED
            record.outcome = received.result.value
            record.detail = received.message
            self._snapshot(record, manager)
            return record

        self._feed_payload(manager, pkg, interrupt=(fault is DeviceFault.DOWNLOAD_INTERRUPT))
        if fault is DeviceFault.DOWNLOAD_INTERRUPT:
            record.status = DeviceStatus.INTERRUPTED
            record.outcome = ReceiveResult.RESUMED_SAME.value
            record.detail = f"下载中断：仅收到 {manager.state.received}/{manager.state.payload_size} 字节"
            self._snapshot(record, manager)
            return record

        finished = manager.finish_download()
        if manager.state.received != manager.state.payload_size:
            record.status = DeviceStatus.INTERRUPTED
            record.detail = finished.message
            self._snapshot(record, manager)
            return record

        if fault is DeviceFault.MIGRATION_FAIL:
            self._inject_migration_failure(manager)
        elif fault is DeviceFault.LOST_AFTER_TRIAL:
            self._inject_lost_after_boot(manager)
        elif fault is DeviceFault.BOOT_FAIL:
            self._inject_boot_result(manager, ok=False, healthy=False, reason="注入故障：候选固件启动失败")
        elif fault is DeviceFault.HEALTH_FAIL:
            self._inject_boot_result(manager, ok=True, healthy=False, reason="注入故障：健康检查失败")

        trial = manager.start_trial()
        if trial.result is ActivateResult.TRIAL_STARTED:
            if fault is DeviceFault.LOST_AFTER_TRIAL:
                # 试启动健康，但确认通道失联：现场保留，算失败并停止后续批次
                record.status = DeviceStatus.FAILED
                record.outcome = ActivateResult.LOST_CONTACT.value
                record.detail = trial.message
                self._snapshot(record, manager)
                return record
            activation = manager.confirm()
            record.outcome = activation.result.value
            record.detail = activation.message
        else:
            record.outcome = trial.result.value
            record.detail = trial.message

        self._snapshot(record, manager)
        if manager.state.stage.value == "confirmed":
            record.status = DeviceStatus.SUCCEEDED
        else:
            record.status = DeviceStatus.FAILED
        return record

    def _inject_migration_failure(self, manager: UpgradeManager) -> None:
        """让本次迁移在隔离副本上失败（不碰 current）。"""
        original = manager.data.prepare_trial_copy

        def failing(_target_format: int):  # noqa: ANN202
            raise MigrationError("注入故障：迁移到一半数据无法转换")

        manager.data.prepare_trial_copy = failing  # type: ignore[method-assign]

    def _inject_lost_after_boot(self, manager: UpgradeManager) -> None:
        """试启动本身健康，但启动完成后链路立刻中断（确认无法送达）。"""
        original_boot = manager.device.boot

        def boot_then_lose_contact():  # noqa: ANN202
            result = original_boot()
            manager.device.set_online(False)
            return result

        manager.device.boot = boot_then_lose_contact  # type: ignore[method-assign]

    def _inject_boot_result(self, manager: UpgradeManager, *, ok: bool,
                            healthy: bool, reason: str) -> None:
        original_boot = manager.device.boot

        def forced_boot():  # noqa: ANN202
            result = original_boot()
            from .device import BootResult

            return BootResult(ok, result.slot, result.version, healthy, reason)

        manager.device.boot = forced_boot  # type: ignore[method-assign]

    def _feed_payload(self, manager: UpgradeManager, pkg: FirmwarePackage,
                      *, interrupt: bool) -> None:
        size = len(pkg.payload)
        cut = max(self.chunk_size, size // 3) if interrupt else size
        offset = manager.state.received
        if interrupt and offset >= cut:
            return  # 已在中断点或更靠后：不再写新分片
        while offset < cut:
            chunk = pkg.payload[offset : min(offset + self.chunk_size, cut)]
            manager.write_segment(offset, chunk)
            offset += len(chunk)

    # ---------- 记录 / 落盘 ----------

    def _snapshot(self, record: DeviceRecord, manager: UpgradeManager) -> None:
        record.version_after = manager.device.current_version
        record.data_format_after = manager.data.current_format

    def _fill_success(self, record: DeviceRecord, manager: UpgradeManager) -> None:
        record.status = DeviceStatus.SUCCEEDED
        record.outcome = ReceiveResult.ALREADY_INSTALLED.value
        record.detail = "设备此前已确认成功，重复投递被幂等忽略"
        self._snapshot(record, manager)

    def _mark_skipped(self, report: BatchReport, device_id: str,
                      batch_index: int, message: str) -> None:
        root = str(self.device_root(device_id))
        record = report.records.setdefault(
            device_id, DeviceRecord(device_id, root, batch_index))
        if record.status is DeviceStatus.PENDING:
            record.status = DeviceStatus.SKIPPED
            record.detail = message

    def _load_report(self, package_id: str) -> BatchReport:
        if not self.report_path.exists():
            return BatchReport(package_id=package_id)
        data = read_json(self.report_path)
        if data.get("package_id") != package_id:
            return BatchReport(package_id=package_id)
        return BatchReport(
            package_id=package_id,
            halted=bool(data.get("halted", False)),
            records={k: DeviceRecord.from_dict(v) for k, v in data["records"].items()},
        )

    def _save_report(self, report: BatchReport) -> None:
        atomic_write_json(self.report_path, report.to_dict())
