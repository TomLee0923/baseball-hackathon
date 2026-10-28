r"""
tests/test_scenario.py：情境分析（結果分類、觸擊辨識、分析不變式）的單元測試。
"""
import json

import numpy as np
import pandas as pd
import pytest

import scenario as S


@pytest.mark.parametrize("result, category", [
    ("1B", "安打"), ("HR", "安打"), ("uBB", "保送／觸身"), ("IBB", "保送／觸身"),
    ("GO", "一般出局"), ("FC", "一般出局"), ("SO", "三振"),
    ("GIDP", "雙殺／三殺"), ("SH", "犧牲觸擊"), ("SH_FC", "犧牲觸擊"),
    ("SH_E", "失誤"), ("E", "失誤"),
    ("H", "其他"), ("ID", "其他"),
    ("", S.NO_RESULT), (None, S.NO_RESULT), (float("nan"), S.NO_RESULT),
])
def test_categorize(result, category):
    assert S.categorize(result) == category


def test_category_codes_do_not_overlap():
    codes = [c for codes in S.CATEGORY_CODES.values() for c in codes]
    assert len(codes) == len(set(codes))


@pytest.mark.parametrize("raw, expected", [
    (json.dumps(["B", "BUNT"]), True),
    (json.dumps(["TRY_BUNT"]), True),
    (json.dumps(["FOUL_BUNT", "S"]), True),
    (json.dumps(["B", "S", "SW"]), False),
    ("[]", False), ("", False), (None, False), ("not json", False),
    (json.dumps({"BUNT": 1}), False),
])
def test_has_bunt_code(raw, expected):
    assert S.has_bunt_code(raw) is expected


def test_mean_se():
    mean, se = S.mean_se(pd.Series([1.0, 2.0, 3.0, 4.0]))
    assert mean == pytest.approx(2.5)
    assert se == pytest.approx(np.std([1, 2, 3, 4], ddof=1) / 2)
    assert np.isnan(S.mean_se(pd.Series([], dtype=float))[0])


def synthetic_obs(n=40, season="CPBL-2025-JO"):
    """n 筆 (0 出局, 一壘有人) 觀察值：每 4 筆中有 1 筆觸擊犧牲、1 筆安打得 1 分、其餘三振。"""
    rows = []
    for i in range(n):
        kind = i % 4
        if kind == 0:      # 觸擊犧牲：推進到 1 出局二壘，後續再得 1 分
            res, pitch, rem, on_play, nxt = "SH", ["BUNT"], 1, 0, (1, 2)
        elif kind == 1:    # 安打：本打席得 1 分，之後 0 分
            res, pitch, rem, on_play, nxt = "1B", ["B", "H"], 1, 1, (0, 3)
        else:              # 三振：局面沒有再得分
            res, pitch, rem, on_play, nxt = "SO", ["S", "S", "S"], 0, 0, (1, 1)
        rows.append({
            "game_id": f"{season}-{i}", "season_id": season, "half": 1, "inning": 1,
            "outs": 0, "bases": 1, "runs_remaining": rem,
            "half_complete": True, "final_source": "next_half_pre_score",
            "result": res, "pitchCodes": json.dumps(pitch), "record_type": "plate_appearance",
            "runs_after": rem - on_play, "runs_on_play": on_play,
            "next_outs": nxt[0], "next_bases": nxt[1], "next_is_half_end": False,
        })
    return pd.DataFrame(rows)


def test_analyze_invariants():
    result = S.analyze(synthetic_obs(), 0, 1)
    cat = result["by_category"]
    assert cat.n.sum() == 40
    assert result["summary"]["observations_used"] == 40
    assert result["summary"]["RE_pooled"] == pytest.approx(0.5)  # (10*1 + 10*1 + 20*0)/40
    # 各大類 n 與平均
    by = cat.set_index("category")
    assert by.loc["犧牲觸擊", "n"] == 10 and by.loc["犧牲觸擊", "mean_runs_remaining"] == 1
    assert by.loc["安打", "mean_runs_on_play"] == 1 and by.loc["安打", "mean_runs_after"] == 0
    assert by.loc["三振", "mean_runs_remaining"] == 0


def test_analyze_transitions_and_bunt_compare():
    result = S.analyze(synthetic_obs(), 0, 1)
    trans = result["transitions"]
    sh = trans[trans.category == "犧牲觸擊"].iloc[0]
    assert sh.next_state == "1出局・二壘有人" and sh.pct_in_category == 1.0
    bunt = result["bunt_compare"].set_index("group")
    assert bunt.loc["有觀察到觸擊代碼", "n"] == 10
    assert bunt.loc["未偵測到觸擊代碼", "n"] == 30
    assert bunt.loc["有觀察到觸擊代碼", "mean_runs_remaining"] == 1
    assert bunt.loc["未偵測到觸擊代碼", "mean_runs_remaining"] == pytest.approx(1 / 3)


def test_small_groups_are_marked_insufficient():
    result = S.analyze(synthetic_obs(n=20), 0, 1)        # 觸擊組只有 5 筆 < MIN_N
    diff = result["bunt_compare"].iloc[2]
    assert diff["note"] == "資料不足"
    assert result["by_category"].set_index("category").loc["犧牲觸擊", "note"] == "資料不足"


def test_incomplete_half_is_excluded():
    obs = synthetic_obs()
    obs.loc[0, "half_complete"] = False
    result = S.analyze(obs, 0, 1)
    assert result["summary"]["observations_used"] == 39
    assert result["summary"]["excluded_incomplete_half"] == 1


def test_state_without_samples_fails_loudly():
    obs = synthetic_obs()
    obs["outs"] = 1          # 沒有 (0 出局, 一壘) 的觀察值
    with pytest.raises(Exception):
        S.analyze(obs, 0, 1)  # RE 表無樣本；應明確失敗，而不是回傳假數字
