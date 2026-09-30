"""第二版：带数据格式变化的升级——程序与数据联动的事务一致性。"""

import json

import pytest

from conftest import feed

from edge_upgrade import migration
from edge_upgrade.manager import (
    ActivateResult,
    ReceiveResult,
    UpgradeManager,
)
from edge_upgrade.package import build_package, load_package
from edge_upgrade.state import Stage


def _v2_pkg(tmp_path, *, name="v2.fwpkg", **flags):
    return load_package(
        build_package(tmp_path / name, version="2.0.0", data_format=2, **flags)
    )


def test_migration_v1_to_v2_rewrites_records():
    doc = {"records": [{"id": 1, "value": "alpha"}]}
    migrated = migration.migrate_dataset(doc, 1, 2)
    assert migrated == {"records": [{"id": 1, "text": "alpha", "schema": 2}]}
    # 纯函数：输入不被修改
    assert doc == {"records": [{"id": 1, "value": "alpha"}]}


def test_successful_upgrade_commutes_firmware_and_data(manager, tmp_path):
    pkg = _v2_pkg(tmp_path)
    manager.receive_package(pkg)
    feed(manager, pkg)
    manager.finish_download()
    result = manager.boot_and_confirm()
    assert result.result is ActivateResult.COMMITTED

    active = manager.device.active_slot
    assert manager.device.current_version == "2.0.0"
    assert manager.device.data_format(active) == 2
    records = manager.device.read_data(active)["records"]
    assert all(r.get("schema") == 2 and "text" in r for r in records)
    # 旧槽：旧程序 + 旧格式数据原样保留
    old = manager.device.inactive_slot
    assert manager.device.slot_version(old) == "1.0.0"
    assert manager.device.data_format(old) == 1
    assert "value" in manager.device.read_data(old)["records"][0]


def test_migration_runs_on_isolated_copy_and_active_data_untouched(manager, tmp_path):
    pkg = _v2_pkg(tmp_path, migration_ok=False)
    active = manager.device.active_slot
    before_data = manager.device.read_data(active)
    before_fw = manager.device.read_slot_firmware(active)

    manager.receive_package(pkg)
    feed(manager, pkg)
    outcome = manager.finish_download()
    assert outcome.result is ReceiveResult.MIGRATION_FAILED

    # 活动槽程序与数据一个字节都没变；事务回到 rolled_back
    assert manager.state.stage is Stage.ROLLED_BACK
    assert manager.device.current_version == "1.0.0"
    assert manager.device.read_data(active) == before_data
    assert manager.device.read_slot_firmware(active) == before_fw
    assert manager.device.data_format(active) == 1
    # 隔离副本已随回退清理，没有残留新格式
    target = manager.device.inactive_slot
    assert manager.device.data_format(target) is None


def test_failed_migration_package_redelivered_does_not_rebreak_data(manager, tmp_path):
    pkg = _v2_pkg(tmp_path, migration_ok=False)
    manager.receive_package(pkg)
    feed(manager, pkg)
    first = manager.finish_download()
    assert first.result is ReceiveResult.MIGRATION_FAILED
    active_data = manager.device.read_data(manager.device.active_slot)

    again = manager.receive_package(pkg)
    assert again.result is ReceiveResult.PREVIOUS_ROLLED_BACK
    # 旧数据依旧完好，没有被第二次迁移破坏
    assert manager.device.read_data(manager.device.active_slot) == active_data
    assert manager.device.current_version == "1.0.0"


def test_boot_failure_rolls_back_firmware_and_data_together(manager, tmp_path):
    pkg = _v2_pkg(tmp_path, name="bad.fwpkg", boot_ok=False)
    manager.receive_package(pkg)
    feed(manager, pkg)
    manager.finish_download()
    result = manager.boot_and_confirm()
    assert result.result is ActivateResult.TRIAL_BOOT_FAILED_ROLLED_BACK
    # 不能只回退程序：活动槽上必须还是旧程序 + 旧格式数据
    active = manager.device.active_slot
    assert manager.device.current_version == "1.0.0"
    assert manager.device.data_format(active) == 1


def test_lost_contact_after_trial_rolls_back(manager, tmp_path):
    pkg = _v2_pkg(tmp_path)
    manager.receive_package(pkg)
    feed(manager, pkg)
    manager.finish_download()
    trial = manager.boot_trial()
    assert trial.result is ActivateResult.AWAITING_CONFIRM
    # 确认一直没到 -> 失联
    lost = manager.mark_lost_contact()
    assert lost.result is ActivateResult.LOST_CONTACT_ROLLED_BACK
    assert manager.device.current_version == "1.0.0"
    assert manager.device.data_format(manager.device.active_slot) == 1
    assert manager.device.trial() is None
    # 失联回退后同样拒绝失败包重投
    assert manager.receive_package(pkg).result is ReceiveResult.PREVIOUS_ROLLED_BACK


