from __future__ import annotations

from dataclasses import dataclass


@dataclass
class AdaptiveSyncSchedule:
    """Back off quiet DAS polling and return to the active interval on activity."""

    active_seconds: int = 10 * 60
    quiet_seconds: int = 30 * 60
    idle_seconds: int = 60 * 60
    quiet_after: int = 3
    idle_after: int = 6
    empty_runs: int = 0
    interval_seconds: int = 10 * 60

    def __post_init__(self) -> None:
        if not 0 < self.active_seconds <= self.quiet_seconds <= self.idle_seconds:
            raise ValueError("sync intervals must be positive and ordered")
        if not 0 < self.quiet_after < self.idle_after:
            raise ValueError("sync idle thresholds must be positive and ordered")
        self.interval_seconds = self.active_seconds

    def reset(self) -> bool:
        changed = self.interval_seconds != self.active_seconds or self.empty_runs != 0
        self.empty_runs = 0
        self.interval_seconds = self.active_seconds
        return changed

    def observe(self, changed: bool) -> int:
        if changed:
            self.reset()
            return self.interval_seconds
        self.empty_runs += 1
        if self.empty_runs >= self.idle_after:
            self.interval_seconds = self.idle_seconds
        elif self.empty_runs >= self.quiet_after:
            self.interval_seconds = self.quiet_seconds
        else:
            self.interval_seconds = self.active_seconds
        return self.interval_seconds

