"""
cli.py

Terminal interface for Schedule.ai.
Commands:
  add       "finish the lit review by Thursday, 2 hours, morning"
  blocked   add / list / remove recurring blocked times
  list      show pending and scheduled tasks
  reschedule  move a task to a new slot
  done      mark a task complete
  clear     remove old completed tasks from the local store
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import typer
from rich.console import Console
from rich.table import Table
from rich import box

from src.groq_client import parse_task
from src.scheduler import find_slot, ScheduleResult, SEARCH_DAYS
from src.calendar_client import get_events, insert_event, delete_event, update_event_time
from config import TASKS_PATH, BLOCKED_PATH, TIMEZONE

app     = Console()
err     = Console(stderr=True)
cli     = typer.Typer(
    name="schedule",
    help="Schedule.ai — AI-powered task scheduler linked to Google Calendar.",
    add_completion=False,
)


# ---------------------------------------------------------------------------
# Helpers: task store
# ---------------------------------------------------------------------------

def _load_tasks() -> list[dict]:
    if not TASKS_PATH.exists():
        return []
    text = TASKS_PATH.read_text().strip()
    return json.loads(text) if text else []


def _save_tasks(tasks: list[dict]) -> None:
    TASKS_PATH.parent.mkdir(parents=True, exist_ok=True)
    TASKS_PATH.write_text(json.dumps(tasks, indent=2))


def _find_task(tasks: list[dict], task_id: str) -> dict | None:
    return next((t for t in tasks if t["id"] == task_id), None)


def _prune_old_tasks(tasks: list[dict]) -> list[dict]:
    """Drop completed tasks older than 7 days."""
    tz  = ZoneInfo(TIMEZONE)
    now = datetime.now(tz)
    kept = []
    for t in tasks:
        if t["status"] != "done":
            kept.append(t)
            continue
        created = datetime.fromisoformat(t["created_at"])
        if created.tzinfo is None:
            created = created.replace(tzinfo=tz)
        if (now - created).days <= 7:
            kept.append(t)
    return kept


# ---------------------------------------------------------------------------
# Command: add
# ---------------------------------------------------------------------------

@cli.command()
def add(
    description: str = typer.Argument(..., help='Natural language task, e.g. "finish essay by Friday, 2 hours, morning"'),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show Groq's reasoning"),
    dry_run: bool = typer.Option(False, "--dry-run", "-d", help="Find a slot but don't create the Calendar event"),
):
    """Parse a task with Groq and schedule it on Google Calendar."""

    # 1. Parse with Groq
    app.print("\n[bold cyan]⟳[/bold cyan]  Parsing task with Groq…")
    try:
        task = parse_task(description, timezone=TIMEZONE)
    except ValueError as e:
        err.print(f"[red]✗ Groq parsing failed:[/red] {e}")
        raise typer.Exit(1)

    _print_task_summary(task, verbose)

    # 2. Fetch calendar events
    app.print("[bold cyan]⟳[/bold cyan]  Fetching Google Calendar…")
    try:
        gcal_events = get_events(SEARCH_DAYS, timezone=TIMEZONE)
    except Exception as e:
        err.print(f"[red]✗ Calendar fetch failed:[/red] {e}")
        raise typer.Exit(1)

    _auto_sync(gcal_events)

    # 3. Find slot
    result: ScheduleResult = find_slot(
        task=task,
        gcal_events=gcal_events,
        blocked_path=BLOCKED_PATH,
        timezone=TIMEZONE,
    )

    if not result.success:
        err.print(f"\n[red]✗ Could not schedule:[/red] {result.reasoning}")
        _suggest_fix(result.failure_reason)
        raise typer.Exit(1)

    slot = result.slot
    app.print(f"\n[bold green]✓ Slot found:[/bold green] {_fmt_slot(slot, task['duration_minutes'])}")
    app.print(f"  [dim]{result.reasoning}[/dim]")

    if dry_run:
        app.print("\n[yellow]Dry run — no event created.[/yellow]")
        return

    # 4. Confirm with user
    confirmed = typer.confirm("\nCreate this Calendar event?", default=True)
    if not confirmed:
        app.print("[dim]Cancelled.[/dim]")
        return

    # 5. Insert into Google Calendar
    try:
        gcal_id = insert_event(task, slot.start, timezone=TIMEZONE)
    except Exception as e:
        err.print(f"[red]✗ Calendar insert failed:[/red] {e}")
        raise typer.Exit(1)

    # 6. Persist task
    task["scheduled_start"] = slot.start.isoformat()
    task["gcal_event_id"]   = gcal_id
    task["status"]          = "scheduled"

    tasks = _load_tasks()
    tasks.append(task)
    _save_tasks(_prune_old_tasks(tasks))

    app.print(f"\n[bold green]✓ Scheduled![/bold green]  '{task['title']}' added to your calendar.")


# ---------------------------------------------------------------------------
# Command: list
# ---------------------------------------------------------------------------

@cli.command(name="list")
def list_tasks(
    status: str = typer.Option("all", "--status", "-s", help="Filter: all | pending | scheduled | done"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show IDs and Groq reasoning"),
):
    """Show your tasks."""
    tasks = _load_tasks()

    if status != "all":
        tasks = [t for t in tasks if t["status"] == status]

    if not tasks:
        app.print("[dim]No tasks found.[/dim]")
        return

    table = Table(box=box.ROUNDED, show_header=True, header_style="bold cyan")
    table.add_column("ID",       style="dim",   width=7)
    table.add_column("Title",    min_width=22)
    table.add_column("Duration", justify="right", width=9)
    table.add_column("Deadline", width=17)
    table.add_column("Priority", width=8)
    table.add_column("Scheduled", width=19)
    table.add_column("Status",   width=10)

    status_colors = {"pending": "yellow", "scheduled": "green", "done": "dim"}

    for t in sorted(tasks, key=lambda x: x["created_at"], reverse=True):
        color    = status_colors.get(t["status"], "white")
        deadline = _fmt_dt(t.get("deadline")) + (
            " [dim](~)[/dim]" if t.get("deadline_confidence") == "inferred" else ""
        )
        table.add_row(
            t["id"],
            t["title"] or "—",
            f"{t.get('duration_minutes', '?')} min",
            deadline or "—",
            t.get("priority", "—"),
            _fmt_dt(t.get("scheduled_start")) or "—",
            f"[{color}]{t['status']}[/{color}]",
        )
        if verbose and t.get("groq_reasoning"):
            table.add_row("", f"[dim italic]{t['groq_reasoning']}[/dim italic]",
                          "", "", "", "", "")

    app.print(table)
    app.print(f"[dim]{len(tasks)} task(s). [~] = inferred deadline[/dim]")


# ---------------------------------------------------------------------------
# Command: reschedule
# ---------------------------------------------------------------------------

@cli.command()
def reschedule(
    task_id: str = typer.Argument(..., help="Task ID (from the list command)"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
):
    """Move a scheduled task to a new slot."""
    tasks = _load_tasks()
    task  = _find_task(tasks, task_id)

    if not task:
        err.print(f"[red]✗ Task '{task_id}' not found.[/red]")
        raise typer.Exit(1)

    if task["status"] == "done":
        err.print("[red]✗ Task is already done — can't reschedule.[/red]")
        raise typer.Exit(1)

    app.print(f"\nRescheduling: [bold]{task['title']}[/bold]")

    # Fetch fresh calendar state
    app.print("[bold cyan]⟳[/bold cyan]  Fetching Google Calendar…")
    gcal_events = get_events(SEARCH_DAYS, timezone=TIMEZONE)

    _auto_sync(gcal_events)

    # Exclude the task's own current event from busy intervals
    if task.get("gcal_event_id"):
        gcal_events = [e for e in gcal_events if e["id"] != task["gcal_event_id"]]

    result = find_slot(
        task=task,
        gcal_events=gcal_events,
        blocked_path=BLOCKED_PATH,
        timezone=TIMEZONE,
    )

    if not result.success:
        err.print(f"\n[red]✗ Could not reschedule:[/red] {result.reasoning}")
        _suggest_fix(result.failure_reason)
        raise typer.Exit(1)

    slot = result.slot
    app.print(f"\n[bold green]✓ New slot found:[/bold green] {_fmt_slot(slot, task['duration_minutes'])}")
    app.print(f"  [dim]{result.reasoning}[/dim]")

    confirmed = typer.confirm("\nMove the Calendar event to this slot?", default=True)
    if not confirmed:
        app.print("[dim]Cancelled.[/dim]")
        return

    # Update or recreate the GCal event
    try:
        if task.get("gcal_event_id"):
            update_event_time(
                task["gcal_event_id"],
                slot.start,
                task["duration_minutes"],
                timezone=TIMEZONE,
            )
        else:
            task["gcal_event_id"] = insert_event(task, slot.start, timezone=TIMEZONE)
    except Exception as e:
        err.print(f"[red]✗ Calendar update failed:[/red] {e}")
        raise typer.Exit(1)

    # Update task record
    task["scheduled_start"] = slot.start.isoformat()
    task["status"]          = "scheduled"
    _save_tasks(tasks)

    app.print(f"\n[bold green]✓ Rescheduled![/bold green]  '{task['title']}' moved to {_fmt_dt(slot.start.isoformat())}.")


# ---------------------------------------------------------------------------
# Command: done
# ---------------------------------------------------------------------------

@cli.command()
def done(
    task_id: str = typer.Argument(..., help="Task ID to mark as complete"),
    keep_event: bool = typer.Option(False, "--keep-event", help="Don't delete the Google Calendar event"),
):
    """Mark a task as done and optionally remove its Calendar event."""
    tasks = _load_tasks()
    task  = _find_task(tasks, task_id)

    if not task:
        err.print(f"[red]✗ Task '{task_id}' not found.[/red]")
        raise typer.Exit(1)

    task["status"] = "done"

    if task.get("gcal_event_id") and not keep_event:
        try:
            delete_event(task["gcal_event_id"])
            app.print("[dim]Calendar event removed.[/dim]")
        except Exception as e:
            err.print(f"[yellow]⚠ Could not delete Calendar event:[/yellow] {e}")

    _save_tasks(tasks)
    app.print(f"[bold green]✓ Done![/bold green]  '{task['title']}' marked complete.")


# ---------------------------------------------------------------------------
# Command: blocked
# ---------------------------------------------------------------------------

blocked_app = typer.Typer(help="Manage recurring blocked times (sleep, classes, work hours).")
cli.add_typer(blocked_app, name="blocked")


@blocked_app.command("add")
def blocked_add(
    label: str = typer.Option(..., "--label", "-l", help='Name, e.g. "Sleep"'),
    days:  str = typer.Option(..., "--days",  "-d", help='Comma-separated days, e.g. "mon,tue,wed,thu,fri"'),
    start: str = typer.Option(..., "--start", "-s", help='Start time HH:MM, e.g. "23:00"'),
    end:   str = typer.Option(..., "--end",   "-e", help='End time HH:MM, e.g. "07:00"'),
):
    """Add a recurring blocked period."""
    day_list = [d.strip().lower() for d in days.split(",")]
    valid    = {"mon","tue","wed","thu","fri","sat","sun"}
    bad      = [d for d in day_list if d not in valid]
    if bad:
        err.print(f"[red]✗ Unknown day(s):[/red] {', '.join(bad)}. Use mon/tue/wed/thu/fri/sat/sun.")
        raise typer.Exit(1)

    blocked = _load_blocked()
    blocked.append({"label": label, "days": day_list, "start_time": start, "end_time": end})
    _save_blocked(blocked)
    app.print(f"[bold green]✓[/bold green]  Added block: [bold]{label}[/bold] {days} {start}–{end}")


@blocked_app.command("list")
def blocked_list():
    """Show all recurring blocked times."""
    blocked = _load_blocked()
    if not blocked:
        app.print("[dim]No blocked times set.[/dim]")
        return

    table = Table(box=box.ROUNDED, header_style="bold cyan")
    table.add_column("Label",  min_width=14)
    table.add_column("Days",   min_width=25)
    table.add_column("Start",  width=7)
    table.add_column("End",    width=7)

    for b in blocked:
        table.add_row(b["label"], ", ".join(b["days"]), b["start_time"], b["end_time"])

    app.print(table)


@blocked_app.command("remove")
def blocked_remove(
    label: str = typer.Argument(..., help="Label of the block to remove"),
):
    """Remove a recurring block by label."""
    blocked = _load_blocked()
    updated = [b for b in blocked if b["label"].lower() != label.lower()]

    if len(updated) == len(blocked):
        err.print(f"[red]✗ No block found with label '{label}'.[/red]")
        raise typer.Exit(1)

    _save_blocked(updated)
    app.print(f"[bold green]✓[/bold green]  Removed block: [bold]{label}[/bold]")


def _auto_sync(gcal_events: list[dict]) -> int:
    """Remove tasks whose GCal event no longer exists. Returns count removed."""
    tasks = _load_tasks()
    live_ids = {ev["id"] for ev in gcal_events}
    surviving = []
    removed = 0
    for t in tasks:
        if t.get("status") == "scheduled" and t.get("gcal_event_id") and t["gcal_event_id"] not in live_ids:
            app.print(f"[dim]Synced: removed '[bold]{t['title']}[/bold]' (calendar event deleted).[/dim]")
            removed += 1
        else:
            surviving.append(t)
    if removed:
        _save_tasks(surviving)
    return removed


def _load_blocked() -> list[dict]:
    if not BLOCKED_PATH.exists():
        return []
    text = BLOCKED_PATH.read_text().strip()
    return json.loads(text) if text else []


def _save_blocked(blocked: list[dict]) -> None:
    BLOCKED_PATH.parent.mkdir(parents=True, exist_ok=True)
    BLOCKED_PATH.write_text(json.dumps(blocked, indent=2))


# ---------------------------------------------------------------------------
# Command: sync
# ---------------------------------------------------------------------------

@cli.command()
def sync():
    """Remove tasks whose Google Calendar events have been manually deleted."""
    app.print("[bold cyan]⟳[/bold cyan]  Fetching Google Calendar…")
    try:
        gcal_events = get_events(SEARCH_DAYS, timezone=TIMEZONE)
    except Exception as e:
        err.print(f"[red]✗ Calendar fetch failed:[/red] {e}")
        raise typer.Exit(1)

    removed = _auto_sync(gcal_events)
    if removed == 0:
        app.print("[dim]All tasks are in sync — nothing removed.[/dim]")


# ---------------------------------------------------------------------------
# Command: clear
# ---------------------------------------------------------------------------

@cli.command()
def clear(
    force: bool = typer.Option(False, "--force", "-f", help="Skip confirmation"),
):
    """Remove completed tasks older than 7 days from local storage."""
    tasks   = _load_tasks()
    pruned  = _prune_old_tasks(tasks)
    removed = len(tasks) - len(pruned)

    if removed == 0:
        app.print("[dim]Nothing to clear.[/dim]")
        return

    if not force:
        confirmed = typer.confirm(f"Remove {removed} old completed task(s)?", default=True)
        if not confirmed:
            app.print("[dim]Cancelled.[/dim]")
            return

    _save_tasks(pruned)
    app.print(f"[bold green]✓[/bold green]  Cleared {removed} task(s).")


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _fmt_dt(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso)
        return dt.strftime("%a %b %d %H:%M")
    except ValueError:
        return iso


def _fmt_slot(slot, duration_minutes: int) -> str:
    end = slot.start + __import__("datetime").timedelta(minutes=duration_minutes)
    return (
        f"{slot.start.strftime('%A %b %d, %H:%M')} → {end.strftime('%H:%M')} "
        f"({duration_minutes} min)"
    )


def _print_task_summary(task: dict, verbose: bool) -> None:
    conf_label = {"explicit": "", "inferred": " [dim](~)[/dim]", "none": ""}
    deadline   = _fmt_dt(task.get("deadline")) or "none"
    conf       = conf_label.get(task.get("deadline_confidence", "none"), "")

    app.print(f"\n  [bold]{task['title']}[/bold]")
    app.print(f"  Duration  : {task.get('duration_minutes')} min")
    app.print(f"  Deadline  : {deadline}{conf}")
    app.print(f"  Priority  : {task.get('priority')}")
    app.print(f"  Preferred : {task.get('preferred_time_of_day')}")

    if verbose and task.get("groq_reasoning"):
        app.print(f"  Reasoning : [dim italic]{task['groq_reasoning']}[/dim italic]")


def _suggest_fix(reason: str | None) -> None:
    hints = {
        "deadline_too_tight": (
            "The deadline is too soon to fit this task. "
            "Try shortening the duration or extending the deadline."
        ),
        "no_slot_before_deadline": (
            "Your calendar is fully booked before this deadline. "
            "Consider moving or shortening a blocked period."
        ),
        "no_free_slot": (
            "No free slot found in the search window. "
            "Try running [bold]schedule blocked list[/bold] to review your blocked times."
        ),
    }
    hint = hints.get(reason or "", "")
    if hint:
        app.print(f"  [dim]Hint: {hint}[/dim]")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    cli()