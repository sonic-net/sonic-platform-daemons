//! Tests of the SoC client against an in-process tonic server: requests,
//! deadlines, connectivity states and the failure writes, GracefulRestart.

use std::net::{IpAddr, SocketAddr};
use std::path::Path;
use std::pin::Pin;
use std::process::Command;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use tokio::net::{TcpListener, TcpStream};
use tokio::sync::{mpsc, oneshot};
use tokio_stream::wrappers::{TcpListenerStream, UnboundedReceiverStream};
use tonic::transport::{Certificate, Identity, Server, ServerTlsConfig};
use tonic::{Request, Response, Status};

use crate::actor::Shared;
use crate::bridge::YCablePlatform;
use crate::daemon;
use crate::db::{Db, DbId, MemDb};
use crate::grpc::{self, RpcError, SocConnector, TonicConnector};
use crate::ports::PortMap;
use crate::proto::dual_to_r_active_server::{DualToRActive, DualToRActiveServer};
use crate::proto::graceful_restart_server::{GracefulRestart, GracefulRestartServer};
use crate::proto::*;
use crate::schema::{FORWARDING_STATE_RESPONSE, GRPC_PORT, LOOPBACK_INTERFACE, MUX_CABLE};
use crate::testing::{MockConnector, MockPlatform};

#[derive(Default)]
struct SocState {
    admin: Vec<bool>,
    delay: Option<Duration>,
    requests: Vec<String>,
    gr_period: Option<i32>,
    gr_requests: Vec<(Instant, i32)>,
}

#[derive(Clone, Default)]
struct TestSoc(Arc<Mutex<SocState>>);

impl TestSoc {
    async fn pause(&self) {
        let d = self.0.lock().unwrap().delay;
        if let Some(d) = d {
            tokio::time::sleep(d).await;
        }
    }

    fn log(&self, what: String) {
        self.0.lock().unwrap().requests.push(what);
    }
}

#[tonic::async_trait]
impl DualToRActive for TestSoc {
    async fn query_admin_forwarding_port_state(
        &self,
        r: Request<AdminRequest>,
    ) -> Result<Response<AdminReply>, Status> {
        self.pause().await;
        self.log(format!(
            "query {:?} {:?}",
            r.get_ref().portid,
            r.get_ref().state
        ));
        let admin = self.0.lock().unwrap().admin.clone();
        Ok(Response::new(AdminReply {
            portid: vec![0, 1],
            state: admin,
        }))
    }

    async fn set_admin_forwarding_port_state(
        &self,
        r: Request<AdminRequest>,
    ) -> Result<Response<AdminReply>, Status> {
        self.pause().await;
        let req = r.into_inner();
        self.log(format!("set {:?} {:?}", req.portid, req.state));
        Ok(Response::new(AdminReply {
            portid: req.portid,
            state: req.state,
        }))
    }

    async fn query_operation_port_state(
        &self,
        r: Request<OperationRequest>,
    ) -> Result<Response<OperationReply>, Status> {
        self.log(format!("oper {:?}", r.get_ref().portid));
        Ok(Response::new(OperationReply {
            portid: vec![0, 1],
            state: vec![false, true],
        }))
    }

    async fn query_link_state(
        &self,
        r: Request<LinkStateRequest>,
    ) -> Result<Response<LinkStateReply>, Status> {
        self.log(format!("link {:?}", r.get_ref().portid));
        Ok(Response::new(LinkStateReply {
            portid: vec![0, 1],
            state: vec![true, false],
        }))
    }

    async fn query_server_version(
        &self,
        r: Request<ServerVersionRequest>,
    ) -> Result<Response<ServerVersionReply>, Status> {
        self.log(format!("version {}", r.get_ref().version));
        Ok(Response::new(ServerVersionReply {
            version: "test-soc".into(),
        }))
    }
}

type GrStream = Pin<Box<dyn futures::Stream<Item = Result<GracefulAdminResponse, Status>> + Send>>;

#[tonic::async_trait]
impl GracefulRestart for TestSoc {
    type NotifyGracefulRestartStartStream = GrStream;

