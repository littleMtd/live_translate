use crate::paths::{app_root, python_exe};
use serde::{Deserialize, Serialize};
use std::process::Command;

#[derive(Debug, Deserialize, Serialize)]
pub struct ExportableRun {
    pub run_id: String,
    pub started_at: String,
    pub ended_at: String,
    pub event_count: u64,
    pub run_kind: String,
    pub run_complete: bool,
}

#[derive(Debug, Deserialize, Serialize)]
pub struct BundleExportResult {
    pub run_id: String,
    pub output_path: String,
    pub file_count: u64,
    pub total_bytes: u64,
    pub event_count: u64,
    pub runtime_event_files: Vec<String>,
    pub audio_included: u64,
}

pub(crate) fn run_exporter(args: &[String]) -> Result<String, String> {
    let root = app_root();
    let script = root.join("scripts").join("export_chatgpt_bundle.py");
    let output = Command::new(python_exe())
        .arg(script)
        .args(args)
        .current_dir(&root)
        .output()
        .map_err(|error| format!("Failed to start bundle exporter: {error}"))?;
    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr).trim().to_string();
        return Err(if stderr.is_empty() {
            format!("Bundle exporter exited with {}", output.status)
        } else {
            stderr
        });
    }
    String::from_utf8(output.stdout)
        .map(|value| value.trim().to_string())
        .map_err(|error| format!("Bundle exporter returned invalid UTF-8: {error}"))
}

pub(crate) fn export_run_sync(run_id: &str) -> Result<BundleExportResult, String> {
    let output = run_exporter(&["--run-id".to_string(), run_id.to_string()])?;
    serde_json::from_str(&output)
        .map_err(|error| format!("Bundle exporter returned invalid JSON: {error}"))
}

#[tauri::command]
pub async fn list_exportable_runs() -> Result<Vec<ExportableRun>, String> {
    let output = tauri::async_runtime::spawn_blocking(|| {
        run_exporter(&["--list-runs".to_string()])
    })
    .await
    .map_err(|error| format!("Bundle run discovery failed: {error}"))??;
    serde_json::from_str(&output)
        .map_err(|error| format!("Bundle run discovery returned invalid JSON: {error}"))
}

#[tauri::command]
pub async fn export_chatgpt_bundle(
    run_id: String,
    include_audio: bool,
) -> Result<BundleExportResult, String> {
    if run_id.trim().is_empty() || run_id.len() > 200 || run_id.chars().any(char::is_control) {
        return Err("Invalid run_id".to_string());
    }
    let output = tauri::async_runtime::spawn_blocking(move || {
        let mut args = vec!["--run-id".to_string(), run_id];
        if include_audio {
            args.push("--include-audio".to_string());
        }
        run_exporter(&args)
    })
    .await
    .map_err(|error| format!("Bundle export failed: {error}"))??;
    serde_json::from_str(&output)
        .map_err(|error| format!("Bundle exporter returned invalid JSON: {error}"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_export_result_contract() {
        let result: BundleExportResult = serde_json::from_str(
            r#"{"run_id":"r","output_path":"C:/bundle","file_count":5,"total_bytes":9,"event_count":2,"runtime_event_files":["runtime_events.jsonl"],"audio_included":0}"#,
        )
        .unwrap();
        assert_eq!(result.run_id, "r");
        assert_eq!(result.runtime_event_files, vec!["runtime_events.jsonl"]);
    }

    #[test]
    fn parses_run_list_contract() {
        let runs: Vec<ExportableRun> = serde_json::from_str(
            r#"[{"run_id":"r","started_at":"a","ended_at":"b","event_count":3,"run_kind":"live","run_complete":false}]"#,
        )
        .unwrap();
        assert_eq!(runs[0].event_count, 3);
        assert!(!runs[0].run_complete);
    }
}
