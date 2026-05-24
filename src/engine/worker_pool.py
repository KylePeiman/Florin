"""Worker pool: runs floor(total_capital_dollars) parallel $1 arb sessions.

Each worker is an independent SimSession with a $1 budget.  When a worker's
total value (liquid + locked) reaches $10, the supervisor signals it to stop,
banks its capital, and spawns enough new $1 workers to match the new target.

Usage (via CLI):
    python -m src.cli live --simulate --pool --bankroll 10
"""
from __future__ import annotations

import signal
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from sqlalchemy.orm import Session as DbSession

PER_WORKER_CENTS: float = 100.0    # $1 starting budget per worker
RESET_THRESHOLD_CENTS: float = 1000.0  # graduate a worker at $10
SUPERVISOR_INTERVAL: float = 2.0   # seconds between supervisor ticks


@dataclass
class _WorkerState:
    session_id: int
    stop_event: threading.Event
    thread: threading.Thread
    worker_index: int


class WorkerPool:
    """Manages a pool of parallel arb workers that each start with $1."""

    def __init__(self, db_factory: Callable[[], DbSession], worker_kwargs: dict):
        self._db_factory = db_factory
        self._worker_kwargs = worker_kwargs
        self._lock = threading.Lock()
        self._capital_cents: float = 0.0
        self._workers: dict[int, _WorkerState] = {}
        self._stop_all = threading.Event()
        self._pool_id: int = int(time.time())
        self._next_worker_idx: int = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self, initial_capital_usd: float) -> None:
        """Block until Ctrl+C or stop() is called."""
        self._capital_cents = initial_capital_usd * 100.0
        signal.signal(signal.SIGINT, lambda _s, _f: self.stop())

        supervisor = threading.Thread(target=self._supervisor_loop, daemon=True, name="pool-supervisor")
        supervisor.start()

        self._stop_all.wait()
        supervisor.join(timeout=10)
        for ws in list(self._workers.values()):
            ws.thread.join(timeout=30)
        print(f"  [POOL] All workers stopped. pool_id={self._pool_id}")

    def stop(self) -> None:
        print("\n  [POOL] Stopping — signalling all workers...")
        with self._lock:
            for ws in self._workers.values():
                ws.stop_event.set()
        self._stop_all.set()

    # ------------------------------------------------------------------
    # Supervisor
    # ------------------------------------------------------------------

    def _supervisor_loop(self) -> None:
        while not self._stop_all.is_set():
            with self._lock:
                self._collect_finished()
                self._check_graduations()
                self._spawn_to_target()
            time.sleep(SUPERVISOR_INTERVAL)

    def _collect_finished(self) -> None:
        """Bank capital from workers whose threads have exited."""
        finished = [idx for idx, ws in self._workers.items() if not ws.thread.is_alive()]
        for idx in finished:
            ws = self._workers.pop(idx)
            db = self._db_factory()
            try:
                from src.storage.models import SimSession
                sim = db.get(SimSession, ws.session_id)
                if sim is not None:
                    returned = sim.total_value_cents()
                    self._capital_cents += returned
                    print(
                        f"  [POOL] worker-{ws.worker_index} (session #{ws.session_id}) "
                        f"banked ${returned/100:.4f}"
                    )
            finally:
                db.close()

    def _check_graduations(self) -> None:
        """Signal workers that have crossed the $10 graduation threshold."""
        db = self._db_factory()
        try:
            from src.storage.models import SimSession
            for ws in self._workers.values():
                if ws.stop_event.is_set():
                    continue
                sim = db.get(SimSession, ws.session_id)
                if sim is not None and sim.total_value_cents() >= RESET_THRESHOLD_CENTS:
                    print(
                        f"  [POOL] worker-{ws.worker_index} reached "
                        f"${sim.total_value_cents()/100:.4f} — graduating"
                    )
                    ws.stop_event.set()
        finally:
            db.close()

    def _spawn_to_target(self) -> None:
        """Spawn workers until count reaches floor(total_capital / $1)."""
        active = sum(1 for ws in self._workers.values() if ws.thread.is_alive())
        # Conservatively estimate total capital: bank + $1 per active worker
        total_capital = self._capital_cents + active * PER_WORKER_CENTS
        target = max(1, int(total_capital // PER_WORKER_CENTS))
        while active < target and self._capital_cents >= PER_WORKER_CENTS:
            self._spawn_one()
            active += 1

    # ------------------------------------------------------------------
    # Worker lifecycle
    # ------------------------------------------------------------------

    def _spawn_one(self) -> None:
        idx = self._next_worker_idx
        self._next_worker_idx += 1
        stop_event = threading.Event()

        logs_dir = self._worker_kwargs.get("logs_dir", "logs")
        Path(logs_dir).mkdir(exist_ok=True)
        ts_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        log_path = str(Path(logs_dir) / f"pool{self._pool_id}_w{idx}_{ts_str}.log")

        db = self._db_factory()
        try:
            from src.storage.models import SimSession
            sim = SimSession(
                initial_bankroll_cents=PER_WORKER_CENTS,
                current_bankroll_cents=PER_WORKER_CENTS,
                pool_id=self._pool_id,
                worker_index=idx,
                log_path=log_path,
            )
            db.add(sim)
            db.commit()
            session_id = sim.id
        finally:
            db.close()

        self._capital_cents -= PER_WORKER_CENTS

        t = threading.Thread(
            target=self._run_worker,
            args=(idx, stop_event, session_id),
            daemon=True,
            name=f"worker-{idx}",
        )
        t.start()
        self._workers[idx] = _WorkerState(
            session_id=session_id,
            stop_event=stop_event,
            thread=t,
            worker_index=idx,
        )
        print(f"  [POOL] Spawned worker-{idx} (session #{session_id})")

    def _run_worker(self, idx: int, stop_event: threading.Event, session_id: int) -> None:
        db = self._db_factory()
        try:
            from src.engine.live_sim import run_live_simulation
            run_live_simulation(
                db=db,
                initial_bankroll_usd=PER_WORKER_CENTS / 100.0,
                resume_session_id=session_id,
                stop_event=stop_event,
                **self._worker_kwargs,
            )
        except Exception as exc:
            print(f"  [POOL] worker-{idx} (session #{session_id}) died: {exc}")
        finally:
            db.close()
