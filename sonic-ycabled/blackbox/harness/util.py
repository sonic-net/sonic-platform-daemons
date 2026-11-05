"""Small helpers shared by the harness modules."""

import os
import shutil
import time

APPL_DB = 0
CONFIG_DB = 4
STATE_DB = 6

DB_IDS = {"APPL_DB": APPL_DB, "CONFIG_DB": CONFIG_DB, "STATE_DB": STATE_DB}
DB_NAMES = {v: k for k, v in DB_IDS.items()}
SEPARATORS = {APPL_DB: ":", CONFIG_DB: "|", STATE_DB: "|"}

# Format of every timestamp ycabled writes (UTC).
TIMESTAMP_RE = r"^\d{4}-[A-Z][a-z]{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{6}$"


class WaitTimeout(AssertionError):
    pass


def wait_until(predicate, timeout, interval=0.05, message=None):
    """Poll `predicate` until it returns a truthy value, and return that value.
    `message` may be a callable, evaluated when the wait times out."""
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() >= deadline:
            if callable(message):
                message = message()
            raise WaitTimeout(message or "condition not met within {}s".format(timeout))
        time.sleep(interval)


def stays_true(predicate, duration, interval=0.05):
    """Return True if `predicate` holds for the whole `duration`."""
    deadline = time.monotonic() + duration
    while time.monotonic() < deadline:
        if not predicate():
            return False
        time.sleep(interval)
    return bool(predicate())


def clear_dir(path):
    """Empties `path`, creating it if needed. The directory itself stays: the
    suite may not be allowed to delete it (env.SYSTEM_DIRS)."""
    os.makedirs(path, exist_ok=True)
    for name in os.listdir(path):
        entry = os.path.join(path, name)
        if os.path.isdir(entry) and not os.path.islink(entry):
            shutil.rmtree(entry)
        else:
            os.unlink(entry)
