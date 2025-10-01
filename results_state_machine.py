"""State machine utilities for tennis court polling logic."""

from __future__ import annotations

import enum
import hashlib
import json
from dataclasses import dataclass, field
from typing import Dict, List, Optional


class CourtPhase(enum.Enum):
    """Represents high level polling state for a given court."""

    IDLE_NAMES = "idle_names"
    PRE_START = "pre_start"
    LIVE_POINTS = "live_points"
    LIVE_GAMES = "live_games"
    TIEBREAK7 = "tiebreak7"
    LIVE_SETS = "live_sets"
    SUPER_TB10 = "super_tb10"
    FINISHED = "finished"


def _default_offset(kort_id: str) -> float:
    fingerprint = hashlib.sha1(str(kort_id).encode("utf-8")).hexdigest()
    return (int(fingerprint[:8], 16) % 700) / 100.0  # 0.00-6.99 s


def _fingerprint(data: Dict[str, object]) -> str:
    try:
        return json.dumps(data, sort_keys=True, default=str)
    except TypeError:
        normalized = {k: str(v) for k, v in data.items()}
        return json.dumps(normalized, sort_keys=True)


@dataclass
class CommandSpec:
    name: str
    interval: float
    offset: float = 0.0
    initial_delay: float = 0.0


@dataclass
class CommandSchedule:
    spec: CommandSpec
    next_due: Optional[float] = None
    last_run: Optional[float] = None

    def reset(self, base_time: float, now: float) -> None:
        target = base_time + self.spec.initial_delay + self.spec.offset
        if target < now:
            target = now
        self.next_due = target
        self.last_run = None

    def is_due(self, now: float) -> bool:
        if self.next_due is None:
            return False
        return now + 1e-9 >= self.next_due

    def mark_run(self, now: float) -> None:
        self.last_run = now
        base = self.next_due if self.next_due is not None else now
        candidate = max(now + self.spec.interval, base + self.spec.interval)
        self.next_due = candidate


# Uwaga: nazwy komend są abstrakcyjne (mapujesz je później na konkretne API: Points A/B, Games A/B itd.)
_COMMAND_SPECS: Dict[CourtPhase, List[CommandSpec]] = {
    CourtPhase.IDLE_NAMES: [
        CommandSpec("GetNamePlayerA", interval=2.0, offset=0.0, initial_delay=0.0),
        CommandSpec("GetNamePlayerB", interval=2.0, offset=1.0, initial_delay=0.0),
    ],
    CourtPhase.PRE_START: [
        CommandSpec("GetPoints", interval=2.0, initial_delay=2.0),
    ],
    CourtPhase.LIVE_POINTS: [
        CommandSpec("GetPoints", interval=1.0, initial_delay=1.0),
    ],
    CourtPhase.LIVE_GAMES: [
        CommandSpec("GetGames", interval=4.0, initial_delay=4.0),
        CommandSpec("ProbePoints", interval=6.0, initial_delay=6.0),
    ],
    CourtPhase.TIEBREAK7: [
        CommandSpec("GetPoints", interval=1.5, initial_delay=1.5),
    ],
    CourtPhase.LIVE_SETS: [
        CommandSpec("GetSets", interval=8.0, initial_delay=8.0),
        CommandSpec("ProbeGames", interval=6.0, offset=3.0, initial_delay=6.0),
    ],
    CourtPhase.SUPER_TB10: [
        CommandSpec("GetPoints", interval=1.5, initial_delay=1.5),
    ],
    CourtPhase.FINISHED: [
        CommandSpec("GetNamePlayerA", interval=30.0, initial_delay=1.0),
        CommandSpec("GetNamePlayerB", interval=30.0, offset=15.0, initial_delay=1.0),
    ],
}


