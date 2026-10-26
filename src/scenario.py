"""
scenario.py：階段 C 情境分析。給定起始狀態 (outs, bases)，輸出歷史統計。

用法（專案根目錄）：
    python src\\scenario.py                      # 預設：0 出局、一壘有人
    python src\\scenario.py --outs 1 --bases 2   # 1 出局、二壘有人

輸入：data\\processed\\re_observations.csv（由 re_engine.py 產生）
輸出：data\\processed\\scenario_{outs}out_bases{bases}\\ 內的 CSV 與 summary.json

三個剩餘得分指標（務必分開看）
------------------------------
- runs_remaining：從「該打席開始前」算起，半局還會得幾分。RE 就是它的平均。
  比較不同打擊結果／戰術時，應以此為主（涵蓋打席本身造成的得分）。
- runs_on_play：該打席本身造成的得分。
- runs_after：從「該打席結束後」算起，半局還會得幾分
  （不含該打席本身的得分）。用來看「打完之後局面還剩多少價值」。
  runs_remaining = runs_on_play + runs_after。

限制
----
- 以上皆為歷史相關，不是因果。觸擊與否受分差、局數、打者能力影響（選擇偏誤）。
- 標準誤假設觀察值互相獨立；同一半局內的打席實際上相關，標準誤略為低估。
- 樣本數 < MIN_N 的組別標示「資料不足」，不下結論。
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

import re_engine as R

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = PROJECT_ROOT / "data" / "processed" / "re_observations.csv"
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "data" / "processed"

MIN_N = 30  # 組別樣本數低於此值，標示資料不足

# 大類對照（依 result 欄位；原始 result 不改寫）
CATEGORY_CODES = {
    "安打": ["1B", "2B", "3B", "HR", "IHR"],
    "保送／觸身": ["uBB", "IBB", "HBP"],
    "一般出局": ["GO", "FO", "FC", "SF"],
    "三振": ["SO"],
    "雙殺／三殺": ["GIDP", "DP", "TP"],
    "犧牲觸擊": ["SH", "SH_FC"],
    "失誤": ["E", "SH_E", "SF_E", "GIDP_E"],
}
CODE_TO_CATEGORY = {
    code: cat for cat, codes in CATEGORY_CODES.items() for code in codes
}
NO_RESULT = "無打席結果（跑壘中斷等）"
OTHER = "其他"
BUNT_CODES = {"BUNT", "TRY_BUNT", "FOUL_BUNT"}


def categorize(result):
    if not isinstance(result, str) or not result.strip():
        return NO_RESULT
    return CODE_TO_CATEGORY.get(result.strip(), OTHER)


def has_bunt_code(raw):
    """pitchCodes（JSON 字串）是否含觸擊代碼；解析失敗視為沒有。"""
    if not isinstance(raw, str) or not raw.strip():
        return False
    try:
        codes = json.loads(raw)
    except json.JSONDecodeError:
        return False
    return isinstance(codes, list) and bool(BUNT_CODES & set(codes))


def state_label(outs, bases):
    return f"{int(outs)}出局・{R.BASES_TEXT[int(bases)]}"


def mean_se(series):
    n = len(series)
    if n == 0:
        return np.nan, np.nan
    se = series.std(ddof=1) / np.sqrt(n) if n > 1 else np.nan
    return series.mean(), se


def read_observations(path):
    """讀入 re_observations.csv 並檢查欄位（API 啟動時只需讀一次）。"""
    obs = pd.read_csv(path)
    needed = ["season_id", "outs", "bases", "half_complete", "runs_remaining",
              "runs_after", "runs_on_play", "next_outs", "next_bases",
              "next_is_half_end", "result", "pitchCodes"]
    missing = [c for c in needed if c not in obs.columns]
    if missing:
        raise SystemExit(
            f"{path} 缺少欄位 {missing}；請先用新版 re_engine.py 重新產生。"
        )
    return obs


def prepare_state(obs, outs, bases):
    """取指定起始狀態，並與 RE 同口徑（完整半局、剩餘得分 >= 0）。"""
    state = obs[(obs["outs"] == outs) & (obs["bases"] == bases)]
    valid = state[state["half_complete"] & (state["runs_remaining"] >= 0)].copy()

    valid["category"] = valid["result"].map(categorize)
    valid["bunt_attempt"] = valid["pitchCodes"].map(has_bunt_code)
    valid["next_state"] = [
        "半局結束" if end else state_label(o, b)
        for end, o, b in zip(
            valid["next_is_half_end"], valid["next_outs"], valid["next_bases"]
        )
    ]
    return state, valid


def re_by_scope(obs_all, outs, bases):
    """主表 RE：沿用 re_engine.compute_re_table，確保與 re_table.csv 同口徑。"""
    rows = []
    scopes = [("三季合併", obs_all)] + [
        (s, obs_all[obs_all["season_id"] == s])
        for s in sorted(obs_all["season_id"].unique())
    ]
    for name, sub in scopes:
        t = R.compute_re_table(sub)
        r = t[(t["outs"] == outs) & (t["bases"] == bases)].iloc[0]
        rows.append({"scope": name, "n": int(r["n"]),
                     "RE": r["RE"], "se": r["se"]})
    return pd.DataFrame(rows)


def by_category(valid):
    rows = []
    total = len(valid)
    for cat, g in valid.groupby("category"):
        rows.append({
            "category": cat,
            "results_included": ",".join(sorted(g["result"].dropna().unique())),
            "n": len(g),
            "pct": len(g) / total,
            "mean_runs_remaining": g["runs_remaining"].mean(),
            "mean_runs_on_play": g["runs_on_play"].mean(),
            "mean_runs_after": g["runs_after"].mean(),
            "note": "資料不足" if len(g) < MIN_N else "",
        })
    return pd.DataFrame(rows).sort_values("n", ascending=False)


def transitions(valid):
    counts = (
        valid.groupby(["category", "next_state"]).size().rename("n").reset_index()
    )
    counts["pct_in_category"] = counts["n"] / counts.groupby("category")["n"].transform("sum")
    return counts.sort_values(["category", "n"], ascending=[True, False])


def bunt_compare(valid):
    rows = []
    stats = {}
    for flag, label in [(True, "有觀察到觸擊代碼"), (False, "未偵測到觸擊代碼")]:
        g = valid[valid["bunt_attempt"] == flag]
        m, se = mean_se(g["runs_remaining"])
        stats[flag] = (m, se, len(g))
        rows.append({
            "group": label, "n": len(g),
            "mean_runs_remaining": m, "se_runs_remaining": se,
            "mean_runs_on_play": g["runs_on_play"].mean(),
            "mean_runs_after": g["runs_after"].mean(),
            "note": "資料不足" if len(g) < MIN_N else "",
        })
    (m1, s1, n1), (m0, s0, n0) = stats[True], stats[False]
    if min(n1, n0) >= MIN_N:
        diff = m1 - m0
        diff_se = float(np.sqrt(s1 ** 2 + s0 ** 2))
        verdict = ("差異在約 2 個標準誤內，無法排除抽樣誤差"
                   if abs(diff) < 2 * diff_se else "差異超過約 2 個標準誤（仍非因果）")
    else:
        diff, diff_se, verdict = np.nan, np.nan, "資料不足"
    rows.append({
        "group": "差（有觸擊 − 未偵測）", "n": n1 + n0,
        "mean_runs_remaining": diff, "se_runs_remaining": diff_se,
        "mean_runs_on_play": np.nan, "mean_runs_after": np.nan,
        "note": verdict,
    })
    return pd.DataFrame(rows)


def bunt_by_result(valid):
    g = valid[valid["bunt_attempt"]].copy()
    g["result"] = g["result"].fillna("(空)")
    out = g.groupby("result").agg(
        n=("result", "size"),
        mean_runs_remaining=("runs_remaining", "mean"),
        mean_runs_after=("runs_after", "mean"),
    ).reset_index().sort_values("n", ascending=False)
    out["pct_of_attempts"] = out["n"] / len(g) if len(g) else np.nan
    return out


def analyze(obs, outs, bases):
    """對指定狀態做完整分析；CLI 與 API 共用。回傳 dict（含 DataFrame）。"""
    state, valid = prepare_state(obs, outs, bases)
    scope = re_by_scope(obs, outs, bases)
    cat = by_category(valid)

    # 自我檢查：大類樣本合計 = 納入樣本；加權平均 = RE
    pooled_re = float(scope.loc[scope["scope"] == "三季合併", "RE"].iloc[0])
    assert cat["n"].sum() == len(valid)
    weighted = float((cat["n"] * cat["mean_runs_remaining"]).sum() / cat["n"].sum())
    assert abs(weighted - pooled_re) < 1e-9, (weighted, pooled_re)

    summary = {
        "state": {"outs": outs, "bases": bases, "label": state_label(outs, bases)},
        "observations_in_state": int(len(state)),
        "observations_used": int(len(valid)),
        "excluded_incomplete_half": int(len(state) - len(valid)),
        "RE_pooled": pooled_re,
        "min_n_for_conclusion": MIN_N,
        "bunt_attempt_codes": sorted(BUNT_CODES),
        "other_category_results": sorted(
            valid.loc[valid["category"] == OTHER, "result"].dropna().unique()
        ),
        "caveats": [
            "歷史相關，非因果；觸擊與否有選擇偏誤（分差、局數、打者能力）。",
            "標準誤假設觀察值獨立，同半局內相關，實際略大。",
            "BUNT 代碼不等於犧牲成功；請對照 bunt_by_result。",
        ],
    }
    return {
        "summary": summary, "scope": scope, "by_category": cat,
        "transitions": transitions(valid), "bunt_compare": bunt_compare(valid),
        "bunt_by_result": bunt_by_result(valid),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="階段 C：情境分析")
    parser.add_argument("--outs", type=int, default=0, choices=[0, 1, 2])
    parser.add_argument("--bases", type=int, default=1, choices=range(8))
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    args = parser.parse_args(argv)

    if not args.input.exists():
        print(f"找不到輸入檔：{args.input}（請先執行 python src\\re_engine.py）")
        return 1

    obs = read_observations(args.input)
    result = analyze(obs, args.outs, args.bases)
    summary = result["summary"]
    scope, cat, trans = result["scope"], result["by_category"], result["transitions"]
    bunt, bunt_res = result["bunt_compare"], result["bunt_by_result"]

    out_dir = args.output_root / f"scenario_{args.outs}out_bases{args.bases}"
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        for name, frame in [
            ("re_scope.csv", scope), ("by_category.csv", cat),
            ("transitions.csv", trans), ("bunt_compare.csv", bunt),
            ("bunt_by_result.csv", bunt_res),
        ]:
            frame.to_csv(out_dir / name, index=False, encoding="utf-8-sig")
        (out_dir / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except PermissionError as error:
        print(f"寫檔失敗（請關閉正在開啟的檔案）：{error}")
        return 1

    pd.set_option("display.width", 200)
    pd.set_option("display.max_columns", 20)
    print(f"情境：{summary['state']['label']}　（納入 {summary['observations_used']} 筆；"
          f"排除不完整半局 {summary['excluded_incomplete_half']} 筆）\n")
    print("【RE 與樣本數】")
    print(scope.round(4).to_string(index=False))
    print("\n【打席結果大類】（remaining=從打席開始算；after=打席結束後）")
    print(cat.drop(columns=["results_included"]).round(3).to_string(index=False))
    print("\n【各大類最常見的下一狀態（前 3 名）】")
    print(trans.groupby("category").head(3).round(3).to_string(index=False))
    print("\n【觸擊代碼 vs 未偵測（歷史相關，非因果）】")
    print(bunt.round(3).to_string(index=False))
    print("\n【觸擊嘗試的實際結果】")
    print(bunt_res.round(3).to_string(index=False))
    if summary["other_category_results"]:
        print(f"\n歸入「{OTHER}」的原始 result：{summary['other_category_results']}")
    print(f"\n已輸出至：{out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())