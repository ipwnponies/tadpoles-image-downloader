from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
import subprocess
from collections.abc import Awaitable, Iterable
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import TypeVar, cast
from urllib.parse import urlparse

import filetype
import pendulum
import piexif
import typer
import yaml
from aiohttp import ClientSession, TCPConnector
from filetype.types import IMAGE
from PIL import Image
from platformdirs import PlatformDirs

from tadpoles_image_downloader.cloud_storage import (
    google_photos_session,
    mint,
    upload_to_google_photos,
)

app = typer.Typer()

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)

T = TypeVar("T")

DEFAULT_FETCH_CONCURRENCY = 15
DEFAULT_PROCESS_CONCURRENCY = 6
DEFAULT_UPLOAD_CONCURRENCY = 3


@functools.cache
def secrets() -> dict:
    SECRETS_FILE = Path(__file__).parents[1] / "secrets.yaml"
    AGE_IDENTITY = Path(PlatformDirs().user_config_path) / "age" / "tadpoles-image-downloader.agekey"
    if not (AGE_IDENTITY.exists() and SECRETS_FILE.exists()):
        raise RuntimeError(f"Not all required age files are present: {AGE_IDENTITY}, {SECRETS_FILE}")

    p = subprocess.run(
        ["sops", "decrypt", SECRETS_FILE],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "SOPS_AGE_KEY_FILE": str(AGE_IDENTITY)},
    )
    return yaml.safe_load(p.stdout)


@dataclass
class FetchedEntry:
    filename: str
    caption: str
    timestamp: str
    payload: bytes | None


def _validate_concurrency(name: str, value: int) -> int:
    if value < 1:
        raise typer.BadParameter(f"{name} must be greater than or equal to 1")
    return value


async def gather_with_concurrency(limit: int, awaitables: Iterable[Awaitable[T]]) -> list[T]:
    semaphore = asyncio.Semaphore(limit)

    async def run(awaitable: Awaitable[T]) -> T:
        async with semaphore:
            return await awaitable

    return await asyncio.gather(*(run(awaitable) for awaitable in awaitables))


def write_image_file(
    data: bytes,
    file: Path,
    taken_at: str,
    tz: str = "America/Los_Angeles",
) -> None:
    """Write image data to file with timestamp in EXIF metadata."""

    current_time = cast(pendulum.DateTime, pendulum.parse(taken_at)).in_tz(tz)

    timestamp = current_time.format("YYYY:MM:DD HH:mm:ss")
    offset = current_time.format("ZZ")

    exif = piexif.dump(
        {
            "Exif": {
                piexif.ExifIFD.DateTimeOriginal: timestamp,
                piexif.ExifIFD.OffsetTimeOriginal: offset,
            }
        }
    )

    kind = filetype.guess(data)
    if kind not in IMAGE:
        logging.warning("Unknown or non-image format, skipping")
        return
    file = file.with_suffix(f".{kind.extension}")

    img = Image.open(BytesIO(data))
    img.save(file, exif=exif)


async def _fetch_entry(
    session: ClientSession,
    entry: dict[str, str],
    dry_run: bool,
) -> FetchedEntry:
    async with session.get(entry["url"], params={"d": "t"}) as resp:
        resp.raise_for_status()
        payload = await resp.read()
        redirected_url = str(resp.url)

    filename = Path(urlparse(redirected_url).path).name
    logging.info("Downloaded: %s", filename)
    return FetchedEntry(
        filename=filename,
        caption=entry.get("caption", ""),
        timestamp=entry["timestamp"],
        payload=payload if not dry_run else None,
    )


