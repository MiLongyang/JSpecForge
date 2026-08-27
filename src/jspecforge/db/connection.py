"""数据库连接管理。

基于 ``dbutils.pooled_db.PooledDB`` 的线程安全 MySQL 连接池。

主要接口：

- :func:`get_pool` —— 按 ``database`` 名称惰性创建并缓存连接池
- :func:`get_connection` —— 从池中借一个连接（with 语句安全归还）
- :func:`connect_raw` —— 绕过连接池的原生 PyMySQL 连接（建库脚本使用）
- :func:`close_all_pools` —— 关闭所有已创建的池（一般用于程序退出 / 测试 teardown）
"""

from __future__ import annotations

import logging
from threading import Lock
from typing import Any, Dict, Optional

import pymysql
from pymysql.cursors import DictCursor

try:
    from dbutils.pooled_db import PooledDB
except ImportError:
    PooledDB = None  # type: ignore[assignment,misc]

from jspecforge.config.settings import DatabaseConfig

logger = logging.getLogger(__name__)

_pools: Dict[str, PooledDB] = {}
_pools_lock = Lock()


def build_connect_kwargs(
    config: DatabaseConfig,
    db_name: Optional[str],
) -> Dict[str, Any]:
    """构建 PyMySQL connect 的关键字参数。``db_name`` 为 None 时省略 database 字段。"""
    kwargs: Dict[str, Any] = {
        "host": config.host,
        "port": config.port,
        "user": config.user,
        "password": config.password,
        "charset": config.charset,
        "connect_timeout": config.connect_timeout,
        "read_timeout": config.read_timeout,
        "write_timeout": config.write_timeout,
        "autocommit": False,
        "cursorclass": DictCursor,
    }
    if db_name is not None:
        kwargs["database"] = db_name
    return kwargs


def get_pool(
    config: DatabaseConfig,
    db_name: Optional[str] = None,
) -> "PooledDB":
    """获取（或惰性创建）指定数据库的连接池。

    Args:
        config: 数据库配置（提供 host/port/user/password 等）。
        db_name: 目标数据库名。None 时使用 ``config.database``。

    Returns:
        线程安全的 :class:`PooledDB` 实例。
    """
    if PooledDB is None:
        raise ImportError(
            "DBUtils is not installed. Use connect_raw() or install DBUtils."
        )
    target_db = db_name or config.database
    with _pools_lock:
        existing = _pools.get(target_db)
        if existing is not None:
            return existing

        connect_kwargs = build_connect_kwargs(config, target_db)
        pool = PooledDB(
            creator=pymysql,
            mincached=min(2, config.pool_size),
            maxcached=config.pool_size,
            maxconnections=config.pool_size,
            blocking=True,
            ping=1,
            **connect_kwargs,
        )
        _pools[target_db] = pool
        logger.info(
            "created MySQL pool: db=%s, host=%s:%s, pool_size=%d",
            target_db,
            config.host,
            config.port,
            config.pool_size,
        )
        return pool


def get_connection(
    config: DatabaseConfig,
    db_name: Optional[str] = None,
) -> Any:
    """从连接池借一个连接。

    用法::

        with get_connection(config) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
            conn.commit()

    退出 ``with`` 时连接会自动归还到池。
    若 DBUtils 未安装则回退到原生 PyMySQL 连接。
    """
    if PooledDB is None:
        return connect_raw(config, db_name or config.database)
    return get_pool(config, db_name).connection()


def connect_raw(
    config: DatabaseConfig,
    db_name: Optional[str] = None,
) -> pymysql.connections.Connection:
    """创建一个 **不经过连接池** 的原生 PyMySQL 连接。

    主要用于建库脚本——首次运行尚未存在目标数据库，需要先以无 database
    的方式连到 MySQL 服务器并执行 ``CREATE DATABASE``。
    调用方需自行 ``close()`` 或使用 ``with`` 语法。
    """
    return pymysql.connect(**build_connect_kwargs(config, db_name))


def close_all_pools() -> None:
    """关闭并清空所有已创建的连接池。"""
    with _pools_lock:
        for name, pool in list(_pools.items()):
            try:
                pool.close()
            except Exception as exc:  # noqa: BLE001
                logger.warning("close pool failed: db=%s, err=%s", name, exc)
        _pools.clear()
