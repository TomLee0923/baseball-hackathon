import argparse
import json
import os
import sys
from pathlib import Path

import pandas as pd


# =========================
# 1. 路徑設定
# =========================

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_DIR = PROJECT_ROOT / "data" / "raw"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "data" / "processed"
OUTPUT_FILENAME = "plate_appearances_all.csv"
REPORT_DIRNAME = "import_reports"

# 每筆一般打席應具備的欄位（缺漏會標記為需人工確認，不會自動補值）
REQUIRED_PA_KEYS = [
    "inning", "outs", "bases", "result",
    "awayScores", "homeScores",
    "endOuts", "endBases", "endAwayScores", "endHomeScores",
]


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


def make_game_id(game):
    """以 seasonId 與 seq 組成 game_id；缺漏時直接報錯，避免 ID 碰撞。"""
    season_id = game.get("seasonId")
    seq = game.get("seq")

    if is_empty_result(season_id) or is_empty_result(seq):
        raise ValueError(
            f"缺少 seasonId 或 seq，無法建立 game_id"
            f"（seasonId={season_id!r}, seq={seq!r}）"
        )

    return f"{season_id}-{seq}"


def atomic_write(path, write_fn):
    """先寫入暫存檔再取代正式檔。失敗時舊檔保持不變，並清除暫存檔。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")

    try:
        write_fn(tmp_path)
        os.replace(tmp_path, path)
    except Exception:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise


# =========================
# 3. 特殊占位紀錄辨識
# =========================

def is_sub_only_record(pa):
    """純換人紀錄：無結果、無投球，且事件只有 SUB。"""
    if not isinstance(pa, dict) or not is_empty_result(pa.get("result")):
        return False
    if not is_empty_list(pa.get("pitchCodes")):
        return False
    events = pa.get("events")
    return (
        isinstance(events, list)
        and len(events) > 0
        and all(isinstance(e, dict) and e.get("type") == "SUB" for e in events)
    )


def neighbor_index(pa_list, index, step):
    """從 index 往前（-1）或往後（+1）找第一筆「非純換人」紀錄；找不到回傳 None。"""
    j = index + step
    while 0 <= j < len(pa_list):
        if not is_sub_only_record(pa_list[j]):
            return j
        j += step
    return None


def is_tiebreak_placeholder(pa_list, index):
    """
    辨識「高度疑似」突破僵局制占位紀錄。

    條件：
    1. 第 10 局或之後；
    2. result 為空，且 pitchCodes/events 都是空列表；
    3. 前一筆（略過純換人紀錄）是前一局最後一筆打席，且 endOuts == 3；
    4. 下一筆（略過純換人紀錄）是同一局的實際打席，且起始 bases == 2。

    占位紀錄的前後可能夾著純換人紀錄（例如替換延長賽起始跑者），
    所以前後檢查會略過這類紀錄（2024 年有 11 筆因此原本未被辨識）。
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

    prev_index = neighbor_index(pa_list, index, -1)
    next_index = neighbor_index(pa_list, index, +1)

    # 必須有前後紀錄才能交叉驗證
    if prev_index is None or next_index is None:
        return False

    previous_pa = pa_list[prev_index]
    next_pa = pa_list[next_index]

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

def extract_plate_appearances(game, source_file, game_id):
    """將單場比賽的客隊與主隊打席整理成同一份資料。"""
    rows = []

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
        # 不再以空列表默默代替缺漏的欄位
        if key not in game:
            raise ValueError(f"缺少 {key}")

        pa_list = game[key]

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

            # 記錄原始資料中實際缺少的鍵（不補值）
            missing = [k for k in REQUIRED_PA_KEYS if k not in pa]
            row["missing_fields"] = ",".join(missing)

            rows.append(row)

    if not rows:
        raise ValueError("awayPAList 與 homePAList 都沒有任何紀錄")

    return rows


# =========================
# 5. 資料品質檢查（逐列旗標 + 彙總）
# =========================

def event_type_set(events):
    """取出 events 列表中所有事件類型。"""
    if not isinstance(events, list):
        return set()
    return {e.get("type") for e in events if isinstance(e, dict)}


