"""数据格式迁移 + 程序/数据成套事务的用例。"""

import json

import pytest

from conftest import feed

from edge_upgrade.manager import ActivateResult, ReceiveResult, UpgradeManager
from edge_upgrade.migrations import MigrationError, migrate_document
from edge_upgrade.state import Stage


def test_migration_chain_transforms_records():
    doc = {"data_format": 1, "records": ["alpha", "beta"]}
    out = migrate_document(doc, 2)
    assert out["data_format"] == 2
    assert out["records"] == [{"id": 0, "value": "alpha"}, {"id": 1, "value": "beta"}]
    # 纯函数：输入不被改动
    assert doc["records"] == ["alpha", "beta"]


def test_full_upgrade_switches_program_and_data_together(manager, package_factory):
    assert manager.data.current_format == 1
    assert manager.data.current_document()["records"] == ["alpha", "beta"]

    pkg = package_factory(version="2.0.0", data_format=2)
    manager.receive_package(pkg)
    feed(manager, pkg)
    manager.finish_download()

    # 试启动前：活动数据仍是旧格式；迁移副本是新格式
    trial = manager.start_trial()
    assert trial.result is ActivateResult.TRIAL_STARTED
    assert manager.data.current_format == 1
    assert manager.data.trial_document()["data_format"] == 2
    assert manager.device.current_version == "1.0.0"  # 程序也还没切

    outcome = manager.confirm()
    assert outcome.result is ActivateResult.COMMITTED
    assert outcome.active_version == "2.0.0"
    assert outcome.data_format == 2
    # 程序版本与数据格式核对一致
    assert manager.device.current_version == "2.0.0"
    assert manager.data.current_format == 2
    assert manager.data.current_document()["records"][0] == {"id": 0, "value": "alpha"}
    # 旧程序槽 + 旧格式数据都成套保留
    assert manager.device.slot_version(manager.device.inactive_slot) == "1.0.0"
    prev = json.loads((manager.data.previous_dir / "data.json").read_text())
    assert prev["data_format"] == 1
    assert prev["records"] == ["alpha", "beta"]


def test_same_format_upgrade_skips_migration(manager, package_factory):
    pkg = package_factory(version="1.5.0", data_format=1)
    manager.receive_package(pkg)
    feed(manager, pkg)
    manager.finish_download()
    trial = manager.start_trial()
    assert trial.result is ActivateResult.TRIAL_STARTED
    assert "无需迁移" in trial.message
    assert manager.confirm().result is ActivateResult.COMMITTED
    assert manager.data.current_format == 1


def test_migration_failure_keeps_old_program_and_old_data(tmp_path, package_factory):
    root = tmp_path / "dev"
    m1 = UpgradeManager.initialize(root, "1.0.0")
    old_data = m1.data.current_document()
    pkg = package_factory(version="2.0.0", data_format=2, migrate_ok=False)
    m1.receive_package(pkg)
    feed(m1, pkg)
    m1.finish_download()

    outcome = m1.start_trial()
    assert outcome.result is ActivateResult.MIGRATION_FAILED_ROLLED_BACK
    # 关键：旧程序和旧格式数据仍在一起，没有留下新格式
    assert m1.device.current_version == "1.0.0"
    assert m1.data.current_format == 1
    assert m1.data.current_document() == old_data
    assert m1.data.trial_document() is None
    assert m1.device.trial() is None
    assert m1.state.stage is Stage.ROLLED_BACK


def test_failed_package_redelivered_does_not_reharm_data(tmp_path, package_factory):
    root = tmp_path / "dev"
    m1 = UpgradeManager.initialize(root, "1.0.0")
    pkg = package_factory(version="2.0.0", data_format=2, migrate_ok=False)
    m1.receive_package(pkg)
    feed(m1, pkg)
    m1.finish_download()
    m1.start_trial()  # 迁移失败并回退
    old_data = m1.data.current_document()

    # 同一个失败包再次投递：拒绝，且旧数据字节级不变
    retry = m1.receive_package(pkg)
    assert retry.result is ReceiveResult.PREVIOUS_ROLLED_BACK
    assert m1.data.current_document() == old_data
    assert m1.data.current_format == 1
    assert m1.device.current_version == "1.0.0"
    assert m1.data.trial_document() is None


