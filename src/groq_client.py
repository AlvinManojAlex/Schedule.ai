import json
import uuid
import re
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from groq import Groq

PROMPT_PATH = Path(__file__).parent.parent / "prompts" / "task_parse.txt"
MODEL = "llama-3.3-70b-versatile"

WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _build_date_reference(now: datetime) -> str:
    """Precompute concrete dates for every weekday phrase so the model never
    has to do calendar arithmetic (llama-3.3-70b does it unreliably). Find the
    user's exact phrase in the list and copy its date — do not reason about it."""
    today = now.date()
    today_wd = today.weekday()

    lines = [
        f"today    = {today.isoformat()} ({today.strftime('%A')})",
        f"tomorrow = {(today + timedelta(days=1)).isoformat()} ({(today + timedelta(days=1)).strftime('%A')})",
        "",
    ]
    for i, name in enumerate(WEEKDAY_NAMES):
        this_date = today + timedelta(days=(i - today_wd) % 7)
        lines.append(f'this {name:<9} = {this_date.isoformat()}')
    lines.append("")
    for i, name in enumerate(WEEKDAY_NAMES):
        next_date = today + timedelta(days=(i - today_wd) % 7 + 7)
        lines.append(f'next {name:<9} = {next_date.isoformat()}')
    return "\n".join(lines)


def _load_prompt(current_datetime: str, user_timezone: str, date_reference: str) -> str:
    template = PROMPT_PATH.read_text()

    return (
        template
        .replace("{current_datetime}", current_datetime)
        .replace("{user_timezone}", user_timezone)
        .replace("{date_reference}", date_reference)
    )

def parse_task(raw_input: str, timezone: str) -> dict:
    """
        Send raw natural language to Groq and return a validated task dict.
        Raises ValueError if Groq returns unparseable output
    """

    client = Groq()

    tz = ZoneInfo(timezone)
    now_dt = datetime.now(tz)
    now = now_dt.strftime("%Y-%m-%dT%H:%M:%S")
    system_prompt = _load_prompt(
        current_datetime = now,
        user_timezone = timezone,
        date_reference = _build_date_reference(now_dt),
    )

    # sending the prompt to groq
    response = client.chat.completions.create(
        model = MODEL,
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": raw_input}
        ],
        temperature = 0.2, # low temperature => deterministic extraction => low creativity
        max_tokens = 512,
    )

    raw_json = response.choices[0].message.content.strip()

    # strip unneccesary markdown fences if the model wraps output
    raw_json = re.sub(r"^```(?:json)?\s*", "", raw_json)
    raw_json = re.sub(r"\s*```$", "", raw_json)

    try:
        parsed = json.loads(raw_json)
    except json.JSONDecodeError as e:
        raise ValueError(f"Groq return non-JSON output: {raw_json}") from e
    
    return _build_task_record(raw_input, parsed)

def _build_task_record(raw_input: str, groq_output: dict) -> dict:
    """
        Merge groq's parsed fields with system generated metadata to produce
        a complete task record ready to write to tasks.json
    """

    return {
        "id": uuid.uuid4().hex[:6],
        "created_at": datetime.now().isoformat(timespec = "seconds"),
        "raw_input": raw_input,

        # groq extracted fields
        "title": groq_output.get("title"),
        "duration_minutes": groq_output.get("duration_minutes"),
        "deadline": groq_output.get("deadline"),
        "deadline_confidence": groq_output.get("deadline_confidence", "none"),
        "priority": groq_output.get("priority", "medium"),
        "preferred_time_of_day": groq_output.get("preferred_time_of_day"),
        "explicit_start_time": groq_output.get("explicit_start_time"),
        "not_before": groq_output.get("not_before"),

        # scheduler.py will fill this later
        "status": "pending",
        "scheduled_start": None,
        "gcal_event_id": None,

        # Debug info
        "groq_reasoning": groq_output.get("groq_reasoning", "")
    }