import logging
import math
import os
from threading import Lock

os.environ.setdefault("YOLO_AUTOINSTALL", "false")

import torch
from ultralytics import YOLO

from .classes import CLASS_COLORS, CLASS_LABELS, display_name

LOGGER = logging.getLogger(__name__)


class Detector:
    def __init__(self, path, confidence):
        self.path = path.resolve()
        if not self.path.is_file():
            raise FileNotFoundError(
                f"找不到道路破損模型：{self.path}。請將 best.pt 放在專案根目錄，"
                "或以環境變數 YOLO_MODEL_PATH 指定模型路徑。"
            )
        try:
            self.model = YOLO(str(self.path))
        except Exception as error:
            raise RuntimeError(f"模型載入失敗：{self.path}（{error}）。請確認檔案是完整的 Ultralytics YOLO best.pt。") from error
        self.names = dict(self.model.names)
        if self.model.task != "detect" or len(self.names) != len(CLASS_COLORS) \
                or set(self.names.values()) != set(CLASS_COLORS):
            raise ValueError(
                f"模型類別不符：需要 {len(CLASS_COLORS)} 類 YOLO 偵測模型 ({', '.join(CLASS_COLORS)})，"
                f"實際為 task={self.model.task}、names={self.names}"
            )
        self.confidence = confidence
        self.lock = Lock()
        self.device = "cpu"
        try:
            if torch.cuda.is_available():
                self.model.to("cuda:0")
                self.device = "cuda:0"
        except (RuntimeError, AssertionError) as error:
            LOGGER.warning("CUDA initialization failed; falling back to CPU: %s", error)
            self.model.to("cpu")
        LOGGER.info("Model loaded: path=%s device=%s classes=%s", self.path, self.device, self.names)

    def predict(self, image):
        with self.lock:
            try:
                result = self.model.predict(image, conf=self.confidence, device=self.device, verbose=False)[0]
            except (RuntimeError, AssertionError):
                if self.device == "cpu":
                    raise
                LOGGER.exception("GPU inference failed; retrying on CPU")
                self.model.to("cpu")
                self.device = "cpu"
                result = self.model.predict(image, conf=self.confidence, device="cpu", verbose=False)[0]
            detections = []
            height, width = image.shape[:2]
            for box, score, class_id in zip(
                result.boxes.xyxy.cpu().tolist(),
                result.boxes.conf.cpu().tolist(),
                result.boxes.cls.cpu().tolist(),
            ):
                if not all(math.isfinite(value) for value in [*box, score, class_id]):
                    raise ValueError("Model produced non-finite detection values")
                class_id = int(class_id)
                name = self.names[class_id]
                x1, y1, x2, y2 = [
                    round(max(0, min(value, limit)), 2)
                    for value, limit in zip(box, [width, height, width, height])
                ]
                cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
                horizontal = "左" if cx < width / 3 else "右" if cx >= width * 2 / 3 else "中"
                vertical = "上" if cy < height / 3 else "下" if cy >= height * 2 / 3 else "中"
                detections.append({
                    "id": len(detections) + 1,
                    "class_id": class_id, "class_code": name, "class_name": name,
                    "class_label": CLASS_LABELS[name], "class_display_name": display_name(name),
                    "confidence": round(score, 6), "color": CLASS_COLORS[name],
                    "bbox": {"x1": x1, "y1": y1, "x2": x2, "y2": y2,
                             "width": round(x2 - x1, 2), "height": round(y2 - y1, 2)},
                    "position": {"label": f"{vertical}{horizontal}",
                                 "center_x": round(cx, 2), "center_y": round(cy, 2)},
                })
            return detections
