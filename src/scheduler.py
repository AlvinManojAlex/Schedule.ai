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

    # If the user pinned an explicit start time, honour it directly
    explicit_start = _parse_dt(task["explicit_start_time"], tz) if task.get("explicit_start_time") else None
    if explicit_start is not None:
        if explicit_start < now:
            return ScheduleResult(
                success=False,
                failure_reason="no_free_slot",
                reasoning=f"Requested start time {explicit_start.strftime('%a %b %d at %H:%M')} is in the past.",
            )
        pinned_end = explicit_start + duration
        busy = _build_busy_intervals(gcal_events, blocked_path, explicit_start, pinned_end, tz)
        conflict = any(iv.start < pinned_end and iv.end > explicit_start for iv in busy)
        if conflict:
            return ScheduleResult(
                success=False,
                failure_reason="no_free_slot",
                reasoning=f"Your requested time {explicit_start.strftime('%a %b %d at %H:%M')} conflicts with an existing event or blocked period.",
            )
        return ScheduleResult(
            success=True,
            slot=FreeSlot(explicit_start, pinned_end),
            reasoning=f"Scheduled at your requested time: {explicit_start.strftime('%a %b %d at %H:%M')}",
        )

    # Determine search window
    search_end = now + timedelta(days = PRIORITY_SEARCH_DAYS[priority])

    # If deadline is end-of-tomorrow (23:59), the user said "tomorrow" — don't schedule before then
    tomorrow_start = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    if deadline and deadline.date() == tomorrow_start.date() and deadline.time() == time(23, 59):
        window_start = tomorrow_start
    else:
        window_start = now

    if deadline:
        # never schedule past the deadline
        search_end = min(search_end, deadline - duration)

        if search_end <= window_start:
            return ScheduleResult(
                success = False,
                failure_reason = "deadline_too_tight",
                reasoning=(
                    f"Deadline is {deadline.strftime('%a %b %d at %H:%M')} but task needs {task['duration_minutes']} min."
                ),
            )

    # build busy timeline (all-day events excluded — handled via date filter below)
    busy = _build_busy_intervals(gcal_events, blocked_path, window_start, search_end, tz)

    # get free slots
    free_slots = _get_free_slots(window_start, search_end, busy, duration)

    # block entire days that have all-day calendar events (holidays, PTO, etc.)
    # done as a post-filter rather than interval merging to avoid midnight boundary
    # bleed where all-day 00:00-23:59 merges with adjacent sleep blocks
    all_day_dates = {
        _parse_dt(ev["start"], tz).date()
        for ev in gcal_events
        if ev.get("all_day")
    }
    if all_day_dates:
        free_slots = [
            s for s in free_slots
            if s.start.astimezone(tz).date() not in all_day_dates
        ]


    if not free_slots:
        return ScheduleResult(
            success = False,
            failure_reason = "no_free_slot" if not deadline else "no_slot_before_deadline",
            reasoning = (
                "No free slot large enough for this task was found in the search window"
            ),
        )
    
    # score and pick best slot
    best = _pick_best_slot(free_slots, pref_tod, deadline, window_start, tz, duration, priority)

    reasoning = _build_reasoning(best, pref_tod, deadline, free_slots)

    return ScheduleResult(success = True, slot = best, reasoning = reasoning)

# build busy timeline function
def _build_busy_intervals(gcal_events: list[dict], blocked_path: Path, window_start: datetime, window_end: datetime, tz: ZoneInfo) -> list[Interval]:
    """
        Merge google calendar events and recurring blocked times into a sorted, merged interval list
    """
    intervals: list[Interval] = []

    # timed calendar events (all-day events are handled separately in find_slot)
    for ev in gcal_events:
        if ev.get("all_day"):
            continue
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
def _pick_best_slot(slots: list[FreeSlot], pref_tod: str, deadline: datetime | None, now: datetime, tz: ZoneInfo, duration: timedelta, priority: str = "medium") -> FreeSlot:
    """
        Lower score is better slot.
        Scoring: minimise distance from a priority-based target time so that
        high-priority tasks schedule ASAP and lower-priority tasks schedule
        closer to (but before) their deadline.
    """

    pref_window = TIME_OF_DAY_WINDOWS[pref_tod]

    # Build preferred candidates: slots that contain a task-sized chunk within the window.
    # Adjust each candidate's start to the effective window start so the event lands
    # at the right time (e.g. 07:00) rather than at the raw slot start (e.g. 06:30).
    preferred_slots = []
    for s in slots:
        effective_start = _effective_start_in_window(s, pref_window, tz, duration)
        if effective_start is not None:
            preferred_slots.append(FreeSlot(effective_start, s.end))

    candidates = preferred_slots if preferred_slots else slots
    fallback_used = not preferred_slots and pref_tod != 'any'

    # Target fraction of the search window at which to schedule based on priority:
    #   high   → 0.0  (ASAP)
    #   medium → 0.5  (midway to deadline / end of search window)
    #   low    → 0.75 (later, close to deadline)
    target_fractions = {"high": 0.0, "medium": 0.5, "low": 0.75}
    fraction = target_fractions.get(priority, 0.5)
    total_window_secs = (slots[-1].start - now).total_seconds() or 1
    target_time = now + timedelta(seconds=total_window_secs * fraction)

    def score(slot: FreeSlot) -> float:
        # Primary: distance from the ideal target time
        distance = abs((slot.start - target_time).total_seconds()) / total_window_secs * 100

        # Secondary: small penalty for cutting it close to the deadline
        deadline_penalty = 0.0
        if deadline:
            time_left = (deadline - slot.start).total_seconds()
            total_time = (deadline - now).total_seconds() or 1
            deadline_penalty = max(0.0, 1 - time_left / total_time) * 20
        return distance + deadline_penalty

    best = min(candidates, key=score)

    best._fallback_used = fallback_used
    best._preferred_slots_count = len(preferred_slots)

    return best


def _effective_start_in_window(slot: FreeSlot, window: tuple[time, time], tz: ZoneInfo, duration: timedelta) -> datetime | None:
    """
    Return the earliest start time at which the task fits within the preferred
    time-of-day window inside this free slot, or None if it doesn't fit.

    This handles slots that start before the window opens (e.g. a free slot
    starting at 06:30 can still host a morning task starting at 07:00).
    """
    local_start = slot.start.astimezone(tz)
    local_end   = slot.end.astimezone(tz)

    win_start = local_start.replace(hour=window[0].hour, minute=window[0].minute, second=0, microsecond=0)
    win_end   = local_start.replace(hour=window[1].hour, minute=window[1].minute, second=0, microsecond=0)

    # Effective task start: no earlier than the window opens, no earlier than the slot
    effective = max(local_start, win_start)

    # Task must finish within both the preferred window and the free slot
    if effective < win_end and effective + duration <= win_end and effective + duration <= local_end:
        return effective.astimezone(slot.start.tzinfo)
    return None

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