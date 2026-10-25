
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

    if isinstance(bases, bool):
        return "未知壘況"

    if not isinstance(bases, numbers.Integral):
        return "未知壘況"

    return base_names.get(int(bases), "未知壘況")


def is_empty_list(value):
    """判斷欄位是否為空列表。"""

    return isinstance(value, list) and len(value) == 0


# =========================
# 3. 特殊占位紀錄辨識
# =========================

def is_tiebreak_placeholder(pa_list, index):
    """
    以多項條件辨識可能的突破僵局制占位紀錄。

    這是依目前資料觀察建立的辨識規則，
    不代表已證明所有資料來源都使用相同規則。
    """

    pa = pa_list[index]

    # 基本條件：延長局、結果空白、沒有投球與事件
    if not isinstance(pa, dict):
        return False

    if pa.get("inning", 0) < 10:
        return False

    if not is_empty_result(pa.get("result")):
        return False

    if not is_empty_list(pa.get("pitchCodes")):
        return False

    if not is_empty_list(pa.get("events")):
        return False

    # 需要前後紀錄才能進一步交叉驗證
    if index == 0 or index + 1 >= len(pa_list):
        return False

    previous_pa = pa_list[index - 1]
    next_pa = pa_list[index + 1]

    if not isinstance(previous_pa, dict):
        return False

    if not isinstance(next_pa, dict):
        return False

    # 前一筆應是前一局結束時的最後一個打者
    previous_is_last_out = (
        previous_pa.get("inning") == pa.get("inning") - 1
        and previous_pa.get("endOuts") == 3
        and previous_pa.get("batterName") == pa.get("batterName")
    )

    # 下一筆應是同一局的實際打席，且打席前二壘有人
    next_is_expected_state = (
        next_pa.get("inning") == pa.get("inning")
        and next_pa.get("bases") == 2
    )

    return previous_is_last_out and next_is_expected_state


# =========================
# 4. 整理單場比賽
# =========================

def extract_plate_appearances(game, source_file):
    """將單場比賽整理為打席資料列。"""

    rows = []

    game_id = f"{game.get('seasonId', '')}-{game.get('seq', '')}"

    game_metadata = {
        "game_id": game_id,
        "seasonId": game.get("seasonId"),
        "season": game.get("season"),
        "seq": game.get("seq"),
        "date": game.get("date"),
        "stadium": game.get("stadium"),
        "awayTeam": game.get("awayTeam"),
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

            # 所有紀錄先有預設類型
            row["record_type"] = "plate_appearance"

            if is_tiebreak_placeholder(pa_list, index):
                row["record_type"] = (
                    "tiebreak_runner_placeholder"
                )

            rows.append(row)

    return rows


# =========================
# 5. 資料品質檢查
# =========================

def validate_plate_appearances(df):
    """檢查必要欄位與基本棒球狀態。"""

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
        col for col in required_columns
        if col not in df.columns
    ]

    report = {
        "total_rows": len(df),
        "missing_columns": missing_columns,
        "invalid_rows": {},
    }

    if missing_columns:
        print("缺少必要欄位：", missing_columns)
        return report

    outs_valid = df["outs"].map(
        lambda x: (
            isinstance(x, numbers.Integral)
            and not isinstance(x, bool)
            and 0 <= x <= 2
        )
    )

    bases_valid = df["bases"].map(
        lambda x: (
            isinstance(x, numbers.Integral)
            and not isinstance(x, bool)
            and 0 <= x <= 7
        )
    )

    inning_valid = df["inning"].map(
        lambda x: (
            isinstance(x, numbers.Integral)
            and not isinstance(x, bool)
            and x >= 1
        )
    )

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
        raise FileNotFoundError(
            f"找不到原始資料資料夾：{DATA_DIR}"
        )

    # 遞迴尋找所有 JSON
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

            rows = extract_plate_appearances(
                game,
                file_path.relative_to(PROJECT_ROOT),
            )

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

    # 結果缺失與人工確認分開處理
    df["result_missing"] = (
        df["result"].map(is_empty_result)
    )

    is_placeholder = (
        df["record_type"] == "tiebreak_runner_placeholder"
    )

    df["needs_review"] = (
        df["result_missing"] & ~is_placeholder
    )

    # 壘況文字僅供閱讀，保留原始 bases 編碼
    df["bases_text"] = df["bases"].apply(decode_bases)

    # 檢查資料
    report = validate_plate_appearances(df)

    print("\n紀錄類型分布：")
    print(df["record_type"].value_counts(dropna=False))

    print("\n打席結果分布：")
    print(df["result"].value_counts(dropna=False))

    print("\n仍需人工確認的紀錄數：")
    print(int(df["needs_review"].sum()))

    # 檢查同一比賽識別碼是否出現在多個檔案
    game_file_counts = (
        df.groupby("game_id")["source_file"].nunique()
    )

    duplicate_game_ids = game_file_counts[
        game_file_counts > 1
    ]

    if not duplicate_game_ids.empty:
        print("\n警告：發現重複 game_id，請確認是否為不同版本資料：")
        print(duplicate_game_ids)

    # 匯出前，將巢狀欄位轉成標準 JSON 字串
    for col in ["pitchCodes", "events"]:
        if col in df.columns:
            df[col] = df[col].apply(
                lambda value: json.dumps(
                    value,
                    ensure_ascii=False
                ) if isinstance(value, (list, dict))
                else value
            )

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    df.to_csv(
        OUTPUT_PATH,
        index=False,
        encoding="utf-8-sig",
    )

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
