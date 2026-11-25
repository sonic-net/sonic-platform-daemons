//! The Y-cable bridge: the platform API and the vendor drivers of
//! active-standby cables, in an embedded CPython (HLD 4.3.6).
//!
//! The interpreter starts only when an active-standby port needs it, without
//! Python's signal handlers, so SIGTERM stays with ycabled-rs. Calls run on
//! tokio's blocking threads, which take the GIL; drivers release it while
//! they wait on the cable, so different ports progress at the same time. Each
//! port's actor makes one call at a time.

use std::cell::OnceCell;
use std::collections::HashSet;
use std::ffi::CString;
use std::sync::{Arc, Mutex};

use async_trait::async_trait;
use pyo3::ffi;
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyFloat, PyInt, PyList, PyString, PyTuple};

use crate::logger::{self, Level};
use crate::pyval::{PyArg, PyVal};

const BRIDGE_SOURCE: &str = include_str!("../python/ycabled_bridge.py");

/// A driver call still running after this long is logged (HLD 4.3.6).
#[cfg(not(test))]
const SLOW_CALL: std::time::Duration = std::time::Duration::from_secs(5);
#[cfg(test)]
const SLOW_CALL: std::time::Duration = std::time::Duration::from_millis(300);

#[derive(Clone, Debug, PartialEq)]
pub enum CallError {
    /// No driver is loaded for the port.
    NoDriver,
    /// The driver raised; `repr()` of the exception.
    Raised(String),
}

impl std::fmt::Display for CallError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            CallError::NoDriver => f.write_str("no driver"),
            CallError::Raised(r) => f.write_str(r),
        }
    }
}

/// The platform API and the vendor drivers, as the port actors use them.
#[async_trait]
pub trait YCablePlatform: Send + Sync {
    /// Whether a cable is plugged into `physical`.
    async fn presence(&self, physical: u32) -> bool;
    /// Selects, loads and checks the driver. Some(initialized) when a driver
    /// is in place, None (after logging why) when there is none.
    async fn load(&self, port: &str, physical: u32) -> Option<bool>;
    async fn unload(&self, physical: u32);
    async fn has_driver(&self, physical: u32) -> bool;
    async fn call(&self, physical: u32, method: &str, args: Vec<PyArg>)
        -> Result<PyVal, CallError>;
    /// [`presence`](Self::presence), then [`call`](Self::call) if the cable
    /// is present, in one trip to the drivers. None: the cable is absent.
    async fn call_if_present(
        &self,
        physical: u32,
        method: &str,
        args: Vec<PyArg>,
    ) -> Option<Result<PyVal, CallError>> {
        if !self.presence(physical).await {
            return None;
        }
        Some(self.call(physical, method, args).await)
    }
    /// A driver attribute: a constant such as EEPROM_ERROR, or download_firmware_status.
    async fn attr(&self, physical: u32, name: &str) -> Result<PyVal, CallError>;
}

#[pyfunction]
fn ycabled_rs_log(level: u8, msg: &str) {
    logger::log(Level::from_u8(level), msg);
}

/// Python's `repr()` of an exception, as Python ycabled logs it.
fn exc_repr(py: Python<'_>, e: &PyErr) -> String {
    e.value(py)
        .repr()
        .map(|r| r.to_string())
        .unwrap_or_else(|_| e.to_string())
}

pub fn to_pyval(obj: &Bound<'_, PyAny>) -> PyVal {
    let text = || obj.str().map(|s| s.to_string()).unwrap_or_default();
    if obj.is_none() {
        PyVal::None
    } else if let Ok(b) = obj.downcast::<PyBool>() {
        PyVal::Bool(b.is_true())
    } else if obj.is_instance_of::<PyInt>() {
        match obj.extract::<i64>() {
            Ok(i) => PyVal::Int(i),
            Err(_) => PyVal::BigInt(text()),
        }
    } else if let Ok(f) = obj.downcast::<PyFloat>() {
        PyVal::Float(f.value(), text())
    } else if let Ok(s) = obj.downcast::<PyString>() {
        PyVal::Str(s.to_string())
    } else if let Ok(l) = obj.downcast::<PyList>() {
        PyVal::List(l.iter().map(|i| to_pyval(&i)).collect(), text())
    } else if let Ok(t) = obj.downcast::<PyTuple>() {
        PyVal::Tuple(t.iter().map(|i| to_pyval(&i)).collect(), text())
    } else if let Ok(d) = obj.downcast::<PyDict>() {
        PyVal::Dict(
            d.iter()
                .map(|(k, v)| (to_pyval(&k), to_pyval(&v)))
                .collect(),
            text(),
        )
    } else {
        let type_name = obj
            .get_type()
            .name()
            .map(|n| n.to_string())
            .unwrap_or_default();
        PyVal::Other(type_name, text())
    }
}