async def process_file(
    file_path: Path,
    done_dir: Path,
    images_dir: Path,
    dry_run: bool,
    fetch_concurrency: int,
    write_concurrency: int,
) -> dict[str, str]:
    try:
        with file_path.open() as handle:
            data = json.load(handle)
    except json.JSONDecodeError:
        logging.warning("Skipping malformed JSON queue file: %s", file_path)
        return {}

    file_metadata: dict[str, str] = {}

    async with ClientSession(connector=TCPConnector(limit=fetch_concurrency)) as session:
        fetched_entries = await gather_with_concurrency(
            fetch_concurrency,
            (_fetch_entry(session, entry, dry_run) for entry in data),
        )

    deduped: dict[str, tuple[pendulum.DateTime, FetchedEntry]] = {}
    for fetched in fetched_entries:
        entry_timestamp = cast(pendulum.DateTime, pendulum.parse(fetched.timestamp))
        existing = deduped.get(fetched.filename)
        if existing:
            existing_timestamp, existing_entry = existing
            if entry_timestamp >= existing_timestamp:
                logging.info(
                    "Skipping duplicate download for %s at %s (keeping %s)",
                    fetched.filename,
                    fetched.timestamp,
                    existing_entry.timestamp,
                )
                continue
            logging.info(
                "Replacing duplicate %s with earlier timestamp %s (was %s)",
                fetched.filename,
                fetched.timestamp,
                existing_entry.timestamp,
            )
        deduped[fetched.filename] = (entry_timestamp, fetched)

    if not dry_run:

        def _write(entry: FetchedEntry, path: Path) -> None:
            if entry.payload is None:
                raise RuntimeError("payload is None in non-dry-run mode — this is a bug")
            write_image_file(entry.payload, path, entry.timestamp)

        await gather_with_concurrency(
            write_concurrency,
            (asyncio.to_thread(_write, entry, images_dir / filename) for filename, (_, entry) in deduped.items()),
        )

    for filename, (_, entry) in deduped.items():
        file_metadata[filename] = entry.caption

    if not dry_run:
        target = done_dir / file_path.name
        await asyncio.to_thread(file_path.replace, target)
    return file_metadata


@app.command()
def upload_images(
    images_dir: Path = typer.Option(..., help="Path to images directory"),
    upload_concurrency: int = typer.Option(
        DEFAULT_UPLOAD_CONCURRENCY,
        help="Maximum number of concurrent uploads",
    ),
):
    upload_concurrency = _validate_concurrency("upload-concurrency", upload_concurrency)
    asyncio.run(_upload_images(images_dir, {}, upload_concurrency))


async def _upload_images(images_dir: Path, file_captions: dict[str, str], upload_concurrency: int):
    images_dir.mkdir(exist_ok=True)
    images = [i for i in images_dir.iterdir() if i.is_file()]
    if not images:
        logging.info("No images found to upload")
        return

    async with google_photos_session() as session:
        upload_tokens = await gather_with_concurrency(
            upload_concurrency,
            (upload_to_google_photos(session, image, file_captions.get(image.stem, "")) for image in images),
        )
        await mint(session, upload_tokens)

    done_dir = images_dir / "Done"
    done_dir.mkdir(exist_ok=True)
    await gather_with_concurrency(
        upload_concurrency,
        (asyncio.to_thread(image.replace, done_dir / image.name) for image in images),
    )


async def _ping_healthcheck() -> None:
    async with ClientSession() as session:
        url = secrets()["healthcheck_url"]
        async with session.get(url) as resp:
            resp.raise_for_status()
    logging.info("Health check ping succeeded")


@app.command()
def main(
    queue_dir: Path = typer.Option(..., help="Path to queue directory"),
    images_dir: Path = typer.Option(..., help="Path to images directory"),
    dry_run: bool = typer.Option(True, help="Print actions without making changes"),
    fetch_concurrency: int = typer.Option(
        DEFAULT_FETCH_CONCURRENCY,
        help="Maximum number of concurrent downloads per queue file",
    ),
    process_concurrency: int = typer.Option(
        DEFAULT_PROCESS_CONCURRENCY,
        help="Maximum number of queue files and image writes processed concurrently",
    ),
    upload_concurrency: int = typer.Option(
        DEFAULT_UPLOAD_CONCURRENCY,
        help="Maximum number of concurrent uploads",
    ),
):
    fetch_concurrency = _validate_concurrency("fetch-concurrency", fetch_concurrency)
    process_concurrency = _validate_concurrency("process-concurrency", process_concurrency)
    upload_concurrency = _validate_concurrency("upload-concurrency", upload_concurrency)
    asyncio.run(_main(queue_dir, images_dir, dry_run, fetch_concurrency, process_concurrency, upload_concurrency))


async def _main(
    queue_dir: Path,
    images_dir: Path,
    dry_run: bool,
    fetch_concurrency: int,
    process_concurrency: int,
    upload_concurrency: int,
) -> None:
    done_dir = queue_dir / "Done"
    done_dir.mkdir(exist_ok=True)
    images_dir.mkdir(exist_ok=True)

    results = await gather_with_concurrency(
        process_concurrency,
        (
            process_file(
                queue_file,
                done_dir,
                images_dir,
                dry_run,
                fetch_concurrency,
                process_concurrency,
            )
            for queue_file in queue_dir.glob("*.json")
        ),
    )

    file_metadatas: dict[str, str] = {}
    for result in results:
        file_metadatas.update(result)

    if not dry_run:
        await _upload_images(images_dir, file_metadatas, upload_concurrency)
    await _ping_healthcheck()


if __name__ == "__main__":
    app()
