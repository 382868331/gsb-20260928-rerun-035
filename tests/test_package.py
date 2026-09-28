import zipfile

import pytest

from edge_upgrade.package import (
    PackageError,
    build_package,
    check_compatibility,
    load_package,
    make_firmware_payload,
    parse_version,
)
from edge_upgrade.package import IncompatibleError


def test_parse_version():
    assert parse_version("2") == (2, 0, 0)
    assert parse_version("2.1") == (2, 1, 0)
    assert parse_version("2.1.3") == (2, 1, 3)
    with pytest.raises(ValueError):
        parse_version("v2.1")


def test_roundtrip_build_and_load(tmp_path):
    path = build_package(tmp_path / "p.fwpkg", version="2.0.0")
    pkg = load_package(path)
    assert pkg.version == "2.0.0"
    assert pkg.firmware_spec()["boot_ok"] is True


def test_corrupted_payload_hash_rejected(tmp_path):
    path = build_package(tmp_path / "p.fwpkg", version="2.0.0")
    # 直接改 ZIP 内 payload 字节，制造传输损坏
    with zipfile.ZipFile(path) as zf:
        entries = {n: zf.read(n) for n in zf.namelist()}
    entries["payload.bin"] += b"\x00"
    with zipfile.ZipFile(tmp_path / "bad.fwpkg", "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    with pytest.raises(PackageError, match="长度|sha256"):
        load_package(tmp_path / "bad.fwpkg")


def test_garbage_zip_rejected(tmp_path):
    p = tmp_path / "x.fwpkg"
    p.write_bytes(b"not a zip at all")
    with pytest.raises(PackageError):
        load_package(p)


def test_payload_version_mismatch_rejected(tmp_path):
    payload = make_firmware_payload("9.9.9")
    build_package(tmp_path / "p.fwpkg", version="2.0.0", payload=payload)
    with pytest.raises(PackageError, match="不一致"):
        load_package(tmp_path / "p.fwpkg")


def test_compatibility_ranges(tmp_path):
    pkg = load_package(
        build_package(tmp_path / "p.fwpkg", version="3.0.0",
                       min_device_version="1.0.0", max_device_version="2.5.0")
    )
    check_compatibility(pkg, "1.0.0")
    check_compatibility(pkg, "2.5")
    with pytest.raises(IncompatibleError, match="低于"):
        check_compatibility(pkg, "0.9.9")
    with pytest.raises(IncompatibleError, match="高于"):
        check_compatibility(pkg, "2.6.0")
