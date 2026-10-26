# 棒球情境分析後端（可行性驗證 Demo）

以中華職棒歷史資料為基礎，驗證「原始資料 → 統計 → 情境查詢 API」這條流程是否可行。
輸入比賽情境（出局數、壘況，另附局數與分差作顯示），回傳該狀態的歷史得分期望值
（Run Expectancy, RE）、樣本數與打席結果分布。

> 本專案目標是**確認開發可行性**，不是完整的戰術決策系統。所有數字皆為歷史統計，
> 不是因果推論，也不是戰術建議。

## 資料來源與授權

- 資料：[Rebas 野球革命 Open Data](https://github.com/rebas-tw/rebas.tw-open-data)
  （台灣棒球進階資料庫，原始數據共享計劃）。
- 授權：ODC-By。使用、公開或展示時須標註資料來源。
- 範圍：2023～2025 年中華職棒**例行賽**，共 1,020 場
  （2023：300 場、2024：360 場、2025：360 場）。季後賽與冠軍賽未納入。
- 原始 JSON 與衍生 CSV 因檔案大小與授權考量，**不放進 Git**（見 `.gitignore`），
  請依下方步驟自行放入與重建。

## 環境與安裝

Windows、PowerShell、VS Code、Python 3.14（開發環境）。

```
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## 使用流程

1. 將原始 JSON 放入 `data\raw\`（可含年度子資料夾）。
2. 依序執行：

```
python src\data_processor.py     # JSON → 逐打席 CSV 與匯入報告
python src\re_engine.py          # 逐打席 CSV → 24 格 RE 與逐打席觀察值
python src\scenario.py           # 單一狀態的情境分析（預設 0 出局、一壘有人）
```

`scenario.py` 可指定狀態，例如 `python src\scenario.py --outs 2 --bases 5`。

3. 啟動 API：

```
python -m uvicorn main:app --app-dir src
```

開啟 http://127.0.0.1:8000/docs 操作。若要開發時自動重載，加上
`--reload --reload-dir src`（只監看 `src`，避免被 `.venv` 的變動觸發重啟）。

4. 執行測試：

```
python -m pytest tests -v
```

## 專案結構

| 路徑 | 說明 |
|---|---|
| `src\data_processor.py` | 讀取原始 JSON、展開成逐打席表、分類紀錄型態、品質檢查、輸出主 CSV 與匯入報告 |
| `src\re_engine.py` | 計算 24 格 RE（三季合併與各季）、與來源對照，並輸出逐打席觀察值 |
| `src\scenario.py` | 指定 (出局數, 壘況) 的情境分析；CLI 與 API 共用 `analyze()` |
| `src\main.py` | FastAPI 入口 |
| `tests\test_api.py` | API 固定測試案例 |
| `data\raw\` | 原始 JSON（不可修改） |
| `data\processed\plate_appearances_all.csv` | 逐打席主資料 |
| `data\processed\re_table.csv` | 24 格 RE 主表（三季合併） |
| `data\processed\re_by_season.csv` | 各季 RE 並排比較 |
| `data\processed\re_observations.csv` | 逐打席觀察值（含結果、剩餘得分、下一狀態） |
| `data\processed\import_reports\` | 匯入稽核：逐檔狀態、總摘要、需人工確認清單 |
| `data\processed\scenario_*\` | `scenario.py` 的輸出 |

## 資料規則與統計口徑

### 壘況編碼

`bases` 為數字編碼：一壘 +1、二壘 +2、三壘 +4。
0 無人、1 一壘、2 二壘、3 一二壘、4 三壘、5 一三壘、6 二三壘、7 滿壘。

### 資料處理（data_processor）

- `game_id = seasonId-seq`。同一 `game_id` 出現在多個檔案視為錯誤，全部排除並停止匯出。
- 有失敗檔案或重複 ID 時預設不匯出（`--allow-partial` 才匯出）；寫檔先寫暫存檔再取代。
- 原始 `result`、`RE`、`RE24`、`WPA` 等欄位一律不覆寫、不補值。
- `record_type` 分類：

| 類型 | 說明 |
|---|---|
| `plate_appearance` | 一般打席 |
| `tiebreak_runner_placeholder` | 突破僵局制的占位紀錄（第 10 局起，2023 無） |
| `substitution_only` | 只有換人事件 |
| `baserunning_inning_end` | 2 出局時因盜壘失敗、牽制出局等跑壘出局結束半局，`result` 為空 |
| `game_ended_mid_pa` | 比賽在打席途中結束 |

### 兩個已驗證的語意陷阱

- `endBases` 不可靠：半局內相鄰打席約 6% 對不上（多為安打、全壘打、保送）。
  狀態轉移一律改用「下一個真實打席的打席前 `outs`、`bases`」。
- `endAwayScores`／`endHomeScores` 可能落後於跑者得分。半局最終得分改取
  「下一半局第一個真實打席的打席前比分」。

### RE 的定義

- 觀察單位：真實打席開始前的狀態 (出局數, 壘況)。排除占位紀錄與純換人紀錄。
- 剩餘得分 = 半局最終得分 − 該打席打席前的進攻方得分。
- 僅納入打滿 3 出局的半局；再見安打與縮短或中斷的比賽（88 個半局）排除。
- 因跑壘出局而中斷的打席（`baserunning_inning_end`）**納入**：該打席開始前的狀態是真實發生過的，
  且其後續得分為 0；排除它們會讓兩出局的 RE 偏高。
- 延長賽納入。以 2025 資料檢驗，排除延長賽對 24 格 RE 的影響最大 0.032、平均 0.007，
  在標準誤內。
- 對外標示為「2023～2025 聯盟平均」。

### 三個剩餘得分指標（務必分開看）

| 指標 | 意義 |
|---|---|
| `runs_remaining` | 從**打席開始前**算起的剩餘得分（RE 為其平均）；比較不同結果或戰術時用這個 |
| `runs_on_play` | 打席本身造成的得分 |
| `runs_after` | 從**打席結束後**算起的剩餘得分，不含該打席的得分 |

`runs_remaining = runs_on_play + runs_after`。

### 打席結果大類（scenario.py）

安打：`1B 2B 3B HR IHR`；保送／觸身：`uBB IBB HBP`；一般出局：`GO FO FC SF`；
三振：`SO`；雙殺／三殺：`GIDP DP TP`；犧牲觸擊：`SH SH_FC`；
失誤：`E SH_E SF_E GIDP_E`；結果為空者另列「無打席結果」；其餘歸「其他」。

### 官方文件未定義的代碼

- 資料中出現 `SH_E`、`SH_FC`、`SF_E`、`GIDP_E`，官方 `result` 清單未列出，
  依後綴推測為「帶失誤」、「帶野手選擇」的變體，本專案照資料原值使用。
- 資料中另有 3 筆 `result = H`（CPBL-2024-xa-115、-219、-237），官方未定義。
  三筆皆為出局數不變、打者上壘，推測為安打簡寫，但**未經證實**。
  處理方式：保留原值，分類時歸入「其他」。

## 結果快照（已驗證）

- 匯入：1,020 場、77,956 列（一般打席 77,490）；失敗檔案 0、重複 `game_id` 0、需人工確認 0。
- RE：77,790 筆觀察值中納入 77,417 筆（約 0.5% 因不完整半局排除）；24 格樣本最少 187。
- 例：0 出局、一壘有人，樣本 4,714、RE 0.810（標準誤 0.019）；
  各季 0.823（2023）、0.854（2024）、0.749（2025）。
- 與來源對照：以資料內 `RE` 欄位（最後 90 天平均）為對照，本專案 24 格全部較低，
  平均低約 0.066（比值約 0.906）。成因**尚未驗證**；推測與來源累積了更早、得分較高的賽季有關，
  需更早賽季資料才能確認。

## API

| 端點 | 說明 |
|---|---|
| `GET /` | 資料與口徑說明 |
| `GET /re-table` | 24 格 RE 主表 |
| `POST /scenario` | 輸入情境，回傳 RE、樣本數、各季值、結果分布、下一狀態、觸擊比較、白話解釋與限制 |

`POST /scenario` 範例輸入：

```json
{
  "inning": 8, "score_diff": -1, "outs": 0,
  "runner_on_first": true, "runner_on_second": false, "runner_on_third": false
}
```

- 壘況以三個布林值輸入，程式自行換算成 0～7 編碼。
- **`inning` 與 `score_diff`（我方得分 − 對手得分）僅作情境顯示，不參與計算**；
  回應的 `context.used_in_calculation` 為 `false`。統計只依出局數與壘況。

## 已知限制

- 統計不依局數、分差、打者、投手、球場分組；樣本數足夠的分組分析屬後續工作。
- 歷史相關，非因果。例如觸擊與否受分差、局數與打者能力影響（選擇偏誤），
  「有觸擊代碼」與「未偵測到」的差異不能解讀為觸擊的好壞。
- `BUNT` 投球代碼不等於犧牲觸擊成功，請對照 `bunt_attempt_results`。
- 標準誤假設觀察值獨立；同一半局內的打席實際相關，標準誤略為低估。
- 組別樣本數 < 30 標示「資料不足」，不下結論。
- 部分欄位為人工記錄，進階欄位（球速、落點等）可能缺漏；本專案尚未使用。
- 本專案尚未展開 `event`、`runner` 層，盜壘與觸擊的事件級分析屬後續工作。
- 與 Rebas 公布值有系統性差距（見上），RE 僅應視為本資料範圍內的估計。

## 驗證狀態

- `data_processor.py` 可由原始 JSON 完全重現 `plate_appearances_all.csv`。
- `re_engine.py` 的輸出可完全重現。
- `tests\test_api.py` 7 項測試通過（固定案例：0 出局一壘有人、樣本不足、輸入驗證等）。
- 尚無 `data_processor.py` 與 `re_engine.py` 的單元測試。