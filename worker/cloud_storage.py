import requests
import pickle
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request

import typing

if typing.TYPE_CHECKING:
    from pathlib import Path

SCOPES = [
    "https://www.googleapis.com/auth/photoslibrary.appendonly",
    "https://www.googleapis.com/auth/photoslibrary.readonly.appcreateddata",
]

CREDENTIALS_FILE = Path("credentials.json")
TOKEN_FILE = Path("token_photos.pickle")


def get_photos_session() -> requests.Session:
    creds = None
    if TOKEN_FILE.exists():
        with TOKEN_FILE.open("rb") as token:
            creds = pickle.load(token)
    if creds and creds.valid:
        # Credentials are valid, no action needed
        pass
    else:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                str(CREDENTIALS_FILE), SCOPES
            )
            creds = flow.run_local_server(port=0)
        with TOKEN_FILE.open("wb") as token:
            pickle.dump(creds, token)

    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {creds.token}"})
    return session


def upload_to_google_photos(image_path: Path, album_id) -> None:
    session = get_photos_session()

    # Upload the image
    with image_path.open("rb") as img:
        upload_token = session.post(
            "https://photoslibrary.googleapis.com/v1/uploads",
            data=img,
            headers={
                "Content-type": "application/octet-stream",
                "X-Goog-Upload-File-Name": image_path.name,
                "X-Goog-Upload-Protocol": "raw",
            },
        ).text

    if not upload_token:
        raise Exception("Failed to get upload token")

    # Step 2: Create media item (add to album if provided)
    create_item = {
        "newMediaItems": [
            {
                "description": "Uploaded via script",
                "simpleMediaItem": {"uploadToken": upload_token},
            }
        ],
        "albumId": album_id,
    }

    resp = session.post(
        "https://photoslibrary.googleapis.com/v1/mediaItems:batchCreate",
        json=create_item,
    )
    resp.raise_for_status()
    print(f"Uploaded {image_path} → Album {album_id or '(library only)'}")
