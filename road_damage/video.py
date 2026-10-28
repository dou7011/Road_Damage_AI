import copy
import json
import logging
import math
import re
import shutil
import subprocess
import time
from pathlib import Path
from threading import RLock, Thread
from uuid import uuid4

import cv2
import imageio_ffmpeg

from .classes import CLASS_COLORS
from .images import annotate

LOGGER = logging.getLogger(__name__)
VIDEO_EXTENSIONS = {"mp4", "avi", "mov", "mkv"}
VIDEO_FILE = re.compile(
    r"([0-9a-f]{32})_(?:upload\.(?:mp4|avi|mov|mkv)|raw\.avi|marked\.avi|original\.mp4|annotated\.mp4|frames\.jsonl)"
)
PUBLIC_VIDEO = re.compile(r"([0-9a-f]{32})_annotated\.mp4")


class VideoError(ValueError):
    pass


class VideoBusy(VideoError):
    pass


def remove_files(paths):
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            LOGGER.warning("Could not remove video file %s", path, exc_info=True)


def transcode(source, target, timeout):
    executable = shutil.which("ffmpeg") or imageio_ffmpeg.get_ffmpeg_exe()
    try:
        result = subprocess.run(
            [executable, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
             "-i", str(source), "-map", "0:v:0", "-an", "-c:v", "libx264",
             "-preset", "fast", "-crf", "20", "-pix_fmt", "yuv420p",
             "-movflags", "+faststart", str(target)],
            capture_output=True, timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise VideoError("FFmpeg 無法啟動或轉碼逾時，請確認安裝與影片長度。") from error
    if result.returncode != 0:
        LOGGER.error("FFmpeg failed: %s", result.stderr.decode("utf-8", errors="replace")[-4000:])
        raise VideoError("H.264 MP4 轉碼失敗，請查看伺服器日誌；未產生可用結果。")
    if not target.is_file() or target.stat().st_size == 0:
        raise VideoError("FFmpeg 未產生影片檔案。")


def validate_output(path, expected_frames, size, deadline=None):
    capture = cv2.VideoCapture(str(path))
    count = 0
    try:
        if not capture.isOpened():
            raise VideoError("輸出影片無法重新開啟。")
        while True:
            check_deadline(deadline)
            ok, frame = capture.read()
            if not ok:
                break
            if (frame.shape[1], frame.shape[0]) != size:
                raise VideoError("輸出影片解析度不符。")
            count += 1
            if count > expected_frames:
                raise VideoError("輸出影片影格數不符。")
    finally:
        capture.release()
    if count != expected_frames:
        raise VideoError("輸出影片不完整，影格數與已處理影格數不符。")


def check_deadline(deadline):
    if deadline is not None and time.monotonic() >= deadline:
        raise VideoError("YouTube 工作超過處理時限，已停止並清理暫存。")


class VideoJobs:
    """Single-process, single-video admission; the detector lock is shared with images."""

    def __init__(self, detector, config):
        self.detector = detector
        self.config = config
        self.directory = Path(config["RESULT_DIR"]) / "video"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.lock = RLock()
        self.jobs = {}
        self.active_id = None
        self.cleanup()

    def cleanup(self):
        with self.lock:
            cutoff = time.time() - self.config["RESULT_TTL_SECONDS"]
            expired = [
                job_id for job_id, job in self.jobs.items()
                if job_id != self.active_id and job["updated_at"] < cutoff
            ]
            for job_id in expired:
                del self.jobs[job_id]
            for path in self.directory.iterdir():
                match = VIDEO_FILE.fullmatch(path.name)
                if match and match[1] != self.active_id and path.is_file():
                    try:
                        if path.stat().st_mtime < cutoff:
                            path.unlink(missing_ok=True)
                    except OSError:
                        LOGGER.warning("Could not clean expired video %s", path, exc_info=True)

    def reserve(self):
        with self.lock:
            self.cleanup()
            if self.active_id is not None:
                raise VideoBusy("已有影片正在上傳或處理，請待該工作完成後再試。")
            if len(self.jobs) >= self.config["VIDEO_MAX_JOB_RECORDS"]:
                raise VideoBusy("影片工作紀錄已達上限，請待過期清理後再試。")
            if shutil.disk_usage(self.directory).free < self.config["VIDEO_MIN_FREE_BYTES"]:
                raise VideoError("磁碟可用空間不足 1 GB，請先清理空間。")
            job_id = uuid4().hex
            now = time.time()
            self.jobs[job_id] = {
                "job_id": job_id, "status": "uploading", "progress": 0,
                "processed_frames": 0, "total_frames": None,
                "created_at": now, "updated_at": now, "elapsed_seconds": 0,
                "result": None, "error": None,
            }
            self.active_id = job_id
            return job_id

    def update(self, job_id, **values):
        with self.lock:
            self.jobs[job_id].update(values, updated_at=time.time())

    def snapshot(self, job_id):
        with self.lock:
            self.cleanup()
            job = self.jobs.get(job_id)
            if job is None:
                return None
            snapshot = copy.deepcopy(job)
            if job["status"] not in {"completed", "failed"}:
                snapshot["elapsed_seconds"] = round(time.time() - job["created_at"], 2)
            return snapshot

    def abandon_upload(self, job_id, source):
        remove_files([source])
        with self.lock:
            self.jobs.pop(job_id, None)
            if self.active_id == job_id:
                self.active_id = None

    def start(self, job_id, source, youtube_url=None):
        self.update(job_id, status="validating")
        Thread(target=self._run, args=(job_id, source, youtube_url), daemon=True, name=f"video-{job_id[:8]}").start()

    def _run(self, job_id, source, youtube_url=None):
        started = time.perf_counter()
        deadline = time.monotonic() + self.config["YOUTUBE_JOB_TIMEOUT"] if youtube_url else None
        marked = self.directory / f"{job_id}_marked.avi"
        output = self.directory / f"{job_id}_annotated.mp4"
        records = self.directory / f"{job_id}_frames.jsonl"
        record_stream = None
        capture = None
        writers = []
        completed = False
        try:
            if youtube_url:
                from .youtube import acquire

                acquire(youtube_url, source, self.config, lambda **values: self.update(job_id, **values))
            check_deadline(deadline)
            capture = cv2.VideoCapture(str(source))
            if not capture.isOpened():
                raise VideoError("OpenCV 無法開啟影片。容器或編碼可能不受目前環境支援，請轉成 H.264 MP4 後重試。")
            fps = capture.get(cv2.CAP_PROP_FPS)
            frame_count = capture.get(cv2.CAP_PROP_FRAME_COUNT)
            if not math.isfinite(fps) or not 0 < fps <= 240:
                raise VideoError("無法取得有效 FPS（支援大於 0 且不超過 240），無法維持影片播放速度。")
            if not math.isfinite(frame_count) or frame_count < 1:
                raise VideoError("無法取得有效總影格數，請先將影片轉成具有完整索引的 MP4。")
            total = round(frame_count)
            if total > self.config["VIDEO_MAX_FRAMES"] or total / fps > self.config["VIDEO_MAX_SECONDS"]:
                raise VideoError(f"影片超過 {self.config['VIDEO_MAX_SECONDS'] // 60} 分鐘或 {self.config['VIDEO_MAX_FRAMES']} 影格限制。")
            reported_width = capture.get(cv2.CAP_PROP_FRAME_WIDTH)
            reported_height = capture.get(cv2.CAP_PROP_FRAME_HEIGHT)
            if not all(math.isfinite(value) and value >= 1 for value in (reported_width, reported_height)):
                raise VideoError("無法取得有效影片解析度，請先重新編碼。")
            if reported_width * reported_height > self.config["VIDEO_MAX_PIXELS"]:
                raise VideoError("影片解析度超過 3840 × 2160 像素上限。")
            ok, frame = capture.read()
            if not ok or frame is None:
                raise VideoError("影片沒有可解碼影格；請檢查編碼、檔案完整性或先轉成 H.264 MP4。")
            height, width = frame.shape[:2]
            if width * height > self.config["VIDEO_MAX_PIXELS"]:
                raise VideoError("影片解析度超過 3840 × 2160 像素上限。")
            # MJPEG and yuv420p require even dimensions; pad, never crop original pixels.
            size = (width + width % 2, height + height % 2)
            writer = cv2.VideoWriter(str(marked), cv2.VideoWriter_fourcc(*"MJPG"), fps, size)
            writers.append(writer)
            if not writer.isOpened():
                raise VideoError("OpenCV MJPEG VideoWriter 初始化失敗，請檢查編碼器與磁碟權限。")
            self.update(job_id, status="processing", total_frames=total, metadata={
                "width": width, "height": height, "fps": fps, "total_frames": total,
                "duration": round(total / fps, 4), "output_width": size[0], "output_height": size[1],
            })
            LOGGER.info("Video job %s: %sx%s fps=%s reported_frames=%s", job_id, width, height, fps, total)
            summary = {"total": 0, **{name: 0 for name in CLASS_COLORS}}
            score_sums = {name: 0.0 for name in CLASS_COLORS}
            processed = damaged_frames = 0
            inference_time = 0.0
            record_stream = records.open("w", encoding="utf-8", newline="\n")
            while ok:
                check_deadline(deadline)
                if frame is None or frame.shape[:2] != (height, width):
                    raise VideoError("影片中途解析度改變或影格無效，無法產生一致的輸出。")
                if processed >= self.config["VIDEO_MAX_FRAMES"] or (processed + 1) / fps > self.config["VIDEO_MAX_SECONDS"]:
                    raise VideoError("實際解碼影格數超過影片長度或影格數上限。")
                inference_started = time.perf_counter()
                detections = self.detector.predict(frame)
                inference_time += time.perf_counter() - inference_started
                damaged_frames += bool(detections)
                if detections:
                    record_stream.write(json.dumps({
                        "frame_index": processed, "timestamp": round(processed / fps, 9),
                        "detections": detections,
                    }, ensure_ascii=False, allow_nan=False) + "\n")
                    if record_stream.tell() > self.config["VIDEO_MAX_REPORT_BYTES"]:
                        raise VideoError("逐幀辨識明細超過 128 MB 限制，請縮短影片。")
                for item in detections:
                    summary["total"] += 1
                    summary[item["class_code"]] += 1
                    score_sums[item["class_code"]] += item["confidence"]
                annotated = annotate(frame, detections)
                writers[0].write(cv2.copyMakeBorder(annotated, 0, height % 2, 0, width % 2, cv2.BORDER_CONSTANT))
                processed += 1
                self.update(job_id, processed_frames=processed,
                            progress=round(min(processed / total, 1) * 90, 2))
                if processed % 30 == 0:
                    if marked.stat().st_size > self.config["VIDEO_MAX_INTERMEDIATE_BYTES"]:
                        raise VideoError("影片暫存檔超過 4 GB 限制，請縮短影片或降低解析度。")
                    if shutil.disk_usage(self.directory).free < self.config["VIDEO_MIN_FREE_BYTES"]:
                        raise VideoError("處理途中磁碟空間不足，工作已停止。")
                ok, frame = capture.read()
            capture.release()
            capture = None
            record_stream.close()
            record_stream = None
            for writer in writers:
                writer.release()
            writers.clear()
            if processed < total - 1:
                raise VideoError(f"影片提早解碼結束：預期 {total} 影格，僅讀取 {processed}；檔案可能損壞或編碼不支援。")
            if marked.stat().st_size > self.config["VIDEO_MAX_INTERMEDIATE_BYTES"]:
                raise VideoError("影片暫存檔超過 4 GB 限制。")
            self.update(job_id, status="encoding", progress=90)
            check_deadline(deadline)
            timeout = self.config["VIDEO_TRANSCODE_TIMEOUT"]
            transcode(marked, output, min(timeout, max(.1, deadline - time.monotonic())) if deadline else timeout)
            self.update(job_id, status="checking_output", progress=98)
            validate_output(output, processed, size, deadline)
            check_deadline(deadline)
            elapsed = time.perf_counter() - started
            result = {
                "filename": output.name,
                "width": width, "height": height, "output_width": size[0], "output_height": size[1],
                "fps": round(fps, 6), "reported_total_frames": total,
                "processed_frames": processed, "duration": round(processed / fps, 4),
                "processing_time": round(elapsed, 4),
                "inference_time": round(inference_time, 6),
                "overall_average_confidence": round(sum(score_sums.values()) / summary["total"], 6) if summary["total"] else None,
                "source": "youtube" if youtube_url else "video",
                "input_info": self.snapshot(job_id).get("input_info"),
                "timestamp_basis": "frame_index_divided_by_fps_estimate",
                "frame_index_base": 0,
                "precise_source_timestamps_available": False,
                "average_frame_time": round(elapsed / processed, 6),
                "summary": summary, "frames_with_detections": damaged_frames,
                "average_confidence": {
                    name: round(score_sums[name] / summary[name], 6) if summary[name] else None
                    for name in CLASS_COLORS
                },
                "device": self.detector.device, "confidence_threshold": self.detector.confidence,
                "statistics_type": "frame_detections_not_unique_objects",
                "audio_included": False,
                "message": "影片辨識完成" if summary["total"] else "未偵測到符合條件的道路損壞",
            }
            # Publish completed only after releasing the admission slot and temporary files.
            completed = True
        except Exception as error:
            LOGGER.exception("Video job %s failed", job_id)
            message = str(error) if isinstance(error, VideoError) else "影片處理失敗，請查看伺服器日誌後重試。"
            self.update(job_id, status="failed", error={"code": "video_processing_failed", "message": message},
                        elapsed_seconds=round(time.perf_counter() - started, 2))
        finally:
            if capture is not None:
                capture.release()
            if record_stream is not None:
                record_stream.close()
            for writer in writers:
                writer.release()
            remove_files([source, marked] + ([] if completed else [output, records]))
            with self.lock:
                if completed:
                    self.update(job_id, status="completed", progress=100, result=result,
                                elapsed_seconds=round(time.perf_counter() - started, 2))
                    LOGGER.info("Video job %s completed: frames=%s seconds=%.3f", job_id, processed, elapsed)
                self.active_id = None
