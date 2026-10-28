r"""
tests/test_re_engine.py：RE 引擎的單元測試。
以手算過的小型合成比賽驗證觀察單位、剩餘得分、狀態轉移與 24 格 RE。
"""
import json

import pandas as pd
import pytest

import re_engine as R


def row(game, side, inning, outs, bases, result, away, home, end_outs,
        events=None, record_type="plate_appearance", pitch="[]"):
    return {
        "game_id": game, "team_side": side, "inning": inning,
        "outs": outs, "bases": bases, "result": result,
        "awayScores": away, "homeScores": home,
        "endOuts": end_outs, "endAwayScores": away, "endHomeScores": home,
        "pitchCodes": pitch, "record_type": record_type,
        "events": json.dumps(events if events is not None else [{"type": "PITCH"}]),
        "date": "2025-01-01", "RE": 0.5,
    }


G = "CPBL-2025-JO-1"


def toy_game():
    """
    客隊第 1 局：1B → HR（得 2 分）→ SO → SO → SO，最終 2 分。
    主隊第 1 局：SO、SO、SO（0 分）。
    客隊第 2 局：開局打席的打席前比分為 2（用來推回第 1 局最終得分）。
    """
    return [
        row(G, "away", 1, 0, 0, "1B", 0, 0, 0),
        row(G, "away", 1, 0, 1, "HR", 0, 0, 0),
        row(G, "away", 1, 0, 0, "SO", 2, 0, 1),
        row(G, "away", 1, 1, 0, "SO", 2, 0, 2),
        row(G, "away", 1, 2, 0, "SO", 2, 0, 3),
        row(G, "home", 1, 0, 0, "SO", 2, 0, 1),
        row(G, "home", 1, 1, 0, "SO", 2, 0, 2),
        row(G, "home", 1, 2, 0, "SO", 2, 0, 3),
        row(G, "away", 2, 0, 0, "SO", 2, 0, 1),   # 之後只有一個打席：半局未打滿 3 出局
    ]


@pytest.fixture
def obs():
    return R.build_observations(pd.DataFrame(toy_game()))[0]


def test_remaining_runs_use_next_half_pre_score(obs):
    away1 = obs[(obs.half == 1)]
    assert away1.runs_remaining.tolist() == [2, 2, 0, 0, 0]
    assert (away1.final_source == "next_half_pre_score").all()


def test_after_and_on_play_decomposition(obs):
    away1 = obs[(obs.half == 1)]
    assert away1.runs_on_play.tolist() == [0, 2, 0, 0, 0]
    assert away1.runs_after.tolist() == [2, 0, 0, 0, 0]
    assert (obs.runs_remaining == obs.runs_on_play + obs.runs_after).all()


def test_next_state_uses_next_real_pa_not_end_bases(obs):
    away1 = obs[(obs.half == 1)].reset_index(drop=True)
    assert (away1.loc[0, "next_outs"], away1.loc[0, "next_bases"]) == (0, 1)
    assert (away1.loc[1, "next_outs"], away1.loc[1, "next_bases"]) == (0, 0)
    assert bool(away1.loc[4, "next_is_half_end"]) is True
    assert pd.isna(away1.loc[4, "next_outs"])


def test_incomplete_half_flagged_and_excluded(obs):
    last = obs[obs.half == 3]
    assert (~last.half_complete).all()
    table = R.compute_re_table(obs)
    assert table.n.sum() == int(obs.half_complete.sum()) == 8


def test_re_table_has_24_states_and_correct_means(obs):
    table = R.compute_re_table(obs)
    assert len(table) == 24
    cell = lambda o, b: table[(table.outs == o) & (table.bases == b)].iloc[0]
    # (0 出局, 無人)：客隊 1B 打席、客隊 HR 後的 SO 打席、主隊第一個 SO 打席，共 3 筆
    assert cell(0, 0).n == 3
    assert cell(0, 0).RE == pytest.approx(2 / 3)
    assert cell(0, 1).RE == 2 and cell(0, 1).n == 1
    assert cell(1, 0).RE == 0
    assert cell(2, 7).n == 0 and cell(2, 7).note == "無樣本"
    assert cell(0, 1).note == "樣本數 < 100"


def test_select_real_pa_excludes_placeholder_and_sub_only():
    rows = toy_game()
    rows.insert(0, row(G, "away", 10, 0, 2, "", 0, 0, 0, events=[],
                       record_type="tiebreak_runner_placeholder"))
    rows.insert(1, row(G, "away", 1, 0, 0, "", 0, 0, 0,
                       events=[{"type": "SUB"}], record_type="substitution_only"))
    real = R.select_real_pa(pd.DataFrame(rows))
    assert len(real) == len(toy_game())
    assert "tiebreak_runner_placeholder" not in set(real.record_type)


def test_baserunning_interruption_is_kept_as_observation():
    """跑壘出局中斷的打席：result 為空，但打席前狀態是真實觀察，應納入。"""
    rows = [
        row(G, "away", 1, 0, 0, "SO", 0, 0, 1),
        row(G, "away", 1, 1, 0, "SO", 0, 0, 2),
        row(G, "away", 1, 2, 1, "", 0, 0, 3, record_type="baserunning_inning_end",
            events=[{"type": "PITCH", "runners": [{"type": "CS", "isOut": True}]}]),
        row(G, "home", 1, 0, 0, "SO", 0, 0, 1),
        row(G, "home", 1, 1, 0, "SO", 0, 0, 2),
        row(G, "home", 1, 2, 0, "SO", 0, 0, 3),
        row(G, "away", 2, 0, 0, "SO", 0, 0, 1),
    ]
    o = R.build_observations(pd.DataFrame(rows))[0]
    kept = o[(o.half == 1) & (o.outs == 2) & (o.bases == 1)]
    assert len(kept) == 1 and bool(kept.half_complete.iloc[0])
    table = R.compute_re_table(o)
    assert table[(table.outs == 2) & (table.bases == 1)].n.iloc[0] == 1


def test_by_season_table_matches_pooled_for_single_season(obs):
    wide = R.build_by_season_table(obs)
    assert (wide["n_CPBL-2025-JO"] == wide["n_pooled"]).all()
    assert wide["RE_pooled"].fillna(-1).tolist() == wide["RE_CPBL-2025-JO"].fillna(-1).tolist()


def test_write_outputs_is_all_or_nothing(tmp_path):
    good, bad = tmp_path / "a.csv", tmp_path / "b.csv"
    good.write_text("old-a", encoding="utf-8")
    bad.write_text("old-b", encoding="utf-8")

    def boom(path):
        raise RuntimeError("第二個檔寫入失敗")

    with pytest.raises(RuntimeError):
        R.write_outputs([
            (good, lambda p: p.write_text("new-a", encoding="utf-8")),
            (bad, boom),
        ])
    assert good.read_text(encoding="utf-8") == "old-a"   # 第一個檔也沒被取代
    assert bad.read_text(encoding="utf-8") == "old-b"
    assert not list(tmp_path.glob("*.tmp"))
