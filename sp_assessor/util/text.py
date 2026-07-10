"""공용 문자열 정규화 유틸."""
from __future__ import annotations

import pandas as pd


def clean_str(value) -> str:
    """NaN/None-안전 문자열 정규화. pandas 는 빈 CSV 셀을 NaN(float) 으로 읽으므로
    `value or ""` 패턴은 NaN 이 truthy 라 "nan" 문자열을 만들어내는 버그가 된다."""
    if value is None:
        return ""
    if isinstance(value, float) and pd.isna(value):
        return ""
    s = str(value).strip()
    return "" if s.lower() == "nan" else s
