# tests/conftest.py
import os
import sys
from imp import load_source

# 1) Ensure the daemon script is importable and loaded exactly once
_REPO_ROOT = os.path.dirname(os.path.dirname(__file__))
_SCRIPTS_DIR = os.path.join(_REPO_ROOT, "scripts")
_base = os.path.join(_SCRIPTS_DIR, "chassisd")
_chassisd_path = _base if os.path.exists(_base) else _base + ".py"

# Keep environment light for unit tests (many tests set this too; setting early helps)
os.environ.setdefault("CHASSISD_UNIT_TESTING", "1")

if "chassisd" not in sys.modules:
    load_source("chassisd", _chassisd_path)

# 2) Provide a very small, memory-light stub for SonicV2Connector used by chassisd
#    (Prevents lots of real redis connectors from being created across tests.)
class _FakeRedis:
    __slots__ = ("_h",)  # keep memory footprint tiny
    def __init__(self): self._h = {}
    def hgetall(self, key): return dict(self._h.get(key, {}))
    def hset(self, key, *args, **kwargs):
        if "mapping" in kwargs:
            self._h.setdefault(key, {}).update(kwargs["mapping"])
            return 1
        if len(args) == 2:
            field, value = args
            self._h.setdefault(key, {})[field] = value
            return 1
        return 0

class _DummyV2:
    # match what production code uses
    STATE_DB = 6
    CHASSIS_STATE_DB = 15  # harmless constant if referenced
    def __init__(self, *a, **k): self._client = _FakeRedis()
    def connect(self, _dbid): return None
    def close(self): return None
    def get_redis_client(self, _dbid): return self._client

# 3) Patch both the module-under-test’s swsscommon and the global swsscommon, if present
chassisd = sys.modules["chassisd"]

try:
    import swsscommon as _sc
except Exception:
    _sc = None

# Patch the symbol used by chassisd
if hasattr(chassisd, "swsscommon"):
    chassisd.swsscommon.SonicV2Connector = _DummyV2
    # ensure constants exist if code references them
    if not hasattr(chassisd.swsscommon.SonicV2Connector, "STATE_DB"):
        chassisd.swsscommon.SonicV2Connector.STATE_DB = 6

# Also patch the top-level swsscommon for tests that import it directly
if _sc is not None:
    _sc.SonicV2Connector = _DummyV2
    if not hasattr(_sc.SonicV2Connector, "STATE_DB"):
        _sc.SonicV2Connector.STATE_DB = 6
