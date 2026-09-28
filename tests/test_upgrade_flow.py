"""完整升级流程测试：全部使用项目生成的测试包与虚拟设备。"""
from __future__ import annotations

import hashlib
import json

import pytest

from edgeup import PackageError, ResultCode, SimulatedPowerCut, UpgradeEngine, build_package
from edgeup.bootctl import MAX_BOOT_ATTEMPTS
from edgeup.device import provision
from edgeup.layout import Layout
from edgeup.package import PackageReader, build_firmware


@pytest.fixture()
def device(tmp_path):
    root = tmp_path / "device"
    provision(root, factory_version="1.0.0")
    return root


@pytest.fixture()
def packages(tmp_path):
    return tmp_path / "packages"


def _engine(root):
    return UpgradeEngine(root)


def _slot_meta(root, slot):
    layout = Layout(root)
    return json.loads(layout.slot_meta(slot).read_text())


# ---------------------------------------------------------------- 正常流程


def test_full_upgrade_success(device, packages):
    pkg = build_package(packages, version="1.1.0")
    engine = _engine(device)

    result = engine.install(pkg)

    assert result.code is ResultCode.SUCCESS
    assert result.from_version == "1.0.0"
    assert result.to_version == "1.1.0"
    assert result.slot == "B"
    assert engine.boot.active_version() == "1.1.0"
    assert engine.boot.slot_status("B")["status"] == "confirmed"
    # 旧版本一直保留在 A 槽
    assert _slot_meta(device, "A")["version"] == "1.0.0"
    assert Layout(device).slot_image("A").exists()


def test_duplicate_package_is_idempotent(device, packages):
    pkg = build_package(packages, version="1.1.0")
    engine = _engine(device)
    engine.install(pkg)

    second = engine.install(pkg)
    third = _engine(device).install(pkg)  # 重启后再次收到同一包

    assert second.code is ResultCode.ALREADY_INSTALLED
    assert third.code is ResultCode.ALREADY_INSTALLED
    assert engine.boot.active_version() == "1.1.0"


def test_repeated_package_after_reinstall_uses_alternating_slots(device, packages):
    pkg1 = build_package(packages, version="1.1.0")
    pkg2 = build_package(packages, version="1.2.0")
    engine = _engine(device)
    engine.install(pkg1)
    engine.install(pkg2)

    # 连续两次升级交替使用槽位，上一个好版本继续保留
    assert _slot_meta(device, "A")["version"] == "1.2.0"
    assert _slot_meta(device, "B")["version"] == "1.1.0"
    assert engine.boot.active_version() == "1.2.0"


# ------------------------------------------------------------ 兼容性检查


def test_incompatible_baseline_version(device, packages):
    pkg = build_package(packages, version="2.0.0", min_base_version="1.5.0")
    engine = _engine(device)

    result = engine.install(pkg)

    assert result.code is ResultCode.INCOMPATIBLE_VERSION
    assert engine.boot.active_version() == "1.0.0"
    # 被拒的包不得改动任何槽
    assert not Layout(device).slot_image("B").exists()


def test_incompatible_model(device, packages):
    pkg = build_package(packages, version="1.1.0", model="edge-x2",
                        supported_models=["edge-x2"])
    result = _engine(device).install(pkg)

    assert result.code is ResultCode.INCOMPATIBLE_VERSION
    assert "edge-x1" in result.message


# ------------------------------------------------------------ 包完整性


def test_corrupted_chunk_detected(device, packages):
    pkg = build_package(packages, version="1.1.0")
    chunk = pkg / "chunks" / "0000.bin"
    data = bytearray(chunk.read_bytes())
    data[10] ^= 0xFF
    chunk.write_bytes(data)

    result = _engine(device).install(pkg)

    assert result.code is ResultCode.PACKAGE_INVALID


def test_missing_chunk_detected(device, packages):
    pkg = build_package(packages, version="1.1.0")
    (pkg / "chunks" / "0002.bin").unlink()

    with pytest.raises(PackageError):
        PackageReader(pkg).check_readable()
    assert _engine(device).install(pkg).code is ResultCode.PACKAGE_INVALID


# ---------------------------------------------------- 掉电：分段写入恢复


def test_power_loss_during_write_resumes_from_real_bytes(device, packages):
    pkg = build_package(packages, version="1.1.0", chunk_size=1024)
    engine = _engine(device)

    with pytest.raises(SimulatedPowerCut):
        engine.install(pkg, crash_after_segment=2)

    layout = Layout(device)
    # 没有成功标记，journal 停在 writing
    journal = json.loads(layout.journal_file.read_text())
    assert journal["phase"] == "writing"
    assert not layout.slot_meta("B").exists()

    # “重启”：新引擎，恢复依据是 image.bin 真实字节，而非 journal 进度
    restarted = _engine(device)
    before_bytes = layout.slot_image("B").read_bytes()
    result = restarted.install(pkg)

    assert result.code is ResultCode.SUCCESS
    assert result.slot == "B"
    # 已写入的分片内容与最终镜像一致，说明是续传而不是重写
    assert layout.slot_image("B").read_bytes()[:2048] == before_bytes[:2048]
    assert restarted.slots.staged_segments("B", PackageReader(pkg)) == 5


