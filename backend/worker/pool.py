import logging
import multiprocessing as mp
import signal
import sys
import threading
import time
from typing import TYPE_CHECKING, Any, Optional

from backend.logging_utils import configure_logging_env

from .process.base import WorkerProcess
from .process.io_bound_web_worker import IOBoundWebWorkerProcess

if TYPE_CHECKING:
    from multiprocessing.connection import Connection

WORKER_PROCESS_CONFIGS: list[tuple[type[WorkerProcess], int]] = [
    (IOBoundWebWorkerProcess, 1)
]


class WorkerPool:
    def __init__(self) -> None:
        self.processes: dict[type[WorkerProcess], list[Optional[mp.Process]]] = {
            worker_process_cls: [None] * num_workers
            for (worker_process_cls, num_workers) in WORKER_PROCESS_CONFIGS
        }
        self.heartbeat_conns: dict[type[WorkerProcess], list[Optional[Connection]]] = {
            worker_process_cls: [None] * num_workers
            for (worker_process_cls, num_workers) in WORKER_PROCESS_CONFIGS
        }
        self._shutdown = threading.Event()

    def start(self) -> None:
        self._start_all_workers()
        time.sleep(2)  #  Add delay to let workers initialize
        self._start_heartbeat_monitor()

    def _start_worker(self, worker_process_cls: type[WorkerProcess], i: int) -> None:
        assert (
            worker_process_cls in self.processes
            and worker_process_cls in self.heartbeat_conns
        )

        processes = self.processes[worker_process_cls]
        heartbeat_conns = self.heartbeat_conns[worker_process_cls]

        # Clean up old process
        old_proc = processes[i]
        if old_proc is not None:
            logging.info(
                f"Cleaning up old process: {worker_process_cls.__name__}-{i}"
                f", pid: {old_proc.pid}"
            )
            if old_proc.is_alive():
                old_proc.terminate()
            old_proc.join(timeout=1)
            if old_proc.is_alive():
                old_proc.kill()
                old_proc.join()

        # Clean up old heartbeat pipe
        old_conn = heartbeat_conns[i]
        if old_conn:
            try:
                old_conn.close()
            except Exception:
                pass

        if self._shutdown.is_set():
            logging.info(
                "[Pool] Shutdown in progress. "
                f"Skipping worker {worker_process_cls.__name__}-{i} restart"
            )
            return

        # Start new worker
        parent_conn, child_conn = mp.Pipe(duplex=True)
        p = worker_process_cls(
            child_conn, name=f"worker-{worker_process_cls.__name__}-{i}"
        )
        p.daemon = False
        p.start()

        if not p.is_alive():
            logging.exception(
                f"Worker {worker_process_cls.__name__}-{i} failed to start"
            )

        self.processes[worker_process_cls][i] = p
        self.heartbeat_conns[worker_process_cls][i] = parent_conn
        logging.info(
            f"[Pool] Started worker {worker_process_cls.__name__}-{i} with PID {p.pid}"
        )

    def _start_all_workers(self) -> None:
        for worker_process_cls, num_workers in WORKER_PROCESS_CONFIGS:
            for i in range(num_workers):
                self._start_worker(worker_process_cls, i)

    def _start_heartbeat_monitor(self) -> None:
        def monitor_and_restart_workers() -> None:
            while not self._shutdown.is_set():
                for worker_process_cls, num_workers in WORKER_PROCESS_CONFIGS:
                    for i in range(num_workers):
                        if self._shutdown.is_set():
                            break  # Check again inside loop to exit early

                        conn = self.heartbeat_conns[worker_process_cls][i]
                        p = self.processes[worker_process_cls][i]

                        dead = False
                        if p is None:
                            dead = True
                        elif not p.is_alive() or p.exitcode is not None:
                            dead = True
                        elif conn is None or not conn.poll(0.5):
                            dead = True

                        if dead and not self._shutdown.is_set():
                            logging.info(
                                f"[Pool] Worker {i} is dead or unresponsive. Restarting..."
                            )
                            self._start_worker(worker_process_cls, i)

                time.sleep(1)

        threading.Thread(target=monitor_and_restart_workers, daemon=True).start()

    def shutdown(self) -> None:
        self._shutdown.set()

        # 🔁 Tell each worker to shut down via the pipe
        for worker_process_cls, _num_workers in WORKER_PROCESS_CONFIGS:
            for i, (p, conn) in enumerate(
                zip(
                    self.processes[worker_process_cls],
                    self.heartbeat_conns[worker_process_cls],
                )
            ):
                if p is None:
                    continue

                try:
                    if conn is not None:
                        conn.send("shutdown")  # 🛑 Graceful shutdown message
                except Exception as e:
                    logging.warning(
                        "[Pool] Failed to send shutdown to worker"
                        f"{worker_process_cls.__name__}-{i}: {e}"
                    )

        # 🔁 Join all processes
        for worker_process_cls, _num_workers in WORKER_PROCESS_CONFIGS:
            for i, p in enumerate(self.processes[worker_process_cls]):
                if p is None:
                    continue
                logging.info(f"[Pool] Joining process: {p.pid}")
                p.join(timeout=2)
                if p.is_alive():
                    logging.warning(
                        f"[Pool] Process {worker_process_cls.__name__}-{i}, "
                        f"pid: {p.pid} did not exit in time, terminating..."
                    )
                    p.terminate()
                    p.join(timeout=2)
                    if p.is_alive():
                        p.kill()
                        p.join()


def main() -> None:
    configure_logging_env()

    pool = WorkerPool()
    pool.start()

    def handle_signal(sig: int, frame: Any) -> None:
        pool.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    # Wait for signal in main thread
    threading.Event().wait()


if __name__ == "__main__":
    main()
