# Server YAML、Docker 與圖庫重建

## YAML 設定

複製範例並限制檔案權限：

```bash
cp config.example.yaml config.yaml
chmod 600 config.yaml
```

將原本 `cookie.sh` 的 `NH_COOKIE`、`NH_USER_AGENT` 值填入 `auth.cookie` 與
`auth.user_agent`。Cookie 應使用引號包住；`config.yaml` 已被 `.gitignore` 排除。

設定優先序為：命令列或既有 `NH_*` 環境變數、YAML、程式預設。若沒有 YAML auth，程式仍會讀取專案根目錄的
`cookie.sh`，因此原本的直接執行方式不會失效。

Arch Linux 的直接執行模式需要 YAML 套件：

```bash
sudo pacman -S --needed python-yaml
python3 server/nh_server.py --config config.yaml
```

也可在 Python virtual environment 安裝 `requirements-server.txt`。

## Docker server

Docker image 不會 COPY `config.yaml` 或 `cookie.sh`。Compose 將主機的設定檔唯讀掛入容器，並把
`~/nh` 掛到相同的絕對路徑，因此同一份 `config.yaml` 可供直接執行與容器使用。

先停止直接執行的 systemd 服務，避免兩個 process 同時使用相同埠與 SQLite：

```bash
sudo systemctl stop nh-downloader.service
docker compose up --build -d nh-server
docker compose ps
docker compose logs -f nh-server
```

Compose 使用 Linux host networking，既有 nginx `127.0.0.1:8766` upstream 不必修改，也不會遇到不固定的
Docker bridge subnet。下載 API 仍由 server 的 `allowed_networks` 驗證來源。

若主機使用者不是 UID/GID 1000，啟動時指定：

```bash
NH_UID=$(id -u) NH_GID=$(id -g) docker compose up --build -d nh-server
```

切回直接執行：

```bash
docker compose down
sudo systemctl start nh-downloader.service
```

## 只靠 library.sqlite3 重建圖庫

重建命令會讀取舊資料庫中的所有 gallery ID，依序重新下載；目標已有 CBZ 時不重抓。已下架、Cloudflare
阻擋或其他下載失敗會寫入報告並繼續下一本。成功項目使用新下載取得的 metadata，但恢復舊資料庫的
`downloaded_at`；若新 metadata 不完整，會沿用舊資料庫的標題與 taxonomy。

直接執行：

```bash
python3 server/nh_server.py --config config.yaml \
  --restore-library-db /path/to/old/library.sqlite3
```

Docker 執行；來源資料庫必須使用絕對路徑並唯讀掛載：

```bash
docker compose run --rm \
  -v /absolute/path/library.sqlite3:/restore/library.sqlite3:ro \
  nh-server --config /app/config.yaml \
  --restore-library-db /restore/library.sqlite3
```

預設報告寫到目標 `~/nh/.nh-local/restore-report.json`，包含 `downloaded`、`already_present` 和帶錯誤原因的
`failed`。重建可安全中斷後重跑：已存在的 CBZ 會略過，舊下載時間仍會重新套用。也可用
`--restore-report /path/report.json` 指定報告位置。

執行重建時不要同時啟動正式 server，避免兩個 process 同時修改同一個圖庫資料庫。
