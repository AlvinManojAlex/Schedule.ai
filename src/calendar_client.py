"""
    Wrapper around Google Calendar API

    - Load OAuth2 credentials (auth flow handled in auth/google_auth.py)
    - Fetch busy/free events in a time window
    - Insert a scheduled task as a Calendar event
    - Delete or update an event when a task is rescheduled
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from auth.google_auth import get_credentials

CALENDAR_ID = "primary"
# this tag added to every event description for debugging/identification
SCHEDULE_AI_TAG = "schedule-ai"

# client
def get_calendar_service():
    """
        Build and return an authenticated google calendar service object
    """

    creds = get_credentials()
    return build("calendar", "v3", credentials = creds, cache_discovery = False)

# fetching events
def get_events(days_ahead: int, timezone: str) -> list[dict]:
    """
        Return all calendar events in the next `days_ahead` days as plain data

        each dict contains:
        - id
        - title
        - start
        - end
        - all_day (bool, true for all day events)
    """

    service = get_calendar_service()
    tz = ZoneInfo(timezone)
    now = datetime.now(tz)
    
    time_min = now.isoformat()
    time_max = (now + timedelta(days = days_ahead)).isoformat()

    try:
        result = service.events.list(
            calendarId = CALENDAR_ID,
            timeMin = time_min,
            timeMax = time_max,
            singleEvents = True,
            orderBy = "startTime",
            maxResults = 500,
        ).execute()
    
    except HttpError as e:
        raise RuntimeError(f"Google Calendar API error fetching events: {e}") from e
    
    events = []
    for item in result.get("items", []):
        parsed = _parse_event(item, tz)
        if parsed:
            events.append(parsed)
        
    return events

def _parse_event(item: dict, tz: ZoneInfo) -> dict | None:
    """
        Convert a raw GCal event item into a clean flat dict
    """

    start_raw = item.get("start", {})
    end_raw = item.get("end", {})

    all_day = "date" in start_raw and "dateTime" not in start_raw
    
    if all_day:
        # treat all-day events as blocking the whole day
        day_str = start_raw["date"]
        day = datetime.fromisoformat(day_str)
        start_iso = day.replace(hour = 0, minute = 0, tzinfo = tz).isoformat()
        end_iso = day.replace(hour = 23, minute = 59, tzinfo = tz).isoformat()
    
    else:
        start_iso = start_raw.get("dateTime")
        end_iso = end_raw.get("dateTime")

        if not start_iso or not end_iso:
            return None
        
    return {
        "id": item.get("id", ""),
        "title": item.get("title", "(no title)"),
        "start": start_iso,
        "end": end_iso,
        "all_day": all_day,
    }

# inserting a scheduled task
def insert_event(task: dict, scheduled_start: datetime, timezone: str) -> str:
    """
        Create google calendar event for the given task starting at scheduled_start
        Returns the created GCal event ID
    """

    service = get_calendar_service()

    duration = timedelta(minutes = task["duration_minutes"])
    scheduled_end = scheduled_start + duration

    description_lines = [
        f"Scheduled by schedule.ai",
        f"Priority: {task.get('priority', 'medium')}",
        f"tag:{SCHEDULE_AI_TAG}"
    ]

    if task.get("groq_reasoning"):
        description_lines.append(f"\nReasoning: {task['groq_reasoning']}")

    event_body = {
        "summary": task["title"],
        "description": "\n".join(description_lines),
        "start": {
            "dateTime": scheduled_start.isoformat,
            "timeZone": timezone
        },
        "end": {
            "dateTime": scheduled_end.isoformat,
            "timeZone": timezone
        },
        "reminders": {
            "useDefault": False,
            "overrides": [
                {"method": "popup", "minutes": 10},
            ],
        },
    }

    try:
        created = service.events().insert(
            calendarId = CALENDAR_ID,
            body = event_body,
        ).execute()

    except HttpError as e:
        raise RuntimeError(f"Google Calendar API error inserting event: {e}") from e
    
    return created['id']

# function to delete an event
def delete_event(gcal_event_id: str) -> None:
    """
        Remove a previously scheduled Schedule.ai event from the calendar
    """
    service = get_calendar_service()
    
    try:
        service.events().delete(
            calendarId=CALENDAR_ID,
            eventId=gcal_event_id,
        ).execute()
    
    except HttpError as e:
        if e.resp.status == 404:
            # Event already deleted or never existed — treat as success
            return
        raise RuntimeError(f"Google Calendar API error deleting event: {e}") from e
 
 
# function to update event
def update_event_time(gcal_event_id: str, new_start: datetime, duration_minutes: int, timezone: str) -> None:
    """
        Move an existing event to a new start time without touching other fields.
        Used by the reschedule command.
    """
    service = get_calendar_service()
    new_end = new_start + timedelta(minutes=duration_minutes)
 
    try:
        # Patch only the time fields — preserves title, description, reminders
        service.events().patch(
            calendarId=CALENDAR_ID,
            eventId=gcal_event_id,
            body={
                "start": {"dateTime": new_start.isoformat(), "timeZone": timezone},
                "end":   {"dateTime": new_end.isoformat(),   "timeZone": timezone},
            },
        ).execute()
    
    except HttpError as e:
        raise RuntimeError(f"Google Calendar API error updating event: {e}") from e