fn to_python<'py>(py: Python<'py>, arg: &PyArg) -> PyResult<Bound<'py, PyAny>> {
    Ok(match arg {
        PyArg::None => py.None().into_bound(py),
        PyArg::Bool(b) => PyBool::new(py, *b).to_owned().into_any(),
        PyArg::Int(i) => i.into_pyobject(py)?.into_any(),
        PyArg::Float(f) => PyFloat::new(py, *f).into_any(),
        PyArg::Str(s) => PyString::new(py, s).into_any(),
    })
}

/// The Python thread state of a thread that has made a call through the
/// bridge, kept until the thread exits. Without it, each `with_gil` on a
/// thread that Python did not start creates a thread state and its frame
/// stack and destroys them again (an mmap and a munmap per call).
struct ThreadState {
    gstate: ffi::PyGILState_STATE,
    tstate: *mut ffi::PyThreadState,
}

impl Drop for ThreadState {
    fn drop(&mut self) {
        // The thread is exiting, idle: take the GIL to delete its state.
        unsafe {
            ffi::PyEval_RestoreThread(self.tstate);
            ffi::PyGILState_Release(self.gstate);
        }
    }
}

thread_local! {
    static THREAD_STATE: OnceCell<ThreadState> = const { OnceCell::new() };
}

/// Gives this thread a Python thread state for the rest of its life. The
/// interpreter must be initialized, and the thread must not hold the GIL.
fn keep_thread_state() {
    THREAD_STATE.with(|s| {
        s.get_or_init(|| unsafe {
            let gstate = ffi::PyGILState_Ensure();
            let tstate = ffi::PyEval_SaveThread();
            ThreadState { gstate, tstate }
        });
    });
}

/// How many Python calls run at once, as in Python ycabled, whose calls run
/// one at a time. More would mostly queue for the GIL, each on a thread of
/// its own, and every hand-off of the GIL costs a futex wake-up and a
/// context switch.
pub const BRIDGE_SLOTS: usize = 1;

/// A call still running after this long gives its slot back. It is waiting
/// on the cable rather than competing for the GIL, and a slow cable must not
/// hold up the calls of other ports (N1): K hung cables delay the others by
/// at most K times this.
const SLOT_HOLD: std::time::Duration = std::time::Duration::from_millis(10);

/// The embedded interpreter with the bridge module loaded.
pub struct PyBridge {
    module: Py<PyModule>,
    slots: Arc<tokio::sync::Semaphore>,
}

impl PyBridge {
    /// Starts the interpreter and loads the platform API (not on VS). Err:
    /// the platform API cannot be loaded.
    pub fn start(is_vs: bool) -> Result<Arc<PyBridge>, String> {
        pyo3::prepare_freethreaded_python();
        Python::with_gil(|py| {
            let code = CString::new(BRIDGE_SOURCE).map_err(|e| e.to_string())?;
            let module = PyModule::from_code(py, &code, c"ycabled_bridge.py", c"ycabled_bridge")
                .map_err(|e| exc_repr(py, &e))?;
            let log = wrap_pyfunction!(ycabled_rs_log, py).map_err(|e| exc_repr(py, &e))?;
            module
                .getattr("init")
                .and_then(|f| f.call1((is_vs, log)))
                .map_err(|e| exc_repr(py, &e))?;
            Ok(Arc::new(PyBridge {
                module: module.unbind(),
                slots: Arc::new(tokio::sync::Semaphore::new(BRIDGE_SLOTS)),
            }))
        })
    }

    /// Runs `f` with the GIL on a blocking thread, in one of the
    /// [`BRIDGE_SLOTS`] until it has run for [`SLOT_HOLD`]. A Python call
    /// cannot be cancelled, so one that is still running after [`SLOW_CALL`]
    /// is only logged, as `what`.
    async fn with_module<T, F>(self: &Arc<Self>, what: impl FnOnce() -> String, f: F) -> T
    where
        T: Send + 'static,
        F: FnOnce(Python<'_>, &Bound<'_, PyModule>) -> T + Send + 'static,
    {
        // The semaphore is never closed.
        let slot = Arc::clone(&self.slots).acquire_owned().await.ok();
        let this = Arc::clone(self);
        let start = std::time::Instant::now();
        let mut task = tokio::task::spawn_blocking(move || {
            keep_thread_state();
            Python::with_gil(|py| f(py, this.module.bind(py)))
        });
        let res = match tokio::time::timeout(SLOT_HOLD, &mut task).await {
            Ok(res) => res,
            Err(_) => {
                drop(slot);
                match tokio::time::timeout(SLOW_CALL.saturating_sub(SLOT_HOLD), &mut task).await {
                    Ok(res) => res,
                    Err(_) => {
                        let what = what();
                        crate::log_warning!(
                            "Y-cable call {} has not returned after {} s",
                            what,
                            SLOW_CALL.as_secs_f64()
                        );
                        let res = task.await;
                        crate::log_notice!(
                            "Y-cable call {} returned after {:.1} s",
                            what,
                            start.elapsed().as_secs_f64()
                        );
                        res
                    }
                }
            }
        };
        match res {
            Ok(v) => v,
            // Only a panic in `f`, which the panic hook already reported.
            Err(e) => std::panic::resume_unwind(e.into_panic()),
        }
    }
}

