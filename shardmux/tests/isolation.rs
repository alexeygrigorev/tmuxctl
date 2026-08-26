#![cfg(target_os = "linux")]

use std::process::{Command, Output};
use std::thread;
use std::time::{Duration, Instant};

use serde_json::Value;
use tempfile::TempDir;

#[test]
fn killing_one_session_server_does_not_break_another() {
    let temp = TempDir::new().expect("temp directory");
    let state = temp.path().join("state");
    let runtime = temp.path().join("runtime");
    let binary = env!("CARGO_BIN_EXE_shardmux");

    let run = |arguments: &[&str]| -> Output {
        Command::new(binary)
            .args(arguments)
            .env("SHARDMUX_STATE_DIR", &state)
            .env("SHARDMUX_RUNTIME_DIR", &runtime)
            .output()
            .expect("run shardmux")
    };

    assert_success(
        run(&[
            "create",
            "alpha",
            "--direct",
            "--no-limit",
            "--",
            "/bin/cat",
        ]),
        "create alpha",
    );
    assert_success(
        run(&["create", "beta", "--direct", "--no-limit", "--", "/bin/cat"]),
        "create beta",
    );

    let alpha = status(&run, "alpha");
    let alpha_pid = alpha["session"]["server_pid"]
        .as_u64()
        .expect("alpha server pid") as libc::pid_t;
    assert_eq!(unsafe { libc::kill(alpha_pid, libc::SIGKILL) }, 0);

    let deadline = Instant::now() + Duration::from_secs(5);
    while Instant::now() < deadline {
        if !status(&run, "alpha")["live"].as_bool().unwrap_or(false) {
            break;
        }
        thread::sleep(Duration::from_millis(100));
    }
    assert!(!status(&run, "alpha")["live"].as_bool().unwrap_or(true));

    let beta = status(&run, "beta");
    assert!(
        beta["live"].as_bool().unwrap_or(false),
        "beta stopped when alpha's independent server was killed: {beta:#}"
    );
    assert_success(
        run(&["send", "beta", "--message", "still-alive", "--no-enter"]),
        "send to beta after alpha server death",
    );

    assert_success(run(&["kill", "beta"]), "kill beta");
    let _ = run(&["prune"]);
}

fn status(run: &impl Fn(&[&str]) -> Output, name: &str) -> Value {
    let output = run(&["status", name, "--json"]);
    assert_success_ref(&output, &format!("status {name}"));
    serde_json::from_slice(&output.stdout).expect("status JSON")
}

fn assert_success(output: Output, operation: &str) {
    assert_success_ref(&output, operation);
}

fn assert_success_ref(output: &Output, operation: &str) {
    assert!(
        output.status.success(),
        "{operation} failed\nstdout:\n{}\nstderr:\n{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
}
