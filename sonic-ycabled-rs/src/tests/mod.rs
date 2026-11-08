//! Tests that need more than one module or an external program (redis-server,
//! the embedded interpreter, a gRPC server).

mod bridge;
mod cli;
mod daemon;
mod grpc;
mod redis;
