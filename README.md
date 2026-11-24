# 保險表格查詢（第一版：AIA、周大福人壽）

每天早上 6:17（香港／台北時間）自動到官網檢查表格。
有改版、新增或下架時，發 Telegram 通知；業務用手機打開網站就能找到最新版。

## 檔案說明

| 檔案 | 用途 |
|---|---|
| `scraper/scrape.py` | 抓取程式（不用改） |
| `scraper/sources.json` | 要抓哪幾家公司、哪些網頁（之後加公司就改這裡） |
| `docs/` | 業務看到的網站 |
| `docs/forms.json` | 表格清單，程式每天自動更新 |
| `.github/workflows/update.yml` | 每天自動執行的排程 |

## 設定步驟

### 1. 建立 Repository
1. 登入 GitHub，按右上角「+」→「New repository」
2. Repository name 填 `insurance-forms`
3. 選 **Public**（免費帳號要公開才能用網站功能；裡面只有官網公開表格的清單，沒有機密資料）
4. 按「Create repository」

### 2. 上傳檔案
1. 在新的 repository 頁面，按「uploading an existing file」
2. 把解壓縮後的 `scraper` 和 `docs` 兩個資料夾，以及 `README.md`，一起拖進去
3. 按最下面的「Commit changes」

### 3. 建立排程檔（`.github` 是隱藏資料夾，拖不進去，要手動建）
1. 按「Add file」→「Create new file」
2. 檔名欄位輸入：`.github/workflows/update.yml`（打到 `/` 時會自動變成資料夾）
3. 用記事本打開壓縮檔裡的 `update.yml`，全部複製貼上
4. 按「Commit changes」

### 4. 開啟網站
1. 進入 repository 的「Settings」→ 左邊「Pages」
2. Source 選「Deploy from a branch」
3. Branch 選 `main`，資料夾選 `/docs`，按「Save」

### 5. 第一次執行
1. 按上方「Actions」分頁
2. 左邊點「每日更新表格」→ 右邊「Run workflow」→「Run workflow」
3. 等 3～5 分鐘，出現綠色勾勾就是成功；紅色叉叉就點進去，把錯誤畫面截圖給 Claude

### 6. 給業務使用
網址：`https://你的GitHub使用者名稱.github.io/insurance-forms/`

- **iPhone／iPad**：用 Safari 打開 →「分享」→「加入主畫面」
- **Android**：用 Chrome 打開 → 右上「⋮」→「加到主畫面」

## Telegram 通知（之後再設定）
還沒設定也能正常運作，只是不會發通知。設定方式另外提供。