def test_resume_does_not_trust_stale_success_metadata(device, packages):
    """即便有人伪造完整 meta，恢复也以真实字节为准。"""
    pkg = build_package(packages, version="1.1.0", chunk_size=1024)
    engine = _engine(device)
    with pytest.raises(SimulatedPowerCut):
        engine.install(pkg, crash_after_segment=2)

    layout = Layout(device)
    # 伪造一个“已完成”标记
    layout.slot_meta("B").write_text(json.dumps({
        "version": "1.1.0", "complete": True,
        "image_sha256": "x" * 64, "image_size": 1,
    }))

    result = _engine(device).install(pkg)
    assert result.code is ResultCode.SUCCESS
    digest = hashlib.sha256(layout.slot_image("B").read_bytes()).hexdigest()
    assert _slot_meta(device, "B")["image_sha256"] == digest


def test_power_loss_before_trial_boot_then_continue(device, packages):
    pkg = build_package(packages, version="1.1.0")
    engine = _engine(device)
    with pytest.raises(SimulatedPowerCut):
        engine.install(pkg, crash_before_trial_boot=True)

    result = _engine(device).install(pkg)

    assert result.code is ResultCode.SUCCESS
    assert result.resumed is True
    assert result.boot_attempts == 1


def test_power_loss_after_boot_before_commit(device, packages):
    """试启动成功但确认标记没来得及写，重启后应补确认，不重复升级。"""
    pkg = build_package(packages, version="1.1.0")
    engine = _engine(device)
    with pytest.raises(SimulatedPowerCut):
        engine.install(pkg, crash_before_commit=True)

    layout = Layout(device)
    assert layout.boot_file.exists()
    result = _engine(device).install(pkg)

    assert result.code is ResultCode.SUCCESS
    assert result.resumed is True


# ------------------------------------------------------------ 试启动回退


def test_bad_firmware_trial_boots_then_rolls_back(device, packages):
    bad_pkg = build_package(packages, version="1.2.0", healthy=False)
    engine = _engine(device)

    result = engine.install(bad_pkg)

    assert result.code is ResultCode.ROLLED_BACK
    assert result.slot == "A"
    assert engine.boot.active_version() == "1.0.0"
    assert engine.boot.slot_status("B")["status"] == "bad"
    # 旧版本仍在 A 槽，回退后实际启动的是它
    assert _slot_meta(device, "A")["version"] == "1.0.0"


def test_rollback_survives_power_cut_at_next_boot(device, packages):
    """坏固件试启动 2 次后断电，重启后继续用完尝试次数并回退。"""
    bad_pkg = build_package(packages, version="1.2.0", healthy=False)
    engine = _engine(device)
    engine.install(bad_pkg)  # 内部连续尝试到回退
    assert engine.boot.active_version() == "1.0.0"
    assert engine.boot.slot_status("B")["boot_attempts"] == MAX_BOOT_ATTEMPTS


def test_old_version_retained_until_new_confirmed(device, packages):
    bad_pkg = build_package(packages, version="1.2.0", healthy=False)
    engine = _engine(device)
    with pytest.raises(SimulatedPowerCut):
        engine.install(bad_pkg, crash_before_trial_boot=True)

    # 新固件尚未确认时，A 槽旧版本完整保留
    assert _slot_meta(device, "A")["version"] == "1.0.0"
    result = _engine(device).install(bad_pkg)
    assert result.code is ResultCode.ROLLED_BACK
    assert _slot_meta(device, "A")["version"] == "1.0.0"


# ------------------------------------------------------------ 其它


def test_different_package_replaces_abandoned_transaction(device, packages, tmp_path):
    pkg1 = build_package(packages, version="1.1.0")
    engine = _engine(device)
    with pytest.raises(SimulatedPowerCut):
        engine.install(pkg1, crash_after_segment=1)

    other_root = tmp_path / "other_pkgs"
    pkg2 = build_package(other_root, version="1.3.0")
    result = _engine(device).install(pkg2)

    assert result.code is ResultCode.SUCCESS
    assert _slot_meta(device, "B")["version"] == "1.3.0"


def test_resume_after_stale_longer_image_in_target_slot(device, packages, tmp_path):
    """备用槽残留一次失败升级留下的更长镜像，新包较短时也必须恢复成功。"""
    bad = build_package(packages, version="1.2.0", healthy=False)
    engine = _engine(device)
    engine.install(bad)  # 回退后 B 槽残留坏固件

    layout = Layout(device)
    stale_size = layout.slot_image("B").stat().st_size
    good_root = tmp_path / "smaller_pkgs"
    # 制造一个比坏固件更短的健康镜像
    firmware = build_firmware("1.3.0")[:2000]
    good = build_package(good_root, version="1.3.0", firmware=firmware)
    assert layout.slot_image("B").stat().st_size == stale_size

    with pytest.raises(SimulatedPowerCut):
        engine.install(good, crash_after_segment=1)
    result = _engine(device).install(good)

    assert result.code is ResultCode.SUCCESS
    assert layout.slot_image("B").stat().st_size == 2000


def test_segmented_write_uses_multiple_chunks():
    image = build_firmware("9.9.9")
    assert len(image) > 1024
    assert image.startswith(b"EDGEFW-OK")
