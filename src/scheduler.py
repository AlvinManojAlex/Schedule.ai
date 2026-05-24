"""
Core slot finding logic
Gets data in from the json and returns a scheduled result

Flow:
    - Build a timeline of busy intervals (blocked times + existing GCal events)
    - Carve out free slots within the search window
    - Score each free slot: preferred_time_of_day match, proximity to current time, deadline pressure
    - Return the best slot or a failure with a reason
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

# Constants
# Buffer minutes for how much time gap between two scheduled slots
# Search days for how ahead must the scheduler look for a slot
BUFFER_MINUTES = 15
SEARCH_DAYS = 14
DAY_ABBREVS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

TIME_OF_DAY_WINDOWS: dict[str, tuple[time, time]] = {
    "morning": (time(7, 0), time(12, 0)),
    "afternoon": (time(12, 0), time(17, 0)),
    "evening": (time(17, 0), time(22,0)),
    "any": (time(0, 0), time(23, 59)),
}

PRIORITY_SEARCH_DAYS = {
    "high": 3,
    "medium" : 7,
    "low": SEARCH_DAYS,
}

# Data types
@dataclass(order=True)
class Interval:
    """
        A contiguous busy period
    """
    start: datetime
    end: datetime

    def overlaps(self, other: "Interval") -> bool:
        return self.start < other.end and other.start < self.end
    
    def with_buffer(self) -> "Interval":
        """
            Expand interval by BUFFER_MINUTES on each side
        """
        return Interval(
            self.start - timedelta(minutes = BUFFER_MINUTES),
            self.end + timedelta(minutes = BUFFER_MINUTES),
        )
    
@dataclass
class FreeSlot:
    start: datetime
    end: datetime
    duration: timedelta = field(init = False)

    def __post_init__(self):
        self.duration = self.end - self.start


@dataclass
class ScheduleResult:
    success: bool
    slot: FreeSlot | None = None
    failure_reason: Literal[
        "no_free_slot",
        "deadline_too_tight",
        "no_slot_before_deadline",
    ] | None = None
    reasoning: str = ""

# entry point for the scheduler
def find_slot(task: dict, gcal_events: list[dict], blocked_path: Path, timezone: str) -> ScheduleResult:
    """
        function to find the best possible free slot

        task: task details
        gcal_events: Event list from google calendar
        blocked_path: file path to blocked.json
        timezone: IANA timezone string
    """
    tz = ZoneInfo(timezone)
    now = datetime.now(tz)

    duration = timedelta(minutes = task["duration_minutes"])
    deadline = _parse_dt(task["deadline"], tz) if task["deadline"] else None
    priority = task.get("priority", "medium")
    pref_tod = task.get("preferred_time_of_day", "any")

    # Determine search window
    search_end = now + timedelta(days = PRIORITY_SEARCH_DAYS[priority])

    if deadline:
        # never schedule past the deadline
        search_end = min(search_end, deadline - duration)

        if search_end <= now:
            return ScheduleResult(
                success = False,
                failure_reason = "deadline_too_tight",
                reasoning=(
                    f"Deadline is {deadline.strftime('%a %b %d at %H:%M')} but task needs {task['duration_minutes']} min."
                ),
            )
        
    # build busy timeline
    busy = _build_busy_intervals(gcal_events, blocked_path, now, search_end, tz)

    # get free slots
    free_slots = _get_free_slots(now, search_end, busy, duration)

    if not free_slots:
        return ScheduleResult(
            success = False,
            failure_reason = "no_free_slot" if not deadline else "no_slot_before_deadline",
            reasoning = (
                "No free slot large enough for this task was found in the search window"
            ),
        )
    
    # score and pick best slot
    best = _pick_best_slot(free_slots, pref_tod, deadline, now, tz)

    reasoning = _build_reasoning(best, pref_tod, deadline, free_slots)

    return ScheduleResult(success = True, slot = best, reasoning = reasoning)

# build busy timeline function
def _build_busy_intervals(gcal_events: list[dict], blocked_path: Path, window_start: datetime, window_end: datetime, tz: ZoneInfo) -> list[Interval]:
    """
        Merge google calendar events and recurring blocked times into a sorted, merged interval list
    """
    intervals: list[Interval] = []

    # google calendar events
    for ev in gcal_events:
        start = _parse_dt(ev["start"], tz)
        end = _parse_dt(ev["end"], tz)
        if start < window_end and end > window_start:
            intervals.append(Interval(start, end).with_buffer())

    # recurring blocked times
    blocked = _load_blocked(blocked_path)
    current = window_start.date()
    end_date = window_end.date() + timedelta(days = 1)

    while current <= end_date:
        day_abbrev = DAY_ABBREVS[current.weekday()]
        for block in blocked:
            if day_abbrev not in block["days"]:
                continue

            b_start, b_end = _block_interval_for_date(block, current, tz)
            if b_start < window_end and b_end > window_start:
                # no buffer on blocked times - already full exclusions
                intervals.append(Interval(b_start, b_end))
        
        current += timedelta(days=1)

    return _merge_intervals(sorted(intervals))

def _block_interval_for_date(block: dict, day: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """
        Convert a blocked.json entry into concrete datetimes for a given date.
        Handles overnight blocks (sleep)
    """
    sh, sm = map(int, block["start_time"].split(":"))
    eh, em = map(int, block["end_time"].split(":"))

    start = datetime(day.year, day.month, day.day, sh, sm, tzinfo = tz)
    end = datetime(day.year, day.month, day.day, eh, em, tzinfo = tz)

    # if overnight -> push end to next day
    if end <= start:
        end += timedelta(days = 1)

    return start, end

def _merge_intervals(intervals: list[Interval]) -> list[Interval]:
    """
        Merge overlapping intervals into a minimal sorted list
    """

    if not intervals:
        return []
    
    merged = [intervals[0]]

    for iv in intervals[1:]:
        if iv.start <= merged[-1].end:
            merged[-1] = Interval(merged[-1].start, max(merged[-1].end, iv.end))
        else:
            merged.append(iv)
    
    return merged

# function for extracting free slots
def _get_free_slots(window_start: datetime, window_end: datetime, busy: list[Interval], min_duration: timedelta) -> list[FreeSlot]:
    """
        Return all contiguous free gaps that fit min_duration
    """

    free: list[FreeSlot] = []
    cursor = window_start

    for interval in busy:
        if interval.start > cursor:
            gap_end = interval.start

            if gap_end - cursor >= min_duration:
                free.append(FreeSlot(cursor, gap_end))
        
        cursor = max(cursor, interval.end)

    # trailing gap after last busy block
    if window_end - cursor >= min_duration:
        free.append(FreeSlot(cursor, window_end))

    return free

# function to score free slots and pick the best one
def _pick_best_slot(slots: list[FreeSlot], pref_tod: str, deadline: datetime | None, now: datetime, tz: ZoneInfo) -> FreeSlot:
    """
        Lower score is better slot
        - time of day match: 0 for match, 100 for no match
        - deadline pressure: 0-50, schedule early so reward earlier slots
        - proximity to now: 0-30, sooner better but not at cost of preference
    """

    pref_window = TIME_OF_DAY_WINDOWS[pref_tod]
    preferred_slots = [s for s in slots if _slot_in_window(s, pref_window, tz)]

    # if preferred slot exist, then only score those. Otherwise fallback to all slots
    candidates = preferred_slots if preferred_slots else slots
    fallback_used = not preferred_slots and pref_tod != 'any'

    total_window = (slots[-1].start - now).total_seconds() or 1

    def score(slot: FreeSlot) -> float:
        proximity = (slot.start - now).total_seconds() / total_window * 30

        deadline_score = 0.0
        if deadline:
            time_to_deadline = (deadline - now).total_seconds()
            total_time = (deadline - now).total_seconds() or 1
            deadline_score = (1 - time_to_deadline / total_time) * 50
        return proximity + deadline_score
    
    best = min(candidates, key = score)

    # tag whether fallback was used (for reasoning purpose)
    best._fallback_used = fallback_used
    best._preferred_slots_count = len(preferred_slots)

    return best

def _slot_in_window(slot: FreeSlot, window: tuple[time, time], tz: ZoneInfo) -> bool:
    """
        True if the slot's start time falls within the time-of-day window
    """
    local_start = slot.start.astimezone(tz).time()
    return window[0] <= local_start < window[1]

# function to build human-readable reasoning
def _build_reasoning(slot: FreeSlot, pref_tod: str, deadline: datetime | None, all_slots: list[FreeSlot]) -> str:
    lines = []

    slot_str = slot.start.strftime("%A %b %d at %H:%M")
    lines.append(f"Scheduled for {slot_str}")

    fallback = getattr(slot, "_fallback_used", False)
    if fallback:
        lines.append(f"No '{pref_tod}' slots were available - fell back to the next best free window")

    elif pref_tod != "any":
        lines.append(f"Matched your '{pref_tod}' preference")

    if deadline:
        gap = deadline - slot.start
        hours = int(gap.total_seconds() // 3600)
        lines.append(f"{hours}h before deadline")

    lines.append(f"{len(all_slots)} total free slots considered")

    return " ".join(lines)

# utility functions that were used
def _parse_dt(iso_str: str, tz: ZoneInfo) -> datetime:
    """
        Parse ISO 8601 string: attach timezone if naive
    """
    dt = datetime.fromisoformat(iso_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo = tz)
    return dt

def _load_blocked(path: Path) -> list[dict]:
    if not path.exists():
        return []
    
    return json.loads(path.read_text())