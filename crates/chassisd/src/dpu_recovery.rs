//! The DPU recovery state machine.
//!
//! Ports `chassisd:SmartSwitchModuleUpdater._process_single_dpu_recovery` and
//! the transitions around it
//! (`chassisd:SmartSwitchModuleUpdater._is_planned_transition_in_progress`,
//! `chassisd:SmartSwitchModuleUpdater._enter_power_cycle_or_unrecoverable`,
//! `chassisd:SmartSwitchModuleUpdater._enter_booting`,
//! `chassisd:SmartSwitchModuleUpdater._enter_ready`,
//! `chassisd:SmartSwitchModuleUpdater._enter_wait_for_self_recovery`,
//! `chassisd:SmartSwitchModuleUpdater._should_handle_hw_offline`,
//! `chassisd:SmartSwitchModuleUpdater._handle_boot_timeout_expired`,
//! `chassisd:SmartSwitchModuleUpdater.init_dpu_recovery_state`,
//! `chassisd:SmartSwitchModuleUpdater._npu_crash_on_last_boot`,
//! `chassisd:SmartSwitchModuleUpdater.update_dpu_recovery_state`).  One DPU
//! that stops answering is power cycled, up to a limit, and then left alone for
//! an operator -- and the whole value of the thing is in *not* power cycling: a
//! planned reboot, a data plane that has not converged yet, and a DPU an
//! operator has shut down all look like failure from the outside and none of
//! them is one.
//!
//! Written as a step function over explicit inputs so every one of those cases
//! is a test rather than a thing to reason about. Power cycling real hardware
//! in a loop is not a mistake that shows up in a unit test otherwise.

use std::time::{Duration, Instant};

use platform_api::{ModuleMidplaneDownReason, PlatformError};

/// Where one DPU is in its recovery.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DpuState {
    /// Coming up, with a deadline.
    Booting,
    /// All three planes up.
    Ready,
    /// Just failed; given a grace period to come back on its own before
    /// anything is done to it.
    WaitForSelfRecovery,
    /// Power cycled, waiting to see whether it worked.
    PowerCycle,
    /// Auto-recovery is off, or it has been tried and did not work.
    ManualIntervention,
    /// The reset limit is spent.  Terminal until an operator shuts the DPU
    /// down and starts it again.
    Unrecoverable,
    /// An operator has it shut down.
    AdminDown,
}

/// `RECOVERY_STATUS` as CHASSIS_STATE_DB carries it.
pub const RECOVERY_RECOVERABLE: &str = "recoverable";
pub const RECOVERY_UNRECOVERABLE: &str = "unrecoverable";

/// `chassisd:DEFAULT_DPU_BOOT_TIMEOUT`,
/// `chassisd:DEFAULT_DPU_SELF_RECOVERY_TIMEOUT` and
/// `chassisd:DEFAULT_DPU_RESET_LIMIT`, overridable from platform.json.
#[derive(Debug, Clone, Copy)]
pub struct Thresholds {
    pub boot_timeout: Duration,
    pub self_recovery_timeout: Duration,
    pub reset_limit: u32,
}

impl Default for Thresholds {
    fn default() -> Self {
        Self {
            boot_timeout: Duration::from_secs(600),
            self_recovery_timeout: Duration::from_secs(300),
            reset_limit: 2,
        }
    }
}

/// `chassisd:MINIMUM_SELF_RECOVERY_GRACE_PERIOD` and
/// `chassisd:MINIMUM_SELF_RECOVERY_POLL_COUNT`.  A DPU is given at least this
/// much of the grace period however fast the loop runs, so a short poll
/// interval cannot shorten it.
const MINIMUM_GRACE: Duration = Duration::from_secs(30);
const MINIMUM_GRACE_POLLS: u32 = 3;

/// The three planes, as CHASSIS_STATE_DB reports them.
///
/// `None` is "the field is not there", which is not the same as `down`: a DPU
/// that has never published is not a DPU that has failed.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Planes {
    pub midplane: Option<String>,
    pub control: Option<String>,
    pub data: Option<String>,
}

