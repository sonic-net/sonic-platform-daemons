//! Why each DPU last went down, kept on disk and republished.
//!
//! Ports `chassisd:SmartSwitchModuleUpdater.dpu_boot_id_update`,
//! `chassisd:SmartSwitchModuleUpdater.persist_dpu_reboot_cause`,
//! `chassisd:SmartSwitchModuleUpdater._rotate_files`,
//! `chassisd:SmartSwitchModuleUpdater.retrieve_dpu_reboot_info` and
//! `chassisd:SmartSwitchModuleUpdater.update_dpu_reboot_cause_to_db`, and the
//! files the midplane-down reason is kept in
//! (`chassisd:SmartSwitchModuleUpdater._read_midplane_down_reason` and its
//! write and clear).
//!
//! A reboot is recognised by the DPU's kernel boot_id changing.  The DPU's own
//! copy of this daemon publishes the boot_id into DPU_STATE, and the NPU
//! records the cause on the DPU's behalf, because a DPU that crashed is in no
//! position to record why.  It lives under `/host`, so it outlives both the
//! container and the reboot -- the files are the record, and the REBOOT_CAUSE
//! rows in CHASSIS_STATE_DB that `show reboot-cause history` reads are rebuilt
//! from them.  Every file is written atomically and synced, as
//! `chassisd:_atomic_write` does, so a power cut leaves either the old record
//! or the new one and never half of either.

use std::fs::File;
use std::io::Write;
use std::path::{Path, PathBuf};

use platform_api::{ModuleRebootCause, PlatformApi, PlatformError};
use pmon_common::db::TableLike;
// What `chassisd:try_get` answers for a platform that does not implement
// `get_reboot_cause`, and so the cause such a platform's reboots record.
use pmon_common::fmt::NOT_AVAILABLE;

/// `chassisd:MODULE_REBOOT_CAUSE_DIR`.
pub const MODULE_REBOOT_CAUSE_DIR: &str = "/host/reboot-cause/module/";
/// `chassisd:MAX_HISTORY_FILES`: the history kept per DPU, and so the rows
/// the table is rebuilt with.
pub const MAX_HISTORY_FILES: usize = 10;

/// One recorded reboot.
#[derive(Debug, Clone, PartialEq)]
pub struct RebootCause {
    pub cause: String,
    pub comment: String,
    pub device: String,
    /// Human-readable, `%a %b %d %I:%M:%S %p UTC %Y`.
    pub time: String,
    /// Sortable, `%Y_%m_%d_%H_%M_%S`; also the history file's name and the
    /// REBOOT_CAUSE key's last component.
    pub name: String,
    /// The DPU's kernel boot_id this record was taken for.  `None` for a
    /// record written before boot_ids were kept, which has no such field;
    /// written as `""` when the record was not taken for a boot_id.
    pub boot_id: Option<String>,
}

impl RebootCause {
    /// The record as `json.dump` lays it out: Python's key order and its
    /// `", "` and `": "` separators, so a file written by either daemon reads
    /// the same.
    ///
    /// Each value goes through serde_json's string encoder.  The cause and the
    /// comment are whatever the vendor answered, and a newline or a tab in one
    /// written raw is not JSON: `previous` would fail to read it back, find no
    /// boot_id, and record the same reboot again on every DPU_STATE event --
    /// pushing the real history out of the ten files kept.  Non-ASCII is left
    /// as UTF-8 where Python escapes it (`ensure_ascii`); both are valid JSON
    /// and read back the same.
    fn to_json(&self) -> String {
        let q = |s: &str| serde_json::Value::from(s).to_string();
        format!(
            concat!(
                r#"{{"cause": {}, "comment": {}, "device": {}, "#,
                r#""time": {}, "name": {}, "boot_id": {}}}"#,
            ),
            q(&self.cause),
            q(&self.comment),
            q(&self.device),
            q(&self.time),
            q(&self.name),
            q(self.boot_id.as_deref().unwrap_or(""))
        )
    }

    fn from_json(text: &str) -> Option<Self> {
        let v: serde_json::Value = serde_json::from_str(text).ok()?;
        let s = |k: &str| v.get(k).and_then(|x| x.as_str()).unwrap_or("").to_string();
        Some(Self {
            cause: s("cause"),
            comment: s("comment"),
            device: s("device"),
            time: s("time"),
            name: s("name"),
            boot_id: v.get("boot_id").and_then(|x| x.as_str()).map(str::to_string),
        })
    }
}

/// Splits a single answer into a cause and a comment.
///
/// For the platforms that answer one `"cause,comment"` string rather than the
/// `(cause, comment)` pair the base class documents.  A pair with a detail
/// arrives as two arguments and does not come through here; a pair whose
/// detail is None does, because the facade hands it over exactly as it hands
/// over a bare string.  Such a cause is split on its first comma where Python
/// keeps it whole and writes a null comment -- at worst a cause with a comma
/// in it loses its tail to the comment.  A platform that answers nothing gets
/// the pair Python invents.
pub fn split_cause(answer: Option<&str>) -> (String, String) {
    match answer {
        None | Some("") => ("Unknown".to_string(), NOT_AVAILABLE.to_string()),
        Some(s) => match s.split_once(',') {
            Some((c, comment)) => (c.to_string(), comment.to_string()),
            None => (s.to_string(), NOT_AVAILABLE.to_string()),
        },
    }
}

/// Where one DPU's files live.  Lower-cased, as Python does.
fn dir_for(root: &Path, module: &str) -> PathBuf {
    root.join(module.to_lowercase())
}

