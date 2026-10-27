import io
import csv
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import patch

import cv2
import imageio_ffmpeg
import numpy as np

from config import Config
from road_damage import create_app
from road_damage.detector import Detector
from road_damage.video import VideoError, transcode


def make_video(path, frames=6, fps=6):
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), fps, (320, 240))
    if not writer.isOpened():
        raise RuntimeError("Test requires OpenCV MJPEG writer")
    try:
        for index in range(frames):
            image = np.full((240, 320, 3), 70 + index, dtype=np.uint8)
            writer.write(image)
    finally:
        writer.release()


def detection(confidence):
    return {
        "id": 1, "class_id": 3, "class_code": "D40", "class_name": "D40",
        "confidence": confidence, "color": "#fb7185",
        "bbox": {"x1": 60, "y1": 90, "x2": 200, "y2": 180, "width": 140, "height": 90},
        "position": {"label": "中中", "center_x": 130, "center_y": 135},
    }


class VideoDetector:
    path = Path("test-only.pt")
    device = "cpu"
    confidence = .25
    names = {0: "D00", 1: "D10", 2: "D20", 3: "D40"}

    def __init__(self):
        self.frames = 0
        self.positive = False

    def predict(self, frame):
        self.frames += 1
        if self.positive and self.frames in (1, 2):
            return [detection(.8 if self.frames == 1 else .6)]
        return []


class VideoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.source = self.directory / "fixture.avi"
        make_video(self.source)
        self.detector = VideoDetector()
        self.app = create_app({
            "TESTING": True, "RESULT_DIR": self.directory / "results",
            "VIDEO_MIN_FREE_BYTES": 0,
        }, self.detector)
        self.client = self.app.test_client()
        self.manager = self.app.extensions["video_jobs"]
        self.blocker = None

    def tearDown(self):
        if self.blocker:
            self.blocker.set()
        deadline = time.monotonic() + 15
        while self.manager.active_id is not None and time.monotonic() < deadline:
            time.sleep(.02)
        if self.manager.active_id is not None:
            raise RuntimeError("Video worker did not stop before test cleanup")
        self.temp.cleanup()

    def upload(self, content=None, filename="road.avi"):
        return self.client.post("/api/detect/video/jobs", data={
            "video": (io.BytesIO(self.source.read_bytes() if content is None else content), filename)
        })

    def wait(self, job_id):
        deadline = time.monotonic() + 30
        snapshots = []
        while time.monotonic() < deadline:
            response = self.client.get(f"/api/detect/video/jobs/{job_id}")
            self.assertEqual(response.status_code, 200)
            job = response.json["job"]
            snapshots.append(job)
            if job["status"] in {"completed", "failed"} and self.manager.active_id is None:
                return job, snapshots
            time.sleep(.02)
        self.fail("Background job did not finish")

    def test_completed_statistics_playable_output_and_range(self):
        self.detector.positive = True
        response = self.upload(filename="../../road.avi")
        self.assertEqual(response.status_code, 202)
        self.assertIn("Location", response.headers)
        job, snapshots = self.wait(response.json["job_id"])
        self.assertEqual(job["status"], "completed", job["error"])
        result = job["result"]
        self.assertEqual(job["progress"], 100)
        self.assertTrue(all(0 <= item["progress"] <= 100 for item in snapshots))
        self.assertEqual(result["summary"], {"total": 2, "D00": 0, "D10": 0, "D20": 0, "D40": 2})
        self.assertEqual(result["frames_with_detections"], 2)
        self.assertEqual(result["average_confidence"]["D40"], .7)
        self.assertIsNone(result["average_confidence"]["D00"])
        self.assertEqual(result["processed_frames"], 6)
        self.assertEqual((result["width"], result["height"]), (320, 240))
        self.assertEqual(result["fps"], 6)
        self.assertEqual(result["duration"], 1)
        self.assertGreater(result["average_frame_time"], 0)
        self.assertAlmostEqual(result["average_frame_time"], result["processing_time"] / 6, places=4)
        self.assertEqual(result["statistics_type"], "frame_detections_not_unique_objects")
        self.assertFalse(result["audio_included"])
        with self.client.get(result["video"], headers={"Range": "bytes=0-99"}) as video:
            self.assertEqual(video.status_code, 206)
            self.assertEqual(video.mimetype, "video/mp4")
            self.assertEqual(len(video.data), 100)
        with self.client.get(result["download_url"]) as download:
            self.assertEqual(download.status_code, 200)
            self.assertIn("attachment;", download.headers["Content-Disposition"])
        self.assertNotIn("original_filename", result)
        self.assertNotIn("original_video", result)
        cap = cv2.VideoCapture(str(self.manager.directory / result["filename"]))
        ok, _ = cap.read()
        cap.release()
        self.assertTrue(ok)
        files = list(self.manager.directory.iterdir())
        self.assertEqual(len(files), 2)
        self.assertEqual(sum(path.suffix == ".mp4" for path in files), 1)
        self.assertEqual(sum(path.suffix == ".jsonl" for path in files), 1)
        self.assertEqual(result["overall_average_confidence"], .7)
        self.assertGreaterEqual(result["inference_time"], 0)

    def test_empty_frames_have_no_fake_boxes(self):
        job, _ = self.wait(self.upload(filename="道路測試.avi").json["job_id"])
        self.assertEqual(job["status"], "completed")
        result = job["result"]
        self.assertEqual(result["summary"]["total"], 0)
        self.assertEqual(result["frames_with_detections"], 0)
        self.assertEqual(result["message"], "未偵測到符合條件的道路損壞")
        self.assertIsNone(result["overall_average_confidence"])
        frames = self.client.get(result["frames_url"]).json
        self.assertEqual(frames["frames"], [])
        self.assertEqual(frames["total"], 0)
        self.assertIsNone(frames["next_offset"])
        with self.client.get(result["json_url"]) as report:
            self.assertEqual(report.json["detections"], [])
        with self.client.get(result["csv_url"]) as report:
            self.assertEqual(list(csv.DictReader(io.StringIO(report.data.decode("utf-8-sig")))), [])

    def test_paginated_frames_and_exports_match_true_detections(self):
        self.detector.positive = True
        job, _ = self.wait(self.upload().json["job_id"])
        result = job["result"]
        response = self.client.get(result["frames_url"] + "?offset=0&limit=1")
        self.assertEqual(response.status_code, 200)
        page = response.json
        self.assertEqual(page["total"], 2)
        self.assertEqual(page["next_offset"], 1)
        self.assertEqual(page["frames"][0]["frame_index"], 0)
        self.assertEqual(page["frames"][0]["timestamp"], 0)
        self.assertEqual(page["frames"][0]["detections"], [detection(.8)])
        page = self.client.get(result["frames_url"] + "?offset=1&limit=1").json
        self.assertIsNone(page["next_offset"])
        self.assertEqual(page["frames"][0]["frame_index"], 1)
        self.assertAlmostEqual(page["frames"][0]["timestamp"], 1 / 6, places=8)
        self.assertEqual(page["frames"][0]["detections"], [detection(.6)])
        self.assertEqual(self.client.get(result["frames_url"] + "?offset=100").json["frames"], [])
        for query in ["?offset=-1", "?limit=0", "?limit=26", "?offset=nope", "?limit=2.5"]:
            self.assertEqual(self.client.get(result["frames_url"] + query).status_code, 400)
        with self.client.get(result["json_url"]) as report:
            self.assertEqual(report.mimetype, "application/json")
            exported = report.json
        self.assertEqual(len(exported["detections"]), 2)
        self.assertEqual(exported["metadata"]["summary"], result["summary"])
        self.assertEqual(exported["metadata"]["timestamp_basis"], "frame_index_divided_by_fps_estimate")
        self.assertFalse(exported["metadata"]["precise_source_timestamps_available"])
        self.assertEqual(exported["detections"][0]["source"], "video")
        self.assertEqual(exported["detections"][1]["bbox"], detection(.6)["bbox"])
        self.assertAlmostEqual(exported["detections"][1]["timestamp"], 1 / 6, places=8)
        with self.client.get(result["csv_url"]) as report:
            rows = list(csv.DictReader(io.StringIO(report.data.decode("utf-8-sig"))))
        self.assertEqual(len(rows), 2)
        self.assertEqual([int(row["frame_index"]) for row in rows], [0, 1])
        self.assertEqual([float(row["confidence"]) for row in rows], [.8, .6])
        self.assertEqual(rows[1]["class_name"], "D40")
        self.assertEqual(rows[1]["class_label"], "坑洞")
        self.assertEqual(exported["detections"][0]["class_label"], "坑洞")
        listing = self.client.get(result["detections_url"]).json
        self.assertEqual(listing["total"], 2)
        self.assertEqual(listing["fps"], 6)
        self.assertEqual(listing["fields"], ["frame_index", "timestamp", "class_code", "confidence", "id"])
        self.assertEqual([row[0] for row in listing["rows"]], [0, 1])
        self.assertEqual([row[2] for row in listing["rows"]], ["D40", "D40"])
        self.assertEqual([row[3] for row in listing["rows"]], [.8, .6])
        self.assertAlmostEqual(listing["rows"][1][1], 1 / 6, places=8)
        self.assertEqual(float(rows[1]["x2"]), 200)
        self.assertAlmostEqual(float(rows[1]["timestamp"]), 1 / 6, places=8)
        self.assertEqual(self.client.get(result["csv_url"].replace("/csv", "/exe")).status_code, 404)
        self.assertEqual(self.client.get("/api/detect/video/jobs/" + "a" * 32 + "/export/json").status_code, 404)

    def test_report_size_limit_fails_and_cleans_outputs(self):
        self.detector.positive = True
        self.app.config["VIDEO_MAX_REPORT_BYTES"] = 1
        with self.assertLogs("road_damage.video", level="ERROR"):
            job, _ = self.wait(self.upload().json["job_id"])
        self.assertEqual(job["status"], "failed")
        self.assertIn("明細", job["error"]["message"])
        self.assertEqual(list(self.manager.directory.iterdir()), [])

    def test_real_model_video(self):
        self.manager.detector = Detector(Config.MODEL_PATH, Config.CONFIDENCE)
        job, _ = self.wait(self.upload().json["job_id"])
        self.assertEqual(job["status"], "completed", job["error"])
        self.assertEqual(job["processed_frames"], 6)
        self.assertEqual(job["result"]["summary"]["total"], 0)

    def test_mp4_mov_mkv_containers(self):
        executable = imageio_ffmpeg.get_ffmpeg_exe()
        for extension in ("mp4", "mov", "mkv"):
            with self.subTest(extension=extension):
                target = self.directory / f"source.{extension}"
                subprocess.run([
                    executable, "-nostdin", "-loglevel", "error", "-y", "-i", str(self.source),
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(target)
                ], check=True, capture_output=True)
                job, _ = self.wait(self.upload(target.read_bytes(), target.name).json["job_id"])
                self.assertEqual(job["status"], "completed", job["error"])
                self.assertEqual(job["processed_frames"], 6)

    def test_invalid_uploads_and_path_guards(self):
        self.assertEqual(self.client.post("/api/detect/video/jobs").status_code, 400)
        self.assertEqual(self.upload(b"invalid", "road.exe").status_code, 415)
        self.assertEqual(self.upload(b"").status_code, 422)
        with self.assertLogs("road_damage.video", level="ERROR"):
            job, _ = self.wait(self.upload(b"not-a-video", "road.mp4").json["job_id"])
        self.assertEqual(job["status"], "failed")
        self.assertIn("OpenCV", job["error"]["message"])
        self.assertIsNone(job["result"])
        self.assertEqual(list(self.manager.directory.iterdir()), [])
        for route in ["/api/results/video/best.pt", "/api/results/video/..%2Fconfig.py",
                      "/api/detect/video/jobs/not-real", "/api/results/video/" + "a" * 32 + "_annotated.mp4"]:
            self.assertEqual(self.client.get(route).status_code, 404)

    def test_video_limit_does_not_change_image_limit(self):
        self.app.config["VIDEO_MAX_CONTENT_LENGTH"] = 1024
        self.assertEqual(self.upload().status_code, 413)
        response = self.client.post("/api/detect/image", data=b"x" * 2048, content_type="application/octet-stream")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.app.config["MAX_CONTENT_LENGTH"], 16 * 1024 * 1024)

    def test_only_one_job_and_images_still_work(self):
        self.blocker = Event()
        entered = Event()
        original_predict = self.detector.predict

        def delayed(frame):
            if frame.shape[:2] != (240, 320):
                return []
            entered.set()
            if not self.blocker.wait(10):
                raise RuntimeError("Test blocker timed out")
            return original_predict(frame)

        with patch.object(self.detector, "predict", side_effect=delayed):
            first = self.upload()
            self.assertTrue(entered.wait(5))
            second = self.upload()
            self.assertEqual(second.status_code, 409)
            job_id = first.json["job_id"]
            status = self.client.get(f"/api/detect/video/jobs/{job_id}").json["job"]
            self.assertEqual(status["status"], "processing")
            self.assertEqual(status["total_frames"], 6)
            self.assertEqual(self.client.get("/api/health").status_code, 200)
            self.assertEqual(self.client.get("/").status_code, 200)
            from PIL import Image
            image = io.BytesIO()
            Image.new("RGB", (64, 48)).save(image, format="PNG")
            image.seek(0)
            image_response = self.client.post("/api/detect/image", data={"image": (image, "still.png")})
            self.assertEqual(image_response.status_code, 200)
            self.assertEqual(image_response.json["summary"]["total"], 0)
            self.blocker.set()
            job, _ = self.wait(job_id)
        self.assertEqual(job["status"], "completed")

    def test_frame_and_pixel_limits(self):
        for key, value in [("VIDEO_MAX_FRAMES", 2), ("VIDEO_MAX_PIXELS", 100), ("VIDEO_MAX_SECONDS", .1)]:
            with self.subTest(key=key):
                old = self.app.config[key]
                self.app.config[key] = value
                with self.assertLogs("road_damage.video", level="ERROR"):
                    job, _ = self.wait(self.upload().json["job_id"])
                self.app.config[key] = old
                self.assertEqual(job["status"], "failed")
                self.assertIsNone(job["result"])
                self.assertEqual(list(self.manager.directory.iterdir()), [])

    def test_writer_failure_cleans_files(self):
        with patch("road_damage.video.cv2.VideoWriter") as writer, self.assertLogs("road_damage.video", level="ERROR"):
            writer.return_value.isOpened.return_value = False
            job, _ = self.wait(self.upload().json["job_id"])
        self.assertEqual(job["status"], "failed")
        self.assertIn("VideoWriter", job["error"]["message"])
        self.assertEqual(list(self.manager.directory.iterdir()), [])

    def test_encoder_failure_cleans_files_and_releases_slot(self):
        with patch("road_damage.video.transcode", side_effect=VideoError("encoder failed")), self.assertLogs(
            "road_damage.video", level="ERROR"
        ):
            job, _ = self.wait(self.upload().json["job_id"])
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["error"]["message"], "encoder failed")
        self.assertEqual(list(self.manager.directory.iterdir()), [])
        next_job, _ = self.wait(self.upload().json["job_id"])
        self.assertEqual(next_job["status"], "completed")

    def test_storage_and_record_limits(self):
        with patch("road_damage.video.shutil.disk_usage") as disk:
            disk.return_value.free = -1
            self.assertEqual(self.upload().status_code, 507)
        self.app.config["VIDEO_MAX_JOB_RECORDS"] = 0
        self.assertEqual(self.upload().status_code, 409)

    def test_expiry_preserves_unrelated_files(self):
        job, _ = self.wait(self.upload().json["job_id"])
        unrelated = self.manager.directory / "keep.txt"
        unrelated.write_text("keep")
        with patch("road_damage.video.time.time", return_value=time.time() + 90000):
            self.assertEqual(self.client.get(f"/api/detect/video/jobs/{job['job_id']}").status_code, 404)
        self.assertEqual(list(self.manager.directory.iterdir()), [unrelated])

    def test_ffmpeg_nonzero_exit_is_not_success(self):
        with patch("road_damage.video.subprocess.run") as run:
            run.return_value.returncode = 1
            run.return_value.stderr = b"test: missing encoder"
            with self.assertLogs("road_damage.video", level="ERROR"), self.assertRaises(VideoError):
                transcode(self.source, self.directory / "bad.mp4", 1)

    def test_partial_decode_is_failure_not_shortened_success(self):
        actual_capture = cv2.VideoCapture

        class ShortCapture:
            def __init__(self, path):
                self.capture = actual_capture(path)
                self.read_count = 0

            def isOpened(self):
                return self.capture.isOpened()

            def get(self, property_id):
                return self.capture.get(property_id)

            def read(self):
                self.read_count += 1
                return self.capture.read() if self.read_count <= 2 else (False, None)

            def release(self):
                self.capture.release()

        with patch("road_damage.video.cv2.VideoCapture", ShortCapture), self.assertLogs(
            "road_damage.video", level="ERROR"
        ):
            job, _ = self.wait(self.upload().json["job_id"])
        self.assertEqual(job["status"], "failed")
        self.assertIn("提早解碼結束", job["error"]["message"])
        self.assertEqual(job["processed_frames"], 2)
        self.assertEqual(list(self.manager.directory.iterdir()), [])

    def test_invalid_fps(self):
        with patch("road_damage.video.cv2.VideoCapture") as capture, self.assertLogs(
            "road_damage.video", level="ERROR"
        ):
            capture.return_value.isOpened.return_value = True
            capture.return_value.get.return_value = 0
            job, _ = self.wait(self.upload().json["job_id"])
        self.assertEqual(job["status"], "failed")
        self.assertIn("FPS", job["error"]["message"])
        self.assertEqual(list(self.manager.directory.iterdir()), [])

    def test_bad_output_check_removes_all_outputs(self):
        with patch("road_damage.video.validate_output", side_effect=VideoError("output incomplete")), self.assertLogs(
            "road_damage.video", level="ERROR"
        ):
            job, _ = self.wait(self.upload().json["job_id"])
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["error"]["message"], "output incomplete")
        self.assertEqual(list(self.manager.directory.iterdir()), [])

    def test_odd_dimensions_are_padded_not_cropped(self):
        odd = self.directory / "odd.mkv"
        subprocess.run([
            imageio_ffmpeg.get_ffmpeg_exe(), "-nostdin", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc=size=321x241:rate=4:duration=1",
            "-c:v", "ffv1", "-pix_fmt", "bgr0", str(odd),
        ], check=True, capture_output=True)
        job, _ = self.wait(self.upload(odd.read_bytes(), odd.name).json["job_id"])
        self.assertEqual(job["status"], "completed", job["error"])
        result = job["result"]
        self.assertEqual((result["width"], result["height"]), (321, 241))
        self.assertEqual((result["output_width"], result["output_height"]), (322, 242))


if __name__ == "__main__":
    unittest.main()