impl Planes {
    fn is(v: &Option<String>, what: &str) -> bool {
        v.as_deref() == Some(what)
    }
    pub fn all_up(&self) -> bool {
        Self::is(&self.midplane, "up")
            && Self::is(&self.control, "up")
            && Self::is(&self.data, "up")
    }
    fn mp_up(&self) -> bool {
        Self::is(&self.midplane, "up")
    }
    fn cp_up(&self) -> bool {
        Self::is(&self.control, "up")
    }
    fn mp_down(&self) -> bool {
        Self::is(&self.midplane, "down")
    }
    fn cp_down(&self) -> bool {
        Self::is(&self.control, "down")
    }
    fn dp_down(&self) -> bool {
        Self::is(&self.data, "down")
    }
    fn show(v: &Option<String>) -> &str {
        v.as_deref().unwrap_or("None")
    }
}

/// What one step wants written to CHASSIS_STATE_DB.
///
/// Returned rather than written so the machine stays a function of its inputs;
/// the caller does the I/O.
#[derive(Debug, Clone, PartialEq)]
pub enum Effect {
    ReadyStatus(bool),
    LastDownTime,
    LastReadyTime,
    ResetCount(u32),
    RecoveryStatus(&'static str),
}

/// What a SmartSwitch pass asks of the hardware.
///
/// Power cycling one DPU is what the recovery machine needs.  The midplane
/// check needs one more answer, why a DPU's midplane went down, and it asks
/// through the same handle because the pass holds the platform through this
/// one borrow and cannot take a second.
pub trait PowerCycler {
    /// Take the state-transition lock, run the graceful down/up, release it.
    ///
    /// False when the lock could not be taken, which means another operation
    /// owns this DPU and nothing was done to it.
    fn power_cycle(&mut self, module: &str) -> bool;

    /// `ModuleBase.get_midplane_down_reason()` for one DPU, asked at the
    /// moment its midplane goes down.  Not implemented unless the handle
    /// reaches a platform.
    fn midplane_down_reason(
        &mut self,
        module: &str,
    ) -> Result<ModuleMidplaneDownReason, PlatformError> {
        let _ = module;
        Err(PlatformError::NotSupported("get_module_midplane_down_reason".to_string()))
    }
}

/// What the machine is told each cycle.
pub struct Inputs<'a> {
    pub planes: &'a Planes,
    /// `Offline` is the one value that matters.
    pub oper_status: &'a str,
    pub admin_up: bool,
    pub auto_recovery: bool,
    /// A planned shutdown or reboot is under way.  The control plane goes down
    /// during one, and reading that as a failure would have the daemon fight
    /// the operator.
    pub transition_in_progress: bool,
    pub now: Instant,
}

/// One DPU's recovery state.
#[derive(Debug, Clone)]
pub struct Recovery {
    pub state: DpuState,
    pub reset_count: u32,
    boot_start: Instant,
    self_recovery_start: Instant,
    self_recovery_polls: u32,
    /// The "data plane still not up" warning is worth saying once per boot.
    boot_dp_warning_logged: bool,
    /// Whether the last cycle already reported the data plane down, so a
    /// steady dp-down DPU does not rewrite two fields every ten seconds.
    dp_was_down: bool,
    /// Carried across the AdminDown state so an operator shutting an
    /// unrecoverable DPU down and starting it again clears the condition --
    /// which is the documented way out of Unrecoverable.
    was_unrecoverable: bool,
}

impl Recovery {
    pub fn new(now: Instant) -> Self {
        Self {
            state: DpuState::Booting,
            reset_count: 0,
            boot_start: now,
            self_recovery_start: now,
            self_recovery_polls: 0,
            boot_dp_warning_logged: false,
            dp_was_down: false,
            was_unrecoverable: false,
        }
    }

    fn enter_booting(&mut self, now: Instant) {
        self.state = DpuState::Booting;
        self.boot_start = now;
        self.boot_dp_warning_logged = false;
    }

    fn enter_ready(&mut self, name: &str, why: &str, out: &mut Vec<Effect>) {
        self.state = DpuState::Ready;
        out.push(Effect::ReadyStatus(true));
        out.push(Effect::LastReadyTime);
        log::info!("{name}: {why}");
    }

    fn enter_wait_for_self_recovery(&mut self, now: Instant) {
        self.state = DpuState::WaitForSelfRecovery;
        self.self_recovery_start = now;
        self.self_recovery_polls = 0;
    }

