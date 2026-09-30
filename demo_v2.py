"""第二版端到端演示：数据格式迁移、失联、重复确认、多设备批次熔断。

运行: python demo_v2.py
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from edge_upgrade.fleet import BatchUpgradeOrchestrator, DeviceFault, DeviceStatus
from edge_upgrade.manager import ActivateResult, UpgradeManager
from edge_upgrade.package import build_package, load_package


def _feed(manager, pkg, chunk_size=64):
    for index in range(0, len(pkg.payload), chunk_size):
        manager.write_segment(index, pkg.payload[index : index + chunk_size])


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="edge-upgrade-v2-"))
    pkg_dir = workdir / "packages"
    v2 = load_package(build_package(pkg_dir / "v2.fwpkg", version="2.0.0", data_format=2))

    # ---- 1. 带数据格式变化的升级：迁移隔离副本 -> 确认后成套切换 ----
    m = UpgradeManager.initialize(workdir / "dev1", "1.0.0")
    print(f"[初始] 程序={m.device.current_version} 数据格式={m.data.current_format} "
          f"数据={m.data.current_document()['records']}")
    m.receive_package(v2)
    _feed(m, v2)
    m.finish_download()
    trial = m.start_trial()
    print(f"[试启动] {trial.result.value}: {trial.message}")
    print(f"         确认前在线版本仍是 {m.device.current_version}/格式 {m.data.current_format}，"
          f"隔离副本格式={m.data.trial_document()['data_format']}")
    committed = m.confirm()
    print(f"[已确认] {committed.result.value}: {committed.message}")
    print(f"         新数据={m.data.current_document()['records']}")
    again = m.confirm()
    print(f"[重复确认] {again.result.value}: {again.message}")

    # ---- 2. 迁移失败 -> 旧程序与旧格式数据成套保留，坏包重投被拒 ----
    bad_migrate = load_package(build_package(
        pkg_dir / "bad-migrate.fwpkg", version="3.0.0",
        data_format=2, migrate_ok=False))
    m2 = UpgradeManager.initialize(workdir / "dev2", "1.0.0")
    m2.receive_package(bad_migrate)
    _feed(m2, bad_migrate)
    m2.finish_download()
    failed = m2.start_trial()
    print(f"[迁移失败] {failed.result.value}: {failed.message}")
    print(f"           在线: 程序={m2.device.current_version} 格式={m2.data.current_format}")
    retry = m2.receive_package(bad_migrate)
    print(f"[坏包重投] {retry.result.value}")

    # ---- 3. 试启动后失联：旧程序+旧数据在线，重启后整套回退 ----
    m3 = UpgradeManager.initialize(workdir / "dev3", "1.0.0")
    m3.receive_package(v2)
    _feed(m3, v2)
    m3.finish_download()
    m3.start_trial()
    m3.device.set_online(False)
    lost = m3.confirm()
    print(f"[失联] {lost.result.value}: 在线仍为 "
          f"{m3.device.current_version}/格式 {m3.data.current_format}")
    m3.device.set_online(True)
    m3r = UpgradeManager.open(workdir / "dev3")
    print(f"[重启对账] 阶段={m3r.state.stage.value} "
          f"程序={m3r.device.current_version} 格式={m3r.data.current_format}")

    # ---- 4. 多设备批次：第二批因第一批失败被熔断，成功者独立保留 ----
    fleet = BatchUpgradeOrchestrator(workdir / "fleet", chunk_size=40)
    for device_id in ("d1", "d2", "d3", "d4"):
        fleet.register_device(device_id, 0 if device_id in ("d1", "d2") else 1)
    fleet.set_fault("d2", DeviceFault.MIGRATION_FAIL)
    fleet.set_fault("d4", DeviceFault.DOWNLOAD_INTERRUPT)  # 不应执行到
    report = fleet.run_batches(v2, [["d1", "d2"], ["d3", "d4"]])
    print(f"[批次] halted={report.halted}")
    for device_id, record in report.records.items():
        print(f"        {device_id}: {record.status.value} "
              f"程序={record.version_after} 格式={record.data_format_after} "
              f"原因={record.outcome}")
    assert report.records["d1"].status is DeviceStatus.SUCCEEDED
    assert report.records["d2"].status is DeviceStatus.FAILED
    assert report.records["d3"].status is DeviceStatus.SKIPPED
    assert report.records["d4"].status is DeviceStatus.SKIPPED

    print(f"\n演示目录（可检查真实文件）: {workdir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
