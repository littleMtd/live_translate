#![cfg_attr(all(not(debug_assertions), target_os = "windows"), windows_subsystem = "windows")]

mod handlers;
mod state;

fn main() {
    tauri::Builder::default()
        .manage(state::AppState::new())
        .invoke_handler(tauri::generate_handler![
            handlers::config::get_config,
            handlers::config::update_config,
            handlers::config::reload_config,
            handlers::cache::get_cache_stats,
            handlers::cache::clear_cache,
            handlers::stats::get_system_stats,
            handlers::python::start_python,
            handlers::python::stop_python,
            handlers::python::python_status,
        ])
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::CloseRequested { .. } = event {
                let state = window.state::<state::AppState>();
                let _ = handlers::python::do_stop_python(&state);
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
