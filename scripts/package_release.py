r"""
scripts/package_release.py：把專案打包成可交付的 ZIP。

預設排除：虛擬環境（.venv、.venv-*）、快取（__pycache__、.pytest_cache）、
.git、暫存檔，以及體積大的 data\raw 與 data\processed（依 README 可重建）。
檔名一律以 UTF-8 寫入 ZIP（標記 UTF-8 旗標），避免中文檔名在解壓後變亂碼。

用法（專案根目錄）：
    python scripts\package_release.py                    # 只含程式、測試與文件
    python scripts\package_release.py --include-api-data  # 另含 API 所需的小型資料（約 10 MB，可直接啟動 API）
    python scripts\package_release.py --include-processed # 另含完整 data\processed（約 150 MB）
    python scripts\package_release.py --include-raw       # 另含 data\raw
    python scripts\package_release.py --include-git       # 另含 .git（版本歷史）
"""
import argparse
import fnmatch
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

ALWAYS_EXCLUDE_DIRS = {".venv", "__pycache__", ".pytest_cache", ".mypy_cache"}
ALWAYS_EXCLUDE_PATTERNS = ["*.tmp", "*.pyc", ".DS_Store", "*.zip"]
# 啟動 API 與查看結果所需的小型檔案（不含 139 MB 的逐打席主資料）
API_DATA_FILES = {"re_observations.csv", "re_table.csv", "re_by_season.csv", "re_summary.json"}


def is_excluded(rel_parts, include_raw, include_processed, include_git, include_api_data=False):
    """rel_parts：相對專案根目錄的路徑分段。"""
    for part in rel_parts[:-1] + (rel_parts[-1],):
        if part in ALWAYS_EXCLUDE_DIRS or part.startswith(".venv"):
            return True
    if not include_git and ".git" in rel_parts[:-1]:
        return True
    if not include_git and rel_parts[0] == ".git":
        return True
    if len(rel_parts) >= 2 and rel_parts[0] == "data":
        if rel_parts[1] == "raw" and not include_raw:
            return True
        if rel_parts[1] == "processed" and not include_processed:
            small_api_file = len(rel_parts) == 3 and rel_parts[2] in API_DATA_FILES
            if not (include_api_data and small_api_file):
                return True
    return any(fnmatch.fnmatch(rel_parts[-1], p) for p in ALWAYS_EXCLUDE_PATTERNS)


def build_zip(root, out_path, include_raw=False, include_processed=False,
              include_git=False, include_api_data=False):
    """回傳寫入 ZIP 的檔案相對路徑清單。"""
    root, out_path = Path(root), Path(out_path)
    written = []
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.resolve() == out_path.resolve():
                continue
            rel = path.relative_to(root)
            if is_excluded(rel.parts, include_raw, include_processed, include_git, include_api_data):
                continue
            archive.write(path, arcname=rel.as_posix())  # 非 ASCII 檔名會自動標記 UTF-8
            written.append(rel.as_posix())
    return written


def main(argv=None):
    parser = argparse.ArgumentParser(description="打包可交付 ZIP")
    parser.add_argument("--output", type=Path,
                        default=PROJECT_ROOT.parent / f"{PROJECT_ROOT.name}-release.zip")
    parser.add_argument("--include-raw", action="store_true")
    parser.add_argument("--include-processed", action="store_true")
    parser.add_argument("--include-api-data", action="store_true",
                        help="只附 API 所需的小型資料檔（re_observations.csv 等）")
    parser.add_argument("--include-git", action="store_true")
    args = parser.parse_args(argv)

    files = build_zip(PROJECT_ROOT, args.output, args.include_raw,
                      args.include_processed, args.include_git, args.include_api_data)
    size_mb = args.output.stat().st_size / 1024 / 1024
    print(f"已輸出：{args.output}（{len(files)} 個檔案，{size_mb:.1f} MB）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
