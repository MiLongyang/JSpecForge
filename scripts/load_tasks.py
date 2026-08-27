"""任务加载脚本(P7.2)。

把 ``all-compiled-success.jsonl`` 这类原始数据集加载到 ``tasks`` 表。

用法::

    python scripts/load_tasks.py --jsonl D:/path/to/batch_001.jsonl
    python scripts/load_tasks.py --jsonl xxx.jsonl --reset-pending
    python scripts/load_tasks.py --jsonl xxx.jsonl --config /path/to/config.yaml

字段映射(P6 §6.6):

==========================  ============================
jsonl 字段                  tasks 表字段
==========================  ============================
``source_dataset``          ``source_dataset_name``
``source_index``            ``source_dataset_index``
``task_id``                 ``source_task_id``
``nl_description``          ``nl_description``
``method_code``             ``method_code``
``full_java_code``          ``full_code``
==========================  ============================

附加字段:
- ``source_machine``: 取 ``config.task_loading.source_machine`` (区分多机器)
- ``input_generator_code``: 任务级输入生成器代码(可选,来自再处理数据集)
- ``branch_coverage``: 生成器分支覆盖率(可选,来自再处理数据集)

幂等保证:
- ``tasks`` 表 UNIQUE KEY (source_dataset_name, source_dataset_index)
- 用 ``INSERT IGNORE`` 写入,已存在的行自动跳过
- 重复跑同一份 jsonl 不会产生重复记录

``--reset-pending`` 选项:
- DELETE FROM tasks WHERE status='pending' (注意:**只删 pending,不动
  in_progress / success / failed**)
- 用户在切换数据集前可以用此清空"上次加载但还没跑过"的任务
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))

from jspecforge.config.loader import load_config  # noqa: E402
from jspecforge.config.settings import AppConfig  # noqa: E402
from jspecforge.db.connection import get_connection  # noqa: E402

logger = logging.getLogger("scripts.load_tasks")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="把 jsonl 数据集加载到 tasks 表(幂等)",
    )
    parser.add_argument(
        "--jsonl",
        type=Path,
        required=True,
        help="待加载的 jsonl 文件路径",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=_ROOT / "config.yaml",
        help="config.yaml 路径(默认: 仓库根目录)",
    )
    parser.add_argument(
        "--reset-pending",
        action="store_true",
        help="加载前先 DELETE 所有 status=pending 的任务"
        "(不影响 in_progress / success / failed)",
    )
    parser.add_argument(
        "--max-rows",
        type=int,
        default=0,
        help="只加载前 N 行(调试用),0=全部",
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# jsonl 读取 + 字段映射
# ---------------------------------------------------------------------------


_REQUIRED_JSONL_FIELDS = (
    "source_dataset",
    "source_index",
    "task_id",
    "nl_description",
    "method_code",
    "full_java_code",
)


def parse_jsonl_row(raw: Dict[str, Any]) -> Dict[str, Any]:
    """jsonl 单行 → tasks 表字段 dict。

    Args:
        raw: 一行 JSON 解析后的 dict。

    Returns:
        ``{source_dataset_name, source_dataset_index, source_task_id,
        nl_description, method_code, full_code,
        input_generator_code, branch_coverage}`` 字段映射后的 dict。

    Raises:
        ValueError: 缺少必填字段。
    """
    missing = [k for k in _REQUIRED_JSONL_FIELDS if k not in raw]
    if missing:
        raise ValueError(f"jsonl 行缺少必填字段: {missing}; 原始行: {raw!r}")

    return {
        "source_dataset_name": str(raw["source_dataset"]).strip(),
        "source_dataset_index": int(raw["source_index"]),
        "source_task_id": str(raw["task_id"]).strip(),
        "nl_description": str(raw["nl_description"]),
        "method_code": str(raw["method_code"]),
        "full_code": str(raw["full_java_code"]),
        "input_generator_code": raw.get("input_generator_code"),
        "branch_coverage": (
            float(raw["branch_coverage"]) if raw.get("branch_coverage") is not None else None
        ),
    }


def read_jsonl(path: Path, max_rows: int = 0) -> List[Dict[str, Any]]:
    """逐行读 jsonl,做字段映射,返回 dict 列表。

    Args:
        path: jsonl 文件路径。
        max_rows: 最多读取行数,0 = 全部。

    Returns:
        映射后的 dict 列表。
    """
    if not path.is_file():
        raise FileNotFoundError(f"jsonl 文件不存在: {path}")

    rows: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"jsonl 第 {lineno} 行解析失败: {exc}"
                ) from exc
            try:
                rows.append(parse_jsonl_row(raw))
            except ValueError as exc:
                raise ValueError(f"jsonl 第 {lineno} 行: {exc}") from exc
            if max_rows and len(rows) >= max_rows:
                break

    logger.info("read_jsonl: %s -> %d 行", path, len(rows))
    return rows


# ---------------------------------------------------------------------------
# 入库
# ---------------------------------------------------------------------------


_INSERT_SQL = """
INSERT IGNORE INTO tasks
    (source_dataset_name, source_dataset_index, source_task_id, source_machine,
     nl_description, method_code, full_code, input_generator_code,
     branch_coverage, status)
