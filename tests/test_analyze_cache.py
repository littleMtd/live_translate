import sqlite3

from scripts.analyze_cache import analyze_cache, main


def _create_db(path):
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE translations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_text TEXT NOT NULL,
            target_text TEXT NOT NULL,
            source_lang TEXT NOT NULL DEFAULT 'ko',
            target_lang TEXT NOT NULL,
            engine TEXT NOT NULL,
            model TEXT NOT NULL,
            hit_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            last_used_at TEXT NOT NULL,
            prompt_version TEXT NOT NULL DEFAULT 'v1'
        )"""
    )
    rows = [
        ("안녕하세요", "大家好", "zh-TW", "gemini", "gemini-pro", 5, "2026-05-14T00:00:00Z", "2026-05-14T00:00:00Z", "p1"),
        ("발로란트", "Valorant", "zh-TW", "gemini", "gemini-pro", 2, "2026-05-14T01:00:00Z", "2026-05-14T01:00:00Z", "p1"),
        ("오류", "這是一段非常非常非常非常非常長的錯誤輸出，明顯比來源長很多，而且還繼續延伸出不必要的內容", "zh-TW", "claude", "claude-3", 0, "2026-05-15T00:00:00Z", "2026-05-15T00:00:00Z", "p2"),
        ("미번역", "미번역", "zh-TW", "claude", "claude-3", 3, "2026-05-15T01:00:00Z", "2026-05-15T01:00:00Z", "p2"),
    ]
    conn.executemany(
        """INSERT INTO translations
           (source_text, target_text, target_lang, engine, model, hit_count,
            created_at, last_used_at, prompt_version)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        rows,
    )
    conn.commit()
    conn.close()


def test_analyze_cache_returns_unavailable_for_missing_db(tmp_path):
    report = analyze_cache(tmp_path / "missing.db")

    assert report["available"] is False
    assert "does not exist" in report["reason"]


def test_analyze_cache_reports_cache_statistics(tmp_path):
    db_path = tmp_path / "cache.db"
    _create_db(db_path)

    report = analyze_cache(db_path, top_n=2)

    assert report["available"] is True
    assert report["total_rows"] == 4
    assert report["total_hits"] == 10
    assert report["by_prompt_version"][0]["prompt_version"] == "p1"
    assert report["by_engine_model_prompt"][0]["engine"] in {"gemini", "claude"}
    assert report["top_sources"][0]["source_text"] == "안녕하세요"
    assert report["daily_inserts"] == [
        {"date": "2026-05-14", "rows": 2},
        {"date": "2026-05-15", "rows": 2},
    ]


def test_analyze_cache_reports_outliers_and_untranslated_rows(tmp_path):
    db_path = tmp_path / "cache.db"
    _create_db(db_path)

    report = analyze_cache(db_path, top_n=5)

    assert report["output_length_outliers"][0]["source_text"] == "오류"
    assert report["suspicious_untranslated"][0]["source_text"] == "미번역"


def test_main_prints_json_report(tmp_path, capsys):
    db_path = tmp_path / "cache.db"
    _create_db(db_path)

    result = main(["--db", str(db_path), "--json"])

    captured = capsys.readouterr()
    assert result == 0
    assert '"total_rows": 4' in captured.out
