"""State machine utilities for tennis court polling logic."""

from __future__ import annotations

import enum
import hashlib
import json
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


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


class CourtPollingStage(enum.Enum):
    """Execution stage for command scheduling (normal vs. OFF slow polling)."""

    NORMAL = "normal"
    OFF = "off"


def _default_offset(kort_id: str) -> float:
    fingerprint = hashlib.sha1(str(kort_id).encode("utf-8")).hexdigest()
    return (int(fingerprint[:8], 16) % 700) / 100.0  # 0.00-6.99 s


def _fingerprint(data: Dict[str, object]) -> str:
    try:
        return json.dumps(data, sort_keys=True, default=str)
    except TypeError:
        normalized = {k: str(v) for k, v in data.items()}
        return json.dumps(normalized, sort_keys=True)


def _normalize_int(value: object) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if not text or text.lower() in {"na", "null", "none", "-"}:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def _point_category(value: object) -> str:
    if value is None:
        return "unknown"
    if isinstance(value, (int, float)):
        return "positive" if value > 0 else "zero"
    text = str(value).strip().lower()
    if not text or text in {"-", "", "na", "null", "none"}:
        return "unknown"
    if text in {"0", "00", "love", "l"}:
        return "zero"
    if text.isdigit():
        return "positive" if int(text) > 0 else "zero"
    if text in {"ad", "adv", "advantage", "game", "won", "w"}:
        return "positive"
    return "positive"