    async fn notify_graceful_restart_start(
        &self,
        r: Request<GracefulAdminRequest>,
    ) -> Result<Response<GrStream>, Status> {
        let period = {
            let mut s = self.0.lock().unwrap();
            s.gr_requests.push((Instant::now(), r.get_ref().tor));
            s.gr_period
        };
        let items: Vec<Result<GracefulAdminResponse, Status>> = period
            .map(|p| {
                vec![Ok(GracefulAdminResponse {
                    msgtype: 1,
                    notifytype: 2,
                    guid: "g".into(),
                    period: p,
                })]
            })
            .unwrap_or_default();
        Ok(Response::new(Box::pin(futures::stream::iter(items))))
    }
}

/// Serves `soc` on `ip`:50075 until the returned sender is dropped or fired.
async fn serve(ip: &str, soc: TestSoc) -> oneshot::Sender<()> {
    let addr: SocketAddr = format!(
        "{}:{GRPC_PORT}",
        if ip.contains(':') {
            format!("[{ip}]")
        } else {
            ip.to_string()
        }
    )
    .parse()
    .unwrap();
    // A SoC served again after a stop: the stopped one may still be
    // closing its listener.
    let mut tries = 0;
    let listener = loop {
        match tokio::net::TcpListener::bind(addr).await {
            Ok(l) => break l,
            Err(e) if e.kind() == std::io::ErrorKind::AddrInUse && tries < 100 => {
                tries += 1;
                tokio::time::sleep(Duration::from_millis(20)).await;
            }
            Err(e) => panic!("{addr}: {e}"),
        }
    };
    let (tx, rx) = oneshot::channel::<()>();
    tokio::spawn(async move {
        let _ = tonic::transport::Server::builder()
            .add_service(DualToRActiveServer::new(soc.clone()))
            .add_service(GracefulRestartServer::new(soc))
            .serve_with_incoming_shutdown(TcpListenerStream::new(listener), async {
                let _ = rx.await;
            })
            .await;
    });
    tx
}

