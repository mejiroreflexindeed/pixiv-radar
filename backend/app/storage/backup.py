"""显式路径的 SQLite 备份/恢复工具。

规则（见 docs/PHASE_F_R1_H0_CONTRACT.md WP4 与晚间任务书 WP1/WP2）：
- 只使用 SQLite backup API，覆盖已提交 WAL 内容，不复制原始 .db 文件；
- 本轮只支持"完整合法的 Migration v1~v6 雷达库"：迁移序列必须恰为 1..6、
  必需业务表与关键列必须存在；旧版本/未来版本/有洞/空壳库明确拒绝，不自动迁移；
- 备份先写目标父目录内的独占暂存文件，全部校验通过且连接关闭后才原子发布；
  发布时目标已被其他操作占用则拒绝，绝不覆盖/删除他人的输出；
- 失败只清理本次确实创建并持有的暂存产物；先关连接再清理（Windows 文件锁）；
  原始业务错误不被清理失败掩盖；
- restore 只接收不存在的新目录；异常时不 rmtree 他人创建的目录；
- 受保护库名大小写不敏感；原始路径链上的符号链接在 resolve 之前检查；
- 报告只含 schema 版本、表计数与校验结果，不输出作品/凭据数据。
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import uuid
from pathlib import Path
from urllib.parse import quote

SUPPORTED_VERSIONS = (1, 2, 3, 4, 5, 6)

# 必需业务表 → 关键列（显式规则，来自当前冻结迁移 v1~v6；不凭名称猜测）
REQUIRED_SCHEMA: dict[str, frozenset[str]] = {
    "app_meta": frozenset({"key", "value"}),
    "scan_runs": frozenset(
        {"id", "layer", "status", "started_at", "finished_at", "results_seen", "results_written"}
    ),
    "targets": frozenset({"novel_id", "title", "first_seen_at", "last_seen_at"}),
    "target_tags": frozenset({"novel_id", "name"}),
    "metric_snapshots": frozenset(
        {"novel_id", "scan_run_id", "total_bookmarks", "total_view", "total_comments", "captured_at"}
    ),
    "scan_run_items": frozenset({"scan_run_id", "novel_id"}),
    "scan_leases": frozenset({"resource", "owner"}),
    "tag_scan_state": frozenset({"tag"}),
    "target_states": frozenset({"novel_id", "state"}),
    "target_metrics": frozenset({"novel_id", "scan_run_id", "result"}),
    "state_transitions": frozenset({"novel_id", "from_state", "to_state"}),
    "events": frozenset({"event_key", "type"}),
    "discovery_runs": frozenset({"run_key", "status"}),
    "discovery_sightings": frozenset(
        {"discovery_run_id", "novel_id", "source_kind"}
    ),
    "discovery_query_state": frozenset({"query_key"}),
    "feedback_events": frozenset({"id", "novel_id", "event_type"}),
    "content_documents": frozenset(
        {"novel_id", "content_hash", "body_text", "fetched_at"}
    ),
    "content_features": frozenset(
        {
            "novel_id",
            "content_hash",
            "analyzer_version",
            "language",
            "structure_score",
            "is_clutter",
        }
    ),
}

_PROTECTED_NAMES = {"radar.db", "radar_demo.db"}


class BackupError(ValueError):
    """可安全展示给本地 CLI/UI 的固定错误。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code
        self.cleanup_failed = False
        self.cleanup_paths: list[str] = []


class _CleanupError(OSError):
    def __init__(self, paths: list[Path]) -> None:
        super().__init__("temporary_cleanup_failed")
        self.paths = paths


def _reject_symlinks_raw(raw: Path) -> None:
    """在 resolve 之前沿原始路径链逐段检查符号链接（保留链接信息）。"""
    for part in (raw, *raw.parents):
        try:
            if part.exists() and part.is_symlink():
                raise BackupError("symlink_path_not_allowed")
        except OSError:
            raise BackupError("path_inaccessible")


