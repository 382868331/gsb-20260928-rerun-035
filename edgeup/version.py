from __future__ import annotations


def parse_version(text: str) -> tuple[int, ...]:
    """把 1.2.3 这类版本号解析为可比较的整数元组。"""
    parts = []
    for raw in str(text).strip().split("."):
        if not raw.isdigit():
            raise ValueError(f"非法版本号: {text!r}")
        parts.append(int(raw))
    if not parts:
        raise ValueError(f"空版本号: {text!r}")
    return tuple(parts)


def version_at_least(current: str, minimum: str) -> bool:
    cur = parse_version(current)
    need = parse_version(minimum)
    width = max(len(cur), len(need))
    cur += (0,) * (width - len(cur))
    need += (0,) * (width - len(need))
    return cur >= need
