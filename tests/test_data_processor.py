r"""
tests/test_data_processor.py：資料匯入與品質檢查的單元測試（合成資料，不依賴真實檔案）。

執行（專案根目錄）：python -m pytest tests -v
"""
import json

import pandas as pd
import pytest

import data_processor as dp


def pa(inning=1, outs=0, bases=0, result="1B", end_outs=0, pitch=("B",),
       events=None, **extra):
    """建立一筆合成打席。"""
    if events is None:
        events = [{"type": "PITCH"}]
    base = {
        "inning": inning, "outs": outs, "bases": bases, "result": result,
        "awayScores": 0, "homeScores": 0, "endOuts": end_outs,
        "endBases": 0, "endAwayScores": 0, "endHomeScores": 0,
        "pitchCodes": list(pitch), "events": events,
    }
    base.update(extra)
    return base


def sub_only(inning=10):
    return pa(inning=inning, result="", pitch=(), events=[{"type": "SUB"}])


# ---------- 基本工具 ----------

@pytest.mark.parametrize("value, expected", [
    (3, 3), ("3", 3), (3.0, 3), (3.5, None), ("x", None),
    (None, None), (True, None), (float("nan"), None),
])
def test_as_int(value, expected):
    assert dp.as_int(value) == expected


@pytest.mark.parametrize("code, text", [
    (0, "無人在壘"), (1, "一壘有人"), (2, "二壘有人"), (3, "一、二壘有人"),
    (4, "三壘有人"), (5, "一、三壘有人"), (6, "二、三壘有人"), (7, "滿壘"),
    (8, "未知壘況"), (None, "未知壘況"),
])
def test_decode_bases(code, text):
    assert dp.decode_bases(code) == text


def test_bases_code_is_bitwise_sum():
    """壘況編碼：一壘 1、二壘 2、三壘 4，八種組合互不重複。"""
    codes = {a + 2 * b + 4 * c for a in (0, 1) for b in (0, 1) for c in (0, 1)}
    assert codes == set(range(8))


def test_make_game_id():
    assert dp.make_game_id({"seasonId": "CPBL-2025-JO", "seq": 343}) == "CPBL-2025-JO-343"
    with pytest.raises(ValueError):
        dp.make_game_id({"seasonId": "CPBL-2025-JO"})


def test_is_empty_result():
    assert dp.is_empty_result(None) and dp.is_empty_result("  ")
    assert dp.is_empty_result(float("nan"))
    assert not dp.is_empty_result("1B")


def test_serialize_json_field():
    assert dp.serialize_json_field(["B", "S"]) == '["B", "S"]'
    assert dp.serialize_json_field(None) == ""
    assert dp.serialize_json_field("SH") == "SH"


# ---------- 特殊紀錄辨識 ----------

def test_is_sub_only_record():
    assert dp.is_sub_only_record(sub_only())
    assert not dp.is_sub_only_record(pa())  # 有結果
    assert not dp.is_sub_only_record(
        pa(result="", pitch=(), events=[{"type": "SUB"}, {"type": "PITCH"}])
    )


def placeholder():
    return pa(inning=10, result="", pitch=(), events=[], outs=0, bases=2)


def test_tiebreak_placeholder_detected():
    lst = [
        pa(inning=9, end_outs=3),
        placeholder(),
        pa(inning=10, bases=2, result="1B"),
    ]
    assert dp.is_tiebreak_placeholder(lst, 1) is True


def test_tiebreak_placeholder_skips_substitution_rows():
    lst = [
        pa(inning=9, end_outs=3),
        sub_only(10),
        placeholder(),
        sub_only(10),
        pa(inning=10, bases=2, result="1B"),
    ]
    assert dp.is_tiebreak_placeholder(lst, 2) is True


@pytest.mark.parametrize("mutate", [
    lambda l: l[1].update(inning=9),                    # 未到第 10 局
    lambda l: l[1].update(result="1B"),                 # 有結果
    lambda l: l[1].update(pitchCodes=["B"]),            # 有投球
    lambda l: l[2].update(bases=0),                     # 下一筆不是二壘有人
    lambda l: l[0].update(endOuts=2),                   # 前一筆沒有打滿 3 出局
])
def test_tiebreak_placeholder_rejected(mutate):
    lst = [
        pa(inning=9, end_outs=3),
        placeholder(),
        pa(inning=10, bases=2, result="1B"),
    ]
    mutate(lst)
    assert dp.is_tiebreak_placeholder(lst, 1) is False


def test_placeholder_needs_neighbours():
    assert dp.is_tiebreak_placeholder([placeholder()], 0) is False


# ---------- 單場展開 ----------

def make_game(away, home):
    return {"seasonId": "CPBL-2025-JO", "seq": 1, "awayPAList": away, "homePAList": home}


def test_extract_plate_appearances_basic():
    rows = dp.extract_plate_appearances(
        make_game([pa(), pa(outs=1)], [pa()]), "f.json", "CPBL-2025-JO-1"
    )
    assert len(rows) == 3
    assert [r["team_side"] for r in rows] == ["away", "away", "home"]
    assert all(r["game_id"] == "CPBL-2025-JO-1" for r in rows)
    assert all(r["record_type"] == "plate_appearance" for r in rows)
    assert all(r["missing_fields"] == "" for r in rows)


