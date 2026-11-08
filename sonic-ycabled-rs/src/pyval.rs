//! Values that cross from the Python vendor drivers into Rust.
//!
//! A driver method returns a Python object. The bridge turns it into a
//! [`PyVal`] that keeps the Python type and the text Python's `str()` gives
//! for it, so that ycabled-rs writes exactly what Python ycabled writes
//! (`str(3.3)` is `3.3`, `str(1e-09)` is `1e-09`, `str(True)` is `True`), and
//! checks the same types (`isinstance(x, int)` is true for a bool).

use std::fmt;

#[derive(Clone, Debug, PartialEq)]
pub enum PyVal {
    None,
    Bool(bool),
    Int(i64),
    /// An int that does not fit i64, as Python's `str()`.
    BigInt(String),
    /// The value and Python's `str()` of it.
    Float(f64, String),
    Str(String),
    /// Items and Python's `str()` of the list.
    List(Vec<PyVal>, String),
    Tuple(Vec<PyVal>, String),
    /// Items in iteration order and Python's `str()` of the dict.
    Dict(Vec<(PyVal, PyVal)>, String),
    /// Any other object: its type name and `str()`.
    Other(String, String),
}

impl PyVal {
    pub fn str_(s: impl Into<String>) -> PyVal {
        PyVal::Str(s.into())
    }

    pub fn float(v: f64) -> PyVal {
        PyVal::Float(v, py_float_str(v))
    }

    pub fn list(items: Vec<PyVal>) -> PyVal {
        let s = format!(
            "[{}]",
            items
                .iter()
                .map(|i| i.py_repr())
                .collect::<Vec<_>>()
                .join(", ")
        );
        PyVal::List(items, s)
    }

    pub fn dict(items: Vec<(PyVal, PyVal)>) -> PyVal {
        let s = format!(
            "{{{}}}",
            items
                .iter()
                .map(|(k, v)| format!("{}: {}", k.py_repr(), v.py_repr()))
                .collect::<Vec<_>>()
                .join(", ")
        );
        PyVal::Dict(items, s)
    }

    /// Python's `str()`.
    pub fn py_str(&self) -> String {
        match self {
            PyVal::None => "None".to_string(),
            PyVal::Bool(true) => "True".to_string(),
            PyVal::Bool(false) => "False".to_string(),
            PyVal::Int(i) => i.to_string(),
            PyVal::BigInt(s) | PyVal::Float(_, s) | PyVal::Str(s) => s.clone(),
            PyVal::List(_, s) | PyVal::Tuple(_, s) | PyVal::Dict(_, s) | PyVal::Other(_, s) => {
                s.clone()
            }
        }
    }

    /// Python's `repr()`, for values built in Rust (tests and mocks).
    pub fn py_repr(&self) -> String {
        match self {
            PyVal::Str(s) => py_str_repr(s),
            other => other.py_str(),
        }
    }

    /// `isinstance(x, int)`: true for bools too.
    pub fn is_int(&self) -> bool {
        matches!(self, PyVal::Int(_) | PyVal::Bool(_) | PyVal::BigInt(_))
    }

    pub fn is_float(&self) -> bool {
        matches!(self, PyVal::Float(..))
    }

    /// `isinstance(x, list)`: not a tuple.
    pub fn is_list(&self) -> bool {
        matches!(self, PyVal::List(..))
    }

    pub fn is_dict(&self) -> bool {
        matches!(self, PyVal::Dict(..))
    }

    pub fn is_none(&self) -> bool {
        matches!(self, PyVal::None)
    }

    /// `x is True`.
    pub fn is_true(&self) -> bool {
        matches!(self, PyVal::Bool(true))
    }

    /// The value of an int or a bool.
    pub fn as_int(&self) -> Option<i64> {
        match self {
            PyVal::Int(i) => Some(*i),
            PyVal::Bool(b) => Some(*b as i64),
            _ => None,
        }
    }

    /// The value of a number (int, bool or float), for `<` and `==` against numbers.
    pub fn as_number(&self) -> Option<f64> {
        match self {
            PyVal::Int(i) => Some(*i as f64),
            PyVal::Bool(b) => Some(*b as i64 as f64),
            PyVal::Float(f, _) => Some(*f),
            _ => None,
        }
    }

