#!/usr/bin/env python
"""创建消融实验的 3 个空库并建表(仅 schema, 无数据)。"""
from __future__ import annotations
import sys
from pathlib import Path
import pymysql
import yaml
from pymysql.constants import CLIENT

_ROOT = Path(__file__).resolve().parent.parent
SCHEMA = _ROOT / "sql" / "jspecforge.sql"
DBS = ["jspecforge_abl_full", "jspecforge_abl_norp", "jspecforge_abl_oj"]


def main() -> int:
    cfg = yaml.safe_load((_ROOT / "config.yaml").read_text(encoding="utf-8"))["database"]
    schema = SCHEMA.read_text(encoding="utf-8")
    server = pymysql.connect(
        host=cfg["host"], port=int(cfg["port"]), user=cfg["user"],
        password=cfg["password"], charset="utf8mb4", autocommit=True,
    )
    for db in DBS:
        with server.cursor() as cur:
            cur.execute(f"CREATE DATABASE IF NOT EXISTS `{db}` "
                        f"CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci")
        print(f"[init] 库 {db} 已就绪")
    server.close()

    for db in DBS:
        conn = pymysql.connect(
            host=cfg["host"], port=int(cfg["port"]), user=cfg["user"],
            password=cfg["password"], database=db, charset="utf8mb4",
            autocommit=False, client_flag=CLIENT.MULTI_STATEMENTS,
        )
        with conn.cursor() as cur:
            cur.execute(schema)
            while cur.nextset():
                pass
        conn.commit()
        conn.close()
        print(f"[init] 库 {db} 建表完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