def test_extract_marks_placeholder_and_missing_keys():
    away = [
        pa(inning=9, end_outs=3),
        placeholder(),
        pa(inning=10, bases=2, result="1B"),
    ]
    del away[2]["endBases"]
    rows = dp.extract_plate_appearances(make_game(away, [pa()]), "f.json", "g")
    assert rows[1]["record_type"] == "tiebreak_runner_placeholder"
    assert rows[2]["missing_fields"] == "endBases"


def test_extract_rejects_bad_structure():
    with pytest.raises(ValueError):
        dp.extract_plate_appearances({"awayPAList": []}, "f", "g")  # 缺 homePAList
    with pytest.raises(ValueError):
        dp.extract_plate_appearances(make_game([], []), "f", "g")   # 全空
    with pytest.raises(ValueError):
        dp.extract_plate_appearances(make_game(["x"], [pa()]), "f", "g")


# ---------- 空 result 分類與品質旗標 ----------

def runner_out_events():
    return [{"type": "PITCH", "runners": [{"type": "CS", "isOut": True}]}]


def frame(rows):
    df = pd.DataFrame(rows)
    df["record_type"] = "plate_appearance"
    df["missing_fields"] = ""
    return df


def test_classify_empty_result_rows():
    rows = [
        dict(pa(), game_id="g", team_side="away"),                                  # 0 一般
        dict(pa(result="", pitch=(), events=[{"type": "SUB"}], outs=1, end_outs=1),
             game_id="g", team_side="away"),                                        # 1 純換人
        dict(pa(result="", outs=2, end_outs=3, events=runner_out_events()),
             game_id="g", team_side="away"),                                        # 2 跑壘出局
        dict(pa(result="", outs=1, end_outs=1), game_id="g", team_side="away"),     # 3 比賽中斷（該隊最後一筆）
        dict(pa(result="", outs=0, end_outs=0), game_id="g", team_side="home"),     # 4 未知空 result
        dict(pa(), game_id="g", team_side="home"),                                  # 5 一般（讓 4 不是最後一筆）
    ]
    df = frame(rows)
    dp.add_quality_flags(df)
    assert df.record_type.tolist() == [
        "plate_appearance", "substitution_only", "baserunning_inning_end",
        "game_ended_mid_pa", "plate_appearance", "plate_appearance",
    ]
    # 只有「未知空 result」需要人工確認
    assert df.needs_review.tolist() == [False, False, False, False, True, False]
    assert "result 為空" in df.loc[4, "review_reason"]


def test_quality_flags_count_invalid_values():
    rows = [
        dict(pa(outs=3), game_id="g", team_side="away"),
        dict(pa(bases=9), game_id="g", team_side="away"),
        dict(pa(inning="x"), game_id="g", team_side="away"),
        dict(pa(), game_id="g", team_side="away"),
    ]
    df = frame(rows)
    counts = dp.add_quality_flags(df)
    assert counts["invalid_outs"] == 1
    assert counts["invalid_bases"] == 1
    assert counts["invalid_inning"] == 1
    assert df.needs_review.tolist() == [True, True, True, False]


# ---------- 讀檔、重複 ID、安全寫檔 ----------

def write_game(path, seq, season="CPBL-2025-JO"):
    path.write_text(json.dumps(
        {"seasonId": season, "seq": seq, "awayPAList": [pa()], "homePAList": [pa()]}
    ), encoding="utf-8")


def test_collect_games_blocks_duplicates_and_reports_failures(tmp_path):
    a, b, c, bad = (tmp_path / n for n in ("a.json", "b.json", "c.json", "bad.json"))
    write_game(a, 1)
    write_game(b, 1)          # 與 a 同一 game_id
    write_game(c, 2)
    bad.write_text("[1, 2]", encoding="utf-8")   # 最外層不是物件

    accepted, report = dp.collect_games([a, b, c, bad])
    assert [game_id for *_, game_id in accepted] == ["CPBL-2025-JO-2"]
    statuses = sorted(r["status"] for r in report)
    assert statuses == ["duplicate_game_id", "duplicate_game_id", "failed"]


def test_atomic_write_replaces_file(tmp_path):
    target = tmp_path / "out.csv"
    dp.atomic_write(target, lambda p: p.write_text("new", encoding="utf-8"))
    assert target.read_text(encoding="utf-8") == "new"
    assert not list(tmp_path.glob("*.tmp"))


def test_atomic_write_failure_keeps_old_file(tmp_path):
    target = tmp_path / "out.csv"
    target.write_text("old", encoding="utf-8")

    def boom(path):
        path.write_text("partial", encoding="utf-8")
        raise RuntimeError("寫到一半失敗")

    with pytest.raises(RuntimeError):
        dp.atomic_write(target, boom)
    assert target.read_text(encoding="utf-8") == "old"
    assert not list(tmp_path.glob("*.tmp"))
