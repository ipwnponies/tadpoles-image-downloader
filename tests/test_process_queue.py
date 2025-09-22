from __future__ import annotations

import json
from io import BytesIO
import sys
import types
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Iterable
from unittest import TestCase, mock

import piexif
from PIL import Image

typer_stub = types.ModuleType("typer")


def _option(*args, **kwargs):  # pragma: no cover - trivial shim
    if args:
        return args[0]
    return kwargs.get("default")


typer_stub.Option = _option
typer_stub.run = lambda func: func  # type: ignore[assignment]
sys.modules.setdefault("typer", typer_stub)

from worker.process_queue import process_file, write_image_file


def _jpeg_bytes() -> bytes:
    img = Image.new("RGB", (2, 2), color="white")
    buf = BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


def _read_exif_value(exif: dict, key: int) -> str:
    value = exif[key]
    if isinstance(value, bytes):
        return value.decode()
    return value


def _normalize_offset(offset: str) -> str:
    if len(offset) == 5 and offset[0] in "+-" and offset[1:].isdigit():
        return f"{offset[:3]}:{offset[3:]}"
    return offset


class WriteImageFileTests(TestCase):
    def test_uses_provided_timestamp(self) -> None:
        timestamp = "2024-12-31T23:59:59-05:00"

        with TemporaryDirectory() as tmp_dir:
            destination = Path(tmp_dir) / "example"
            write_image_file(_jpeg_bytes(), destination, taken_at=timestamp)

            written_files: Iterable[Path] = list(Path(tmp_dir).glob("example.*"))
            self.assertEqual(1, len(written_files))
            image_path = next(iter(written_files))

            exif = piexif.load(image_path.read_bytes())["Exif"]
            self.assertEqual(
                "2024:12:31 23:59:59",
                _read_exif_value(exif, piexif.ExifIFD.DateTimeDigitized),
            )
            self.assertEqual(
                "-05:00",
                _normalize_offset(
                    _read_exif_value(exif, piexif.ExifIFD.OffsetTimeOriginal)
                ),
            )


class ProcessFileTests(TestCase):
    def setUp(self) -> None:
        self.image_bytes = _jpeg_bytes()

    def test_prefers_url_timestamp_and_falls_back_to_batch(self) -> None:
        url_timestamp = "2024-07-04T10:30:00+02:00"
        batch_timestamp = "2024-07-04T00:00:00+00:00"

        queue_payload = {
            "urls": [
                {"url": "https://example.com/photo?id=1", "timestamp": url_timestamp},
                {"url": "https://example.com/photo?id=2"},
            ],
            "timestamp": batch_timestamp,
        }

        with TemporaryDirectory() as tmp_dir:
            queue_dir = Path(tmp_dir)
            queue_file = queue_dir / "queue.json"
            queue_file.write_text(json.dumps(queue_payload))

            done_dir = queue_dir / "Done"
            images_dir = queue_dir / "images"
            done_dir.mkdir()
            images_dir.mkdir()

            first_response = mock.Mock()
            first_response.content = self.image_bytes
            first_response.url = "https://cdn.example.com/assets/photo1.jpg"
            first_response.raise_for_status = mock.Mock()

            second_response = mock.Mock()
            second_response.content = self.image_bytes
            second_response.url = "https://cdn.example.com/assets/photo2"
            second_response.raise_for_status = mock.Mock()

            with mock.patch("worker.process_queue.requests.get") as mock_get:
                mock_get.side_effect = [first_response, second_response]

                process_file(queue_file, done_dir, images_dir, dry_run=False)

            self.assertFalse(queue_file.exists())
            self.assertTrue((done_dir / "queue.json").exists())

            mock_get.assert_has_calls(
                [
                    mock.call("https://example.com/photo?id=1", params={"d": "t"}),
                    mock.call("https://example.com/photo?id=2", params={"d": "t"}),
                ]
            )

            photo1 = images_dir / "photo1.jpg"
            photo2 = images_dir / "photo2.jpg"
            self.assertTrue(photo1.exists())
            self.assertTrue(photo2.exists())

            exif_first = piexif.load(photo1.read_bytes())["Exif"]
            self.assertEqual(
                "2024:07:04 10:30:00",
                _read_exif_value(exif_first, piexif.ExifIFD.DateTimeDigitized),
            )
            self.assertEqual(
                "+02:00",
                _normalize_offset(
                    _read_exif_value(exif_first, piexif.ExifIFD.OffsetTimeOriginal)
                ),
            )

            exif_second = piexif.load(photo2.read_bytes())["Exif"]
            self.assertEqual(
                "2024:07:04 00:00:00",
                _read_exif_value(exif_second, piexif.ExifIFD.DateTimeDigitized),
            )
            self.assertEqual(
                "+00:00",
                _normalize_offset(
                    _read_exif_value(exif_second, piexif.ExifIFD.OffsetTimeOriginal)
                ),
            )
