//! The gRPC client of the NIC SoCs of active-active ports (HLD 4.3.4).
//!
//! One lazily connected tonic channel per port, with HTTP/2 keepalive every
//! 4 s (8 s timeout, also while idle) and a 0.5 s deadline on every RPC, as
//! Python ycabled sets up grpcio. tonic does not report gRPC connectivity
//! states, which linkmgrd depends on (`response=failure`, C2): the connector
//! makes the TCP connection and TLS itself and reports connection attempts,
//! connections that are up, failed attempts and closed connections, and
//! [`watch_connectivity`] derives the states from them. As in grpc-core, a
//! connection is up only once TLS, when configured, is done and the SoC's
//! first HTTP/2 frame has arrived, within [`CONNECT_TIMEOUT`].

use std::future::Future;
use std::io;
use std::net::IpAddr;
use std::pin::Pin;
use std::sync::{Arc, Mutex};
use std::task::{Context, Poll};
use std::time::Duration;

use async_trait::async_trait;
use hyper_util::rt::TokioIo;
use tokio::io::{AsyncRead, AsyncWrite, ReadBuf};
use tokio::net::TcpStream;
use tokio::sync::mpsc;
use tokio_rustls::client::TlsStream;
use tokio_rustls::rustls::{self, pki_types::ServerName, ClientConfig, RootCertStore};
use tokio_rustls::TlsConnector;
use tonic::transport::{Channel, Endpoint, Uri};

use crate::config::{self, ChannelConfig, TlsSettings};
use crate::db::{Db, DbId};
use crate::proto::dual_to_r_active_client::DualToRActiveClient;
use crate::proto::graceful_restart_client::GracefulRestartClient;
use crate::proto::{
    AdminReply, AdminRequest, GracefulAdminRequest, LinkStateReply, LinkStateRequest,
    OperationReply, OperationRequest, ServerVersionReply, ServerVersionRequest, ToRSide,
};
use crate::schema::{FORWARDING_STATE_RESPONSE, GRPC_PORT};
use crate::{log_debug, log_notice, log_warning};

/// QUERY_ADMIN_FORWARDING_TIMEOUT and SET_ADMIN_FORWARDING_TIMEOUT.
pub const RPC_TIMEOUT: Duration = Duration::from_millis(500);
pub const DEFAULT_PORT_IDS: [i32; 2] = [0, 1];

#[derive(Clone, Debug, PartialEq)]
pub enum RpcError {
    Status(tonic::Code),
    Timeout,
}

pub type RpcResult<T> = Result<T, RpcError>;

/// grpcio's name of a status code, as in Python ycabled's log messages.
pub fn code_name(code: tonic::Code) -> &'static str {
    use tonic::Code::*;
    match code {
        Ok => "StatusCode.OK",
        Cancelled => "StatusCode.CANCELLED",
        Unknown => "StatusCode.UNKNOWN",
        InvalidArgument => "StatusCode.INVALID_ARGUMENT",
        DeadlineExceeded => "StatusCode.DEADLINE_EXCEEDED",
        NotFound => "StatusCode.NOT_FOUND",
        AlreadyExists => "StatusCode.ALREADY_EXISTS",
        PermissionDenied => "StatusCode.PERMISSION_DENIED",
        ResourceExhausted => "StatusCode.RESOURCE_EXHAUSTED",
        FailedPrecondition => "StatusCode.FAILED_PRECONDITION",
        Aborted => "StatusCode.ABORTED",
        OutOfRange => "StatusCode.OUT_OF_RANGE",
        Unimplemented => "StatusCode.UNIMPLEMENTED",
        Internal => "StatusCode.INTERNAL",
        Unavailable => "StatusCode.UNAVAILABLE",
        DataLoss => "StatusCode.DATA_LOSS",
        Unauthenticated => "StatusCode.UNAUTHENTICATED",
    }
}

