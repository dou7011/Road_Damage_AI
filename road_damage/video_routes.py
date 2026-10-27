import re
from pathlib import Path

from flask import Blueprint, Response, abort, current_app, jsonify, request, send_from_directory, url_for

from .video import PUBLIC_VIDEO, VIDEO_EXTENSIONS, VideoBusy, VideoError
from .youtube import canonical_url
from .reports import csv_chunks, detection_rows, frame_records, json_chunks, video_detections

video_routes = Blueprint("video_routes", __name__)


@video_routes.before_app_request
def video_request_limit():
    if request.endpoint == "video_routes.submit_video":
        request.max_content_length = current_app.config["VIDEO_MAX_CONTENT_LENGTH"]
    elif request.endpoint == "video_routes.submit_youtube":
        request.max_content_length = 4096


@video_routes.post("/api/detect/youtube")
def submit_youtube():
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or body.get("authorized") is not True:
        return jsonify(success=False, error={
            "code": "authorization_required",
            "message": "請確認你有權取得與處理此影片，且符合平台條款及著作權規範。",
        }), 400
    try:
        url = canonical_url(body.get("url"))
    except VideoError as error:
        return jsonify(success=False, error={"code": "invalid_youtube_url", "message": str(error)}), 400
    manager = current_app.extensions["video_jobs"]
    try:
        job_id = manager.reserve()
    except VideoBusy as error:
        return jsonify(success=False, error={"code": "video_busy", "message": str(error)}), 409
    except VideoError as error:
        current_app.logger.warning("YouTube admission rejected: %s", error)
        return jsonify(success=False, error={"code": "insufficient_storage", "message": str(error)}), 507
    source = manager.directory / f"{job_id}_upload.mp4"
    try:
        manager.update(job_id, source_type="youtube")
        manager.start(job_id, source, youtube_url=url)
    except Exception:
        manager.abandon_upload(job_id, source)
        raise
    status_url = url_for("video_routes.video_status", job_id=job_id)
    response = jsonify(success=True, job_id=job_id, status="accepted", status_url=status_url)
    response.status_code = 202
    response.headers["Location"] = status_url
    return response


@video_routes.post("/api/detect/video/jobs")
def submit_video():
    upload = request.files.get("video")
    if upload is None or not upload.filename:
        return jsonify(success=False, error={"code": "invalid_video", "message": "請使用 video 欄位上傳影片。"}), 400
    # Only the allowlisted suffix is reused; the client filename is never stored.
    extension = Path(upload.filename).suffix.lower().lstrip(".")
    if extension not in VIDEO_EXTENSIONS:
        return jsonify(success=False, error={"code": "invalid_video", "message": "只支援 MP4、AVI、MOV、MKV。"}), 415
    manager = current_app.extensions["video_jobs"]
    try:
        job_id = manager.reserve()
    except VideoBusy as error:
        return jsonify(success=False, error={"code": "video_busy", "message": str(error)}), 409
    except VideoError as error:
        current_app.logger.warning("Video admission rejected: %s", error)
        return jsonify(success=False, error={"code": "insufficient_storage", "message": str(error)}), 507
    source = manager.directory / f"{job_id}_upload.{extension}"
    try:
        upload.save(source)
        if source.stat().st_size == 0:
            manager.abandon_upload(job_id, source)
            return jsonify(success=False, error={"code": "invalid_video", "message": "影片檔案是空的。"}), 422
        manager.update(job_id, source_type="video", input_info={
            "name": Path(upload.filename.replace("\\", "/")).name[:255], "size_bytes": source.stat().st_size,
        })
        manager.start(job_id, source)
    except Exception:
        manager.abandon_upload(job_id, source)
        raise
    status_url = url_for("video_routes.video_status", job_id=job_id)
    response = jsonify(success=True, job_id=job_id, status="accepted", status_url=status_url)
    response.status_code = 202
    response.headers["Location"] = status_url
    return response


