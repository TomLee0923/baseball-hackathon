import json
import numbers
from pathlib import Path

import pandas as pd


# =========================
# 1. 路徑設定
# =========================

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data" / "raw"
OUTPUT_DIR = PROJECT_ROOT / "data" / "processed"
OUTPUT_PATH = OUTPUT_DIR / "plate_appearances_all.csv"


# =========================
# 2. 基本工具
# =========================

def load_game_json(file_path):
    """讀取單場比賽 JSON，並檢查最外層格式。"""
    with open(file_path, "r", encoding="utf-8") as file:
        data = json.load(file)

    if not isinstance(data, dict):
        raise ValueError(f"JSON 最外層必須是字典：{file_path}")

    return data


def is_empty_result(value):
    """判斷打席結果是否為空或缺失。"""
    if value is None:
        return True

    if isinstance(value, str):
        return value.strip() == ""

    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def is_empty_list(value):
    """判斷欄位是否為空列表。"""
    return isinstance(value, list) and len(value) == 0


def as_int(value):
    """將整數或整數字串安全轉為 int；無法轉換則回傳 None。"""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
        if number.is_integer():
            return int(number)
    except (TypeError, ValueError):
        pass
    return None


def decode_bases(bases):
    """將 0～7 的壘況編碼轉成文字。"""
    base_names = {
        0: "無人在壘",
        1: "一壘有人",
        2: "二壘有人",
        3: "一、二壘有人",
        4: "三壘有人",
        5: "一、三壘有人",
        6: "二、三壘有人",
        7: "滿壘",
    }

    value = as_int(bases)
    return base_names.get(value, "未知壘況")


def serialize_json_field(value):
    """將 list/dict 以標準 JSON 字串存入 CSV；保留空值為空字串。"""
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)

    if value is None:
        return ""

    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass

    return value


# =========================
# 3. 特殊占位紀錄辨識
# =========================

def is_tiebreak_placeholder(pa_list, index):
    """
    辨識「高度疑似」突破僵局制占位紀錄。

    條件：
    1. 第 10 局或之後；
    2. result 為空，且 pitchCodes/events 都是空列表；
    3. 前一筆是前一局最後一筆打席，且 endOuts == 3；
    4. 下一筆是同一局的實際打席，且起始 bases == 2。

    注意：這是依目前資料觀察建立的規則，仍應以更多場次驗證。
    不以 batterName 相同作為必要條件，避免第三出局為跑者出局時漏判。
    """
    pa = pa_list[index]

    if not isinstance(pa, dict):
        return False

    if (as_int(pa.get("inning")) or 0) < 10:
        return False

    if not is_empty_result(pa.get("result")):
        return False

    if not is_empty_list(pa.get("pitchCodes")):
        return False

    if not is_empty_list(pa.get("events")):
        return False

    # 必須有前後紀錄才能交叉驗證
    if index == 0 or index + 1 >= len(pa_list):
        return False

    previous_pa = pa_list[index - 1]
    next_pa = pa_list[index + 1]

    if not isinstance(previous_pa, dict) or not isinstance(next_pa, dict):
        return False

    current_inning = as_int(pa.get("inning"))
    previous_inning = as_int(previous_pa.get("inning"))
    next_inning = as_int(next_pa.get("inning"))

    previous_is_last_out = (
        current_inning is not None
        and previous_inning == current_inning - 1
        and as_int(previous_pa.get("endOuts")) == 3
    )

    next_is_actual_pa_with_runner_on_second = (
        next_inning == current_inning
        and as_int(next_pa.get("bases")) == 2
        and not is_empty_result(next_pa.get("result"))
    )

    return (
        previous_is_last_out
        and next_is_actual_pa_with_runner_on_second
    )


# =========================
# 4. 整理單場比賽
# =========================

def extract_plate_appearances(game, source_file):
    """將單場比賽的客隊與主隊打席整理成同一份資料。"""
    rows = []

    season_id = game.get("seasonId", "")
    seq = game.get("seq", "")
    game_id = f"{season_id}-{seq}"

    game_metadata = {
        "game_id": game_id,
        "seasonId": game.get("seasonId"),
        "season": game.get("season"),
        "seq": game.get("seq"),
        "date": game.get("date"),
        "stadium": game.get("stadium"),
        "awayTeamId": game.get("awayTeamId"),
        "awayTeam": game.get("awayTeam"),
        "homeTeamId": game.get("homeTeamId"),
        "homeTeam": game.get("homeTeam"),
        "source_file": str(source_file),
    }

    for side, key in [
        ("away", "awayPAList"),
        ("home", "homePAList"),
    ]:
        pa_list = game.get(key, [])

        if not isinstance(pa_list, list):
            raise ValueError(f"{key} 必須是列表")

        for index, pa in enumerate(pa_list):
            if not isinstance(pa, dict):
                raise ValueError(
                    f"{source_file} 的 {key}[{index}] 不是字典"
                )

            row = pa.copy()
            row.update(game_metadata)
            row["team_side"] = side
            row["record_type"] = "plate_appearance"

            if is_tiebreak_placeholder(pa_list, index):
                row["record_type"] = "tiebreak_runner_placeholder"

            rows.append(row)

    return rows


# =========================
# 5. 資料品質檢查
# =========================