#[cfg(test)]
impl PyBridge {
    /// Runs Python code in the bridge module's namespace, to set up fakes.
    pub fn exec(&self, code: &str) -> Result<(), String> {
        Python::with_gil(|py| {
            let globals = self.module.bind(py).dict();
            let code = CString::new(code).map_err(|e| e.to_string())?;
            py.run(&code, Some(&globals), None)
                .map_err(|e| exc_repr(py, &e))
        })
    }
}

/// [`YCablePlatform`] over the embedded interpreter.
pub struct PyPlatform {
    bridge: Arc<PyBridge>,
    /// The ports in the bridge module's `_drivers`, updated with it under
    /// the GIL, so that `has_driver` does not call into Python.
    drivers: Arc<Mutex<HashSet<u32>>>,
}

impl PyPlatform {
    pub fn new(bridge: Arc<PyBridge>) -> Arc<PyPlatform> {
        Arc::new(PyPlatform {
            bridge,
            drivers: Arc::default(),
        })
    }
}

fn driver_set(drivers: &Mutex<HashSet<u32>>) -> std::sync::MutexGuard<'_, HashSet<u32>> {
    drivers.lock().unwrap_or_else(|p| p.into_inner())
}

/// The cable's presence from the platform API; an exception reads as absent.
fn presence_in(py: Python<'_>, m: &Bound<'_, PyModule>, physical: u32) -> bool {
    match m.getattr("presence").and_then(|f| f.call1((physical,))) {
        Ok(v) => v.is_truthy().unwrap_or(false),
        Err(e) => {
            crate::log_warning!(
                "Failed to get the presence of port {} due to {}",
                physical,
                exc_repr(py, &e)
            );
            false
        }
    }
}

/// A driver method, with the GIL held. The driver set changes only with the
/// GIL held too, so it agrees with the bridge module's `_drivers` here.
fn call_in(
    py: Python<'_>,
    m: &Bound<'_, PyModule>,
    drivers: &Mutex<HashSet<u32>>,
    physical: u32,
    method: &str,
    args: &[PyArg],
) -> Result<PyVal, CallError> {
    if !driver_set(drivers).contains(&physical) {
        return Err(CallError::NoDriver);
    }
    let py_args: PyResult<Vec<Bound<'_, PyAny>>> = args.iter().map(|a| to_python(py, a)).collect();
    let res = py_args.and_then(|a| PyTuple::new(py, a)).and_then(|t| {
        m.getattr("call")
            .and_then(|f| f.call1((physical, method, t)))
    });
    match res {
        Ok(v) => Ok(to_pyval(&v)),
        Err(e) => Err(CallError::Raised(exc_repr(py, &e))),
    }
}

#[async_trait]
impl YCablePlatform for PyPlatform {
    async fn presence(&self, physical: u32) -> bool {
        self.bridge
            .with_module(
                || format!("get_presence() of port {}", physical),
                move |py, m| presence_in(py, m, physical),
            )
            .await
    }

    async fn load(&self, port: &str, physical: u32) -> Option<bool> {
        let port = port.to_string();
        let what = format!("to load the driver of {}", port);
        let drivers = Arc::clone(&self.drivers);
        self.bridge
            .with_module(
                || what,
                move |py, m| {
                    let res = m
                        .getattr("load")
                        .and_then(|f| f.call1((port.as_str(), physical)));
                    // load() drops the port's driver first, and keeps the
                    // new one only when it returns (True, initialized).
                    let loaded = match res {
                        Ok(v) => match v.extract::<(bool, Option<bool>)>() {
                            Ok((true, initialized)) => Some(initialized.unwrap_or(true)),
                            _ => None,
                        },
                        Err(e) => {
                            crate::log_warning!(
                                "Failed to load the Y-cable driver for port {} due to {}",
                                port,
                                exc_repr(py, &e)
                            );
                            None
                        }
                    };
                    let has = m
                        .getattr("has_driver")
                        .and_then(|f| f.call1((physical,)))
                        .and_then(|v| v.is_truthy())
                        .unwrap_or(false);
                    let mut set = driver_set(&drivers);
                    if has {
                        set.insert(physical);
                    } else {
                        set.remove(&physical);
                    }
                    loaded
                },
            )
            .await
    }

