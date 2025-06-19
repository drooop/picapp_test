use tauri::command;
use std::process::Command;

#[command]
fn run_python() -> String {
    let output = Command::new("python3").arg("../app.py").output().unwrap();
    String::from_utf8_lossy(&output.stdout).to_string()
}

fn main() {
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![run_python])
        .run(tauri::generate_context!())
        .unwrap();
}
