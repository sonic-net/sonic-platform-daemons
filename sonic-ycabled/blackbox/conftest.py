"""Fixtures of the ycabled black-box suite. See README.md.

The suite takes over /dev/log, /var/run/redis*, /etc/sonic and /usr/share/sonic,
so it runs only where BB_ENABLE=1 says that is fine: run.sh's container, or a
CI container. Elsewhere, for example a `pytest` run of the unit tests on a
developer machine or in a sonic-buildimage build, its tests are not collected.
"""

import glob
import json
import os
import pathlib
import re
import subprocess
import sys

import pytest

BLACKBOX_DIR = os.path.dirname(os.path.abspath(__file__))
TESTS_DIR = os.path.join(BLACKBOX_DIR, "tests")
ENABLED = os.environ.get("BB_ENABLE") == "1"
# ycabled's environment. Taken now, before the unit tests of the same session
# change os.environ.
BASE_ENV = dict(os.environ)

sys.path.insert(0, BLACKBOX_DIR)

_ENV = {}
_NOT_COLLECTED = []
# The black-box test modules collected, and the black-box tests deselected
# (-k, -m, --deselect, --lf): to tell a run of the whole suite.
_COLLECTED_MODULES = set()
_DESELECTED = []


def _in_suite(item):
    return str(item.path).startswith(TESTS_DIR + os.sep)


def pytest_ignore_collect(collection_path, config):
    if not ENABLED and TESTS_DIR in (str(collection_path), str(collection_path.parent)):
        _NOT_COLLECTED.append(collection_path)
        return True
    return None


def pytest_addoption(parser):
    group = parser.getgroup("ycabled black-box")
    group.addoption("--bb-workdir", default="/tmp/ycabled-blackbox", help="scratch directory")
    group.addoption("--bb-report-dir", default=None, help="where to write the coverage reports")
    group.addoption("--bb-coverage", action="store_true",
                    help="measure Python ycabled's code coverage under the suite")
    group.addoption("--bb-no-require-full-contract", action="store_false", dest="bb_require_full_contract",
                    help="do not fail a run of the whole suite on Python ycabled that left a DB contract entry "
                         "unexercised (checked by default)")
    group.addoption("--bb-test-timeout", type=float, default=300,
                    help="seconds a black-box test may take, fixtures included (pytest-timeout); 0: no limit")


def pytest_configure(config):
    if ENABLED:
        from harness.env import check_prerequisites
        problem = check_prerequisites()
        # Several known issues are hangs: no run goes without a time limit.
        if not problem and config.getoption("--bb-test-timeout") > 0 and not config.pluginmanager.hasplugin("timeout"):
            problem = "pytest-timeout is not installed (pip install pytest-timeout, or the python3-pytest-timeout " \
                      "package), and --bb-test-timeout is not 0"
        if problem:
            raise pytest.UsageError("BB_ENABLE=1, but " + problem)
    config.addinivalue_line("markers", "slow: waits for one of ycabled's 60 s timers")
    config.addinivalue_line("markers", "known_issue(ref, reason): a known issue of Python ycabled; "
                                       "expected to fail on the Python implementation")
    config.addinivalue_line("markers", "python_only: checks a mechanism of Python ycabled only")


def pytest_collection_modifyitems(config, items):
    # The hook sees the whole session, unit tests included.
    items = [item for item in items if _in_suite(item)]
    if not items:
        return
    _COLLECTED_MODULES.update(str(item.path) for item in items)
    # A marker, not the timeout ini option: it applies whichever pytest.ini
    # the run uses, sonic-ycabled's included.
    timeout = config.getoption("--bb-test-timeout")
    if timeout > 0 and config.pluginmanager.hasplugin("timeout"):
        for item in items:
            if item.get_closest_marker("timeout") is None:
                item.add_marker(pytest.mark.timeout(timeout))
    from harness.ycabled import implementation
    if implementation() != "python":
        for item in items:
            if item.get_closest_marker("python_only"):
                item.add_marker(pytest.mark.skip(reason="checks a mechanism of Python ycabled"))
        return
    for item in items:
        mark = item.get_closest_marker("known_issue")
        if mark is not None:
            ref = mark.args[0] if mark.args else ""
            reason = mark.args[1] if len(mark.args) > 1 else mark.kwargs.get("reason", "")
            # AssertionError only: a harness error (harness.util.HarnessError) or
            # a timeout must not pass as the known issue.
            item.add_marker(pytest.mark.xfail(strict=True, raises=AssertionError,
                                              reason="known issue {}: {}".format(ref, reason)))


def pytest_deselected(items):
    _DESELECTED.extend(item for item in items if _in_suite(item))


def _partial_selection(config):
    """Why this run does not select the whole suite, or None: an argument
    names a part of it (a file, a test, a subdirectory), a module was not
    collected (--ignore), or tests were deselected."""
    tests = pathlib.Path(TESTS_DIR).resolve()
    whole = False
    for arg in config.args:
        path, node, _ = arg.partition("::")
        p = (config.invocation_params.dir / path).resolve()
        if p == tests and not node:
            whole = True
        elif p == tests or tests in p.parents:
            return "a part of the suite was selected ({})".format(arg)
        elif p in tests.parents and not node:
            whole = True
    if not whole:
        return "the suite's tests directory was not selected"
    missing = sorted(os.path.basename(f) for f in glob.glob(os.path.join(TESTS_DIR, "test_*.py"))
                     if f not in _COLLECTED_MODULES)
    if missing:
        return "not collected: {}".format(", ".join(missing))
    if _DESELECTED:
        return "{} test{} deselected".format(len(_DESELECTED), "" if len(_DESELECTED) == 1 else "s")
    return None


