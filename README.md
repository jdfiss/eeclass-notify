# eeclass → Telegram 通知

自動登入[中央大學 eeclass](https://ncueeclass.ncu.edu.tw/dashboard)，把作業期限、新公告、新教材整理成 Telegram 訊息傳給你。用 GitHub Actions 跑，**免費、不用開電腦**。

- 📰 **早報**：每天 7 點後第一次執行，傳完整整理（今日重點、7 天內所有期限）
- 🔔 **其他時段有新東西才傳**，沒有就不打擾（台灣時間 07:00～00:00 每小時檢查）
- ⏰ **作業截止前 3 小時提醒一次**；期限顏色 🔴 今明天、🟠 3 天內、🟢 7 天內
- 🆕 新作業／考試、📢 新公告（附內容摘要）、📂 新教材、💬 新討論，依課程分組
- 📎 **附件直接傳到 TG**：新公告的附件、新教材的檔案（每次最多 8 個、單檔 50 MB 以內）
- 🤖 **AI 摘要（選用、免費）**：用 Google Gemini 免費額度幫公告寫一句話重點

## 🔒 安全性：先看這段

這個工具需要你的 eeclass 帳號密碼才能幫你登入，所以請照下面的方式用：

1. **帳密只存在你自己 repo 的 GitHub Secrets 裡。** Secrets 是加密儲存的，設定後連你自己都看不到內容，log 裡出現也會自動變成 `***`。作者和其他人都拿不到。**不要把帳密寫進任何檔案或 commit。**
2. **你的 repo 一定要是 Private。** 公開 repo 的 Actions 執行紀錄、`state/seen.json` 任何人都看得到，會洩漏你修了哪些課。workflow 偵測到 repo 是 public 時會直接拒絕執行。
3. **不要把 repo 的寫入權限給別人。** 有寫入權限的人可以改 workflow 把 Secrets 印出來。
4. 程式只會連到：eeclass、Telegram API，以及（有設定的話）Google Gemini API。可以自己看 `eeclass_notify.py` 確認，全部約 700 行。
5. 不想用了：刪掉 repo 就好，Secrets 會一起刪除。擔心的話順便改一次 eeclass 密碼。

> 程式預設**不會**把 eeclass 頁面內容印到 log。登入失敗要除錯時，才在 Variables 設 `DEBUG=1`（會記錄頁面文字和截圖，含姓名學號），除完錯記得刪掉。

## 設定步驟（約 10 分鐘）

### 1. 建立你自己的 private repo
在這個頁面右上角按 **Use this template → Create a new repository**，**Visibility 選 Private**。

> 不要用 Fork：fork 公開 repo 只能是 public。

### 2. 建一個 Telegram bot
1. 在 Telegram 找 **@BotFather**，傳 `/newbot`，照指示取名，拿到 **bot token**（長得像 `123456:ABC-...`）。
2. 找你剛建好的 bot，按 **Start**，隨便傳一句話給它（不傳的話 bot 不能主動傳訊息給你）。
3. 找 **@userinfobot** 傳任何訊息，它會回你的 **chat id**（一串數字）。

> 想傳到群組：把 bot 加進群組，chat id 用群組的 ID（負數）。

### 3. 設定 Secrets
到你的 repo **Settings → Secrets and variables → Actions → Secrets → New repository secret**，新增：

| 名稱 | 內容 | 必填 |
| --- | --- | --- |
| `EECLASS_ACCOUNT` | eeclass 帳號（學號） | ✅ |
| `EECLASS_PASSWORD` | eeclass 密碼 | ✅ |
| `TG_BOT_TOKEN` | 步驟 2 的 bot token | ✅ |
| `TG_CHAT_ID` | 步驟 2 的 chat id | ✅ |
| `GEMINI_API_KEY` | AI 摘要用，見下方 | 選用 |

### 4. 手動跑一次
到 **Actions** 分頁（第一次要按綠色按鈕啟用 workflows）→ **eeclass notify → Run workflow**。

第一次會傳目前的作業期限，並開始記錄現有的公告和教材，之後有新的才會通知。之後每小時會自動執行。

### （選用）AI 摘要
1. 用 Google 帳號登入 <https://aistudio.google.com/apikey>，按 **Create API key**，複製金鑰（`AIza` 開頭）。不用綁信用卡。
2. 新增 Secret `GEMINI_API_KEY`，貼上金鑰。
3. 手動 Run workflow 並勾「傳完整早報」，訊息開頭出現「🤖 今日重點」就成功了。

> 開啟後，新公告內文和附件（PDF、PPTX）的文字會送到 Google Gemini 產生摘要。不想讓課程內容送到第三方就不要設定。

## 可調整的設定（Variables，都是選用）
**Settings → Secrets and variables → Actions → Variables → New repository variable**：

| 名稱 | 說明 | 預設 |
| --- | --- | --- |
| `DUE_DAYS` | 早報列出幾天內截止的作業 | `7` |
| `EECLASS_BASE` | eeclass 網址（其他學校用同一套系統可以試試） | `https://ncueeclass.ncu.edu.tw` |
| `AI_MODEL` | 指定 Gemini 模型 | 自動挑 |
| `DEBUG` | 設 `1` 會把頁面文字、截圖記到 log／artifact，除錯用 | 關 |

## 運作方式
- 用 Playwright 開無頭瀏覽器登入，讀 eeclass「最近事件、最新公告、最新教材、最新討論」。
- 帳號在其他裝置登入時，會自動按「保持登入」，不會把你其他裝置登出。
- 看過的項目記在 `state/seen.json`，由 Actions 自動 commit 回你的 repo。
- 執行失敗時會傳 ⚠️ 訊息到 Telegram。
- 一天 18 次、一次約 1 分鐘，一個月約 540 分鐘，在 private repo 每月 2000 分鐘免費額度內。
- 台灣時間 1～7 點的執行會直接結束，不登入也不傳訊息。

## 常見問題
- **排程沒有自動跑／常常晚很久**：GitHub 內建排程本來就不準，有時整段跳過。想準時可以用 [cron-job.org](https://cron-job.org) 之類的服務每小時呼叫 [workflow_dispatch API](https://docs.github.com/en/rest/actions/workflows#create-a-workflow-dispatch-event)；token 請用 fine-grained PAT，只給**這一個 repo** 的 **Actions: Read and write** 權限。
- **登入失敗**：先確認帳密正確。若 eeclass 出現驗證碼或擋國外 IP（GitHub 機器在美國），可以改在自己電腦上排程執行：
  ```bash
  pip install -r requirements.txt && python -m playwright install chromium
  EECLASS_ACCOUNT=... EECLASS_PASSWORD=... TG_BOT_TOKEN=... TG_CHAT_ID=... python eeclass_notify.py
  ```
  本機執行時，帳密請放在環境變數或不會被 commit 的 `.env`，不要寫進程式。
- **想看排版效果**：Run workflow 時勾「把目前所有公告、教材重傳一次」。

## 免責聲明
這是學生自製的非官方工具，與國立中央大學、eeclass（旭聯科技）無關。請自行評估使用風險，並遵守學校的資訊使用規範。

## License
MIT