    async fn unload(&self, physical: u32) {
        let drivers = Arc::clone(&self.drivers);
        self.bridge
            .with_module(
                || format!("to unload the driver of port {}", physical),
                move |_py, m| {
                    let _ = m.getattr("unload").and_then(|f| f.call1((physical,)));
                    driver_set(&drivers).remove(&physical);
                },
            )
            .await
    }

    async fn has_driver(&self, physical: u32) -> bool {
        driver_set(&self.drivers).contains(&physical)
    }

    async fn call(
        &self,
        physical: u32,
        method: &str,
        args: Vec<PyArg>,
    ) -> Result<PyVal, CallError> {
        let drivers = Arc::clone(&self.drivers);
        let name = method.to_string();
        self.bridge
            .with_module(
                || format!("{}() of port {}", method, physical),
                move |py, m| call_in(py, m, &drivers, physical, &name, &args),
            )
            .await
    }

    async fn call_if_present(
        &self,
        physical: u32,
        method: &str,
        args: Vec<PyArg>,
    ) -> Option<Result<PyVal, CallError>> {
        let drivers = Arc::clone(&self.drivers);
        let name = method.to_string();
        self.bridge
            .with_module(
                || format!("get_presence() and {}() of port {}", method, physical),
                move |py, m| {
                    presence_in(py, m, physical)
                        .then(|| call_in(py, m, &drivers, physical, &name, &args))
                },
            )
            .await
    }

    async fn attr(&self, physical: u32, name: &str) -> Result<PyVal, CallError> {
        let drivers = Arc::clone(&self.drivers);
        let attr = name.to_string();
        self.bridge
            .with_module(
                || format!("{} of port {}", name, physical),
                move |py, m| {
                    if !driver_set(&drivers).contains(&physical) {
                        return Err(CallError::NoDriver);
                    }
                    match m
                        .getattr("attr")
                        .and_then(|f| f.call1((physical, attr.as_str())))
                    {
                        Ok(v) => Ok(to_pyval(&v)),
                        Err(e) => Err(CallError::Raised(exc_repr(py, &e))),
                    }
                },
            )
            .await
    }
}

/// The bridge, started the first time an active-standby port needs it: an
/// active-active-only device never starts the interpreter.
pub struct LazyPlatform {
    is_vs: bool,
    inner: tokio::sync::OnceCell<Option<Arc<PyPlatform>>>,
}

impl LazyPlatform {
    pub fn new(is_vs: bool) -> Arc<LazyPlatform> {
        Arc::new(LazyPlatform {
            is_vs,
            inner: tokio::sync::OnceCell::new(),
        })
    }

    async fn get(&self) -> Option<Arc<PyPlatform>> {
        let is_vs = self.is_vs;
        self.inner
            .get_or_init(|| async move {
                match tokio::task::spawn_blocking(move || PyBridge::start(is_vs)).await {
                    Ok(Ok(b)) => Some(PyPlatform::new(b)),
                    Ok(Err(e)) => {
                        crate::log_warning!("Failed to load chassis due to {}", e);
                        None
                    }
                    Err(_) => None,
                }
            })
            .await
            .clone()
    }
}

#[async_trait]
impl YCablePlatform for LazyPlatform {
    async fn presence(&self, physical: u32) -> bool {
        match self.get().await {
            Some(p) => p.presence(physical).await,
            None => false,
        }
    }

    async fn load(&self, port: &str, physical: u32) -> Option<bool> {
        self.get().await?.load(port, physical).await
    }

    async fn unload(&self, physical: u32) {
        if let Some(p) = self.get().await {
            p.unload(physical).await
        }
    }

    async fn has_driver(&self, physical: u32) -> bool {
        match self.get().await {
            Some(p) => p.has_driver(physical).await,
            None => false,
        }
    }

    async fn call(
        &self,
        physical: u32,
        method: &str,
        args: Vec<PyArg>,
    ) -> Result<PyVal, CallError> {
        match self.get().await {
            Some(p) => p.call(physical, method, args).await,
            None => Err(CallError::NoDriver),
        }
    }

    async fn call_if_present(
        &self,
        physical: u32,
        method: &str,
        args: Vec<PyArg>,
    ) -> Option<Result<PyVal, CallError>> {
        // Without the platform API no cable is present, as for presence().
        self.get()
            .await?
            .call_if_present(physical, method, args)
            .await
    }

    async fn attr(&self, physical: u32, name: &str) -> Result<PyVal, CallError> {
        match self.get().await {
            Some(p) => p.attr(physical, name).await,
            None => Err(CallError::NoDriver),
        }
    }
}
