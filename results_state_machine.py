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


STATE_INTERVALS: Dict[CourtPhase, float] = {
    CourtPhase.IDLE_NAMES: 15.0,
    CourtPhase.PRE_START: 5.0,
    CourtPhase.LIVE_POINTS: 2.0,
    CourtPhase.LIVE_GAMES: 4.0,
    CourtPhase.TIEBREAK7: 1.5,
    CourtPhase.LIVE_SETS: 8.0,
    CourtPhase.SUPER_TB10: 2.5,
    CourtPhase.FINISHED: 30.0,
}


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
class CourtState:
    kort_id: str
    phase: CourtPhase = CourtPhase.IDLE_NAMES
    last_polled: float = 0.0
    next_poll: float = 0.0
    phase_started_at: float = 0.0
    finished_name_signature: Optional[str] = None
    finished_raw_signature: Optional[str] = None
    phase_offset: float = field(default=None)
    tick_counter: int = 0
    command_index: int = 0
    next_player: str = "A"
    pending_players: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.phase_offset is None:
            self.phase_offset = _default_offset(self.kort_id)

    def schedule_next(self, now: float) -> None:
        interval = STATE_INTERVALS.get(self.phase, 10.0)
        if self.phase is CourtPhase.FINISHED:
            interval += self.phase_offset
        self.next_poll = now + interval

    def mark_polled(self, now: float) -> None:
        self.last_polled = now

    def transition(self, phase: CourtPhase, now: float) -> None:
        if phase is self.phase:
            return
        self.phase = phase
        self.phase_started_at = now
        self.command_index = 0
        self.pending_players.clear()
        self.next_player = "A"
        if phase is not CourtPhase.FINISHED:
            self.finished_name_signature = None
            self.finished_raw_signature = None

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


__all__ = ["CourtPhase", "CourtState", "STATE_INTERVALS"]

