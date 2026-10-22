"""SQLite 连接管理。

设计参考 dong-jpg/pixiv-novel-sync（MIT License，见根目录 LICENSE-NOTICE.md）：
- WAL 日志模式，读写并发友好；
- busy_timeout 避免写竞争下的 SQLITE_BUSY；
- 显式开启外键约束；
- 短事务用 BEGIN IMMEDIATE 抢占写锁，网络/计算期间不持有写事务；
- isolation_level=None（自动提交模式）：普通连接不产生隐式事务，
  只有显式 transaction() 才会开启事务。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


def connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(path), timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """显式写事务：BEGIN IMMEDIATE 抢占写锁，退出时统一 commit/rollback。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
