"""设备数据与数据格式迁移。

升级不只是换程序：固件自带的数据格式（``data_format``）可能变化，
新版程序需要把旧格式数据迁移成新格式。迁移有两条铁律：

1. **只在隔离副本上迁移**：活动数据目录 ``data/current`` 永远不被原地改动，
   迁移输出到 ``data/trial``。迁移到一半失败、断电，旧数据都完好无损。
2. **提交时整体切换**：健康确认通过后，``current`` 与 ``trial`` 原子对调
   （旧数据改存 ``data/previous`` 作为回滚保底），程序槽指针与数据在同一
   事务点生效，绝不出现“新程序 + 旧数据”或“旧程序 + 新格式”的错配。

内置一条 1 -> 2 的示例迁移（把 ``records`` 里裸字符串包成 ``{id, value}``），
并支持注册表扩展更多格式的迁移函数。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .device import atomic_write_bytes, atomic_write_json, read_json

FACTORY_DATA_FORMAT = 1


class MigrationError(RuntimeError):
    """数据迁移失败（找不到迁移路径、数据内容无法转换等）。"""


# ---------------------------------------------------------------- 迁移函数

def _migrate_1_to_2(doc: dict[str, Any]) -> dict[str, Any]:
    """格式 1 -> 2：records 从裸字符串列表变成 {id, value} 对象列表。"""
    records = doc.get("records")
    if records is None:
        migrated: list[dict[str, Any]] = []
    elif isinstance(records, list):
        migrated = [
            {"id": index, "value": item} if not isinstance(item, dict) else item
            for index, item in enumerate(records)
        ]
    else:
        raise MigrationError("字段 records 不是列表，无法迁移到格式 2")
    return {
        "data_format": 2,
        "records": migrated,
        "migrated_from": doc.get("data_format", 1),
    }


MigrationFn = Callable[[dict[str, Any]], dict[str, Any]]

REGISTRY: dict[tuple[int, int], MigrationFn] = {
    (1, 2): _migrate_1_to_2,
}


def register_migration(from_format: int, to_format: int, fn: MigrationFn) -> None:
    """注册一条自定义迁移函数（适配真实设备时扩展用）。"""
    REGISTRY[(int(from_format), int(to_format))] = fn


def migration_chain(from_format: int, to_format: int) -> list[MigrationFn]:
    """返回 from->to 的顺序迁移链；每一步只升一个大格式号。"""
    if from_format == to_format:
        return []
    if from_format > to_format:
        raise MigrationError(
            f"不支持数据格式降级: {from_format} -> {to_format}（回退请使用旧数据副本）"
        )
    chain: list[MigrationFn] = []
    current = from_format
    while current < to_format:
        step = REGISTRY.get((current, current + 1))
        if step is None:
            raise MigrationError(f"缺少数据格式 {current} -> {current + 1} 的迁移定义")
        chain.append(step)
        current += 1
    return chain


def migrate_document(doc: dict[str, Any], target_format: int) -> dict[str, Any]:
    """把一份数据文档按迁移链转换到目标格式（纯函数，不触碰磁盘）。"""
    try:
        source_format = int(doc.get("data_format", FACTORY_DATA_FORMAT))
    except (TypeError, ValueError) as exc:
        raise MigrationError(f"数据缺少合法的 data_format: {doc.get('data_format')!r}") from exc
    result = dict(doc)
    for step in migration_chain(source_format, target_format):
        result = step(result)
        expected = int(result.get("data_format", 0))
        if expected != source_format + 1:
            raise MigrationError(
                f"迁移函数输出格式 {expected} 与期望 {source_format + 1} 不一致"
            )
        source_format = expected
    if target_format != source_format:
        raise MigrationError(f"迁移结果格式 {source_format} != 目标 {target_format}")
    return result


# ------------------------------------------------------------ 设备数据管理

@dataclass(frozen=True)
class MigrationReport:
    target_format: int
    source_format: int
    migrated: bool


def factory_data() -> dict[str, Any]:
    """出厂示例数据：格式 1，两条裸字符串记录。"""
    return {"data_format": FACTORY_DATA_FORMAT, "records": ["alpha", "beta"]}


class DataStore:
    """管理设备上的数据目录。

    布局::

        data/current/data.json   活动程序正在使用的数据
        data/trial/data.json     试启动期间的隔离迁移副本
        data/previous/data.json  上次升级前的旧格式数据（回滚保底）

    三个目录互斥切换，迁移与试启动过程中 ``current`` 永不被原地修改。
    """

    FILE_NAME = "data.json"

    def __init__(self, root: str | Path):
        self.data_dir = Path(root) / "data"
        self.current_dir = self.data_dir / "current"
        self.trial_dir = self.data_dir / "trial"
        self.previous_dir = self.data_dir / "previous"

    def _path(self, name: str) -> Path:
        return self.data_dir / name / self.FILE_NAME

    # ---------- 初始化 / 读取 ----------

    def initialize(self, doc: dict[str, Any] | None = None) -> None:
        if self.current_dir.exists():
            raise MigrationError(f"设备数据已初始化: {self.data_dir}")
        doc = dict(doc or factory_data())
        atomic_write_json(self._path("current"), doc)

    def _read(self, name: str) -> dict[str, Any]:
        return read_json(self._path(name))

    def current_document(self) -> dict[str, Any]:
        return self._read("current")

    def trial_document(self) -> dict[str, Any] | None:
        path = self._path("trial")
        return read_json(path) if path.exists() else None

    @property
    def current_format(self) -> int:
        return int(self.current_document().get("data_format", FACTORY_DATA_FORMAT))

    def has_trial(self) -> bool:
        return self._path("trial").exists()

    # ---------- 隔离迁移 ----------

    def prepare_trial_copy(self, target_format: int) -> MigrationReport:
        """把 current 复制到 trial，并仅在 trial 上执行迁移。

        每次调用都从 current 重新复制，因此失败包再次投递时不会在
        上次迁移了一半的产物上继续，旧数据始终不受影响。
        """
        source = self.current_document()
        source_format = int(source.get("data_format", FACTORY_DATA_FORMAT))
        self.discard_trial()
        trial_path = self._path("trial")
        atomic_write_json(trial_path, source)  # 先落一份与旧数据一致的副本
        migrated = migrate_document(source, target_format)
        atomic_write_json(trial_path, migrated)  # 再用迁移结果原子替换副本
        return MigrationReport(target_format, source_format, target_format != source_format)

    def discard_trial(self) -> None:
        _remove_tree(self.trial_dir)

    # ---------- 提交 / 回滚（与槽位切换同一事务点） ----------

    def commit_trial(self) -> int:
        """trial 提升为 current；旧 current 存为 previous。返回新格式号。"""
        new_doc = self.trial_document()
        if new_doc is None:
            raise MigrationError("没有待提交的迁移数据")
        _remove_tree(self.previous_dir)
        if self.current_dir.exists():
            self.current_dir.rename(self.previous_dir)
        self.trial_dir.rename(self.current_dir)
        return int(new_doc.get("data_format", FACTORY_DATA_FORMAT))

    def rollback_trial(self) -> None:
        """丢弃 trial；current 从未被修改，旧程序与旧格式数据继续配套运行。"""
        self.discard_trial()


def _remove_tree(path: Path) -> None:
    if not path.exists():
        return
    for child in sorted(path.rglob("*"), reverse=True):
        if child.is_file() or child.is_symlink():
            child.unlink()
        elif child.is_dir():
            child.rmdir()
    path.rmdir()
