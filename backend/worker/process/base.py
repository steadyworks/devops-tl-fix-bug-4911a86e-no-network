import logging
import multiprocessing as mp
import threading
import time
from abc import ABC, abstractmethod
from multiprocessing.connection import Connection

from backend.logging_utils import configure_logging_env

SEND_HEARTBEAT_EVERY_SECS = 1


class WorkerProcess(mp.Process, ABC):
    def __init__(self, heartbeat_connection: Connection, name: str = "worker"):
        super().__init__()
        self.name = name
        self.heartbeat_connection = heartbeat_connection

    @abstractmethod
    def _run_impl(self) -> None: ...

    def _start_heartbeat_thread(self) -> None:
        def send_heartbeat() -> None:
            while True:
                try:
                    self.heartbeat_connection.send("ping")
                    time.sleep(SEND_HEARTBEAT_EVERY_SECS)
                except Exception:
                    break  # parent closed pipe

        threading.Thread(target=send_heartbeat, daemon=True).start()

    def run(self) -> None:
        try:
            # Run in child process
            configure_logging_env()
            logging.info(f"[{self.name}] Worker started with PID {self.pid}")

            # Establish heartbeat
            self._start_heartbeat_thread()

            # Start main job defined in children classes
            self._run_impl()
        except Exception as e:
            logging.exception(f"[{self.name}] Worker crashed: {e}")