/// Logs a failed RPC the way Python's try_grpc does.
pub fn log_rpc_error(err: &RpcError) {
    match err {
        RpcError::Timeout => log_notice!(
            "rpc timeout exceeded for port= {} timeout = {}",
            code_name(tonic::Code::DeadlineExceeded),
            RPC_TIMEOUT.as_secs_f64()
        ),
        RpcError::Status(code) => {
            let name = code_name(*code);
            match code {
                tonic::Code::Cancelled => log_notice!("rpc cancelled for port= {}", name),
                tonic::Code::Unavailable => log_notice!("rpc unavailable for port= {}", name),
                tonic::Code::InvalidArgument => {
                    log_notice!("rpc invalid arguement for port= {}", name)
                }
                tonic::Code::DeadlineExceeded => log_notice!(
                    "rpc timeout exceeded for port= {} timeout = {}",
                    name,
                    RPC_TIMEOUT.as_secs_f64()
                ),
                _ => log_notice!("rpc exception error for port= {}", name),
            }
        }
    }
}

/// The DualToRActive service of one port's SoC.
#[async_trait]
pub trait SocApi: Send + Sync {
    async fn query_admin_forwarding(&self) -> RpcResult<AdminReply>;
    async fn set_admin_forwarding(&self, portid: i32, state: bool) -> RpcResult<AdminReply>;
    async fn query_link_state(&self) -> RpcResult<LinkStateReply>;
    async fn query_operation_state(&self) -> RpcResult<OperationReply>;
    async fn query_server_version(&self) -> RpcResult<ServerVersionReply>;
    /// The last connectivity state (grpc_connection_status), "unknown" before any.
    fn connectivity(&self) -> String;
}

/// Sets up the SoC channel of a port.
#[async_trait]
pub trait SocConnector: Send + Sync {
    /// A channel to `ip`, configured from CONFIG_DB GRPCCLIENT, or None if
    /// the configuration does not allow one.
    async fn connect(&self, port: &str, ip: IpAddr, db: Arc<dyn Db>) -> Option<Arc<dyn SocApi>>;
}

// --- Connection monitoring ---------------------------------------------------

/// How long a connection to a SoC may take to come up: TCP, TLS when
/// configured, and the SoC's first HTTP/2 frame, its SETTINGS. An attempt
/// that has not come up by then has failed, as grpc-core fails an attempt
/// without the SETTINGS frame. RPCs end at their own deadline meanwhile.
#[cfg(not(test))]
pub const CONNECT_TIMEOUT: Duration = Duration::from_secs(5);
#[cfg(test)]
pub const CONNECT_TIMEOUT: Duration = Duration::from_secs(1);

/// HTTP/2, by ALPN.
const ALPN_H2: &[u8] = b"h2";

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ConnEvent {
    /// A connection attempt started.
    Connecting,
    /// The connection is up: TLS, when configured, is done and the SoC's
    /// first HTTP/2 frame has arrived.
    Connected,
    /// The attempt failed before the connection came up, or did not come up
    /// within [`CONNECT_TIMEOUT`].
    ConnectFailed,
    /// A connection that was up is gone.
    Closed,
}

/// gRPC connectivity state from a connection event.
pub fn state_for(event: ConnEvent) -> &'static str {
    match event {
        ConnEvent::Connecting => "CONNECTING",
        ConnEvent::Connected => "READY",
        ConnEvent::ConnectFailed => "TRANSIENT_FAILURE",
        ConnEvent::Closed => "IDLE",
    }
}

/// The state `event` leads to from `current`, None if the state stays. As in
/// grpc-core's pick_first, a channel in TRANSIENT_FAILURE stays there while
/// it tries again, until a connection is up.
pub fn next_state(current: &str, event: ConnEvent) -> Option<&'static str> {
    let new = state_for(event);
    if new == current || (current == "TRANSIENT_FAILURE" && event == ConnEvent::Connecting) {
        return None;
    }
    Some(new)
}

