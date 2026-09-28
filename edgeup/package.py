"""项目自带的测试升级包：生成、读取与校验，不接云端。

升级包为目录形式（方便观察与按段读取）：

    pkg_1.1.0/
      manifest.json   清单（版本、兼容型号、基线、分片大小、分片哈希）
      chunks/
        0000.bin ...  固件镜像分段

固件镜像内容（image.bin）由生成器合成，虚拟固件在头若干字节编码
版本号与“启动健康”标志，供虚拟 bootloader 判定试启动成败。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .persistence import atomic_write_json, read_json

MANIFEST_NAME = "manifest.json"
CHUNKS_DIR = "chunks"


class PackageError(Exception):
    pass


def build_firmware(version: str, *, healthy: bool = True) -> bytes:
    """合成一个确定性的虚拟固件镜像。

   头部记录版本与健康标志，后续填充带偏移的数据，保证不同包字节不同。
    """
    marker = b"EDGEFW-OK\n" if healthy else b"EDGEFW-BAD\n"
    header = f"version={version}\n".encode("utf-8")
    seed = int(hashlib.sha256(version.encode()).hexdigest(), 16)
    body = bytes(((seed >> (i % 24)) ^ (i * 31)) & 0xFF for i in range(4096))
    return marker + header + body


def build_package(
    out_dir: str | Path,
    *,
    version: str,
    model: str = "edge-x1",
    min_base_version: str = "1.0.0",
    supported_models: list[str] | None = None,
    chunk_size: int = 1024,
    firmware: bytes | None = None,
    healthy: bool = True,
) -> Path:
    """在 out_dir/pkg_<version> 生成升级包，返回包目录路径。"""
    image = firmware if firmware is not None else build_firmware(version, healthy=healthy)
    if chunk_size <= 0:
        raise ValueError("chunk_size 必须为正数")
    pkg_dir = Path(out_dir) / f"pkg_{version}"
    chunks_dir = pkg_dir / CHUNKS_DIR
    chunks_dir.mkdir(parents=True, exist_ok=True)

    chunks = []
    for index, offset in enumerate(range(0, len(image), chunk_size)):
        data = image[offset:offset + chunk_size]
        name = f"{index:04d}.bin"
        (chunks_dir / name).write_bytes(data)
        chunks.append({
            "index": index,
            "name": name,
            "offset": offset,
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        })

    manifest = {
        "package_format": 1,
        "model": model,
        "supported_models": supported_models or [model],
        "version": version,
        "min_base_version": min_base_version,
        "image_size": len(image),
        "image_sha256": hashlib.sha256(image).hexdigest(),
        "chunk_size": chunk_size,
        "chunks": chunks,
    }
    atomic_write_json(pkg_dir / MANIFEST_NAME, manifest)
    return pkg_dir


class PackageReader:
    """读取并严格校验升级包，任何不一致都抛 PackageError。"""

    def __init__(self, pkg_dir: str | Path):
        self.pkg_dir = Path(pkg_dir)
        manifest_path = self.pkg_dir / MANIFEST_NAME
        try:
            self.manifest = read_json(manifest_path)
        except (OSError, json.JSONDecodeError) as exc:
            raise PackageError(f"无法读取 manifest: {exc}") from exc
        if not isinstance(self.manifest, dict):
            raise PackageError("manifest 格式错误")
        self._validate_manifest()

    def _validate_manifest(self) -> None:
        required = {
            "package_format", "supported_models", "version", "min_base_version",
            "image_size", "image_sha256", "chunk_size", "chunks",
        }
        missing = required - self.manifest.keys()
        if missing:
            raise PackageError(f"manifest 缺少字段: {sorted(missing)}")
        chunks = self.manifest["chunks"]
        if not chunks or not isinstance(chunks, list):
            raise PackageError("分片列表为空或格式错误")
        for pos, chunk in enumerate(chunks):
            if chunk["index"] != pos:
                raise PackageError(f"分片顺序错误: 位置 {pos} 编号 {chunk['index']}")
            if pos < len(chunks) - 1 and chunk["size"] != self.manifest["chunk_size"]:
                raise PackageError(f"分片 {pos} 大小与 chunk_size 不符")
            if pos > 0 and chunk["offset"] != chunks[pos - 1]["offset"] + chunks[pos - 1]["size"]:
                raise PackageError(f"分片 {pos} 偏移不连续")
        if chunks[-1]["offset"] + chunks[-1]["size"] != self.manifest["image_size"]:
            raise PackageError("分片总大小与 image_size 不符")

    @property
    def version(self) -> str:
        return self.manifest["version"]

    @property
    def model(self) -> str:
        return self.manifest.get("model", "")

    def check_readable(self) -> None:
        """逐段读取并校验哈希，再做整镜像哈希，确认包本身完整。"""
        whole = hashlib.sha256()
        for chunk in self.manifest["chunks"]:
            path = self.pkg_dir / CHUNKS_DIR / chunk["name"]
            if not path.is_file():
                raise PackageError(f"缺少分片文件: {chunk['name']}")
            data = path.read_bytes()
            if len(data) != chunk["size"]:
                raise PackageError(f"分片 {chunk['index']} 大小错误")
            if hashlib.sha256(data).hexdigest() != chunk["sha256"]:
                raise PackageError(f"分片 {chunk['index']} 哈希不匹配")
            whole.update(data)
        if whole.hexdigest() != self.manifest["image_sha256"]:
            raise PackageError("整镜像哈希不匹配")

    def iter_chunks(self):
        for chunk in self.manifest["chunks"]:
            yield chunk, (self.pkg_dir / CHUNKS_DIR / chunk["name"]).read_bytes()