    /// `x == n` for an int `n`, compared exactly, as Python compares numbers.
    pub fn eq_int(&self, n: i64) -> bool {
        match self {
            PyVal::Int(i) => *i == n,
            PyVal::Bool(b) => *b as i64 == n,
            PyVal::Float(f, _) => f.fract() == 0.0 && float_floor(*f) == Ok(n),
            _ => false,
        }
    }

    /// `x < n`, compared exactly, as Python compares numbers. Python raises
    /// for a string; ycabled-rs treats that as false.
    pub fn lt_int(&self, n: i64) -> bool {
        match self {
            PyVal::Int(i) => *i < n,
            PyVal::Bool(b) => (*b as i64) < n,
            // For an int n, x < n exactly when floor(x) < n.
            PyVal::Float(f, _) => match float_floor(*f) {
                Ok(i) => i < n,
                Err(below) => below,
            },
            // Out of the i64 range: below every i64 if negative.
            PyVal::BigInt(s) => s.starts_with('-'),
            _ => false,
        }
    }

    pub fn as_str(&self) -> Option<&str> {
        match self {
            PyVal::Str(s) => Some(s),
            _ => None,
        }
    }

    pub fn items(&self) -> Option<&[PyVal]> {
        match self {
            PyVal::List(v, _) | PyVal::Tuple(v, _) => Some(v),
            _ => None,
        }
    }

    pub fn dict_items(&self) -> Option<&[(PyVal, PyVal)]> {
        match self {
            PyVal::Dict(v, _) => Some(v),
            _ => None,
        }
    }

    /// `d.get(key)` of a dict with string keys.
    pub fn dict_get(&self, key: &str) -> Option<&PyVal> {
        self.dict_items()?
            .iter()
            .find(|(k, _)| k.as_str() == Some(key))
            .map(|(_, v)| v)
    }

    /// Python truthiness.
    pub fn truthy(&self) -> bool {
        match self {
            PyVal::None => false,
            PyVal::Bool(b) => *b,
            PyVal::Int(i) => *i != 0,
            PyVal::BigInt(_) => true,
            PyVal::Float(f, _) => *f != 0.0,
            PyVal::Str(s) => !s.is_empty(),
            PyVal::List(v, _) | PyVal::Tuple(v, _) => !v.is_empty(),
            PyVal::Dict(v, _) => !v.is_empty(),
            PyVal::Other(..) => true,
        }
    }
}

impl fmt::Display for PyVal {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.py_str())
    }
}

/// An argument for a driver method.
#[derive(Clone, Debug, PartialEq)]
pub enum PyArg {
    None,
    Bool(bool),
    Int(i64),
    Float(f64),
    Str(String),
}

impl PyArg {
    pub fn py_repr(&self) -> String {
        match self {
            PyArg::None => "None".into(),
            PyArg::Bool(true) => "True".into(),
            PyArg::Bool(false) => "False".into(),
            PyArg::Int(i) => i.to_string(),
            PyArg::Float(f) => py_float_str(*f),
            PyArg::Str(s) => py_str_repr(s),
        }
    }
}

impl From<i64> for PyArg {
    fn from(v: i64) -> Self {
        PyArg::Int(v)
    }
}
impl From<i32> for PyArg {
    fn from(v: i32) -> Self {
        PyArg::Int(v as i64)
    }
}
impl From<&str> for PyArg {
    fn from(v: &str) -> Self {
        PyArg::Str(v.to_string())
    }
}
impl From<String> for PyArg {
    fn from(v: String) -> Self {
        PyArg::Str(v)
    }
}
impl From<bool> for PyArg {
    fn from(v: bool) -> Self {
        PyArg::Bool(v)
    }
}

