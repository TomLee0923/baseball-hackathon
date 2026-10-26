"""
main.py：階段 D，FastAPI 情境查詢 Demo。

啟動（專案根目錄，已啟用 .venv）：
    python -m uvicorn main:app --app-dir src --reload
然後開啟 http://127.0.0.1:8000/docs 操作。

前置：需先執行 python src\\re_engine.py，產生 data\\processed\\re_observations.csv。

設計說明
--------
- 統計只依「出局數 + 壘況」計算（與 re_table.csv、scenario.py 同口徑）。
- 局數（inning）與分差（score_diff）僅作情境顯示，不參與計算，回應中會明講。
- 只呈現歷史統計，不給戰術建議；觸擊比較為歷史相關，非因果。
"""
import json
import sys
from functools import lru_cache
from pathlib import Path

import pandas as pd
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).resolve().parent))

import re_engine as R  # noqa: E402
import scenario as S  # noqa: E402

OBS_PATH = S.DEFAULT_INPUT
DATA_BASIS = "2023～2025 中華職棒例行賽聯盟平均（Rebas Open Data，ODC-By）"
SHORT_BASIS = "2023～2025 聯盟平均"
TOP_TRANSITIONS = 3

if not OBS_PATH.exists():
    raise RuntimeError(
        f"找不到 {OBS_PATH}。請先在專案根目錄執行：python src\\re_engine.py"
    )

OBS = S.read_observations(OBS_PATH)

app = FastAPI(
    title="棒球情境分析 Demo（可行性驗證）",
    description=(
        "輸入比賽情境，回傳該狀態的歷史 RE、樣本數與打席結果分布。\n\n"
        f"資料基礎：{DATA_BASIS}。\n\n"
        "**局數與分差僅作情境顯示，不參與計算。** 所有數字皆為歷史相關，不是因果，"
        "也不是戰術建議。"
    ),
    version="0.1.0",
)


class ScenarioRequest(BaseModel):
    inning: int = Field(..., ge=1, le=20, description="局數（僅作情境顯示）")
    score_diff: int = Field(
        ..., ge=-30, le=30,
        description="分差＝我方得分 − 對手得分，落後為負（僅作情境顯示）",
    )
    outs: int = Field(..., ge=0, le=2, description="出局數（0～2）")
    runner_on_first: bool = Field(False, description="一壘有人")
    runner_on_second: bool = Field(False, description="二壘有人")
    runner_on_third: bool = Field(False, description="三壘有人")

    model_config = {
        "json_schema_extra": {
            "examples": [{
                "inning": 8, "score_diff": -1, "outs": 0,
                "runner_on_first": True,
                "runner_on_second": False, "runner_on_third": False,
            }]
        }
    }

    def bases_code(self):
        """壘況編碼：一壘 +1、二壘 +2、三壘 +4（與 Rebas 定義相同）。"""
        return (
            int(self.runner_on_first)
            + 2 * int(self.runner_on_second)
            + 4 * int(self.runner_on_third)
        )


def records(df):
    """DataFrame → 可 JSON 序列化的 list[dict]（NaN → null，numpy 型別 → 原生型別）。"""
    return json.loads(df.to_json(orient="records", force_ascii=False))


@lru_cache(maxsize=24)
def cached_analysis(outs, bases):
    return S.analyze(OBS, outs, bases)


@lru_cache(maxsize=1)
def cached_re_table():
    table = R.compute_re_table(OBS)
    return table[["outs", "bases", "bases_text", "n", "RE", "se", "note"]]