def has_runner_out(events):
    """events 中是否有任何跑者出局（盜壘失敗、牽制出局等）。"""
    if not isinstance(events, list):
        return False
    for event in events:
        if not isinstance(event, dict):
            continue
        for runner in event.get("runners") or []:
            if isinstance(runner, dict) and runner.get("isOut") is True:
                return True
    return False


def classify_empty_result_rows(df):
    """
    result 為空但屬已知合法型態者，改用專屬 record_type（不改動 result）：
    - substitution_only：只有 SUB 事件，出局數不變（單純換人紀錄）
    - baserunning_inning_end：2 出局變 3 出局，且有跑者出局
      （例如盜壘失敗、牽制出局造成打席中斷，所以沒有打席結果）
    - game_ended_mid_pa：該隊最後一筆，有投球、無結果、未達 3 出局
      （比賽在打席途中結束）
    其餘空 result 仍保留為 plate_appearance，由品質旗標要求人工確認。
    """
    empty = (df["record_type"] == "plate_appearance") \
        & df["result"].map(is_empty_result)
    outs = df["outs"].map(as_int)
    end_outs = df["endOuts"].map(as_int)
    types = df["events"].map(event_type_set)

    sub_only = empty \
        & types.map(lambda t: bool(t) and t <= {"SUB"}) \
        & (outs == end_outs)
    inning_end = empty & ~sub_only \
        & (outs == 2) & (end_outs == 3) \
        & df["events"].map(has_runner_out)

    # 比賽中途結束：該隊打席列表的最後一筆、有投球但沒有結果、未達 3 出局
    # （例如縮短比賽、時間限制或比賽中斷）
    is_last_of_side = (
        df.groupby(["game_id", "team_side"]).cumcount(ascending=False) == 0
    )
    game_ended = empty & ~sub_only & ~inning_end \
        & is_last_of_side \
        & types.map(lambda t: "PITCH" in t) \
        & (end_outs < 3)

    df.loc[sub_only, "record_type"] = "substitution_only"
    df.loc[inning_end, "record_type"] = "baserunning_inning_end"
    df.loc[game_ended, "record_type"] = "game_ended_mid_pa"


def add_quality_flags(df):
    """
    建立逐列品質旗標。只標記、不修改原始資料。
    - 空 result（非占位）
    - 缺少必要鍵
    - outs / bases / inning 超出範圍
    占位紀錄不套用上述檢查（其狀態欄位語意不同）。
    """
    for col in REQUIRED_PA_KEYS + ["game_id", "team_side", "record_type"]:
        if col not in df.columns:
            df[col] = None

    if "events" not in df.columns:
        df["events"] = None
    classify_empty_result_rows(df)

    is_placeholder = df["record_type"] == "tiebreak_runner_placeholder"
    is_ordinary_pa = df["record_type"] == "plate_appearance"
    reasons = pd.Series("", index=df.index)

    def add_reason(mask, text, applies_to=None):
        applies_to = ~is_placeholder if applies_to is None else applies_to
        mask = mask & applies_to
        reasons.loc[mask] = reasons.loc[mask].map(
            lambda s: s + ("；" if s else "") + text
        )

    def valid_range(value, low, high):
        number = as_int(value)
        return number is not None and low <= number <= high

    df["result_missing"] = df["result"].map(is_empty_result)

    outs_bad = ~df["outs"].map(lambda x: valid_range(x, 0, 2))
    bases_bad = ~df["bases"].map(lambda x: valid_range(x, 0, 7))
    inning_bad = ~df["inning"].map(lambda x: valid_range(x, 1, 999))
    fields_missing = df["missing_fields"].fillna("") != ""

    add_reason(df["result_missing"],
               "result 為空，且不符合已知的占位、換人或跑壘出局型態",
               applies_to=is_ordinary_pa)
    add_reason(outs_bad, "outs 不在 0～2 或無法解析")
    add_reason(bases_bad, "bases 不在 0～7 或無法解析")
    add_reason(inning_bad, "inning 無法解析")
    add_reason(fields_missing, "缺少必要欄位")

    df["review_reason"] = reasons
    df["needs_review"] = reasons != ""

    return {
        "invalid_outs": int((outs_bad & ~is_placeholder).sum()),
        "invalid_bases": int((bases_bad & ~is_placeholder).sum()),
        "invalid_inning": int((inning_bad & ~is_placeholder).sum()),
        "rows_missing_required_keys": int(
            (fields_missing & ~is_placeholder).sum()
        ),
    }


