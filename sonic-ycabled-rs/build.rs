//! Generates the SoC gRPC client from the proto Python ycabled uses, so that
//! both implementations speak the same service definition.

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let proto_dir = "../sonic-ycabled/proto";
    let proto = format!("{proto_dir}/proto_out/linkmgr_grpc_driver.proto");
    println!("cargo:rerun-if-changed={proto}");
    tonic_build::configure()
        .build_server(true)
        .build_client(true)
        .compile_protos(&[proto.as_str()], &[proto_dir])?;
    Ok(())
}