/// Follows a port's connection events: records the state, logs each change
/// and writes `response=failure` to FORWARDING_STATE_RESPONSE when the
/// channel fails (TRANSIENT_FAILURE) or loses its connection (IDLE), as
/// Python's wait_for_state_change does. A channel that keeps failing stays
/// in TRANSIENT_FAILURE, so its retries write no more failures.
pub async fn watch_connectivity(
    port: String,
    mut events: mpsc::UnboundedReceiver<ConnEvent>,
    state: Arc<Mutex<String>>,
    db: Arc<dyn Db>,
) {
    while let Some(ev) = events.recv().await {
        let new = {
            let mut s = state.lock().unwrap();
            let Some(new) = next_state(&s, ev) else {
                continue;
            };
            *s = new.to_string();
            new
        };
        log_notice!(
            "gRPC port {} state changed to {} tid {}",
            port,
            new,
            "ycabled-rs"
        );
        if new == "TRANSIENT_FAILURE" || new == "IDLE" {
            db.set(
                DbId::Appl,
                FORWARDING_STATE_RESPONSE,
                &port,
                &[("response", "failure".to_string())],
            )
            .await;
        }
    }
}

/// A connection to a SoC: TCP, or TLS over it.
enum SocIo {
    Plain(TcpStream),
    Tls(Box<TlsStream<TcpStream>>),
}

impl AsyncRead for SocIo {
    fn poll_read(
        self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buf: &mut ReadBuf<'_>,
    ) -> Poll<io::Result<()>> {
        match self.get_mut() {
            SocIo::Plain(s) => Pin::new(s).poll_read(cx, buf),
            SocIo::Tls(s) => Pin::new(s.as_mut()).poll_read(cx, buf),
        }
    }
}

impl AsyncWrite for SocIo {
    fn poll_write(
        self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buf: &[u8],
    ) -> Poll<io::Result<usize>> {
        match self.get_mut() {
            SocIo::Plain(s) => Pin::new(s).poll_write(cx, buf),
            SocIo::Tls(s) => Pin::new(s.as_mut()).poll_write(cx, buf),
        }
    }

    fn poll_flush(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        match self.get_mut() {
            SocIo::Plain(s) => Pin::new(s).poll_flush(cx),
            SocIo::Tls(s) => Pin::new(s.as_mut()).poll_flush(cx),
        }
    }

    fn poll_shutdown(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        match self.get_mut() {
            SocIo::Plain(s) => Pin::new(s).poll_shutdown(cx),
            SocIo::Tls(s) => Pin::new(s.as_mut()).poll_shutdown(cx),
        }
    }
}

/// A connection, as tonic's HTTP/2 client uses it, that reports to the
/// port's connectivity watcher: up when the SoC's first HTTP/2 frame
/// arrives, failed if the connection ends or [`CONNECT_TIMEOUT`] passes
/// before that, closed if it ends after.
struct MonitoredIo {
    inner: SocIo,
    events: Option<mpsc::UnboundedSender<ConnEvent>>,
    /// Until the connection is up: when the attempt fails.
    deadline: Option<Pin<Box<tokio::time::Sleep>>>,
}

impl MonitoredIo {
    fn ended(&mut self) {
        let event = if self.deadline.take().is_some() {
            ConnEvent::ConnectFailed
        } else {
            ConnEvent::Closed
        };
        if let Some(tx) = self.events.take() {
            let _ = tx.send(event);
        }
    }
}

impl Drop for MonitoredIo {
    fn drop(&mut self) {
        self.ended();
    }
}

impl AsyncRead for MonitoredIo {
    fn poll_read(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buf: &mut ReadBuf<'_>,
    ) -> Poll<io::Result<()>> {
        let before = buf.filled().len();
        let res = Pin::new(&mut self.inner).poll_read(cx, buf);
        match &res {
            Poll::Ready(Ok(())) if buf.filled().len() > before => {
                if self.deadline.take().is_some() {
                    if let Some(tx) = &self.events {
                        let _ = tx.send(ConnEvent::Connected);
                    }
                }
            }
            Poll::Ready(Ok(())) if buf.remaining() > 0 => self.ended(),
            Poll::Ready(Err(_)) => self.ended(),
            Poll::Pending => {
                let timed_out = self
                    .deadline
                    .as_mut()
                    .is_some_and(|d| d.as_mut().poll(cx).is_ready());
                if timed_out {
                    self.ended();
                    return Poll::Ready(Err(io::Error::new(
                        io::ErrorKind::TimedOut,
                        "the SoC sent no HTTP/2 frame",
                    )));
                }
            }
            _ => {}
        }
        res
    }
}