    /// Power cycle, or give up if the limit is spent.
    ///
    /// The limit is checked *before* counting, so `reset_limit = 2` means two
    /// power cycles and not three.
    fn power_cycle_or_give_up(
        &mut self,
        name: &str,
        t: &Thresholds,
        now: Instant,
        hw: &mut dyn PowerCycler,
        out: &mut Vec<Effect>,
    ) {
        if self.reset_count >= t.reset_limit {
            self.state = DpuState::Unrecoverable;
            out.push(Effect::RecoveryStatus(RECOVERY_UNRECOVERABLE));
            log::error!(
                "{name}: Reached reset_limit ({}). Marking as unrecoverable.", t.reset_limit);
            return;
        }

        self.reset_count += 1;
        out.push(Effect::ResetCount(self.reset_count));
        log::warn!(
            "{name}: Initiating power-cycle (reset_count={}/{})", self.reset_count, t.reset_limit);

        if !hw.power_cycle(name) {
            // Another operation holds the DPU.  The attempt is given back --
            // nothing was done to the hardware, so counting it would spend the
            // budget on an operation that never happened.
            log::warn!(
                "{name}: Cannot acquire state transition lock; another operation in progress \
                 — skipping power-cycle");
            self.reset_count -= 1;
            out.push(Effect::ResetCount(self.reset_count));
        }

        // Entered either way: the boot timeout is what brings the daemon back
        // to try again, and a DPU whose lock was busy still needs watching.
        self.state = DpuState::PowerCycle;
        self.boot_start = now;
    }

    fn boot_timeout_expired(
        &mut self,
        name: &str,
        t: &Thresholds,
        context: &str,
        input: &Inputs<'_>,
        hw: &mut dyn PowerCycler,
        out: &mut Vec<Effect>,
    ) {
        log::warn!("{name}: Boot timeout ({}s) expired {context}", t.boot_timeout.as_secs());
        out.push(Effect::ReadyStatus(false));
        if input.auto_recovery {
            self.power_cycle_or_give_up(name, t, input.now, hw, out);
        } else {
            self.state = DpuState::ManualIntervention;
        }
    }

