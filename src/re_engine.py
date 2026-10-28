"""
re_engine.py：由 plate_appearances_all.csv 計算 24 種壘況／出局數狀態的 RE。

定義（本專案口徑）
------------------
- 觀察單位：真實打席開始前的狀態 (outs, bases)。
- 剩餘得分：該半局「最終得分」減去該打席「打席前」的進攻方得分。
- 半局最終得分：取下一個半局第一個真實打席的打席前比分
  （endAwayScores/endHomeScores 在 78 筆會落後於後續跑者得分，故不使用）；
  若為全場最後一個半局，才退而取最後一筆的 end 比分。
- 排除：突破僵局制占位紀錄、純換人紀錄、未打滿 3 出局的半局（再見、縮短比賽）。
- 延長賽半局納入（起始狀態 0 出局二壘有人本身是合法狀態）。2023 尚未實施
  突破僵局制；以 2025 資料檢驗，排除延長賽對 24 格 RE 的影響最大 0.032、
  平均 0.007，均在標準誤內，因此各年度合併計算。

階段 C 新增欄位（re_observations.csv，每個真實打席一列）
------------------------------------------------------------
- result、pitchCodes、record_type：原樣帶入，不改寫、不補值。
- runs_remaining：從「該打席開始前」算起的剩餘得分（RE 即其平均）。
- runs_after：從「該打席結束後」算起的剩餘得分 = 半局最終得分 − 下一個真實打席
  的打席前比分；該半局最後一個打席為 0。
- runs_on_play：該打席本身造成的得分 = runs_remaining − runs_after。
- next_outs、next_bases：同半局下一個真實打席的打席前狀態；半局最後一個打席為空，
  next_is_half_end 為 True（僅在 half_complete 時有意義）。不使用 endBases。

注意：Rebas 公布的 RE 為 2018 年至今逐日累積值；本引擎主表為 --primary
指定範圍（預設 pooled＝所有賽季合併），另輸出各季比較（re_by_season.csv）。
與來源的對照以資料內 RE 欄位（最後 90 天平均）為準，差距僅供量級與趨勢對照。
"""
import argparse
import json
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from data_processor import as_int

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = PROJECT_ROOT / "data" / "processed" / "plate_appearances_all.csv"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "data" / "processed"

BASES_TEXT = {
    0: "無人在壘", 1: "一壘有人", 2: "二壘有人", 3: "一、二壘有人",
    4: "三壘有人", 5: "一、三壘有人", 6: "二、三壘有人", 7: "滿壘",
}

# Rebas 網頁（https://www.rebas.tw/coefficient?coef=re）手動抄錄值，列 = bases 0..7，
# 欄 = 0/1/2 出局；抄錄日 2026-10-26，為逐日累積值，截止日晚於本資料。
# 僅供參考：bases 3 與 4 兩列與資料內 RE 欄位的方向相反（疑為網頁圖示與數值錯置），
# 因此主要對照改用資料內的來源 RE 欄位（見 build_source_reference）。
WEB_PAGE_RE = {
    0: (0.504, 0.264, 0.098),
    1: (0.881, 0.535, 0.213),
    2: (1.144, 0.708, 0.342),
    3: (1.469, 0.984, 0.368),
    4: (1.531, 0.971, 0.477),
    5: (1.860, 1.200, 0.523),
    6: (2.135, 1.404, 0.580),
    7: (2.432, 1.602, 0.782),
}

REFERENCE_WINDOW_DAYS = 90   # 來源 RE 對照：取資料最後 90 天
REFERENCE_MIN_N = 20         # 對照格樣本數低於此值不使用

SMALL_SAMPLE = 100


def event_types(raw):
    """從 CSV 中的 events JSON 字串取出事件類型集合；解析失敗回傳空集合。"""
    if not isinstance(raw, str) or not raw.strip():
        return set()
    try:
        events = json.loads(raw)
    except json.JSONDecodeError:
        return set()
    return {e.get("type") for e in events if isinstance(e, dict)}


def select_real_pa(df):
    """保留「真實打席開始」的紀錄：排除占位紀錄與只有換人的紀錄。"""
    types = df["events"].map(event_types)
    only_sub_or_empty = types.map(lambda s: len(s - {"SUB"}) == 0)
    is_placeholder = df["record_type"] == "tiebreak_runner_placeholder"
    return df[~is_placeholder & ~only_sub_or_empty].copy()


