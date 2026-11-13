import argparse
import json
import logging
import os
from pathlib import Path
import signal
import threading
import time

from dotenv import load_dotenv

from .cache import cleanup_completed
from .clients import Audiobookshelf, ConfigurationError, Kokoro, Readeck, RemoteError, check_services
from .config import Config, log_level_from_env
from .state import State, data_lock
from .worker import Worker


def main():
    parser = argparse.ArgumentParser(description="Narrate labeled Readeck articles into Audiobookshelf")
    parser.add_argument("command", nargs="?", choices=["run", "once", "check", "status", "retry", "reconcile", "cleanup"], default="run")
    parser.add_argument("--key", help="Job key for retry or read-only remote reconciliation")
    parser.add_argument("--max-jobs", type=int, help="Limit jobs handled by a single once cycle")
    args = parser.parse_args()
    if args.command in {"retry", "reconcile"} and not args.key:
        parser.error(f"{args.command} requires --key")
    if args.max_jobs is not None and args.max_jobs < 1:
        parser.error("--max-jobs must be positive")
    load_dotenv()
    try:
        log_level = log_level_from_env()
    except ValueError as error:
        parser.exit(1, f"Error: {error}\n")
    logging.basicConfig(level=log_level, format="%(asctime)s %(levelname)s %(message)s")
    log = logging.getLogger(__name__)
    try:
        if args.command in {"status", "retry", "cleanup"}:
            directory = Path(os.getenv("DATA_DIR", "").strip() or "data")
            if args.command == "status":
                state = State(directory)
                try:
                    print(json.dumps(state.status(), indent=2))
                finally:
                    state.db.close()
                return
            with data_lock(directory):
                state = State(directory)
                try:
                    if args.command == "cleanup":
                        cleanup_completed(state)
                        if state.pending_cleanup():
                            raise RuntimeError("Some delivered audio could not be removed; inspect status or logs")
                        return
                    job = state.get(args.key)
                    if not job or job["state"] in {"complete", "cancelled", "ignored"}:
                        raise ValueError("Choose an unfinished job key from status")
                    state.update(job["key"], state="pending", attempts=0, next_attempt=0, error=None)
                finally:
                    state.db.close()
            return
        config = Config.from_env()
        if args.command == "check":
            readeck, kokoro, abs_client = Readeck(config), Kokoro(config), Audiobookshelf(config)
            print(json.dumps(check_services(config, readeck, kokoro, abs_client), indent=2))
            return
        with data_lock(config.data_dir):
            worker = Worker(config)
            if args.command == "reconcile":
                job = worker.state.get(args.key)
                if not job or not job["snapshot"]:
                    raise ValueError("Choose a snapshotted job key from status")
                worker.abs.resolve()
                if json.loads(job["snapshot"])["destination"] != worker.destination():
                    raise ValueError("Destination changed for this job")
                found = worker.abs.find_item(job["key"])
                if found:
                    worker.complete(job["key"], found)
                print(json.dumps({"key": job["key"], "found": bool(found)}))
                return
            if args.command == "once":
                check_services(config, worker.readeck, worker.kokoro, worker.abs)
                worker.discover()
                worker.work(limit=args.max_jobs)
                return
            stop = threading.Event()
            for signum in (signal.SIGTERM, signal.SIGINT):
                signal.signal(signum, lambda *_: stop.set())
            next_poll = 0
            ready = False
            while not stop.is_set():
                try:
                    if time.monotonic() >= next_poll:
                        check_services(config, worker.readeck, worker.kokoro, worker.abs)
                        worker.discover()
                        ready = True
                        next_poll = time.monotonic() + config.poll_interval
                    if ready:
                        worker.work(limit=1)
                except ConfigurationError:
                    raise
                except RemoteError as error:
                    error.check_credentials()
                    ready = False
                    log.warning("Service unavailable; processing paused for %ss: %s", config.retry_interval, error)
                    next_poll = time.monotonic() + config.retry_interval
                except Exception as error:
                    ready = False
                    log.warning("Discovery or destination unavailable: %s", error)
                    next_poll = time.monotonic() + config.retry_interval
                stop.wait(1)
    except RemoteError as error:
        try:
            error.check_credentials()
        except ConfigurationError as problem:
            parser.exit(1, f"Error: {problem}\n")
        parser.exit(1, f"Error: {error}\n")
    except (ValueError, RuntimeError) as error:
        parser.exit(1, f"Error: {error}\n")


if __name__ == "__main__":
    main()