/// Python's `repr()` of a str: single quotes unless the text has a single
/// quote and no double quote.
pub fn py_str_repr(s: &str) -> String {
    let quote = if s.contains('\'') && !s.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut out = String::with_capacity(s.len() + 2);
    out.push(quote);
    for c in s.chars() {
        match c {
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if c == quote => {
                out.push('\\');
                out.push(c);
            }
            c if (c as u32) < 0x20 || c as u32 == 0x7f => {
                out.push_str(&format!("\\x{:02x}", c as u32))
            }
            c => out.push(c),
        }
    }
    out.push(quote);
    out
}

/// Python's `repr()`/`str()` of a float: the shortest text that reads back
/// the same value, in exponent form below 1e-4 and from 1e16.
pub fn py_float_str(v: f64) -> String {
    if v.is_nan() {
        return "nan".into();
    }
    if v.is_infinite() {
        return if v > 0.0 { "inf".into() } else { "-inf".into() };
    }
    if v == 0.0 {
        return if v.is_sign_negative() {
            "-0.0".into()
        } else {
            "0.0".into()
        };
    }
    // Shortest round-trip digits and exponent, from Rust's own formatter.
    let sci = format!("{:e}", v);
    let (mantissa, exp) = sci.split_once('e').unwrap();
    let exp: i32 = exp.parse().unwrap();
    let negative = mantissa.starts_with('-');
    let digits: String = mantissa.chars().filter(|c| c.is_ascii_digit()).collect();
    let sign = if negative { "-" } else { "" };
    if (-4..16).contains(&exp) {
        let mut s = String::new();
        if exp < 0 {
            s.push_str("0.");
            for _ in 0..(-exp - 1) {
                s.push('0');
            }
            s.push_str(&digits);
        } else {
            let int_len = (exp + 1) as usize;
            if digits.len() <= int_len {
                s.push_str(&digits);
                for _ in digits.len()..int_len {
                    s.push('0');
                }
                s.push_str(".0");
            } else {
                s.push_str(&digits[..int_len]);
                s.push('.');
                s.push_str(&digits[int_len..]);
            }
        }
        format!("{sign}{s}")
    } else {
        let m = if digits.len() == 1 {
            digits.clone()
        } else {
            format!("{}.{}", &digits[..1], &digits[1..])
        };
        format!(
            "{sign}{m}e{}{:02}",
            if exp < 0 { '-' } else { '+' },
            exp.abs()
        )
    }
}

