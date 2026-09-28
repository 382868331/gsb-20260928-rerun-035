import json

import pytest

from conftest import feed

from edge_upgrade.device import VirtualDevice
from edge_upgrade.manager import (
    ActivateResult,
    ReceiveResult,
    UpgradeManager,
)
from edge_upgrade.package import load_package
from edge_upgrade.state import Stage


def test_full_success_keeps_old_version(manager, package_factory):
    assert manager.device.current_version == "1.0.0"
    pkg = package_factory(version="2.0.0")
    outcome = manager.receive_package(pkg)
    assert outcome.result is ReceiveResult.ACCEPTED_NEW
    assert outcome.total == len(pkg.payload)

    feed(manager, pkg)
    assert manager.finish_download().message.startswith("固件已装入")

    activated = manager.boot_and_confirm()
    assert activated.result is ActivateResult.COMMITTED
    assert activated.active_version == "2.0.0"
    assert manager.state.stage is Stage.CONFIRMED

    # 旧版本一直保留在另一个槽里
    old_slot = manager.device.inactive_slot
    assert manager.device.slot_version(old_slot) == "1.0.0"
    assert manager.device.trial() is None


def test_power_loss_mid_download_resumes_from_real_bytes(tmp_path, package_factory):
    root = tmp_path / "dev"
    m1 = UpgradeManager.initialize(root, "1.0.0")
    pkg = package_factory(version="2.0.0")
    m1.receive_package(pkg)

    chunk = pkg.payload[:30]
    m1.write_segment(0, chunk)
    real_written = m1.device.staging_size()
    assert real_written == 30

    # “断电”：丢弃管理器对象，用全新实例重新打开
    m2 = UpgradeManager.open(root)
    report = m2.recovery_report()
    assert report["stage"] == Stage.DOWNLOADING.value
    assert report["received"] == 30  # 来自真实文件，而非状态声称值
    assert m2.state.received == 30

    # 同一包重新下发 -> 续传而非从头
    outcome = m2.receive_package(pkg)
    assert outcome.result is ReceiveResult.RESUMED_SAME
    assert outcome.received == 30

    # 重发已写分片（重复包/重复分片），内容一致时忽略
    dup = m2.write_segment(0, chunk)
    assert dup.result is ReceiveResult.DUPLICATE_SEGMENT
    assert m2.device.staging_size() == 30

    # 从真实偏移继续写完
    m2.write_segment(30, pkg.payload[30:])
    m2.finish_download()
    result = m2.boot_and_confirm()
    assert result.result is ActivateResult.COMMITTED
    assert result.active_version == "2.0.0"


def test_resume_corrects_state_against_disk(tmp_path, package_factory):
    """状态文件声称写完了，但磁盘暂存文件其实只有部分字节 -> 以磁盘为准。"""
    root = tmp_path / "dev"
    m1 = UpgradeManager.initialize(root, "1.0.0")
    pkg = package_factory(version="2.0.0")
    m1.receive_package(pkg)
    m1.write_segment(0, pkg.payload[:10])
    # 恶意/掉电造成状态与磁盘不一致：把状态里的 received 改大
    state_path = root / "state" / "upgrade_state.json"
    data = json.loads(state_path.read_text())
    data["received"] = len(pkg.payload)
    state_path.write_text(json.dumps(data))

    m2 = UpgradeManager.open(root)
    assert m2.state.received == 10  # 被真实长度纠正
    outcome = m2.finish_download()
    assert "不完整" in outcome.message


def test_power_loss_before_any_durable_write_resets(tmp_path, package_factory):
    root = tmp_path / "dev"
    m1 = UpgradeManager.initialize(root, "1.0.0")
    pkg = package_factory(version="2.0.0")
    m1.receive_package(pkg)
    # 暂存区被外部清空（断电发生在落盘前）
    m1.device.discard_staging()
    m2 = UpgradeManager.open(root)
    assert m2.state.stage is Stage.IDLE
    assert m2.device.current_version == "1.0.0"