@dataclass
class CourtState:
    kort_id: str
    phase: CourtPhase = CourtPhase.IDLE_NAMES
    last_polled: float = 0.0
    phase_started_at: float = 0.0
    finished_name_signature: Optional[str] = None
    finished_raw_signature: Optional[str] = None
    phase_offset: float = field(default=None)

    # --- pola z gałęzi "codex" (scheduler i stabilizacja nazwisk)
    command_schedules: Dict[str, CommandSchedule] = field(default_factory=dict)
    command_history: List[tuple[float, str]] = field(default_factory=list)
    last_command: Optional[str] = None
    last_command_at: Optional[float] = None
    last_name_signature: Optional[str] = None
    name_stability: int = 0

    # --- pola z gałęzi "main" (rotacja A/B itp.)
    tick_counter: int = 0
    next_player_by_spec: Dict[str, str] = field(default_factory=dict)
    pending_players_by_spec: Dict[str, List[str]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.phase_offset is None:
            self.phase_offset = _default_offset(self.kort_id)
        self._configure_phase_commands(now=0.0)

    def _configure_phase_commands(self, now: float) -> None:
        specs = _COMMAND_SPECS.get(self.phase, [])
        self.command_schedules = {spec.name: CommandSchedule(spec=spec) for spec in specs}
        base_time = now + self.phase_offset
        for schedule in self.command_schedules.values():
            schedule.reset(base_time, now)

    def mark_polled(self, now: float) -> None:
        self.last_polled = now
        self.tick_counter += 1

    def transition(self, phase: CourtPhase, now: float) -> None:
        if phase is self.phase:
            return
        self.phase = phase
        self.phase_started_at = now
        # reset pomocniczych liczników/rotacji
        self.pending_players_by_spec.clear()
        self.next_player_by_spec.clear()
        # wyczyść sygnatury tylko jeśli nie wchodzimy w FINISHED
        if phase is not CourtPhase.FINISHED:
            self.finished_name_signature = None
            self.finished_raw_signature = None
        # przeładowanie harmonogramu komend dla nowej fazy
        self._configure_phase_commands(now)

    def compute_name_signature(self, snapshot: Dict[str, object]) -> str:
        players = snapshot.get("players") or {}
        names = [
            str((players.get("A") or {}).get("name") or ""),
            str((players.get("B") or {}).get("name") or ""),
        ]
        return "|".join(names)

    def compute_raw_signature(self, snapshot: Dict[str, object]) -> str:
        raw = snapshot.get("raw") or {}
        if not isinstance(raw, dict):
            return str(raw)
        return _fingerprint(raw)

    def update_name_stability(self, signature: str) -> None:
        # pusta lub „nie-sensowna” sygnatura resetuje stabilność
        if not signature or not any(part.strip() for part in signature.split("|")):
            self.name_stability = 0
            self.last_name_signature = signature
            return
        if signature == self.last_name_signature:
            self.name_stability = min(self.name_stability + 1, 10_000)
        else:
            self.name_stability = 1
        self.last_name_signature = signature

    def pop_due_command(self, now: float) -> Optional[str]:
        if not self.command_schedules:
            return None
        due = [s for s in self.command_schedules.values() if s.is_due(now)]
        if not due:
            return None
        due.sort(
            key=lambda s: (
                s.next_due or float("inf"),
                s.last_run if s.last_run is not None else -float("inf"),
                s.spec.name,
            )
        )
        selected = due[0]
        selected.mark_run(now)
        self.last_command = selected.spec.name
        self.last_command_at = now
        self.command_history.append((now, selected.spec.name))
        return selected.spec.name

    def peek_next_due(self) -> Optional[float]:
        if not self.command_schedules:
            return None
        return min(schedule.next_due for schedule in self.command_schedules.values())

    def command_last_run(self, name: str) -> Optional[float]:
        schedule = self.command_schedules.get(name)
        if schedule is None:
            return None
        return schedule.last_run


__all__ = ["CourtPhase", "CourtState", "CommandSpec", "CommandSchedule"]