    /// One cycle of the machine for one DPU.
    pub fn step(
        &mut self,
        name: &str,
        input: &Inputs<'_>,
        t: &Thresholds,
        hw: &mut dyn PowerCycler,
    ) -> Vec<Effect> {
        let mut out = Vec::new();
        let now = input.now;

        // Checked first, and before Unrecoverable, so that shutting an
        // unrecoverable DPU down and starting it again is the way out of that
        // state.  Reordering these two would make Unrecoverable permanent.
        if !input.admin_up {
            if self.state != DpuState::AdminDown {
                if self.state == DpuState::Unrecoverable {
                    self.was_unrecoverable = true;
                }
                self.state = DpuState::AdminDown;
                out.push(Effect::ReadyStatus(false));
            }
            return out;
        }

        if self.state == DpuState::Unrecoverable {
            return out;
        }

        // A planned shutdown or reboot takes the control plane down.  Reading
        // that as a failure would have the daemon power cycle a DPU an
        // operator is deliberately restarting.
        if input.transition_in_progress {
            return out;
        }

        // Only a DPU that *was* Ready treats Offline as a new hardware fault.
        // An allow-list rather than a deny-list: a stale Offline snapshot seen
        // in AdminDown would otherwise power cycle a DPU that is merely
        // starting up.
        if input.oper_status == "Offline" && self.state == DpuState::Ready {
            log::warn!("{name}: Hardware failure detected (oper_status: Offline)");
            out.push(Effect::ReadyStatus(false));
            out.push(Effect::LastDownTime);
            if input.auto_recovery {
                self.power_cycle_or_give_up(name, t, now, hw, &mut out);
            } else {
                self.state = DpuState::ManualIntervention;
            }
            return out;
        }

        match self.state {
            DpuState::AdminDown => {
                // Admin has gone down -> up.  An operator doing that is also
                // how an unrecoverable DPU gets its budget back.
                if std::mem::take(&mut self.was_unrecoverable) {
                    self.reset_count = 0;
                    out.push(Effect::ResetCount(0));
                    out.push(Effect::RecoveryStatus(RECOVERY_RECOVERABLE));
                    pmon_common::notice!(
                        "{name}: Operator module startup — resetting recovery state");
                }
                self.enter_booting(now);
            }

            DpuState::Booting => {
                if input.planes.all_up() {
                    self.enter_ready(name, "DPU is ready (all states up)", &mut out);
                } else if now.duration_since(self.boot_start) >= t.boot_timeout {
                    // A DPU whose control plane and midplane are up is running;
                    // only its forwarding pipeline has not converged.  Power
                    // cycling it would throw away a working SONiC for a data
                    // plane that may be seconds away.
                    if input.planes.cp_up() && input.planes.mp_up() {
                        if !self.boot_dp_warning_logged {
                            log::warn!(
                                "{name}: data plane not up after {}s", t.boot_timeout.as_secs());
                            self.boot_dp_warning_logged = true;
                        }
                    } else {
                        self.boot_timeout_expired(
                            name, t, "without reaching Ready", input, hw, &mut out);
                    }
                }
            }

            DpuState::Ready => {
                if input.planes.mp_down() || input.planes.cp_down() {
                    log::warn!(
                        "{name}: failure detected (mp={}, cp={})",
                        Planes::show(&input.planes.midplane),
                        Planes::show(&input.planes.control));
                    out.push(Effect::ReadyStatus(false));
                    out.push(Effect::LastDownTime);
                    if input.auto_recovery {
                        self.enter_wait_for_self_recovery(now);
                    } else {
                        self.state = DpuState::ManualIntervention;
                    }
                } else if input.planes.dp_down() {
                    // The data plane alone is not a recovery condition: the DPU
                    // is up and reachable, it just is not forwarding.
                    if !self.dp_was_down {
                        out.push(Effect::ReadyStatus(false));
                        out.push(Effect::LastDownTime);
                        self.dp_was_down = true;
                    }
                } else if self.dp_was_down {
                    out.push(Effect::ReadyStatus(true));
                    out.push(Effect::LastReadyTime);
                    self.dp_was_down = false;
                }
            }

            DpuState::WaitForSelfRecovery => {
                self.self_recovery_polls += 1;
                // A DPU that watchdog-rebooted is back in well under a minute,
                // and power cycling it in that window turns a self-healing
                // event into an outage.
                if now.duration_since(self.self_recovery_start) < MINIMUM_GRACE
                    && self.self_recovery_polls < MINIMUM_GRACE_POLLS
                {
                    return out;
                }
                if input.planes.mp_up() || input.planes.cp_up() {
                    pmon_common::notice!(
                        "{name}: self-recovering (mp={}, cp={}); entering Booting",
                        Planes::show(&input.planes.midplane),
                        Planes::show(&input.planes.control));
                    self.enter_booting(now);
                } else if now.duration_since(self.self_recovery_start) >= t.self_recovery_timeout {
                    log::warn!(
                        "{name}: self-recovery timeout ({}s) expired; both cp and midplane \
                         still down", t.self_recovery_timeout.as_secs());
                    if input.auto_recovery {
                        self.power_cycle_or_give_up(name, t, now, hw, &mut out);
                    } else {
                        self.state = DpuState::ManualIntervention;
                    }
                }
            }

            DpuState::PowerCycle => {
                if input.planes.all_up() {
                    self.enter_ready(name, "DPU recovered after power-cycle", &mut out);
                } else if now.duration_since(self.boot_start) >= t.boot_timeout {
                    self.boot_timeout_expired(name, t, "after power-cycle", input, hw, &mut out);
                }
            }

            DpuState::ManualIntervention => {
                if input.planes.all_up() {
                    self.enter_ready(name, "DPU recovered (manual intervention)", &mut out);
                } else if input.auto_recovery {
                    // Auto-recovery was switched on while this DPU was waiting
                    // for a human.  Now there is something to try.
                    self.power_cycle_or_give_up(name, t, now, hw, &mut out);
                }
            }

            DpuState::Unrecoverable => unreachable!("returned above"),
        }
        out
    }

    /// The start-up reset,
    /// `chassisd:SmartSwitchModuleUpdater.init_dpu_recovery_state`.
    pub fn reset_for_startup(&mut self, now: Instant) -> Vec<Effect> {
        self.reset_count = 0;
        self.was_unrecoverable = false;
        self.dp_was_down = false;
        self.enter_booting(now);
        vec![
            Effect::ReadyStatus(false),
            Effect::RecoveryStatus(RECOVERY_RECOVERABLE),
            Effect::ResetCount(0),
            Effect::LastDownTime,
        ]
    }

