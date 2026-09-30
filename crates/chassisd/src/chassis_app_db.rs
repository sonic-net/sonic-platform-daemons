//
// SPDX-FileCopyrightText: NVIDIA CORPORATION & AFFILIATES
// Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// Apache-2.0
//

//! Removing a long-gone line card's rows from the chassis app DB.
//!
//! Ports `chassisd:ModuleUpdater._cleanup_chassis_app_db` and
//! `chassisd:ModuleUpdater.module_down_chassis_db_cleanup`.  Only the
//! supervisor does this, and only for a line card that has been off-line for
//! half an hour: the rows are the card's contribution to the chassis-wide
//! forwarding state, and a card that is rebooting will want them back.

use std::process::Command;
use std::time::{Duration, SystemTime};



use crate::module_updater::{DownModule, Tables};
use crate::names::*;

/// `chassisd:CHASSIS_DB_CLEANUP_MODULE_DOWN_PERIOD` -- half an hour, in minutes
/// there and here in full.
pub const CLEANUP_AFTER: Duration = Duration::from_secs(30 * 60);

/// The Lua the cleanup runs, verbatim from the `script` in
/// `chassisd:ModuleUpdater._cleanup_chassis_app_db`.
///
/// It deletes only the rows the one ASIC that went away created, which is why
/// it is a script and not four `DEL`s: the LAG tables carry an id allocation
/// that has to be given back in the same pass, or the chassis leaks LAG ids
/// every time a card is pulled.
const CLEANUP_SCRIPT: &str = r#"local host = string.gsub(ARGV[1], '%-', '%%-')
local dev = ARGV[2]
local tables = {'SYSTEM_NEIGH*', 'SYSTEM_INTERFACE*', 'SYSTEM_LAG_MEMBER_TABLE*'}
for i = 1, table.getn(tables) do
    local ps = tables[i] .. '|' .. host .. '|' .. dev
    local keylist = redis.call('KEYS', tables[i])
    for j,key in ipairs(keylist) do
        if string.match(key, ps) ~= nil then
            redis.call('DEL', key)
        end
    end
end
local ps = 'SYSTEM_LAG_TABLE*|' .. '(' .. host .. '|' .. dev ..'.*' .. ')'
local keylist = redis.call('KEYS', 'SYSTEM_LAG_TABLE*')
for j,key in ipairs(keylist) do
    local lagname = string.match(key, ps)
    if lagname ~= nil then
        redis.call('DEL', key)
        local lagid = redis.call('HGET', 'SYSTEM_LAG_ID_TABLE', lagname)
        redis.call('SREM', 'SYSTEM_LAG_ID_SET', lagid)
        redis.call('HDEL', 'SYSTEM_LAG_ID_TABLE', lagname)
        redis.call('rpush', 'SYSTEM_LAG_IDS_FREE_LIST', lagid)
    end
end
return"#;

/// Split a down-module key back into the module and the hostname it ran under.
///
/// An empty hostname means the card never published one, and the chassis app DB
/// is partitioned by hostname -- so there is nothing to address the cleanup to.
pub fn split_down_key(key: &str) -> (&str, &str) {
    key.split_once('|').unwrap_or((key, ""))
}

/// Which modules are due a cleanup now.
///
/// Pure, so the half-hour arithmetic and the "only line cards, only once"
/// conditions can be tested without a redis or a clock.
pub fn due<'a>(
    is_supervisor: bool,
    down: impl Iterator<Item = (&'a String, &'a DownModule)>,
    now: SystemTime,
) -> Vec<String> {
    if !is_supervisor {
        // A line card has no view of the chassis app DB to clean.
        return Vec::new();
    }
    down.filter(|(_, m)| !m.cleaned)
        .filter(|(_, m)| now.duration_since(m.down_time).unwrap_or_default() >= CLEANUP_AFTER)
        .map(|(k, _)| k.clone())
        .collect()
}

