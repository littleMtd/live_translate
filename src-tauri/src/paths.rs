use std::path::{Path, PathBuf};

pub fn app_root() -> PathBuf {
    let base = std::env::current_exe()
        .ok()
        .and_then(|p| p.parent().map(|d| d.to_path_buf()))
        .unwrap_or_else(|| PathBuf::from("."));

    if let Some(project_root) = base.ancestors().nth(3) {
        let root = project_root.to_path_buf();
        if root.join("main.py").exists() {
            return root;
        }
    }

    if base.join("main.py").exists() || base.join("logs").exists() {
        return base;
    }

    base
}

pub fn config_path() -> PathBuf {
    app_root().join("logs").join("live_translate_config.json")
}

pub fn db_path() -> PathBuf {
    app_root().join("logs").join("live_translate.db")
}

pub fn resolve_db_path(root: &Path, configured: &str) -> PathBuf {
    let path = PathBuf::from(configured);
    if path.is_absolute() {
        path
    } else {
        root.join(path)
    }
}

pub fn configured_db_path() -> PathBuf {
    let root = app_root();
    let fallback = db_path();
    let Ok(content) = std::fs::read_to_string(config_path()) else {
        return fallback;
    };
    let Ok(value) = serde_json::from_str::<serde_json::Value>(&content) else {
        return fallback;
    };
    let Some(configured) = value
        .get("database")
        .and_then(|database| database.get("db_path"))
        .and_then(|path| path.as_str())
        .map(str::trim)
        .filter(|path| !path.is_empty())
    else {
        return fallback;
    };

    resolve_db_path(&root, configured)
}

pub fn python_exe() -> PathBuf {
    let venv = app_root()
        .join("live-subtitle-env")
        .join("Scripts")
        .join("python.exe");
    if venv.exists() {
        venv
    } else {
        PathBuf::from("python")
    }
}

pub fn main_py_path() -> PathBuf {
    app_root().join("main.py")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn resolve_db_path_keeps_absolute_path() {
        let path = if cfg!(windows) {
            PathBuf::from(r"C:\tmp\custom.db")
        } else {
            PathBuf::from("/tmp/custom.db")
        };

        assert_eq!(resolve_db_path(Path::new("root"), path.to_str().unwrap()), path);
    }

    #[test]
    fn resolve_db_path_joins_relative_path_to_root() {
        let root = PathBuf::from("project");

        assert_eq!(
            resolve_db_path(&root, "logs/custom.db"),
            root.join("logs").join("custom.db")
        );
    }
}
