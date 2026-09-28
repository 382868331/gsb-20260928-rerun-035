"""生成测试用升级包（不连设备/云端，纯本地）。

示例::

    python tools/make_package.py out/pkg-2.0.fwpkg --version 2.0.0 \\
        --min-device 1.0.0 --max-device 1.9.9
    python tools/make_package.py out/bad-boot.fwpkg --version 2.1.0 --boot-fail
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from edge_upgrade.package import build_package  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description="生成虚拟设备测试升级包")
    p.add_argument("output", help="输出路径（建议 .fwpkg）")
    p.add_argument("--version", required=True, help="固件版本，如 2.0.0")
    p.add_argument("--min-device", default="0.0.0", help="允许的最低设备版本")
    p.add_argument("--max-device", default=None, help="允许的最高设备版本（不含则不封顶）")
    p.add_argument("--boot-fail", action="store_true", help="生成试启动失败的固件")
    p.add_argument("--health-fail", action="store_true", help="生成健康检查失败的固件")
    args = p.parse_args()

    path = build_package(
        args.output,
        version=args.version,
        min_device_version=args.min_device,
        max_device_version=args.max_device,
        boot_ok=not args.boot_fail,
        health_ok=not args.health_fail,
    )
    print(f"已生成测试包: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
