use std::process::Command;

#[test]
fn help_does_not_inject_optional_manager_flags() {
    let output = Command::new(env!("CARGO_BIN_EXE_xcvrd-rs"))
        .arg("--help")
        .output()
        .unwrap();
    assert!(output.status.success());
    assert!(String::from_utf8_lossy(&output.stdout).starts_with("usage: xcvrd-rs"));
    assert!(
        output.stderr.is_empty(),
        "help unexpectedly changed the launch arguments: {}",
        String::from_utf8_lossy(&output.stderr)
    );
}
