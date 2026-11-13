//! Static and dynamic STORAGE_INFO publication.
use crate::formatted_time;
use crate::fsio::Reconciler;
use platform_api::StorageDeviceInfo;
use pmon_common::db::TableLike;

/// Model and serial, which do not change while the switch is up.
pub(crate) fn publish_static(devices: &[StorageDeviceInfo], table: &dyn TableLike) {
    for d in devices {
        if !d.available {
            pmon_common::notice!(
                "{} does not have an instantiated object. Static Information cannot be gathered.",
                d.name
            );
            continue;
        }
        // The model or the serial raised: Python's per-device `except` in
        // `stormond:DaemonStorage.get_static_fields_update_state_db`, at ERR,
        // and nothing published for the disk.
        if let Some(e) = &d.static_error {
            log::error!("get_static_fields_update_state_db() failed with: {e}");
            continue;
        }
        pmon_common::notice!(
            "Storage Device: {}, Device Model: {}, Serial: {}",
            d.name,
            d.model,
            d.serial
        );
        if let Err(e) = table.set(
            &d.name,
            &[
                ("device_model", d.model.clone()),
                ("serial", d.serial.clone()),
            ],
        ) {
            log::error!("get_static_fields_update_state_db() failed with: {e}");
        }
    }
}

/// Everything that moves, including the reconciled lifetime totals.
pub(crate) fn publish_dynamic(
    devices: &[StorageDeviceInfo],
    reconciler: &Reconciler,
    table: &dyn TableLike,
) {
    for d in devices {
        if !d.available {
            pmon_common::notice!(
                "Storage device '{}' does not have an instantiated object. \
                 Dynamic Information cannot be gathered.",
                d.name
            );
            continue;
        }
        // A reading raised: the per-device `except` in
        // `stormond:DaemonStorage.get_dynamic_fields_update_state_db`, at
        // NOTICE, and nothing published for the disk this pass.
        if let Some(e) = &d.error {
            pmon_common::notice!("get_dynamic_fields_update_state_db() failed with: {e}");
            continue;
        }

        let (latest_reads, latest_writes) = (d.fs_io_reads, d.fs_io_writes);
        let num = |v: Option<i64>| {
            v.map_or_else(
                || pmon_common::fmt::NOT_AVAILABLE.to_string(),
                |n| n.to_string(),
            )
        };

        // A counter that could not be read keeps the last known-good values,
        // republished from STATE_DB ("0" where there are none), as
        // `stormond:DaemonStorage.get_dynamic_fields_update_state_db` does
        // through `stormond:DaemonStorage._get_last_fsio_statedb_value`.
        //
        // It comes in two shapes.  Both counters at 0 is the one Python knows:
        // the platform's FSIO getters answer 0 when /proc/diskstats cannot be
        // read, as when the disk has gone read-only, and a disk in use never
        // has both at 0 -- reconciling that 0 would collapse the lifetime
        // totals.  A counter that is not there at all (`None`: the getter
        // raised, or answered something that is not a number) is the other.
        // Python fails on that one in its arithmetic, inside the per-device
        // `try`, and writes nothing for the disk, so last cycle's row stays;
        // republishing the same values here comes to the same thing.  What
        // must not reach the table is a placeholder: these four fields are
        // what both baselines are read from. An "N/A" would invalidate this
        // disk's STATE_DB baseline; file sync must retain its previous counters
        // while continuing to persist the other healthy disks.
        let [published_reads, published_writes, total_reads, total_writes] =
            match (latest_reads, latest_writes) {
                (Some(r), Some(w)) if (r, w) != (0, 0) => {
                    let (tr, tw) = reconciler.totals(&d.name, r, w);
                    [r.to_string(), w.to_string(), tr.to_string(), tw.to_string()]
                }
                _ => {
                    log::warn!(
                        "FSIO counters unavailable for {}. Retaining last known-good FSIO values.",
                        d.name
                    );
                    let row = match table.get(&d.name) {
                        Ok(row) => row.unwrap_or_default(),
                        // Python reads these inside the per-device try, so a
                        // failed read skips this disk with the same NOTICE.
                        Err(e) => {
                            pmon_common::notice!(
                                "get_dynamic_fields_update_state_db() failed with: {e}"
                            );
                            continue;
                        }
                    };
                    let last = |field: &str| {
                        row.iter()
                            .find(|(k, _)| k == field)
                            .map_or_else(|| "0".to_string(), |(_, v)| v.clone())
                    };
                    [
                        last("latest_fsio_reads"),
                        last("latest_fsio_writes"),
                        last("total_fsio_reads"),
                        last("total_fsio_writes"),
                    ]
                }
            };

        let last_sync = formatted_time();
        let fvs = [
            ("firmware", d.firmware.clone()),
            ("health", d.health.clone()),
            ("temperature", d.temperature.clone()),
            ("latest_fsio_reads", published_reads),
            ("latest_fsio_writes", published_writes),
            ("total_fsio_reads", total_reads.clone()),
            ("total_fsio_writes", total_writes.clone()),
            ("disk_io_reads", d.disk_io_reads.clone()),
            ("disk_io_writes", d.disk_io_writes.clone()),
            ("reserved_blocks", d.reserved_blocks.clone()),
            ("last_sync_time", last_sync.clone()),
        ];
        if let Err(e) = table.set(&d.name, &fvs) {
            // NOTICE, not WARNING: Python's dynamic pass catches this in
            // `stormond:DaemonStorage.get_dynamic_fields_update_state_db` with
            // `log_notice`.  Its static counterpart uses `log_error`
            // (`stormond:DaemonStorage.get_static_fields_update_state_db`) and
            // so does `publish_static` above.
            pmon_common::notice!("get_dynamic_fields_update_state_db() failed with: {e}");
            continue;
        }

        pmon_common::notice!(
            "Storage Device: {}, Firmware: {}, health: {}%, Temp: {}C, \
             FS IO Reads: {}, FS IO Writes: {}",
            d.name,
            d.firmware,
            d.health,
            d.temperature,
            total_reads,
            total_writes
        );
        // This cycle's readings, not what was published: a failed read still
        // shows 0 here, which is how the log tells it apart.
        pmon_common::notice!(
            "Latest FSIO Reads: {}, Latest FSIO Writes: {}",
            num(latest_reads),
            num(latest_writes)
        );
        pmon_common::notice!(
            "Disk IO Reads: {}, Disk IO Writes: {}, Reserved Blocks: {}",
            d.disk_io_reads,
            d.disk_io_writes,
            d.reserved_blocks
        );
        pmon_common::notice!("Last successful sync time to STATE_DB: {last_sync}");
    }
}
