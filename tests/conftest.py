import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from edge_upgrade.manager import UpgradeManager
from edge_upgrade.package import build_package, load_package


@pytest.fixture()
def manager(tmp_path):
    return UpgradeManager.initialize(tmp_path / "device", factory_version="1.0.0")


@pytest.fixture()
def package_factory(tmp_path):
    created = []

    def _make(name="pkg.fwpkg", *, version="2.0.0", boot_ok=True, health_ok=True,
              min_device="0.0.0", max_device=None):
        path = build_package(
            tmp_path / name,
            version=version,
            min_device_version=min_device,
            max_device_version=max_device,
            boot_ok=boot_ok,
            health_ok=health_ok,
        )
        created.append(path)
        return load_package(path)

    return _make


def feed(manager, pkg, chunk_size=64):
    """把整个 payload 按分段喂给管理器。"""
    for index in range(0, len(pkg.payload), chunk_size):
        manager.write_segment(index, pkg.payload[index : index + chunk_size])
