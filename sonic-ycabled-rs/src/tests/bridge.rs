//! Tests of the Y-cable bridge in the embedded interpreter, with fake
//! sonic_y_cable and platform modules: Python ycabled's driver selection
//! (test_check_identifier_presence_and_update_mux_table_entry*,
//! test_y_cable_wrapper_get_presence*, test_y_cable_wrapper_get_transceiver_info*,
//! test_y_cable_helper_format_mapping_identifier*, test_hook_y_cable_simulated).

use std::ffi::CString;

use pyo3::prelude::*;

use crate::bridge::{to_pyval, CallError, PyBridge, PyPlatform, YCablePlatform};
use crate::logger::{self, Level};
use crate::pyval::{PyArg, PyVal};

/// Every PyBridge of the test process loads the same `ycabled_bridge`
/// module object, so tests that change its globals take turns.
static BRIDGE_MODULE: tokio::sync::Mutex<()> = tokio::sync::Mutex::const_new(());

const FAKES: &str = r#"
import sys, types
pkg = types.ModuleType('sonic_y_cable'); pkg.__path__ = []
vm = types.ModuleType('sonic_y_cable.y_cable_vendor_mapping')
vm.mapping = {'bb_vendor': {'bb_model': 'bbt.driver', 'bb_noclass': 'bbt.noclass', 'bb_nomodule': 'bbt.missing'},
              'microsoft': {'simulated': 'bbt.driver'}}
pkg.y_cable_vendor_mapping = vm
drv = types.ModuleType('sonic_y_cable.bbt.driver')
noclass = types.ModuleType('sonic_y_cable.bbt.noclass')
VENDORS = {}
INIT = {}
RAISE_INIT = set()
PRESENT = {}
INFO = {}

class YCable(object):
    EEPROM_ERROR = -1
    def __init__(self, port, logger):
        if port in RAISE_INIT:
            raise OSError('EEPROM unreadable')
        self.port = port
        self._logger = logger
        logger.log_notice('y_cable_port {}: loaded'.format(port))
        self._initialized = INIT.get(port, True)
    def get_vendor(self):
        v = VENDORS.get(self.port, 'BB Vendor')
        if isinstance(v, Exception):
            raise v
        return v
    def get_read_side(self):
        return 2
    def get_eye_heights(self, target):
        return [1.5e-9, 7]
    def toggle_mux_to_tor_a(self):
        raise OSError('I2C failure')

drv.YCable = YCable
for m in (pkg, vm, drv, noclass):
    sys.modules[m.__name__] = m

class Sfp(object):
    def __init__(self, i):
        self.i = i
    def get_presence(self):
        p = PRESENT.get(self.i, True)
        if p is NotImplementedError:
            raise NotImplementedError()
        return p
    def get_transceiver_info(self):
        return INFO.get(self.i, {'manufacturer': 'BB Vendor', 'model': 'BB Model'})

class Chassis(object):
    def get_sfp(self, i):
        return Sfp(i)

_chassis = Chassis()
"#;

#[tokio::test]
async fn bridge_selects_and_calls_drivers() {
    let _turn = BRIDGE_MODULE.lock().await;
    let bridge = PyBridge::start(true).expect("bridge");
    bridge.exec(FAKES).expect("fakes");
    let p = PyPlatform::new(bridge.clone());

    // Presence through the chassis; NotImplementedError falls back to VS.
    assert!(p.presence(1).await);
    bridge
        .exec("PRESENT[2] = False\nPRESENT[3] = NotImplementedError")
        .unwrap();
    assert!(!p.presence(2).await);
    assert!(p.presence(3).await);

    // A driver: loaded, called, its exceptions and constants.
    assert_eq!(p.load("Ethernet0", 1).await, Some(true));
    assert!(p.has_driver(1).await);
    assert_eq!(p.call(1, "get_read_side", vec![]).await, Ok(PyVal::Int(2)));
    match p
        .call(1, "get_eye_heights", vec![PyArg::Int(1)])
        .await
        .unwrap()
    {
        PyVal::List(items, s) => {
            assert_eq!(s, "[1.5e-09, 7]");
            assert_eq!(items[0].py_str(), "1.5e-09");
            assert!(items[1].is_int());
        }
        other => panic!("{other:?}"),
    }
    assert_eq!(
        p.call(1, "toggle_mux_to_tor_a", vec![]).await,
        Err(CallError::Raised("OSError('I2C failure')".into()))
    );
    assert_eq!(p.attr(1, "EEPROM_ERROR").await, Ok(PyVal::Int(-1)));
    assert!(
        matches!(p.attr(1, "NO_SUCH").await, Err(CallError::Raised(e)) if e.starts_with("AttributeError"))
    );

    // No usable driver, for each reason Python ycabled checks.
    let failures = [
        (4, "INFO[4] = None"),
        (5, "INFO[5] = {'model': 'BB Model'}"),
        (6, "INFO[6] = {'manufacturer': 'BB Vendor'}"),
        (7, "INFO[7] = {'manufacturer': 123, 'model': 'BB Model'}"),
        (
            8,
            "INFO[8] = {'manufacturer': 'Nobody', 'model': 'BB Model'}",
        ),
        (
            9,
            "INFO[9] = {'manufacturer': 'BB Vendor', 'model': 'Unknown'}",
        ),
        (
            10,
            "INFO[10] = {'manufacturer': 'BB Vendor', 'model': 'BB NoModule'}",
        ),
        (
            11,
            "INFO[11] = {'manufacturer': 'BB Vendor', 'model': 'BB NoClass'}",
        ),
        (12, "RAISE_INIT.add(12)"),
        (13, "VENDORS[13] = OSError('busy')"),
        (14, "VENDORS[14] = 'Somebody Else'"),
    ];
    for (port, setup) in failures {
        bridge.exec(setup).unwrap();
        assert_eq!(p.load("EthernetX", port).await, None, "{setup}");
        assert!(!p.has_driver(port).await, "{setup}");
        assert_eq!(
            p.call(port, "get_read_side", vec![]).await,
            Err(CallError::NoDriver)
        );
    }

    // A driver that is not initialized yet; normalized vendor and model.
    bridge.exec("INIT[15] = False").unwrap();
    assert_eq!(p.load("Ethernet56", 15).await, Some(false));
    bridge
        .exec("INFO[16] = {'manufacturer': '  bb   VENDOR ', 'model': ' BB MODEL'}")
        .unwrap();
    assert_eq!(p.load("Ethernet60", 16).await, Some(true));

    // The mux simulator's config file selects the simulated driver.
    let dir = tempfile::tempdir().unwrap();
    let sim = dir.path().join("mux_simulator.json");
    std::fs::write(&sim, "{}").unwrap();
    bridge
        .exec(&format!(
            "MUX_SIMULATOR_CONFIG_FILE = {:?}\nVENDORS[17] = 'microsoft'",
            sim.to_str().unwrap()
        ))
        .unwrap();
    assert_eq!(p.load("Ethernet64", 17).await, Some(true));
    // Without the file the cable's own vendor is used, which the driver rejects.
    bridge
        .exec("MUX_SIMULATOR_CONFIG_FILE = '/nonexistent/mux_simulator.json'")
        .unwrap();
    assert_eq!(p.load("Ethernet64", 17).await, None);

    // Unloaded on removal.
    p.unload(1).await;
    assert!(!p.has_driver(1).await);

    // VS without a chassis: always present, no transceiver info, so no driver.
    bridge.exec("_chassis = None").unwrap();
    assert!(p.presence(20).await);
    assert_eq!(p.load("Ethernet76", 20).await, None);
}

