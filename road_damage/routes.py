import re
import json
import time
from pathlib import Path
from uuid import uuid4

from flask import Blueprint, abort, current_app, jsonify, render_template, request, send_from_directory, url_for
from werkzeug.utils import secure_filename

from .detector import CLASS_COLORS
from .images import ALLOWED_FORMATS, InvalidImage, annotate, decode_image, encode_png
from .reports import csv_chunks, export_detection

routes = Blueprint("routes", __name__)
RESULT_NAME = re.compile(r"[0-9a-f]{32}_(original|annotated)\.png")
OWNED_IMAGE_FILE = re.compile(r"[0-9a-f]{32}_(?:(?:original|annotated)\.png|report\.(?:json|csv))")


def cleanup_results():
    cutoff = time.time() - current_app.config["RESULT_TTL_SECONDS"]
    for path in current_app.config["RESULT_DIR"].iterdir():
        if OWNED_IMAGE_FILE.fullmatch(path.name) and path.is_file():
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)
            except OSError:
                current_app.logger.warning("Could not clean result %s", path, exc_info=True)


def invalid(message, status=400):
    current_app.logger.warning("Rejected image upload: %s", message)
    return jsonify(success=False, error={"code": "invalid_image", "message": message}), status


@routes.get("/")
def index():
    return render_template("home.html", active="home")


@routes.get("/image")
def image_page():
    return render_template("image.html", active="image")


@routes.get("/video")
def video_page():
    return render_template("video.html", active="video")


@routes.get("/api/health")
def health():
    detector = current_app.extensions["detector"]
    return jsonify(success=True, status="ok", model={
        "loaded": True, "path": str(detector.path), "device": detector.device,
        "names": detector.names, "confidence_threshold": detector.confidence,
    })


@routes.post("/api/detect/image")
def detect_image():
    started = time.perf_counter()
    upload = request.files.get("image")
    if upload is None or not upload.filename:
        return invalid("請使用 image 欄位上傳圖片。")
    filename = secure_filename(upload.filename)
    extension = Path(filename).suffix.lower().lstrip(".")
    if extension not in ALLOWED_FORMATS:
        return invalid("只支援 JPG、JPEG、PNG、WEBP 圖片。", 415)
    upload_data = upload.read()
    try:
        image = decode_image(upload_data, extension, current_app.config["MAX_IMAGE_PIXELS"])
    except InvalidImage as error:
        return invalid(str(error), 422)
    detector = current_app.extensions["detector"]
    inference_started = time.perf_counter()
    detections = detector.predict(image)
    inference_time = time.perf_counter() - inference_started
    marked = annotate(image, detections)
    cleanup_results()
    token = uuid4().hex
    original_name, result_name = f"{token}_original.png", f"{token}_annotated.png"
    directory = current_app.config["RESULT_DIR"]
    original_bytes, result_bytes = encode_png(image), encode_png(marked)
    summary = {"total": len(detections), **{name: 0 for name in CLASS_COLORS}}
    for item in detections:
        summary[item["class_code"]] += 1
    height, width = image.shape[:2]
    body = dict(
        success=True, image=url_for("routes.result_image", filename=result_name),
        original_image=url_for("routes.result_image", filename=original_name),
        download_url=url_for("routes.download_image", filename=result_name),
        processing_time=round(time.perf_counter() - started, 4),
        image_width=width, image_height=height, summary=summary, detections=detections,
        device=detector.device, confidence_threshold=detector.confidence,
        message="辨識完成" if detections else "未偵測到符合條件的道路損壞",
        source="image", result_id=token,
        input_info={"name": Path(upload.filename.replace("\\", "/")).name[:255], "size_bytes": len(upload_data)},
        inference_time=round(inference_time, 6),
        average_confidence=round(sum(item["confidence"] for item in detections) / len(detections), 6) if detections else None,
        json_url=url_for("routes.image_report", result_id=token, report_format="json"),
        csv_url=url_for("routes.image_report", result_id=token, report_format="csv"),
    )
    report_names = (f"{token}_report.json", f"{token}_report.csv")
    try:
        (directory / original_name).write_bytes(original_bytes)
        (directory / result_name).write_bytes(result_bytes)
        body["processing_time"] = round(time.perf_counter() - started, 4)
        exported = [export_detection(item, "image") for item in detections]
        report = {"metadata": {key: value for key, value in body.items() if key != "detections"},
                  "detections": exported}
        (directory / report_names[0]).write_text(json.dumps(report, ensure_ascii=False, allow_nan=False), encoding="utf-8")
        with (directory / report_names[1]).open("w", encoding="utf-8", newline="") as stream:
            stream.writelines(csv_chunks(exported))
    except (OSError, ValueError):
        for name in (original_name, result_name, *report_names):
            try:
                (directory / name).unlink(missing_ok=True)
            except OSError:
                current_app.logger.warning("Could not remove incomplete output %s", name, exc_info=True)
        raise
    return jsonify(body)


@routes.get("/api/results/image/<result_id>/export/<report_format>")
def image_report(result_id, report_format):
    if not re.fullmatch(r"[0-9a-f]{32}", result_id) or report_format not in {"json", "csv"}:
        abort(404)
    cleanup_results()
    response = send_from_directory(
        current_app.config["RESULT_DIR"], f"{result_id}_report.{report_format}",
        as_attachment=True, download_name=f"road-damage-image.{report_format}",
        mimetype="application/json" if report_format == "json" else "text/csv",
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


def serve_result(filename, download=False):
    from flask import abort

    if not RESULT_NAME.fullmatch(filename):
        abort(404)
    cleanup_results()
    response = send_from_directory(
        current_app.config["RESULT_DIR"], filename,
        as_attachment=download, mimetype="image/png",
        download_name="road-damage-result.png" if download else None,
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@routes.get("/api/results/<filename>")
def result_image(filename):
    return serve_result(filename)


@routes.get("/api/download/<filename>")
def download_image(filename):
    return serve_result(filename, download=True)