@video_routes.get("/api/detect/video/jobs/<job_id>")
def video_status(job_id):
    manager = current_app.extensions["video_jobs"]
    job = manager.snapshot(job_id)
    if job is None:
        abort(404)
    result = job["result"]
    if result:
        result["video"] = url_for("video_routes.video_result", filename=result["filename"])
        result["download_url"] = url_for("video_routes.video_download", filename=result["filename"])
        result["json_url"] = url_for("video_routes.video_report", job_id=job_id, report_format="json")
        result["csv_url"] = url_for("video_routes.video_report", job_id=job_id, report_format="csv")
        result["frames_url"] = url_for("video_routes.video_frames", job_id=job_id)
        result["detections_url"] = url_for("video_routes.video_detection_list", job_id=job_id)
    response = jsonify(success=True, job=job)
    response.headers["Cache-Control"] = "no-store"
    return response


def completed_job(job_id):
    if not re.fullmatch(r"[a-f0-9]{32}", job_id):
        abort(404)
    manager = current_app.extensions["video_jobs"]
    job = manager.snapshot(job_id)
    if job is None or job["status"] != "completed":
        abort(404)
    path = manager.directory / f"{job_id}_frames.jsonl"
    if not path.is_file():
        abort(404)
    return job, path


@video_routes.get("/api/detect/video/jobs/<job_id>/frames")
def video_frames(job_id):
    job, path = completed_job(job_id)
    try:
        offset = int(request.args.get("offset", "0"))
        limit = int(request.args.get("limit", "10"))
        if offset < 0 or not 1 <= limit <= 25:
            raise ValueError
    except ValueError:
        abort(400, description="offset 必須為非負整數，limit 必須為 1–25。")
    frames = []
    with path.open(encoding="utf-8") as stream:
        for index, frame in enumerate(frame_records(stream)):
            if index < offset:
                continue
            if len(frames) == limit:
                break
            frames.append(frame)
    total = job["result"]["frames_with_detections"]
    response = jsonify(success=True, frames=frames, offset=offset, total=total,
                       next_offset=offset + len(frames) if offset + len(frames) < total else None,
                       timestamp_basis=job["result"]["timestamp_basis"])
    response.headers["Cache-Control"] = "no-store"
    return response


@video_routes.get("/api/detect/video/jobs/<job_id>/detections")
def video_detection_list(job_id):
    job, path = completed_job(job_id)
    with path.open(encoding="utf-8") as stream:
        rows = list(detection_rows(frame_records(stream)))
    response = jsonify(success=True, fps=job["result"]["fps"], total=len(rows), rows=rows,
                       fields=["frame_index", "timestamp", "class_code", "confidence", "id"],
                       timestamp_basis=job["result"]["timestamp_basis"])
    response.headers["Cache-Control"] = "no-store"
    return response


@video_routes.get("/api/detect/video/jobs/<job_id>/export/<report_format>")
def video_report(job_id, report_format):
    if report_format not in {"json", "csv"}:
        abort(404)
    job, path = completed_job(job_id)
    metadata = {**job["result"], "source_info": job.get("source_info"), "job_id": job_id}
    stream = path.open(encoding="utf-8")

    def generate():
        try:
            detections = video_detections(stream, job["result"]["source"])
            yield from json_chunks(metadata, detections) if report_format == "json" else csv_chunks(detections)
        finally:
            stream.close()

    response = Response(generate(), mimetype="application/json" if report_format == "json" else "text/csv")
    response.call_on_close(stream.close)
    response.headers["Content-Disposition"] = f'attachment; filename="road-damage-video.{report_format}"'
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


def serve_video(filename, download=False):
    match = PUBLIC_VIDEO.fullmatch(filename)
    if match is None:
        abort(404)
    manager = current_app.extensions["video_jobs"]
    job = manager.snapshot(match[1])
    if job is None or job["status"] != "completed":
        abort(404)
    response = send_from_directory(
        manager.directory, filename, mimetype="video/mp4", as_attachment=download,
        download_name="road-damage-video.mp4" if download else None, conditional=True,
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@video_routes.get("/api/results/video/<filename>")
def video_result(filename):
    return serve_video(filename)


@video_routes.get("/api/download/video/<filename>")
def video_download(filename):
    return serve_video(filename, download=True)