def test_duplicate_confirm_is_idempotent(manager, tmp_path):
    pkg = _v2_pkg(tmp_path)
    manager.receive_package(pkg)
    feed(manager, pkg)
    manager.finish_download()
    manager.boot_trial()
    first = manager.confirm()
    assert first.result is ActivateResult.COMMITTED
    data_after_first = manager.device.read_data(manager.device.active_slot)

    again = manager.confirm()
    assert again.result is ActivateResult.COMMITTED
    assert "重复" in again.message
    assert manager.state.stage is Stage.CONFIRMED
    assert manager.device.read_data(manager.device.active_slot) == data_after_first
    assert manager.receive_package(pkg).result is ReceiveResult.ALREADY_INSTALLED


def test_confirm_before_healthy_trial_rejected(tmp_path, package_factory):
    root = tmp_path / "dev"
    manager = UpgradeManager.initialize(root, "1.0.0")
    pkg = _v2_pkg(tmp_path, name="unhealthy.fwpkg", health_ok=False)
    manager.receive_package(pkg)
    feed(manager, pkg)
    manager.finish_download()
    trial = manager.boot_trial()
    assert trial.result is ActivateResult.HEALTH_CHECK_FAILED_ROLLED_BACK
    assert manager.device.current_version == "1.0.0"
    assert manager.device.data_format(manager.device.active_slot) == 1


def test_power_loss_mid_download_then_upgrade_with_migration(tmp_path, package_factory):
    root = tmp_path / "dev"
    m1 = UpgradeManager.initialize(root, "1.0.0")
    pkg = _v2_pkg(tmp_path, name="resume.fwpkg")
    m1.receive_package(pkg)
    m1.write_segment(0, pkg.payload[:20])

    m2 = UpgradeManager.open(root)  # 下载中断后重开
    assert m2.receive_package(pkg).result is ReceiveResult.RESUMED_SAME
    m2.write_segment(20, pkg.payload[20:])
    m2.finish_download()
    committed = m2.boot_and_confirm()
    assert committed.result is ActivateResult.COMMITTED
    assert committed.active_version == "2.0.0"
    assert m2.device.data_format(m2.device.active_slot) == 2


def test_reboot_with_unconfirmed_trial_restores_old_program_and_data(tmp_path):
    root = tmp_path / "dev"
    m1 = UpgradeManager.initialize(root, "1.0.0")
    pkg = _v2_pkg(tmp_path, name="trial.fwpkg")
    m1.receive_package(pkg)
    feed(m1, pkg)
    m1.finish_download()
    m1.boot_trial()

    m2 = UpgradeManager.open(root)  # 确认前重启
    assert m2.state.stage is Stage.ROLLED_BACK
    assert m2.device.current_version == "1.0.0"
    assert m2.device.data_format(m2.device.active_slot) == 1
    assert m2.device.trial() is None


def test_no_migration_path_rejected_before_writes(manager, tmp_path):
    # data_format=9 没有任何迁移链
    pkg = load_package(
        build_package(tmp_path / "v9.fwpkg", version="9.0.0", data_format=9)
    )
    active = manager.device.active_slot
    before = manager.device.read_data(active)
    outcome = manager.receive_package(pkg)
    assert outcome.result is ReceiveResult.INCOMPATIBLE
    assert manager.device.read_data(active) == before


def test_consistency_check_ok_and_detects_mismatch(manager, tmp_path):
    pkg = _v2_pkg(tmp_path, name="audit.fwpkg")
    assert manager.consistency_check()["ok"] is True
    manager.receive_package(pkg)
    feed(manager, pkg)
    manager.finish_download()
    manager.boot_trial()
    assert manager.consistency_check()["ok"] is True
    manager.confirm()
    report = manager.consistency_check()
    assert report["ok"] is True
    assert report["active_version"] == "2.0.0"
    assert report["active_data_format"] == 2

    # 人为篡改：状态声称的版本与活动槽实际版本不符
    state_path = manager.store.path
    data = json.loads(state_path.read_text())
    data["version"] = "9.9.9"
    state_path.write_text(json.dumps(data))
    bad = UpgradeManager.open(manager.device.root).consistency_check()
    assert bad["ok"] is False
    assert any("不一致" in issue for issue in bad["issues"])