impl AsyncWrite for MonitoredIo {
    fn poll_write(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buf: &[u8],
    ) -> Poll<io::Result<usize>> {
        let res = Pin::new(&mut self.inner).poll_write(cx, buf);
        if let Poll::Ready(Err(_)) = &res {
            self.ended();
        }
        res
    }

    fn poll_flush(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        Pin::new(&mut self.inner).poll_flush(cx)
    }

    fn poll_shutdown(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        Pin::new(&mut self.inner).poll_shutdown(cx)
    }
}

/// The client side of TLS from the GRPCCLIENT settings.
#[derive(Clone)]
struct SocTls {
    config: Arc<ClientConfig>,
    name: ServerName<'static>,
}

/// TLS as tonic's ClientTlsConfig sets it up from the same settings: the CA
/// as the only root, the client certificate for mutual authentication,
/// HTTP/2 by ALPN, and the name the SoC's certificate is checked against.
fn soc_tls(t: &TlsSettings) -> Result<SocTls, String> {
    let mut roots = RootCertStore::empty();
    for cert in rustls_pemfile::certs(&mut t.ca_pem.as_slice()) {
        let cert = cert.map_err(|e| format!("ca_crt: {e}"))?;
        roots.add(cert).map_err(|e| format!("ca_crt: {e}"))?;
    }
    let builder =
        ClientConfig::builder_with_provider(Arc::new(rustls::crypto::ring::default_provider()))
            .with_safe_default_protocol_versions()
            .map_err(|e| e.to_string())?
            .with_root_certificates(roots);
    let mut config = match &t.identity {
        None => builder.with_no_client_auth(),
        Some((cert, key)) => {
            let chain = rustls_pemfile::certs(&mut cert.as_slice())
                .collect::<Result<Vec<_>, _>>()
                .map_err(|e| format!("client_crt: {e}"))?;
            let key = rustls_pemfile::private_key(&mut key.as_slice())
                .map_err(|e| format!("client_key: {e}"))?
                .ok_or_else(|| "client_key: no private key".to_string())?;
            builder
                .with_client_auth_cert(chain, key)
                .map_err(|e| e.to_string())?
        }
    };
    config.alpn_protocols.push(ALPN_H2.to_vec());
    let name = ServerName::try_from(t.target_name.clone())
        .map_err(|e| format!("grpc_ssl_credential {}: {e}", t.target_name))?;
    Ok(SocTls {
        config: Arc::new(config),
        name,
    })
}

/// Connects to a SoC, with TLS when configured, and reports to the port's
/// connectivity watcher. It does TLS itself, rather than tonic, so that one
/// deadline bounds TCP, TLS and the SoC's first HTTP/2 frame, and the
/// connection is up only once all three are through.
#[derive(Clone)]
struct MonitoredConnector {
    events: Option<mpsc::UnboundedSender<ConnEvent>>,
    tls: Option<SocTls>,
}

type BoxError = Box<dyn std::error::Error + Send + Sync>;

impl tower::Service<Uri> for MonitoredConnector {
    type Response = TokioIo<MonitoredIo>;
    type Error = BoxError;
    type Future = Pin<Box<dyn Future<Output = Result<Self::Response, Self::Error>> + Send>>;

    fn poll_ready(&mut self, _cx: &mut Context<'_>) -> Poll<Result<(), Self::Error>> {
        Poll::Ready(Ok(()))
    }

