from app.core.timeutil import normalize_utc_iso, utc_now_iso


def test_utc_now_has_offset():
    assert utc_now_iso().endswith("+00:00")


def test_normalize_offset_to_utc():
    assert (
        normalize_utc_iso("2026-08-01T00:00:00+09:00")
        == "2026-07-31T15:00:00+00:00"
    )


def test_normalize_z_suffix():
    assert normalize_utc_iso("2026-08-01T00:00:00Z") == "2026-08-01T00:00:00+00:00"


def test_normalize_naive_as_utc():
    assert normalize_utc_iso("2026-08-01T00:00:00") == "2026-08-01T00:00:00+00:00"


def test_normalize_negative_offset():
    assert (
        normalize_utc_iso("2026-08-01T00:00:00-05:00")
        == "2026-08-01T05:00:00+00:00"
    )


def test_normalize_unparseable_becomes_none():
    assert normalize_utc_iso("not-a-date") is None


def test_normalize_none_and_empty():
    assert normalize_utc_iso(None) is None
    assert normalize_utc_iso("") == ""
    assert normalize_utc_iso("   ") is None
