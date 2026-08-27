#!/usr/bin/env python
"""合并消融实验某一组的多台服务器 SQL dump 到单个本地数据库。

背景: 每组实验在 5 台云服务器上跑同一数据集的互斥切片。每台服务器的
tasks.id 都从 1 自增, 因此 5 份 dump 的 task_id 互相冲突, 不能直接拼接。
本脚本按唯一键 (source_dataset_name, source_dataset_index) 做去重, 并给
tasks 重新分配自增 id, 同时把子表的 task_id 一并重映射。

用法::
    python scripts/merge_ablation.py \
        --group-dir "a/实验结果/full" \
        --target-db jspecforge_abl_full \
        --schema-file sql/jspecforge.sql \
        --config config.yaml

会依次处理 <group-dir>/<1..N>/sql/*/jspecforge_full.sql。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pymysql
import yaml
from pymysql.constants import CLIENT

_ROOT = Path(__file__).resolve().parent.parent

CHILD_TABLES = [
    "task_llm_messages",
    "pbt_attempts",
    "rac_attempts",
    "oj_attempts",
    "oj_bug_reports",
]


def load_db_cfg(config_path: Path) -> Dict[str, Any]:
    cfg = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    return cfg["database"]


def connect(db_cfg: Dict[str, Any], database: Optional[str] = None,
            multi: bool = False) -> pymysql.Connection:
    kwargs: Dict[str, Any] = dict(
        host=db_cfg["host"],
        port=int(db_cfg["port"]),
        user=db_cfg["user"],
        password=db_cfg["password"],
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
        read_timeout=300,
        write_timeout=300,
    )
    if database:
        kwargs["database"] = database
    if multi:
        kwargs["client_flag"] = CLIENT.MULTI_STATEMENTS
    return pymysql.connect(**kwargs)


def ensure_target_schema(db_cfg: Dict[str, Any], target_db: str, schema_file: Path) -> None:
    """建目标库(如不存在)并执行建表 SQL(幂等, DROP TABLE IF EXISTS)。"""
    conn = connect(db_cfg)
    try:
        with conn.cursor() as cur:
            cur.execute(f"CREATE DATABASE IF NOT EXISTS `{target_db}` "
                        f"CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci")
        conn.commit()
    finally:
        conn.close()

    schema_sql = schema_file.read_text(encoding="utf-8")
    conn = connect(db_cfg, target_db, multi=True)
    try:
        with conn.cursor() as cur:
            cur.execute(schema_sql)
            while cur.nextset():
                pass
        conn.commit()
    finally:
        conn.close()
    print(f"[schema] 目标库 {target_db} 建表完成")


def import_dump_into_temp(db_cfg: Dict[str, Any], temp_db: str, dump_path: Path) -> None:
    """把一份 mysqldump 导入临时库(用 MULTI_STATEMENTS 原生执行)。"""
    sql = dump_path.read_text(encoding="utf-8")
    conn = connect(db_cfg)
    try:
        with conn.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS `{temp_db}`")
            cur.execute(f"CREATE DATABASE `{temp_db}` "
                        f"CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci")
        conn.commit()
    finally:
        conn.close()

    conn = connect(db_cfg, temp_db, multi=True)
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            while cur.nextset():
                pass
        conn.commit()
    finally:
        conn.close()


def read_rows(conn: pymysql.Connection, table: str) -> List[Dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(f"SELECT * FROM `{table}`")
        return list(cur.fetchall())


def insert_row(conn: pymysql.Connection, table: str, row: Dict[str, Any],
               id_remap: Dict[int, int]) -> int:
    """插入一行, 丢弃原 id(自动重分配), 重映射 task_id。返回新 id。"""
    data = dict(row)
    data.pop("id", None)
    if "task_id" in data and data["task_id"] is not None:
        data["task_id"] = id_remap.get(int(data["task_id"]))
    cols = list(data.keys())
    placeholders = ", ".join(["%s"] * len(cols))
    sql = f"INSERT INTO `{table}` (`{'`, `'.join(cols)}`) VALUES ({placeholders})"
    with conn.cursor() as cur:
        cur.execute(sql, [data[c] for c in cols])
        last_id = cur.lastrowid
    return last_id


def merge_one_dump(db_cfg: Dict[str, Any], target_db: str, dump_path: Path,
                   seen_keys: set, stats: Dict[str, int], temp_db: str) -> None:
    import_dump_into_temp(db_cfg, temp_db, dump_path)
    tmp_conn = connect(db_cfg, temp_db)
    tgt_conn = connect(db_cfg, target_db)
    try:
        tasks = read_rows(tmp_conn, "tasks")
        id_remap: Dict[int, int] = {}
        for t in tasks:
            key = (t["source_dataset_name"], t["source_dataset_index"])
            if key in seen_keys:
                stats["tasks_skipped"] += 1
                continue
            seen_keys.add(key)
            old_id = int(t["id"])
            new_id = insert_row(tgt_conn, "tasks", t, id_remap)
            id_remap[old_id] = new_id
            stats["tasks_imported"] += 1
        for child in CHILD_TABLES:
            rows = read_rows(tmp_conn, child)
            for r in rows:
                old_tid = int(r.get("task_id") or 0)
                if old_tid not in id_remap:
                    # 父任务被去重跳过, 子行一并跳过
                    continue
                insert_row(tgt_conn, child, r, id_remap)
                stats["child_rows"] += 1
        tgt_conn.commit()
    finally:
        tmp_conn.close()
        tgt_conn.close()
    conn = connect(db_cfg)
    try:
        with conn.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS `{temp_db}`")
        conn.commit()
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="合并一组实验的多台服务器 SQL dump")
    ap.add_argument("--group-dir", type=Path, required=True)
    ap.add_argument("--target-db", type=str, required=True)
    ap.add_argument("--schema-file", type=Path, default=_ROOT / "sql" / "jspecforge.sql")
    ap.add_argument("--config", type=Path, default=_ROOT / "config.yaml")
    ap.add_argument("--temp-db", type=str, default="jspecforge_merge_tmp")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    db_cfg = load_db_cfg(args.config)
    dumps = sorted(
        list(args.group_dir.glob("*/sql/*/jspecforge_full.sql")),
        key=lambda p: p.as_posix(),
    )
    if not dumps:
        print(f"[错误] {args.group_dir} 下未找到任何 jspecforge_full.sql")
        return 1
    print(f"[merge] 发现 {len(dumps)} 份 dump:")
    for d in dumps:
        print(f"    - {d}")

    if not args.dry_run:
        ensure_target_schema(db_cfg, args.target_db, args.schema_file)

    seen_keys: set = set()
    stats = {"tasks_imported": 0, "tasks_skipped": 0, "child_rows": 0}
    for i, d in enumerate(dumps, 1):
        print(f"[merge] 处理 {i}/{len(dumps)}: {d.parent.parent.parent.name}")
        if not args.dry_run:
            merge_one_dump(db_cfg, args.target_db, d, seen_keys, stats,
                           f"{args.temp_db}_{i}")

    print("=" * 60)
    print(f"合并完成 → 目标库 {args.target_db}")
    print(f"  tasks 导入: {stats['tasks_imported']}")
    print(f"  tasks 跳过(重复): {stats['tasks_skipped']}")
    print(f"  子表行导入: {stats['child_rows']}")
    if args.dry_run:
        print("  [DRY-RUN, 未实际写入]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
