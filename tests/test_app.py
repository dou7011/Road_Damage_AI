import io
import csv
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PIL import Image

from config import Config
from road_damage import create_app
from road_damage.classes import CLASS_COLORS, CLASS_LABELS, catalog, display_name
from road_damage.detector import Detector
from road_damage.images import InvalidImage, annotate, decode_image

# Index order of the trained YOLO11m best.pt (read from model.names, never assumed by the app).
MODEL_NAMES = {0: "D00", 1: "D10", 2: "D20", 3: "D40", 4: "D44", 5: "D50", 6: "Repair", 7: "D43", 8: "D01", 9: "D11", 10: "Block crack", 11: "D0w0"}


def image_bytes(fmt="PNG", size=(640, 480)):
    stream = io.BytesIO()
    Image.new("RGB", size, "#606060").save(stream, format=fmt)
    return stream.getvalue()


class FixtureDetector:
    path = Path("test-only.pt")
    device = "cpu"
    confidence = 0.25
    names = MODEL_NAMES

    def __init__(self, detections=None):
        self.detections = detections or []

    def predict(self, image):
        return self.detections


class APITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.detector = FixtureDetector()
        self.app = create_app({"TESTING": True, "RESULT_DIR": self.directory}, self.detector)
        self.client = self.app.test_client()

    def tearDown(self):
        self.temp.cleanup()

    def upload(self, content=None, name="road.png"):
        return self.client.post("/api/detect/image", data={
            "image": (io.BytesIO(image_bytes() if content is None else content), name)
        })

    def test_home_and_health(self):
        self.assertEqual(self.client.get("/").status_code, 200)
        response = self.client.get("/api/health")
        self.assertTrue(response.json["model"]["loaded"])
        self.assertEqual(response.json["model"]["names"]["3"], "D40")
        self.assertEqual(len(response.json["model"]["names"]), 12)

    def test_all_12_classes_have_traditional_chinese_names(self):
        self.assertEqual(list(CLASS_LABELS), [
            "D00", "D10", "D20", "D40", "D44", "D50", "Repair", "D43", "D01", "D11", "Block crack", "D0w0"])
        self.assertEqual(display_name("D40"), "坑洞（D40）")
        self.assertEqual(display_name("D0w0"), "其他道路破損（D0w0）")
        home = self.client.get("/").get_data(as_text=True)
        self.assertIn("可辨識的 12 種道路破損", home)
        page = self.client.get("/image").get_data(as_text=True)
        start = page.index('id="class-catalog">') + len('id="class-catalog">')
        import json
        embedded = json.loads(page[start:page.index("</script>", start)])
        self.assertEqual(embedded, catalog())
        for code, info in embedded.items():
            self.assertIn(info["label"], home)
            self.assertEqual(info["display_name"], display_name(code))

    def test_every_class_is_counted_under_its_own_code(self):
        def detection(index, code):
            return {
                "id": index + 1, "class_id": index, "class_code": code, "class_name": code,
                "class_label": CLASS_LABELS[code], "class_display_name": display_name(code),
                "confidence": .8, "color": CLASS_COLORS[code],
                "bbox": {"x1": 10, "y1": 10, "x2": 50, "y2": 50, "width": 40, "height": 40},
                "position": {"label": "上左", "center_x": 30, "center_y": 30},
            }
        self.detector.detections = [detection(index, code) for index, code in MODEL_NAMES.items()]
        body = self.upload().json
        self.assertEqual(body["summary"], {"total": 12, **{code: 1 for code in MODEL_NAMES.values()}})
        with self.client.get(body["csv_url"]) as report:
            records = list(csv.DictReader(io.StringIO(report.data.decode("utf-8-sig"))))
        self.assertEqual([row["class_code"] for row in records], list(MODEL_NAMES.values()))
        self.assertEqual([row["class_label"] for row in records], [CLASS_LABELS[c] for c in MODEL_NAMES.values()])
        self.assertEqual(records[11]["class_display_name"], "其他道路破損（D0w0）")

    def test_image_and_video_are_separate_pages(self):
        home = self.client.get("/").get_data(as_text=True)
        image = self.client.get("/image").get_data(as_text=True)
        video = self.client.get("/video").get_data(as_text=True)
        self.assertIn('href="/image"', home)
        self.assertIn('href="/video"', home)
        self.assertIn('id="fileInput" type="file" multiple', image)
        self.assertNotIn('id="videoInput"', image)
        self.assertIn('id="youtubeForm"', video)
        self.assertNotIn('id="fileInput"', video)
        for path in ("/static/css/style.css", "/static/js/common.js", "/static/js/image.js", "/static/js/video.js"):
            with self.client.get(path) as response:
                self.assertEqual(response.status_code, 200)

    def test_multiple_images_can_be_processed_one_request_each(self):
        responses = [self.upload(name=f"road-{index}.png") for index in range(3)]
        self.assertEqual([response.status_code for response in responses], [200, 200, 200])
        self.assertEqual(len({response.json["result_id"] for response in responses}), 3)

    def test_empty_result_and_download(self):
        response = self.upload()
        self.assertEqual(response.status_code, 200)
        body = response.json
        self.assertEqual(body["message"], "未偵測到符合條件的道路損壞")
        self.assertEqual(body["summary"], {"total": 0, **{code: 0 for code in MODEL_NAMES.values()}})
        self.assertEqual(len(body["summary"]), 13)
        self.assertEqual((body["image_width"], body["image_height"]), (640, 480))
        self.assertGreaterEqual(body["processing_time"], 0)
        for url in (body["image"], body["original_image"], body["download_url"]):
            with self.client.get(url) as fetched:
                self.assertEqual(fetched.status_code, 200)
                self.assertEqual(Image.open(io.BytesIO(fetched.data)).size, (640, 480))
        with self.client.get(body["download_url"]) as download:
            self.assertIn("attachment;", download.headers["Content-Disposition"])
        self.assertIsNone(body["average_confidence"])
        with self.client.get(body["json_url"]) as report:
            self.assertEqual(report.json["detections"], [])
            self.assertEqual(report.json["metadata"]["summary"]["total"], 0)
        with self.client.get(body["csv_url"]) as report:
            records = list(csv.DictReader(io.StringIO(report.data.decode("utf-8-sig"))))
            self.assertEqual(records, [])

    def test_real_annotations_and_object_details(self):
        item = {
            "id": 1, "class_id": 3, "class_code": "D40", "class_name": "D40",
            "class_label": "坑洞", "class_display_name": "坑洞（D40）", "confidence": 0.92, "color": "#fb7185",
            "bbox": {"x1": 120, "y1": 200, "x2": 360, "y2": 340, "width": 240, "height": 140},
            "position": {"label": "中中", "center_x": 240, "center_y": 270},
        }
        self.detector.detections = [item]
        body = self.upload().json
        self.assertEqual(body["summary"]["D40"], 1)
        self.assertEqual(body["detections"][0], item)
        with self.client.get(body["original_image"]) as response:
            original = response.data
        with self.client.get(body["image"]) as response:
            marked = response.data
        self.assertNotEqual(original, marked)
        pixels = np.array(Image.open(io.BytesIO(marked)))
        self.assertTupleEqual(tuple(pixels[200, 200]), (251, 113, 133))
        self.assertEqual(body["average_confidence"], .92)
        self.assertGreaterEqual(body["inference_time"], 0)
        self.assertEqual(body["input_info"]["size_bytes"], len(image_bytes()))
        with self.client.get(body["json_url"]) as report:
            exported = report.json
            self.assertEqual(exported["metadata"]["inference_time"], body["inference_time"])
            self.assertEqual(exported["detections"][0]["bbox"], item["bbox"])
            self.assertEqual(exported["detections"][0]["source"], "image")
            self.assertIsNone(exported["detections"][0]["frame_index"])
            self.assertIsNone(exported["detections"][0]["timestamp"])
        with self.client.get(body["csv_url"]) as report:
            self.assertIn("attachment", report.headers["Content-Disposition"])
            records = list(csv.DictReader(io.StringIO(report.data.decode("utf-8-sig"))))
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["timestamp"], "")
            self.assertEqual(records[0]["frame_index"], "")
            self.assertEqual(records[0]["source"], "image")
            self.assertEqual(records[0]["class_name"], "D40")
            self.assertEqual(float(records[0]["x1"]), 120)
            self.assertEqual(float(records[0]["confidence"]), .92)

    def test_all_supported_formats(self):
        for fmt, extension in [("PNG", "png"), ("JPEG", "jpg"), ("JPEG", "jpeg"), ("WEBP", "webp")]:
            with self.subTest(fmt=fmt, extension=extension):
                self.assertEqual(self.upload(image_bytes(fmt), f"road.{extension}").status_code, 200)

    def test_reject_invalid_uploads(self):
        for content, name, status in [
            (b"bad", "road.exe", 415), (b"bad", "road.png", 422),
            (image_bytes("PNG"), "road.jpg", 422), (b"", "road.webp", 422),
        ]:
            response = self.upload(content, name)
            self.assertEqual(response.status_code, status)
            self.assertFalse(response.json["success"])
            self.assertIn("message", response.json["error"])
        self.assertEqual(self.client.post("/api/detect/image").status_code, 400)

    def test_limits(self):
        self.app.config["MAX_IMAGE_PIXELS"] = 100
        self.assertEqual(self.upload().status_code, 422)
        self.app.config["MAX_CONTENT_LENGTH"] = 100
        self.assertEqual(self.upload().status_code, 413)

    def test_traversal_and_safe_output_names(self):
        body = self.upload(name="../../road.png").json
        self.assertRegex(body["image"], r"^/api/results/[0-9a-f]{32}_annotated\.png$")
        for url in ["/api/results/config.py", "/api/results/..%2Fconfig.py", "/api/download/best.pt"]:
            self.assertEqual(self.client.get(url).status_code, 404)

    def test_retention_and_unrelated_files(self):
        expired = self.directory / ("a" * 32 + "_annotated.png")
        expired.write_bytes(b"old")
        unrelated = self.directory / "keep.txt"
        unrelated.write_text("keep")
        with patch("road_damage.routes.time.time", return_value=time.time() + 90000):
            self.assertEqual(self.client.get("/api/results/" + expired.name).status_code, 404)
        self.assertFalse(expired.exists())
        self.assertTrue(unrelated.exists())

    def test_failures_are_explicit(self):
        with patch.object(self.detector, "predict", side_effect=RuntimeError("test failure")), self.assertLogs(
            self.app.logger, level="ERROR"
        ):
            response = self.upload()
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json["error"]["code"], "internal_error")
        self.assertEqual(list(self.directory.glob("*.png")), [])

    def test_report_expiry_and_path_guards(self):
        body = self.upload().json
        for path in [
            "/api/results/image/../export/json",
            f"/api/results/image/{body['result_id']}/export/exe",
            "/api/results/image/" + "a" * 32 + "/export/json",
        ]:
            self.assertEqual(self.client.get(path).status_code, 404)
        with patch("road_damage.routes.time.time", return_value=time.time() + 90000):
            self.assertEqual(self.client.get(body["json_url"]).status_code, 404)
            self.assertEqual(self.client.get(body["csv_url"]).status_code, 404)
        self.assertEqual(list(self.directory.glob("*_report.*")), [])

    def test_report_write_failure_cleans_all_image_outputs(self):
        original_open = Path.open

        def fail_report(path, *args, **kwargs):
            if path.name.endswith("_report.json"):
                raise OSError("test report write failed")
            return original_open(path, *args, **kwargs)

        with patch.object(Path, "open", fail_report), self.assertLogs(self.app.logger, level="ERROR"):
            response = self.upload()
        self.assertEqual(response.status_code, 500)
        self.assertEqual(list(self.directory.glob("*.*")), [])


