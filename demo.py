"""端到端演示。

第一版：完整成功 + 断电续传 + 不兼容 + 失败回退。
第二版：数据格式迁移（隔离副本）、迁移失败保护旧数据、试启动后失联回退、
       确认重复到达幂等、多设备按批次推进与失败熔断。

运行: python demo.py；全程只用本地临时目录里的虚拟设备和项目生成的测试包。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from edge_upgrade.manager import ActivateResult, ReceiveResult, UpgradeManager
from edge_upgrade.package import build_package, load_package
from edge_upgrade.fleet import DeviceStatus, FleetOrchestrator


def _send_in_chunks(manager, pkg, chunk_size: int, *, cut_after: int | None = None) -> int:
    """分段推送 payload；cut_after 表示推到第几分片时“断电”（直接退出，不保存任何内存态）。"""
    sent = 0
    for index in range(0, len(pkg.payload), chunk_size):
        chunk = pkg.payload[index : index + chunk_size]
        if cut_after is not None and index // chunk_size >= cut_after:
            return sent  # 模拟进程被杀：什么都不返回、内存状态全丢
        manager.write_segment(index, chunk)
        sent += len(chunk)
    return sent


def show(title: str, manager: UpgradeManager) -> None:
    r = manager.recovery_report()
    print(f"[{title}] 阶段={r['stage']} 活动槽={r['active_slot']} "
          f"版本={r['active_version']} 进度={r['received']}/{r['total']}")


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="edge-upgrade-demo-"))
    pkg_dir = workdir / "packages"
    good = build_package(pkg_dir / "v2.fwpkg", version="2.0.0", min_device_version="1.0.0")
    bad_boot = build_package(pkg_dir / "bad.fwpkg", version="2.1.0", boot_ok=False)
    incompatible = build_package(pkg_dir / "v3.fwpkg", version="3.0.0",
                                 min_device_version="5.0.0")

    # ---- 1. 完整成功流程：分段写入 -> 试启动 -> 确认；旧版本保留 ----
    dev_root = workdir / "device"
    manager = UpgradeManager.initialize(dev_root, "1.0.0")
    pkg = load_package(good)
    outcome = manager.receive_package(pkg)
    assert outcome.result is ReceiveResult.ACCEPTED_NEW
    _send_in_chunks(manager, pkg, chunk_size=64)
    manager.finish_download()
    activated = manager.boot_and_confirm()
    assert activated.result is ActivateResult.COMMITTED
    old_slot = "A" if activated.active_slot == "B" else "B"
    print(f"[成功升级] {activated.message}；旧槽 {old_slot} 仍保留版本 "
          f"{manager.device.slot_version(old_slot)}")

    # ---- 2. 重复收到同一包 ----
    dup = manager.receive_package(pkg)
    print(f"[重复包] {dup.result.value}: {dup.message}")

    # ---- 3. 升级中途断电：新进程按真实已写内容续传 ----
    dev_root2 = workdir / "device2"
    m1 = UpgradeManager.initialize(dev_root2, "1.0.0")
    pkg2 = load_package(build_package(pkg_dir / "v2b.fwpkg", version="2.0.0"))
    m1.receive_package(pkg2)
    _send_in_chunks(m1, pkg2, chunk_size=40, cut_after=1)  # 只写完第 1 片就“断电”
    m2 = UpgradeManager.open(dev_root2)  # 全新进程/重启
    show("重启对账", m2)
    resume = m2.receive_package(pkg2)  # 同一包重新下发
    print(f"[断电续传] {resume.result.value}: {resume.message}")
    for index in range(resume.received, len(pkg2.payload), 40):
        m2.write_segment(index, pkg2.payload[index : index + 40])
    m2.finish_download()
    act2 = m2.boot_and_confirm()
    print(f"[断电续传] 激活结果={act2.result.value}, 当前版本={act2.active_version}")

    # ---- 4. 版本不兼容 ----
    m3 = UpgradeManager.initialize(workdir / "device3", "1.0.0")
    inc = m3.receive_package(load_package(incompatible))
    print(f"[不兼容] {inc.result.value}: {inc.message}")

    # ---- 5. 试启动失败 -> 回退，旧版本继续在线 ----
    m4 = UpgradeManager.initialize(workdir / "device4", "1.0.0")
    bad_pkg = load_package(bad_boot)
    m4.receive_package(bad_pkg)
    _send_in_chunks(m4, bad_pkg, chunk_size=64)
    m4.finish_download()
    act4 = m4.boot_and_confirm()
    print(f"[失败回退] {act4.result.value}: {act4.message}; "
          f"活动槽={act4.active_slot} 版本={act4.active_version}")
    retry = m4.receive_package(bad_pkg)
    print(f"[坏包重发] {retry.result.value}: {retry.message}")

    # ================= 第二版：数据格式迁移 + 批次 =================

    # ---- 6. 带数据格式变化的升级：隔离副本迁移，旧程序+旧数据一起保留 ----
    v2_pkg = load_package(
        build_package(pkg_dir / "v2-data.fwpkg", version="2.0.0", data_format=2)
    )
    m5 = UpgradeManager.initialize(workdir / "device5", "1.0.0")
    active_before = m5.device.active_slot
    old_records = m5.device.read_data(active_before)["records"]
    print(f"[迁移前] 活动槽 {active_before} 数据示例: {old_records[0]}")
    m5.receive_package(v2_pkg)
    _send_in_chunks(m5, v2_pkg, chunk_size=64)
    m5.finish_download()
    pending = m5.boot_trial()
    assert pending.result is ActivateResult.AWAITING_CONFIRM
    committed = m5.confirm()
    assert committed.result is ActivateResult.COMMITTED
    active_after = m5.device.active_slot
    new_records = m5.device.read_data(active_after)["records"]
    old_slot = "A" if active_after == "B" else "B"
    print(f"[迁移提交] 版本 {committed.active_version}，新格式记录: {new_records[0]}")
    print(f"[旧槽保留] 槽 {old_slot} 版本={m5.device.slot_version(old_slot)} "
          f"数据格式={m5.device.data_format(old_slot)} 记录="
          f"{m5.device.read_data(old_slot)['records'][0]}")
    dup_confirm = m5.confirm()
    print(f"[确认重复] {dup_confirm.result.value}: {dup_confirm.message}")

    # ---- 7. 迁移失败：旧数据一个字节都没被改，失败包重投被拒绝 ----
    bad_migration = load_package(
        build_package(pkg_dir / "bad-migrate.fwpkg", version="2.3.0",
                      data_format=2, migration_ok=False)
    )
    m6 = UpgradeManager.initialize(workdir / "device6", "1.0.0")
    slot = m6.device.active_slot
    before_data = m6.device.read_data(slot)
    m6.receive_package(bad_migration)
    _send_in_chunks(m6, bad_migration, chunk_size=64)
    failed = m6.finish_download()
    print(f"[迁移失败] {failed.result.value}: {failed.message}")
    assert m6.device.read_data(slot) == before_data
    re = m6.receive_package(bad_migration)
    print(f"[失败包重投] {re.result.value}: {re.message}")

    # ---- 8. 试启动后失联：确认超时未到，旧程序+旧格式数据一起恢复 ----
    m7 = UpgradeManager.initialize(workdir / "device7", "1.0.0")
    m7.receive_package(v2_pkg)
    _send_in_chunks(m7, v2_pkg, chunk_size=64)
    m7.finish_download()
    m7.boot_trial()
    lost = m7.mark_lost_contact()
    print(f"[失联回退] {lost.result.value}: {lost.message}; "
          f"版本={m7.device.current_version} "
          f"数据格式={m7.device.data_format(m7.device.active_slot)}")

    # ---- 9. 多设备分批：批次 2 出现失败 -> 后续批次停止，成功设备状态保留 ----
    fleet = FleetOrchestrator(workdir / "fleet")
    for device_id in ("dev-1", "dev-2", "dev-3", "dev-4", "dev-5"):
        fleet.register_device(device_id, "1.0.0")
    good = load_package(
        build_package(pkg_dir / "fleet-good.fwpkg", version="2.0.0", data_format=2)
    )
    bad_boot = load_package(
        build_package(pkg_dir / "fleet-bad.fwpkg", version="2.1.0",
                      data_format=2, boot_ok=False)
    )
    packages = {"dev-1": good, "dev-2": good, "dev-3": bad_boot,
                "dev-4": good, "dev-5": good}
    rollout = fleet.create_rollout(
        "nightly", packages, batches=[["dev-1", "dev-2"], ["dev-3"], ["dev-4", "dev-5"]]
    )
    rollout.advance()
    rollout.advance()  # 第二批失败熔断；第三批不会被推进
    for device_id, record in rollout.records().items():
        print(f"[批次结果] {device_id}: {record.status.value} ({record.version}) {record.detail}")
    audit = rollout.audit()
    assert all(item["ok"] for item in audit.values())
    print(f"[一致性核对] {len(audit)} 台设备版本/数据/事务全部一致")

    print(f"\n演示目录（可检查真实文件）: {workdir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
