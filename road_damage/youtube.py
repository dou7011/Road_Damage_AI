import http.client
import ipaddress
import json
import logging
import math
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time
from urllib.parse import parse_qs, urlencode, urlsplit

import certifi

from .video import VideoError

LOGGER = logging.getLogger(__name__)
VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")
FALLBACK = "請僅在平台允許且你有權使用時取得影片，再改用「影片上傳」；不提供登入或限制繞過。"


def canonical_url(value):
    if not isinstance(value, str) or len(value) > 2048 or any(ord(char) <= 32 for char in value):
        raise VideoError("請輸入有效的 HTTPS YouTube 影片網址。")
    try:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port is not None:
            raise ValueError
        host = parsed.hostname
        query = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=30)
        if host == "youtu.be":
            video_id = parsed.path.removeprefix("/")
        elif host in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
            if parsed.path == "/watch" and len(query.get("v", [])) == 1:
                video_id = query["v"][0]
            elif parsed.path.startswith("/shorts/"):
                video_id = parsed.path[len("/shorts/"):]
            else:
                raise ValueError
        else:
            raise ValueError
        if VIDEO_ID.fullmatch(video_id) is None:
            raise ValueError
    except ValueError as error:
        raise VideoError("只接受 youtube.com/watch、youtube.com/shorts 或 youtu.be 的單支影片，影片 ID 必須為 11 碼。") from error
    return "https://www.youtube.com/watch?" + urlencode({"v": video_id})


def metadata(url, config):
    command = [
        sys.executable, "-m", "yt_dlp", "--ignore-config", "--no-plugin-dirs",
        "--no-js-runtimes", "--no-remote-components", "--no-cache-dir",
        "--use-extractors", "youtube", "--no-playlist", "--skip-download",
        "--dump-single-json", "--no-warnings", "--socket-timeout", "10",
        "--retries", "0", "--extractor-retries", "0", "--proxy", "", "--", url,
    ]
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        try:
            process = subprocess.Popen(command, stdout=output, stderr=errors, shell=False)
        except OSError as error:
            raise VideoError("無法啟動 yt-dlp，請確認已安裝必要套件。" + FALLBACK) from error
        deadline = time.monotonic() + config["YOUTUBE_METADATA_TIMEOUT"]
        try:
            while process.poll() is None:
                if time.monotonic() >= deadline:
                    raise VideoError("YouTube 資訊取得逾時。" + FALLBACK)
                if max(os.fstat(output.fileno()).st_size, os.fstat(errors.fileno()).st_size) > config["YOUTUBE_MAX_METADATA_BYTES"]:
                    raise VideoError("YouTube 資訊超過大小限制。" + FALLBACK)
                time.sleep(.05)
            if max(os.fstat(output.fileno()).st_size, os.fstat(errors.fileno()).st_size) > config["YOUTUBE_MAX_METADATA_BYTES"]:
                raise VideoError("YouTube 資訊超過大小限制。" + FALLBACK)
            if process.returncode:
                errors.seek(0)
                diagnostic = errors.read(4096).decode("utf-8", errors="replace")
                # Never expose signed media URLs or raw platform output to clients.
                LOGGER.warning("YouTube metadata failed (exit=%s)", process.returncode)
                lower = diagnostic.lower()
                if any(word in lower for word in ("private", "sign in", "age", "members", "premium", "login")):
                    reason = "影片需要登入、年齡確認、會員或其他授權，無法處理。"
                elif any(word in lower for word in ("unavailable", "removed", "country", "copyright")):
                    reason = "影片不存在、已移除或受到地區／權利限制。"
                else:
                    reason = "YouTube 目前無法提供影片資料，可能為網路、平台限制或 yt-dlp 相容性問題。"
                raise VideoError(reason + FALLBACK)
            output.seek(0)
            try:
                info = json.load(output)
            except (ValueError, UnicodeError) as error:
                raise VideoError("YouTube 回傳的資訊無法解析。" + FALLBACK) from error
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
    if not isinstance(info, dict):
        raise VideoError("YouTube 未回傳單支影片資訊。" + FALLBACK)
    return info


