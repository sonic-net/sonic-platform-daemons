"""Runs Python ycabled under coverage.py for the black-box suite.

Python ycabled does not exit on SIGTERM (it waits for threads that never
stop), and kills itself when a worker thread dies, so the suite cannot rely
on an exit hook alone: it sends SIGUSR1 every few seconds, and before it kills
the process it sends SIGUSR1 and waits for the marker file.
"""

import atexit
import os
import random
import signal
import socket
import sys

import coverage

# "bb" tells the suite's data files from others next to them, such as
# pytest-cov's, which combines them all into one report.
cov = coverage.Coverage(data_file=os.environ["BB_COV_DATA_FILE"],
                        data_suffix="bb.{}.{}.{:06d}".format(socket.gethostname(), os.getpid(),
                                                             random.randint(0, 999999)),
                        source=[os.environ["BB_COV_SOURCE"]], branch=os.environ["BB_COV_BRANCH"] == "1",
                        config_file=False)
cov.start()


def _save(signum, frame):
    # No stop()/start(): start() would trace only this thread and new ones.
    # save() is safe while the other threads are traced.
    cov.save()
    with open(os.path.join(os.environ["BB_COV_SAVED_DIR"], str(os.getpid())), "w"):
        pass


def _save_at_exit():
    cov.stop()
    cov.save()


signal.signal(signal.SIGUSR1, _save)
# Startup failures end in sys.exit().
atexit.register(_save_at_exit)

sys.argv = ["/usr/local/bin/ycabled"]
from ycable.ycable import main  # noqa: E402

main()
