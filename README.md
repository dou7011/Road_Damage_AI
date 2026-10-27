# Road Damage AI

以 Flask、YOLO 與 OpenCV 建置的道路損壞辨識網站，可分析圖片、影片，以及使用者有權取得與處理的 YouTube 影片。模型可辨識四種道路損壞：縱向裂縫、橫向裂縫、龜裂與坑洞。

## 功能

- **圖片辨識：** 一次選取多張圖片，檢視原圖與標記結果，下載 PNG、JSON 或 CSV。
- **影片辨識：** 上傳影片並查看逐幀辨識進度、標記影片、統計與 JSON／CSV 報告。
- **YouTube 影片：** 透過 HTTPS 網址分析符合限制且已獲授權的影片。
- **本機處理：** Flask 網站預設只監聽 `127.0.0.1`，不需資料庫或前端框架。

## 系統需求

- Python 3.12（其他 Python 版本及 CUDA 組合尚未全部驗證）
- Git
- 模型權重檔 `best.pt`，或自行設定其他相容模型路徑

## 安裝與啟動

在專案根目錄執行以下指令。

### macOS / Linux

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python app.py
```

### Windows PowerShell

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python app.py
```

啟動後開啟 <http://127.0.0.1:5000>。按 `Ctrl+C` 停止伺服器。

## 模型設定

預設模型位置依序為：

1. 環境變數 `ROAD_DAMAGE_MODEL` 指定的路徑（若有設定）。
2. `runs/road_damage/yolo11n_rdd2022_100ep/weights/best.pt`（若存在）。
3. 專案根目錄的 `best.pt`。

若使用其他位置的模型，請設定 `ROAD_DAMAGE_MODEL`：

```bash
export ROAD_DAMAGE_MODEL="/path/to/best.pt"
```

```powershell
$env:ROAD_DAMAGE_MODEL = "C:\path\to\best.pt"
```

模型必須是支援 D00、D10、D20、D40 四種類別的 YOLO 偵測模型。模型載入失敗時，應用程式會停止並在終端機顯示錯誤。使用 CUDA 時會優先嘗試 GPU；GPU 初始化或推論失敗時改用 CPU。

## 支援格式與限制

| 類型 | 支援格式 | 預設限制 |
|---|---|---|
| 圖片 | JPG、JPEG、PNG、WEBP | 單張請求 16 MB、最多 24,000,000 像素 |
| 影片 | MP4、AVI、MOV、MKV | 256 MB、10 分鐘、最多 36,000 幀、最高 3840 × 2160 |
| YouTube | HTTPS 單支影片網址 | 需授權、最多 10 分鐘、最高 720p；僅支援符合程式限制的可取得影片 |

圖片與影片結果暫存於 `instance/results/`，保存 24 小時，並在後續請求時清理。影片輸出為 H.264 MP4；影片與 YouTube 共用單一背景工作名額。各項限制可在 [config.py](config.py) 調整。

**YouTube 使用提醒：公開可觀看不代表有權下載。** 請只處理自己擁有或已獲授權，且符合平台條款與著作權規範的影片。程式不使用登入資訊、Cookies 或方式繞過 DRM、付費／地區限制及其他存取控制；YouTube 網址可能因平台或格式限制而無法處理。

**辨識結果僅供參考。** 模型可能誤判或漏判，未偵測到損壞不代表道路安全，也不能用來判定事故風險。

## API

| 方法 | 路徑 | 說明 |
|---|---|---|
| `GET` | `/api/health` | 查看模型載入狀態與辨識設定 |
| `POST` | `/api/detect/image` | 以 multipart 欄位 `image` 上傳圖片 |
| `POST` | `/api/detect/video/jobs` | 以 multipart 欄位 `video` 提交影片，回傳工作 ID |
| `POST` | `/api/detect/youtube` | 以 JSON `{ "url": "...", "authorized": true }` 提交 YouTube 網址 |
| `GET` | `/api/detect/video/jobs/<job_id>` | 查詢影片工作進度與結果 |
| `GET` | `/api/results/image/<result_id>/export/<json或csv>` | 下載圖片辨識報告 |
| `GET` | `/api/detect/video/jobs/<job_id>/export/<json或csv>` | 下載影片辨識報告 |

圖片辨識 API 會在請求完成後回傳結果。影片與 YouTube API 回傳 `202 Accepted` 表示已接受工作；請使用回應中的 `status_url` 查詢處理狀態。

## 測試

```bash
python -m compileall -q app.py config.py road_damage tests
python -m unittest discover -s tests -v
```

測試會使用目前設定的模型；部分整合測試需要模型權重及影片編碼所需元件。測試通過不代表模型的道路辨識準確率，也不保證 YouTube 平台允許取得任何特定影片。

## GitHub 上傳注意事項

- 根目錄 `best.pt` 是預設啟動所需的模型權重，`.gitignore` **不會忽略**它。上傳前請確認你有權分享該模型。
- 虛擬環境、Python 快取、`.env`、IDE 個人設定、訓練輸出與 `instance/` 結果不應提交。
- 請勿提交 API 金鑰、密碼、私人影片或其他機密／未授權資料。

## 專案結構

```text
.
├── app.py                 # Flask 啟動入口
├── config.py              # 模型路徑與服務限制
├── road_damage/           # 辨識、圖片／影片處理及 API
├── templates/             # Flask 頁面
├── static/                # CSS 與 JavaScript
├── tests/                 # 自動化測試
├── requirements.txt       # Python 依賴
└── best.pt                # 預設 YOLO 模型
```
