use crate::paths::{app_root, python_exe};
use std::process::Command;

#[tauri::command]
pub fn launch_identity_roi_calibration() -> Result<String, String> {
    let child = Command::new(python_exe())
        .args(["-m", "modules.identity_roi", "--calibrate"])
        .current_dir(app_root())
        .spawn()
        .map_err(|error| format!("Failed to start identity ROI calibration: {error}"))?;
    Ok(format!("Identity ROI calibration started (PID: {})", child.id()))
}
