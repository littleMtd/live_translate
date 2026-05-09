use crate::state::AppState;
use std::io::{BufRead, BufReader};
use std::path::PathBuf;
use std::process::{Command, Stdio};
use std::thread;
use tauri::State;

fn app_root() -> PathBuf {
    let base = std::env::current_exe()
        .ok()
        .and_then(|p| p.parent().map(|d| d.to_path_buf()))
        .unwrap_or_else(|| PathBuf::from("."));

    // Dev mode: .../live_translate/src-tauri/target/debug/<app>.exe
    // We need the project root: .../live_translate
    if let Some(project_root) = base.ancestors().nth(3) {
        let root = project_root.to_path_buf();
        if root.join("main.py").exists() {
            return root;
        }
    }

    // Packaged mode: binary sits next to app files.
    if base.join("main.py").exists() {
        return base;
    }

    base
}

fn python_exe() -> PathBuf {
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

fn main_py_path() -> PathBuf {
    app_root().join("main.py")
}

pub(crate) fn is_python_running(state: &AppState) -> bool {
    state.python_process.lock().unwrap().is_some()
}

pub(crate) fn do_stop_python(state: &AppState) -> Result<String, String> {
    let mut guard = state.python_process.lock().unwrap();
    if let Some(mut proc) = guard.take() {
        proc.kill()
            .map_err(|e| format!("Failed to stop Python: {}", e))?;
        proc.wait()
            .map_err(|e| format!("Wait error: {}", e))?;
        Ok("Python process stopped".to_string())
    } else {
        Err("No Python process running".to_string())
    }
}

#[tauri::command]
pub fn start_python(state: State<AppState>) -> Result<String, String> {
    if is_python_running(&state) {
        return Err("Python is already running".to_string());
    }

    let mut proc = Command::new(python_exe())
        .arg("-u")
        .arg(main_py_path())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| format!("Failed to spawn Python: {}", e))?;

    let pid = proc.id();

    if let Some(stdout) = proc.stdout.take() {
        thread::spawn(|| {
            let reader = BufReader::new(stdout);
            for line in reader.lines().flatten() {
                println!("[Python] {}", line);
            }
        });
    }
    if let Some(stderr) = proc.stderr.take() {
        thread::spawn(|| {
            let reader = BufReader::new(stderr);
            for line in reader.lines().flatten() {
                eprintln!("[Python ERR] {}", line);
            }
        });
    }

    *state.python_process.lock().unwrap() = Some(proc);
    Ok(format!("Python started (PID: {})", pid))
}

#[tauri::command]
pub fn stop_python(state: State<AppState>) -> Result<String, String> {
    do_stop_python(&state)
}

#[tauri::command]
pub fn python_status(state: State<AppState>) -> bool {
    is_python_running(&state)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::state::AppState;

    #[test]
    fn status_false_when_no_process() {
        let state = AppState::new();
        assert!(!is_python_running(&state));
    }

    #[test]
    fn stop_returns_error_when_not_running() {
        let state = AppState::new();
        let result = do_stop_python(&state);
        assert!(result.is_err());
        assert!(result.unwrap_err().contains("No Python process running"));
    }

    #[test]
    fn stop_clears_process_slot() {
        // After a failed stop, the slot must still be None (no zombie entry)
        let state = AppState::new();
        let _ = do_stop_python(&state);
        assert!(!is_python_running(&state));
    }

    #[test]
    fn python_exe_returns_a_path() {
        let exe = python_exe();
        // Either the venv path or the fallback "python" string
        assert!(!exe.as_os_str().is_empty());
    }
}