def _path(value: str | Path) -> Path:
    if not isinstance(value, (str, Path)):
        raise BackupError("path_required")
    raw = Path(value).expanduser()
    _reject_symlinks_raw(raw)
    return raw.resolve(strict=False)


def _reject_symlinks(path: Path) -> None:
    for parent in (path, *path.parents):
        if parent.is_symlink():
            raise BackupError("symlink_path_not_allowed")


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _check_scope(paths: tuple[Path, ...], allowed_root: str | Path | None) -> None:
    if allowed_root is None:
        return
    root = _path(allowed_root)
    _reject_symlinks(root)
    if not all(_within(path, root) for path in paths):
        raise BackupError("path_out_of_scope")


def _is_protected_database(path: Path) -> bool:
    return path.name.lower() in _PROTECTED_NAMES and path.parent.name.lower() == "state"


def _under_data_state(path: Path) -> bool:
    """目标不得位于任意 data/state 目录（含大小写变体），不放行正式目的地。"""
    parts = [part.lower() for part in path.parts]
    return any(
        parts[index] == "data" and parts[index + 1] == "state"
        for index in range(len(parts) - 1)
    )


def _is_explicit_formal_source(
    source: Path, allowed_root: Path | None
) -> bool:
    """仅放行显式、位于允许工作区内的 data/state/radar.db（大小写不敏感）。"""
    if allowed_root is None or source.name.lower() != "radar.db":
        return False
    expected = allowed_root / "data" / "state" / "radar.db"
    return os.path.normcase(str(source)) == os.path.normcase(str(expected))


def _check_source(
    source: Path,
    *,
    allowed_root: Path | None = None,
    allow_formal_source: bool = False,
) -> None:
    _reject_symlinks(source)
    if not source.exists() or not source.is_file():
        raise BackupError("source_not_found")
    if not _is_protected_database(source):
        return
    # 默认行为完全不变：正式/demo 库一律拒绝。显式开关只放行工作区内
    # data/state/radar.db；radar_demo.db、任意其他 state 名与越界路径仍拒绝。
    if not allow_formal_source or not _is_explicit_formal_source(source, allowed_root):
        raise BackupError("formal_database_not_allowed")


def _check_new_file(target: Path) -> None:
    _reject_symlinks(target)
    if target.exists():
        raise BackupError("target_already_exists")
    if not target.parent.exists() or not target.parent.is_dir():
        raise BackupError("target_parent_not_found")


def _validate_schema(conn: sqlite3.Connection) -> dict[str, object]:
    integrity = [row[0] for row in conn.execute("PRAGMA integrity_check").fetchall()]
    if integrity != ["ok"]:
        raise BackupError("integrity_check_failed")
    if conn.execute("PRAGMA foreign_key_check").fetchall():
        raise BackupError("foreign_key_check_failed")

    migrations = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
    ).fetchone()
    if migrations is None:
        raise BackupError("schema_migrations_missing")
    columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(schema_migrations)")
    }
    if "version" not in columns:
        raise BackupError("schema_migrations_invalid")

    versions = [
        row[0]
        for row in conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        )
    ]
    if versions != list(SUPPORTED_VERSIONS):
        raise BackupError("schema_version_unsupported")

    tables = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    for table, required_columns in REQUIRED_SCHEMA.items():
        if table not in tables:
            raise BackupError("schema_incomplete")
        actual = {
            row["name"] for row in conn.execute(f"PRAGMA table_info({table})")
        }
        if not required_columns.issubset(actual):
            raise BackupError("schema_incomplete")

    return {
        "schema_version": SUPPORTED_VERSIONS[-1],
        "table_count": len(tables),
        "integrity": "ok",
        "foreign_keys": "ok",
    }


def _read_metadata(conn: sqlite3.Connection) -> dict[str, object]:
    return _validate_schema(conn)


def _readonly_uri(path: Path) -> str:
    encoded = quote(str(path), safe="/:\\")
    return f"file:{encoded}?mode=ro"


