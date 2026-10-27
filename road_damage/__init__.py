from flask import Flask, jsonify, request
from werkzeug.exceptions import HTTPException

from config import Config
from .detector import Detector
from .routes import routes
from .video import VideoJobs
from .video_routes import video_routes


def create_app(config=None, detector=None):
    app = Flask(__name__, template_folder="../templates", static_folder="../static")
    app.config.from_object(Config)
    if config:
        app.config.update(config)
    app.json.sort_keys = False
    app.config["RESULT_DIR"].mkdir(parents=True, exist_ok=True)
    app.extensions["detector"] = detector if detector is not None else Detector(
        app.config["MODEL_PATH"], app.config["CONFIDENCE"]
    )
    app.extensions["video_jobs"] = VideoJobs(app.extensions["detector"], app.config)
    app.register_blueprint(routes)
    app.register_blueprint(video_routes)

    @app.errorhandler(HTTPException)
    def http_error(error):
        messages = {
            413: "YouTube 請求超過 4 KB 限制。" if request.endpoint == "video_routes.submit_youtube"
            else f"上傳內容超過 {request.max_content_length // (1024 * 1024)} MB 限制。",
            404: "找不到資源，結果可能已過期。",
            405: "不支援此 HTTP 方法。",
        }
        return jsonify(success=False, error={
            "code": error.name.lower().replace(" ", "_"),
            "message": messages.get(error.code, error.description),
        }), error.code

    @app.errorhandler(Exception)
    def unexpected_error(error):
        app.logger.exception("Unhandled request failure")
        return jsonify(success=False, error={
            "code": "internal_error", "message": "處理失敗，請重試並查看伺服器日誌。"
        }), 500

    return app