fn history_dir(root: &Path, module: &str) -> PathBuf {
    dir_for(root, module).join("history")
}

fn with_suffix(path: &Path, suffix: &str) -> PathBuf {
    let mut s = path.as_os_str().to_owned();
    s.push(suffix);
    PathBuf::from(s)
}

/// `chassisd:_fsync_parent_directory`: make a rename or a new directory entry
/// survive a power cut, which syncing the file alone does not.
fn fsync_parent(path: &Path) -> std::io::Result<()> {
    let dir = path.parent().filter(|d| !d.as_os_str().is_empty()).unwrap_or(Path::new("."));
    File::open(dir)?.sync_all()
}

/// `chassisd:_atomic_write`: fill a temporary file beside `path`, sync it,
/// rename it into place and sync the directory.  The temporary file is removed
/// if any step fails, so a failure leaves the old contents and no debris.
pub fn atomic_write(path: &Path, contents: &[u8]) -> std::io::Result<()> {
    if let Some(dir) = path.parent() {
        std::fs::create_dir_all(dir)?;
    }
    let tmp = with_suffix(path, ".tmp");
    let result = (|| {
        let mut f = File::create(&tmp)?;
        f.write_all(contents)?;
        f.sync_all()?;
        std::fs::rename(&tmp, path)?;
        fsync_parent(path)
    })();
    if result.is_err() && tmp.symlink_metadata().is_ok() {
        let _ = std::fs::remove_file(&tmp);
    }
    result
}

/// `chassisd:_atomic_replace_symlink`: point `link` at `target` without ever
/// leaving `link` absent, which removing it first and linking it again did.
pub fn atomic_replace_symlink(target: &Path, link: &Path) -> std::io::Result<()> {
    let tmp = with_suffix(link, ".tmp");
    if tmp.symlink_metadata().is_ok() {
        std::fs::remove_file(&tmp)?;
    }
    std::os::unix::fs::symlink(target, &tmp)?;
    let result = std::fs::rename(&tmp, link).and_then(|()| fsync_parent(link));
    if result.is_err() && tmp.symlink_metadata().is_ok() {
        let _ = std::fs::remove_file(&tmp);
    }
    result
}

/// The most recent recorded reboot, from `previous-reboot-cause.json`.
///
/// `chassisd:SmartSwitchModuleUpdater.retrieve_dpu_reboot_info`: a DPU with
/// nothing recorded yet is the normal case and only a debug line, and a file
/// that will not read is an error that reads as nothing recorded.
pub fn previous(root: &Path, module: &str) -> Option<RebootCause> {
    let path = dir_for(root, module).join("previous-reboot-cause.json");
    let text = match std::fs::read_to_string(&path) {
        Ok(text) => text,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            log::debug!("{module}: previous-reboot-cause.json not found");
            return None;
        }
        Err(e) => {
            log::error!("{module}: Failed to read previous-reboot-cause.json: {e}");
            return None;
        }
    };
    let parsed = RebootCause::from_json(&text);
    if parsed.is_none() {
        log::error!("{module}: Failed to read previous-reboot-cause.json: not valid JSON");
    }
    parsed
}

/// Write one reboot cause: the history file, the symlink to the latest, and
/// the rotation.
///
/// Named for `now_name`, which the caller takes when it sees the new boot_id.
/// `chassisd:SmartSwitchModuleUpdater.persist_dpu_reboot_cause` takes its name
/// a little later, after `get_reboot_cause` has answered; the two differ by
/// however long the platform took to answer.
///
/// A rotation that fails is a failed persist, as in Python, where the
/// `os.remove` is not wrapped: the record is on disk, and the caller does not
/// go on to publish.
pub fn persist(
    root: &Path,
    module: &str,
    answer: Option<&str>,
    detail: Option<&str>,
    boot_id: Option<&str>,
    now_name: &str,
    formatted: impl Fn(&str) -> String,
) -> std::io::Result<RebootCause> {
    let (cause, comment) = match detail {
        // The platform answered both halves, which is the ordinary case: the
        // base class documents `(cause, comment)` and the facade now carries
        // both slots of it.
        Some(d) => (answer.unwrap_or("Unknown").to_string(), d.to_string()),
        // No detail: either the platform has none, or it answered the single
        // `"cause,comment"` string some do.  `split_cause` covers both.
        None => split_cause(answer),
    };

    let record = RebootCause {
        cause,
        comment,
        device: module.to_string(),
        time: formatted(now_name),
        name: now_name.to_string(),
        boot_id: Some(boot_id.unwrap_or("").to_string()),
    };

    let file = history_dir(root, module).join(format!("{now_name}_reboot_cause.json"));
    atomic_write(&file, record.to_json().as_bytes())?;
    atomic_replace_symlink(&file, &dir_for(root, module).join("previous-reboot-cause.json"))?;

    rotate(root, module)?;
    Ok(record)
}

