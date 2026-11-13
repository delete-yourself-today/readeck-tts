from contextlib import contextmanager
import logging
import signal


class ShutdownRequested(BaseException):
    """Unwind blocking work without treating cancellation as an article failure."""


@contextmanager
def shutdown_signals():
    def stop(signum, frame):
        raise ShutdownRequested

    previous = {}
    try:
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous[signum] = signal.signal(signum, stop)
        try:
            yield
        except ShutdownRequested:
            logging.getLogger(__name__).info("Shutdown requested; unfinished work will resume on restart")
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
