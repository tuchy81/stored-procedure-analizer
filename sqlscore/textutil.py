"""SQL 텍스트 전처리 (주석/문자열 마스킹) — s2_metrics 정규식 계열과 동일 규약."""
from __future__ import annotations

import re

BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
STRING_LITERAL_RE = re.compile(r"'(?:[^']|'')*'")


def strip_comments(text: str) -> str:
    """블록(/* */) 및 라인(--) 주석 제거. 라인 구조는 보존."""
    no_block = BLOCK_COMMENT_RE.sub(lambda m: "\n" * m.group(0).count("\n"), text)
    out_lines = []
    for line in no_block.split("\n"):
        idx = line.find("--")
        out_lines.append(line[:idx] if idx >= 0 else line)
    return "\n".join(out_lines)


def mask_strings(text: str) -> str:
    """문자열 리터럴을 빈 리터럴('')로 치환 — 키워드 오탐 방지."""
    return STRING_LITERAL_RE.sub("''", text)


def clean(text: str) -> str:
    """주석 제거 + 문자열 마스킹."""
    return mask_strings(strip_comments(text))