/// Keep the newest [`MAX_HISTORY_FILES`] records.
///
/// Sorted by name, which is the `%Y_%m_%d_%H_%M_%S` timestamp and therefore
/// also chronological -- the reason the file name carries that format.  Only
/// the records count: a `.tmp` left by a write that was cut off is not
/// history, and rotating it out would cost a real record its place.
fn rotate(root: &Path, module: &str) -> std::io::Result<()> {
    let dir = history_dir(root, module);
    let entries = match std::fs::read_dir(&dir) {
        Ok(entries) => entries,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(()),
        Err(e) => return Err(e),
    };
    let mut names: Vec<String> = entries
        .filter_map(|e| e.ok())
        .map(|e| e.file_name().to_string_lossy().to_string())
        .filter(|n| n.ends_with("_reboot_cause.json"))
        .collect();
    if names.len() <= MAX_HISTORY_FILES {
        return Ok(());
    }
    names.sort();
    for old in &names[..names.len() - MAX_HISTORY_FILES] {
        std::fs::remove_file(dir.join(old))?;
    }
    Ok(())
}

/// Republish one DPU's whole history into CHASSIS_STATE_DB.
///
/// The table is rebuilt rather than appended to, so a rotation on disk is
/// reflected rather than leaving rows for files that no longer exist.
///
/// A file that cannot be read, or whose row will not write, costs that one
/// entry and is warned about in Python's words: each file is published under
/// its own `try` in
/// `chassisd:SmartSwitchModuleUpdater.update_dpu_reboot_cause_to_db`.  Clearing
/// the old rows is not wrapped there, so a key listing or a delete that fails
/// ends the pass here, and the caller says the record is still on disk.  Every
/// field the file holds is published, so the boot_id is too, and a record
/// written before boot_ids were kept is published without one.
pub fn publish(
    root: &Path,
    module: &str,
    table: &dyn TableLike,
) -> Result<(), String> {
    let prefix = format!("{}|", module.to_uppercase());
    for key in table.get_keys()? {
        if key.starts_with(&prefix) {
            table.del(&key)?;
        }
    }

    let dir = history_dir(root, module);
    let Ok(entries) = std::fs::read_dir(&dir) else {
        // A module with no history yet is the normal first-boot case, not a
        // database failure.
        log::warn!("No reboot cause history files found for module: {module}");
        return Ok(());
    };
    // Said when there are no history files, as Python's empty `glob` says
    // it -- not when there are files and none of them would read, which the
    // per-file warnings below have already said.
    let mut found = false;
    for entry in entries.filter_map(|e| e.ok()) {
        let path = entry.path();
        if !path.to_string_lossy().ends_with("_reboot_cause.json") {
            continue;
        }
        found = true;
        let text = match std::fs::read_to_string(&path) {
            Ok(text) => text,
            Err(e) => {
                log::warn!("Error processing file {}: {e}", path.display());
                continue;
            }
        };
        let Some(r) = RebootCause::from_json(&text) else {
            log::warn!("Failed to decode JSON from file: {}", path.display());
            continue;
        };
        let key = format!("{}|{}", module.to_uppercase(), r.name);
        let mut fvs = vec![
            ("cause", r.cause.clone()),
            ("comment", r.comment.clone()),
            ("device", r.device.clone()),
            ("time", r.time.clone()),
            ("name", r.name.clone()),
        ];
        if let Some(boot_id) = &r.boot_id {
            fvs.push(("boot_id", boot_id.clone()));
        }
        if let Err(e) = table.set(&key, &fvs) {
            log::warn!("Error processing file {}: {e}", path.display());
        }
    }
    if !found {
        log::warn!("No reboot cause history files found for module: {module}");
    }
    Ok(())
}

/// Record why a DPU restarted, once per boot_id it reports.
///
/// `chassisd:SmartSwitchModuleUpdater.dpu_boot_id_update`.  A boot_id already
/// recorded is the same boot seen again -- the NPU's own restart replays every
/// DPU_STATE row -- and records nothing, which is what keeps one reboot from
/// showing up twice in the history.
///
/// The DPU is looked up by name as Python looks it up, with
/// `get_module_index`, and its `get_reboot_cause` read through the key that
/// answers.  Each step that fails is logged in Python's words and ends the
/// capture there: the history file is the record, so one that could not be
/// written is not published, and one that could not be published is still on
/// disk for the next pass.
pub fn boot_id_update(
    root: &Path,
    module: &str,
    boot_id: &str,
    platform: &mut dyn PlatformApi,
    table: &dyn TableLike,
    now_name: &str,
    formatted: impl Fn(&str) -> String,
) {
    if boot_id.is_empty() {
        return;
    }
    if previous(root, module).and_then(|r| r.boot_id).as_deref() == Some(boot_id) {
        return;
    }
    let key = match platform.get_module_key(module) {
        Ok(Some(key)) => key,
        // `try_get(get_module_index, default=INVALID_MODULE_INDEX)`: not
        // implemented, or no such module.
        Ok(None) | Err(PlatformError::NotSupported(_)) => {
            log::error!("Unable to get module-index for {module} to capture reboot cause");
            return;
        }
        Err(e) => {
            log::error!(
                "Failed to look up module {module} to capture reboot cause: {}",
                detail_of(&e)
            );
            return;
        }
    };
    pmon_common::notice!("{module}: new boot_id {boot_id} detected, capturing reboot cause");

    let (cause, detail) = match platform.get_module_reboot_cause(&key) {
        // What a platform that answered nothing gets: `chassisd:try_get`
        // turns both a NotImplementedError and a `None` into "N/A", and that
        // string is then the cause.
        Ok(ModuleRebootCause { cause: None, detail: None })
        | Err(PlatformError::NotSupported(_)) => (Some(NOT_AVAILABLE.to_string()), None),
        Ok(ModuleRebootCause { cause, detail }) => (cause, detail),
        Err(e) => {
            log::error!("Failed to get reboot cause for {module}: {}", detail_of(&e));
            return;
        }
    };

    if let Err(e) = persist(
        root, module, cause.as_deref(), detail.as_deref(), Some(boot_id), now_name, formatted)
    {
        log::error!("Failed to persist reboot cause for {module}: {e}");
        return;
    }
    if let Err(e) = publish(root, module, table) {
        log::error!(
            "Failed to update reboot cause to DB for {module}: {e}. \
             The boot_id and reboot cause is stored in json file.");
    }
}

