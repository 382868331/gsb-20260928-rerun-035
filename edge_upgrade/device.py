"""虚拟设备：本地文件模拟 A/B 分区槽、分段写入、启动与健康检查。

磁盘布局::

    root/
      boot/
        active        已确认的活动槽（"A" 或 "B"），原子替换
        trial.json    试启动标记 {slot, version, data_format, attempts, verified}
                       存在即表示待确认；程序槽与数据槽按名字一一配对
      slots/
        A/firmware.bin, A/info.json
        B/firmware.bin, B/info.json
      data/
        A/data.json, A/info.json     与槽配对的业务数据及格式版本
        B/data.json, B/info.json
      staging/
        firmware.part  分段下载/写入的暂存区
        expected.json  目标槽、期望大小与 sha256

所有“已写内容”以磁盘上 staging 文件的真实字节为准，内存里不缓存固件，
因此进程重启（模拟断电）后可以直接按真实长度续传。

升级事务以“槽”为单位：固件和该版本对应的格式数据放在同一个槽名下面。
提交只是原子切换活动指针；回退时旧槽的程序和旧格式数据都原封不动，
不会出现“程序回退了、数据却是新格式”的错配。
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .migration import INITIAL_FORMAT, initial_dataset
from .package import make_firmware_payload

SLOT_A = "A"
SLOT_B = "B"


class DeviceError(RuntimeError):
    """设备侧操作失败（槽损坏、写入断档等）。"""


@dataclass(frozen=True)
class BootResult:
    ok: bool
    slot: str
    version: str | None
    healthy: bool
    reason: str | None = None

    def __bool__(self) -> bool:  # 方便 if result: 判读
        return self.ok and self.healthy


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """同目录临时文件 + rename + fsync，保证原子落盘。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    _fsync_dir(path.parent)


def atomic_write_json(path: Path, obj: Any) -> None:
    atomic_write_bytes(path, json.dumps(obj, ensure_ascii=False, sort_keys=True).encode("utf-8"))


def read_json(path: Path) -> Any:
    with open(path, "rb") as f:
        return json.loads(f.read().decode("utf-8"))