def test_lost_contact_keeps_old_pair_then_recover_by_reboot(tmp_path, package_factory):
    root = tmp_path / "dev"
    m1 = UpgradeManager.initialize(root, "1.0.0")
    old_data = m1.data.current_document()
    pkg = package_factory(version="2.0.0", data_format=2)
    m1.receive_package(pkg)
    feed(m1, pkg)
    m1.finish_download()
    m1.start_trial()  # 迁移副本就绪、候选已试启动

    # 设备失联：确认无法送达
    m1.device.set_online(False)
    lost = m1.confirm()
    assert lost.result is ActivateResult.LOST_CONTACT
    assert m1.device.current_version == "1.0.0"
    assert m1.data.current_document() == old_data

    # 设备恢复可达，但确认期已过（重启对账）：整套回退，不留新格式
    m1.device.set_online(True)
    m2 = UpgradeManager.open(root)
    assert m2.state.stage is Stage.ROLLED_BACK
    assert m2.device.current_version == "1.0.0"
    assert m2.data.current_format == 1
    assert m2.data.current_document() == old_data
    assert m2.data.trial_document() is None


def test_duplicate_confirm_is_idempotent(manager, package_factory):
    pkg = package_factory(version="2.0.0", data_format=2)
    manager.receive_package(pkg)
    feed(manager, pkg)
    manager.finish_download()
    manager.start_trial()
    first = manager.confirm()
    assert first.result is ActivateResult.COMMITTED
    data_after = manager.data.current_document()

    second = manager.confirm()
    assert second.result is ActivateResult.ALREADY_CONFIRMED
    third = manager.boot_and_confirm()
    assert third.result is ActivateResult.ALREADY_CONFIRMED
    # 数据没有被重复切换/破坏
    assert manager.data.current_document() == data_after
    assert manager.data.current_format == 2


def test_reboot_during_half_commit_completes_transaction(tmp_path, package_factory):
    """程序槽已切、数据还没切时断电 -> 重启后补齐，仍然成套。"""
    root = tmp_path / "dev"
    m1 = UpgradeManager.initialize(root, "1.0.0")
    pkg = package_factory(version="2.0.0", data_format=2)
    m1.receive_package(pkg)
    feed(m1, pkg)
    m1.finish_download()
    m1.start_trial()

    # 手工制造两阶段提交中间态：committed 标记 + 槽指针已切，数据未切
    trial_slot = m1.device.trial()["slot"]
    from edge_upgrade.device import atomic_write_bytes
    atomic_write_bytes(m1.device.active_path, trial_slot.encode("ascii"))
    m1.state.committed = True
    m1.store.save(m1.state)

    m2 = UpgradeManager.open(root)
    assert m2.state.stage is Stage.CONFIRMED
    assert m2.device.current_version == "2.0.0"
    assert m2.data.current_format == 2
    assert m2.data.current_document()["records"][0]["value"] == "alpha"


def test_power_loss_during_trial_rolls_back_both_program_and_data(tmp_path, package_factory):
    root = tmp_path / "dev"
    m1 = UpgradeManager.initialize(root, "1.0.0")
    old_data = m1.data.current_document()
    pkg = package_factory(version="2.0.0", data_format=2)
    m1.receive_package(pkg)
    feed(m1, pkg)
    m1.finish_download()
    m1.start_trial()  # trial 副本已是格式 2
    assert m1.data.trial_document()["data_format"] == 2

    # 试启动后、确认前直接重启 -> 程序与数据一起回到旧版
    m2 = UpgradeManager.open(root)
    assert m2.state.stage is Stage.ROLLED_BACK
    assert m2.device.current_version == "1.0.0"
    assert m2.data.current_format == 1
    assert m2.data.current_document() == old_data


def test_unknown_migration_path_is_data_error():
    with pytest.raises(MigrationError, match="迁移"):
        migrate_document({"data_format": 1, "records": []}, 5)
