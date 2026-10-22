from app.storage.connection import connect, transaction
from app.storage.migrations import apply_migrations, apply_migrations_up_to


def test_v5_upgrade_adds_content_tables_and_preserves_rows(tmp_path):
    conn = connect(tmp_path / "v5.db")
    try:
        assert apply_migrations_up_to(conn, 5) == [1, 2, 3, 4, 5]
        with transaction(conn):
            conn.execute(
                "INSERT INTO targets (novel_id, title, first_seen_at, last_seen_at) VALUES (1, 'keep', 'a', 'b')"
            )
            conn.execute(
                "INSERT INTO feedback_events (novel_id, event_type, scoring_version, weight, created_at) VALUES (1, 'good', 'feedback-v1', 1.0, 'now')"
            )
        assert apply_migrations(conn) == [6]
        assert conn.execute("SELECT title FROM targets WHERE novel_id=1").fetchone()[0] == "keep"
        assert conn.execute("SELECT event_type FROM feedback_events").fetchone()[0] == "good"
        names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {"content_documents", "content_features"} <= names
        assert apply_migrations(conn) == []
    finally:
        conn.close()


def test_content_rows_cascade_with_target(tmp_path):
    conn = connect(tmp_path / "cascade.db")
    try:
        apply_migrations(conn)
        with transaction(conn):
            conn.execute(
                "INSERT INTO targets (novel_id, title, first_seen_at, last_seen_at) VALUES (1, 'x', 'a', 'b')"
            )
            conn.execute(
                "INSERT INTO content_documents VALUES (1, 'hash', 'body', 'now', NULL)"
            )
            conn.execute(
                "INSERT INTO content_features VALUES (1, 'hash', 'v', 'ja', 4, 1, 0, 0, 1, 0, 0, '{}', '[]', 'now')"
            )
            conn.execute("DELETE FROM targets WHERE novel_id=1")
        assert conn.execute("SELECT COUNT(*) FROM content_documents").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM content_features").fetchone()[0] == 0
    finally:
        conn.close()
