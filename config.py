import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TRAINED_MODEL = ROOT / "runs/road_damage/yolo11n_rdd2022_100ep/weights/best.pt"


class Config:
    SERVER_PORT = int(os.environ.get("PORT", "5000"))
    MODEL_PATH = Path(
        os.environ.get("ROAD_DAMAGE_MODEL", str(TRAINED_MODEL if TRAINED_MODEL.is_file() else ROOT / "best.pt"))
    )
    if not MODEL_PATH.is_absolute():
        MODEL_PATH = ROOT / MODEL_PATH
    RESULT_DIR = Path(os.environ.get("ROAD_DAMAGE_RESULT_DIR", str(ROOT / "instance/results")))
    MAX_CONTENT_LENGTH = 16 * 1024 * 1024
    MAX_IMAGE_PIXELS = 24_000_000
    VIDEO_MAX_CONTENT_LENGTH = int(os.environ.get("VIDEO_MAX_CONTENT_LENGTH", str(256 * 1024 * 1024)))
    VIDEO_MAX_PIXELS = 3840 * 2160
    VIDEO_MAX_SECONDS = 600
    VIDEO_MAX_FRAMES = 36000
    VIDEO_MAX_INTERMEDIATE_BYTES = int(
        os.environ.get("VIDEO_MAX_INTERMEDIATE_BYTES", str(4 * 1024 * 1024 * 1024))
    )
    VIDEO_MAX_REPORT_BYTES = 128 * 1024 * 1024
    VIDEO_MIN_FREE_BYTES = int(os.environ.get("VIDEO_MIN_FREE_BYTES", str(1024 * 1024 * 1024)))
    VIDEO_MAX_JOB_RECORDS = 100
    VIDEO_TRANSCODE_TIMEOUT = 600
    YOUTUBE_METADATA_TIMEOUT = 45
    YOUTUBE_DOWNLOAD_TIMEOUT = 180
    YOUTUBE_MAX_METADATA_BYTES = 8 * 1024 * 1024
    YOUTUBE_MAX_HEIGHT = 720
    YOUTUBE_JOB_TIMEOUT = 3600
    RESULT_TTL_SECONDS = 24 * 60 * 60
    CONFIDENCE = 0.25
    JSON_SORT_KEYS = False
