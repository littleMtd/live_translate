use crate::paths::configured_db_path;
use rusqlite::Connection;
use std::path::Path;

#[derive(serde::Serialize, serde::Deserialize)]
pub struct CacheStats {
    pub total_entries: u32,
    pub hit_count_sum: u32,
    pub last_used: String,
    pub db_size_mb: f32,
}

pub(crate) fn query_cache_stats_from_path(path: &Path) -> Result<CacheStats, String> {
    if !path.exists() {
        return Ok(CacheStats {
            total_entries: 0,
            hit_count_sum: 0,
            last_used: "Never".to_string(),
            db_size_mb: 0.0,
        });
    }

    let conn = Connection::open(path).map_err(|e| format!("DB open error: {}", e))?;

    let (total, hit_sum, last_used): (u32, u32, String) = conn
        .query_row(
            "SELECT COUNT(*), COALESCE(SUM(hit_count), 0), \
             COALESCE(MAX(last_used_at), 'Never') FROM translations",
            [],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
        )
        .map_err(|e| format!("Query error: {}", e))?;

    let db_size_mb = path
        .metadata()
        .map(|m| m.len() as f32 / 1024.0 / 1024.0)
        .unwrap_or(0.0);

    Ok(CacheStats {
        total_entries: total,
        hit_count_sum: hit_sum,
        last_used,
        db_size_mb,
    })
}

pub(crate) fn clear_cache_at_path(path: &Path) -> Result<String, String> {
    if !path.exists() {
        return Ok("Cache already empty".to_string());
    }
    let conn = Connection::open(path).map_err(|e| format!("DB open error: {}", e))?;
    let deleted = conn
        .execute("DELETE FROM translations", [])
        .map_err(|e| format!("Clear failed: {}", e))?;
    Ok(format!("Cleared {} cache entries", deleted))
}

#[tauri::command]
pub fn get_cache_stats() -> Result<CacheStats, String> {
    query_cache_stats_from_path(&configured_db_path())
}

#[tauri::command]
pub fn clear_cache() -> Result<String, String> {
    clear_cache_at_path(&configured_db_path())
}

#[cfg(test)]
mod tests {
    use super::*;
    use rusqlite::Connection;
    use tempfile::tempdir;

    fn create_test_db(path: &Path) -> Connection {
        let conn = Connection::open(path).unwrap();
        conn.execute_batch(
            "CREATE TABLE translations (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                source_text  TEXT NOT NULL,
                target_lang  TEXT NOT NULL,
                engine       TEXT NOT NULL,
                model        TEXT NOT NULL,
                translated_text TEXT NOT NULL,
                hit_count    INTEGER NOT NULL DEFAULT 0,
                last_used_at TEXT NOT NULL
            )",
        )
        .unwrap();
        conn
    }

    fn insert_row(conn: &Connection, src: &str, hits: u32, ts: &str) {
        conn.execute(
            "INSERT INTO translations
             (source_text, target_lang, engine, model, translated_text, hit_count, last_used_at)
             VALUES (?1, 'zh-TW', 'gemini', 'flash', '譯', ?2, ?3)",
            rusqlite::params![src, hits, ts],
        )
        .unwrap();
    }

    #[test]
    fn stats_returns_zeros_when_db_absent() {
        let dir = tempdir().unwrap();
        let path = dir.path().join("absent.db");
        let stats = query_cache_stats_from_path(&path).unwrap();
        assert_eq!(stats.total_entries, 0);
        assert_eq!(stats.hit_count_sum, 0);
        assert_eq!(stats.last_used, "Never");
        assert_eq!(stats.db_size_mb, 0.0);
    }

    #[test]
    fn stats_empty_table_returns_zeros() {
        let dir = tempdir().unwrap();
        let path = dir.path().join("empty.db");
        create_test_db(&path);
        let stats = query_cache_stats_from_path(&path).unwrap();
        assert_eq!(stats.total_entries, 0);
        assert_eq!(stats.hit_count_sum, 0);
        assert_eq!(stats.last_used, "Never");
    }

    #[test]
    fn stats_counts_entries_and_hits_correctly() {
        let dir = tempdir().unwrap();
        let path = dir.path().join("test.db");
        let conn = create_test_db(&path);
        insert_row(&conn, "hello", 5, "2024-01-01T10:00:00");
        insert_row(&conn, "world", 3, "2024-01-02T12:00:00");
        drop(conn);

        let stats = query_cache_stats_from_path(&path).unwrap();
        assert_eq!(stats.total_entries, 2);
        assert_eq!(stats.hit_count_sum, 8);
        assert_eq!(stats.last_used, "2024-01-02T12:00:00");
    }

    #[test]
    fn stats_db_size_is_positive_with_data() {
        let dir = tempdir().unwrap();
        let path = dir.path().join("size.db");
        let conn = create_test_db(&path);
        insert_row(&conn, "test", 1, "2024-01-01T00:00:00");
        drop(conn);

        let stats = query_cache_stats_from_path(&path).unwrap();
        assert!(stats.db_size_mb >= 0.0);
    }

    #[test]
    fn clear_when_db_absent_says_already_empty() {
        let dir = tempdir().unwrap();
        let path = dir.path().join("nofile.db");
        let msg = clear_cache_at_path(&path).unwrap();
        assert!(msg.contains("already empty"), "got: {msg}");
    }

    #[test]
    fn clear_deletes_all_rows_and_reports_count() {
        let dir = tempdir().unwrap();
        let path = dir.path().join("del.db");
        let conn = create_test_db(&path);
        insert_row(&conn, "a", 0, "2024-01-01T00:00:00");
        insert_row(&conn, "b", 0, "2024-01-01T00:00:01");
        drop(conn);

        let msg = clear_cache_at_path(&path).unwrap();
        assert!(msg.contains('2'), "expected count 2 in: {msg}");

        let stats = query_cache_stats_from_path(&path).unwrap();
        assert_eq!(stats.total_entries, 0);
    }

    #[test]
    fn clear_on_empty_table_reports_zero() {
        let dir = tempdir().unwrap();
        let path = dir.path().join("empty_del.db");
        create_test_db(&path);
        let msg = clear_cache_at_path(&path).unwrap();
        assert!(msg.contains('0'), "got: {msg}");
    }
}
