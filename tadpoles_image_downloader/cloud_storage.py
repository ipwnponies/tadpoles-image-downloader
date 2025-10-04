from __future__ import annotations

import asyncio
import logging
import pickle
import typing
from contextlib import asynccontextmanager
from pathlib import Path

import aiohttp
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow

if typing.TYPE_CHECKING:
    from typing import Iterable

SCOPES = [
    "https://www.googleapis.com/auth/photoslibrary.appendonly",
]

CREDENTIALS_FILE = Path("client.json")
TOKEN_FILE = Path("token_photos.pickle")


def _load_credentials():
    creds = None
    if TOKEN_FILE.exists():
        with TOKEN_FILE.open("rb") as token:
            creds = pickle.load(token)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        logging.debug("Refreshing expired token")
        creds.refresh(Request())
    else:
        logging.warning("Credentials are missing or invalid. Fetching new token")
        flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
        creds = flow.run_local_server(port=0)

    with TOKEN_FILE.open("wb") as token:
        pickle.dump(creds, token)
    return creds


@asynccontextmanager
async def google_photos_session():
    creds = await asyncio.to_thread(_load_credentials)
    headers = {"Authorization": f"Bearer {creds.token}"}
    async with aiohttp.ClientSession(headers=headers) as session:
        yield session


async def upload_to_google_photos(session: aiohttp.ClientSession, image_path: Path, caption: str) -> tuple[str, str]:
    logging.info(f"Uploading {image_path} to Google Photos")

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


async def mint(session: aiohttp.ClientSession, upload_tokens: Iterable[tuple[str, str]]) -> None:
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
