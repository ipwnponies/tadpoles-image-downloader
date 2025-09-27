from __future__ import annotations

import asyncio
import json
import logging
import os
from io import BytesIO
from pathlib import Path
from urllib.parse import urlparse

import filetype
import pendulum
import piexif
import typer
from aiohttp import ClientSession, TCPConnector
from PIL import Image

from tadpoles_image_downloader.cloud_storage import (
    google_photos_session,
    mint,
    upload_to_google_photos,
)

app = typer.Typer()

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s")


def write_image_file(
    data: bytes,
    file: Path,
    taken_at: str,
    tz: str = "America/Los_Angeles",
) -> None:
    """Write image data to file with timestamp in EXIF metadata."""

    current_time = pendulum.parse(taken_at).in_tz(tz)

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

    if kind := filetype.guess(data):
        ext = kind.extension
    else:
        logging.warning("Unknown format, assuming png")
        ext = "png"
    file = file.with_suffix(f".{ext}")

    img = Image.open(BytesIO(data))
    img.save(file, exif=exif)


async def _fetch_entry(
    session: ClientSession,
    entry: dict[str, str],
    images_dir: Path,
    dry_run: bool,
) -> tuple[str, str]:
    async with session.get(entry["url"], params={"d": "t"}) as resp:
        resp.raise_for_status()
        payload = await resp.read()
        redirected_url = str(resp.url)

    filename = Path(urlparse(redirected_url).path).name
    entry["filename"] = filename

    if not dry_run:
        await asyncio.to_thread(
            write_image_file,
            payload,
            images_dir / filename,
            entry["timestamp"],
        )
    logging.info("Downloaded: %s", filename)
    return filename, entry.get("caption", "")


async def process_file(
    file_path: Path,
    done_dir: Path,
    images_dir: Path,
    dry_run: bool,
) -> dict[str, str]:
    with file_path.open() as handle:
        data = json.load(handle)

    file_metadata: dict[str, str] = {}

    async with ClientSession(connector=TCPConnector()) as session:
        tasks = [asyncio.create_task(_fetch_entry(session, entry, images_dir, dry_run)) for entry in data]
        results = await asyncio.gather(*tasks)
        for filename, caption in results:
            file_metadata[filename] = caption

    if not dry_run:
        target = done_dir / file_path.name
        await asyncio.to_thread(file_path.replace, target)
    return file_metadata


@app.command()
def upload_images(images_dir: Path = typer.Option(..., help="Path to images directory")):
    asyncio.run(_upload_images(images_dir, {}))


async def _upload_images(images_dir: Path, file_captions: dict[str, str]):
    images_dir.mkdir(exist_ok=True)
    images = [i for i in images_dir.iterdir() if i.is_file()]
    if not images:
        logging.info("No images found to upload")
        return

    async with google_photos_session() as session:
        upload_tokens = await asyncio.gather(
            *[
                asyncio.create_task(upload_to_google_photos(session, image, file_captions[image.stem]))
                for image in images
            ]
        )
        await mint(session, upload_tokens)

    done_dir = images_dir / "Done"
    done_dir.mkdir(exist_ok=True)
    await asyncio.gather(*[asyncio.to_thread(image.replace, done_dir / image.name) for image in images])


@app.command()
def main(
    queue_dir: Path = typer.Option(..., help="Path to queue directory"),
    images_dir: Path = typer.Option(..., help="Path to images directory"),
    dry_run: bool = typer.Option(True, help="Print actions without making changes"),
):
    asyncio.run(_main(queue_dir, images_dir, dry_run))


async def _main(queue_dir: Path, images_dir: Path, dry_run: bool) -> None:
    done_dir = queue_dir / "Done"
    done_dir.mkdir(exist_ok=True)
    images_dir.mkdir(exist_ok=True)

    tasks = [asyncio.create_task(process_file(i, done_dir, images_dir, dry_run)) for i in queue_dir.glob("*.json")]
    results = await asyncio.gather(*tasks)  # results is a list of dicts

    file_metadatas: dict[str, str] = {}
    for result in results:
        file_metadatas.update(result)

    if not dry_run:
        await _upload_images(images_dir, file_metadatas)


if __name__ == "__main__":
    app()
