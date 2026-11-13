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

Disk replacement and counter-reset policy are separate follow-up work; the
existing counter arithmetic is preserved. Review context:
https://github.com/sonic-net/sonic-platform-daemons/pull/919#discussion_r4544955333
