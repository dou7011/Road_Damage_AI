import io
import json
import socket
import ssl
import tempfile
import time
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import MagicMock, patch

import certifi

from road_damage import create_app
from road_damage.video import VideoError
from road_damage.youtube import (
    PublicHTTPS, canonical_url, choose_format, download, media_host, metadata,
)
from test_video import VideoDetector, make_video

URL = "https://www.youtube.com/watch?v=AbC_123-xYz"
MEDIA = "https://rr1---sn-example.googlevideo.com/videoplayback?token=test"


def info():
    return {
        "id": "AbC_123-xYz", "_type": "video", "title": "<script>fixture</script>",
        "channel": "Test fixture", "availability": "public", "age_limit": 0,
        "live_status": "not_live", "duration": 1,
        "formats": [{
            "url": MEDIA, "ext": "mp4", "protocol": "https", "vcodec": "avc1",
            "acodec": "none", "width": 320, "height": 240, "filesize": 1000,
        }],
    }


class URLTests(unittest.TestCase):
    def test_allowed_urls_are_reconstructed_not_forwarded(self):
        for value in [
            URL, URL + "&list=PL-ignore&redirect=http://127.0.0.1",
            "https://youtu.be/AbC_123-xYz?si=ignore",
            "https://www.youtube.com/shorts/AbC_123-xYz",
            "https://m.youtube.com/watch?v=AbC_123-xYz",
            "https://youtube.com/watch?v=AbC_123-xYz#ignore",
        ]:
            with self.subTest(value=value):
                self.assertEqual(canonical_url(value), URL)

    def test_bad_domains_ids_and_schemes(self):
        for value in [
            None, 1, "", "http://www.youtube.com/watch?v=AbC_123-xYz",
            "https://youtube.com.evil.example/watch?v=AbC_123-xYz",
            "https://youtube.com@127.0.0.1/watch?v=AbC_123-xYz",
            "https://www.youtube.com:443/watch?v=AbC_123-xYz",
            "https://www.youtube.com:bad/watch?v=AbC_123-xYz",
            "https://youtu.be/too-short", "https://youtu.be/AbC_123-xYz/extra",
            "https://www.youtube.com/watch?v=AbC_123-xYz&v=AbC_123-xYz",
            "file:///etc/passwd", "https://127.0.0.1/", "https://[::1]/",
            "https://www.youtube.com/playlist?list=abc",
            "https://www.youtube.com/shorts/AbC_123-xYz/extra",
            "https://youtu.be/AbC_123-xYz\n", "https://www.youtube.com\\@localhost/",
            "--exec=malicious", "https://youtu.be/AbC_123-xY%00",
        ]:
            with self.subTest(value=value), self.assertRaises(VideoError):
                canonical_url(value)

    def test_cdn_allowlist(self):
        self.assertEqual(media_host(MEDIA).hostname, "rr1---sn-example.googlevideo.com")
        for value in [
            "https://localhost/video.mp4", "https://googlevideo.com.evil.example/file",
            "https://127.0.0.1/file", "http://a.googlevideo.com/file",
            "https://a.googlevideo.com:8443/file", "https://user@a.googlevideo.com/file",
            "https://a.googlevideo.com/file\r\nHost: localhost", None,
        ]:
            with self.subTest(value=value), self.assertRaises(VideoError):
                media_host(value)

    def test_dns_private_addresses_blocked_before_connect(self):
        for address in ["127.0.0.1", "::1", "10.0.0.1", "169.254.169.254", "192.168.1.1"]:
            with self.subTest(address=address), patch(
                "road_damage.youtube.socket.getaddrinfo",
                return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))],
            ), patch("road_damage.youtube.socket.create_connection") as connect:
                with self.assertRaises(VideoError):
                    PublicHTTPS("a.googlevideo.com").connect()
                connect.assert_not_called()

    def test_dns_public_address_is_pinned_and_tls_uses_original_host(self):
        with patch("road_damage.youtube.socket.getaddrinfo", return_value=[
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
        ]), patch("road_damage.youtube.socket.create_connection") as connect, patch(
            "road_damage.youtube.ssl.create_default_context"
        ) as context:
            client = PublicHTTPS("a.googlevideo.com", timeout=10)
            client.connect()
            connect.assert_called_once_with(("8.8.8.8", 443), timeout=10)
            context.return_value.load_verify_locations.assert_called_once_with(cafile=certifi.where())
            context.return_value.wrap_socket.assert_called_once_with(
                connect.return_value, server_hostname="a.googlevideo.com",
            )

    def test_tls_verification_failure_closes_socket(self):
        error = ssl.SSLCertVerificationError("untrusted certificate")
        with patch("road_damage.youtube.socket.getaddrinfo", return_value=[
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
        ]), patch("road_damage.youtube.socket.create_connection") as connect, patch(
            "road_damage.youtube.ssl.create_default_context"
        ) as context:
            context.return_value.wrap_socket.side_effect = error
            with self.assertRaises(ssl.SSLCertVerificationError):
                PublicHTTPS("a.googlevideo.com").connect()
            connect.return_value.close.assert_called_once()


