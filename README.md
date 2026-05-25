# Schedule.ai

A terminal based personal task scheduler that uses Groq's LLM API to parse task descriptions, resolves optimal calendar slots and schedules the result via Google Calendar API.

## Setup

### Prerequisites

- Python 3.11+
- A [Groq API key](https://console.groq.com)
- A Google Cloud project with the Calendar API enabled and an OAuth 2.0 Desktop credentials

### Installation

```bash
pip install -r requirements.txt
```

Create a `.env` file in the project root:

```
GROQ_API_KEY=your_key_here
```

Place your Google OAuth credential file at `auth/credentials.json` (download from Google Cloud Console → APIs & Services → Credentials).

Set your timezone in `config.py`:

```python
TIMEZONE = "America/New_York"   # IANA format
```

On first run, a browser window will open for Google Calendar OAuth consent. The token is saved to `auth/token.json` automatically.

## Usage

### Interactive mode (recommended)

```bash
python main.py
```

Launches a REPL — type tasks in plain English, enter `quit` or `exit` to exit.

### Single task

```bash
python main.py add "finish the report by Thursday, ~2 hours, morning"
python main.py add "call dentist, 30 min" --dry-run     # find slot, don't create event
python main.py add "urgent fix by tomorrow" --verbose   # show Groq reasoning
```

### Managing tasks

```bash
python main.py list                      # all tasks
python main.py list --status pending     # filter by status
python main.py reschedule <task-id>      # find a new slot and move the calendar event
python main.py done <task-id>            # mark done + delete calendar event
python main.py done <task-id> --keep-event
```

### Blocked times

Blocked times are recurring windows (sleep, classes, etc.) the scheduler will never place tasks into.

```bash
python main.py blocked add --label "Sleep" --days mon,tue,wed,thu,fri,sat,sun --start 23:00 --end 07:00
python main.py blocked list
python main.py blocked remove "Sleep"
```

### Sync & cleanup

```bash
python main.py sync          # remove tasks whose calendar events were manually deleted
python main.py clear         # remove completed tasks older than 7 days
python main.py clear --force # skip confirmation
```

## Design

### Must-haves

- Takes user's blocked time/non-negotiable time (like classes, office hours, sleep, etc.)

- Takes user input of task details.

- Groq works on the input and converts the natural language into a json object for the program.

- Uses Google Calendar API connected via OAuth to fetch free slots and place user's task into a matching slot.

### LLM Considerations

- Should check user's blocked times and decide best day and time and reasoning for the choice (helpful for debugging purposes).

- By using Groq to just make decisions and let the software handle slot and scheduling keeps token usage minimal.

### Data-related considerations

- Task data upto the previous week can be stored on client-side. This is simpler, since this is intended for personal use, and so users do not have to set up a SQL database for running this.

- Moreover, the json file containing the task information will take up 1KB per task, and assuming that a user creates a maximum of 10000 tasks in a month, the json will take up 10MB of file size which is a very small amount.

- Another smaller json data store will be needed to store user's blocked time like work/office hours, classes, sleep, etc.

### System Architecture

![System Architecture](images/sys-arch.png)