    /// Unconditionally power cycle, for the NPU-crash path at start-up.
    pub fn force_power_cycle(
        &mut self,
        name: &str,
        t: &Thresholds,
        now: Instant,
        hw: &mut dyn PowerCycler,
    ) -> Vec<Effect> {
        let mut out = Vec::new();
        self.power_cycle_or_give_up(name, t, now, hw, &mut out);
        out
    }
}

/// True when the last NPU reboot looks like a kernel panic.
///
/// Only a panic: `unknown` is what a first boot reports, and treating it as a
/// crash would power cycle every DPU on every new switch.
pub fn npu_crash_on_last_boot(reboot_cause: Option<&str>) -> bool {
    reboot_cause.is_some_and(|c| c.to_lowercase().contains("kernel panic"))
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Records what was asked of the hardware, and can refuse the lock.
    struct Hw {
        cycles: Vec<String>,
        lock: bool,
    }

    impl Hw {
        fn new() -> Self {
            Self { cycles: Vec::new(), lock: true }
        }
        fn busy() -> Self {
            Self { cycles: Vec::new(), lock: false }
        }
    }

    impl PowerCycler for Hw {
        fn power_cycle(&mut self, module: &str) -> bool {
            self.cycles.push(module.to_string());
            self.lock
        }
    }

    fn planes(mp: &str, cp: &str, dp: &str) -> Planes {
        Planes {
            midplane: Some(mp.to_string()),
            control: Some(cp.to_string()),
            data: Some(dp.to_string()),
        }
    }

    fn all_up() -> Planes {
        planes("up", "up", "up")
    }

    struct Ctx {
        r: Recovery,
        t: Thresholds,
        hw: Hw,
        now: Instant,
        auto: bool,
        admin_up: bool,
        oper: String,
        transition: bool,
    }

    impl Ctx {
        fn new() -> Self {
            let now = Instant::now();
            Self {
                r: Recovery::new(now),
                t: Thresholds::default(),
                hw: Hw::new(),
                now,
                auto: true,
                admin_up: true,
                oper: "Online".to_string(),
                transition: false,
            }
        }

        fn advance(&mut self, d: Duration) -> &mut Self {
            self.now += d;
            self
        }

        fn step(&mut self, p: &Planes) -> Vec<Effect> {
            let input = Inputs {
                planes: p,
                oper_status: &self.oper,
                admin_up: self.admin_up,
                auto_recovery: self.auto,
                transition_in_progress: self.transition,
                now: self.now,
            };
            self.r.step("DPU0", &input, &self.t, &mut self.hw)
        }
    }

    #[test]
    fn a_dpu_that_comes_up_reaches_ready() {
        let mut c = Ctx::new();
        let out = c.step(&all_up());
        assert_eq!(c.r.state, DpuState::Ready);
        assert!(out.contains(&Effect::ReadyStatus(true)));
        assert!(out.contains(&Effect::LastReadyTime));
    }

    /// A planned reboot takes the control plane down.  Recovering from it
    /// would have the daemon fight the operator who asked for it.
    #[test]
    fn a_planned_transition_suppresses_everything() {
        let mut c = Ctx::new();
        c.step(&all_up());
        c.transition = true;
        let out = c.step(&planes("down", "down", "down"));
        assert!(out.is_empty());
        assert_eq!(c.r.state, DpuState::Ready);
        assert!(c.hw.cycles.is_empty());
    }

    /// An operator's shutdown is not a failure.
    #[test]
    fn an_admin_down_dpu_is_not_recovered() {
        let mut c = Ctx::new();
        c.step(&all_up());
        c.admin_up = false;
        let out = c.step(&planes("down", "down", "down"));
        assert_eq!(c.r.state, DpuState::AdminDown);
        assert_eq!(out, vec![Effect::ReadyStatus(false)]);
        assert!(c.hw.cycles.is_empty());
    }

    /// And it is written once, not once per ten seconds for as long as the DPU
    /// stays shut down.
    #[test]
    fn admin_down_is_published_once() {
        let mut c = Ctx::new();
        c.admin_up = false;
        assert_eq!(c.step(&Planes::default()).len(), 1);
        assert!(c.step(&Planes::default()).is_empty());
    }

    /// A DPU that loses its midplane is given a grace period first: a watchdog
    /// reboot is back in well under a minute, and power cycling inside that
    /// window turns a self-healing event into an outage.
    #[test]
    fn a_failure_waits_for_self_recovery_before_anything_is_done() {
        let mut c = Ctx::new();
        c.step(&all_up());
        c.step(&planes("down", "up", "up"));
        assert_eq!(c.r.state, DpuState::WaitForSelfRecovery);
        assert!(c.hw.cycles.is_empty());
    }

    /// And the grace period is at least 30 seconds *and* three polls, so a
    /// fast poll interval cannot shorten it.
    #[test]
    fn the_grace_period_is_a_time_and_a_poll_count() {
        let mut c = Ctx::new();
        c.step(&all_up());
        c.step(&planes("down", "down", "down"));

        // Two polls, one second apart: neither condition met.
        c.advance(Duration::from_secs(1)).step(&planes("down", "down", "down"));
        c.advance(Duration::from_secs(1)).step(&planes("down", "down", "down"));
        assert_eq!(c.r.state, DpuState::WaitForSelfRecovery);
        assert!(c.hw.cycles.is_empty());

        // Past the self-recovery timeout, both conditions met.
        c.advance(Duration::from_secs(400)).step(&planes("down", "down", "down"));
        assert_eq!(c.hw.cycles, vec!["DPU0".to_string()]);
    }

    /// The DPU comes back on its own inside the grace period, which is what it
    /// is for.
    #[test]
    fn a_dpu_that_heals_itself_goes_back_to_booting_and_not_to_a_power_cycle() {
        let mut c = Ctx::new();
        c.step(&all_up());
        c.step(&planes("down", "down", "down"));
        c.advance(Duration::from_secs(40)).step(&planes("up", "up", "down"));
        assert_eq!(c.r.state, DpuState::Booting);
        assert!(c.hw.cycles.is_empty());
        c.step(&all_up());
        assert_eq!(c.r.state, DpuState::Ready);
    }

    /// The data plane alone is not a recovery condition: the DPU is up and
    /// reachable, it just is not forwarding yet.
    #[test]
    fn a_data_plane_down_alone_never_power_cycles() {
        let mut c = Ctx::new();
        c.step(&all_up());
        let out = c.advance(Duration::from_secs(10)).step(&planes("up", "up", "down"));
        assert_eq!(c.r.state, DpuState::Ready);
        assert!(out.contains(&Effect::ReadyStatus(false)));
        assert!(c.hw.cycles.is_empty());
    }

    /// And it is reported once, not every cycle: the row would otherwise be
    /// rewritten every ten seconds for as long as the pipeline is down.
    #[test]
    fn a_steady_data_plane_failure_is_written_once() {
        let mut c = Ctx::new();
        c.step(&all_up());
        assert!(!c.step(&planes("up", "up", "down")).is_empty());
        assert!(c.step(&planes("up", "up", "down")).is_empty());
        // And coming back is the other event.
        let out = c.step(&all_up());
        assert!(out.contains(&Effect::ReadyStatus(true)));
    }

    /// A boot that overruns with the control plane *up* is a data plane that
    /// has not converged.  Power cycling would throw away a working SONiC.
    #[test]
    fn a_boot_timeout_with_the_control_plane_up_only_warns() {
        let mut c = Ctx::new();
        c.advance(Duration::from_secs(700));
        c.step(&planes("up", "up", "down"));
        assert_eq!(c.r.state, DpuState::Booting);
        assert!(c.hw.cycles.is_empty());
    }

    /// With the control plane down it is a genuine boot failure.
    #[test]
    fn a_boot_timeout_with_the_control_plane_down_power_cycles() {
        let mut c = Ctx::new();
        c.advance(Duration::from_secs(700));
        c.step(&planes("down", "down", "down"));
        assert_eq!(c.hw.cycles, vec!["DPU0".to_string()]);
        assert_eq!(c.r.state, DpuState::PowerCycle);
        assert_eq!(c.r.reset_count, 1);
    }

    /// The limit is checked before counting, so `reset_limit = 2` is two power
    /// cycles and not three.
    #[test]
    fn the_reset_limit_is_the_number_of_power_cycles() {
        let mut c = Ctx::new();
        for _ in 0..4 {
            c.advance(Duration::from_secs(700));
            c.step(&planes("down", "down", "down"));
        }
        assert_eq!(c.hw.cycles.len(), 2, "reset_limit = 2");
        assert_eq!(c.r.state, DpuState::Unrecoverable);
    }

    /// Unrecoverable is terminal while the DPU stays admin-up.
    #[test]
    fn an_unrecoverable_dpu_is_left_alone() {
        let mut c = Ctx::new();
        c.r.state = DpuState::Unrecoverable;
        assert!(c.step(&planes("down", "down", "down")).is_empty());
        assert!(c.hw.cycles.is_empty());
    }

    /// And the documented way out of it is a shutdown followed by a startup,
    /// which gives the budget back.  Checking Unrecoverable before admin-down
    /// would make the state permanent.
    #[test]
    fn shutting_an_unrecoverable_dpu_down_and_starting_it_clears_the_condition() {
        let mut c = Ctx::new();
        c.r.state = DpuState::Unrecoverable;
        c.r.reset_count = 2;

        c.admin_up = false;
        c.step(&Planes::default());
        assert_eq!(c.r.state, DpuState::AdminDown);

        c.admin_up = true;
        let out = c.step(&Planes::default());
        assert_eq!(c.r.state, DpuState::Booting);
        assert_eq!(c.r.reset_count, 0);
        assert!(out.contains(&Effect::RecoveryStatus(RECOVERY_RECOVERABLE)));
        assert!(out.contains(&Effect::ResetCount(0)));
    }

    /// A shutdown that is not out of Unrecoverable does not reset the budget:
    /// an operator bouncing a flapping DPU should not get unlimited retries.
    #[test]
    fn an_ordinary_bounce_does_not_reset_the_budget() {
        let mut c = Ctx::new();
        c.advance(Duration::from_secs(700));
        c.step(&planes("down", "down", "down"));
        assert_eq!(c.r.reset_count, 1);

        c.admin_up = false;
        c.step(&Planes::default());
        c.admin_up = true;
        let out = c.step(&Planes::default());
        assert_eq!(c.r.reset_count, 1);
        assert!(!out.iter().any(|e| matches!(e, Effect::ResetCount(_))));
    }

    /// With auto-recovery off nothing is done to the hardware -- but the state
    /// is still tracked and published, which is what an operator reads.
    #[test]
    fn auto_recovery_off_reaches_manual_intervention_without_touching_the_hardware() {
        let mut c = Ctx::new();
        c.auto = false;
        c.step(&all_up());
        let out = c.step(&planes("down", "down", "down"));
        assert_eq!(c.r.state, DpuState::ManualIntervention);
        assert!(out.contains(&Effect::ReadyStatus(false)));
        assert!(c.hw.cycles.is_empty());
    }

    /// Switching it on later picks the DPU up.
    #[test]
    fn enabling_auto_recovery_later_picks_up_a_waiting_dpu() {
        let mut c = Ctx::new();
        c.auto = false;
        c.step(&all_up());
        c.step(&planes("down", "down", "down"));
        assert_eq!(c.r.state, DpuState::ManualIntervention);

        c.auto = true;
        c.step(&planes("down", "down", "down"));
        assert_eq!(c.hw.cycles, vec!["DPU0".to_string()]);
    }

    /// And an operator who fixed it by hand is noticed.
    #[test]
    fn a_manually_recovered_dpu_returns_to_ready() {
        let mut c = Ctx::new();
        c.auto = false;
        c.step(&all_up());
        c.step(&planes("down", "down", "down"));
        c.step(&all_up());
        assert_eq!(c.r.state, DpuState::Ready);
    }

    /// The lock belongs to whatever else is operating on this DPU.  Nothing
    /// was done to the hardware, so the attempt must not be counted -- or a
    /// busy lock would silently spend the recovery budget.
    #[test]
    fn a_power_cycle_that_could_not_take_the_lock_is_not_counted() {
        let mut c = Ctx::new();
        c.hw = Hw::busy();
        c.advance(Duration::from_secs(700));
        c.step(&planes("down", "down", "down"));
        assert_eq!(c.r.reset_count, 0, "the attempt is given back");
        assert_eq!(c.r.state, DpuState::PowerCycle, "but the DPU is still watched");
    }

    /// Only a DPU that was Ready reads Offline as a new fault.  A stale
    /// Offline seen while it is starting up would otherwise power cycle it.
    #[test]
    fn offline_is_a_hardware_failure_only_from_ready() {
        let mut c = Ctx::new();
        c.oper = "Offline".to_string();
        c.step(&planes("down", "down", "down"));
        assert!(c.hw.cycles.is_empty(), "Booting does not act on it");

        let mut c = Ctx::new();
        c.step(&all_up());
        c.oper = "Offline".to_string();
        c.step(&all_up());
        assert_eq!(c.hw.cycles, vec!["DPU0".to_string()]);
    }

    /// A plane nobody has published is not a plane that failed.
    #[test]
    fn an_absent_plane_reading_is_not_a_failure() {
        let mut c = Ctx::new();
        c.step(&all_up());
        let out = c.step(&Planes::default());
        assert_eq!(c.r.state, DpuState::Ready, "no reading is not `down`");
        assert!(out.is_empty());
    }

    /// A boot that overruns with auto-recovery off waits for a human rather
    /// than sitting in Booting forever with nobody told.
    #[test]
    fn a_boot_timeout_with_auto_recovery_off_asks_for_a_human() {
        let mut c = Ctx::new();
        c.auto = false;
        c.advance(Duration::from_secs(700));
        c.step(&planes("down", "down", "down"));
        assert_eq!(c.r.state, DpuState::ManualIntervention);
        assert!(c.hw.cycles.is_empty());
    }

    /// The "data plane still not up" warning is worth saying once per boot,
    /// not once per ten seconds for as long as the pipeline is down.
    #[test]
    fn the_data_plane_warning_is_said_once_per_boot() {
        let mut c = Ctx::new();
        c.advance(Duration::from_secs(700));
        for _ in 0..5 {
            c.step(&planes("up", "up", "down"));
        }
        assert_eq!(c.r.state, DpuState::Booting);
        // And a fresh boot may say it again: the flag is cleared on entry.
        c.step(&all_up());
        assert_eq!(c.r.state, DpuState::Ready);
    }

    /// A power cycle that did not take gets a second one, up to the limit --
    /// which is the boot timeout after the first bringing it back here.
    #[test]
    fn a_power_cycle_that_did_not_work_is_followed_by_another() {
        let mut c = Ctx::new();
        c.advance(Duration::from_secs(700));
        c.step(&planes("down", "down", "down"));
        assert_eq!(c.r.state, DpuState::PowerCycle);
        c.advance(Duration::from_secs(700));
        c.step(&planes("down", "down", "down"));
        assert_eq!(c.hw.cycles.len(), 2);
    }

    /// And one that did puts the DPU back in service.
    #[test]
    fn a_power_cycle_that_worked_reaches_ready() {
        let mut c = Ctx::new();
        c.advance(Duration::from_secs(700));
        c.step(&planes("down", "down", "down"));
        let out = c.step(&all_up());
        assert_eq!(c.r.state, DpuState::Ready);
        assert!(out.contains(&Effect::ReadyStatus(true)));
    }

    /// The self-recovery wait ends in a power cycle when nothing came back,
    /// and the DPU is watched from there rather than left.
    #[test]
    fn a_self_recovery_that_timed_out_power_cycles_and_keeps_watching() {
        let mut c = Ctx::new();
        c.step(&all_up());
        c.step(&planes("down", "down", "down"));
        c.advance(Duration::from_secs(400));
        c.step(&planes("down", "down", "down"));
        assert_eq!(c.r.state, DpuState::PowerCycle);
        assert_eq!(c.hw.cycles.len(), 1);
    }

    /// With auto-recovery off the self-recovery wait is skipped entirely: there
    /// is nothing to wait for, and the operator should be told now.
    #[test]
    fn auto_recovery_off_skips_the_self_recovery_wait() {
        let mut c = Ctx::new();
        c.auto = false;
        c.step(&all_up());
        c.step(&planes("down", "up", "up"));
        assert_eq!(c.r.state, DpuState::ManualIntervention, "not WaitForSelfRecovery");
    }

    /// `unknown` is what a first boot reports.  Treating it as a crash would
    /// power cycle every DPU on every new switch.
    #[test]
    fn only_a_kernel_panic_counts_as_an_npu_crash() {
        assert!(npu_crash_on_last_boot(Some("Kernel Panic")));
        assert!(npu_crash_on_last_boot(Some("kernel panic - not syncing")));
        assert!(!npu_crash_on_last_boot(Some("Unknown")));
        assert!(!npu_crash_on_last_boot(Some("Power Loss")));
        assert!(!npu_crash_on_last_boot(None));
    }
}
