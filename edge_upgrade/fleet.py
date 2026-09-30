"""多设备按批次推进的升级编排。

在单设备 :class:`~edge_upgrade.manager.UpgradeManager` 的事务之上做编排：

- 设备各自有独立磁盘目录与独立状态文件，成功/失败互不影响、分别保留；
- 批次顺序推进，一批内逐个设备完成“收包(可中断续传) -> 迁移 -> 试启动
  -> 确认”；任一批内出现失败且 ``stop_on_failure=True`` 时，后续批次全部
  停止推进（状态标记为 stopped，而不是悄悄继续）；
- 已确认设备的状态单独保留，失败设备可以重新投递升级包——设备侧事务会
  拒绝重复破坏旧数据（返回 ``previous_rolled_back``）。

编排状态落盘在 ``fleet/rollouts/<name>.json``，重启后可继续查看/推进。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .device import atomic_write_json, read_json
from .manager import (
    ActivateResult,
    ReceiveResult,
    ReceiveOutcome,
    UpgradeManager,
)
from .package import FirmwarePackage


class DeviceStatus(str, enum.Enum):
    PENDING = "pending"                  # 尚未轮到
    CONFIRMED = "confirmed"              # 升级成功并确认
    ROLLED_BACK = "rolled_back"          # 失败已回退（旧程序+旧数据在线）
    SKIPPED = "skipped"                  # 因前序失败被停止，未推进


@dataclass
class DeviceRecord:
    device_id: str
    status: DeviceStatus
    version: str | None = None
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "status": self.status.value,
            "version": self.version,
            "detail": self.detail,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DeviceRecord":
        return cls(
            device_id=str(data["device_id"]),
            status=DeviceStatus(data["status"]),
            version=data.get("version"),
            detail=str(data.get("detail", "")),
        )


class FleetError(RuntimeError):
    pass


class FleetOrchestrator:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.devices_dir = self.root / "fleet" / "devices"
        self.rollouts_dir = self.root / "fleet" / "rollouts"
        self.registry_path = self.root / "fleet" / "devices.json"

    # ---------- 设备登记 ----------

    def register_device(self, device_id: str, factory_version: str = "1.0.0") -> Path:
        if "/" in device_id or device_id in ("", ".", ".."):
            raise FleetError(f"非法设备 id: {device_id!r}")
        path = self.devices_dir / device_id
        if path.exists():
            raise FleetError(f"设备已登记: {device_id}")
        UpgradeManager.initialize(path, factory_version=factory_version)
        registry = self._load_registry()
        registry[device_id] = factory_version
        atomic_write_json(self.registry_path, registry)
        return path

    def _load_registry(self) -> dict[str, str]:
        if not self.registry_path.exists():
            return {}
        return dict(read_json(self.registry_path))

    def open_device(self, device_id: str) -> UpgradeManager:
        path = self.devices_dir / device_id
        if not path.exists():
            raise FleetError(f"设备未登记: {device_id}")
        return UpgradeManager.open(path)

    def device_root(self, device_id: str) -> Path:
        return self.devices_dir / device_id

    # ---------- 创建批次计划 ----------

    def create_rollout(
        self,
        name: str,
        packages: FirmwarePackage | dict[str, FirmwarePackage],
        batches: list[list[str]],
        *,
        stop_on_failure: bool = True,
    ) -> "Rollout":
        if not batches or any(not batch for batch in batches):
            raise FleetError("批次不能为空")
        registry = self._load_registry()
        seen: set[str] = set()
        for batch in batches:
            for device_id in batch:
                if device_id not in registry:
                    raise FleetError(f"批次包含未登记设备: {device_id}")
                if device_id in seen:
                    raise FleetError(f"设备在批次中重复出现: {device_id}")
                seen.add(device_id)
        if isinstance(packages, FirmwarePackage):
            package_map = {device_id: packages for device_id in seen}
        else:
            missing = seen - set(packages)
            if missing:
                raise FleetError(f"以下设备缺少升级包: {sorted(missing)}")
            package_map = dict(packages)

        self.rollouts_dir.mkdir(parents=True, exist_ok=True)
        package_store = self.rollouts_dir / f"{name}.pkgs"
        package_store.mkdir(exist_ok=True)
        stored: dict[str, str] = {}
        for device_id, pkg in package_map.items():
            fname = f"{device_id}.fwpkg"
            (package_store / fname).write_bytes(pkg_raw(pkg))
            stored[device_id] = fname

        plan = {
            "name": name,
            "stop_on_failure": bool(stop_on_failure),
            "batches": [list(batch) for batch in batches],
            "packages": stored,
            "stopped": False,
            "records": {
                device_id: DeviceRecord(device_id, DeviceStatus.PENDING).to_dict()
                for device_id in seen
            },
        }
        atomic_write_json(self._rollout_path(name), plan)
        return self.open_rollout(name)

    def _rollout_path(self, name: str) -> Path:
        if "/" in name or name in ("", ".", ".."):
            raise FleetError(f"非法 rollout 名称: {name!r}")
        return self.rollouts_dir / f"{name}.json"

    def open_rollout(self, name: str) -> "Rollout":
        path = self._rollout_path(name)
        if not path.exists():
            raise FleetError(f"升级计划不存在: {name}")
        return Rollout(self, name)


def pkg_raw(pkg: FirmwarePackage) -> bytes:
    """把内存中的升级包重新打包为 ZIP 字节（用于按设备落盘投递）。"""
    import io
    import json
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json",
                    json.dumps(pkg.manifest.to_dict(), ensure_ascii=False, indent=2))
        zf.writestr("payload.bin", pkg.payload)
    return buf.getvalue()


class Rollout:
    """一次批次升级的可变视图（每次操作都基于磁盘上的 JSON 计划）。"""

    def __init__(self, fleet: FleetOrchestrator, name: str):
        self.fleet = fleet
        self.name = name
        self.path = fleet._rollout_path(name)

    def _load(self) -> dict[str, Any]:
        return read_json(self.path)

    def _save(self, plan: dict[str, Any]) -> None:
        atomic_write_json(self.path, plan)

    def _package_for(self, plan: dict[str, Any], device_id: str) -> FirmwarePackage:
        from .package import load_package

        fname = plan["packages"][device_id]
        return load_package(self.fleet.rollouts_dir / f"{self.name}.pkgs" / fname)

    def records(self) -> dict[str, DeviceRecord]:
        plan = self._load()
        return {
            device_id: DeviceRecord.from_dict(data)
            for device_id, data in plan["records"].items()
        }

    def status(self, device_id: str) -> DeviceRecord:
        return self.records()[device_id]

    def stopped(self) -> bool:
        return bool(self._load()["stopped"])

    def progress(self) -> dict[str, Any]:
        plan = self._load()
        return {
            "name": plan["name"],
            "stopped": plan["stopped"],
            "batches": plan["batches"],
            "records": {
                device_id: DeviceRecord.from_dict(data).to_dict()
                for device_id, data in plan["records"].items()
            },
        }

    # ---------- 单设备推进（含下载中断续传、失联模拟） ----------

    def run_device(
        self,
        device_id: str,
        *,
        chunk_size: int = 64,
        cut_chunk: int | None = None,
        lost_contact: bool = False,
    ) -> DeviceRecord:
        """对一台设备执行完整事务。

        cut_chunk: 传完第几个分片后模拟下载中断（进程退出），随后自动
                   用同一包重新投递并按真实字节续传。
        lost_contact: 试启动健康通过后不发送确认，改判失联回退。
        """
        plan = self._load()
        if device_id not in plan["records"]:
            raise FleetError(f"设备不在本次计划中: {device_id}")
        pkg = self._package_for(plan, device_id)
        manager = self.fleet.open_device(device_id)

        outcome = manager.receive_package(pkg)
        if outcome.result is ReceiveResult.PREVIOUS_ROLLED_BACK:
            record = DeviceRecord(
                device_id, DeviceStatus.ROLLED_BACK,
                manager.device.current_version,
                "失败包再次投递被拒绝，旧程序与旧数据保持不动",
            )
            self._put_record(plan, record)
            return record

        chunks: list[tuple[int, bytes]] = [
            (index, pkg.payload[index:index + chunk_size])
            for index in range(0, len(pkg.payload), chunk_size)
        ]
        if cut_chunk is not None:
            for index, chunk in chunks[:cut_chunk]:
                manager.write_segment(index, chunk)
            # “断电/中断”：丢弃管理器实例，再以新进程重开、同一包重新投递
            del manager
            manager = self.fleet.open_device(device_id)
            resumed = manager.receive_package(pkg)
            if resumed.result is not ReceiveResult.RESUMED_SAME:
                raise FleetError(f"下载中断后续传异常: {resumed.result}")
            offset = resumed.received
        else:
            offset = 0
        while offset < len(pkg.payload):
            manager.write_segment(offset, pkg.payload[offset:offset + chunk_size])
            offset += chunk_size

        finished = manager.finish_download()
        if finished.result is ReceiveResult.MIGRATION_FAILED:
            return self._record_rollback(manager, device_id, finished.message)

        trial = manager.boot_trial()
        if trial.result is ActivateResult.AWAITING_CONFIRM:
            if lost_contact:
                lost = manager.mark_lost_contact()
                return self._record_rollback(manager, device_id, lost.message)
            confirmed = manager.confirm()
            if confirmed.result is ActivateResult.COMMITTED:
                return self._record_confirmed(manager, device_id, confirmed.message)
            return self._record_rollback(manager, device_id, confirmed.message)
        return self._record_rollback(manager, device_id, trial.message)

    def _record_rollback(
        self, manager: UpgradeManager, device_id: str, detail: str
    ) -> DeviceRecord:
        plan = self._load()
        record = DeviceRecord(
            device_id, DeviceStatus.ROLLED_BACK,
            manager.device.current_version, detail,
        )
        self._put_record(plan, record)
        self._halt_after_failure(plan, device_id)
        return record

    def _record_confirmed(
        self, manager: UpgradeManager, device_id: str, detail: str
    ) -> DeviceRecord:
        plan = self._load()
        record = DeviceRecord(
            device_id, DeviceStatus.CONFIRMED,
            manager.device.current_version, detail,
        )
        self._put_record(plan, record)
        return record

    def _put_record(self, plan: dict[str, Any], record: DeviceRecord) -> None:
        plan["records"][record.device_id] = record.to_dict()
        self._save(plan)

    def _halt_after_failure(self, plan: dict[str, Any], failed_device: str) -> None:
        """失败设备所在批次之后的所有批次标记 skipped 并停止推进。

        同批中排在失败设备之后、尚未处理的设备保持 pending（同批顺序推进
        语义由调用方决定；advance 会继续处理同批剩余设备），跨批次才熔断。
        这里处理单设备接口（run_device）触发失败的情况。
        """
        if not plan["stop_on_failure"]:
            return
        failed_index = next(
            i for i, batch in enumerate(plan["batches"]) if failed_device in batch
        )
        later = {
            device_id
            for batch in plan["batches"][failed_index + 1:]
            for device_id in batch
        }
        if not later:
            return
        plan["stopped"] = True
        for device_id in later:
            record = plan["records"][device_id]
            if DeviceStatus(record["status"]) is DeviceStatus.PENDING:
                record["status"] = DeviceStatus.SKIPPED.value
                record["detail"] = "前序批次出现失败，已停止后续批次推进"
        self._save(plan)

    # ---------- 批次推进 ----------

    def advance(
        self,
        *,
        cut_chunk_for: str | None = None,
        lost_contact_for: set[str] | None = None,
    ) -> list[DeviceRecord]:
        """推进下一个未处理批次；已停止或全部完成时返回空列表。

        每个设备的状态独立落盘；批次内出现回退且配置了 stop_on_failure，
        则把后续批次标记为 skipped 并置 stopped=True。
        """
        lost_contact_for = lost_contact_for or set()
        plan = self._load()
        if plan["stopped"]:
            return []
        records = plan["records"]
        target_batch: list[str] | None = None
        for batch in plan["batches"]:
            if any(
                DeviceStatus(records[device_id]["status"]) is DeviceStatus.PENDING
                for device_id in batch
            ):
                target_batch = batch
                break
        if target_batch is None:
            return []

        results: list[DeviceRecord] = []
        batch_failed = False
        for device_id in target_batch:
            if DeviceStatus(records[device_id]["status"]) is not DeviceStatus.PENDING:
                continue
            record = self.run_device(
                device_id,
                cut_chunk=(2 if device_id == cut_chunk_for else None),
                lost_contact=device_id in lost_contact_for,
            )
            results.append(record)
            if record.status is DeviceStatus.ROLLED_BACK:
                batch_failed = True

        plan = self._load()
        if batch_failed and plan["stop_on_failure"]:
            plan["stopped"] = True
            current_index = plan["batches"].index(target_batch)
            later = {
                device_id
                for batch in plan["batches"][current_index + 1:]
                for device_id in batch
            }
            for device_id in later:
                record = plan["records"][device_id]
                if DeviceStatus(record["status"]) is DeviceStatus.PENDING:
                    record["status"] = DeviceStatus.SKIPPED.value
                    record["detail"] = "前序批次出现失败，已停止后续批次推进"
            self._save(plan)
        return results

    def redeliver(self, device_id: str) -> ReceiveOutcome:
        """失败设备重新收到升级包（再次投递）；事务拒绝重复破坏旧数据。"""
        plan = self._load()
        pkg = self._package_for(plan, device_id)
        manager = self.fleet.open_device(device_id)
        return manager.receive_package(pkg)

    def audit(self) -> dict[str, dict[str, Any]]:
        """逐台设备核对版本、数据格式与升级事务的一致性。"""
        report: dict[str, dict[str, Any]] = {}
        for device_id in self._load()["records"]:
            manager = self.fleet.open_device(device_id)
            report[device_id] = manager.consistency_check()
        return report