class YouTubeJobs(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "fixture.avi"
        make_video(self.source)
        self.detector = VideoDetector()
        self.app = create_app({
            "TESTING": True, "RESULT_DIR": self.root / "results", "VIDEO_MIN_FREE_BYTES": 0,
        }, self.detector)
        self.client = self.app.test_client()
        self.manager = self.app.extensions["video_jobs"]
        self.config = self.app.config
        self.blocker = None

    def tearDown(self):
        if self.blocker:
            self.blocker.set()
        deadline = time.monotonic() + 10
        while self.manager.active_id is not None and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertIsNone(self.manager.active_id)
        self.temp.cleanup()

    def submit(self, url=URL, authorized=True):
        return self.client.post("/api/detect/youtube", json={"url": url, "authorized": authorized})

    def wait(self, job_id):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            response = self.client.get(f"/api/detect/video/jobs/{job_id}")
            self.assertEqual(response.status_code, 200)
            job = response.json["job"]
            if job["status"] in {"completed", "failed"} and self.manager.active_id is None:
                return job
            time.sleep(.01)
        self.fail("Job timeout")

    def fake_download(self, url, target, config, update):
        self.assertEqual(url, MEDIA)
        target.write_bytes(self.source.read_bytes())
        update(downloaded_bytes=target.stat().st_size)

    def test_shared_pipeline_success(self):
        self.detector.positive = True
        with patch("road_damage.youtube.metadata", return_value=info()) as extractor, patch(
            "road_damage.youtube.download", side_effect=self.fake_download
        ):
            response = self.submit("https://youtu.be/AbC_123-xYz?si=ignored")
            self.assertEqual(response.status_code, 202)
            self.assertEqual(response.json["status_url"], f"/api/detect/video/jobs/{response.json['job_id']}")
            job = self.wait(response.json["job_id"])
        extractor.assert_called_once_with(URL, self.config)
        self.assertEqual(job["status"], "completed", job["error"])
        self.assertEqual(job["source_type"], "youtube")
        self.assertEqual(job["source_info"]["title"], "<script>fixture</script>")
        self.assertEqual(job["processed_frames"], 6)
        self.assertEqual(job["result"]["summary"]["D40"], 2)
        self.assertEqual(job["result"]["average_confidence"]["D40"], .7)
        self.assertEqual(len(list(self.manager.directory.iterdir())), 2)
        self.assertEqual(job["result"]["source"], "youtube")
        self.assertEqual(job["result"]["input_info"]["size_bytes"], self.source.stat().st_size)
        with self.client.get(job["result"]["json_url"]) as report:
            self.assertEqual(report.json["detections"][0]["source"], "youtube")
        serialized = json.dumps(job)
        self.assertNotIn("googlevideo.com", serialized)
        self.assertNotIn("token=test", serialized)
        with self.client.get(job["result"]["video"]) as result:
            self.assertEqual(result.status_code, 200)

    def test_invalid_requests_never_start_extractor(self):
        with patch("road_damage.youtube.metadata") as extractor:
            for value, authorized in [("https://127.0.0.1", True), (URL, False), (URL, "true")]:
                self.assertEqual(self.submit(value, authorized).status_code, 400)
            self.assertEqual(self.client.post("/api/detect/youtube", json=[]).status_code, 400)
            self.assertEqual(self.client.post("/api/detect/youtube", data="bad").status_code, 400)
            self.assertEqual(self.client.post("/api/detect/youtube", data="x" * 5000,
                                             content_type="application/json").status_code, 413)
            extractor.assert_not_called()
        self.assertIsNone(self.manager.active_id)

    def test_youtube_and_upload_share_admission(self):
        self.blocker = Event()
        entered = Event()

        def delayed(*args):
            entered.set()
            if not self.blocker.wait(5):
                raise VideoError("Test timed out")
            return info()

        with patch("road_damage.youtube.metadata", side_effect=delayed), patch(
            "road_damage.youtube.download", side_effect=self.fake_download
        ):
            response = self.submit()
            self.assertTrue(entered.wait(5))
            self.assertEqual(self.submit().status_code, 409)
            upload = self.client.post("/api/detect/video/jobs", data={
                "video": (io.BytesIO(self.source.read_bytes()), "fixture.avi")
            })
            self.assertEqual(upload.status_code, 409)
            self.assertEqual(self.client.get("/api/health").status_code, 200)
            status = self.client.get(response.json["status_url"]).json["job"]
            self.assertEqual(status["status"], "fetching_info")
            self.blocker.set()
            job = self.wait(response.json["job_id"])
        self.assertEqual(job["status"], "completed", job["error"])

    def test_access_restrictions_rejected_without_download(self):
        for values in [
            {"availability": "private"}, {"availability": "unlisted"}, {"age_limit": 18},
            {"is_live": True}, {"live_status": "was_live"}, {"has_drm": True},
            {"duration": 601}, {"duration": None}, {"id": "mismatch"},
            {"_type": "playlist"}, {"formats": []},
        ]:
            with self.subTest(values=values):
                data = info()
                data.update(values)
                with patch("road_damage.youtube.metadata", return_value=data), patch(
                    "road_damage.youtube.download"
                ) as downloader, self.assertLogs("road_damage.video", level="ERROR"):
                    job = self.wait(self.submit().json["job_id"])
                self.assertEqual(job["status"], "failed")
                downloader.assert_not_called()
                self.assertIsNone(job["result"])
                self.assertEqual(list(self.manager.directory.iterdir()), [])

    def test_too_long_video_message_includes_actual_length(self):
        data = info()
        data["duration"] = 754
        with self.assertRaisesRegex(VideoError, "12 分 34 秒.*10 分鐘"):
            choose_format(data, self.config, "AbC_123-xYz")

    def test_bad_formats(self):
        for changes in [
            {"url": "https://127.0.0.1/media"}, {"protocol": "m3u8_native"},
            {"height": 1080}, {"width": 1920}, {"filesize": 300 * 1024 * 1024},
            {"has_drm": True}, {"vcodec": "none"},
        ]:
            with self.subTest(changes=changes):
                data = info()
                data["formats"][0].update(changes)
                with self.assertRaises(VideoError):
                    choose_format(data, self.config, "AbC_123-xYz")

    def test_download_failure_cleans_partial_file(self):
        def partial(url, target, config, update):
            target.write_bytes(b"partial")
            raise VideoError("Download unavailable; use upload")

        with patch("road_damage.youtube.metadata", return_value=info()), patch(
            "road_damage.youtube.download", side_effect=partial
        ), self.assertLogs("road_damage.video", level="ERROR"):
            job = self.wait(self.submit().json["job_id"])
        self.assertEqual(job["status"], "failed")
        self.assertIn("use upload", job["error"]["message"])
        self.assertEqual(list(self.manager.directory.iterdir()), [])
        self.assertIsNone(self.manager.active_id)

    def test_job_deadline_cleans_downloaded_file(self):
        self.config["YOUTUBE_JOB_TIMEOUT"] = 0
        with patch("road_damage.youtube.metadata", return_value=info()), patch(
            "road_damage.youtube.download", side_effect=self.fake_download
        ), self.assertLogs("road_damage.video", level="ERROR"):
            job = self.wait(self.submit().json["job_id"])
        self.assertEqual(job["status"], "failed")
        self.assertIn("時限", job["error"]["message"])
        self.assertEqual(self.detector.frames, 0)
        self.assertEqual(list(self.manager.directory.iterdir()), [])

    def test_stream_download_limits_and_redirect_refusal(self):
        for status, length, chunks, max_bytes in [
            (302, None, [], 10), (200, "100", [], 10),
            (200, None, [b"x" * 11], 10),
            (200, "5", [b"x", b""], 10), (200, None, [b""], 10),
        ]:
            with self.subTest(status=status, length=length):
                self.config["VIDEO_MAX_CONTENT_LENGTH"] = max_bytes
                with patch("road_damage.youtube.PublicHTTPS") as connection:
                    response = connection.return_value.getresponse.return_value
                    response.status = status
                    response.getheader.return_value = length
                    response.read.side_effect = chunks
                    with self.assertRaises(VideoError):
                        download(MEDIA, self.root / "download.mp4", self.config, MagicMock())
                    connection.return_value.close.assert_called_once()

    def test_stream_download_success_and_timeout(self):
        target = self.root / "download.mp4"
        with patch("road_damage.youtube.PublicHTTPS") as connection:
            response = connection.return_value.getresponse.return_value
            response.status = 200
            response.getheader.return_value = "6"
            response.read.side_effect = [b"abc", b"def", b""]
            update = MagicMock()
            download(MEDIA, target, self.config, update)
            self.assertEqual(target.read_bytes(), b"abcdef")
            self.assertEqual(update.call_args.kwargs["downloaded_bytes"], 6)
            self.assertEqual(response.read.call_args.args, (64 * 1024,))
        self.config["YOUTUBE_DOWNLOAD_TIMEOUT"] = 0
        with patch("road_damage.youtube.PublicHTTPS") as connection:
            response = connection.return_value.getresponse.return_value
            response.status = 200
            response.getheader.return_value = "6"
            with self.assertRaisesRegex(VideoError, "逾時"):
                download(MEDIA, target, self.config, MagicMock())

    def test_certificate_failure_has_specific_message_and_closes_connection(self):
        error = ssl.SSLCertVerificationError("untrusted certificate")
        with patch("road_damage.youtube.PublicHTTPS") as connection:
            connection.return_value.request.side_effect = error
            with self.assertRaisesRegex(VideoError, "HTTPS 憑證驗證失敗") as raised:
                download(MEDIA, self.root / "download.mp4", self.config, MagicMock())
            self.assertIs(raised.exception.__cause__, error)
            self.assertIn("影片上傳", str(raised.exception))
            connection.return_value.close.assert_called_once()

    def test_metadata_command_isolated_and_output_limits(self):
        def fake_process(command, stdout, stderr, shell):
            self.assertFalse(shell)
            self.assertEqual(command[-1], URL)
            for flag in ["--ignore-config", "--no-plugin-dirs", "--no-js-runtimes",
                         "--no-remote-components", "--no-playlist", "--skip-download"]:
                self.assertIn(flag, command)
            stdout.write(json.dumps(info()).encode())
            stdout.flush()
            process = MagicMock()
            process.poll.return_value = 0
            process.returncode = 0
            return process

        with patch("road_damage.youtube.subprocess.Popen", side_effect=fake_process):
            self.assertEqual(metadata(URL, self.config)["id"], "AbC_123-xYz")
            self.config["YOUTUBE_MAX_METADATA_BYTES"] = 10
            with self.assertRaisesRegex(VideoError, "大小限制"):
                metadata(URL, self.config)

    def test_metadata_timeout_kills_worker_and_login_failure_has_fallback(self):
        process = MagicMock()
        process.poll.return_value = None
        self.config["YOUTUBE_METADATA_TIMEOUT"] = 0
        with patch("road_damage.youtube.subprocess.Popen", return_value=process):
            with self.assertRaisesRegex(VideoError, "逾時"):
                metadata(URL, self.config)
        process.kill.assert_called_once()
        process.wait.assert_called_once()

        def failed(command, stdout, stderr, shell):
            stderr.write(b"Sign in to confirm your age")
            stderr.flush()
            failed_process = MagicMock()
            failed_process.poll.return_value = 1
            failed_process.returncode = 1
            return failed_process

        with patch("road_damage.youtube.subprocess.Popen", side_effect=failed), self.assertLogs(
            "road_damage.youtube", level="WARNING"
        ):
            with self.assertRaisesRegex(VideoError, "登入"):
                metadata(URL, self.config)


if __name__ == "__main__":
    unittest.main()
