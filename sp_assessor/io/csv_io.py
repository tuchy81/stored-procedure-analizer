"""CSV 로드/저장 유틸."""
from __future__ import annotations

import csv
from pathlib import Path

import pandas as pd


def read_csv(path: Path, dtype: dict[str, str] | None = None) -> pd.DataFrame:
    """UTF-8 CSV 로드. 빈 파일이나 부재 시 빈 DataFrame."""
    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame()
    return pd.read_csv(path, dtype=dtype, encoding="utf-8", keep_default_na=False, na_values=[""])


def write_csv(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False, encoding="utf-8", quoting=csv.QUOTE_MINIMAL, lineterminator="\n")


def read_headers(path: Path) -> tuple[str, ...]:
    if not path.exists():
        return ()
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        try:
            row = next(reader)
        except StopIteration:
            return ()
        return tuple(row)
