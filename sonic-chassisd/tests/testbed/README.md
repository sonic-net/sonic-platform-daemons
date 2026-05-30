# Chassisd Testbed Scripts

End-to-end verification scripts for SmartSwitch DPU reboot-cause tracking.
These scripts run on a real SmartSwitch testbed (not in CI) and validate
chassisd behavior across various status transitions.

## Scripts

- **test_dpu_reboot_cause_persistence.sh** — Full testbed script covering
  6 scenarios: normal reboot, config-reload during reboot, config-reload with
  DPU offline, history preservation, deferred same-cause, and back-to-back
  reboot deduplication.

## Usage

```bash
# Copy patched chassisd to the switch
scp sonic-chassisd/scripts/chassisd admin@<SWITCH>:/tmp/chassisd_patched

# SSH to the switch and run
sudo bash test_dpu_reboot_cause_persistence.sh -d DPU0
```

## Prerequisites

- SmartSwitch with at least one DPU
- Root access on the switch
- Patched chassisd copied to the switch