def _exclusive_temp_file(target: Path) -> Path:
    """在目标父目录独占创建暂存文件（O_CREAT|O_EXCL），返回持有路径。"""
    tmp = target.parent / f".{target.name}.tmp-{uuid.uuid4().hex[:12]}"
    fd = os.open(str(tmp), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    os.close(fd)
    return tmp


def _exclusive_temp_dir(target_dir: Path) -> Path:
    """在目标父目录独占创建暂存目录（mkdir 原子），返回持有路径。"""
    tmp = target_dir.parent / f".{target_dir.name}.restore-tmp-{uuid.uuid4().hex[:12]}"
    tmp.mkdir()
    return tmp


def _close_quietly(conn: sqlite3.Connection | None) -> None:
    if conn is not None:
        try:
            conn.close()
        except sqlite3.Error:
            pass


def _prepare_snapshot(conn: sqlite3.Connection) -> None:
    """只将本次暂存库转为单文件快照；绝不改变源库的 WAL 模式。"""
    conn.commit()
    # SQLite backup 会继承源库的 WAL 模式。发布前在独占副本上完成 checkpoint，
    # 由 SQLite 收束 WAL/SHM，避免只重命名主文件而丢下旧名伴生文件。
    mode = conn.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
    if str(mode).lower() != "delete":
        raise BackupError("snapshot_journal_mode_failed")


def _cleanup_own_files(paths: list[Path | None]) -> None:
    """只清理本操作创建并持有的路径；先关连接再删（Windows 文件锁）。

    输入仅限本次独占暂存主文件/目录，不得传入源或发布后的目标。
    中途失败可能尚未转为单文件快照，须一并清理同名 SQLite 伴生文件。
    """
    failed: list[Path] = []
    for path in paths:
        if path is None:
            continue
        if path.is_dir() and not path.is_symlink():
            try:
                shutil.rmtree(path)
            except OSError:
                failed.append(path)
        else:
            # 精确名字，不 glob，不涉及最终目标或源库的 sidecar。
            for item in (path, *(Path(str(path) + suffix) for suffix in ("-wal", "-shm", "-journal"))):
                try:
                    item.unlink(missing_ok=True)
                except OSError:
                    failed.append(item)
    if failed:
        raise _CleanupError(failed)


def _cleanup_after_failure(error: BackupError, paths: list[Path | None]) -> None:
    """保留原始受控 code；清理诊断另行交给调用方和 CLI。"""
    try:
        _cleanup_own_files(paths)
    except OSError as cleanup_error:
        error.cleanup_failed = True
        remaining = getattr(cleanup_error, "paths", None)
        if remaining is None:
            remaining = [path for path in paths if path is not None]
        error.cleanup_paths = [str(path) for path in remaining]


def _publish(tmp: Path, target: Path) -> None:
    """原子发布：目标已被其他操作占用时拒绝，不覆盖、不删除。"""
    try:
        os.rename(str(tmp), str(target))
    except FileExistsError:
        raise BackupError("target_already_exists") from None
    except OSError:
        raise BackupError("publish_failed") from None


def backup_database(
    source: str | Path,
    target: str | Path,
    *,
    allowed_root: str | Path | None = None,
    allow_formal_source: bool = False,
) -> dict[str, object]:
    source_path = _path(source)
    target_path = _path(target)
    _check_scope((source_path, target_path), allowed_root)
    root = _path(allowed_root) if allowed_root is not None else None
    _check_source(
        source_path, allowed_root=root, allow_formal_source=allow_formal_source
    )
    if source_path == target_path:
        raise BackupError("same_path_not_allowed")
    if _under_data_state(target_path):
        raise BackupError("formal_target_not_allowed")
    _check_new_file(target_path)

    source_conn: sqlite3.Connection | None = None
    target_conn: sqlite3.Connection | None = None
    tmp_path: Path | None = None
    try:
        source_conn = sqlite3.connect(_readonly_uri(source_path), uri=True)
        source_conn.row_factory = sqlite3.Row
        metadata = _read_metadata(source_conn)

        # 源校验通过后才创建暂存（本操作独占持有）
        tmp_path = _exclusive_temp_file(target_path)
        target_conn = sqlite3.connect(str(tmp_path))
        source_conn.backup(target_conn)
        _prepare_snapshot(target_conn)
        # 发布/校验前先关闭全部连接（Windows 打开中的文件不可移动/删除）
        _close_quietly(target_conn)
        target_conn = None

        verify_conn = sqlite3.connect(_readonly_uri(tmp_path), uri=True)
        try:
            verify_conn.row_factory = sqlite3.Row
            _read_metadata(verify_conn)
        finally:
            _close_quietly(verify_conn)

        _publish(tmp_path, target_path)
        tmp_path = None  # 已发布，不再清理
    except BackupError as exc:
        _close_quietly(target_conn)
        _close_quietly(source_conn)
        _cleanup_after_failure(exc, [tmp_path])
        raise
    except (sqlite3.Error, OSError):
        _close_quietly(target_conn)
        _close_quietly(source_conn)
        error = BackupError("backup_failed")
        _cleanup_after_failure(error, [tmp_path])
        raise error from None
    finally:
        _close_quietly(target_conn)
        _close_quietly(source_conn)
    return {
        "operation": "backup",
        "source": str(source_path),
        "target": str(target_path),
        **metadata,
        "bytes": target_path.stat().st_size,
    }


def restore_database(
    backup: str | Path,
    target_dir: str | Path,
    *,
    allowed_root: str | Path | None = None,
) -> dict[str, object]:
    backup_path = _path(backup)
    target_dir_path = _path(target_dir)
    target_db_path = target_dir_path / "radar.db"
    _check_scope((backup_path, target_dir_path), allowed_root)
    _check_source(backup_path)
    _reject_symlinks(target_dir_path)
    if _under_data_state(target_dir_path):
        raise BackupError("formal_target_not_allowed")
    if target_dir_path.exists():
        raise BackupError("restore_directory_already_exists")
    if not target_dir_path.parent.exists() or not target_dir_path.parent.is_dir():
        raise BackupError("restore_parent_not_found")

    source_conn: sqlite3.Connection | None = None
    target_conn: sqlite3.Connection | None = None
    tmp_dir: Path | None = None
    try:
        source_conn = sqlite3.connect(_readonly_uri(backup_path), uri=True)
        source_conn.row_factory = sqlite3.Row
        metadata = _read_metadata(source_conn)

        # 备份校验通过后才创建暂存目录（本操作独占持有）
        tmp_dir = _exclusive_temp_dir(target_dir_path)
        tmp_db = tmp_dir / "radar.db"
        target_conn = sqlite3.connect(str(tmp_db))
        source_conn.backup(target_conn)
        _prepare_snapshot(target_conn)
        _close_quietly(target_conn)
        target_conn = None

        verify_conn = sqlite3.connect(_readonly_uri(tmp_db), uri=True)
        try:
            verify_conn.row_factory = sqlite3.Row
            _read_metadata(verify_conn)
        finally:
            _close_quietly(verify_conn)

        _publish(tmp_dir, target_dir_path)
        tmp_dir = None  # 已发布，不再清理
    except BackupError as exc:
        _close_quietly(target_conn)
        _close_quietly(source_conn)
        _cleanup_after_failure(exc, [tmp_dir])
        raise
    except (sqlite3.Error, OSError):
        _close_quietly(target_conn)
        _close_quietly(source_conn)
        error = BackupError("restore_failed")
        _cleanup_after_failure(error, [tmp_dir])
        raise error from None
    finally:
        _close_quietly(target_conn)
        _close_quietly(source_conn)
    return {
        "operation": "restore",
        "source": str(backup_path),
        "target_dir": str(target_dir_path),
        "target": str(target_db_path),
        **metadata,
        "bytes": target_db_path.stat().st_size,
    }
