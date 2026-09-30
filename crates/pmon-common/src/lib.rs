//
// SPDX-FileCopyrightText: NVIDIA CORPORATION & AFFILIATES
// Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// Apache-2.0
//

//! What every pmon daemon needs and none of them should own a copy of.
//!
//! Seven daemons are being ported.  Each one opens STATE_DB tables, logs to
//! syslog under its own identifier, and shuts down on SIGTERM; written once per
//! daemon that is six copies of the same hundred lines, and six places for the
//! next syslog-identifier bug to hide.
//!
//! thermalctld still carries its own `db` and `logging` modules -- it was
//! ported before this crate existed and its change is in review.  Moving it
//! over is a follow-up, not a reason to hold the others back.

pub mod cadence;
pub mod cycles;
pub mod db;
pub mod fmt;
pub mod logging;
pub mod platform_env;
pub mod report_once;