# =========================
# 6. 載入、去重、彙整
# =========================

def collect_games(json_files):
    """
    讀取所有檔案。原始資料夾不應有重複比賽；若同一 game_id 出現在
    多個檔案，視為錯誤並全部排除，交由人工確認（不做自動去重）。
    狀態：loaded / failed / duplicate_game_id
    """
    file_report = []
    loaded = {}  # game_id -> list of (path, rel, game)

    for file_path in json_files:
        rel = file_path.relative_to(PROJECT_ROOT).as_posix() \
            if PROJECT_ROOT in file_path.parents else str(file_path)
        try:
            game = load_game_json(file_path)
            game_id = make_game_id(game)
            loaded.setdefault(game_id, []).append((file_path, rel, game))
        except Exception as error:
            file_report.append({
                "file": rel, "game_id": "", "status": "failed",
                "rows": 0, "detail": f"{type(error).__name__}: {error}",
            })
            print(f"讀取失敗：{file_path.name}：{error}")

    accepted = []

    for game_id, versions in sorted(loaded.items()):
        if len(versions) == 1:
            path, rel, game = versions[0]
            accepted.append((path, rel, game, game_id))
        else:
            names = [v[1] for v in versions]
            for _, rel, _ in versions:
                file_report.append({
                    "file": rel, "game_id": game_id,
                    "status": "duplicate_game_id", "rows": 0,
                    "detail": f"同一 game_id 出現在多個檔案：{names}",
                })
            print(f"重複 game_id：{game_id}，已全部排除：{names}")

    return accepted, file_report


def build_dataframe(accepted, file_report):
    all_rows = []

    for path, rel, game, game_id in accepted:
        try:
            rows = extract_plate_appearances(game, rel, game_id)
            all_rows.extend(rows)
            file_report.append({
                "file": rel, "game_id": game_id, "status": "loaded",
                "rows": len(rows), "detail": "",
            })
            print(f"完成：{path.name}（{game_id}），整理 {len(rows)} 筆紀錄")
        except Exception as error:
            file_report.append({
                "file": rel, "game_id": game_id, "status": "failed",
                "rows": 0, "detail": f"{type(error).__name__}: {error}",
            })
            print(f"處理失敗：{path.name}：{error}")

    if not all_rows:
        return None

    df = pd.DataFrame(all_rows)
    issue_counts = add_quality_flags(df)

    is_placeholder = df["record_type"] == "tiebreak_runner_placeholder"

    df["result_for_analysis"] = df["result"]
    df.loc[is_placeholder, "result_for_analysis"] = "PLACEHOLDER"

    df["bases_text"] = df["bases"].apply(decode_bases)

    # 保留原始 RE/WPA 等欄位；另建立分析用欄位，占位紀錄設為空值
    for source_col, analysis_col in [
        ("RE", "RE_for_analysis"),
        ("WPA", "WPA_for_analysis"),
        ("RE24", "RE24_for_analysis"),
        ("homeWE", "homeWE_for_analysis"),
    ]:
        if source_col in df.columns:
            df[analysis_col] = df[source_col].where(~is_placeholder)

    return df, issue_counts


def build_summary(df, issue_counts, file_report):
    status_counts = pd.Series(
        [r["status"] for r in file_report]
    ).value_counts().to_dict()

    rows_per_game = df.groupby("game_id").size()

    return {
        "json_files_found": len(file_report),
        "file_status_counts": {k: int(v) for k, v in status_counts.items()},
        "games_imported": int(df["game_id"].nunique()),
        "total_rows": int(len(df)),
        "plate_appearance_rows": int(
            (df["record_type"] == "plate_appearance").sum()
        ),
        "tiebreak_placeholder_rows": int(
            (df["record_type"] == "tiebreak_runner_placeholder").sum()
        ),
        "substitution_only_rows": int(
            (df["record_type"] == "substitution_only").sum()
        ),
        "baserunning_inning_end_rows": int(
            (df["record_type"] == "baserunning_inning_end").sum()
        ),
        "game_ended_mid_pa_rows": int(
            (df["record_type"] == "game_ended_mid_pa").sum()
        ),
        "missing_result_rows_needing_review": int(
            (df["result_missing"]
             & (df["record_type"] == "plate_appearance")).sum()
        ),
        "rows_needing_review": int(df["needs_review"].sum()),
        "quality_issues": issue_counts,
        "seasons_present": sorted(
            str(x) for x in df["seasonId"].dropna().unique()
        ),
        "rows_per_game_min": int(rows_per_game.min()),
        "rows_per_game_max": int(rows_per_game.max()),
    }


