import json
import pandas as pd
from pathlib import Path

# 找到專案根目錄
PROJECT_ROOT = Path(__file__).resolve().parents[1]

# 指定原始資料路徑
DATA_DIR = PROJECT_ROOT / "data" / "raw"


def load_game_json(file_path):
    """讀取一場比賽的 JSON 檔案。"""

    with open(file_path, "r", encoding="utf-8") as file:
        data = json.load(file)

    return data

def inspect_game(data):
    """檢查比賽資料的基本結構。"""

    print("資料型態：", type(data).__name__)

    if not isinstance(data, dict):
        print("注意：最外層不是字典，需進一步檢查。")
        return

    print("最外層欄位：")

    for key, value in data.items():
        print(
            key,
            "->",
            type(value).__name__
        )

def extract_plate_appearances(game):
    """將客隊與主隊的打席整理成同一張表。"""

    rows = []

    for side, key in [
        ("away", "awayPAList"),
        ("home", "homePAList"),
    ]:
        plate_appearances = game.get(key, [])

        if not isinstance(plate_appearances, list):
            raise ValueError(f"{key} 必須是列表")

        for pa in plate_appearances:
            if not isinstance(pa, dict):
                continue

            row = pa.copy()
            row["team_side"] = side

            rows.append(row)

    return pd.DataFrame(rows)

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

    if isinstance(bases, bool) or not isinstance(bases, int):
        return "未知壘況"

    return base_names.get(bases, "未知壘況")

def validate_plate_appearances(df):
    """檢查必要欄位與基本棒球狀態。"""

    required_columns = [
    "inning",
    "outs",
    "bases",
    "result",  # 原本誤寫成 results
    "awayScores",
    "homeScores",
    "endOuts",
    "endBases",
    "endAwayScores",
    "endHomeScores",
]

    missing_columns = [
        col for col in required_columns
        if col not in df.columns
    ]

    if missing_columns:
        print("缺少必要欄位：", missing_columns)
        return {
            "total_rows": len(df),
            "missing_columns": missing_columns,
            "invalid_rows": {},
        }

    # 只檢查欄位存在且值為整數的基本狀態
    outs_valid = df["outs"].map(
        lambda x: (
            isinstance(x, int)
            and not isinstance(x, bool)
            and 0 <= x <= 2
        )
    )

    bases_valid = df["bases"].map(
        lambda x: (
            isinstance(x, int)
            and not isinstance(x, bool)
            and 0 <= x <= 7
        )
    )

    invalid_rows = {
        "invalid_outs": int((~outs_valid).sum()),
        "invalid_bases": int((~bases_valid).sum()),
    }

    report = {
        "total_rows": len(df),
        "missing_columns": [],
        "invalid_rows": invalid_rows,
    }

    print("資料品質報告：")
    print(report)

    return report

if __name__ == "__main__":
    file_path = DATA_DIR / "中職2025年-G343.json"

    # 1. 讀取原始資料
    game = load_game_json(file_path)

    # 2. 整理打席
    df = extract_plate_appearances(game)
    
    print("\n打席結果分布：")
    print(df["result"].value_counts(dropna=False))

    print("\nresult 為空的筆數：")
    print(
        df["result"].isna().sum()
        + (df["result"] == "").sum()
    )

    # 3. 轉換壘況
    if "bases" in df.columns:
        df["bases_text"] = df["bases"].apply(decode_bases)

    # 4. 驗證資料
    report = validate_plate_appearances(df)

    # 5. 匯出 CSV
    output_dir = PROJECT_ROOT / "data" / "processed"
    output_dir.mkdir(parents=True, exist_ok=True)

    output_path = output_dir / "plate_appearances.csv"

    df.to_csv(output_path, index=False, encoding="utf-8-sig")

    print(f"成功匯出：{output_path}")
    print(f"共整理 {len(df)} 筆打席")