    fn call(&mut self, uri: Uri) -> Self::Future {
        let events = self.events.clone();
        let tls = self.tls.clone();
        // A task of its own, so that the attempt goes on, and fails at its
        // deadline, while no RPC waits for it: tonic drives an attempt only
        // while a request waits to be sent on it.
        let attempt = tokio::spawn(async move {
            let send = |e: ConnEvent| {
                if let Some(tx) = &events {
                    let _ = tx.send(e);
                }
            };
            send(ConnEvent::Connecting);
            let deadline = tokio::time::Instant::now() + CONNECT_TIMEOUT;
            let host = uri
                .host()
                .unwrap_or("")
                .trim_start_matches('[')
                .trim_end_matches(']')
                .to_string();
            let port = uri.port_u16().unwrap_or(GRPC_PORT);
            let connect = async move {
                let tcp = TcpStream::connect((host.as_str(), port)).await?;
                let _ = tcp.set_nodelay(true);
                let Some(tls) = tls else {
                    return Ok(SocIo::Plain(tcp));
                };
                let stream = TlsConnector::from(tls.config)
                    .connect(tls.name, tcp)
                    .await?;
                // As tonic requires.
                if stream.get_ref().1.alpn_protocol() != Some(ALPN_H2) {
                    return Err(io::Error::new(
                        io::ErrorKind::InvalidData,
                        "HTTP/2 was not negotiated",
                    ));
                }
                Ok(SocIo::Tls(Box::new(stream)))
            };
            match tokio::time::timeout_at(deadline, connect).await {
                Ok(Ok(io)) => Ok(TokioIo::new(MonitoredIo {
                    inner: io,
                    events: events.clone(),
                    deadline: Some(Box::pin(tokio::time::sleep_until(deadline))),
                })),
                Ok(Err(e)) => {
                    log_debug!("Could not connect to the SoC at {}: {}", uri, e);
                    send(ConnEvent::ConnectFailed);
                    Err(Box::new(e) as BoxError)
                }
                Err(_) => {
                    log_debug!(
                        "Could not connect to the SoC at {} within {} s",
                        uri,
                        CONNECT_TIMEOUT.as_secs_f64()
                    );
                    send(ConnEvent::ConnectFailed);
                    Err("connection timed out".into())
                }
            }
        });
        Box::pin(async move { attempt.await.map_err(|e| Box::new(e) as BoxError)? })
    }
}

/// `<ip>:50075`, `[<ipv6>]:50075` with the scheme.
pub fn target_uri(ip: IpAddr, tls: bool) -> String {
    let scheme = if tls { "https" } else { "http" };
    match ip {
        IpAddr::V4(a) => format!("{scheme}://{a}:{GRPC_PORT}"),
        IpAddr::V6(a) => format!("{scheme}://[{a}]:{GRPC_PORT}"),
    }
}

/// A lazily connected channel to `ip` with the settings of `config`. None if
/// the TLS material is not usable.
pub fn make_channel(
    ip: IpAddr,
    config: &ChannelConfig,
    events: Option<mpsc::UnboundedSender<ConnEvent>>,
) -> Option<Channel> {
    // TLS is the connector's, so tonic connects as to a plaintext endpoint;
    // the origin keeps the scheme and authority of the requests.
    let mut endpoint = Endpoint::from_shared(target_uri(ip, false))
        .ok()?
        .http2_keep_alive_interval(Duration::from_secs(4))
        .keep_alive_timeout(Duration::from_secs(8))
        .keep_alive_while_idle(true);
    let tls = match config {
        ChannelConfig::Insecure => None,
        ChannelConfig::Secure(t) => match soc_tls(t) {
            Ok(tls) => {
                endpoint = endpoint.origin(target_uri(ip, true).parse().ok()?);
                Some(tls)
            }
            Err(e) => {
                log_warning!("gRPC TLS configuration is not usable: {}", e);
                return None;
            }
        },
    };
    Some(endpoint.connect_with_connector_lazy(MonitoredConnector { events, tls }))
}

/// The tonic client of one port's SoC.
pub struct TonicSoc {
    dual: DualToRActiveClient<Channel>,
    state: Arc<Mutex<String>>,
}

impl TonicSoc {
    async fn rpc<T, F>(&self, call: F) -> RpcResult<T>
    where
        F: Future<Output = Result<tonic::Response<T>, tonic::Status>>,
    {
        match tokio::time::timeout(RPC_TIMEOUT, call).await {
            Ok(Ok(resp)) => Ok(resp.into_inner()),
            // The deadline, as the server saw it (grpc-timeout): grpcio says
            // DEADLINE_EXCEEDED, tonic cancels with "Timeout expired".
            Ok(Err(status)) => match status.code() {
                tonic::Code::DeadlineExceeded => Err(RpcError::Timeout),
                tonic::Code::Cancelled if status.message() == "Timeout expired" => {
                    Err(RpcError::Timeout)
                }
                code => Err(RpcError::Status(code)),
            },
            Err(_) => Err(RpcError::Timeout),
        }
    }