def build_interpretation(req, result):
    s = result["summary"]
    label = s["state"]["label"]
    scope = result["scope"].iloc[0]
    cat = result["by_category"]
    bunt = result["bunt_compare"]
    diff_text = "平手" if req.score_diff == 0 else (
        f"領先 {req.score_diff} 分" if req.score_diff > 0
        else f"落後 {-req.score_diff} 分"
    )
    lines = [
        f"歷史上（{SHORT_BASIS}），從「{label}」開始，該半局平均還能得 "
        f"{scope['RE']:.2f} 分（樣本 {int(scope['n']):,}、標準誤 {scope['se']:.3f}）。",
        f"你輸入的情境為第 {req.inning} 局、{diff_text}；"
        "目前統計不依局數與分差分組，這兩項只作情境顯示。",
    ]
    if int(scope["n"]) < R.SMALL_SAMPLE:
        lines.append(f"注意：此狀態樣本數不足 {R.SMALL_SAMPLE}，數字不穩定。")

    top = cat.iloc[0]
    lines.append(
        f"最常見的打席結果類別是「{top['category']}」"
        f"（{top['pct'] * 100:.1f}%，{int(top['n']):,} 筆）。"
    )

    with_code, without_code, diff = bunt.iloc[0], bunt.iloc[1], bunt.iloc[2]
    if diff["note"] == "資料不足":
        lines.append(
            f"觸擊比較：有觸擊代碼的打席僅 {int(with_code['n'])} 筆，樣本不足"
            f"（需 ≥ {S.MIN_N}），不做比較。"
        )
    else:
        lines.append(
            f"觸擊比較：有觸擊代碼 {int(with_code['n'])} 筆，從打席開始算平均剩餘得分 "
            f"{with_code['mean_runs_remaining']:.3f}；未偵測到者 "
            f"{int(without_code['n'])} 筆，{without_code['mean_runs_remaining']:.3f}；"
            f"差 {diff['mean_runs_remaining']:+.3f}（標準誤 {diff['se_runs_remaining']:.3f}），"
            f"{diff['note']}。這是歷史相關，不是觸擊好壞的結論。"
        )
    return lines


@app.get("/", summary="資料與口徑說明")
def meta():
    return {
        "data_basis": DATA_BASIS,
        "games": int(OBS["game_id"].nunique()),
        "observations": int(len(OBS)),
        "seasons": sorted(OBS["season_id"].unique().tolist()),
        "state_definition": "真實打席開始前的 (出局數, 壘況)；bases 編碼：一壘+1、二壘+2、三壘+4",
        "RE_definition": "從該狀態開始，該半局剩餘得分的歷史平均",
        "used_in_calculation": ["outs", "bases"],
        "display_only": ["inning", "score_diff"],
        "docs": "/docs",
    }


@app.get("/re-table", summary="24 格 RE 主表（三季合併）")
def re_table():
    return {"data_basis": DATA_BASIS, "table": records(cached_re_table())}


@app.post("/scenario", summary="輸入情境，回傳 RE、樣本數與結果分布")
def scenario(req: ScenarioRequest):
    outs, bases = req.outs, req.bases_code()
    try:
        result = cached_analysis(outs, bases)
    except Exception as error:  # 不應發生；保留訊息方便除錯
        raise HTTPException(status_code=500, detail=f"分析失敗：{error}")

    summary = result["summary"]
    return {
        "input": req.model_dump(),
        "state": summary["state"],
        "context": {
            "inning": req.inning,
            "score_diff": req.score_diff,
            "used_in_calculation": False,
            "note": "局數與分差僅作情境顯示；統計只依出局數與壘況。",
        },
        "data_basis": DATA_BASIS,
        "run_expectancy": records(result["scope"]),
        "outcomes": records(result["by_category"]),
        "transitions": {
            category: records(group.head(TOP_TRANSITIONS)[["next_state", "n", "pct_in_category"]])
            for category, group in result["transitions"].groupby("category", sort=False)
        },
        "bunt_comparison": records(result["bunt_compare"]),
        "bunt_attempt_results": records(result["bunt_by_result"]),
        "interpretation": build_interpretation(req, result),
        "sample": {
            "observations_used": summary["observations_used"],
            "excluded_incomplete_half": summary["excluded_incomplete_half"],
            "min_n_for_conclusion": summary["min_n_for_conclusion"],
            "other_category_results": summary["other_category_results"],
        },
        "caveats": summary["caveats"],
    }