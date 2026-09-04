use crate::paths::{app_root, main_py_path, python_exe};
use crate::state::AppState;
use std::io::{BufRead, BufReader};
use std::process::{Command, Stdio};
use std::time::{SystemTime, UNIX_EPOCH};
use std::thread;
use tauri::State;

pub(crate) fn is_python_running(state: &AppState) -> bool {
    let mut guard = state.python_process.lock().unwrap();
    match guard.as_mut() {
        // Reap the child: if main.py has exited or crashed, clear the slot so the
        // dashboard stops showing Online and Start is allowed again. Without try_wait()
        // a dead process still reads as running.
        Some(child) => match child.try_wait() {
            Ok(Some(_status)) => {
                *guard = None;
                *state.python_run_id.lock().unwrap() = None;
                false
            }
            Ok(None) => true,
            Err(_) => true,
        },
        None => false,
    }
}

pub(crate) fn do_stop_python(state: &AppState) -> Result<String, String> {
    let mut guard = state.python_process.lock().unwrap();
    if let Some(mut proc) = guard.take() {
        proc.kill()
            .map_err(|e| format!("Failed to stop Python: {}", e))?;
        proc.wait().map_err(|e| format!("Wait error: {}", e))?;
        *state.python_run_id.lock().unwrap() = None;
        Ok("Python process stopped".to_string())
    } else {
        Err("No Python process running".to_string())
    }
}

pub(crate) fn do_stop_python_and_export(state: &AppState) -> Result<String, String> {
    let run_id = state.python_run_id.lock().unwrap().clone();
    let stopped = do_stop_python(state)?;
    let Some(run_id) = run_id else {
        return Ok(stopped);
    };
    match crate::handlers::bundle::export_run_sync(&run_id) {
        Ok(bundle) => Ok(format!("{}; ChatGPT bundle: {}", stopped, bundle.output_path)),
        Err(error) => {
            eprintln!("Automatic ChatGPT bundle export after forced stop failed: {error}");
            Ok(stopped)
        }
    }
}

#[tauri::command]
pub fn start_python(state: State<AppState>) -> Result<String, String> {
    if is_python_running(&state) {
        return Err("Python is already running".to_string());
    }

    // Run from the project root so the pipeline's cwd-relative paths (logs/, data/,
    // logs/audio_dump/, ...) resolve exactly as they do when launched manually with
    // `cd live_translate && python main.py`. Without this the spawned process inherits
    // Tauri's cwd (src-tauri/ in dev) and the pipeline cannot find its data dirs.
    let run_id = format!(
        "dashboard-{}-{}",
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_millis(),
        std::process::id()
    );
    let mut proc = Command::new(python_exe())
        .arg("-u")
        .arg(main_py_path())
        .current_dir(app_root())
        // Opt the pipeline into applying dashboard-saved config overrides
        // (config.py reads logs/live_translate_config.json only when this is set).
        .env("LIVE_TRANSLATE_APPLY_DASHBOARD_CONFIG", "1")
        .env("LIVE_TRANSLATE_RUN_ID", &run_id)
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
    *state.python_run_id.lock().unwrap() = Some(run_id);
    Ok(format!("Python started (PID: {})", pid))
}

#[tauri::command]
pub fn stop_python(state: State<AppState>) -> Result<String, String> {
    do_stop_python_and_export(&state)
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

    #[test]
    fn reaps_exited_child_and_allows_restart() {
        use std::process::Command;
        let state = AppState::new();
        // A process that exits immediately.
        let spawned = if cfg!(windows) {
            Command::new("cmd").args(["/C", "exit", "0"]).spawn()
        } else {
            Command::new("true").spawn()
        };
        let Ok(child) = spawned else {
            return; // spawn unavailable in this environment; skip
        };
        *state.python_process.lock().unwrap() = Some(child);

        // The child exits within milliseconds; is_python_running must reap it.
        let mut reaped = false;
        for _ in 0..50 {
            if !is_python_running(&state) {
                reaped = true;
                break;
            }
            std::thread::sleep(std::time::Duration::from_millis(20));
        }
        assert!(reaped, "an exited child must be reaped to false");
        assert!(state.python_process.lock().unwrap().is_none());
    }
}