def validate_plate_appearances(df):
    """檢查必要欄位及基本棒球狀態；不將狀態連續性直接視為硬性錯誤。"""
    required_columns = [
        "game_id",
        "team_side",
        "inning",
        "outs",
        "bases",
        "result",
        "awayScores",
        "homeScores",
        "endOuts",
        "endBases",
        "endAwayScores",
        "endHomeScores",
        "record_type",
    ]

    missing_columns = [
        col for col in required_columns if col not in df.columns
    ]

    report = {
        "total_rows": len(df),
        "missing_columns": missing_columns,
        "invalid_rows": {},
    }

    if missing_columns:
        print("缺少必要欄位：", missing_columns)
        return report

    def valid_range(value, low, high):
        number = as_int(value)
        return number is not None and low <= number <= high

    outs_valid = df["outs"].map(lambda x: valid_range(x, 0, 2))
    bases_valid = df["bases"].map(lambda x: valid_range(x, 0, 7))
    inning_valid = df["inning"].map(lambda x: valid_range(x, 1, 999))

    report["invalid_rows"] = {
        "invalid_outs": int((~outs_valid).sum()),
        "invalid_bases": int((~bases_valid).sum()),
        "invalid_inning": int((~inning_valid).sum()),
    }

    print("\n資料品質報告：")
    print(report)
    return report


# =========================
# 6. 批次處理所有 JSON
# =========================

def main():
    if not DATA_DIR.exists():
        raise FileNotFoundError(f"找不到原始資料資料夾：{DATA_DIR}")

    json_files = sorted(DATA_DIR.rglob("*.json"))

    if not json_files:
        raise FileNotFoundError(
            f"{DATA_DIR} 中沒有找到任何 JSON 檔案"
        )

    print(f"找到 {len(json_files)} 個 JSON 檔案")

    all_rows = []
    failed_files = []

    for file_path in json_files:
        try:
            game = load_game_json(file_path)
            relative_path = file_path.relative_to(PROJECT_ROOT).as_posix()

            rows = extract_plate_appearances(game, relative_path)
            all_rows.extend(rows)

            print(
                f"完成：{file_path.name}，"
                f"整理 {len(rows)} 筆紀錄"
            )

        except Exception as error:
            failed_files.append({
                "file": str(file_path),
                "error": str(error),
            })
            print(f"處理失敗：{file_path.name}：{error}")

    if not all_rows:
        raise RuntimeError("沒有成功整理任何打席資料")

    df = pd.DataFrame(all_rows)

    # 空白結果與是否需要人工確認分開處理
    df["result_missing"] = df["result"].map(is_empty_result)

    is_placeholder = (
        df["record_type"] == "tiebreak_runner_placeholder"
    )

    df["needs_review"] = df["result_missing"] & ~is_placeholder
    df["review_reason"] = ""

    # 保留原始 result；另提供分析標籤，避免終端輸出出現難辨識的空白類別
    df["result_for_analysis"] = df["result"]
    df.loc[is_placeholder, "result_for_analysis"] = "PLACEHOLDER"
    df.loc[df["needs_review"], "review_reason"] = (
        "result 為空，且未符合目前的特殊占位紀錄辨識條件"
    )

    # 只新增壘況文字，不覆寫原始 bases 編碼
    df["bases_text"] = df["bases"].apply(decode_bases)

    # 保留原始 RE/WPA 等欄位；另建立供分析使用的欄位，
    # 避免特殊占位紀錄的數值混入一般打席統計。
    for source_col, analysis_col in [
        ("RE", "RE_for_analysis"),
        ("WPA", "WPA_for_analysis"),
        ("RE24", "RE24_for_analysis"),
        ("homeWE", "homeWE_for_analysis"),
    ]:
        if source_col in df.columns:
            df[analysis_col] = df[source_col].where(~is_placeholder, "")

    validate_plate_appearances(df)

    print("\n紀錄類型分布：")
    print(df["record_type"].value_counts(dropna=False))

    print("\n打席結果分布（分析標籤）：")
    print(df["result_for_analysis"].value_counts(dropna=False))

    print("\n仍需人工確認的紀錄數：")
    print(int(df["needs_review"].sum()))

    # 檢查同一 game_id 是否出現在多個不同來源檔案
    game_file_counts = df.groupby("game_id")["source_file"].nunique()
    duplicate_game_ids = game_file_counts[game_file_counts > 1]

    if not duplicate_game_ids.empty:
        print("\n警告：發現重複 game_id，請確認是否為不同版本資料：")
        print(duplicate_game_ids)

    # 將巢狀資料序列化為標準 JSON 字串，方便 CSV 讀回後重新解析
    for col in ["pitchCodes", "events"]:
        if col in df.columns:
            df[col] = df[col].apply(serialize_json_field)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    df.to_csv(OUTPUT_PATH, index=False, encoding="utf-8-sig")

    print(f"\n成功匯出：{OUTPUT_PATH}")
    print(f"成功整理紀錄數：{len(df)}")
    print(f"成功處理檔案數：{len(json_files) - len(failed_files)}")
    print(f"失敗檔案數：{len(failed_files)}")

    if failed_files:
        print("\n失敗檔案清單：")
        for item in failed_files:
            print(item)


if __name__ == "__main__":
    main()
