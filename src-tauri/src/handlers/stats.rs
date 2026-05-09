use std::time::{SystemTime, UNIX_EPOCH};

#[derive(serde::Serialize)]
pub struct SystemStats {
    pub uptime_seconds: u64,
    pub platform: String,
    pub arch: String,
}

pub(crate) fn build_system_stats() -> SystemStats {
    let uptime_seconds = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);

    SystemStats {
        uptime_seconds,
        platform: std::env::consts::OS.to_string(),
        arch: std::env::consts::ARCH.to_string(),
    }
}

#[tauri::command]
pub fn get_system_stats() -> Result<SystemStats, String> {
    Ok(build_system_stats())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn platform_is_non_empty() {
        let stats = build_system_stats();
        assert!(!stats.platform.is_empty());
    }

    #[test]
    fn arch_is_non_empty() {
        let stats = build_system_stats();
        assert!(!stats.arch.is_empty());
    }

    #[test]
    fn uptime_seconds_is_positive() {
        let stats = build_system_stats();
        assert!(stats.uptime_seconds > 0);
    }

    #[test]
    fn platform_is_known_os() {
        let stats = build_system_stats();
        let known = ["windows", "macos", "linux", "ios", "android"];
        assert!(
            known.contains(&stats.platform.as_str()),
            "unexpected platform: {}",
            stats.platform
        );
    }

    #[test]
    fn arch_is_known_arch() {
        let stats = build_system_stats();
        let known = ["x86_64", "aarch64", "x86", "arm"];
        assert!(
            known.contains(&stats.arch.as_str()),
            "unexpected arch: {}",
            stats.arch
        );
    }
}
