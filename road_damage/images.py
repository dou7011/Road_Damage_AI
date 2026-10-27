import logging
import os
import warnings
from functools import lru_cache
from io import BytesIO
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError

from .detector import CLASS_COLORS, CLASS_LABELS

LOGGER = logging.getLogger(__name__)

ALLOWED_FORMATS = {"jpg": "JPEG", "jpeg": "JPEG", "png": "PNG", "webp": "WEBP"}


class InvalidImage(ValueError):
    pass


def decode_image(data, extension, max_pixels):
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as probe:
                if probe.format != ALLOWED_FORMATS[extension]:
                    raise InvalidImage("副檔名與實際圖片格式不符。")
                if probe.width * probe.height > max_pixels:
                    raise InvalidImage("圖片超過 2400 萬像素限制。")
                if getattr(probe, "is_animated", False):
                    raise InvalidImage("請使用靜態圖片，不支援動態 PNG 或 WEBP。")
                probe.verify()
            with Image.open(BytesIO(data)) as source:
                # Normalize EXIF orientation once for both inference and displayed coordinates.
                rgba = ImageOps.exif_transpose(source).convert("RGBA")
                background = Image.new("RGBA", rgba.size, "white")
                background.alpha_composite(rgba)
                return cv2.cvtColor(np.array(background.convert("RGB")), cv2.COLOR_RGB2BGR)
    except InvalidImage:
        raise
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError,
            Image.DecompressionBombError, Image.DecompressionBombWarning) as error:
        raise InvalidImage("圖片損壞、格式不支援或無法安全解碼。") from error


# OpenCV cannot draw Chinese text, so labels are rendered with a system CJK font via Pillow.
FONT_CANDIDATES = [
    os.environ.get("ROAD_DAMAGE_FONT", ""),
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/Supplemental/Songti.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
    "C:/Windows/Fonts/msjh.ttc",
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/mingliu.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/wenquanyi/wqy-microhei/wqy-microhei.ttc",
]


@lru_cache(maxsize=16)
def label_font(size):
    for candidate in FONT_CANDIDATES:
        if candidate and Path(candidate).is_file():
            try:
                font = ImageFont.truetype(candidate, size)
                if font.getmask("縱").getbbox():
                    return font
            except OSError:
                continue
    LOGGER.warning("找不到可顯示中文的字型，標籤改用類別代碼；可用環境變數 ROAD_DAMAGE_FONT 指定字型檔。")
    return None


def label_text(item, font):
    code = item["class_code"]
    name = CLASS_LABELS.get(code, item["class_name"]) if font else code
    return f"#{item['id']} {name} {item['confidence']:.0%}"


def draw_label(output, text, font, anchor_x, anchor_y, color):
    """Draw a filled label whose bottom edge sits on anchor_y (or at the image top)."""
    height, width = output.shape[:2]
    pad = max(3, font.size // 5)
    left, top, right, bottom = font.getbbox(text)
    patch_w, patch_h = right - left + 2 * pad, bottom - top + 2 * pad
    patch = Image.new("RGB", (patch_w, patch_h), (color[2], color[1], color[0]))
    ImageDraw.Draw(patch).text((pad - left, pad - top), text, font=font, fill=(15, 23, 42))
    x = max(0, min(anchor_x, width - patch_w))
    y = max(0, anchor_y - patch_h)
    crop_w, crop_h = min(patch_w, width - x), min(patch_h, height - y)
    output[y:y + crop_h, x:x + crop_w] = cv2.cvtColor(np.asarray(patch)[:crop_h, :crop_w], cv2.COLOR_RGB2BGR)


def annotate(image, detections):
    output = image.copy()
    height, width = output.shape[:2]
    thickness = max(2, round(min(height, width) / 400))
    font_scale = max(0.45, min(height, width) / 1100)
    font = label_font(max(16, round(min(height, width) / 34)))
    for item in detections:
        box = item["bbox"]
        x1, y1, x2, y2 = [int(round(box[key])) for key in ("x1", "y1", "x2", "y2")]
        color_hex = CLASS_COLORS[item["class_code"]].lstrip("#")
        color = tuple(int(color_hex[index:index + 2], 16) for index in (4, 2, 0))
        cv2.rectangle(output, (x1, y1), (min(x2, width - 1), min(y2, height - 1)), color, thickness)
        text = label_text(item, font)
        if font:
            draw_label(output, text, font, x1, y1, color)
            continue
        (tw, th), baseline = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, font_scale, thickness)
        tx = max(0, min(x1, width - tw - 8))
        bottom = max(th + baseline + 8, y1)
        cv2.rectangle(output, (tx, bottom - th - baseline - 8), (tx + tw + 8, bottom), color, -1)
        cv2.putText(output, text, (tx + 4, bottom - baseline - 4),
                    cv2.FONT_HERSHEY_SIMPLEX, font_scale, (15, 23, 42), thickness, cv2.LINE_AA)
    return output


def encode_png(image):
    success, buffer = cv2.imencode(".png", image)
    if not success:
        raise OSError("Could not encode result image")
    return buffer.tobytes()