def test_power_loss_during_trial_reboots_to_old_version(tmp_path, package_factory):
    root = tmp_path / "dev"
    m1 = UpgradeManager.initialize(root, "1.0.0")
    pkg = package_factory(version="2.0.0")
    m1.receive_package(pkg)
    feed(m1, pkg)
    m1.finish_download()
    assert m1.device.trial() is not None  # 新版本尚未确认

    # 此时重启：未确认的试启动必须自动回退到旧版本
    m2 = UpgradeManager.open(root)
    assert m2.state.stage is Stage.ROLLED_BACK
    assert m2.device.current_version == "1.0.0"
    assert m2.device.trial() is None


def test_duplicate_package_after_confirm_is_idempotent(manager, package_factory):
    pkg = package_factory(version="2.0.0")
    manager.receive_package(pkg)
    feed(manager, pkg)
    manager.finish_download()
    manager.boot_and_confirm()

    again = manager.receive_package(pkg)
    assert again.result is ReceiveResult.ALREADY_INSTALLED
    # 设备内容没有被动过
    assert manager.device.current_version == "2.0.0"


def test_resending_failed_package_is_refused(manager, package_factory):
    bad = package_factory(version="2.1.0", boot_ok=False)
    manager.receive_package(bad)
    feed(manager, bad)
    manager.finish_download()
    result = manager.boot_and_confirm()
    assert result.result is ActivateResult.TRIAL_BOOT_FAILED_ROLLED_BACK
    assert result.active_version == "1.0.0"  # 旧版本继续在线

    retry = manager.receive_package(bad)
    assert retry.result is ReceiveResult.PREVIOUS_ROLLED_BACK


def test_health_check_failure_rolls_back(manager, package_factory):
    pkg = package_factory(version="2.2.0", health_ok=False)
    manager.receive_package(pkg)
    feed(manager, pkg)
    manager.finish_download()
    result = manager.boot_and_confirm()
    assert result.result is ActivateResult.HEALTH_CHECK_FAILED_ROLLED_BACK
    assert manager.device.current_version == "1.0.0"


def test_incompatible_version_rejected_without_touching_slots(manager, package_factory):
    before = manager.device.read_slot_firmware(manager.device.active_slot)
    pkg = package_factory(version="5.0.0", min_device="4.0.0")
    outcome = manager.receive_package(pkg)
    assert outcome.result is ReceiveResult.INCOMPATIBLE
    assert manager.state.stage is Stage.IDLE
    # 没有暂存、没有槽变更
    assert manager.device.staging_info() is None
    assert manager.device.read_slot_firmware(manager.device.active_slot) == before
    assert manager.device.current_version == "1.0.0"


def test_conflicting_different_package_rejected(manager, package_factory):
    pkg_a = package_factory("a.fwpkg", version="2.0.0")
    pkg_b = package_factory("b.fwpkg", version="2.1.0")
    manager.receive_package(pkg_a)
    manager.write_segment(0, pkg_a.payload[:10])
    conflict = manager.receive_package(pkg_b)
    assert conflict.result is ReceiveResult.CONFLICT
    # 原升级仍可继续
    manager.write_segment(10, pkg_a.payload[10:])
    manager.finish_download()
    assert manager.boot_and_confirm().result is ActivateResult.COMMITTED


def test_overlapping_retransmit_with_different_bytes_rejected(manager, package_factory):
    pkg = package_factory(version="2.0.0")
    manager.receive_package(pkg)
    manager.write_segment(0, pkg.payload[:20])
    forged = b"X" * 20
    with pytest.raises(Exception, match="不一致"):
        manager.write_segment(0, forged)


def test_segment_gap_rejected(manager, package_factory):
    pkg = package_factory(version="2.0.0")
    manager.receive_package(pkg)
    with pytest.raises(Exception, match="断档"):
        manager.write_segment(99, pkg.payload[:5])


def test_installed_slot_content_matches_package(manager, package_factory):
    pkg = package_factory(version="2.0.0")
    manager.receive_package(pkg)
    feed(manager, pkg)
    manager.finish_download()
    target = manager.state.target_slot
    assert manager.device.read_slot_firmware(target) == pkg.payload
    # 旧槽仍然完好
    assert manager.device.slot_version(manager.device.active_slot) == "1.0.0"
    manager.boot_and_confirm()