def build_observations(df):
    """
    回傳 (observations, half_report)。
    observations：每個真實打席一列，含 runs_remaining 與半局是否完整。
    """
    real = select_real_pa(df)
    real["side_is_away"] = real["team_side"] == "away"
    real["half"] = np.where(
        real["side_is_away"], real["inning"] * 2 - 1, real["inning"] * 2
    )
    real["own_before"] = np.where(
        real["side_is_away"], real["awayScores"], real["homeScores"]
    )
    real["own_end"] = np.where(
        real["side_is_away"], real["endAwayScores"], real["endHomeScores"]
    )

    obs_rows = []
    half_rows = []

    for game_id, game in real.groupby("game_id", sort=False):
        game = game.sort_values("half", kind="stable")
        halves = sorted(game["half"].unique())
        by_half = {h: game[game["half"] == h] for h in halves}

        for i, half in enumerate(halves):
            sub = by_half[half]
            last = sub.iloc[-1]
            away = bool(sub["side_is_away"].iloc[0])
            own_col = "awayScores" if away else "homeScores"
            complete = as_int(last["endOuts"]) == 3

            has_next = i + 1 < len(halves) and halves[i + 1] == half + 1
            if has_next:
                final = by_half[halves[i + 1]].iloc[0][own_col]
                final_source = "next_half_pre_score"
            else:
                final = last["own_end"]
                final_source = "last_pa_end_score"

            half_rows.append({
                "game_id": game_id, "half": half,
                "team_side": "away" if away else "home",
                "n_pa": len(sub), "complete": bool(complete),
                "final_source": final_source,
            })

            sub = sub.reset_index(drop=True)
            for k in range(len(sub)):
                r = sub.iloc[k]
                if k + 1 < len(sub):
                    nxt = sub.iloc[k + 1]
                    next_outs, next_bases = as_int(nxt["outs"]), as_int(nxt["bases"])
                    runs_after = final - nxt["own_before"]
                else:
                    next_outs, next_bases = None, None
                    runs_after = 0
                runs_remaining = final - r["own_before"]
                obs_rows.append({
                    "game_id": game_id,
                    "season_id": game_id.rsplit("-", 1)[0],
                    "half": half, "inning": (half + 1) // 2,
                    "outs": as_int(r["outs"]), "bases": as_int(r["bases"]),
                    "runs_remaining": runs_remaining,
                    "half_complete": bool(complete),
                    "final_source": final_source,
                    "result": r["result"],
                    "pitchCodes": r["pitchCodes"],
                    "record_type": r["record_type"],
                    "runs_after": runs_after,
                    "runs_on_play": runs_remaining - runs_after,
                    "next_outs": next_outs, "next_bases": next_bases,
                    "next_is_half_end": next_outs is None,
                })

    return pd.DataFrame(obs_rows), pd.DataFrame(half_rows)


def build_source_reference(df):
    """
    以資料內來源（Rebas）的 RE 欄位作對照：取資料最後 REFERENCE_WINDOW_DAYS 天、
    真實打席的 RE 平均。這是來源在資料截止日附近對該狀態的估計值，可追溯。
    """
    real = select_real_pa(df)
    dates = pd.to_datetime(real["date"].astype(str).str[:10], errors="coerce")
    cutoff = dates.max() - pd.Timedelta(days=REFERENCE_WINDOW_DAYS)
    late = real[dates >= cutoff].copy()
    late["RE"] = pd.to_numeric(late["RE"], errors="coerce")
    late["outs"] = late["outs"].map(as_int)
    late["bases"] = late["bases"].map(as_int)
    ref = late.groupby(["outs", "bases"])["RE"].agg(
        source_RE_ref="mean", source_RE_ref_n="size"
    ).reset_index()
    ref.loc[ref["source_RE_ref_n"] < REFERENCE_MIN_N, "source_RE_ref"] = np.nan
    return ref


def compute_re_table(obs, reference=None):
    """對完整半局的觀察值，依 (outs, bases) 計算 RE。"""
    valid = obs[obs["half_complete"] & (obs["runs_remaining"] >= 0)]
    grouped = valid.groupby(["outs", "bases"])["runs_remaining"]

    table = grouped.agg(
        n="size", runs_sum="sum", RE="mean", std="std"
    ).reset_index()
    table["se"] = table["std"] / np.sqrt(table["n"])

    full = pd.MultiIndex.from_product(
        [[0, 1, 2], range(8)], names=["outs", "bases"]
    ).to_frame(index=False)
    table = full.merge(table, on=["outs", "bases"], how="left")
    table["n"] = table["n"].fillna(0).astype(int)
    table["runs_sum"] = table["runs_sum"].fillna(0)
    table["bases_text"] = table["bases"].map(BASES_TEXT)

    if reference is not None:
        table = table.merge(reference, on=["outs", "bases"], how="left")
    else:
        table["source_RE_ref"] = np.nan
        table["source_RE_ref_n"] = np.nan
    table["diff_vs_source_ref"] = table["RE"] - table["source_RE_ref"]
    table["web_page_RE"] = [
        WEB_PAGE_RE[b][o] for o, b in zip(table["outs"], table["bases"])
    ]
    table["note"] = np.where(
        table["n"] == 0, "無樣本",
        np.where(table["n"] < SMALL_SAMPLE, f"樣本數 < {SMALL_SAMPLE}", ""),
    )
    return table[[
        "outs", "bases", "bases_text", "n", "runs_sum", "RE", "se",
        "source_RE_ref", "diff_vs_source_ref", "web_page_RE", "note",
    ]]


