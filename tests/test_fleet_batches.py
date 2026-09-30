"""多设备分批推进、失败熔断、失败包重投不二次破坏的用例。"""

from edge_upgrade.fleet import (
    BatchUpgradeOrchestrator,
    DeviceFault,
    DeviceStatus,
)
from edge_upgrade.manager import ActivateResult, ReceiveResult, UpgradeManager


def _make_fleet(tmp_path):
    fleet = BatchUpgradeOrchestrator(tmp_path / "fleet", chunk_size=40)
    for device_id in ("d1", "d2", "d3", "d4", "d5"):
        batch = 0 if device_id in ("d1", "d2") else 1
        fleet.register_device(device_id, batch)
    return fleet


def test_all_batches_succeed(tmp_path, package_factory):
    fleet = _make_fleet(tmp_path)
    pkg = package_factory(version="2.0.0", data_format=2)
    report = fleet.run_batches(pkg, [["d1", "d2"], ["d3", "d4", "d5"]])
    assert not report.halted
    assert len(report.succeeded()) == 5
    for record in report.records.values():
        assert record.version_after == "2.0.0"
        assert record.data_format_after == 2


def test_failure_in_batch_stops_later_batches_but_keeps_success(tmp_path, package_factory):
    fleet = _make_fleet(tmp_path)
    # d2 迁移失败；第一批内继续执行，第二批必须整体跳过
    fleet.set_fault("d2", DeviceFault.MIGRATION_FAIL)
    pkg = package_factory(version="2.0.0", data_format=2)
    report = fleet.run_batches(pkg, [["d1", "d2"], ["d3", "d4", "d5"]])

    assert report.halted is True
    by_id = report.records
    # d1 已成功，状态独立保留，不被 d2 牵连
    assert by_id["d1"].status is DeviceStatus.SUCCEEDED
    assert by_id["d1"].version_after == "2.0.0"
    assert by_id["d1"].data_format_after == 2
    # d2 整套回退
    assert by_id["d2"].status is DeviceStatus.FAILED
    assert by_id["d2"].outcome == ActivateResult.MIGRATION_FAILED_ROLLED_BACK.value
    assert by_id["d2"].version_after == "1.0.0"
    assert by_id["d2"].data_format_after == 1
    # 第二批未推进
    for skipped in ("d3", "d4", "d5"):
        assert by_id[skipped].status is DeviceStatus.SKIPPED
        assert by_id[skipped].version_after is None

    # 被跳过的设备确实原封不动
    mgr = UpgradeManager.open(fleet.device_root("d3"))
    assert mgr.device.current_version == "1.0.0"
    assert mgr.data.current_format == 1


def test_lost_contact_device_blocks_next_batch_and_keeps_old_pair(tmp_path, package_factory):
    fleet = _make_fleet(tmp_path)
    fleet.set_fault("d1", DeviceFault.LOST_AFTER_TRIAL)
    pkg = package_factory(version="2.0.0", data_format=2)
    report = fleet.run_batches(pkg, [["d1"], ["d2"]])
    assert report.halted is True
    assert report.records["d1"].status is DeviceStatus.FAILED
    assert report.records["d1"].outcome == ActivateResult.LOST_CONTACT.value
    assert report.records["d2"].status is DeviceStatus.SKIPPED

    mgr = UpgradeManager.open(fleet.device_root("d1"))
    # 失联期间在线服务仍是旧程序+旧数据
    assert mgr.device.current_version == "1.0.0"
    assert mgr.data.current_format == 1


def test_download_interrupt_resumes_and_succeeds(tmp_path, package_factory):
    fleet = _make_fleet(tmp_path)
    fleet.set_fault("d1", DeviceFault.DOWNLOAD_INTERRUPT)
    pkg = package_factory(version="2.0.0", data_format=2)
    report = fleet.run_batches(pkg, [["d1"], ["d2"]])
    # 中断被视为本台未完成：后续批次停止
    assert report.halted is True
    rec = report.records["d1"]
    assert rec.status is DeviceStatus.INTERRUPTED
    partial = UpgradeManager.open(fleet.device_root("d1"))
    assert 0 < partial.state.received < len(pkg.payload)

    # 恢复投递：按真实字节续传，最终成套升级
    resumed = fleet.resume_device("d1", pkg)
    assert resumed.status is DeviceStatus.SUCCEEDED
    assert resumed.version_after == "2.0.0"
    assert resumed.data_format_after == 2
    mgr = UpgradeManager.open(fleet.device_root("d1"))
    assert mgr.state.received == len(pkg.payload)


def test_rerun_does_not_touch_succeeded_devices(tmp_path, package_factory):
    fleet = _make_fleet(tmp_path)
    pkg = package_factory(version="2.0.0", data_format=2)
    fleet.run_batches(pkg, [["d1"]])
    mgr_before = UpgradeManager.open(fleet.device_root("d1"))
    data_before = mgr_before.data.current_document()

    # 控制端重跑/失败包再次投递给同一台已成功设备
    report = fleet.run_batches(pkg, [["d1"]])
    assert report.records["d1"].status is DeviceStatus.SUCCEEDED
    mgr_after = UpgradeManager.open(fleet.device_root("d1"))
    assert mgr_after.receive_package(pkg).result is ReceiveResult.ALREADY_INSTALLED
    assert mgr_after.data.current_document() == data_before
    assert mgr_after.data.current_format == 2


def test_redelivering_failed_package_to_failed_device_is_refused(tmp_path, package_factory):
    fleet = _make_fleet(tmp_path)
    fleet.set_fault("d1", DeviceFault.MIGRATION_FAIL)
    pkg = package_factory(version="2.0.0", data_format=2)
    fleet.run_batches(pkg, [["d1"]])
    old_doc = UpgradeManager.open(fleet.device_root("d1")).data.current_document()

    resumed = fleet.resume_device("d1", pkg)
    assert resumed.status is DeviceStatus.FAILED
    assert "拒绝" in (resumed.detail or "") or "回退" in (resumed.detail or "")
    # 旧数据没有被第二次迁移破坏
    mgr = UpgradeManager.open(fleet.device_root("d1"))
    assert mgr.data.current_document() == old_doc
    assert mgr.device.current_version == "1.0.0"
    assert mgr.data.current_format == 1


def test_boot_and_health_failures_roll_back_per_device(tmp_path, package_factory):
    fleet = _make_fleet(tmp_path)
    fleet.set_fault("d1", DeviceFault.BOOT_FAIL)
    fleet.set_fault("d2", DeviceFault.HEALTH_FAIL)
    pkg = package_factory(version="2.0.0")
    report = fleet.run_batches(pkg, [["d1", "d2"]])
    assert report.halted is True
    for device_id in ("d1", "d2"):
        rec = report.records[device_id]
        assert rec.status is DeviceStatus.FAILED
        assert rec.version_after == "1.0.0"
        assert rec.data_format_after == 1
