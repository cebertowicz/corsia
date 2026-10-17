fn main() {
    tauri_build::try_build(
        tauri_build::Attributes::new().app_manifest(
            tauri_build::AppManifest::new().commands(&[
                "choose_work_folder",
                "configure_work_folder",
                "disconnect_remote",
                "get_service_log_path",
                "open_login_items",
                "remote_discovered",
                "remote_pair",
                "retry_quit",
                "retry_saved_remote",
                "save_setup_report",
                "start_local_service",
                "start_window_drag",
            ]),
        ),
    )
    .expect("failed to build Tauri app manifest");
}
