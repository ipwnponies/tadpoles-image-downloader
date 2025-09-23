from __future__ import annotations

import logging
import pickle
import typing
from functools import cache
from pathlib import Path

import requests
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow

if typing.TYPE_CHECKING:
    from typing import Iterable

SCOPES = [
    "https://www.googleapis.com/auth/photoslibrary.appendonly",
]

CREDENTIALS_FILE = Path("client.json")
TOKEN_FILE = Path("token_photos.pickle")


@cache
def requests_session() -> requests.Session:
    creds = None
    if TOKEN_FILE.exists():
        with TOKEN_FILE.open("rb") as token:
            creds = pickle.load(token)
    if creds and creds.valid:
        # Credentials are valid, no action needed
        pass
    else:
        if creds and creds.expired and creds.refresh_token:
            logging.debug("Refreshing expired token")
            creds.refresh(Request())
        else:
            logging.warning("Credentials are missing or invalid. Fetching new token")
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
            creds = flow.run_local_server(port=0)
        with TOKEN_FILE.open("wb") as token:
            pickle.dump(creds, token)

    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {creds.token}"})
    return session


def upload_to_google_photos(image_path: Path) -> str:
    logging.info(f"Uploading {image_path} to Google Photos")
    # Upload the image
    with image_path.open("rb") as img:
        upload_token = (
            requests_session()
            .post(
                "https://photoslibrary.googleapis.com/v1/uploads",
                data=img,
                headers={
                    "Content-type": "application/octet-stream",
                    "X-Goog-Upload-File-Name": image_path.name,
                    "X-Goog-Upload-Protocol": "raw",
                },
            )
            .text
        )

    if not upload_token:
        raise Exception("Failed to get upload token")
    return upload_token


def mint(upload_tokens: Iterable[str]) -> None:
    if not upload_tokens:
        logging.info("No upload tokens provided, skipping minting")
        return

    # Create media item and attach to album
    create_item = {
        "newMediaItems": [
            {
                "description": "Uploaded via script",
                "simpleMediaItem": {"uploadToken": i},
            }
            for i in upload_tokens
        ],
    }

    resp = requests_session().post(
        "https://photoslibrary.googleapis.com/v1/mediaItems:batchCreate",
        json=create_item,
    )
    resp.raise_for_status()