class ImageTests(unittest.TestCase):
    def test_exif_orientation(self):
        stream = io.BytesIO()
        image = Image.new("RGB", (80, 40))
        exif = image.getexif()
        exif[274] = 6
        image.save(stream, format="JPEG", exif=exif)
        decoded = decode_image(stream.getvalue(), "jpg", 10000)
        self.assertEqual(decoded.shape[:2], (80, 40))

    def test_animation_rejected(self):
        stream = io.BytesIO()
        Image.new("RGB", (20, 20), "red").save(
            stream, format="PNG", save_all=True,
            append_images=[Image.new("RGB", (20, 20), "blue")], duration=100, loop=0,
        )
        with self.assertRaises(InvalidImage):
            decode_image(stream.getvalue(), "png", 10000)

    def test_class_colors(self):
        self.assertEqual(set(CLASS_COLORS), set(MODEL_NAMES.values()))
        self.assertEqual(len(set(CLASS_COLORS.values())), 12)
        for code, color in CLASS_COLORS.items():
            canvas = np.zeros((300, 300, 3), dtype=np.uint8)
            result = annotate(canvas, [{
                "id": 1, "class_code": code, "class_name": code, "confidence": .9,
                "bbox": {"x1": 50, "y1": 100, "x2": 250, "y2": 250},
            }])
            expected = tuple(int(color[index:index + 2], 16) for index in (5, 3, 1))
            self.assertTupleEqual(tuple(result[200, 50]), expected)


