import errno
import logging
import os


log = logging.getLogger(__name__)


def cleanup_completed(state):
    """Only confirmed deliveries may lose their cached audio."""
    for job in state.pending_cleanup():
        key = job["key"]
        directory = state.directory / "articles" / key
        try:
            for filename in ("audio.mp3", "audio.mp3.part"):
                (directory / filename).unlink(missing_ok=True)
            if directory.exists():
                descriptor = os.open(directory, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                try:
                    directory.rmdir()
                except OSError as error:
                    if error.errno not in {errno.ENOENT, errno.ENOTEMPTY}:
                        raise
        except OSError as error:
            state.update(key, error=f"Delivered; local audio cleanup failed: {error}")
            log.warning("Job %s is delivered; local audio cleanup will retry: %s", key, error)
            continue
        state.update(key, cleanup_pending=0, error=None)
