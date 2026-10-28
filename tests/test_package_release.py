r"""tests/test_package_release.py：打包腳本的排除規則與中文檔名測試。"""
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import package_release as pr  # noqa: E402


def make_tree(root):
    files = [
        "README.md", "src/main.py", "tests/test_x.py", "requirements.txt",
        ".venv/Lib/x.py", ".venv-1/Lib/y.py", "src/__pycache__/m.pyc",
        ".pytest_cache/v/cache", ".git/HEAD", "data/raw/2025/中華職棒2025年-G1.json",
        "data/processed/plate_appearances_all.csv", "scratch.tmp",
    ]
    for f in files:
        p = root / f
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x", encoding="utf-8")


def test_default_excludes_env_cache_git_and_data(tmp_path):
    make_tree(tmp_path)
    out = tmp_path.parent / "out_default.zip"
    written = pr.build_zip(tmp_path, out)
    assert sorted(written) == ["README.md", "requirements.txt", "src/main.py", "tests/test_x.py"]


def test_optional_flags(tmp_path):
    make_tree(tmp_path)
    out = tmp_path.parent / "out_flags.zip"
    written = pr.build_zip(tmp_path, out, include_raw=True, include_processed=True, include_git=True)
    assert "data/raw/2025/中華職棒2025年-G1.json" in written
    assert "data/processed/plate_appearances_all.csv" in written
    assert ".git/HEAD" in written
    assert not any(w.startswith(".venv") or "__pycache__" in w for w in written)


def test_chinese_filenames_are_flagged_utf8(tmp_path):
    make_tree(tmp_path)
    out = tmp_path.parent / "out_utf8.zip"
    pr.build_zip(tmp_path, out, include_raw=True)
    with zipfile.ZipFile(out) as z:
        info = z.getinfo("data/raw/2025/中華職棒2025年-G1.json")
        assert info.flag_bits & 0x800          # UTF-8 旗標


def test_api_data_flag_adds_only_small_files(tmp_path):
    make_tree(tmp_path)
    for name in ("re_observations.csv", "re_table.csv", "re_by_season.csv", "re_summary.json"):
        (tmp_path / "data" / "processed" / name).write_text("x", encoding="utf-8")
    out = tmp_path.parent / "out_api.zip"
    written = pr.build_zip(tmp_path, out, include_api_data=True)
    assert "data/processed/re_observations.csv" in written
    assert "data/processed/re_table.csv" in written
    assert "data/processed/plate_appearances_all.csv" not in written   # 大型主資料仍排除
    assert not any(w.startswith("data/raw") for w in written)