class ModelTests(unittest.TestCase):
    def test_real_model_inference(self):
        detector = Detector(Config.MODEL_PATH, Config.CONFIDENCE)
        with tempfile.TemporaryDirectory() as directory:
            app = create_app({"TESTING": True, "RESULT_DIR": Path(directory)}, detector)
            response = app.test_client().post("/api/detect/image", data={
                "image": (io.BytesIO(image_bytes()), "synthetic-blank.png")
            })
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json["success"])
            self.assertEqual(set(detector.names.values()), set(MODEL_NAMES.values()))
            self.assertEqual(len(detector.names), 12)
            self.assertEqual(response.json["summary"]["total"], len(response.json["detections"]))
            self.assertTrue((Path(directory) / Path(response.json["image"]).name).is_file())

    def test_missing_model(self):
        with self.assertRaisesRegex(FileNotFoundError, "YOLO_MODEL_PATH"):
            Detector(Path("does-not-exist.pt"), .25)

    def test_model_with_wrong_classes_is_rejected(self):
        from unittest.mock import MagicMock
        model = MagicMock()
        model.names = {0: "D00", 1: "D10", 2: "D20", 3: "D40"}
        model.task = "detect"
        with patch("road_damage.detector.YOLO", return_value=model):
            with self.assertRaisesRegex(ValueError, "模型類別不符"):
                Detector(Config.MODEL_PATH, .25)

    def test_cuda_failure_retries_cpu(self):
        from unittest.mock import MagicMock
        model = MagicMock()
        model.names = dict(MODEL_NAMES)
        model.task = "detect"
        empty_result = MagicMock()
        empty_result.boxes.xyxy.cpu.return_value.tolist.return_value = []
        empty_result.boxes.conf.cpu.return_value.tolist.return_value = []
        empty_result.boxes.cls.cpu.return_value.tolist.return_value = []
        model.predict.side_effect = [RuntimeError("CUDA unavailable"), [empty_result]]
        with patch("road_damage.detector.YOLO", return_value=model), patch(
            "road_damage.detector.torch.cuda.is_available", return_value=True
        ):
            detector = Detector(Config.MODEL_PATH, .25)
            self.assertEqual(detector.device, "cuda:0")
            with self.assertLogs("road_damage.detector", level="ERROR"):
                self.assertEqual(detector.predict(np.zeros((40, 40, 3), dtype=np.uint8)), [])
            self.assertEqual(detector.device, "cpu")
            self.assertEqual(detector.names[3], "D40")

    def test_model_outputs_use_actual_mapping_and_original_coordinates(self):
        from unittest.mock import MagicMock
        model = MagicMock()
        model.names = dict(MODEL_NAMES)
        model.task = "detect"
        result = MagicMock()
        result.boxes.xyxy.cpu.return_value.tolist.return_value = [[120.0, 200.0, 360.0, 340.0]]
        result.boxes.conf.cpu.return_value.tolist.return_value = [0.9234567]
        result.boxes.cls.cpu.return_value.tolist.return_value = [3.0]
        model.predict.return_value = [result]
        with patch("road_damage.detector.YOLO", return_value=model), patch(
            "road_damage.detector.torch.cuda.is_available", return_value=False
        ):
            detector = Detector(Config.MODEL_PATH, .25)
            items = detector.predict(np.zeros((1080, 1920, 3), dtype=np.uint8))
        self.assertEqual(items[0]["class_id"], 3)
        self.assertEqual(items[0]["class_name"], "D40")
        self.assertEqual(items[0]["class_label"], "坑洞")
        self.assertEqual(items[0]["class_display_name"], "坑洞（D40）")
        self.assertEqual(items[0]["confidence"], 0.923457)
        self.assertEqual(items[0]["bbox"], {
            "x1": 120, "y1": 200, "x2": 360, "y2": 340, "width": 240, "height": 140,
        })
        self.assertEqual(items[0]["position"], {"label": "上左", "center_x": 240, "center_y": 270})


if __name__ == "__main__":
    unittest.main()
