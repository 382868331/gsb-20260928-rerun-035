"""第二版：多设备按批次推进、失败熔断、状态独立保留。"""

import pytest

from edge_upgrade.fleet import DeviceStatus, FleetOrchestrator
from edge_upgrade.manager import ReceiveResult
from edge_upgrade.package import build_package, load_package


@pytest.fixture()
def fleet(tmp_path):
    orchestrator = FleetOrchestrator(tmp_path / "fleet-root")
    for device_id in ("d1", "d2", "d3", "d4"):
        orchestrator.register_device(device_id, factory_version="1.0.0")
    return orchestrator


def _pkg(tmp_path, *, name, **flags):
    return load_package(
        build_package(tmp_path / name, version="2.0.0", data_format=2, **flags)
    )


def test_batches_advance_in_order_with_independent_success(fleet, tmp_path):
    good = _pkg(tmp_path, name="good.fwpkg")
    rollout = fleet.create_rollout(
        "r1", good, batches=[["d1", "d2"], ["d3", "d4"]]
    )
    first = rollout.advance()
    assert {r.device_id for r in first} == {"d1", "d2"}
    assert all(r.status is DeviceStatus.CONFIRMED for r in first)
    assert not rollout.stopped()

    second = rollout.advance()
    assert {r.device_id for r in second} == {"d3", "d4"}
    assert all(r.status is DeviceStatus.CONFIRMED for r in second)
    assert rollout.advance() == []  # 全部完成

    # 每台设备状态独立保留，且版本+数据格式都正确
    for device_id in ("d1", "d2", "d3", "d4"):
        manager = fleet.open_device(device_id)
        assert manager.device.current_version == "2.0.0"
        assert manager.device.data_format(manager.device.active_slot) == 2


def test_failure_stops_later_batches_but_keeps_success(fleet, tmp_path):
    good = _pkg(tmp_path, name="good.fwpkg")
    bad = _pkg(tmp_path, name="bad.fwpkg", boot_ok=False)
    packages = {"d1": good, "d2": bad, "d3": good, "d4": good}
    rollout = fleet.create_rollout(
        "r2", packages, batches=[["d1", "d2"], ["d3"], ["d4"]]
    )

    batch1 = rollout.advance()
    statuses = {r.device_id: r.status for r in batch1}
    assert statuses == {
        "d1": DeviceStatus.CONFIRMED,
        "d2": DeviceStatus.ROLLED_BACK,
    }
    assert rollout.stopped()
    assert rollout.advance() == []  # 后续批次不再推进

    records = rollout.records()
    # 成功设备状态单独保留；未推进设备标记 skipped
    assert records["d1"].status is DeviceStatus.CONFIRMED
    assert records["d1"].version == "2.0.0"
    assert records["d2"].status is DeviceStatus.ROLLED_BACK
    assert records["d2"].version == "1.0.0"
    assert records["d3"].status is DeviceStatus.SKIPPED
    assert records["d4"].status is DeviceStatus.SKIPPED

    # 失败设备仍是旧程序+旧数据；成功设备不受影响
    failed = fleet.open_device("d2")
    assert failed.device.current_version == "1.0.0"
    assert failed.device.data_format(failed.device.active_slot) == 1
    succeeded = fleet.open_device("d1")
    assert succeeded.device.current_version == "2.0.0"


def test_download_interruption_resumes_per_device(fleet, tmp_path):
    good = _pkg(tmp_path, name="good.fwpkg")
    rollout = fleet.create_rollout("r3", good, batches=[["d1"]])
    record = rollout.run_device("d1", cut_chunk=2)  # 第 2 片后下载中断
    assert record.status is DeviceStatus.CONFIRMED
    manager = fleet.open_device("d1")
    assert manager.device.current_version == "2.0.0"
    assert manager.device.data_format(manager.device.active_slot) == 2


def test_lost_contact_device_rolls_back_and_halts_batch(fleet, tmp_path):
    good = _pkg(tmp_path, name="good.fwpkg")
    packages = {device_id: good for device_id in ("d1", "d2", "d3", "d4")}
    rollout = fleet.create_rollout(
        "r4", packages, batches=[["d1"], ["d2", "d3"], ["d4"]]
    )
    record = rollout.run_device("d1", lost_contact=True)
    assert record.status is DeviceStatus.ROLLED_BACK
    assert "失联" in record.detail
    manager = fleet.open_device("d1")
    assert manager.device.current_version == "1.0.0"
    assert manager.device.data_format(manager.device.active_slot) == 1

    # 编排层显式推进后续批次会被熔断
    assert rollout.advance() == []
    records = rollout.records()
    assert records["d2"].status is DeviceStatus.SKIPPED
    assert records["d4"].status is DeviceStatus.SKIPPED


def test_redelivery_to_failed_device_refused_without_rebreaking_data(fleet, tmp_path):
    bad = _pkg(tmp_path, name="bad.fwpkg", migration_ok=False)
    rollout = fleet.create_rollout("r5", bad, batches=[["d1"], ["d2"]])
    record = rollout.run_device("d1")
    assert record.status is DeviceStatus.ROLLED_BACK
    manager = fleet.open_device("d1")
    before = manager.device.read_data(manager.device.active_slot)

    outcome = rollout.redeliver("d1")
    assert outcome.result is ReceiveResult.PREVIOUS_ROLLED_BACK
    manager = fleet.open_device("d1")
    assert manager.device.current_version == "1.0.0"
    assert manager.device.read_data(manager.device.active_slot) == before


def test_rollout_state_survives_restart(fleet, tmp_path):
    good = _pkg(tmp_path, name="good.fwpkg")
    bad = _pkg(tmp_path, name="bad.fwpkg", boot_ok=False)
    packages = {"d1": good, "d2": bad, "d3": good}
    fleet.create_rollout("r6", packages, batches=[["d1", "d2"], ["d3"]])
    reopened = fleet.open_rollout("r6")
    reopened.advance()
    again = FleetOrchestrator(fleet.root).open_rollout("r6")
    assert again.stopped() is True
    assert again.status("d1").status is DeviceStatus.CONFIRMED
    assert again.status("d2").status is DeviceStatus.ROLLED_BACK
    assert again.status("d3").status is DeviceStatus.SKIPPED