    fn request<T>(msg: T) -> tonic::Request<T> {
        let mut req = tonic::Request::new(msg);
        req.set_timeout(RPC_TIMEOUT);
        req
    }
}

#[async_trait]
impl SocApi for TonicSoc {
    async fn query_admin_forwarding(&self) -> RpcResult<AdminReply> {
        let mut c = self.dual.clone();
        let req = Self::request(AdminRequest {
            portid: DEFAULT_PORT_IDS.to_vec(),
            state: vec![false, false],
        });
        self.rpc(c.query_admin_forwarding_port_state(req)).await
    }

    async fn set_admin_forwarding(&self, portid: i32, state: bool) -> RpcResult<AdminReply> {
        let mut c = self.dual.clone();
        let req = Self::request(AdminRequest {
            portid: vec![portid],
            state: vec![state],
        });
        self.rpc(c.set_admin_forwarding_port_state(req)).await
    }

    async fn query_link_state(&self) -> RpcResult<LinkStateReply> {
        let mut c = self.dual.clone();
        let req = Self::request(LinkStateRequest {
            portid: DEFAULT_PORT_IDS.to_vec(),
        });
        self.rpc(c.query_link_state(req)).await
    }

    async fn query_operation_state(&self) -> RpcResult<OperationReply> {
        let mut c = self.dual.clone();
        let req = Self::request(OperationRequest {
            portid: DEFAULT_PORT_IDS.to_vec(),
        });
        self.rpc(c.query_operation_port_state(req)).await
    }

    async fn query_server_version(&self) -> RpcResult<ServerVersionReply> {
        let mut c = self.dual.clone();
        let req = Self::request(ServerVersionRequest {
            version: "1.0".into(),
        });
        self.rpc(c.query_server_version(req)).await
    }

    fn connectivity(&self) -> String {
        self.state.lock().unwrap().clone()
    }
}

/// Channels from CONFIG_DB GRPCCLIENT, re-read for every setup as Python does.
pub struct TonicConnector;

#[async_trait]
impl SocConnector for TonicConnector {
    async fn connect(&self, port: &str, ip: IpAddr, db: Arc<dyn Db>) -> Option<Arc<dyn SocApi>> {
        log_notice!("Setting up gRPC channel for RPC's {} {}", port, ip);
        let Some(cfg) = config::read_channel_config(db.as_ref(), port).await else {
            log_warning!(
                "stub was not setup for gRPC soc ip {} port {}, no gRPC soc server running ?",
                ip,
                port
            );
            log_warning!(
                "channel was not setup for gRPC soc ip {} port {}, no gRPC soc server running ?",
                ip,
                port
            );
            return None;
        };
        let (tx, rx) = mpsc::unbounded_channel();
        let channel = make_channel(ip, &cfg, Some(tx))?;
        let state = Arc::new(Mutex::new("unknown".to_string()));
        tokio::spawn(watch_connectivity(
            port.to_string(),
            rx,
            Arc::clone(&state),
            db,
        ));
        Some(Arc::new(TonicSoc {
            dual: DualToRActiveClient::new(channel),
            state,
        }))
    }
}

// --- GracefulRestart -----------------------------------------------------------

/// The `tor` of the GracefulRestart request, as Python sends it: UPPER_TOR
/// from read side 0, which is the lower ToR, and LOWER_TOR otherwise. Which
/// one the SoC expects is not confirmed (HLD open question 9).
pub fn tor_side(read_side: i32) -> ToRSide {
    if read_side == 0 {
        ToRSide::UpperTor
    } else {
        ToRSide::LowerTor
    }
}