/// A call still running after SLOW_CALL is logged, and so is its end; a
/// quick call is not (HLD 4.3.6). Python calls cannot be cancelled, so the
/// call itself is not cut short.
#[tokio::test]
async fn slow_calls_are_logged() {
    let _turn = BRIDGE_MODULE.lock().await;
    let bridge = PyBridge::start(true).expect("bridge");
    bridge
        .exec(
            r#"
import time
DELAY = {9001: 0.6}
class TimedSfp(object):
    def __init__(self, i):
        self.i = i
    def get_presence(self):
        time.sleep(DELAY.get(self.i, 0))
        return True
class TimedChassis(object):
    def get_sfp(self, i):
        return TimedSfp(i)
_chassis = TimedChassis()
"#,
        )
        .unwrap();
    let p = PyPlatform::new(bridge);

    assert!(p.presence(9001).await);
    let logs = logger::capture::find("get_presence() of port 9001");
    assert_eq!(logs.len(), 2, "{logs:?}");
    assert_eq!(
        logs[0],
        (
            Level::Warning,
            "Y-cable call get_presence() of port 9001 has not returned after 0.3 s".into()
        )
    );
    assert_eq!(logs[1].0, Level::Notice);
    assert!(
        logs[1]
            .1
            .starts_with("Y-cable call get_presence() of port 9001 returned after "),
        "{logs:?}"
    );

    assert!(p.presence(9002).await);
    assert!(logger::capture::find("of port 9002").is_empty());
}

#[test]
fn python_values_keep_type_and_text() {
    pyo3::prepare_freethreaded_python();
    Python::with_gil(|py| {
        let eval = |code: &str| {
            let c = CString::new(code).unwrap();
            to_pyval(&py.eval(&c, None, None).unwrap())
        };
        assert_eq!(eval("3.3"), PyVal::Float(3.3, "3.3".into()));
        assert_eq!(eval("1e-09").py_str(), "1e-09");
        assert_eq!(eval("5.0").py_str(), "5.0");
        assert_eq!(eval("True"), PyVal::Bool(true));
        assert_eq!(eval("-1"), PyVal::Int(-1));
        assert_eq!(
            eval("2**70"),
            PyVal::BigInt("1180591620717411303424".into())
        );
        assert_eq!(eval("None"), PyVal::None);
        assert_eq!(eval("'a b'"), PyVal::Str("a b".into()));
        let l = eval("[1, 'a', None]");
        assert!(l.is_list());
        assert_eq!(l.py_str(), "[1, 'a', None]");
        assert!(!eval("(1, 2)").is_list());
        let d = eval("{'version_active': '1.0', 3: [1]}");
        assert_eq!(
            d.dict_get("version_active"),
            Some(&PyVal::Str("1.0".into()))
        );
        assert_eq!(d.py_str(), "{'version_active': '1.0', 3: [1]}");
        match eval("object()") {
            PyVal::Other(t, _) => assert_eq!(t, "object"),
            other => panic!("{other:?}"),
        }
    });
}
