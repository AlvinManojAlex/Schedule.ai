"""
    Handles the Google OAuth2 flow for Schedule.ai.
    On first run, opens a browser to authorize the app and saves a token.json.
    On subsequent runs, loads and auto-refreshes the token silently.

    - Go to console.cloud.google.com → Create a project
    - Enable the Google Calendar API
    - Create OAuth 2.0 credentials (Desktop app type)
    - Download as credentials.json and place it at the path below
"""

from pathlib import Path
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/calendar"]

# Both files sit outside src/ so they're easy to .gitignore
AUTH_DIR         = Path(__file__).parent
CREDENTIALS_PATH = AUTH_DIR / "credentials.json"
TOKEN_PATH       = AUTH_DIR / "token.json"


def get_credentials() -> Credentials:
    """
        Load credentials from token.json if available and valid.
        Refresh silently if expired. Run the browser OAuth flow if missing entirely.
    """
    creds = None

    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        _save_token(creds)
        return creds

    # First-time auth: open browser
    if not CREDENTIALS_PATH.exists():
        raise FileNotFoundError(
            f"credentials.json not found at {CREDENTIALS_PATH}.\n"
            "Download it from Google Cloud Console → APIs & Services → Credentials."
        )

    flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_PATH), SCOPES)
    creds = flow.run_local_server(port=0, open_browser=True)
    _save_token(creds)
    return creds


def _save_token(creds: Credentials) -> None:
    TOKEN_PATH.write_text(creds.to_json())