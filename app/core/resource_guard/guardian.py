"""ARD Guardian: resource monitoring and safe workload control.

The Guardian only ever slows down or pauses ARD's own background jobs. It
never kills other processes, never shuts the machine down and never deletes
anything.

State changes use sustained thresholds with hysteresis: load must stay above
the high mark for several samples before work is throttled, and must fall
below a lower recovery mark for several samples before it resumes. This stops
the queue from flapping.
"""

from __future__ import annotations

import os
import threading
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psutil


@dataclass(frozen=True)
class Profile:
    name: str
    description: str
    cpu_high: float  # percent
    cpu_recover: float
    mem_high: float  # percent of RAM in use
    mem_recover: float
    mem_critical: float
    disk_min_free_mb: int
    sustain_samples: int
    max_workers: int


def _cpu_count() -> int:
    return os.cpu_count() or 2


def builtin_profiles() -> dict[str, Profile]:
    cpus = _cpu_count()
    return {
        "eco": Profile("eco", "Stay out of the way. One job at a time, pause early.", 60, 40, 75, 65, 88, 2048, 3, 1),
        "balanced": Profile(
            "balanced", "Sensible default for everyday use.", 80, 60, 85, 75, 93, 1024, 4, max(1, min(2, cpus // 2))
        ),
        "performance": Profile(
            "performance", "Use more of the machine. Pause only when memory is nearly full.", 95, 85, 92, 85, 97, 512, 6,
            max(1, min(4, cpus - 1)),
        ),
    }


@dataclass
class Sample:
    timestamp: str
    cpu_percent: float
    mem_percent: float
    mem_available_mb: int
    disk_free_mb: int
    process_rss_mb: int
    process_cpu_percent: float
    temperature_c: float | None = None


@dataclass
class Intervention:
    timestamp: str
    from_state: str
    to_state: str
    explanation: str


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _temperature() -> float | None:
    try:
        sensors = psutil.sensors_temperatures()  # not available on every platform
    except (AttributeError, OSError):
        return None
    readings = [t.current for entries in sensors.values() for t in entries if t.current]
    return round(max(readings), 1) if readings else None


class Guardian:
    """States: ``normal`` -> ``throttled`` -> ``paused`` and back."""

    def __init__(self, data_dir: Path, profile: str = "balanced", interval: float = 2.0,
                 custom: dict[str, Any] | None = None) -> None:
        self._data_dir = data_dir
        self._interval = interval
        self._profiles = builtin_profiles()
        self._lock = threading.RLock()
        self._state = "normal"
        self._over = 0
        self._under = 0
        self._samples: deque[Sample] = deque(maxlen=150)
        self._interventions: deque[Intervention] = deque(maxlen=50)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._process = psutil.Process()
        self._profile = self._profiles["balanced"]
        self.set_profile(profile, custom)

    # ---- profiles ---------------------------------------------------------
    def set_profile(self, name: str, custom: dict[str, Any] | None = None) -> Profile:
        with self._lock:
            if name == "custom":
                base = asdict(self._profiles["balanced"])
                for key, value in (custom or {}).items():
                    if key in base and key not in ("name", "description"):
                        base[key] = type(base[key])(value)
                base["name"] = "custom"
                base["description"] = "Your own limits."
                profile = Profile(**base)
                self._validate(profile)
                self._profiles["custom"] = profile
            elif name not in self._profiles:
                raise ValueError(f"Unknown Guardian profile: {name}")
            self._profile = self._profiles[name]
            self._over = self._under = 0
            return self._profile

    @staticmethod
    def _validate(p: Profile) -> None:
        if not (0 < p.cpu_recover < p.cpu_high <= 100):
            raise ValueError("CPU recovery threshold must be below the high threshold.")
        if not (0 < p.mem_recover < p.mem_high < p.mem_critical <= 100):
            raise ValueError("Memory thresholds must satisfy recover < high < critical.")
        if p.max_workers < 1 or p.max_workers > 16 or p.sustain_samples < 1 or p.disk_min_free_mb < 0:
            raise ValueError("Worker count, sustain samples or disk limit is out of range.")

    @property
    def profile(self) -> Profile:
        return self._profile

    # ---- sampling ---------------------------------------------------------
    def measure(self) -> Sample:
        memory = psutil.virtual_memory()
        try:
            disk_free = psutil.disk_usage(str(self._data_dir)).free
        except OSError:
            disk_free = 0
        with self._process.oneshot():
            rss = self._process.memory_info().rss
            process_cpu = self._process.cpu_percent(interval=None)
        return Sample(
            timestamp=_now(),
            cpu_percent=psutil.cpu_percent(interval=None),
            mem_percent=memory.percent,
            mem_available_mb=int(memory.available / 1_048_576),
            disk_free_mb=int(disk_free / 1_048_576),
            process_rss_mb=int(rss / 1_048_576),
            process_cpu_percent=round(process_cpu, 1),
            temperature_c=_temperature(),
        )

    def observe(self, sample: Sample) -> None:
        """Feed one sample through the state machine. Separate from ``measure`` so it can be tested."""
        with self._lock:
            self._samples.append(sample)
            p = self._profile

            critical = []
            if sample.mem_percent >= p.mem_critical:
                critical.append(f"memory use is {sample.mem_percent:.0f}% (limit {p.mem_critical:.0f}%)")
            if sample.disk_free_mb < p.disk_min_free_mb:
                critical.append(f"only {sample.disk_free_mb} MB of disk space is free (minimum {p.disk_min_free_mb} MB)")

            high = []
            if sample.cpu_percent >= p.cpu_high:
                high.append(f"CPU use is {sample.cpu_percent:.0f}% (limit {p.cpu_high:.0f}%)")
            if sample.mem_percent >= p.mem_high:
                high.append(f"memory use is {sample.mem_percent:.0f}% (limit {p.mem_high:.0f}%)")

            recovered = (
                sample.cpu_percent < p.cpu_recover
                and sample.mem_percent < p.mem_recover
                and sample.disk_free_mb >= p.disk_min_free_mb
            )

            if critical:
                self._over += 1
                self._under = 0
                if self._state != "paused" and self._over >= p.sustain_samples:
                    self._transition("paused", "Background work is paused because " + " and ".join(critical) + ".")
            elif high:
                self._over += 1
                self._under = 0
                if self._state == "normal" and self._over >= p.sustain_samples:
                    self._transition(
                        "throttled",
                        "Background work is limited to one job at a time because " + " and ".join(high) + ".",
                    )
            elif recovered:
                self._under += 1
                self._over = 0
                if self._state != "normal" and self._under >= p.sustain_samples:
                    self._transition("normal", "Load has stayed below the recovery thresholds, so normal work resumed.")
            else:
                # Between the recovery and high marks: hold the current state.
                self._over = 0
                self._under = 0

    def _transition(self, new_state: str, explanation: str) -> None:
        self._interventions.appendleft(Intervention(_now(), self._state, new_state, explanation))
        self._state = new_state
        self._over = self._under = 0

    # ---- decisions used by the job orchestrator ---------------------------
    @property
    def state(self) -> str:
        return self._state

    def worker_limit(self) -> int:
        """How many jobs may run at once right now."""
        with self._lock:
            if self._state == "paused":
                return 1  # interactive work only; see allows_background()
            if self._state == "throttled":
                return 1
            return self._profile.max_workers

    def allows_background(self) -> bool:
        return self._state != "paused"

    # ---- lifecycle --------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        psutil.cpu_percent(interval=None)  # prime the counters
        self._process.cpu_percent(interval=None)
        self._thread = threading.Thread(target=self._run, name="ard-guardian", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self.observe(self.measure())
            except Exception:  # monitoring must never take the app down
                continue

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def snapshot(self, history: int = 60) -> dict[str, Any]:
        with self._lock:
            samples = list(self._samples)[-history:]
            return {
                "state": self._state,
                "profile": asdict(self._profile),
                "profiles": [asdict(p) for p in self._profiles.values()],
                "worker_limit": self.worker_limit(),
                "background_allowed": self.allows_background(),
                "latest": asdict(samples[-1]) if samples else asdict(self.measure()),
                "history": [asdict(s) for s in samples],
                "interventions": [asdict(i) for i in self._interventions],
                "cpu_count": _cpu_count(),
                "gpu": {"available": False, "note": "GPU monitoring is optional and not configured."},
            }
