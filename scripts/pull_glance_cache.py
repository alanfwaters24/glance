#!/usr/bin/env python3
"""
Pulls the latest GLANCE content_cache.json from Google Drive and writes it to
~/glance/data/content_cache.json. Run by cron on dev-automation.

Looks for the most recently created file named 'glance_content_cache.json'
in the shared GLANCE Drive folder, downloads it, and overwrites the local cache. The cloud scheduled task creates a NEW file with this name
each run (no in-place update), so we always want the newest one.
"""
import json
import sys
from pathlib import Path

from google.oauth2 import service_account
from googleapiclient.discovery import build

KEY_PATH = Path.home() / "glance" / ".secrets" / "gdrive_service_account.json"
OUT_PATH = Path.home() / "glance" / "data" / "content_cache.json"
FILE_NAME = "glance_content_cache.json"
FOLDER_ID = "1a4MQDLHkWO_SEfNbMFv5FA-0XmYBl2i2"  # "GLANCE" folder, shared with the service account
SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]


def main():
    creds = service_account.Credentials.from_service_account_file(
        str(KEY_PATH), scopes=SCOPES
    )
    service = build("drive", "v3", credentials=creds)

    resp = (
        service.files()
        .list(
            q=f"name = '{FILE_NAME}' and '{FOLDER_ID}' in parents and trashed = false",
            orderBy="createdTime desc",
            pageSize=10,
            fields="files(id, name, createdTime)",
        )
        .execute()
    )
    files = resp.get("files", [])
    if not files:
        print(f"ERROR: no file named '{FILE_NAME}' found (shared with the service account?)", file=sys.stderr)
        sys.exit(1)

    newest = files[0]
    file_id = newest["id"]
    print(f"Found {len(files)} matching file(s); using newest: {file_id} (created {newest['createdTime']})")

    content = service.files().get_media(fileId=file_id).execute()

    # Validate it's real JSON before overwriting the live cache
    data = json.loads(content)

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(data, indent=2))
    print(f"Wrote {OUT_PATH} ({len(content)} bytes), generated_at={data.get('generated_at')}")
    # No clean-up here: the service account is read-only and only the file owner can
    # delete on a personal Drive. The claude.ai gather task trashes old copies itself.


if __name__ == "__main__":
    main()