VALUES
    (%(source_dataset_name)s, %(source_dataset_index)s, %(source_task_id)s,
     %(source_machine)s, %(nl_description)s, %(method_code)s,
     %(full_code)s, %(input_generator_code)s, %(branch_coverage)s, 'pending')
""".strip()


def insert_tasks(
    conn: Any,
    rows: List[Dict[str, Any]],
    *,
    source_machine: str,
) -> Tuple[int, int]:
    """批量 INSERT IGNORE 写入 tasks 表。

    Args:
        conn: PyMySQL 连接。
        rows: parse_jsonl_row 输出的 dict 列表。
        source_machine: tasks.source_machine 字段值
            (``config.task_loading.source_machine``)。

    Returns:
        ``(inserted, skipped)`` —— 实际新增数 + 因 UNIQUE 冲突跳过数。
    """
    if not rows:
        return (0, 0)

    inserted = 0
    with conn.cursor() as cur:
        for row in rows:
            payload = dict(row)
            payload["source_machine"] = source_machine
            affected = cur.execute(_INSERT_SQL, payload)
            inserted += int(affected or 0)
    conn.commit()

    total = len(rows)
    skipped = total - inserted
    return (inserted, skipped)


def reset_pending(conn: Any) -> int:
    """DELETE FROM tasks WHERE status='pending'。

    返回删除行数。**只删 pending**,不影响其它 status。
    """
    with conn.cursor() as cur:
        affected = cur.execute("DELETE FROM tasks WHERE status='pending'")
    conn.commit()
    return int(affected or 0)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def run_load(
    config: AppConfig,
    jsonl_path: Path,
    *,
    reset_pending_first: bool = False,
    max_rows: int = 0,
) -> Dict[str, int]:
    """完整流程:读 jsonl → (可选 reset_pending) → INSERT IGNORE。

    Args:
        config: ``AppConfig``。
        jsonl_path: jsonl 文件路径。
        reset_pending_first: 加载前先清空 pending 任务。
        max_rows: 0 = 全部。

    Returns:
        ``{"read": N, "inserted": M, "skipped": K, "deleted_pending": D}``。
    """
    rows = read_jsonl(jsonl_path, max_rows=max_rows)
    deleted_pending = 0
    inserted = 0
    skipped = 0

    with get_connection(config.database) as conn:
        if reset_pending_first:
            deleted_pending = reset_pending(conn)
            logger.info(
                "reset-pending: 删除 %d 条 status=pending 任务",
                deleted_pending,
            )
        inserted, skipped = insert_tasks(
            conn,
            rows,
            source_machine=config.task_loading.source_machine,
        )

    return {
        "read": len(rows),
        "inserted": inserted,
        "skipped": skipped,
        "deleted_pending": deleted_pending,
    }


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    args = _parse_args()
    config = load_config(args.config)
    stats = run_load(
        config,
        args.jsonl,
        reset_pending_first=args.reset_pending,
        max_rows=args.max_rows,
    )

    logger.info("=" * 60)
    logger.info("加载完成:")
    logger.info("  读取 jsonl 行数:     %d", stats["read"])
    if stats["deleted_pending"]:
        logger.info("  删除 pending 任务:   %d", stats["deleted_pending"])
    logger.info("  新增任务:           %d", stats["inserted"])
    logger.info("  跳过(已存在):       %d", stats["skipped"])
    logger.info("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
