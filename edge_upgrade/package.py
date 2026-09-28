"""升级包：格式定义、生成、读取、完整性校验与兼容性检查。

升级包是一个 ZIP 文件（仅用标准库），内含::

    manifest.json  清单（版本、兼容范围、payload 大小与 sha256）
    payload.bin    固件内容（虚拟设备按 JSON 解析其行为）

虚拟固件 payload 是一个小 JSON，字段:
    version      固件版本（与 manifest 一致）
    boot_ok      试启动时是否成功（用于模拟坏包）
    health_ok    健康检查是否通过
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MANIFEST_NAME = "manifest.json"
PAYLOAD_NAME = "payload.bin"
PACKAGE_EXT = ".fwpkg"


class PackageError(ValueError):
    """升级包格式错误、损坏或摘要不匹配。"""


class IncompatibleError(ValueError):
    """升级包与当前设备/版本不兼容。"""


_VERSION_RE = re.compile(r"^(\d+)(?:\.(\d+))?(?:\.(\d+))?$")


def parse_version(text: str) -> tuple[int, int, int]:
    """解析 MAJOR[.MINOR[.PATCH]] 为可比较的三元组。"""
    m = _VERSION_RE.match(text.strip())
    if not m:
        raise ValueError(f"无法解析的版本号: {text!r}")
    nums = [int(g or 0) for g in m.groups()]
    return nums[0], nums[1], nums[2]


def _cmp(a: str, b: str) -> int:
    va, vb = parse_version(a), parse_version(b)
    return (va > vb) - (va < vb)


@dataclass(frozen=True)
class Manifest:
    package_id: str
    version: str
    min_device_version: str
    max_device_version: str | None
    payload_size: int
    payload_sha256: str
    metadata: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "package_id": self.package_id,
            "version": self.version,
            "compatibility": {
                "min_device_version": self.min_device_version,
                "max_device_version": self.max_device_version,
            },
            "payload_size": self.payload_size,
            "payload_sha256": self.payload_sha256,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Manifest":
        try:
            compat = data["compatibility"]
            return cls(
                package_id=str(data["package_id"]),
                version=str(data["version"]),
                min_device_version=str(compat["min_device_version"]),
                max_device_version=(
                    str(compat["max_device_version"])
                    if compat.get("max_device_version") is not None
                    else None
                ),
                payload_size=int(data["payload_size"]),
                payload_sha256=str(data["payload_sha256"]),
                metadata=dict(data.get("metadata", {})),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise PackageError(f"manifest.json 字段缺失或非法: {exc}") from exc


@dataclass(frozen=True)
class FirmwarePackage:
    """读入内存的合法升级包。"""

    manifest: Manifest
    payload: bytes

    @property
    def package_id(self) -> str:
        return self.manifest.package_id

    @property
    def version(self) -> str:
        return self.manifest.version

    def firmware_spec(self) -> dict[str, Any]:
        """虚拟设备固件行为描述（payload 为 JSON）。"""
        try:
            spec = json.loads(self.payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PackageError(f"payload.bin 不是合法的虚拟固件 JSON: {exc}") from exc
        spec_version = str(spec.get("version", ""))
        if spec_version != self.manifest.version:
            raise PackageError(
                f"payload 版本 {spec_version!r} 与 manifest 版本 {self.manifest.version!r} 不一致"
            )
        return spec


def make_firmware_payload(
    version: str,
    *,
    boot_ok: bool = True,
    health_ok: bool = True,
    extra: dict[str, Any] | None = None,
) -> bytes:
    """构造虚拟设备固件 payload（测试包用）。"""
    spec: dict[str, Any] = {
        "version": version,
        "boot_ok": bool(boot_ok),
        "health_ok": bool(health_ok),
    }
    if extra:
        spec.update(extra)
    return json.dumps(spec, ensure_ascii=False, sort_keys=True).encode("utf-8")


def build_package(
    output_path: str | Path,
    *,
    version: str,
    min_device_version: str = "0.0.0",
    max_device_version: str | None = None,
    payload: bytes | None = None,
    boot_ok: bool = True,
    health_ok: bool = True,
    metadata: dict[str, Any] | None = None,
) -> Path:
    """生成一个测试用升级包，返回写入路径。"""
    parse_version(version)
    parse_version(min_device_version)
    if max_device_version is not None:
        parse_version(max_device_version)
        if _cmp(min_device_version, max_device_version) > 0:
            raise ValueError("min_device_version 不能大于 max_device_version")
    if payload is None:
        payload = make_firmware_payload(version, boot_ok=boot_ok, health_ok=health_ok)
    digest = hashlib.sha256(payload).hexdigest()
    manifest = Manifest(
        package_id=f"fw-{version}-{digest[:12]}",
        version=version,
        min_device_version=min_device_version,
        max_device_version=max_device_version,
        payload_size=len(payload),
        payload_sha256=digest,
        metadata=metadata or {},
    )
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(MANIFEST_NAME, json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2))
        zf.writestr(PAYLOAD_NAME, payload)
    return out


def load_package(source: str | Path | bytes) -> FirmwarePackage:
    """读取并严格校验升级包（结构、清单、长度与 sha256）。"""
    if isinstance(source, bytes):
        zf_src: Any = io.BytesIO(source)
    else:
        path = Path(source)
        if not path.is_file():
            raise PackageError(f"升级包不存在: {path}")
        zf_src = path
    try:
        with zipfile.ZipFile(zf_src) as zf:
            names = set(zf.namelist())
            missing = {MANIFEST_NAME, PAYLOAD_NAME} - names
            if missing:
                raise PackageError(f"升级包缺少条目: {sorted(missing)}")
            manifest_bytes = zf.read(MANIFEST_NAME)
            payload = zf.read(PAYLOAD_NAME)
    except zipfile.BadZipFile as exc:
        raise PackageError(f"升级包不是合法 ZIP: {exc}") from exc
    try:
        manifest_data = json.loads(manifest_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PackageError(f"manifest.json 非法: {exc}") from exc
    manifest = Manifest.from_dict(manifest_data)
    if len(payload) != manifest.payload_size:
        raise PackageError(
            f"payload 长度 {len(payload)} 与清单 {manifest.payload_size} 不一致（升级包损坏）"
        )
    actual = hashlib.sha256(payload).hexdigest()
    if actual != manifest.payload_sha256:
        raise PackageError("payload sha256 校验失败（升级包损坏或被篡改）")
    # 顺带校验固件本身可被虚拟设备解释
    pkg = FirmwarePackage(manifest=manifest, payload=payload)
    pkg.firmware_spec()
    return pkg


def check_compatibility(pkg: FirmwarePackage, current_version: str) -> None:
    """检查设备当前版本是否落在升级包兼容范围内；不满足则抛 IncompatibleError。"""
    m = pkg.manifest
    if _cmp(current_version, m.min_device_version) < 0:
        raise IncompatibleError(
            f"设备版本 {current_version} 低于升级包最低兼容版本 {m.min_device_version}"
        )
    if m.max_device_version is not None and _cmp(current_version, m.max_device_version) > 0:
        raise IncompatibleError(
            f"设备版本 {current_version} 高于升级包最高兼容版本 {m.max_device_version}"
        )