/// The GracefulRestart subscription of one SoC, as Python's
/// GracefulRestartClient: send the request, read the stream until it ends,
/// then wait the `period` of the last response, or 20 s without one, and
/// subscribe again. The notifications are only logged.
pub async fn graceful_restart_loop(port: String, ip: IpAddr, read_side: i32, db: Arc<dyn Db>) {
    let mut client: Option<GracefulRestartClient<Channel>> = None;
    loop {
        if client.is_none() {
            client = match config::read_channel_config(db.as_ref(), &port).await {
                Some(cfg) => make_channel(ip, &cfg, None).map(GracefulRestartClient::new),
                None => None,
            };
            if client.is_none() {
                log_warning!(
                    "GracefulRestart: no channel for port {}, trying again in 20 s",
                    port
                );
                tokio::time::sleep(Duration::from_secs(20)).await;
                continue;
            }
        }
        let mut c = client.clone().unwrap();
        let tor = tor_side(read_side);
        let mut last_period: Option<i32> = None;
        match c
            .notify_graceful_restart_start(GracefulAdminRequest { tor: tor as i32 })
            .await
        {
            Ok(resp) => {
                let mut stream = resp.into_inner();
                let mut index = 0;
                loop {
                    match stream.message().await {
                        Ok(Some(m)) => {
                            log_debug!(
                                "Async client received from direct read period port = {}: period = {} index = {} guid = {} notifytype {} msgtype = {} tid {}",
                                port, m.period, index, m.guid, m.notifytype, m.msgtype, "ycabled-rs"
                            );
                            index += 1;
                            last_period = Some(m.period);
                        }
                        Ok(None) => {
                            log_notice!(
                                "Async client finished loop from direct read period port:{} ",
                                port
                            );
                            break;
                        }
                        Err(status) => {
                            log_notice!(
                                "Async client port = {} exception occured because of {} tid {}",
                                port,
                                code_name(status.code()),
                                "ycabled-rs"
                            );
                            break;
                        }
                    }
                }
            }
            Err(status) => {
                log_notice!(
                    "Async client port = {} exception occured because of {} tid {}",
                    port,
                    code_name(status.code()),
                    "ycabled-rs"
                );
            }
        }
        let wait = match last_period {
            Some(p) => Duration::from_secs(p.max(0) as u64).max(Duration::from_millis(100)),
            None => Duration::from_secs(20),
        };
        tokio::time::sleep(wait).await;
    }
}

// --- Reply parsing (Python's parse_grpc_response_*) -----------------------------

fn up_down(v: bool) -> &'static str {
    if v {
        "up"
    } else {
        "down"
    }
}

fn active_standby(v: bool) -> &'static str {
    if v {
        "active"
    } else {
        "standby"
    }
}

/// (self, peer) from a two-entry state list: index `read_side` is this ToR.
fn by_read_side(
    states: &[bool],
    portids: usize,
    read_side: i32,
    name: fn(bool) -> &'static str,
) -> Option<(&'static str, &'static str)> {
    if portids != 2 || states.len() != 2 {
        return None;
    }
    match read_side {
        0 => Some((name(states[0]), name(states[1]))),
        1 => Some((name(states[1]), name(states[0]))),
        _ => Some(("unknown", "unknown")),
    }
}

/// Forwarding states (self, peer) of a QueryAdminForwardingPortState reply.
pub fn parse_forwarding_state(
    reply: &RpcResult<AdminReply>,
    read_side: i32,
) -> (&'static str, &'static str) {
    match reply {
        Ok(r) => by_read_side(&r.state, r.portid.len(), read_side, active_standby).unwrap_or_else(|| {
            log_warning!(
                "recieved an error port list while parsing response forwarding port state list size 0 {} {}",
                r.portid.len(),
                r.state.len()
            );
            ("unknown", "unknown")
        }),
        Err(_) => ("unknown", "unknown"),
    }
}

/// Link or operation states (self, peer) of a reply.
pub fn parse_link_state(
    ok: Option<(&[i32], &[bool])>,
    read_side: i32,
    query: &str,
) -> (&'static str, &'static str) {
    match ok {
        Some((portid, state)) => by_read_side(state, portid.len(), read_side, up_down).unwrap_or_else(|| {
            log_warning!(
                "recieved an error port list while parsing response {} port state list size 0 {} {}",
                query,
                portid.len(),
                state.len()
            );
            ("unknown", "unknown")
        }),
        None => ("unknown", "unknown"),
    }
}

