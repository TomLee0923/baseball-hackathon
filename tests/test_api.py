r"""
tests/test_api.py：階段 D 固定測試案例（對照目前 2023～2025 資料的已驗證數字）。

執行（專案根目錄，已啟用 .venv）：
    python -m pytest tests -v
前置：已執行 python src\re_engine.py（產生 data\processed\re_observations.csv）。
若資料更新（新增賽季或口徑變動），下方固定數字需同步更新。
"""
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402

client = TestClient(main.app)


def post(**overrides):
    body = {"inning": 8, "score_diff": -1, "outs": 0,
            "runner_on_first": True, "runner_on_second": False,
            "runner_on_third": False}
    body.update(overrides)
    return client.post("/scenario", json=body)


def test_meta():
    data = client.get("/").json()
    assert data["games"] == 1020
    assert data["used_in_calculation"] == ["outs", "bases"]


def test_re_table_has_24_states():
    table = client.get("/re-table").json()["table"]
    assert len(table) == 24
    cell = next(r for r in table if r["outs"] == 0 and r["bases"] == 1)
    assert cell["n"] == 4714
    assert cell["RE"] == pytest.approx(0.8099, abs=1e-3)


def test_first_base_zero_out_matches_validated_numbers():
    data = post().json()
    pooled = data["run_expectancy"][0]
    assert pooled["n"] == 4714
    assert pooled["RE"] == pytest.approx(0.8099, abs=1e-3)
    assert pooled["se"] == pytest.approx(0.0187, abs=1e-3)
    assert sum(o["n"] for o in data["outcomes"]) == 4714
    bunt = {b["group"]: b for b in data["bunt_comparison"]}
    assert bunt["有觀察到觸擊代碼"]["n"] == 963
    assert bunt["未偵測到觸擊代碼"]["n"] == 3751
    assert data["context"]["used_in_calculation"] is False


def test_inning_and_score_diff_do_not_change_statistics():
    a = post(inning=1, score_diff=5).json()
    b = post(inning=9, score_diff=-3).json()
    assert a["run_expectancy"] == b["run_expectancy"]
    assert a["outcomes"] == b["outcomes"]


def test_runner_flags_map_to_bases_code():
    data = post(runner_on_first=True, runner_on_third=True).json()
    assert data["state"]["bases"] == 5


def test_sparse_bunt_sample_is_marked_insufficient():
    data = post(outs=2, runner_on_first=True, runner_on_third=True).json()
    assert any("樣本不足" in line for line in data["interpretation"])


def test_invalid_outs_rejected():
    assert post(outs=3).status_code == 422
