"""A/B 槽管理：分段写入 image.bin，恢复时以真实已写内容为准。"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from typing import Callable

from .layout import Layout
from .persistence import atomic_write_json, fsync_file, read_json
from .package import PackageReader


@dataclass
class WriteProgress:
    written_segments: int
    total_segments: int


class SlotManager:
    def __init__(self, layout: Layout):
        self.layout = layout
        self.layout.ensure()

    def read_meta(self, slot: str) -> dict | None:
        return read_json(self.layout.slot_meta(slot))

    def write_whole(self, slot: str, version: str, image: bytes) -> None:
        image_path = self.layout.slot_image(slot)
        with open(image_path, "wb") as fh:
            fh.write(image)
            fsync_file(fh)
        atomic_write_json(self.layout.slot_meta(slot), {
            "version": version,
            "image_size": len(image),
            "image_sha256": hashlib.sha256(image).hexdigest(),
            "complete": True,
        })

    def clear_slot(self, slot: str) -> None:
        image_path = self.layout.slot_image(slot)
        if image_path.exists():
            image_path.unlink()
        meta_path = self.layout.slot_meta(slot)
        if meta_path.exists():
            meta_path.unlink()

    @staticmethod
    def _verify_region(path: os.PathLike | str, offset: int, expect: bytes) -> bool:
        """读取 image.bin 在 offset 处的真实字节并与该分片比较。"""
        try:
            with open(path, "rb") as fh:
                fh.seek(offset)
                return fh.read(len(expect)) == expect
        except OSError:
            return False

    def staged_segments(self, slot: str, package: PackageReader) -> int:
        """依据真实已写内容返回连续完好的分片数（不看任何成功标记）。"""
        image_path = self.layout.slot_image(slot)
        if not image_path.exists():
            return 0
        # 注意：即使 meta 声称已完成，也必须逐段核对真实字节，
        # 成功标记本身不作为恢复依据。
        good = 0
        for chunk, data in package.iter_chunks():
            if not self._verify_region(image_path, chunk["offset"], data):
                break
            good += 1
        return good

    def write_segmented(
        self,
        slot: str,
        package: PackageReader,
        *,
        resume: bool = True,
        crash_after_segment: int | None = None,
        on_segment: Callable[[WriteProgress], None] | None = None,
    ) -> WriteProgress:
        """把包分段写入槽内 image.bin。

        resume=True 时先按真实内容跳过已完好写入的分片；
        crash_after_segment 指定写完第 N 个分片后模拟掉电（抛 SimulatedPowerCut）。
        """
        from .updater import SimulatedPowerCut

        chunks = package.manifest["chunks"]
        image_path = self.layout.slot_image(slot)
        image_path.parent.mkdir(parents=True, exist_ok=True)
        start_index = self.staged_segments(slot, package) if resume else 0

        if not resume and image_path.exists():
            image_path.unlink()
        target_size = package.manifest["image_size"]
        # 始终截断到目标长度：备用槽可能残留上一次失败升级留下的更长镜像。
        # 截断不影响恢复正确性——已写分片随后仍按真实字节逐段比对。
        with open(image_path, "r+b" if image_path.exists() else "w+b") as fh:
            fh.truncate(target_size)
            fsync_file(fh)

        for chunk, data in package.iter_chunks():
            index = chunk["index"]
            if index < start_index:
                continue
            with open(image_path, "r+b") as fh:
                fh.seek(chunk["offset"])
                fh.write(data)
                fsync_file(fh)
            if on_segment:
                on_segment(WriteProgress(index + 1, len(chunks)))
            if crash_after_segment is not None and index + 1 == crash_after_segment:
                raise SimulatedPowerCut(f"第 {index + 1} 个分片落盘后断电")

        written = self.staged_segments(slot, package)
        if written != len(chunks):
            raise RuntimeError("分片写入后校验失败")
        image_hash = self._hash_image(image_path)
        if image_hash != package.manifest["image_sha256"]:
            raise RuntimeError("image.bin 整镜像哈希不匹配")
        atomic_write_json(self.layout.slot_meta(slot), {
            "version": package.version,
            "image_size": target_size,
            "image_sha256": image_hash,
            "complete": True,
        })
        return WriteProgress(len(chunks), len(chunks))

    @staticmethod
    def _hash_image(path: os.PathLike | str) -> str:
        digest = hashlib.sha256()
        with open(path, "rb") as fh:
            for block in iter(lambda: fh.read(65536), b""):
                digest.update(block)
        return digest.hexdigest()

    def image_complete(self, slot: str, package: PackageReader) -> bool:
        meta = self.read_meta(slot)
        image_path = self.layout.slot_image(slot)
        if not image_path.exists():
            return False
        if self._hash_image(image_path) != package.manifest["image_sha256"]:
            return False
        return bool(meta and meta.get("complete") and meta.get("version") == package.version)
