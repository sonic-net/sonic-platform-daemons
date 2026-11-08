//! Tests of the SoC client against an in-process tonic server: requests,
//! deadlines, connectivity states and the failure writes, GracefulRestart.

use std::net::{IpAddr, SocketAddr};
use std::pin::Pin;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use tokio::sync::oneshot;
use tokio_stream::wrappers::TcpListenerStream;
use tonic::{Request, Response, Status};

use crate::db::{Db, DbId, MemDb};
use crate::grpc::{self, RpcError, SocConnector, TonicConnector};
use crate::proto::dual_to_r_active_server::{DualToRActive, DualToRActiveServer};
use crate::proto::graceful_restart_server::{GracefulRestart, GracefulRestartServer};
use crate::proto::*;
use crate::schema::{FORWARDING_STATE_RESPONSE, GRPC_PORT};

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
    let listener = tokio::net::TcpListener::bind(addr).await.unwrap();
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
/// fails the RPC and writes response=failure.
#[tokio::test]
async fn refused_connection_writes_failure() {
    let db = MemDb::new();
    let client = TonicConnector
        .connect("Ethernet4", ip("127.0.0.23"), db.clone() as Arc<dyn Db>)
        .await
        .unwrap();
    let r = client.query_admin_forwarding().await;
    assert!(
        matches!(r, Err(RpcError::Status(tonic::Code::Unavailable))),
        "{r:?}"
    );
    assert!(eventually(|| client.connectivity() == "TRANSIENT_FAILURE", 2).await);
    let row = db
        .row(DbId::Appl, FORWARDING_STATE_RESPONSE, "Ethernet4")
        .unwrap();
    assert_eq!(row["response"], "failure");
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
/// response; UPPER_TOR from the upper ToR.
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

/// The lower ToR subscribes as LOWER_TOR.
#[tokio::test]
async fn graceful_restart_lower_tor() {
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
