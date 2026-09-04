use crate::paths::profile_status_path;
use serde_json::Value;
use std::fs;

#[tauri::command]
pub fn get_profile_status() -> Result<Value, String> {
    let path = profile_status_path();
    let content = fs::read_to_string(&path)
        .map_err(|e| format!("Profile status unavailable at {:?}: {}", path, e))?;
    serde_json::from_str(&content).map_err(|e| format!("Invalid profile status JSON: {}", e))
}