def _truthy(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    return text in {
        "1",
        "true",
        "yes",
        "on",
        "tak",
        "finished",
        "finish",
        "complete",
        "completed",
        "done",
    }


@dataclass
class ScoreSnapshot:
    points_known: bool
    points_any: bool
    points_positive: bool
    points_zero: bool
    games_known: bool
    games_any: bool
    games_positive: bool
    sets_present: bool
    sets_completed: int
    total_sets: int
    sets_won: Tuple[int, int]
    tie_break_active: bool
    super_tb_active: bool


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
_NORMAL_COMMAND_SPECS: Dict[CourtPhase, List[CommandSpec]] = {
    CourtPhase.IDLE_NAMES: [
        CommandSpec("GetNamePlayerA", interval=3.0, offset=0.0, initial_delay=0.0),
        CommandSpec("GetNamePlayerB", interval=3.0, offset=1.5, initial_delay=0.0),
        CommandSpec("ProbeAvailability", interval=60.0, offset=0.0, initial_delay=0.0),
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
        CommandSpec("GetPoints", interval=1.0, initial_delay=1.0),
    ],
    CourtPhase.LIVE_SETS: [
        CommandSpec("GetSets", interval=8.0, initial_delay=8.0),
        CommandSpec("ProbeGames", interval=6.0, offset=3.0, initial_delay=6.0),
    ],
    CourtPhase.SUPER_TB10: [
        CommandSpec("GetPoints", interval=1.0, initial_delay=1.0),
    ],
    CourtPhase.FINISHED: [
        CommandSpec("GetNamePlayerA", interval=30.0, initial_delay=1.0),
        CommandSpec("GetNamePlayerB", interval=30.0, offset=15.0, initial_delay=1.0),
    ],
}

_OFF_STAGE_COMMAND_SPECS: Dict[Optional[CourtPhase], List[CommandSpec]] = {
    None: [
        CommandSpec(
            "OffProbeAvailability",
            interval=60.0,
            offset=0.0,
            initial_delay=0.0,
        ),
    ]
}

_COMMAND_SPECS: Dict[
    CourtPollingStage, Dict[Optional[CourtPhase], List[CommandSpec]]
] = {
    CourtPollingStage.NORMAL: _NORMAL_COMMAND_SPECS,
    CourtPollingStage.OFF: _OFF_STAGE_COMMAND_SPECS,
}


@dataclass
class CourtState:
    kort_id: str
    phase: CourtPhase = CourtPhase.IDLE_NAMES
    stage: CourtPollingStage = CourtPollingStage.NORMAL
    last_polled: float = 0.0
    phase_started_at: float = 0.0
    stage_started_at: float = 0.0
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
    last_score_snapshot: Optional[ScoreSnapshot] = None
    points_positive_streak: int = 0
    points_absent_streak: int = 0
    command_error_streak: int = 0
    command_error_streak_by_spec: Dict[str, int] = field(default_factory=dict)
    paused_until: Optional[float] = None
    availability_paused_until: Optional[float] = None

    # --- pola z gałęzi "main" (rotacja A/B itp.)
    tick_counter: int = 0
    next_player_by_spec: Dict[str, str] = field(default_factory=dict)
    pending_players_by_spec: Dict[str, List[str]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.phase_offset is None:
            self.phase_offset = _default_offset(self.kort_id)
        self._configure_phase_commands(now=0.0)

    def _configure_phase_commands(self, now: float) -> None:
        stage_specs = _COMMAND_SPECS.get(self.stage, {})
        specs = stage_specs.get(self.phase)
        if specs is None:
            specs = stage_specs.get(None, [])
        self.command_schedules = {spec.name: CommandSchedule(spec=spec) for spec in specs}
        if self.stage is CourtPollingStage.OFF:
            base_time = now
        else:
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

    def set_stage(self, stage: CourtPollingStage, now: float) -> None:
        if stage is self.stage:
            return
        self.stage = stage
        self.stage_started_at = now
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

    def update_name_stability(self, signature: str, *, required_ticks: int) -> None:
        parts = [part.strip() for part in signature.split("|")] if signature else []
        has_all_names = parts and all(parts)

        if not has_all_names:
            self.name_stability = 0
            self.last_name_signature = signature
            return

        if signature == self.last_name_signature:
            cap = required_ticks if required_ticks > 0 else 10_000
            self.name_stability = min(self.name_stability + 1, cap)
        else:
            self.name_stability = 1
        self.last_name_signature = signature

    def compute_score_snapshot(self, snapshot: Dict[str, object]) -> ScoreSnapshot:
        raw = snapshot.get("raw")
        if not isinstance(raw, dict):
            raw = {}
        players = snapshot.get("players")
        if not isinstance(players, dict):
            players = {}

        point_values = []
        for suffix in ("A", "B"):
            player = players.get(suffix) or {}
            value = player.get("points")
            if value in {None, "", "-"}:
                value = raw.get(f"PointsPlayer{suffix}")
            point_values.append(value)
        point_categories = [_point_category(value) for value in point_values]
        points_known = all(category != "unknown" for category in point_categories)
        points_any = any(category != "unknown" for category in point_categories)
        points_positive = any(category == "positive" for category in point_categories)
        points_zero = points_known and not points_positive

        game_values = []
        for suffix in ("A", "B"):
            value = None
            for key in (
                f"CurrentGamePlayer{suffix}",
                f"GamesPlayer{suffix}",
                f"GamePlayer{suffix}",
            ):
                if key in raw:
                    value = raw.get(key)
                    break
            game_values.append(_normalize_int(value))
        games_known = all(value is not None for value in game_values)
        games_any = any(value is not None for value in game_values)
        games_positive = any((value or 0) > 0 for value in game_values)

        set_scores: List[Tuple[Optional[int], Optional[int]]] = []
        for index in range(1, 8):
            key_a = f"Set{index}PlayerA"
            key_b = f"Set{index}PlayerB"
            value_a = _normalize_int(raw.get(key_a))
            value_b = _normalize_int(raw.get(key_b))
            if value_a is None and value_b is None:
                continue
            set_scores.append((value_a, value_b))
        total_sets = len(set_scores)
        sets_completed = sum(1 for a, b in set_scores if a is not None and b is not None)
        sets_won_a = sum(1 for a, b in set_scores if a is not None and b is not None and a > b)
        sets_won_b = sum(1 for a, b in set_scores if a is not None and b is not None and b > a)
        sets_present = total_sets > 0

        raw_tie_break = any(
            _truthy(raw.get(key))
            for key in (
                "TieBreak",
                "Tiebreak",
                "MatchTieBreak",
                "SetTieBreak",
                "TieBreakInProgress",
            )
        )
        super_tb_active = any(
            _truthy(raw.get(key))
            for key in (
                "SuperTieBreak",
                "SuperTiebreak",
                "MatchSuperTieBreak",
                "SuperTieBreakInProgress",
            )
        )
        tie_break_active = bool(raw_tie_break and not super_tb_active)

        snapshot = ScoreSnapshot(
            points_known=points_known,
            points_any=points_any,
            points_positive=points_positive,
            points_zero=points_zero,
            games_known=games_known,
            games_any=games_any,
            games_positive=games_positive,
            sets_present=sets_present,
            sets_completed=sets_completed,
            total_sets=total_sets,
            sets_won=(sets_won_a, sets_won_b),
            tie_break_active=tie_break_active,
            super_tb_active=super_tb_active,
        )
        self.last_score_snapshot = snapshot
        return snapshot

    def update_score_stability(self, score: ScoreSnapshot) -> None:
        if score.points_positive:
            self.points_positive_streak = min(self.points_positive_streak + 1, 10_000)
        else:
            self.points_positive_streak = 0

        if score.sets_present and not score.points_any:
            self.points_absent_streak = min(self.points_absent_streak + 1, 10_000)
        else:
            self.points_absent_streak = 0

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
        name_due = [
            schedule
            for schedule in due
            if schedule.spec.name in {"GetNamePlayerA", "GetNamePlayerB"}
        ]
        if len(name_due) >= 2:
            preferred: Optional[str] = None
            if self.last_command in {"GetNamePlayerA", "GetNamePlayerB"}:
                preferred = (
                    "GetNamePlayerB"
                    if self.last_command == "GetNamePlayerA"
                    else "GetNamePlayerA"
                )
            if preferred:
                for candidate in name_due:
                    if candidate.spec.name == preferred:
                        selected = candidate
                        break
            else:
                selected = name_due[0]
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

    def effective_pause_until(self) -> Optional[float]:
        candidates = [self.paused_until, self.availability_paused_until]
        active = [value for value in candidates if value is not None]
        if not active:
            return None
        return max(active)

    def is_paused(self, now: float) -> bool:
        effective = self.effective_pause_until()
        if effective is None:
            return False
        return effective > now

    def clear_pause(self) -> None:
        self.paused_until = None
        self.command_error_streak = 0
        self.command_error_streak_by_spec.clear()

    def apply_availability_pause(self, now: float, duration: float) -> None:
        candidate_until = now + duration
        if (
            self.availability_paused_until is None
            or candidate_until > self.availability_paused_until
        ):
            self.availability_paused_until = candidate_until

    def clear_availability_pause(self) -> None:
        self.availability_paused_until = None


__all__ = [
    "CourtPhase",
    "CourtPollingStage",
    "CourtState",
    "CommandSpec",
    "CommandSchedule",
    "ScoreSnapshot",
]