class VirtualDevice:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.boot_dir = self.root / "boot"
        self.slots_dir = self.root / "slots"
        self.data_dir = self.root / "data"
        self.staging_dir = self.root / "staging"
        self.active_path = self.boot_dir / "active"
        self.trial_path = self.boot_dir / "trial.json"
        self.staging_path = self.staging_dir / "firmware.part"
        self.expected_path = self.staging_dir / "expected.json"

    # ---------- 初始化与基本信息 ----------

    @classmethod
    def initialize(cls, root: str | Path, factory_version: str = "1.0.0") -> "VirtualDevice":
        """出厂初始化：A 槽写入出厂固件，A 为活动槽。"""
        dev = cls(root)
        if dev.active_path.exists():
            raise DeviceError(f"设备已初始化: {dev.root}")
        dev.boot_dir.mkdir(parents=True, exist_ok=True)
        payload = make_firmware_payload(factory_version)
        dev._write_slot(SLOT_A, payload)
        dev._write_data(SLOT_A, initial_dataset(), INITIAL_FORMAT)
        atomic_write_bytes(dev.active_path, SLOT_A.encode("ascii"))
        return dev

    @property
    def active_slot(self) -> str:
        if not self.active_path.exists():
            raise DeviceError("设备尚未初始化")
        return self.active_path.read_text(encoding="ascii").strip()

    @property
    def inactive_slot(self) -> str:
        return SLOT_B if self.active_slot == SLOT_A else SLOT_A

    def slot_version(self, slot: str) -> str | None:
        info = self._slot_dir(slot) / "info.json"
        if not info.exists():
            return None
        return str(read_json(info).get("version"))

    @property
    def current_version(self) -> str:
        version = self.slot_version(self.active_slot)
        if version is None:
            raise DeviceError(f"活动槽 {self.active_slot} 缺少固件信息")
        return version

    def _slot_dir(self, slot: str) -> Path:
        if slot not in (SLOT_A, SLOT_B):
            raise DeviceError(f"未知槽: {slot}")
        return self.slots_dir / slot

    def _write_slot(self, slot: str, payload: bytes) -> None:
        slot_dir = self._slot_dir(slot)
        slot_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(slot_dir / "firmware.bin", payload)
        atomic_write_json(
            slot_dir / "info.json",
            {"version": json.loads(payload.decode("utf-8"))["version"],
             "sha256": hashlib.sha256(payload).hexdigest()},
        )

    # ---------- 数据槽（与程序槽配对，承载业务数据及其格式版本） ----------

    def _data_dir(self, slot: str) -> Path:
        if slot not in (SLOT_A, SLOT_B):
            raise DeviceError(f"未知槽: {slot}")
        return self.data_dir / slot

    def data_format(self, slot: str) -> int | None:
        info = self._data_dir(slot) / "info.json"
        if not info.exists():
            return None
        return int(read_json(info).get("format"))

    def read_data(self, slot: str) -> dict[str, Any]:
        data_file = self._data_dir(slot) / "data.json"
        if not data_file.exists():
            raise DeviceError(f"槽 {slot} 没有业务数据")
        return read_json(data_file)

    def _write_data(self, slot: str, doc: dict[str, Any], data_format: int) -> None:
        """原子写入一个槽的数据与格式信息（供初始化和迁移落盘用）。"""
        directory = self._data_dir(slot)
        directory.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(doc, ensure_ascii=False, sort_keys=True).encode("utf-8")
        atomic_write_bytes(directory / "data.json", payload)
        atomic_write_json(
            directory / "info.json",
            {"format": int(data_format),
             "sha256": hashlib.sha256(payload).hexdigest()},
        )

    def clone_data(self, source_slot: str, target_slot: str) -> dict[str, Any]:
        """把源槽数据复制一份隔离副本到目标槽，返回副本内容。

        目标槽的数据会被整体替换（原子写），源槽数据从不修改。
        """
        fmt = self.data_format(source_slot)
        if fmt is None:
            raise DeviceError(f"源槽 {source_slot} 缺少数据格式信息，无法迁移")
        doc = self.read_data(source_slot)
        clone = json.loads(json.dumps(doc))  # 深拷贝，杜绝引用别名
        self._write_data(target_slot, clone, fmt)
        return clone

    def migrate_data_in_slot(
        self,
        slot: str,
        migrated: dict[str, Any],
        new_format: int,
    ) -> None:
        """把已经迁移好的数据原子落盘到该槽，替换其隔离副本。"""
        old_format = self.data_format(slot)
        if old_format is None:
            raise DeviceError(f"槽 {slot} 尚无数据副本，无法写入迁移结果")
        self._write_data(slot, migrated, new_format)

    def discard_slot(self, slot: str) -> None:
        """删除非活动槽上的固件与数据（回退清理；活动槽永远不会被调用）。"""
        import shutil

        if slot == self.active_slot:
            raise DeviceError("拒绝清理当前活动槽")
        for directory in (self._slot_dir(slot), self._data_dir(slot)):
            shutil.rmtree(directory, ignore_errors=True)

    def read_slot_firmware(self, slot: str) -> bytes:
        fw = self._slot_dir(slot) / "firmware.bin"
        if not fw.exists():
            raise DeviceError(f"槽 {slot} 没有固件")
        return fw.read_bytes()

    # ---------- 分段暂存（断电可续） ----------

    def prepare_staging(self, target_slot: str, expected_size: int, expected_sha256: str) -> None:
        """开始一次新的写入：清空暂存并登记期望信息（落盘）。"""
        self.discard_staging()
        self.staging_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(
            self.expected_path,
            {"target_slot": target_slot, "size": expected_size, "sha256": expected_sha256},
        )
        atomic_write_bytes(self.staging_path, b"")

    def discard_staging(self) -> None:
        for p in (self.staging_path, self.expected_path):
            try:
                p.unlink()
            except FileNotFoundError:
                pass
        tmp = self.staging_dir / (self.staging_path.name + ".tmp")
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass

    def staging_info(self) -> dict[str, Any] | None:
        if not self.expected_path.exists():
            return None
        info = read_json(self.expected_path)
        info["bytes_written"] = self.staging_size()
        return info

    def staging_size(self) -> int:
        """以磁盘真实文件长度为准。"""
        try:
            return self.staging_path.stat().st_size
        except FileNotFoundError:
            return 0

    def staging_tail(self, length: int) -> bytes:
        """读取暂存区末尾 length 字节，用于重复分片的内容比对。"""
        size = self.staging_size()
        length = min(length, size)
        with open(self.staging_path, "rb") as f:
            f.seek(size - length)
            return f.read(length)

    def write_segment(self, offset: int, data: bytes) -> int:
        """在 offset 处顺序追加写入一个分片；offset 必须等于真实已写长度。

        返回复盖后的总写入长度。允许同一分片重传（offset 小于已写长度），
        但重传内容必须与已落盘字节完全一致，否则视为冲突/损坏。
        """
        if not self.expected_path.exists():
            raise DeviceError("暂存区尚未准备，无法写入分片")
        info = read_json(self.expected_path)
        written = self.staging_size()
        if offset > written:
            raise DeviceError(f"分片断档: 请求 offset={offset}, 实际已写={written}")
        if offset < written:
            overlap = written - offset
            if len(data) <= overlap:
                if self.staging_tail_overlap(offset, data) != data:
                    raise DeviceError("重传分片内容与已写入内容不一致")
                return written
            head, tail = data[:overlap], data[overlap:]
            if self.staging_tail_overlap(offset, head) != head:
                raise DeviceError("重传分片内容与已写入内容不一致")
            data = tail
            offset = written
        if written + len(data) > int(info["size"]):
            raise DeviceError(
                f"写入超过声明大小: {written + len(data)} > {info['size']}"
            )
        with open(self.staging_path, "ab") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        return self.staging_size()

    def staging_tail_overlap(self, offset: int, data: bytes) -> bytes:
        size = self.staging_size()
        with open(self.staging_path, "rb") as f:
            f.seek(offset)
            return f.read(min(len(data), size - offset))

    def verify_and_install(self) -> str:
        """校验暂存固件大小+sha256（真实内容），原子安装到目标非活动槽。"""
        info = self.staging_info()
        if info is None:
            raise DeviceError("没有可安装的暂存固件")
        payload = self.staging_path.read_bytes()
        if len(payload) != info["size"]:
            raise DeviceError(
                f"暂存固件不完整: {len(payload)}/{info['size']}，不能安装"
            )
        digest = hashlib.sha256(payload).hexdigest()
        if digest != info["sha256"]:
            raise DeviceError("暂存固件 sha256 校验失败，拒绝安装")
        target = info["target_slot"]
        self._write_slot(target, payload)  # 原子替换；旧活动槽完全不动
        self.discard_staging()
        return target

    # ---------- 试启动 / 确认 / 回退 ----------

    def begin_trial(self, slot: str, version: str, data_format: int) -> None:
        if slot == self.active_slot:
            raise DeviceError("试启动槽不能是当前活动槽")
        atomic_write_json(
            self.trial_path,
            {"slot": slot, "version": version,
             "data_format": int(data_format), "attempts": 0, "verified": False},
        )

    def trial(self) -> dict[str, Any] | None:
        return read_json(self.trial_path) if self.trial_path.exists() else None

    def clear_trial(self) -> None:
        try:
            self.trial_path.unlink()
        except FileNotFoundError:
            pass
        _fsync_dir(self.trial_path.parent)

    def boot(self) -> BootResult:
        """（模拟一次上电启动）有试启动标记则从候选槽启动并累计尝试次数。"""
        trial = self.trial()
        if trial is not None:
            slot = trial["slot"]
            trial["attempts"] = int(trial.get("attempts", 0)) + 1
        else:
            slot = self.active_slot
        try:
            spec = json.loads(self.read_slot_firmware(slot).decode("utf-8"))
        except (DeviceError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            result = BootResult(False, slot, None, False, f"固件无法加载: {exc}")
            if trial is not None:
                atomic_write_json(self.trial_path, trial)
            return result
        version = spec.get("version")
        if not spec.get("boot_ok", True):
            result = BootResult(False, slot, version, False, "固件自启动检查失败")
            if trial is not None:
                atomic_write_json(self.trial_path, trial)
            return result
        # 试启动时必须核对：槽内固件版本、配套数据格式与数据本身都要能对上，
        # 任何一项不一致都按启动失败处理（事务不能带着错配数据确认）。
        if trial is not None:
            expected_fmt = int(trial.get("data_format", -1))
            actual_fmt = self.data_format(slot)
            if actual_fmt != expected_fmt:
                trial["attempts"] = int(trial.get("attempts", 0))
                atomic_write_json(self.trial_path, trial)
                return BootResult(
                    False, slot, version, False,
                    f"数据格式 {actual_fmt} 与新版本要求 {expected_fmt} 不一致",
                )
            try:
                self.read_data(slot)
            except (DeviceError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                atomic_write_json(self.trial_path, trial)
                return BootResult(False, slot, version, False,
                                  f"配套数据无法加载: {exc}")
        healthy = bool(spec.get("health_ok", True))
        if not healthy:
            result = BootResult(True, slot, version, False, "健康检查失败")
        else:
            result = BootResult(True, slot, version, True)
            if trial is not None:
                trial["verified"] = True  # 健康检查通过，只待外部确认
        if trial is not None:
            atomic_write_json(self.trial_path, trial)
        return result

    def commit_trial(self) -> str:
        """确认新版本可用：活动指针切到试启动槽（固件+数据同槽一起切换）。"""
        trial = self.trial()
        if trial is None:
            raise DeviceError("没有待确认的试启动")
        if not trial.get("verified"):
            raise DeviceError("试启动尚未通过健康检查，不能确认")
        slot = trial["slot"]
        atomic_write_bytes(self.active_path, slot.encode("ascii"))
        self.clear_trial()
        return slot

    def rollback_trial(self) -> str:
        """回退：丢弃试启动标记，活动指针维持旧槽不动（旧版本从未被破坏）。"""
        self.clear_trial()
        return self.active_slot
