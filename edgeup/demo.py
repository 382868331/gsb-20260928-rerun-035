"""端到端演示：用项目生成的测试包与模拟启动检查完整升级过程。"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

from . import build_package
from .device import provision
from .updater import SimulatedPowerCut, UpgradeEngine


def show(title: str, result) -> None:
    flag = "✓" if result.code.value in ("success", "already_installed") else "="
    print(f"\n[{flag}] {title}")
    print(f"    结果: {result.code.value} | {result.message}")


def main(base: str = "demo_data") -> int:
    root = Path(base) / "device"
    pkgs = Path(base) / "packages"
    if root.exists():
        shutil.rmtree(root)
    if pkgs.exists():
        shutil.rmtree(pkgs)

    provision(root, factory_version="1.0.0")
    good = build_package(pkgs, version="1.1.0", min_base_version="1.0.0")
    bad = build_package(pkgs, version="1.2.0", healthy=False)
    too_new = build_package(pkgs, version="2.0.0", min_base_version="1.5.0")

    engine = UpgradeEngine(root)

    show("正常升级 1.0.0 -> 1.1.0", engine.install(good))
    show("重复投递同一升级包", engine.install(good))
    show("基线版本不兼容 (要求 >=1.5.0)", engine.install(too_new))
    show("试启动失败 -> 回退", engine.install(bad))

    pkg2 = build_package(Path(base) / "packages2", version="1.3.0")
    try:
        engine.install(pkg2, crash_after_segment=2)
    except SimulatedPowerCut as exc:
        print(f"\n[!] 模拟断电: {exc}")
    restarted = UpgradeEngine(root)
    show("重启后依据真实已写内容继续", restarted.install(pkg2))

    pkg3 = build_package(Path(base) / "packages3", version="1.4.0")
    try:
        restarted.install(pkg3, crash_before_trial_boot=True)
    except SimulatedPowerCut as exc:
        print(f"\n[!] 模拟断电: {exc}")
    show("重启后继续试启动", UpgradeEngine(root).install(pkg3))

    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "demo_data"))