/// Run the cleanup for every ASIC of one module.
///
/// Returns whether the module should be marked cleaned; it is marked either
/// way, as Python does -- a cleanup that cannot be addressed will not become
/// addressable by waiting, and retrying every ten seconds forever is the
/// alternative.
pub fn cleanup(down_key: &str, tables: &Tables<'_>) -> bool {
    let (module, host) = split_down_key(down_key);
    // The supervisor is tracked as a down module like any other, but it has no
    // rows in the chassis app DB to remove -- the forwarding state there is the
    // line cards'.  `chassisd:ModuleUpdater.module_down_chassis_db_cleanup`
    // gates on the same prefix.
    if !module.starts_with(platform_api::ModuleType::LineCard.as_str()) {
        return true;
    }
    if host.is_empty() {
        pmon_common::notice!(
            "Host name is not available for Module {module}. Chassis db clean up not done!");
        return true;
    }

    let asics = match asics_of(module, tables) {
        Ok(a) => a,
        // Python reads this count bare
        // (`chassisd:ModuleUpdater._cleanup_chassis_app_db`); an unreadable
        // hostname table means the cleanup cannot know what to address and must
        // not guess.
        Err(e) => {
            log::error!("Failed to read the ASIC count of {module}: {e}");
            return false;
        }
    };
    for asic in asics {
        match run_script(host, &asic) {
            Ok(()) => pmon_common::notice!(
                "Cleaned up chassis app db entries for {module}({host})/{asic}"),
            Err(e) => log::error!(
                "Failed to clean up chassis app db entries for {module}({host})/{asic}: {e}"),
        }
    }
    true
}

/// The ASIC names one module contributed, from the count it published.
///
/// The count is the line card's own statement of how many it has; a card that
/// never published one contributes nothing to clean up, which is the safe
/// reading -- the alternative is guessing a number and deleting rows that
/// belong to whatever really has that ASIC id.
fn asics_of(module: &str, tables: &Tables<'_>) -> Result<Vec<String>, String> {
    let n = tables
        .hostname
        .get(module)?
        .and_then(|row| row.into_iter().find(|(k, _)| k == NUM_ASICS_FIELD))
        .and_then(|(_, v)| v.trim().parse::<usize>().ok())
        .unwrap_or(0);
    Ok((0..n).map(|i| format!("{ASIC_PREFIX}{i}")).collect())
}

