"""Local image preprocessing contract; no model calls or source-image edits."""

import base64
import importlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
backend = importlib.import_module("backends.image_retrieval")


class ImageRetrievalImageTests(unittest.TestCase):
    def test_small_files_with_large_dimensions_are_resized_in_request(self):
        client = backend.ImageRetrievalClient("http://127.0.0.1:9123", "test-vl", "test-rerank", 2)
        for suffix in (".jpg", ".jpeg", ".png", ".bmp", ".webp"):
            for size in ((1800, 900), (900, 1800)):
                with self.subTest(suffix=suffix, size=size), tempfile.TemporaryDirectory() as tmp:
                    source = Path(tmp) / f"source{suffix}"
                    Image.new("RGB", size, "navy").save(source)
                    original = source.read_bytes()
                    original_mtime = source.stat().st_mtime_ns
                    self.assertLess(len(original), 5_000_000)
                    calls = []

                    def request(endpoint, path, payload, timeout, calls=calls):
                        calls.append((endpoint, path, payload, timeout))
                        return {"data": [{"embedding": [0.6, 0.8]}]}

                    with patch.object(backend, "_request_json", side_effect=request):
                        self.assertEqual(client.embed_image(str(source)), [0.6, 0.8])

                    self.assertEqual(len(calls), 1)
                    endpoint, path, payload, timeout = calls[0]
                    self.assertEqual(endpoint, "http://127.0.0.1:9123")
                    self.assertEqual(path, "/v1/embeddings")
                    self.assertEqual(payload["model"], "test-vl")
                    encoded = payload["input"][0]["multimodal_data"][0]
                    image_bytes = base64.b64decode(encoded, validate=True)
                    with Image.open(io.BytesIO(image_bytes)) as decoded:
                        decoded.load()
                        expected = (1152, 576) if size[0] > size[1] else (576, 1152)
                        self.assertEqual(decoded.size, expected)
                        self.assertAlmostEqual(decoded.width / decoded.height, size[0] / size[1])
                        self.assertEqual(decoded.format, "PNG")
                    self.assertEqual(source.read_bytes(), original)
                    self.assertEqual(source.stat().st_mtime_ns, original_mtime)
                    with Image.open(source) as unchanged:
                        self.assertEqual(unchanged.size, size)
                    self.assertEqual(list(Path(tmp).iterdir()), [source])

    def test_supported_images_within_limit_keep_original_bytes(self):
        for suffix in (".jpg", ".jpeg", ".png", ".bmp"):
            for size in ((128, 64), (1152, 576)):
                with self.subTest(suffix=suffix, size=size), tempfile.TemporaryDirectory() as tmp:
                    source = Path(tmp) / f"source{suffix}"
                    Image.new("RGB", size, "navy").save(source)
                    original = source.read_bytes()

                    encoded = backend._image_base64(str(source))

                    self.assertEqual(base64.b64decode(encoded, validate=True), original)
                    with Image.open(io.BytesIO(base64.b64decode(encoded))) as decoded:
                        decoded.load()
                        self.assertEqual(decoded.size, size)
                    self.assertEqual(source.read_bytes(), original)

    def test_small_webp_converts_to_png_without_upscaling(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.webp"
            Image.new("RGB", (128, 64), "navy").save(source)
            original = source.read_bytes()

            encoded = backend._image_base64(str(source))

            with Image.open(io.BytesIO(base64.b64decode(encoded, validate=True))) as decoded:
                decoded.load()
                self.assertEqual(decoded.size, (128, 64))
                self.assertEqual(decoded.format, "PNG")
            self.assertEqual(source.read_bytes(), original)

    def test_missing_pillow_fails_explicitly_instead_of_sending_original(self):
        for suffix in (".jpg", ".png", ".bmp", ".webp"):
            with self.subTest(suffix=suffix), tempfile.TemporaryDirectory() as tmp:
                source = Path(tmp) / f"source{suffix}"
                Image.new("RGB", (128, 64), "navy").save(source)
                original = source.read_bytes()

                with patch.dict(sys.modules, {"PIL": None, "PIL.Image": None}):
                    with self.assertRaisesRegex(RuntimeError, "Pillow"):
                        backend._image_base64(str(source))

                self.assertEqual(source.read_bytes(), original)

    def test_missing_image_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError):
                backend._image_base64(str(Path(tmp) / "missing.png"))


if __name__ == "__main__":
    unittest.main()