async fn eventually<F: Fn() -> bool>(f: F, secs: u64) -> bool {
    let deadline = Instant::now() + Duration::from_secs(secs);
    while Instant::now() < deadline {
        if f() {
            return true;
        }
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
    f()
}

fn ip(s: &str) -> IpAddr {
    s.parse().unwrap()
}

/// The connectivity states the watcher of `port` logged, in order.
fn states(port: &str) -> Vec<String> {
    let prefix = format!("gRPC port {port} state changed to ");
    crate::logger::capture::find(&prefix)
        .into_iter()
        .filter_map(|(_, m)| {
            m.strip_prefix(&prefix)
                .and_then(|s| s.split(' ').next())
                .map(str::to_string)
        })
        .collect()
}

/// How many times `response=failure` was written for `port`.
fn failures(db: &MemDb, port: &str) -> usize {
    db.writes(FORWARDING_STATE_RESPONSE)
        .iter()
        .filter(|(k, _)| k == port)
        .count()
}

const SOC_NAME: &str = "soc.ut.test";

fn openssl(dir: &Path, args: &[&str]) {
    let out = Command::new("openssl")
        .args(args)
        .current_dir(dir)
        .output()
        .expect("openssl");
    assert!(
        out.status.success(),
        "openssl {args:?}: {}",
        String::from_utf8_lossy(&out.stderr)
    );
}

/// A CA and a certificate for SOC_NAME it signed, made with openssl for the
/// test, as the black-box suite makes its own, so that no key is in the tree.
struct TestPki {
    dir: tempfile::TempDir,
}

impl TestPki {
    fn new() -> TestPki {
        let dir = tempfile::tempdir().unwrap();
        let d = dir.path();
        let write = |name: &str, text: &str| std::fs::write(d.join(name), text).unwrap();
        write(
            "ca.cnf",
            "[req]\ndistinguished_name = dn\nprompt = no\nx509_extensions = ext\n[dn]\nCN = ycabled-rs-ut-ca\n[ext]\nbasicConstraints = critical,CA:TRUE\nkeyUsage = critical,keyCertSign\nsubjectKeyIdentifier = hash\n",
        );
        write(
            "soc.cnf",
            &format!("[req]\ndistinguished_name = dn\nprompt = no\n[dn]\nCN = {SOC_NAME}\n"),
        );
        write(
            "soc.ext",
            &format!("basicConstraints = critical,CA:FALSE\nsubjectAltName = DNS:{SOC_NAME}\nextendedKeyUsage = serverAuth\nauthorityKeyIdentifier = keyid\n"),
        );
        let ec = [
            "-newkey",
            "ec",
            "-pkeyopt",
            "ec_paramgen_curve:prime256v1",
            "-nodes",
        ];
        openssl(
            d,
            &[
                &["req", "-x509", "-config", "ca.cnf", "-keyout", "ca.key"][..],
                &ec,
                &["-out", "ca.crt", "-days", "2"],
            ]
            .concat(),
        );
        openssl(
            d,
            &[
                &["req", "-new", "-config", "soc.cnf", "-keyout", "soc.key"][..],
                &ec,
                &["-out", "soc.csr"],
            ]
            .concat(),
        );
        openssl(
            d,
            &[
                "x509",
                "-req",
                "-in",
                "soc.csr",
                "-CA",
                "ca.crt",
                "-CAkey",
                "ca.key",
                "-set_serial",
                "2",
                "-days",
                "2",
                "-out",
                "soc.crt",
                "-extfile",
                "soc.ext",
            ],
        );
        TestPki { dir }
    }

    fn pem(&self, name: &str) -> Vec<u8> {
        std::fs::read(self.dir.path().join(name)).unwrap()
    }

    /// The SoC's side; with `client_auth`, it requires a client certificate
    /// from this CA.
    fn server(&self, client_auth: bool) -> ServerTlsConfig {
        let tls = ServerTlsConfig::new()
            .identity(Identity::from_pem(self.pem("soc.crt"), self.pem("soc.key")));
        if client_auth {
            tls.client_ca_root(Certificate::from_pem(self.pem("ca.crt")))
        } else {
            tls
        }
    }

    /// GRPCCLIENT for TLS with this CA, auth_level server.
    fn configure(&self, db: &MemDb) {
        let ca = self.dir.path().join("ca.crt");
        db.put(
            DbId::Config,
            "GRPCCLIENT",
            "config",
            &[("type", "secure"), ("auth_level", "server")],
        );
        db.put(
            DbId::Config,
            "GRPCCLIENT",
            "certs",
            &[
                ("ca_crt", ca.to_str().unwrap()),
                ("grpc_ssl_credential", SOC_NAME),
            ],
        );
    }
}

/// A SoC on `ip`:50075 whose listener keeps the connections it accepts
/// while `stall` is set, sending nothing, and hands the others to a TLS SoC
/// with `tls`. Without `tls`, it keeps every connection.
async fn serve_stalling(
    ip: &str,
    tls: Option<ServerTlsConfig>,
    stall: Arc<AtomicBool>,
) -> oneshot::Sender<()> {
    let listener = TcpListener::bind(format!("{ip}:{GRPC_PORT}"))
        .await
        .unwrap();
    let (conns, incoming) = mpsc::unbounded_channel::<std::io::Result<TcpStream>>();
    let (stop, stopped) = oneshot::channel::<()>();
    let serving = tls.is_some();
    tokio::spawn(async move {
        let mut held = Vec::new();
        while let Ok((s, _)) = listener.accept().await {
            if stall.load(Ordering::SeqCst) || !serving {
                held.push(s);
            } else if conns.send(Ok(s)).is_err() {
                break;
            }
        }
    });
    if let Some(tls) = tls {
        tokio::spawn(async move {
            let _ = Server::builder()
                .tls_config(tls)
                .unwrap()
                .add_service(DualToRActiveServer::new(TestSoc::default()))
                .serve_with_incoming_shutdown(UnboundedReceiverStream::new(incoming), async {
                    let _ = stopped.await;
                })
                .await;
        });
    }
    stop
}

/// test_setup_grpc_channel_for_port, test_try_grpc, test_create_channel (insecure)
#[tokio::test]
async fn rpcs_reach_the_soc() {
    let soc = TestSoc::default();
    soc.0.lock().unwrap().admin = vec![true, false];
    let _stop = serve("127.0.0.21", soc.clone()).await;
    let db = MemDb::new();
    let client = TonicConnector
        .connect("Ethernet0", ip("127.0.0.21"), db.clone() as Arc<dyn Db>)
        .await
        .unwrap();
    assert_eq!(client.connectivity(), "unknown");
    assert_eq!(
        client.query_admin_forwarding().await.unwrap().state,
        vec![true, false]
    );
    assert_eq!(
        client.set_admin_forwarding(1, true).await.unwrap().portid,
        vec![1]
    );
    assert_eq!(
        client.query_link_state().await.unwrap().state,
        vec![true, false]
    );
    assert_eq!(
        client.query_operation_state().await.unwrap().state,
        vec![false, true]
    );
    assert_eq!(
        client.query_server_version().await.unwrap().version,
        "test-soc"
    );
    assert_eq!(
        soc.0.lock().unwrap().requests,
        vec![
            "query [0, 1] [false, false]",
            "set [1] [true]",
            "link [0, 1]",
            "oper [0, 1]",
            "version 1.0"
        ]
    );
    assert!(eventually(|| client.connectivity() == "READY", 2).await);
    // A ready channel writes no failure.
    assert!(db
        .row(DbId::Appl, FORWARDING_STATE_RESPONSE, "Ethernet0")
        .is_none());
}

/// test_try_grpc (DEADLINE_EXCEEDED): every RPC ends at the 0.5 s deadline.
#[tokio::test]
async fn rpcs_end_at_the_deadline() {
    let soc = TestSoc::default();
    soc.0.lock().unwrap().delay = Some(Duration::from_secs(3));
    let _stop = serve("127.0.0.22", soc).await;
    let client = TonicConnector
        .connect("Ethernet0", ip("127.0.0.22"), MemDb::new() as Arc<dyn Db>)
        .await
        .unwrap();
    let t0 = Instant::now();
    let r = client.set_admin_forwarding(0, false).await;
    let took = t0.elapsed();
    assert_eq!(r, Err(RpcError::Timeout));
    assert!(
        took >= Duration::from_millis(400) && took < Duration::from_millis(1500),
        "{took:?}"
    );
}

/// test_wait_for_state_change (TRANSIENT_FAILURE): a refused connection
/// fails the RPC and writes response=failure. Trying again keeps
/// TRANSIENT_FAILURE, as grpc-core does, and writes no more failures.
#[tokio::test]
async fn refused_connection_writes_failure() {
    let db = MemDb::new();
    let client = TonicConnector
        .connect("Ethernet72", ip("127.0.0.23"), db.clone() as Arc<dyn Db>)
        .await
        .unwrap();
    let r = client.query_admin_forwarding().await;
    assert!(
        matches!(r, Err(RpcError::Status(tonic::Code::Unavailable))),
        "{r:?}"
    );
    assert!(eventually(|| client.connectivity() == "TRANSIENT_FAILURE", 2).await);
    let row = db
        .row(DbId::Appl, FORWARDING_STATE_RESPONSE, "Ethernet72")
        .unwrap();
    assert_eq!(row["response"], "failure");
    assert!(client.query_admin_forwarding().await.is_err());
    tokio::time::sleep(Duration::from_millis(200)).await;
    assert_eq!(states("Ethernet72"), ["CONNECTING", "TRANSIENT_FAILURE"]);
    assert_eq!(failures(&db, "Ethernet72"), 1);
}

/// A SoC that accepts the TCP connection but does not go on with the TLS
/// handshake: the attempt fails within CONNECT_TIMEOUT, TRANSIENT_FAILURE
/// and response=failure, and the channel is never READY meanwhile. The next
/// connection, whose handshake the SoC completes, is READY and serves the
/// RPCs.
#[tokio::test]
async fn stalled_tls_handshake_fails_then_recovers() {
    let pki = TestPki::new();
    let stall = Arc::new(AtomicBool::new(true));
    let _stop = serve_stalling("127.0.0.29", Some(pki.server(false)), Arc::clone(&stall)).await;
    let db = MemDb::new();
    pki.configure(&db);
    let client = TonicConnector
        .connect("Ethernet76", ip("127.0.0.29"), db.clone() as Arc<dyn Db>)
        .await
        .unwrap();
    let t0 = Instant::now();
    assert_eq!(
        client.query_admin_forwarding().await,
        Err(RpcError::Timeout)
    );
    assert!(
        eventually(|| client.connectivity() == "TRANSIENT_FAILURE", 3).await,
        "{}",
        client.connectivity()
    );
    assert!(
        t0.elapsed() < grpc::CONNECT_TIMEOUT + Duration::from_secs(1),
        "{:?}",
        t0.elapsed()
    );
    assert_eq!(failures(&db, "Ethernet76"), 1);
    assert_eq!(states("Ethernet76"), ["CONNECTING", "TRANSIENT_FAILURE"]);

    stall.store(false, Ordering::SeqCst);
    let mut ok = false;
    for _ in 0..30 {
        if client.query_admin_forwarding().await.is_ok() {
            ok = true;
            break;
        }
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    assert!(ok);
    assert!(eventually(|| client.connectivity() == "READY", 2).await);
    assert_eq!(
        states("Ethernet76"),
        ["CONNECTING", "TRANSIENT_FAILURE", "READY"]
    );
}

/// A TLS handshake the SoC fails, here because it requires a client
/// certificate that auth_level server does not present, is a failed
/// attempt, TRANSIENT_FAILURE as grpc-core reports it, not a lost
/// connection (IDLE).
#[tokio::test]
async fn failed_tls_handshake_is_transient_failure() {
    let pki = TestPki::new();
    let _stop = serve_stalling(
        "127.0.0.30",
        Some(pki.server(true)),
        Arc::new(AtomicBool::new(false)),
    )
    .await;
    let db = MemDb::new();
    pki.configure(&db);
    let client = TonicConnector
        .connect("Ethernet80", ip("127.0.0.30"), db.clone() as Arc<dyn Db>)
        .await
        .unwrap();
    for _ in 0..3 {
        assert!(client.query_admin_forwarding().await.is_err());
    }
    assert!(
        eventually(|| client.connectivity() == "TRANSIENT_FAILURE", 3).await,
        "{}",
        client.connectivity()
    );
    assert_eq!(states("Ethernet80"), ["CONNECTING", "TRANSIENT_FAILURE"]);
    assert_eq!(failures(&db, "Ethernet80"), 1);
}

/// A SoC that accepts TCP connections and sends nothing has not completed
/// the HTTP/2 handshake: TRANSIENT_FAILURE within CONNECT_TIMEOUT, as
/// grpc-core, which waits for the SETTINGS frame, reports it, never READY.
#[tokio::test]
async fn connection_without_http2_settings_fails() {
    let _stop = serve_stalling("127.0.0.31", None, Arc::new(AtomicBool::new(true))).await;
    let db = MemDb::new();
    let client = TonicConnector
        .connect("Ethernet84", ip("127.0.0.31"), db.clone() as Arc<dyn Db>)
        .await
        .unwrap();
    assert_eq!(
        client.query_admin_forwarding().await,
        Err(RpcError::Timeout)
    );
    assert!(
        eventually(|| client.connectivity() == "TRANSIENT_FAILURE", 3).await,
        "{}",
        client.connectivity()
    );
    assert_eq!(states("Ethernet84"), ["CONNECTING", "TRANSIENT_FAILURE"]);
    assert_eq!(failures(&db, "Ethernet84"), 1);
}

/// test_wait_for_state_change (IDLE): losing the connection writes
/// response=failure without any request; the next RPC reconnects.
#[tokio::test]
async fn lost_connection_writes_failure_and_reconnects() {
    let soc = TestSoc::default();
    soc.0.lock().unwrap().admin = vec![true, false];
    let stop = serve("127.0.0.24", soc.clone()).await;
    let db = MemDb::new();
    let client = TonicConnector
        .connect("Ethernet8", ip("127.0.0.24"), db.clone() as Arc<dyn Db>)
        .await
        .unwrap();
    client.query_admin_forwarding().await.unwrap();
    assert!(eventually(|| client.connectivity() == "READY", 2).await);
    db.clear_ops();
    let _ = stop.send(());
    assert!(
        eventually(|| client.connectivity() == "IDLE", 5).await,
        "{}",
        client.connectivity()
    );
    assert!(
        eventually(
            || db
                .row(DbId::Appl, FORWARDING_STATE_RESPONSE, "Ethernet8")
                .is_some(),
            2
        )
        .await
    );
    assert_eq!(
        db.row(DbId::Appl, FORWARDING_STATE_RESPONSE, "Ethernet8")
            .unwrap()["response"],
        "failure"
    );
    // The SoC comes back: requests reconnect.
    let _stop2 = serve("127.0.0.24", soc).await;
    let mut ok = false;
    for _ in 0..50 {
        if client.query_admin_forwarding().await.is_ok() {
            ok = true;
            break;
        }
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    assert!(ok);
    assert!(eventually(|| client.connectivity() == "READY", 2).await);
}

/// test_grpc_ipv6.py: a SoC on an IPv6 address.
#[tokio::test]
async fn ipv6_soc() {
    let soc = TestSoc::default();
    soc.0.lock().unwrap().admin = vec![false, true];
    let _stop = serve("::1", soc).await;
    let client = TonicConnector
        .connect("Ethernet0", ip("::1"), MemDb::new() as Arc<dyn Db>)
        .await
        .unwrap();
    assert_eq!(
        client.query_admin_forwarding().await.unwrap().state,
        vec![false, true]
    );
}

/// test_ycable_graceful_client: subscribe again after the period of the last
/// response; UPPER_TOR from read side 0, the lower ToR, as Python sends it.
#[tokio::test]
async fn graceful_restart_subscribes_again_after_period() {
    let soc = TestSoc::default();
    soc.0.lock().unwrap().gr_period = Some(1);
    let _stop = serve("127.0.0.25", soc.clone()).await;
    let task = tokio::spawn(grpc::graceful_restart_loop(
        "Ethernet0".into(),
        ip("127.0.0.25"),
        0,
        MemDb::new() as Arc<dyn Db>,
    ));
    assert!(eventually(|| soc.0.lock().unwrap().gr_requests.len() >= 2, 5).await);
    task.abort();
    let reqs = soc.0.lock().unwrap().gr_requests.clone();
    assert_eq!(
        (reqs[0].1, reqs[1].1),
        (ToRSide::UpperTor as i32, ToRSide::UpperTor as i32)
    );
    let gap = reqs[1].0 - reqs[0].0;
    assert!(
        gap >= Duration::from_millis(800) && gap < Duration::from_secs(3),
        "{gap:?}"
    );
}

/// The subscriptions start once the ports are initialized, with the read
/// side known then: Loopback3 is looked up again if it was unknown at
/// startup, as Python does. Here it makes this ToR the lower one (read side
/// 0), which sends UPPER_TOR.
#[tokio::test]
async fn graceful_restart_uses_the_current_read_side() {
    let soc = TestSoc::default();
    let _stop = serve("127.0.0.28", soc.clone()).await;
    let db = MemDb::new();
    db.put(
        DbId::Config,
        MUX_CABLE,
        "Ethernet0",
        &[
            ("state", "auto"),
            ("cable_type", "active-active"),
            ("soc_ipv4", "127.0.0.28/32"),
        ],
    );
    let shared = Shared::new(
        db.clone() as Arc<dyn Db>,
        MockPlatform::new() as Arc<dyn YCablePlatform>,
        MockConnector::new(None) as Arc<dyn SocConnector>,
        false,
        -1,
        Arc::new(AtomicBool::new(false)),
    );
    db.put(
        DbId::Config,
        LOOPBACK_INTERFACE,
        "Loopback3|10.1.0.39/32",
        &[("NULL", "NULL")],
    );
    let mut ports = PortMap::default();
    ports.insert("Ethernet0", 1, 0);
    daemon::start_graceful_restart(&shared, 0, &ports).await;
    assert!(eventually(|| !soc.0.lock().unwrap().gr_requests.is_empty(), 5).await);
    assert_eq!(
        soc.0.lock().unwrap().gr_requests[0].1,
        ToRSide::UpperTor as i32
    );
    assert_eq!(shared.read_side.load(Ordering::SeqCst), 0);
}

/// The upper ToR (read side 1) subscribes as LOWER_TOR, as Python does.
#[tokio::test]
async fn graceful_restart_upper_tor() {
    let soc = TestSoc::default();
    let _stop = serve("127.0.0.26", soc.clone()).await;
    let task = tokio::spawn(grpc::graceful_restart_loop(
        "Ethernet0".into(),
        ip("127.0.0.26"),
        1,
        MemDb::new() as Arc<dyn Db>,
    ));
    assert!(eventually(|| !soc.0.lock().unwrap().gr_requests.is_empty(), 5).await);
    task.abort();
    assert_eq!(
        soc.0.lock().unwrap().gr_requests[0].1,
        ToRSide::LowerTor as i32
    );
}

/// test_setup_grpc_channel_for_port (secure without certs): no channel.
#[tokio::test]
async fn secure_config_without_certs_gives_no_channel() {
    let db = MemDb::new();
    db.put(
        DbId::Config,
        "GRPCCLIENT",
        "config",
        &[("type", "secure"), ("auth_level", "server")],
    );
    assert!(TonicConnector
        .connect("Ethernet0", ip("127.0.0.27"), db as Arc<dyn Db>)
        .await
        .is_none());
}