/// `redis-cli EVAL`, against the chassis-wide redis.
///
/// Python loads the script once and calls `EVALSHA`
/// (`chassisd:ModuleUpdater._cleanup_chassis_app_db`).  `EVAL` is the same
/// operation with the script inline -- redis caches it by hash on first use
/// either way -- and it removes a load step whose failure mode is a stale sha
/// against a redis that restarted underneath it.  This runs at most once per
/// ASIC per pulled card.
fn run_script(host: &str, asic: &str) -> std::io::Result<()> {
    let out = Command::new("redis-cli")
        .args([
            "-h", "redis_chassis.server",
            "-p", "6380",
            "-n", "12",
            "EVAL", CLEANUP_SCRIPT,
            "0", host, asic,
        ])
        .output()?;
    if out.status.success() {
        Ok(())
    } else {
        Err(std::io::Error::other(String::from_utf8_lossy(&out.stderr).trim().to_string()))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use pmon_common::db::{MockTable, TableLike};
    use std::collections::BTreeMap;

    /// A module that went down at a fixed instant, so the arithmetic below is
    /// against a clock the test owns.
    const WENT_DOWN: u64 = 100_000;

    fn down(cleaned: bool) -> DownModule {
        DownModule {
            down_time: SystemTime::UNIX_EPOCH + Duration::from_secs(WENT_DOWN),
            cleaned,
            slot: 1,
        }
    }

    fn at(minutes_after: u64) -> SystemTime {
        SystemTime::UNIX_EPOCH + Duration::from_secs(WENT_DOWN + minutes_after * 60)
    }

    fn table(entries: &[(&str, DownModule)]) -> BTreeMap<String, DownModule> {
        entries.iter().map(|(k, v)| (k.to_string(), v.clone())).collect()
    }

    /// The half hour is what separates a reboot from a removal.  Cleaning up
    /// after a card that is rebooting throws away forwarding state it is about
    /// to want back.
    #[test]
    fn a_card_is_left_alone_until_it_has_been_gone_for_half_an_hour() {
        let d = table(&[("LINE-CARD0|lc1", down(false))]);
        assert!(due(true, d.iter(), at(29)).is_empty());
        assert_eq!(due(true, d.iter(), at(30)), vec!["LINE-CARD0|lc1".to_string()]);
    }

    /// Once only: the flag is what stops the supervisor re-running a LAG-id
    /// return every ten seconds for as long as the card stays out.
    #[test]
    fn a_card_already_cleaned_is_not_cleaned_again() {
        let d = table(&[("LINE-CARD0|lc1", down(true))]);
        assert!(due(true, d.iter(), at(120)).is_empty());
    }

    /// Only the supervisor has a view of the chassis app DB.
    #[test]
    fn a_line_card_cleans_up_nothing() {
        let d = table(&[("LINE-CARD0|lc1", down(false))]);
        assert!(due(false, d.iter(), at(120)).is_empty());
    }

    #[test]
    fn a_down_key_splits_into_the_module_and_its_hostname() {
        assert_eq!(split_down_key("LINE-CARD0|lc1"), ("LINE-CARD0", "lc1"));
        assert_eq!(split_down_key("LINE-CARD0|"), ("LINE-CARD0", ""));
    }

    /// The supervisor is tracked like any other down module, but the rows in
    /// the chassis app DB are the line cards' -- there is nothing of its own
    /// to remove, and running the script against it would be a scan of every
    /// SYSTEM_* key for nothing.
    #[test]
    fn a_supervisor_going_down_cleans_up_nothing() {
        let t = MockTable::new();
        let tables = Tables {
            chassis: &t, module: &t, midplane: &t, entity: &t,
            asic: &t, hostname: &t, module_reboot: &t, config: None,
        };
        assert!(cleanup("SUPERVISOR0|sup", &tables));
        assert_eq!(t.scans(), 0);
    }

    /// The count is the line card's own statement.  A card that never
    /// published one contributes nothing: guessing a number would delete rows
    /// belonging to whatever really has that ASIC id.
    #[test]
    fn the_asics_to_clean_come_from_the_count_the_card_published() {
        let t = MockTable::new();
        let tables = Tables {
            chassis: &t, module: &t, midplane: &t, entity: &t,
            asic: &t, hostname: &t, module_reboot: &t, config: None,
        };
        assert!(asics_of("LINE-CARD0", &tables).unwrap().is_empty());

        t.set("LINE-CARD0", &[(NUM_ASICS_FIELD, "2".to_string())]).unwrap();
        assert_eq!(asics_of("LINE-CARD0", &tables).unwrap(),
                   vec!["asic0".to_string(), "asic1".to_string()]);

        t.set("LINE-CARD0", &[(NUM_ASICS_FIELD, "many".to_string())]).unwrap();
        assert!(asics_of("LINE-CARD0", &tables).unwrap().is_empty(), "an unreadable count is none");
    }

    /// Without a hostname there is nothing to address the cleanup to: the
    /// chassis app DB is partitioned by it.  The module is still marked, so
    /// this is not retried every cycle forever.
    #[test]
    fn a_module_that_never_published_a_hostname_is_marked_and_skipped() {
        let t = MockTable::new();
        let tables = Tables {
            chassis: &t, module: &t, midplane: &t, entity: &t,
            asic: &t, hostname: &t, module_reboot: &t, config: None,
        };
        assert!(cleanup("LINE-CARD0|", &tables), "marked, so it is not retried");
        assert_eq!(t.scans(), 0, "and nothing was looked up");
    }

    /// A line card that published no ASIC count has nothing to clean, and is
    /// marked: there is no script to run and nothing to retry.
    #[test]
    fn a_card_with_no_asics_is_marked_without_running_anything() {
        let t = MockTable::new();
        let tables = Tables {
            chassis: &t, module: &t, midplane: &t, entity: &t,
            asic: &t, hostname: &t, module_reboot: &t, config: None,
        };
        t.set("LINE-CARD0", &[("hostname", "lc0".to_string())]).unwrap();
        assert!(cleanup("LINE-CARD0|lc0", &tables));
    }

    /// A hostname table that cannot be read leaves the card unmarked, so the
    /// next cycle tries again.
    ///
    /// Unlike a missing count, an unreadable one is not "nothing to clean":
    /// the card may well have ASICs whose rows are still in the chassis app
    /// DB, and marking it would leave them there for good.  Python reads the
    /// count bare (`chassisd:ModuleUpdater._cleanup_chassis_app_db`) and does
    /// not get as far as marking either.
    #[test]
    fn an_unreadable_hostname_table_leaves_the_card_unmarked() {
        let t = MockTable::new();
        let tables = Tables {
            chassis: &t, module: &t, midplane: &t, entity: &t,
            asic: &t, hostname: &t, module_reboot: &t, config: None,
        };
        t.fail_reads("redis is gone");
        assert!(!cleanup("LINE-CARD0|lc0", &tables), "not marked: it has to be retried");
        assert!(asics_of("LINE-CARD0", &tables).is_err());
    }
}
