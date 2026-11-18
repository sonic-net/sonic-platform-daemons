# Rust storage daemon persistence

FSIO baselines are selected per disk from STATE_DB, then the persisted JSON
file, then the current kernel counters. A mixed selection is reported once at
startup. Failed disks retain their previous baseline; unknown counters are
written as JSON null, which both Rust and the unchanged Python loader reject.
Healthy disks can still advance their persisted counters.

The boot identity added to JSON lets Rust distinguish a same-boot restart from
a reboot. The Python loader accepts the extra metadata but does not use it.
If Python falls back to a JSON file in the same boot, it adds the current kernel
counters to the stored total and can count them twice. This is an existing
Python limitation. Switching providers does not migrate that arithmetic.
A rollback to Python requires a reboot or a verified valid STATE_DB baseline;
do not rely on a same-boot JSON fallback for counter continuity.

Storage reads run on a worker. Shutdown persists already published counters
and waits a bounded time for that worker. If a vendor read remains blocked,
process exit skips the vendor atexit/finalize cleanup to avoid waiting forever.

Rust accepts lifetime totals from 0 through `i64::MAX`. If a saved baseline
produces a total outside this range, Rust logs one warning for that disk
and counter. It then uses the current kernel counter as the total for the
rest of the process. The other counter keeps its valid baseline.
Python can publish integer values outside this range. Its implementation
is unchanged. Detection of disk replacement with valid counters remains
separate work. Review context:
https://github.com/sonic-net/sonic-platform-daemons/pull/919#discussion_r4544955333


STATE_DB may be restored across warm, fast and express reboots. Rust records
`boot_id` alongside `FSSTATS_SYNC.successful_sync_time` and reuses that baseline
only in the same boot. A different or missing identity falls back to the saved
JSON baseline. This prevents subtracting the previous boot's kernel counters
from lifetime totals. The Python daemon is unchanged and ignores the extra field.