# =========================
# 7. 主程式
# =========================

def main(argv=None):
    parser = argparse.ArgumentParser(description="整理 Rebas JSON 成逐打席 CSV")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--allow-partial", action="store_true",
        help="即使有失敗檔案或重複 game_id，仍匯出成功的部分（預設不匯出）",
    )
    args = parser.parse_args(argv)

    data_dir = args.data_dir.resolve()
    output_dir = args.output_dir.resolve()
    output_path = output_dir / OUTPUT_FILENAME
    report_dir = output_dir / REPORT_DIRNAME

    if not data_dir.exists():
        print(f"找不到原始資料資料夾：{data_dir}")
        return 1

    json_files = sorted(data_dir.rglob("*.json"))
    if not json_files:
        print(f"{data_dir} 中沒有找到任何 JSON 檔案")
        return 1

    print(f"找到 {len(json_files)} 個 JSON 檔案")

    accepted, file_report = collect_games(json_files)
    result = build_dataframe(accepted, file_report)

    if result is None:
        print("沒有成功整理任何打席資料")
        _write_file_report(report_dir, file_report)
        return 1

    df, issue_counts = result
    summary = build_summary(df, issue_counts, file_report)

    problem_statuses = {"failed", "duplicate_game_id"}
    problems = [r for r in file_report if r["status"] in problem_statuses]
    summary["blocking_problem_files"] = len(problems)

    # ---- 報告永遠輸出 ----
    try:
        _write_file_report(report_dir, file_report)
        atomic_write(
            report_dir / "import_summary.json",
            lambda p: p.write_text(
                json.dumps(summary, ensure_ascii=False, indent=2),
                encoding="utf-8",
            ),
        )
        review_df = df[df["needs_review"]]
        atomic_write(
            report_dir / "rows_needing_review.csv",
            lambda p: review_df.to_csv(p, index=False, encoding="utf-8-sig"),
        )
    except PermissionError as error:
        print(f"\n報告寫入失敗（請關閉正在開啟的檔案）：{error}")
        return 1

    # ---- 終端摘要 ----
    print("\n匯入摘要：")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print("\n紀錄類型分布：")
    print(df["record_type"].value_counts(dropna=False))
    print("\n打席結果分布（分析標籤）：")
    print(df["result_for_analysis"].value_counts(dropna=False))

    if problems and not args.allow_partial:
        print("\n有失敗檔案或重複 game_id，已停止匯出，CSV 未更新。")
        print(f"請查看：{report_dir / 'import_file_report.csv'}")
        print("若確認要先匯出成功的部分，請加上 --allow-partial。")
        return 1

    # 將巢狀資料序列化為標準 JSON 字串
    for col in ["pitchCodes", "events"]:
        if col in df.columns:
            df[col] = df[col].apply(serialize_json_field)

    try:
        atomic_write(
            output_path,
            lambda p: df.to_csv(p, index=False, encoding="utf-8-sig"),
        )
    except PermissionError as error:
        print(f"\n匯出失敗，CSV 並未更新：{error}")
        print("請關閉正在使用該 CSV 的程式（例如 Excel）後重試。")
        return 1

    print(f"\n成功匯出：{output_path}")
    print(f"匯入比賽數：{summary['games_imported']}")
    print(f"成功整理紀錄數：{len(df)}")
    if problems:
        print(f"注意：以 --allow-partial 匯出，另有 {len(problems)} 個問題檔案未納入")

    return 0


def _write_file_report(report_dir, file_report):
    report_df = pd.DataFrame(
        file_report,
        columns=["file", "game_id", "status", "rows", "detail"],
    ).sort_values("file")
    atomic_write(
        report_dir / "import_file_report.csv",
        lambda p: report_df.to_csv(p, index=False, encoding="utf-8-sig"),
    )


if __name__ == "__main__":
    sys.exit(main())