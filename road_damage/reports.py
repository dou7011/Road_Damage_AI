import csv
import io
import json

from .detector import CLASS_LABELS

CSV_FIELDS = [
    "source", "frame_index", "timestamp", "class_label", "class_name", "confidence",
    "x1", "y1", "x2", "y2", "class_id", "class_code", "detection_id",
    "width", "height", "position",
]


def export_detection(item, source, frame_index=None, timestamp=None):
    return {"source": source, "frame_index": frame_index, "timestamp": timestamp,
            "class_label": CLASS_LABELS.get(item["class_code"]), **item}


def csv_chunks(detections):
    # UTF-8 BOM allows Excel on Windows to recognize Chinese text.
    yield "\ufeff"
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=CSV_FIELDS)
    writer.writeheader()
    yield buffer.getvalue()
    for item in detections:
        buffer.seek(0)
        buffer.truncate(0)
        row = {field: item.get(field) for field in CSV_FIELDS}
        row.update(item["bbox"], detection_id=item["id"], position=item["position"]["label"])
        writer.writerow(row)
        yield buffer.getvalue()


def frame_records(stream):
    for line in stream:
        yield json.loads(line)


def detection_rows(frames):
    """Compact [frame_index, timestamp, class_code, confidence, id] rows, one per detection."""
    for frame in frames:
        for item in frame["detections"]:
            yield [frame["frame_index"], frame["timestamp"], item["class_code"], item["confidence"], item["id"]]


def video_detections(stream, source):
    for frame in frame_records(stream):
        for item in frame["detections"]:
            yield export_detection(item, source, frame["frame_index"], frame["timestamp"])


def json_chunks(metadata, detections):
    yield '{"metadata":' + json.dumps(metadata, ensure_ascii=False, allow_nan=False) + ',"detections":['
    first = True
    for item in detections:
        if not first:
            yield ","
        yield json.dumps(item, ensure_ascii=False, allow_nan=False)
        first = False
    yield "]}"
