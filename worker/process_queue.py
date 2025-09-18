from __future__ import annotations
import json
from pathlib import Path
from urllib.parse import urlparse
import requests
import typer
from worker.cloud_storage import mint, upload_to_google_photos
import logging
import datetime
from PIL import Image
import piexif
from io import BytesIO
import filetype

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s")


def write_image_file(data: bytes, file: Path) -> None:
    """Write image data to file with current timestamp in EXIF metadata."""
    # EXIF is timezone naive, just like datetime.
    # So it's actually the rare time where python datetime library is not woefully deficient.
    current_time = datetime.datetime.now().strftime("%Y:%m:%d %H:%M:%S")

    exif = piexif.dump(
        {
            "Exif": {
                piexif.ExifIFD.DateTimeDigitized: current_time,
            }
        }
    )

    if kind := filetype.guess(data):
        ext = kind.extension
    else:
        logging.warn("Unknown format, assuming png")
        ext = "png"
    file = file.with_suffix(f".{ext}")

    img = Image.open(BytesIO(data))
    img.save(file, exif=exif)


def process_file(file_path: Path, done_dir: Path, images_dir: Path, dry_run: bool):
    with open(file_path) as f:
        data = json.load(f)

    for url in data["urls"]:
        # Setting query param d=t causes redirect to backing image
        resp = requests.get(url["url"], params={"d": "t"})
        resp.raise_for_status()

        redirected_url = resp.url

        filename = Path(urlparse(redirected_url).path).name

        if not dry_run:
            write_image_file(resp.content, images_dir / filename)
        logging.info(f"Downloaded: {filename}")

    # Move processed JSON to Done
    if not dry_run:
        file_path.replace(done_dir / file_path.name)


def main(
    queue_dir: Path = typer.Option(..., help="Path to queue directory"),
    images_dir: Path = typer.Option(..., help="Path to images directory"),
    dry_run: bool = typer.Option(True, help="Print actions without making changes"),
):
    done_dir = queue_dir / "Done"

    # Ensure directories exist
    done_dir.mkdir(exist_ok=True)
    images_dir.mkdir(exist_ok=True)

    for file_path in queue_dir.glob("*.json"):
        process_file(file_path, done_dir, images_dir, dry_run)

    if not dry_run:
        images = [i for i in images_dir.iterdir() if i.is_file()]
        upload_tokens = [upload_to_google_photos(i) for i in images]
        mint(upload_tokens)

        done_dir = images_dir / "Done"
        done_dir.mkdir(exist_ok=True)
        for image in images:
            image.replace(done_dir / image.name)


if __name__ == "__main__":
    typer.run(main)
