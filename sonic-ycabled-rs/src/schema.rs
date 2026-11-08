//! Table and field names ycabled uses, and how it writes values (HLD 3.6).

pub const MUX_CABLE: &str = "MUX_CABLE";
pub const PORT: &str = "PORT";
pub const DEVICE_METADATA: &str = "DEVICE_METADATA";
pub const LOOPBACK_INTERFACE: &str = "LOOPBACK_INTERFACE";
pub const GRPCCLIENT: &str = "GRPCCLIENT";
pub const XCVRD_LOG: &str = "XCVRD_LOG";

pub const TRANSCEIVER_INFO: &str = "TRANSCEIVER_INFO";
pub const HW_MUX_CABLE_TABLE: &str = "HW_MUX_CABLE_TABLE";
pub const HW_MUX_CABLE_TABLE_PEER: &str = "HW_MUX_CABLE_TABLE_PEER";
pub const MUX_CABLE_INFO: &str = "MUX_CABLE_INFO";
pub const MUX_CABLE_STATIC_INFO: &str = "MUX_CABLE_STATIC_INFO";
pub const MUX_METRICS_TABLE: &str = "MUX_METRICS_TABLE";

pub const MUX_CABLE_COMMAND_TABLE: &str = "MUX_CABLE_COMMAND_TABLE";
pub const MUX_CABLE_RESPONSE_TABLE: &str = "MUX_CABLE_RESPONSE_TABLE";
pub const FORWARDING_STATE_COMMAND: &str = "FORWARDING_STATE_COMMAND";
pub const FORWARDING_STATE_RESPONSE: &str = "FORWARDING_STATE_RESPONSE";
pub const HW_FORWARDING_STATE_PEER: &str = "HW_FORWARDING_STATE_PEER";

/// Subscribed, as by Python ycabled, but nothing writes it.
pub const XCVRD_DOWN_FW_STATUS_CMD: &str = "XCVRD_DOWN_FW_STATUS_CMD";

/// The CLI command tables, in the order Python ycabled reads them.
pub const CLI_COMMAND_TABLES: [&str; 13] = [
    "XCVRD_SHOW_HWMODE_DIR_CMD",
    "XCVRD_CONFIG_HWMODE_DIR_CMD",
    "XCVRD_SHOW_HWMODE_SWMODE_CMD",
    "XCVRD_CONFIG_HWMODE_SWMODE_CMD",
    "XCVRD_SHOW_FW_CMD",
    "XCVRD_DOWN_FW_CMD",
    "XCVRD_ACTI_FW_CMD",
    "XCVRD_ROLL_FW_CMD",
    "XCVRD_CONFIG_PRBS_CMD",
    "XCVRD_CONFIG_LOOP_CMD",
    "XCVRD_EVENT_LOG_CMD",
    "XCVRD_GET_FEC_CMD",
    "XCVRD_GET_BER_CMD",
];

/// The states a MUX_CABLE entry may have (sonic-mux-cable.yang).
pub const CONFIG_MUX_STATES: [&str; 5] = ["active", "standby", "auto", "manual", "detach"];

pub const GRPC_PORT: u16 = 50075;

/// `datetime.utcnow().strftime("%Y-%b-%d %H:%M:%S.%f")`, the format of every
/// timestamp ycabled writes.
pub fn timestamp() -> String {
    chrono::Utc::now()
        .format("%Y-%b-%d %H:%M:%S%.6f")
        .to_string()
}

/// Field-value pairs from string literals and owned values.
#[macro_export]
macro_rules! fields {
    ($($k:expr => $v:expr),* $(,)?) => {
        vec![$(($k, $v.to_string())),*]
    };
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn timestamp_format() {
        let ts = timestamp();
        let re = regex_lite(&ts);
        assert!(re, "{ts}");
    }

    // ^\d{4}-[A-Z][a-z]{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{6}$
    fn regex_lite(s: &str) -> bool {
        let b = s.as_bytes();
        if b.len() != 27 {
            return false;
        }
        let digit = |i: usize| b[i].is_ascii_digit();
        (0..4).all(digit)
            && b[4] == b'-'
            && b[5].is_ascii_uppercase()
            && b[6].is_ascii_lowercase()
            && b[7].is_ascii_lowercase()
            && b[8] == b'-'
            && digit(9)
            && digit(10)
            && b[11] == b' '
            && digit(12)
            && digit(13)
            && b[14] == b':'
            && digit(15)
            && digit(16)
            && b[17] == b':'
            && digit(18)
            && digit(19)
            && b[20] == b'.'
            && (21..27).all(digit)
    }
}
