"""数据格式迁移：在隔离副本上把旧格式数据逐级转换成新格式。

迁移函数必须是**纯函数**：只接收/返回内存中的数据集对象，绝不直接改设备上
正在使用的数据目录。编排层先复制旧数据、再在副本上迁移，因此：

- 迁移失败时，活动槽上的旧数据一个字节都不会变；
- 健康确认前若回退，旧程序与旧格式数据继续一起工作。

格式约定（虚拟设备）：数据集是一个 JSON 对象，``records`` 为记录列表，
对象里的 ``format`` 字段（与数据一同落盘的 info.json）标记格式版本。

当前注册的迁移：

- v1 -> v2：记录字段 ``value`` 改名为 ``text``，并补 ``schema: 2``。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

INITIAL_FORMAT = 1


class MigrationError(RuntimeError):
    """数据迁移失败（无可用迁移路径或数据内容不符合预期）。"""


def initial_dataset() -> dict[str, Any]:
    """出厂设备自带的 v1 示例数据。"""
    return {
        "records": [
            {"id": 1, "value": "alpha"},
            {"id": 2, "value": "beta"},
        ]
    }


def _v1_to_v2(doc: dict[str, Any]) -> dict[str, Any]:
    records = doc.get("records")
    if not isinstance(records, list):
        raise MigrationError("v1 数据缺少 records 列表，无法迁移到 v2")
    migrated: list[dict[str, Any]] = []
    for record in records:
        if not isinstance(record, dict) or "value" not in record:
            raise MigrationError(f"v1 记录结构非法，无法迁移: {record!r}")
        new_record = {k: v for k, v in record.items() if k != "value"}
        new_record["text"] = record["value"]
        new_record["schema"] = 2
        migrated.append(new_record)
    return {"records": migrated}


MIGRATIONS: dict[int, Callable[[dict[str, Any]], dict[str, Any]]] = {
    2: _v1_to_v2,
}


def migrate_dataset(doc: dict[str, Any], from_format: int, to_format: int) -> dict[str, Any]:
    """把数据集从 from_format 逐级迁移到 to_format，返回新对象（不改输入）。"""
    if from_format == to_format:
        return dict(doc)
    if from_format > to_format:
        raise MigrationError(
            f"不支持降级数据格式: {from_format} -> {to_format}"
        )
    current = dict(doc)
    for fmt in range(from_format + 1, to_format + 1):
        step = MIGRATIONS.get(fmt)
        if step is None:
            raise MigrationError(f"缺少数据格式迁移步骤: -> {fmt}")
        current = step(current)
    return current


def migration_path_exists(from_format: int, to_format: int) -> bool:
    """是否存在从 from_format 到 to_format 的迁移链（不触碰真实数据）。"""
    if from_format > to_format or from_format < INITIAL_FORMAT:
        return False
    return all(fmt in MIGRATIONS for fmt in range(from_format + 1, to_format + 1))