/// floor(f) as an i64, or, out of the i64 range, whether f is below it
/// (false for NaN, which is not below anything).
fn float_floor(f: f64) -> Result<i64, bool> {
    // -2**63 and 2**63, exact as floats.
    const MIN: f64 = -9_223_372_036_854_775_808.0;
    const END: f64 = 9_223_372_036_854_775_808.0;
    if f.is_nan() {
        Err(false)
    } else if f < MIN {
        Err(true)
    } else if f >= END {
        Err(false)
    } else {
        Ok(f.floor() as i64)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn float_str_matches_python() {
        let cases = [
            (3.3, "3.3"),
            (5.0, "5.0"),
            (5.1, "5.1"),
            (0.1, "0.1"),
            (1e-9, "1e-09"),
            (2e-9, "2e-09"),
            (1.5e-5, "1.5e-05"),
            (0.0001, "0.0001"),
            (123456789.0, "123456789.0"),
            (1e16, "1e+16"),
            (1.25e17, "1.25e+17"),
            (-2.5, "-2.5"),
            (0.0, "0.0"),
            (100.0, "100.0"),
            (1.0 / 3.0, "0.3333333333333333"),
        ];
        for (v, want) in cases {
            assert_eq!(py_float_str(v), want, "{v}");
        }
    }

    #[test]
    fn str_of_scalars() {
        assert_eq!(PyVal::None.py_str(), "None");
        assert_eq!(PyVal::Bool(true).py_str(), "True");
        assert_eq!(PyVal::Bool(false).py_str(), "False");
        assert_eq!(PyVal::Int(-1).py_str(), "-1");
        assert_eq!(PyVal::str_("a b").py_str(), "a b");
        assert_eq!(PyVal::float(45.0).py_str(), "45.0");
    }

    #[test]
    fn str_of_containers() {
        let l = PyVal::list(vec![
            PyVal::Int(1),
            PyVal::str_("a"),
            PyVal::float(1e-9),
            PyVal::None,
        ]);
        assert_eq!(l.py_str(), "[1, 'a', 1e-09, None]");
        let d = PyVal::dict(vec![
            (PyVal::str_("k"), PyVal::Int(2)),
            (PyVal::Int(1), PyVal::str_("it's")),
        ]);
        assert_eq!(d.py_str(), "{'k': 2, 1: \"it's\"}");
    }

    #[test]
    fn int_comparisons_are_exact() {
        // -(2**70) and 2**70: beyond i64, ordered by their sign.
        let big_neg = PyVal::BigInt("-1180591620717411303424".into());
        let big_pos = PyVal::BigInt("1180591620717411303424".into());
        assert!(big_neg.lt_int(0) && big_neg.lt_int(i64::MIN));
        assert!(!big_pos.lt_int(0) && !big_pos.lt_int(i64::MAX));
        assert!(!big_neg.eq_int(0));
        // Ints compare as ints: no rounding through f64.
        assert!(PyVal::Int(i64::MAX - 1).lt_int(i64::MAX));
        assert!(!PyVal::Int(i64::MAX).lt_int(i64::MAX));
        assert!(PyVal::Bool(false).lt_int(1));
        assert!(PyVal::float(-0.5).lt_int(0));
        assert!(!PyVal::str_("-1").lt_int(0));
        // Floats against ints beyond 2**53, NaN and the infinities.
        let two_53 = PyVal::float(9007199254740992.0);
        assert!(two_53.lt_int(9007199254740993) && !two_53.eq_int(9007199254740993));
        assert!(two_53.eq_int(9007199254740992) && !two_53.lt_int(9007199254740992));
        assert!(PyVal::float(-1.0).eq_int(-1) && !PyVal::float(-1.5).eq_int(-1));
        assert!(PyVal::float(-1.5).lt_int(-1) && !PyVal::float(-0.5).lt_int(-1));
        assert!(!PyVal::float(f64::NAN).lt_int(0) && !PyVal::float(f64::NAN).eq_int(0));
        assert!(PyVal::float(f64::NEG_INFINITY).lt_int(i64::MIN));
        assert!(!PyVal::float(f64::INFINITY).lt_int(i64::MAX));
        assert!(!PyVal::float(9.3e18).eq_int(i64::MAX) && !PyVal::float(9.3e18).lt_int(i64::MAX));
    }

    #[test]
    fn python_type_checks() {
        assert!(PyVal::Bool(true).is_int());
        assert!(!PyVal::float(1.0).is_int());
        assert!(PyVal::Bool(true).is_true());
        assert!(!PyVal::Int(1).is_true());
        assert!(PyVal::list(vec![]).is_list());
        assert!(!PyVal::Tuple(vec![], "()".into()).is_list());
        assert!(PyVal::Int(-1).lt_int(0));
        assert!(!PyVal::str_("x").lt_int(0));
        assert!(PyVal::float(1.0).eq_int(1));
        assert!(PyVal::Bool(true).eq_int(1));
        assert!(!PyVal::str_("1").eq_int(1));
    }

    #[test]
    fn truthiness() {
        assert!(!PyVal::None.truthy());
        assert!(!PyVal::Int(0).truthy());
        assert!(PyVal::Int(2).truthy());
        assert!(!PyVal::str_("").truthy());
        assert!(PyVal::list(vec![PyVal::None]).truthy());
    }

    #[test]
    fn dict_get_by_str_key() {
        let d = PyVal::dict(vec![(PyVal::str_("version_active"), PyVal::str_("1.0"))]);
        assert_eq!(d.dict_get("version_active"), Some(&PyVal::str_("1.0")));
        assert_eq!(d.dict_get("x"), None);
    }

    #[test]
    fn str_repr_quotes() {
        assert_eq!(py_str_repr("a"), "'a'");
        assert_eq!(py_str_repr("it's"), "\"it's\"");
        assert_eq!(py_str_repr("a'b\"c"), "'a\\'b\"c'");
        assert_eq!(py_str_repr("x\ny"), "'x\\ny'");
    }
}