def _full_contract_not_required(session):
    """Why this run does not have to exercise every DB contract entry, or None."""
    from harness.ycabled import implementation
    if not session.config.getoption("bb_require_full_contract"):
        return "--bb-no-require-full-contract"
    if implementation() != "python":
        # The contract is what Python ycabled may do; another implementation
        # need not make every access Python makes (HLD 3.8 issue 10, say).
        return "the daemon under test is not Python ycabled"
    if session.shouldstop or session.shouldfail:
        return "the run stopped early"
    return _partial_selection(session.config)


@pytest.fixture(scope="session")
def bb_env(request):
    if not ENABLED:
        pytest.skip("set BB_ENABLE=1 to run the black-box tests, in a throwaway container (see README.md)")
    from harness.env import BlackboxEnv
    # Absolute: ycabled runs from /, and /dev/log points into the workdir.
    workdir = os.path.abspath(request.config.getoption("--bb-workdir"))
    env = BlackboxEnv(workdir, BASE_ENV, _coverage_target(request.config, workdir))
    try:
        env.start()
        _ENV["env"] = env
        yield env
    finally:
        # Also after a failed start: redis-server may be running.
        env.stop()


def _coverage_target(config, workdir):
    """Where Python ycabled's coverage data goes: the suite's own report with
    --bb-coverage, else into the pytest-cov report of the run, if any."""
    from harness.env import CoverageTarget
    if config.getoption("--bb-coverage"):
        return CoverageTarget(os.path.join(workdir, "coverage", ".coverage"), True, own_report=True)
    plugin = config.pluginmanager.get_plugin("_cov")
    controller = getattr(plugin, "cov_controller", None)
    cov = getattr(controller, "combining_cov", None) or getattr(controller, "cov", None)
    if cov is None:
        return None
    # pytest-cov combines <data file>.* when the tests are done.
    data_file = os.path.abspath(cov.config.data_file)
    for stale in glob.glob(data_file + ".bb.*"):
        os.unlink(stale)
    return CoverageTarget(data_file, bool(cov.config.branch), own_report=False)


@pytest.fixture
def dut(bb_env, request):
    from harness.dut import Dut
    name = re.sub(r"[^A-Za-z0-9_.-]+", "_", request.node.name)[:80]
    d = Dut(bb_env, name)
    yield d
    problems = d.teardown()
    if problems:
        pytest.fail("\n".join(problems), pytrace=False)


@pytest.fixture(scope="module")
def module_dut(bb_env, request):
    """A device shared by the tests of one module, for cases that wait for
    ycabled's 60 s timers once and then check several things."""
    from harness.dut import Dut
    d = Dut(bb_env, request.module.__name__.rpartition(".")[2])
    yield d
    problems = d.teardown()
    if problems:
        pytest.fail("\n".join(problems), pytrace=False)


def pytest_terminal_summary(terminalreporter):
    if _NOT_COLLECTED:
        terminalreporter.write_line("ycabled black-box tests not run: set BB_ENABLE=1 to run them, in a "
                                    "throwaway container (see {})".format(os.path.join(BLACKBOX_DIR, "README.md")))


def pytest_sessionfinish(session, exitstatus):
    env = _ENV.get("env")
    if env is None or env.monitor is None:
        return
    report_dir = session.config.getoption("--bb-report-dir") or os.path.join(env.workdir, "report")
    os.makedirs(report_dir, exist_ok=True)
    tr = session.config.pluginmanager.get_plugin("terminalreporter")
    if env.monitor.error is None:
        _contract_report(session, env, report_dir, tr)
    elif tr is not None:
        tr.write_line("DB contract: no report, redis MONITOR stopped recording: {!r}".format(env.monitor.error))
    if env.coverage is not None and env.coverage.own_report:
        _coverage_report(env, report_dir, tr)


def _contract_report(session, env, report_dir, tr):
    from harness import contract
    text, missing = contract.coverage_report(env.monitor.accesses(0) + env.extra_accesses)
    with open(os.path.join(report_dir, "db-contract-coverage.md"), "w") as f:
        f.write(text)
    with open(os.path.join(report_dir, "db-contract-missing.json"), "w") as f:
        json.dump([e._asdict() for e in missing], f, indent=1)
    not_required = _full_contract_not_required(session)
    if tr is not None:
        tr.write_line("DB contract: {} of {} entries exercised ({}); report in {}".format(
            len(contract.CONTRACT) - len(missing), len(contract.CONTRACT),
            "not all required: " + not_required if not_required else "all required", report_dir))
    if missing and not not_required:
        if tr is not None:
            for e in missing:
                tr.write_line("  not exercised: {} {} {} {}".format(e.db, e.table, e.kind, e.field or ""))
            tr.write_line("  The run fails: a test should exercise each entry, or the entry is no longer needed. "
                          "--bb-no-require-full-contract skips this check.")
        session.exitstatus = 1


def _coverage_report(env, report_dir, tr):
    data = env.coverage.data_file
    cmd = [sys.executable, "-m", "coverage"]
    subprocess.call(cmd + ["combine", "--data-file", data, os.path.dirname(data)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    with open(os.path.join(report_dir, "ycabled-coverage.txt"), "w") as f:
        subprocess.call(cmd + ["report", "-m", "--data-file", data], stdout=f, stderr=subprocess.STDOUT)
    subprocess.call(cmd + ["html", "--data-file", data, "-d", os.path.join(report_dir, "ycabled-coverage-html")],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.call(cmd + ["json", "--data-file", data, "-o", os.path.join(report_dir, "ycabled-coverage.json")],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if tr is not None:
        with open(os.path.join(report_dir, "ycabled-coverage.txt")) as f:
            for line in f.read().splitlines()[-8:]:
                tr.write_line(line)
