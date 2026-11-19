use std::{
    fs,
    path::PathBuf,
    process::{Child, Command},
    sync::Mutex,
};

use tauri::{
    menu::{Menu, MenuItem},
    tray::TrayIconBuilder,
    Manager, WindowEvent,
};

#[cfg(target_os = "windows")]
use std::os::windows::process::CommandExt;

#[cfg(target_os = "windows")]
const CREATE_NO_WINDOW: u32 = 0x08000000;

struct InstalledBackend {
    executable: PathBuf,
    home: PathBuf,
    api: Child,
    monitor: Option<Child>,
}

type BackendState = Mutex<Option<InstalledBackend>>;

fn spawn_backend(executable: &PathBuf, home: &PathBuf, mode: &str) -> std::io::Result<Child> {
    let mut command = Command::new(executable);
    command
        .arg(mode)
        .current_dir(home)
        .env("TUTOR_RADAR_HOME", home);
    if let Some(resources) = executable.parent().and_then(|directory| directory.parent()) {
        command.env("TUTOR_RADAR_BASEMAP", resources.join("amap-basemap.json"));
    }
    if let Ok(app) = std::env::current_exe() {
        if let Some(directory) = app.parent() {
            command.env("TUTOR_RADAR_INSTALL_DIR", directory);
        }
    }
    #[cfg(target_os = "windows")]
    command.creation_flags(CREATE_NO_WINDOW);
    command.spawn()
}

fn stop_child(child: &mut Child) {
    let _ = child.kill();
    let _ = child.wait();
}

impl Drop for InstalledBackend {
    fn drop(&mut self) {
        if let Some(mut monitor) = self.monitor.take() {
            stop_child(&mut monitor);
        }
        stop_child(&mut self.api);
    }
}

fn project_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .and_then(|path| path.parent())
        .expect("Tauri project must stay inside the tutor project")
        .to_path_buf()
}

fn run_project_script(name: &str) -> Result<(), String> {
    let script = project_root().join(name);
    let mut command = Command::new("powershell.exe");
    command.args([
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        script.to_string_lossy().as_ref(),
    ]);
    #[cfg(target_os = "windows")]
    command.creation_flags(CREATE_NO_WINDOW);
    let status = command.status().map_err(|error| error.to_string())?;
    if status.success() {
        Ok(())
    } else {
        Err(format!("{} exited with {}", name, status))
    }
}

fn show_main_window(app: &tauri::AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.show();
        let _ = window.unminimize();
        let _ = window.set_focus();
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            show_main_window(app);
        }))
        .setup(|app| {
            let resources = app.path().resource_dir()?;
            let executable = resources.join("backend").join("radar-backend.exe");
            let installed = if executable.is_file() {
                let home = app.path().app_local_data_dir()?;
                let private_dir = home.join("data").join("private");
                fs::create_dir_all(&private_dir)?;
                let bundled_map = resources.join("amap-beta.json");
                let local_map = private_dir.join("amap-beta.json");
                if bundled_map.is_file() && !local_map.exists() {
                    fs::copy(bundled_map, local_map)?;
                }
                let api = spawn_backend(&executable, &home, "api")?;
                let mut backend = InstalledBackend {
                    executable,
                    home,
                    api,
                    monitor: None,
                };
                backend.monitor = Some(spawn_backend(
                    &backend.executable,
                    &backend.home,
                    "monitor",
                )?);
                Some(backend)
            } else {
                if let Err(error) = run_project_script("start-app-services.ps1") {
                    eprintln!("Failed to start local services: {error}");
                }
                None
            };
            app.manage(BackendState::new(installed));

            let open = MenuItem::with_id(app, "open", "打开家教雷达", true, None::<&str>)?;
            let pause = MenuItem::with_id(app, "pause", "暂停监控", true, None::<&str>)?;
            let resume = MenuItem::with_id(app, "resume", "继续监控", true, None::<&str>)?;
            let quit = MenuItem::with_id(app, "quit", "彻底退出", true, None::<&str>)?;
            let menu = Menu::with_items(app, &[&open, &pause, &resume, &quit])?;

            TrayIconBuilder::new()
                .icon(app.default_window_icon().expect("application icon").clone())
                .tooltip("家教雷达")
                .menu(&menu)
                .show_menu_on_left_click(true)
                .on_menu_event(|app, event| match event.id.as_ref() {
                    "open" => show_main_window(app),
                    "pause" => {
                        let state = app.state::<BackendState>();
                        if let Some(backend) = state.lock().unwrap().as_mut() {
                            if let Some(mut child) = backend.monitor.take() {
                                stop_child(&mut child);
                            }
                            return;
                        }
                        if let Err(error) = run_project_script("stop-monitor.ps1") {
                            eprintln!("Failed to pause monitor: {error}");
                        }
                    }
                    "resume" => {
                        let state = app.state::<BackendState>();
                        if let Some(backend) = state.lock().unwrap().as_mut() {
                            if backend.monitor.is_none() {
                                backend.monitor =
                                    spawn_backend(&backend.executable, &backend.home, "monitor")
                                        .ok();
                            }
                            return;
                        }
                        if let Err(error) = run_project_script("start-monitor-background.ps1") {
                            eprintln!("Failed to resume monitor: {error}");
                        }
                    }
                    "quit" => {
                        let installed = app.state::<BackendState>().lock().unwrap().take();
                        if installed.is_some() {
                            drop(installed);
                        } else if let Err(error) = run_project_script("stop-app-services.ps1") {
                            eprintln!("Failed to stop local services: {error}");
                        }
                        app.exit(0);
                    }
                    _ => {}
                })
                .build(app)?;
            Ok(())
        })
        .on_window_event(|window, event| {
            if let WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                let _ = window.hide();
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running Tutor Radar");
}