def choose_format(info, config, expected_id):
    if info.get("id") != expected_id or info.get("_type", "video") != "video":
        raise VideoError("YouTube 回傳的影片 ID 或類型不符。")
    if info.get("availability") != "public" or info.get("age_limit", 0) != 0:
        raise VideoError("只接受無需登入且無年齡限制的公開影片。" + FALLBACK)
    if info.get("is_live") or info.get("live_status") not in {None, "not_live"}:
        raise VideoError("不支援直播、即將開始或直播回放，請使用一般公開影片。")
    if info.get("has_drm"):
        raise VideoError("不支援受 DRM 保護的影片。")
    duration = info.get("duration")
    if not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration <= 0:
        raise VideoError("無法取得影片長度，請改用較短的一般公開影片，或使用「影片上傳」。")
    if duration > config["VIDEO_MAX_SECONDS"]:
        minutes, seconds = divmod(int(duration), 60)
        raise VideoError(
            f"這支影片長 {minutes} 分 {seconds} 秒，超過 {config['VIDEO_MAX_SECONDS'] // 60} 分鐘上限。"
            "請改用較短的影片，或先剪輯後以「影片上傳」分析。"
        )
    candidates = []
    for item in info.get("formats", []):
        height, width = item.get("height"), item.get("width")
        if (
            item.get("ext") != "mp4" or item.get("protocol") != "https"
            or item.get("vcodec", "none") == "none" or item.get("has_drm")
            or not isinstance(height, (int, float)) or not 0 < height <= config["YOUTUBE_MAX_HEIGHT"]
            or not isinstance(width, (int, float)) or not 0 < width <= 1280
        ):
            continue
        size = item.get("filesize") or item.get("filesize_approx")
        if size is not None and (not isinstance(size, (int, float)) or not math.isfinite(size)
                                 or not 0 < size <= config["VIDEO_MAX_CONTENT_LENGTH"]):
            continue
        try:
            media_host(item.get("url"))
        except VideoError:
            continue
        candidates.append(item)
    if not candidates:
        raise VideoError("沒有符合限制的 HTTPS 單檔 MP4（最高 720p）；不處理分段串流或解鎖受限格式。" + FALLBACK)
    return max(candidates, key=lambda item: (item["height"], item.get("acodec") != "none", item.get("tbr") or 0))


def media_host(url):
    if not isinstance(url, str) or len(url) > 16384 or any(ord(char) <= 32 for char in url):
        raise VideoError("影片下載網址不合法。")
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        if (parsed.scheme != "https" or parsed.username or parsed.password or parsed.port is not None
                or not host.endswith(".googlevideo.com") or not re.fullmatch(r"[a-z0-9.-]+", host)):
            raise ValueError
    except ValueError as error:
        raise VideoError("影片來源不在允許的 YouTube CDN 範圍。") from error
    return parsed


class PublicHTTPS(http.client.HTTPSConnection):
    def connect(self):
        addresses = socket.getaddrinfo(self.host, 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
            raise VideoError("影片來源解析到非公開 IP，已拒絕連線。")
        # Connect to the checked address, retaining the hostname for TLS/SNI validation.
        sock = socket.create_connection((addresses[0][4][0], 443), timeout=self.timeout)
        try:
            context = ssl.create_default_context()
            context.load_verify_locations(cafile=certifi.where())
            self.sock = context.wrap_socket(sock, server_hostname=self.host)
        except Exception:
            sock.close()
            raise


def download(url, target, config, update):
    parsed = media_host(url)
    limit = config["VIDEO_MAX_CONTENT_LENGTH"]
    deadline = time.monotonic() + config["YOUTUBE_DOWNLOAD_TIMEOUT"]
    connection = PublicHTTPS(parsed.hostname, timeout=10)
    try:
        connection.request("GET", parsed.path + ("?" + parsed.query if parsed.query else ""),
                           headers={"User-Agent": "Mozilla/5.0", "Accept-Encoding": "identity"})
        response = connection.getresponse()
        # Never follow a redirect, even one supplied by an allowlisted server.
        if response.status != 200:
            raise VideoError(f"YouTube 影片下載被拒絕（HTTP {response.status}）。" + FALLBACK)
        length = response.getheader("Content-Length")
        if length is not None:
            try:
                length = int(length)
            except ValueError as error:
                raise VideoError("下載內容長度無效。") from error
            if not 0 < length <= limit:
                raise VideoError("YouTube 影片超過 256 MB 或內容為空。")
        count = 0
        with target.open("wb") as stream:
            while True:
                if time.monotonic() >= deadline:
                    raise VideoError("YouTube 影片下載逾時。" + FALLBACK)
                chunk = response.read(64 * 1024)
                if not chunk:
                    break
                count += len(chunk)
                if count > limit:
                    raise VideoError("YouTube 影片下載超過 256 MB 限制。")
                if shutil.disk_usage(target.parent).free < config["VIDEO_MIN_FREE_BYTES"]:
                    raise VideoError("下載途中磁碟空間不足。")
                stream.write(chunk)
                update(downloaded_bytes=count, download_total_bytes=length)
        if count == 0 or (length is not None and count != length):
            raise VideoError("YouTube 影片下載不完整。" + FALLBACK)
    except ssl.SSLCertVerificationError as error:
        raise VideoError(
            "YouTube 影片 HTTPS 憑證驗證失敗，請更新 certifi CA 憑證套件並檢查網路的憑證設定。"
            + FALLBACK
        ) from error
    except (OSError, http.client.HTTPException) as error:
        raise VideoError("YouTube 影片網路連線或下載失敗。" + FALLBACK) from error
    finally:
        connection.close()


def acquire(url, target, config, update):
    canonical = canonical_url(url)
    update(status="fetching_info", source_type="youtube")
    info = metadata(canonical, config)
    selected = choose_format(info, config, canonical.rsplit("=", 1)[1])
    update(status="downloading", source_info={
        "video_id": info["id"], "url": canonical,
        "title": str(info.get("title") or "未提供標題")[:500],
        "channel": str(info.get("channel") or "未提供頻道")[:200],
        "duration": info["duration"], "height": selected["height"], "width": selected["width"],
    }, downloaded_bytes=0, download_total_bytes=None)
    download(selected["url"], target, config, update)
    update(status="validating", input_info={"name": info["id"] + ".mp4", "size_bytes": target.stat().st_size})