/// An error as Python's `{e}` prints the exception: its message alone.
fn detail_of(e: &PlatformError) -> &str {
    match e {
        PlatformError::NotSupported(m)
        | PlatformError::NotFound(m)
        | PlatformError::Backend(m) => m,
    }
}

// ── the midplane-down reason ──────────────────────────────────────────────────

/// `chassisd:SmartSwitchModuleUpdater._midplane_reason_path`.
fn midplane_reason_path(root: &Path, module: &str) -> PathBuf {
    dir_for(root, module).join("midplane-down-reason.txt")
}

/// The reason recorded when this DPU's midplane last went down, if it has not
/// come back since.
///
/// `chassisd:SmartSwitchModuleUpdater._read_midplane_down_reason`.  Kept on
/// disk so a restart of this daemon republishes the reason it already
/// resolved, rather than asking again once the moment has passed.
pub fn read_midplane_down_reason(root: &Path, module: &str) -> Option<String> {
    match std::fs::read_to_string(midplane_reason_path(root, module)) {
        Ok(text) => Some(text.trim().to_string()).filter(|s| !s.is_empty()),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => None,
        Err(e) => {
            log::error!("{module}: read midplane reason failed: {e}");
            None
        }
    }
}

/// `chassisd:SmartSwitchModuleUpdater._write_midplane_down_reason`.
pub fn write_midplane_down_reason(root: &Path, module: &str, reason: &str) {
    let text = format!("{reason}\n");
    if let Err(e) = atomic_write(&midplane_reason_path(root, module), text.as_bytes()) {
        log::error!("{module}: persist midplane reason failed: {e}");
    }
}

