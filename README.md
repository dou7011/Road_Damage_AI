# Road Damage AI

以 Flask、YOLO 與 OpenCV 建置的道路損壞辨識網站，可分析圖片、影片，以及使用者有權取得與處理的 YouTube 影片。模型為 Ultralytics YOLO11m（輸入 640 × 640），可辨識 12 種道路破損類別，詳見下方「辨識類別」。

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

## 部署到 Google Cloud Run

專案附有 [Dockerfile](Dockerfile) 與 [.dockerignore](.dockerignore)，可從專案根目錄建置並部署 Cloud Run service。先安裝並登入 [Google Cloud CLI](https://cloud.google.com/sdk/docs/install)，選擇已啟用計費的 Google Cloud 專案，並啟用 Cloud Run 與 Cloud Build API：

```bash
gcloud auth login
gcloud config set project YOUR_PROJECT_ID
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com
```

部署至台灣 `asia-east1` 區域：

```bash
gcloud run deploy road-damage-ai \
  --source . \
  --region asia-east1 \
  --allow-unauthenticated \
  --port 8080 \
  --memory 8Gi \
  --cpu 4 \
  --concurrency 8 \
  --min-instances 1 \
  --max-instances 1 \
  --timeout 3600 \
  --no-cpu-throttling \
  --set-env-vars ROAD_DAMAGE_RESULT_DIR=/tmp/road-damage-results,VIDEO_MAX_CONTENT_LENGTH=31457280,VIDEO_MAX_INTERMEDIATE_BYTES=1073741824,VIDEO_MIN_FREE_BYTES=536870912
```

部署完成後，`gcloud` 會輸出服務網址。`--allow-unauthenticated` 會公開網站；若只供授權使用者存取，請移除該旗標並設定 Cloud Run IAM。首次部署需由 Cloud Build 建置映像，並由 Cloud Run 使用 `PORT` 環境變數啟動 Gunicorn。

**目前 Cloud Run 部署的範圍與限制：**

- 設定為單一最小／最大實例，是因工作佇列、模型與影片工作狀態位於單一 Python 程序。單一 worker 避免重複載入模型；固定一個實例避免不同實例看不到彼此的工作與暫存結果。
- 背景影片工作需要持續 CPU，因此部署指令使用 `--no-cpu-throttling` 並維持至少一個實例。這會增加閒置費用；請在 Cloud Billing 設定預算與告警。
- Cloud Run 的容器檔案系統與記憶體中的工作狀態不是持久儲存。重新部署、實例重啟或平台回收後，執行中的工作及已產生的結果可能遺失。這個單實例設定適合展示／小規模使用，**不提供工作持久性、高可用或多實例擴展**；正式服務應將上傳與結果移至 Cloud Storage，並把工作狀態移至持久化資料庫／工作佇列。
- 此部署將影片上傳限制設為 30 MiB（`VIDEO_MAX_CONTENT_LENGTH`），低於 Cloud Run HTTP/1 請求大小限制；不支援直接透過此 API 上傳原先設定的 256 MiB 影片。較大的來源需改用 Cloud Storage 上傳流程。
- 影片中間檔上限設為 1 GiB，暫存檔寫入 `/tmp`；預設 8 GiB 記憶體是為模型與暫存處理預留空間，仍請依實際影片及帳單需求調整。長片或高解析度影片可能先碰到暫存檔上限。

部署參數亦可依需求調整；降低 `--min` 到 0 可能使背景工作在實例回收時中斷。部署前請確認你有權將 `best.pt` 模型權重及專案依賴部署至 Google Cloud。

## 模型設定

預設模型位置依序為：

1. 環境變數 `YOLO_MODEL_PATH` 指定的路徑（若有設定；舊名稱 `ROAD_DAMAGE_MODEL` 仍可用）。
2. 專案根目錄的 `best.pt`。

將訓練好的權重（約 40.5 MB）複製為專案根目錄的 `best.pt` 即可；若放在其他位置，請設定 `YOLO_MODEL_PATH`（可參考 [.env.example](.env.example)，不要把個人電腦的絕對路徑寫進程式或提交到 Git）：

```bash
export YOLO_MODEL_PATH="/path/to/best.pt"
```

```powershell
$env:YOLO_MODEL_PATH = "C:\path\to\best.pt"
```

模型必須是 12 類的 YOLO 偵測模型（類別代碼見下表）。找不到模型、權重損毀或類別不符時，應用程式會在啟動時停止，並在終端機顯示明確的錯誤訊息，不會回傳看似成功的空結果。使用 CUDA 時會優先嘗試 GPU；GPU 初始化或推論失敗時改用 CPU。

### 模型資訊

| 項目 | 數值 |
|---|---|
| 架構 | Ultralytics YOLO11m，輸入 640 × 640，12 類 |
| 驗證集 | 7,677 張 |
| Precision / Recall | 64.9% / 63.8% |
| mAP50 / mAP50-95 | 66.3% / 38.6% |

以上是模型在驗證集上的整體評估結果，不是單張圖片的辨識準確率；偵測框的 confidence 也只是單一偵測的信心分數。

### 辨識類別

類別索引一律取自載入模型的 `model.names`，程式不會自行排列索引；`best.pt` 的實際對應為 0 D00、1 D10、2 D20、3 D40、4 D44、5 D50、6 Repair、7 D43、8 D01、9 D11、10 Block crack、11 D0w0。中文名稱只用於畫面顯示，統計與篩選皆使用原始類別代碼。對照表集中定義於 [road_damage/classes.py](road_damage/classes.py)，網頁由後端注入，不在前端重複定義。

| 代碼 | 顯示名稱 | 代碼 | 顯示名稱 |
|---|---|---|---|
| D00 | 縱向裂縫 | D43 | 斑馬線模糊 |
| D10 | 橫向裂縫 | D01 | 縱向裂縫（施工接縫） |
| D20 | 龜裂 | D11 | 橫向裂縫（施工接縫） |
| D40 | 坑洞 | Block crack | 區塊狀裂縫 |
| D44 | 白線模糊 | D0w0 | 其他道路破損 |
| D50 | 人孔蓋 | Repair | 修補痕跡 |

中文名稱依 RDD2018／RDD2022 標註定義；官方文件未說明 `D0w0`，故顯示為「其他道路破損（D0w0）」，確認含義後再於 `classes.py` 修改。

## 支援格式與限制

| 類型 | 支援格式 | 預設限制 |
|---|---|---|
| 圖片 | JPG、JPEG、PNG、WEBP | 單張請求 16 MB、最多 24,000,000 像素 |
| 影片 | MP4、AVI、MOV、MKV | 256 MB、10 分鐘、最多 36,000 幀、最高 3840 × 2160 |
| YouTube | HTTPS 單支影片網址 | 需授權、最多 10 分鐘、最高 720p；僅支援符合程式限制的可取得影片 |

圖片與影片結果預設暫存於 `instance/results/`，保存 24 小時，並在後續請求時清理；可用 `ROAD_DAMAGE_RESULT_DIR` 更改位置。影片輸出為 H.264 MP4；影片與 YouTube 共用單一背景工作名額。各項限制可在 [config.py](config.py) 調整。

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

- 根目錄 `best.pt`（約 40.5 MB）是預設啟動所需的模型權重。若不想把權重放進 GitHub，請執行 `git rm --cached best.pt` 並在 `.gitignore` 加入 `best.pt`，部署時再另行放置；`.gitignore` 已忽略 `models/` 與其他 `*.pt`。若保留提交，請確認你有權分享該模型。
- 虛擬環境、Python 快取、`.env`、IDE 個人設定、訓練輸出與 `instance/` 結果不應提交。
- 請勿提交 API 金鑰、密碼、私人影片或其他機密／未授權資料。

## 專案結構

```text
.
├── app.py                 # Flask 啟動入口
├── config.py              # 模型路徑（YOLO_MODEL_PATH）與服務限制
├── road_damage/           # 辨識、圖片／影片處理及 API
├── templates/             # Flask 頁面
├── static/                # CSS 與 JavaScript
├── tests/                 # 自動化測試
├── requirements.txt       # Python 依賴
├── .env.example           # 環境變數範例
└── best.pt                # 預設 YOLO11m 模型（12 類）
```
