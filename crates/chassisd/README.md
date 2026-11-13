# Rust chassisd behavior

The modular-chassis and SmartSwitch paths share the runtime from the thermalctld
PR.

## Clean shutdown and errors

A clean shutdown removes this daemon's local rows. On a line card, cleanup of
the shared `CHASSIS_STATE_DB` `CHASSIS_ASIC_TABLE` is limited to keys beginning
with `LINE-CARD<slot-1>|`. The separator is part of the ownership prefix, so
stopping `LINE-CARD1` preserves `LINE-CARD10` and every other card's ASIC rows.
The supervisor retains the fabric ASIC rows.

This deliberately differs from Python `ModuleUpdater.deinit`, which deletes
all keys in the shared ASIC table on a line card. The
`line_card_shutdown_keeps_peer_asics` parity scenario runs the real cleanup
methods against two line cards, checks the surviving Rust peer row exactly,
and records only that specific row as a known difference from Python.
The line-card unit test also covers the reverse shutdown order and similar
key prefixes. A corresponding Python ownership fix is separate work.

An error exit preserves previously published state. It does not synthesize a
down state for a DPU whose plane state could not be read; the error-exit test
checks that both existing plane values and the write count stay unchanged.

## Administrative changes

Administrative changes currently retain Python's thread-per-change behavior.
A bounded worker per module with replacement of pending requests remains
follow-up work; an in-progress hardware operation must keep its serialization
and platform hook ordering.