/// `chassisd:SmartSwitchModuleUpdater._clear_midplane_down_reason`: the
/// midplane is back, so the next time it goes down is a new event.
pub fn clear_midplane_down_reason(root: &Path, module: &str) {
    match std::fs::remove_file(midplane_reason_path(root, module)) {
        Ok(()) => {}
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
        Err(e) => log::error!("{module}: clear midplane reason failed: {e}"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use platform_api::DeviceKey;
    use pmon_common::db::MockTable;

    fn root() -> tempfile::TempDir {
        tempfile::tempdir().unwrap()
    }

    fn fixed(_when: &str) -> String {
        "Mon Sep 15 01:02:03 PM UTC 2026".to_string()
    }

    fn keep(
        r: &Path,
        module: &str,
        cause: Option<&str>,
        boot_id: Option<&str>,
        name: &str,
    ) -> RebootCause {
        persist(r, module, cause, None, boot_id, name, fixed).unwrap()
    }

    fn answered(cause: &str, detail: &str) -> Result<ModuleRebootCause, PlatformError> {
        Ok(ModuleRebootCause { cause: Some(cause.to_string()), detail: Some(detail.to_string()) })
    }

    /// A platform with one DPU, at `modules[0]`, answering `cause` for it.
    struct Dpus {
        key: Result<Option<DeviceKey>, PlatformError>,
        cause: Result<ModuleRebootCause, PlatformError>,
    }

    impl PlatformApi for Dpus {
        fn get_module_key(&mut self, _name: &str) -> Result<Option<DeviceKey>, PlatformError> {
            self.key.clone()
        }
        fn get_module_reboot_cause(
            &mut self,
            key: &DeviceKey,
        ) -> Result<ModuleRebootCause, PlatformError> {
            assert_eq!(key.as_str(), "modules[0]", "asked through the key the name resolved to");
            self.cause.clone()
        }
    }

    fn dpu(cause: Result<ModuleRebootCause, PlatformError>) -> Dpus {
        Dpus { key: Ok(Some(DeviceKey::from("modules[0]"))), cause }
    }

    /// A platform that must not be asked anything.
    struct Untouched;

    impl PlatformApi for Untouched {
        fn get_module_key(&mut self, _name: &str) -> Result<Option<DeviceKey>, PlatformError> {
            panic!("the platform must not be asked")
        }
    }

    #[test]
    fn a_cause_splits_into_a_cause_and_a_comment() {
        assert_eq!(split_cause(Some("Watchdog,fired at 3am")),
                   ("Watchdog".to_string(), "fired at 3am".to_string()));
        assert_eq!(split_cause(Some("Power Loss")),
                   ("Power Loss".to_string(), "N/A".to_string()));
    }

    /// A platform that answers nothing still gets a record: the DPU did go
    /// down, and an empty history reads as "it never happened".
    #[test]
    fn a_platform_that_says_nothing_still_records_a_reboot() {
        assert_eq!(split_cause(None), ("Unknown".to_string(), "N/A".to_string()));
        assert_eq!(split_cause(Some("")), ("Unknown".to_string(), "N/A".to_string()));
    }

    /// Named for the moment it is taken, and carrying the boot_id it was
    /// taken for.
    #[test]
    fn the_record_is_named_for_now_and_carries_its_boot_id() {
        let r = root();
        let rec = keep(r.path(), "DPU0", Some("Watchdog"), Some("b-1"), "2026_09_15_01_05_00");
        assert_eq!(rec.name, "2026_09_15_01_05_00");
        assert_eq!(rec.boot_id.as_deref(), Some("b-1"));
        let file = history_dir(r.path(), "DPU0").join("2026_09_15_01_05_00_reboot_cause.json");
        let text = std::fs::read_to_string(file).unwrap();
        assert!(text.contains(r#""boot_id": "b-1""#), "{text}");
    }

    /// The old bookkeeping files are gone: nothing writes the plain-text copy
    /// or the down time any more, and no `.tmp` is left behind.
    #[test]
    fn a_record_leaves_only_the_history_file_and_the_link() {
        let r = root();
        keep(r.path(), "DPU0", Some("Watchdog"), Some("b-1"), "2026_09_15_01_05_00");
        let mut names: Vec<String> = std::fs::read_dir(dir_for(r.path(), "DPU0"))
            .unwrap()
            .map(|e| e.unwrap().file_name().to_string_lossy().to_string())
            .collect();
        names.sort();
        assert_eq!(names, vec!["history".to_string(), "previous-reboot-cause.json".to_string()]);
    }

    #[test]
    fn the_latest_record_is_readable_back() {
        let r = root();
        keep(r.path(), "DPU0", Some("Watchdog,x"), Some("b-1"), "2026_09_15_01_00_00");
        let prev = previous(r.path(), "DPU0").expect("the symlink resolves");
        assert_eq!(prev.cause, "Watchdog");
        assert_eq!(prev.comment, "x");
        assert_eq!(prev.device, "DPU0");
        assert_eq!(prev.boot_id.as_deref(), Some("b-1"));
    }

    /// Whatever the vendor put in the cause comes back out, control
    /// characters and quotes included.  Written raw, a newline would make the
    /// file unreadable, the boot_id in it unknowable, and the same reboot
    /// recorded again on every DPU_STATE event.
    #[test]
    fn a_cause_with_control_characters_reads_back() {
        let r = root();
        let cause = "Watchdog\tfired \"twice\"\nsee \\var\\log";
        persist(
            r.path(),
            "DPU0",
            Some("Kernel Panic"),
            Some(cause),
            Some("b-1"),
            "2026_09_15_01_00_00",
            fixed,
        )
        .unwrap();
        let prev = previous(r.path(), "DPU0").expect("valid JSON");
        assert_eq!(prev.comment, cause);
        assert_eq!(prev.boot_id.as_deref(), Some("b-1"), "so the same boot is not recorded twice");
    }

    /// Clearing the old rows is not wrapped in Python, and a delete that fails
    /// here ends the publish too -- the caller then says the record is on disk.
    #[test]
    fn a_row_that_will_not_go_ends_the_publish() {
        let r = root();
        keep(r.path(), "DPU0", Some("a"), Some("b-1"), "2026_09_15_01_00_00");
        let t = MockTable::new();
        t.set("DPU0|2026_01_01_00_00_00", &[("cause", "old".to_string())]).unwrap();
        t.fail_writes("redis is gone");
        publish(r.path(), "DPU0", &t).expect_err("a stale row that will not go is reported");
    }

    /// "No history files" is for a DPU with none.  One whose files will not
    /// decode has said so file by file, and is not also told it has none.
    #[test]
    fn files_that_will_not_decode_are_not_no_files() {
        let log = pmon_common::logging::capture();
        let r = root();
        let dir = history_dir(r.path(), "DPU0");
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(dir.join("2026_09_15_01_00_00_reboot_cause.json"), "{not json").unwrap();
        publish(r.path(), "DPU0", &MockTable::new()).unwrap();
        assert!(log.logged(log::Level::Warn, "Failed to decode JSON from file"));
        assert!(!log.contains("No reboot cause history files found"));
    }

    /// Replacing the link never leaves it missing: the second record's link
    /// points at the second record.
    #[test]
    fn the_link_moves_to_the_newest_record() {
        let r = root();
        keep(r.path(), "DPU0", Some("a"), Some("b-1"), "2026_09_15_01_00_00");
        keep(r.path(), "DPU0", Some("b"), Some("b-2"), "2026_09_15_02_00_00");
        assert_eq!(previous(r.path(), "DPU0").unwrap().cause, "b");
        assert!(!with_suffix(&dir_for(r.path(), "DPU0").join("previous-reboot-cause.json"), ".tmp")
            .exists());
    }

    /// Ten is what `show reboot-cause history` pages through, and the file
    /// name sorts chronologically, which is why it carries that timestamp
    /// format rather than a human-readable one.  A leftover `.tmp` is not a
    /// record and does not take one's place.
    #[test]
    fn the_history_keeps_the_newest_ten_records() {
        let r = root();
        std::fs::create_dir_all(history_dir(r.path(), "DPU0")).unwrap();
        std::fs::write(history_dir(r.path(), "DPU0").join("0000_reboot_cause.json.tmp"), "")
            .unwrap();
        for i in 0..14 {
            keep(r.path(), "DPU0", Some("x"), None, &format!("2026_09_15_01_00_{i:02}"));
        }
        let mut names: Vec<String> = std::fs::read_dir(history_dir(r.path(), "DPU0"))
            .unwrap()
            .map(|e| e.unwrap().file_name().to_string_lossy().to_string())
            .filter(|n| n.ends_with("_reboot_cause.json"))
            .collect();
        names.sort();
        assert_eq!(names.len(), MAX_HISTORY_FILES);
        assert_eq!(names[0], "2026_09_15_01_00_04_reboot_cause.json", "the oldest four went");
    }

    #[test]
    fn the_whole_history_is_republished_and_stale_rows_go() {
        let r = root();
        let t = MockTable::new();
        t.set("DPU0|2020_01_01_00_00_00", &[("cause", "old".to_string())]).unwrap();
        keep(r.path(), "DPU0", Some("Watchdog"), Some("b-1"), "2026_09_15_01_00_00");
        publish(r.path(), "DPU0", &t).unwrap();
        assert_eq!(t.keys(), vec!["DPU0|2026_09_15_01_00_00".to_string()]);
        assert_eq!(t.field("DPU0|2026_09_15_01_00_00", "cause").as_deref(), Some("Watchdog"));
        assert_eq!(t.field("DPU0|2026_09_15_01_00_00", "boot_id").as_deref(), Some("b-1"));
    }

    /// A record from before boot_ids were kept has none, and publishes none.
    #[test]
    fn an_old_record_without_a_boot_id_publishes_without_one() {
        let r = root();
        let t = MockTable::new();
        std::fs::create_dir_all(history_dir(r.path(), "DPU0")).unwrap();
        std::fs::write(
            history_dir(r.path(), "DPU0").join("2026_01_01_00_00_00_reboot_cause.json"),
            concat!(
                r#"{"cause": "Watchdog", "comment": "N/A", "device": "DPU0", "#,
                r#""time": "t", "name": "2026_01_01_00_00_00"}"#,
            ),
        ).unwrap();
        publish(r.path(), "DPU0", &t).unwrap();
        assert_eq!(t.field("DPU0|2026_01_01_00_00_00", "cause").as_deref(), Some("Watchdog"));
        assert_eq!(t.field("DPU0|2026_01_01_00_00_00", "boot_id"), None);
    }

    /// A history file that cannot be read is warned about, as Python's `except`
    /// around `open` warns about it, and the readable ones are still published.
    #[test]
    fn a_file_that_cannot_be_read_is_warned_and_the_rest_are_published() {
        let log = pmon_common::logging::capture();
        let r = root();
        let t = MockTable::new();
        keep(r.path(), "DPU0", Some("a"), None, "2026_09_15_01_00_00");
        // A directory where a file is expected: it matches the name, and
        // reading it fails.
        let bad = history_dir(r.path(), "DPU0").join("2026_09_15_02_00_00_reboot_cause.json");
        std::fs::create_dir(&bad).unwrap();
        publish(r.path(), "DPU0", &t).expect("an unreadable file is not the end of the pass");
        assert_eq!(t.keys(), vec!["DPU0|2026_09_15_01_00_00".to_string()]);
        assert!(log.logged(log::Level::Warn,
            &format!("Error processing file {}", bad.display())));
    }

    /// A row that will not write costs that entry only: it is warned about in
    /// Python's words and the next file is still published.
    #[test]
    fn a_row_that_will_not_write_is_warned_and_the_rest_are_published() {
        let log = pmon_common::logging::capture();
        let r = root();
        let t = MockTable::new();
        keep(r.path(), "DPU0", Some("a"), None, "2026_09_15_01_00_00");
        keep(r.path(), "DPU0", Some("b"), None, "2026_09_15_02_00_00");
        t.fail_writes_after(1, "connection reset");
        publish(r.path(), "DPU0", &t).expect("a refused row is not the end of the pass");
        assert_eq!(t.keys().len(), 1, "one of the two went through");
        assert!(log.logged(log::Level::Warn, "Error processing file"));
        assert!(log.logged(log::Level::Warn, "connection reset"));
    }

    /// Another DPU's rows are not this one's to remove.
    #[test]
    fn republishing_one_dpu_leaves_the_others_alone() {
        let r = root();
        let t = MockTable::new();
        t.set("DPU1|2026_01_01_00_00_00", &[("cause", "other".to_string())]).unwrap();
        keep(r.path(), "DPU0", Some("x"), None, "2026_09_15_01_00_00");
        publish(r.path(), "DPU0", &t).unwrap();
        assert!(t.field("DPU1|2026_01_01_00_00_00", "cause").is_some());
    }

    /// A DPU nobody has recorded anything for answers nothing, rather than
    /// failing -- every DPU is in that state until its first reboot.
    #[test]
    fn a_dpu_with_no_history_is_not_an_error() {
        let r = root();
        assert_eq!(previous(r.path(), "DPU9"), None);
        let t = MockTable::new();
        publish(r.path(), "DPU9", &t).unwrap();
        assert!(t.is_empty());
    }

    /// Both halves of what the platform answered reach the record.
    ///
    /// `ModuleBase.get_reboot_cause()` answers `(cause, detail)`, and the
    /// detail is the half a person reads: it is what `comment` carries into
    /// CHASSIS_STATE_DB and into the on-disk history.
    #[test]
    fn the_detail_the_platform_gave_is_the_comment_that_is_recorded() {
        let r = root();
        let rec = persist(
            r.path(), "DPU0",
            Some("Non-Hardware"), Some("Reset due to Power off"), None,
            "2026_09_20_04_06_29", |_| "when".to_string(),
        )
        .expect("persists");
        assert_eq!(rec.cause, "Non-Hardware");
        assert_eq!(rec.comment, "Reset due to Power off");
        assert!(rec.to_json().contains(r#""comment": "Reset due to Power off""#));
    }

    /// And a platform that answers one string still gets it split, which is
    /// what the comma path is for -- it is not a fallback for the pair.
    #[test]
    fn a_single_string_answer_is_still_split_on_its_comma() {
        let r = root();
        let rec = keep(r.path(), "DPU1", Some("Non-Hardware,Reset from CPU"), None,
                       "2026_09_20_04_06_29");
        assert_eq!((rec.cause.as_str(), rec.comment.as_str()),
                   ("Non-Hardware", "Reset from CPU"));
    }

    // ── boot_id ──────────────────────────────────────────────────────────────

    /// A new boot_id is a reboot: the platform is asked, and the record is
    /// written and published with the boot_id in it.
    #[test]
    fn a_new_boot_id_records_and_publishes_the_reboot() {
        let log = pmon_common::logging::capture();
        let r = root();
        let t = MockTable::new();
        boot_id_update(
            r.path(),
            "DPU0",
            "b-2",
            &mut dpu(answered("Non-Hardware", "Reset due to Power off")),
            &t,
            "2026_09_20_04_06_29",
            fixed,
        );
        let prev = previous(r.path(), "DPU0").expect("recorded");
        assert_eq!((prev.cause.as_str(), prev.comment.as_str()),
                   ("Non-Hardware", "Reset due to Power off"));
        assert_eq!(prev.boot_id.as_deref(), Some("b-2"));
        assert_eq!(t.field("DPU0|2026_09_20_04_06_29", "boot_id").as_deref(), Some("b-2"));
        assert!(log.contains("DPU0: new boot_id b-2 detected, capturing reboot cause"));
    }

    /// The boot_id already recorded is the same boot seen again -- the NPU's
    /// restart replays every DPU_STATE row -- so the platform is not asked.
    #[test]
    fn the_boot_id_already_recorded_records_nothing() {
        let r = root();
        let t = MockTable::new();
        keep(r.path(), "DPU0", Some("Watchdog"), Some("b-1"), "2026_09_15_01_00_00");
        boot_id_update(r.path(), "DPU0", "b-1", &mut Untouched,
                       &t, "2026_09_20_04_06_29", fixed);
        assert!(t.is_empty());
    }

    /// No boot_id published yet is nothing to capture.
    #[test]
    fn an_empty_boot_id_records_nothing() {
        let r = root();
        let t = MockTable::new();
        boot_id_update(r.path(), "DPU0", "", &mut Untouched,
                       &t, "2026_09_20_04_06_29", fixed);
        assert_eq!(previous(r.path(), "DPU0"), None);
    }

    /// A platform that does not implement the getter records "N/A", which is
    /// what `chassisd:try_get` hands Python's persist in that case; one that
    /// answers `None` does the same.
    #[test]
    fn a_platform_without_the_getter_records_not_available() {
        for ask in [
            Err(PlatformError::NotSupported("get_reboot_cause".to_string())),
            Ok(ModuleRebootCause::default()),
        ] {
            let r = root();
            let t = MockTable::new();
            boot_id_update(r.path(), "DPU0", "b-2", &mut dpu(ask), &t, "2026_09_20_04_06_29",
                           fixed);
            let prev = previous(r.path(), "DPU0").expect("recorded");
            assert_eq!((prev.cause.as_str(), prev.comment.as_str()), ("N/A", "N/A"));
        }
    }

    /// A DPU the platform does not know, a lookup that fails, or a getter
    /// that fails records nothing, and says which in Python's words.  The
    /// DPU is looked up before the new boot_id is announced, as Python does.
    #[test]
    fn a_dpu_the_platform_cannot_answer_for_records_nothing() {
        let log = pmon_common::logging::capture();
        let r = root();
        let t = MockTable::new();
        for key in [Ok(None), Err(PlatformError::NotSupported("get_module_index".to_string()))] {
            let mut p = Dpus { key, cause: answered("Watchdog", "x") };
            boot_id_update(r.path(), "DPU7", "b-2", &mut p, &t, "2026_09_20_04_06_29", fixed);
        }
        let lookup = Err(PlatformError::Backend("redis".to_string()));
        let mut p = Dpus { key: lookup, cause: answered("Watchdog", "x") };
        boot_id_update(r.path(), "DPU6", "b-2", &mut p, &t, "2026_09_20_04_06_29", fixed);
        let mut p = dpu(Err(PlatformError::Backend("i2c".to_string())));
        boot_id_update(r.path(), "DPU0", "b-2", &mut p, &t, "2026_09_20_04_06_29", fixed);
        for m in ["DPU7", "DPU6", "DPU0"] {
            assert_eq!(previous(r.path(), m), None);
        }
        assert!(log.logged(log::Level::Error,
            "Unable to get module-index for DPU7 to capture reboot cause"));
        assert!(log.logged(log::Level::Error,
            "Failed to look up module DPU6 to capture reboot cause: redis"));
        assert!(!log.contains("DPU7: new boot_id") && !log.contains("DPU6: new boot_id"));
        assert!(log.logged(log::Level::Error, "Failed to get reboot cause for DPU0: i2c"));
    }

    /// A record that will not publish is still on disk, and says so.
    #[test]
    fn a_record_that_will_not_publish_is_kept_on_disk() {
        let log = pmon_common::logging::capture();
        let r = root();
        let t = MockTable::new();
        t.fail_reads("redis went away");
        boot_id_update(r.path(), "DPU0", "b-2", &mut dpu(answered("Watchdog", "x")),
                       &t, "2026_09_20_04_06_29", fixed);
        assert_eq!(previous(r.path(), "DPU0").unwrap().boot_id.as_deref(), Some("b-2"));
        assert!(log.logged(log::Level::Error, "Failed to update reboot cause to DB for DPU0"));
        assert!(log.contains("The boot_id and reboot cause is stored in json file."));
    }

    // ── midplane-down reason ─────────────────────────────────────────────────

    #[test]
    fn a_midplane_reason_is_kept_until_it_is_cleared() {
        let r = root();
        assert_eq!(read_midplane_down_reason(r.path(), "DPU0"), None);
        write_midplane_down_reason(r.path(), "DPU0", "Unplanned: 'Power Loss'");
        assert_eq!(read_midplane_down_reason(r.path(), "DPU0").as_deref(),
                   Some("Unplanned: 'Power Loss'"));
        clear_midplane_down_reason(r.path(), "DPU0");
        assert_eq!(read_midplane_down_reason(r.path(), "DPU0"), None);
        clear_midplane_down_reason(r.path(), "DPU0");
    }

    /// An empty file is no reason, as Python's `or None` has it.
    #[test]
    fn an_empty_midplane_reason_file_is_no_reason() {
        let r = root();
        std::fs::create_dir_all(dir_for(r.path(), "DPU0")).unwrap();
        std::fs::write(midplane_reason_path(r.path(), "DPU0"), "\n").unwrap();
        assert_eq!(read_midplane_down_reason(r.path(), "DPU0"), None);
    }

    // ── failures ─────────────────────────────────────────────────────────────

    /// A previous record that cannot be read, or is not JSON, is an error that
    /// reads as nothing recorded, so the next boot_id is recorded afresh.
    #[test]
    fn an_unreadable_previous_record_reads_as_nothing_recorded() {
        let log = pmon_common::logging::capture();
        let r = root();
        std::fs::create_dir_all(dir_for(r.path(), "DPU0").join("previous-reboot-cause.json"))
            .unwrap();
        assert_eq!(previous(r.path(), "DPU0"), None);
        assert!(log.logged(log::Level::Error, "DPU0: Failed to read previous-reboot-cause.json"));

        std::fs::create_dir_all(dir_for(r.path(), "DPU1")).unwrap();
        std::fs::write(dir_for(r.path(), "DPU1").join("previous-reboot-cause.json"), "not json")
            .unwrap();
        assert_eq!(previous(r.path(), "DPU1"), None);
        assert!(log.logged(log::Level::Error,
            "DPU1: Failed to read previous-reboot-cause.json: not valid JSON"));
    }

    /// A write that cannot land leaves what was there and no temporary file.
    #[test]
    fn a_write_that_cannot_land_leaves_no_temporary_file() {
        let d = root();
        let target = d.path().join("record.json");
        // A directory with something in it where the file should go: the
        // rename onto it fails.
        std::fs::create_dir_all(target.join("occupied")).unwrap();
        assert!(atomic_write(&target, b"{}").is_err());
        assert!(with_suffix(&target, ".tmp").symlink_metadata().is_err());
        assert!(target.join("occupied").exists(), "what was there is untouched");
    }

    /// A temporary link left by a replacement that was cut off is cleared
    /// before the next one.
    #[test]
    fn a_stale_temporary_link_is_cleared_first() {
        let d = root();
        let link = d.path().join("previous-reboot-cause.json");
        std::os::unix::fs::symlink("gone", with_suffix(&link, ".tmp")).unwrap();
        atomic_replace_symlink(Path::new("history/a.json"), &link).unwrap();
        assert_eq!(std::fs::read_link(&link).unwrap(), Path::new("history/a.json"));
        assert!(with_suffix(&link, ".tmp").symlink_metadata().is_err());
    }

    /// A link that cannot be replaced leaves no temporary link behind.
    #[test]
    fn a_link_that_cannot_be_replaced_leaves_no_temporary_link() {
        let d = root();
        let link = d.path().join("previous-reboot-cause.json");
        std::fs::create_dir_all(link.join("occupied")).unwrap();
        assert!(atomic_replace_symlink(Path::new("history/a.json"), &link).is_err());
        assert!(with_suffix(&link, ".tmp").symlink_metadata().is_err());
    }

    /// A record that cannot be written is not published: the file is the
    /// record, and a row without one would vanish at the next republish.
    #[test]
    fn a_record_that_cannot_be_written_is_not_published() {
        let log = pmon_common::logging::capture();
        let d = root();
        // A file where the directory should be: nothing below it can be made.
        let r = d.path().join("not-a-dir");
        std::fs::write(&r, "").unwrap();
        let t = MockTable::new();
        boot_id_update(&r, "DPU0", "b-2", &mut dpu(answered("Watchdog", "x")), &t,
                       "2026_09_20_04_06_29", fixed);
        assert!(t.is_empty());
        assert!(log.logged(log::Level::Error, "Failed to persist reboot cause for DPU0"));
    }

    /// A reason that cannot be cleared is logged in Python's words.
    #[test]
    fn a_midplane_reason_that_cannot_be_cleared_is_logged() {
        let log = pmon_common::logging::capture();
        let r = root();
        std::fs::create_dir_all(midplane_reason_path(r.path(), "DPU0").join("occupied")).unwrap();
        clear_midplane_down_reason(r.path(), "DPU0");
        assert!(log.logged(log::Level::Error, "DPU0: clear midplane reason failed"));
    }
}
