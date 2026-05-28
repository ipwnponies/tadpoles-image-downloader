from __future__ import annotations

import asyncio
import json
import logging
import typing
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import aiohttp
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

if typing.TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence

SCOPES = [
    "https://www.googleapis.com/auth/photoslibrary.appendonly",
]

CREDENTIALS_FILE = Path("client.json")
TOKEN_FILE = Path("token_photos.json")


def _load_credentials(
    credentials_file: Path = CREDENTIALS_FILE,
    token_file: Path = TOKEN_FILE,
) -> Credentials:
    """Load OAuth2 credentials from disk, refreshing or re-authorizing as needed."""
    creds = None

    if token_file.exists():
        data = json.loads(token_file.read_text())
        # expiry is stored as a naive UTC ISO string; google-auth's internal
        # comparison (Credentials.expired) always uses naive UTC datetimes.
        expiry = datetime.fromisoformat(data["expiry"]) if data.get("expiry") else None
        creds = Credentials(
            token=data.get("token"),
            refresh_token=data.get("refresh_token"),
            token_uri=data.get("token_uri"),
            client_id=data.get("client_id"),
            client_secret=data.get("client_secret"),
            scopes=data.get("scopes"),
            expiry=expiry,
        )

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        logging.debug("Refreshing expired token")
        creds.refresh(Request())
    else:
        logging.warning("Credentials are missing or invalid. Fetching new token")
        flow = InstalledAppFlow.from_client_secrets_file(str(credentials_file), SCOPES)
        creds = flow.run_local_server(port=0)

    # Store expiry as naive UTC — google-auth provides and expects naive UTC datetimes.
    token_file.write_text(
        json.dumps(
            {
                "token": creds.token,
                "refresh_token": creds.refresh_token,
                "token_uri": creds.token_uri,
                "client_id": creds.client_id,
                "client_secret": creds.client_secret,
                "scopes": list(creds.scopes or []),
                "expiry": creds.expiry.isoformat() if creds.expiry else None,
            }
        )
    )
    return creds


@asynccontextmanager
async def google_photos_session(
    credentials_file: Path = CREDENTIALS_FILE,
    token_file: Path = TOKEN_FILE,
) -> AsyncIterator[aiohttp.ClientSession]:
    creds = await asyncio.to_thread(_load_credentials, credentials_file, token_file)
    headers = {"Authorization": f"Bearer {creds.token}"}
    async with aiohttp.ClientSession(headers=headers) as session:
        yield session


async def upload_to_google_photos(session: aiohttp.ClientSession, image_path: Path, caption: str) -> tuple[str, str]:
    logging.info("Uploading %s to Google Photos", image_path)

    data = await asyncio.to_thread(image_path.read_bytes)
    async with session.post(
        "https://photoslibrary.googleapis.com/v1/uploads",
        data=data,
        headers={
            "Content-type": "application/octet-stream",
            "X-Goog-Upload-File-Name": image_path.name,
            "X-Goog-Upload-Protocol": "raw",
        },
    ) as resp:
        resp.raise_for_status()
        upload_token = await resp.text()

    if not upload_token:
        raise RuntimeError("Failed to get upload token")
    return upload_token, caption


async def mint(session: aiohttp.ClientSession, upload_tokens: Sequence[tuple[str, str]]) -> None:
    if not upload_tokens:
        logging.info("No upload tokens provided, skipping minting")
        return

    create_item = {
        "newMediaItems": [
            {
                **({"description": caption} if caption else {}),
                "simpleMediaItem": {"uploadToken": token},
            }
            for token, caption in upload_tokens
        ],
    }

    async with session.post(
        "https://photoslibrary.googleapis.com/v1/mediaItems:batchCreate",
        json=create_item,
    ) as resp:
        resp.raise_for_status()