def build_by_season_table(obs):
    """各季 RE 與樣本數的寬表，另含合併值，方便比較年度差異。"""
    wide = None
    for season, sub in sorted(obs.groupby("season_id")):
        t = compute_re_table(sub)[["outs", "bases", "bases_text", "n", "RE", "se"]]
        t = t.rename(columns={
            "n": f"n_{season}", "RE": f"RE_{season}", "se": f"se_{season}"
        })
        wide = t if wide is None else wide.merge(
            t.drop(columns=["bases_text"]), on=["outs", "bases"]
        )
    pooled = compute_re_table(obs)[["outs", "bases", "n", "RE", "se"]].rename(
        columns={"n": "n_pooled", "RE": "RE_pooled", "se": "se_pooled"}
    )
    return wide.merge(pooled, on=["outs", "bases"])


def write_outputs(items):
    """
    先把所有輸出寫成暫存檔，全部成功後才逐一取代正式檔；
    任何一步寫入失敗，就清除暫存檔，正式檔維持原樣。
    """
    staged = []
    try:
        for path, write_fn in items:
            tmp = path.with_name(path.name + ".tmp")
            write_fn(tmp)
            staged.append((tmp, path))
        for tmp, path in staged:
            os.replace(tmp, path)
    except Exception:
        for tmp, _ in staged:
            tmp.unlink(missing_ok=True)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description="計算 24 狀態 RE")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--primary", default="pooled",
        help='主表範圍："pooled"（預設，所有賽季合併）或某個 seasonId',
    )
    parser.add_argument(
        "--exclude-extra-innings", action="store_true",
        help="排除第 10 局以後的半局（敏感度檢查用）",
    )
    args = parser.parse_args(argv)

    if not args.input.exists():
        print(f"找不到輸入檔：{args.input}")
        return 1

    df = pd.read_csv(args.input)
    needed = ["game_id", "team_side", "inning", "outs", "bases", "awayScores",
              "homeScores", "endOuts", "endAwayScores", "endHomeScores",
              "events", "record_type", "result", "pitchCodes"]
    missing = [c for c in needed if c not in df.columns]
    if missing:
        print(f"輸入檔缺少欄位：{missing}")
        return 1

    obs, halves = build_observations(df)
    if args.exclude_extra_innings:
        obs = obs[obs["inning"] < 10]

    seasons = sorted(obs["season_id"].unique())
    if args.primary != "pooled" and args.primary not in seasons:
        print(f"找不到主表範圍 {args.primary}；可用：pooled 或 {seasons}")
        return 1

    reference = build_source_reference(df)
    primary_obs = obs if args.primary == "pooled" \
        else obs[obs["season_id"] == args.primary]
    table = compute_re_table(primary_obs, reference)
    by_season = build_by_season_table(obs)

    def usable(o):
        return int((o["half_complete"] & (o["runs_remaining"] >= 0)).sum())

    summary = {
        "primary": args.primary,
        "exclude_extra_innings": bool(args.exclude_extra_innings),
        "seasons": {
            s: {
                "games": int(obs[obs["season_id"] == s]["game_id"].nunique()),
                "observations_used": usable(obs[obs["season_id"] == s]),
            }
            for s in seasons
        },
        "half_innings": int(len(halves)),
        "half_innings_excluded_incomplete": int((~halves["complete"]).sum()),
        "observations_total": int(len(obs)),
        "observations_used": usable(obs),
        "observations_negative_runs": int((obs["runs_remaining"] < 0).sum()),
        "primary_states_with_n_lt_100": int((table["n"] < SMALL_SAMPLE).sum()),
        "primary_mean_abs_diff_vs_source_ref": round(
            float(table["diff_vs_source_ref"].abs().mean()), 4
        ),
        "primary_mean_diff_vs_source_ref": round(
            float(table["diff_vs_source_ref"].mean()), 4
        ),
        "source_ref_cells_available": int(table["source_RE_ref"].notna().sum()),
    }

    out = args.output_dir
    out.mkdir(parents=True, exist_ok=True)
    try:
        write_outputs([
            (out / "re_table.csv",
             lambda p: table.to_csv(p, index=False, encoding="utf-8-sig")),
            (out / "re_by_season.csv",
             lambda p: by_season.to_csv(p, index=False, encoding="utf-8-sig")),
            (out / "re_observations.csv",
             lambda p: obs.to_csv(p, index=False, encoding="utf-8-sig")),
            (out / "re_summary.json",
             lambda p: p.write_text(
                 json.dumps(summary, ensure_ascii=False, indent=2),
                 encoding="utf-8")),
        ])
    except PermissionError as error:
        print(f"寫檔失敗（請關閉正在開啟的檔案）：{error}")
        return 1

    pd.set_option("display.width", 220)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"\n主表（{args.primary}）：")
    print(table.round(3).to_string(index=False))
    print(f"\n各季比較：")
    print(by_season.drop(columns=["bases_text"]).round(3).to_string(index=False))
    print(f"\n已輸出至：{out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())