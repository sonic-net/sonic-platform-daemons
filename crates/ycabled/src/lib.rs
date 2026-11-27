//! ycabled-rs: the Y-cable daemon of SONiC dual ToR, in Rust.
//!
//! See doc/dualtor/ycabled_to_rust_hld.md in sonic-net/SONiC.

pub mod logger;

pub mod actor;
pub mod bridge;
pub mod cli;
pub mod config;
pub mod daemon;
pub mod db;
pub mod grpc;
pub mod ports;
pub mod pyval;
pub mod schema;
#[cfg(test)]
pub mod testing;
#[cfg(test)]
mod tests;

pub mod proto {
    #![allow(clippy::all, missing_docs)]
    include!(concat!(env!("OUT_DIR"), "/_.rs"));
}
