"""지표 계산 — LOC, 분기/루프, SELECT/서브쿼리 중첩, DML, 동적SQL, 예외, 서브프로그램 수."""
from __future__ import annotations

import re

from sqlscore.model import Metrics, SourceObject
from sqlscore.textutil import clean

# 분기/루프
IF_RE = re.compile(r"\bIF\b", re.IGNORECASE)          # 'END IF' 포함 → END IF 개수만큼 보정
ELSIF_RE = re.compile(r"\bELSIF\b", re.IGNORECASE)
END_IF_RE = re.compile(r"\bEND\s+IF\b", re.IGNORECASE)
WHEN_RE = re.compile(r"\bWHEN\b", re.IGNORECASE)       # CASE arm + 예외 arm
LOOP_RE = re.compile(r"\bLOOP\b", re.IGNORECASE)       # 'END LOOP' 포함 → //2 로 루프 수 산정

DML_RE = re.compile(r"\b(INSERT|UPDATE|DELETE|MERGE)\b", re.IGNORECASE)
DYN_EXEC_RE = re.compile(r"\bEXECUTE\s+IMMEDIATE\b", re.IGNORECASE)
DBMS_SQL_RE = re.compile(r"\bDBMS_SQL\s*\.", re.IGNORECASE)
EXCEPTION_WHEN_RE = re.compile(r"\bWHEN\s+(?:[A-Za-z_][A-Za-z0-9_$#]*|OTHERS)\s+THEN\b", re.IGNORECASE)
WHEN_OTHERS_RE = re.compile(r"\bWHEN\s+OTHERS\b", re.IGNORECASE)

# 패키지/타입 바디 내부 서브프로그램
SUBPROG_RE = re.compile(r"\b(?:PROCEDURE|FUNCTION)\s+[A-Za-z_][A-Za-z0-9_$#]*", re.IGNORECASE)

# 괄호 및 SELECT 토큰 (서브쿼리 중첩 계산용)
PAREN_SELECT_RE = re.compile(r"\(|\)|\bSELECT\b", re.IGNORECASE)


def _compute_loc(lines: list[tuple[int, str]]) -> int:
    loc = 0
    for _, text in lines:
        s = text.strip()
        if not s or s.startswith("--"):
            continue
        loc += 1
    return loc


def _query_nesting(text_clean: str) -> tuple[int, int]:
    """(SELECT 총 개수, 최대 서브쿼리 중첩 깊이)."""
    depth = 0
    max_nest = 0
    count = 0
    for m in PAREN_SELECT_RE.finditer(text_clean):
        tok = m.group(0)
        if tok == "(":
            depth += 1
        elif tok == ")":
            depth = max(0, depth - 1)
        else:  # SELECT
            count += 1
            max_nest = max(max_nest, depth)
    return count, max_nest


def compute_metrics(obj: SourceObject) -> Metrics:
    joined = "\n".join(t for _, t in obj.lines)
    text = clean(joined)

    loc = _compute_loc(obj.lines)

    # 루프: FOR/WHILE/기본 LOOP 모두 'LOOP ... END LOOP' 쌍을 가지므로 LOOP 토큰 //2.
    # (FOR 키워드로 세면 트리거 'FOR EACH ROW' 오탐 및 FOR-LOOP 이중계수 발생)
    loop_count = len(LOOP_RE.findall(text)) // 2

    # 분기: IF 문 수(= IF 토큰 - END IF) + ELSIF + CASE arm.
    # CASE arm 은 전체 WHEN 에서 예외 핸들러 WHEN 을 제외해 근사 (예외는 별도 성분).
    exc_handlers = len(EXCEPTION_WHEN_RE.findall(text))
    if_stmts = max(len(IF_RE.findall(text)) - len(END_IF_RE.findall(text)), 0)
    case_arms = max(len(WHEN_RE.findall(text)) - exc_handlers, 0)
    branch_count = if_stmts + len(ELSIF_RE.findall(text)) + case_arms

    query_count, max_nest = _query_nesting(text)
    dml_count = len(DML_RE.findall(text))
    dynamic_sql = len(DYN_EXEC_RE.findall(text)) + len(DBMS_SQL_RE.findall(text))
    when_others = 1 if WHEN_OTHERS_RE.search(text) else 0

    subprog = 0
    if obj.otype in ("PACKAGE", "PACKAGE_BODY", "TYPE", "TYPE_BODY"):
        subprog = len(SUBPROG_RE.findall(text))

    return Metrics(
        loc=loc, branch_count=branch_count, loop_count=loop_count,
        query_count=query_count, max_query_nesting=max_nest,
        dml_count=dml_count, dynamic_sql_count=dynamic_sql,
        exception_handler_count=exc_handlers, when_others=when_others,
        subprogram_count=subprog,
    )