/// The state a SetAdminForwardingPortState reply confirms for `portid`.
pub fn parse_set_reply(reply: &RpcResult<AdminReply>, portid: i32, port: &str) -> &'static str {
    match reply {
        Ok(r) => {
            if r.portid.is_empty() || r.state.is_empty() {
                log_warning!(
                    "recieved an error portid while parsing response hw mux no portid for port {}",
                    port
                );
                "unknown"
            } else if r.portid[0] != portid {
                log_warning!("recieved an error portid while parsing response hw mux port list size 0 for port {}", port);
                "unknown"
            } else {
                active_standby(r.state[0])
            }
        }
        Err(_) => {
            log_warning!(
                "recieved an error state while parsing response hw mux for port {}",
                port
            );
            "unknown"
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn admin(portid: Vec<i32>, state: Vec<bool>) -> RpcResult<AdminReply> {
        Ok(AdminReply { portid, state })
    }

    #[test]
    fn forwarding_state_by_read_side() {
        assert_eq!(
            parse_forwarding_state(&admin(vec![0, 1], vec![true, false]), 0),
            ("active", "standby")
        );
        assert_eq!(
            parse_forwarding_state(&admin(vec![0, 1], vec![true, false]), 1),
            ("standby", "active")
        );
        assert_eq!(
            parse_forwarding_state(&admin(vec![0, 1], vec![true, false]), -1),
            ("unknown", "unknown")
        );
        assert_eq!(
            parse_forwarding_state(&admin(vec![0], vec![true]), 0),
            ("unknown", "unknown")
        );
        assert_eq!(
            parse_forwarding_state(&Err(RpcError::Timeout), 0),
            ("unknown", "unknown")
        );
    }

    #[test]
    fn link_state_by_read_side() {
        assert_eq!(
            parse_link_state(Some((&[0, 1], &[true, false])), 0, "link_state"),
            ("up", "down")
        );
        assert_eq!(
            parse_link_state(Some((&[0, 1], &[true, false])), 1, "link_state"),
            ("down", "up")
        );
        assert_eq!(
            parse_link_state(Some((&[0], &[true])), 1, "oper_state"),
            ("unknown", "unknown")
        );
        assert_eq!(
            parse_link_state(None, 1, "oper_state"),
            ("unknown", "unknown")
        );
    }

    #[test]
    fn set_reply() {
        assert_eq!(
            parse_set_reply(&admin(vec![1], vec![true]), 1, "E0"),
            "active"
        );
        assert_eq!(
            parse_set_reply(&admin(vec![0], vec![false]), 0, "E0"),
            "standby"
        );
        assert_eq!(
            parse_set_reply(&admin(vec![7], vec![true]), 0, "E0"),
            "unknown"
        );
        assert_eq!(parse_set_reply(&admin(vec![], vec![]), 0, "E0"), "unknown");
        assert_eq!(
            parse_set_reply(&Err(RpcError::Status(tonic::Code::Unavailable)), 0, "E0"),
            "unknown"
        );
    }

    #[test]
    fn targets() {
        assert_eq!(
            target_uri("10.1.1.1".parse().unwrap(), false),
            "http://10.1.1.1:50075"
        );
        assert_eq!(
            target_uri("::1".parse().unwrap(), true),
            "https://[::1]:50075"
        );
    }

    #[test]
    fn tor_sides() {
        assert_eq!(tor_side(0), ToRSide::UpperTor);
        assert_eq!(tor_side(1), ToRSide::LowerTor);
        assert_eq!(tor_side(-1), ToRSide::LowerTor);
    }

    #[test]
    fn code_names() {
        assert_eq!(
            code_name(tonic::Code::Unavailable),
            "StatusCode.UNAVAILABLE"
        );
        assert_eq!(
            code_name(tonic::Code::Unimplemented),
            "StatusCode.UNIMPLEMENTED"
        );
    }

    #[test]
    fn states_from_events() {
        assert_eq!(state_for(ConnEvent::Connecting), "CONNECTING");
        assert_eq!(state_for(ConnEvent::Connected), "READY");
        assert_eq!(state_for(ConnEvent::ConnectFailed), "TRANSIENT_FAILURE");
        assert_eq!(state_for(ConnEvent::Closed), "IDLE");
    }
}
