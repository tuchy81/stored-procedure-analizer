"""S2 — 정적 분석 (지표 산출).

v0.1 은 config.parser.primary 기본값이 "regex" 로 고정되어 있음 (§0 스파이크 전까지
ANTLR PL/SQL 문법은 optional/미도입 상태 — pyproject.toml, core/config.py 참조).
따라서 이 모듈은 REGEX 티어를 1차 구현으로 하고, ANTLR 이 실제로 설치·구성된 경우를
대비한 확장 지점만 남겨둔다 (parser.primary == "antlr4-plsql" 인데 그 파서가 없으면
PARTIAL/REGEX 로 안전하게 폴백하고 로그에 남긴다).
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

import pandas as pd

from sp_assessor.core.config import Config
from sp_assessor.core.paths import ProjectPaths
from sp_assessor.io.csv_io import read_csv, write_csv
from sp_assessor.io.csv_schemas import OVERRIDE_SCHEMAS
from sp_assessor.stages.s1_inventory import PKG_SUBPROG_RE, DBLINK_REF_RE, _make_sp_id
from sp_assessor.util.text import clean_str


BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
STRING_LITERAL_RE = re.compile(r"'(?:[^']|'')*'")
LITERAL_ONLY_RE = re.compile(r"^\s*'(?:[^']|'')*'(?:\s*\|\|\s*'(?:[^']|'')*')*\s*$")
EXEC_IMMEDIATE_RE = re.compile(
    r"EXECUTE\s+IMMEDIATE\s+(.+?)(?=\s+INTO\s|\s+USING\s|;)", re.IGNORECASE | re.DOTALL
)

BRANCH_KEYWORDS = ("IF", "ELSIF", "CASE", "LOOP", "WHILE", "FOR")
KEYWORD_RE = {kw: re.compile(rf"\b{kw}\b", re.IGNORECASE) for kw in BRANCH_KEYWORDS}

CURSOR_EXPLICIT_RE = re.compile(r"\bCURSOR\s+[A-Za-z_][A-Za-z0-9_$#]*\s+IS\b", re.IGNORECASE)
CURSOR_FOR_LOOP_RE = re.compile(r"\bFOR\s+[A-Za-z_][A-Za-z0-9_$#]*\s+IN\s*[\(\w]", re.IGNORECASE)
REFCURSOR_DECL_RE = re.compile(r"\bSYS_REFCURSOR\b", re.IGNORECASE)

TX_CONTROL_RE = re.compile(r"\b(COMMIT|ROLLBACK|SAVEPOINT)\b", re.IGNORECASE)
AUTONOMOUS_TX_RE = re.compile(r"PRAGMA\s+AUTONOMOUS_TRANSACTION", re.IGNORECASE)

CONNECT_BY_RE = re.compile(r"\bCONNECT\s+BY\b", re.IGNORECASE)
MERGE_RE = re.compile(r"\bMERGE\s+INTO\b", re.IGNORECASE)
ANALYTIC_OVER_RE = re.compile(r"\)\s*OVER\s*\(", re.IGNORECASE)
BULK_COLLECT_RE = re.compile(r"\bBULK\s+COLLECT\b", re.IGNORECASE)
FORALL_RE = re.compile(r"\bFORALL\b", re.IGNORECASE)
UTL_PKG_RE = re.compile(r"\bUTL_[A-Z0-9_]*\s*\.", re.IGNORECASE)
DBMS_PKG_RE = re.compile(r"\bDBMS_[A-Z0-9_]*\s*\.", re.IGNORECASE)

EXCEPTION_RE = re.compile(r"\bEXCEPTION\b", re.IGNORECASE)
WHEN_OTHERS_RE = re.compile(r"\bWHEN\s+OTHERS\b", re.IGNORECASE)
GOTO_RE = re.compile(r"\bGOTO\b", re.IGNORECASE)

STMT_START_RE = re.compile(r"\b(SELECT|INSERT|UPDATE|DELETE|MERGE|TRUNCATE)\b", re.IGNORECASE)

PARSE_CONFIDENCE = {"AST": 0.95, "PARTIAL": 0.4, "REGEX": 0.6, "SKIPPED": 0.0, "FAILED": 0.1}

RESERVED_DECL_SKIP = {"BEGIN", "END", "IS", "AS", "PACKAGE", "BODY"}


def _pkg_or_none(value) -> str | None:
    """PACKAGE_NAME 컬럼값 정규화 (NaN/빈문자열 → None)."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    s = str(value).strip()
    return s if s and s.lower() != "nan" else None


# ---------------------------------------------------------------------------
# SP 본문 재조립 (S1 패키지 해체 경계 재사용)
# ---------------------------------------------------------------------------

def _package_subprogram_slices(pkg_lines: pd.DataFrame) -> dict[str, tuple[int, int]]:
    """패키지 본문 라인에서 서브프로그램별 (start_line, end_line) 슬라이스 산출."""
    pkg_lines = pkg_lines.sort_values("LINE")
    starts: list[tuple[int, str]] = []
    for _, row in pkg_lines.iterrows():
        m = PKG_SUBPROG_RE.match(str(row.get("TEXT", "")))
        if m:
            starts.append((int(row["LINE"]), m.group(1).upper()))
    if not starts:
        return {}
    max_line = int(pkg_lines["LINE"].max())
    slices: dict[str, tuple[int, int]] = {}
    for i, (start, name) in enumerate(starts):
        end = starts[i + 1][0] - 1 if i + 1 < len(starts) else max_line
        slices[name] = (start, end)
    return slices


def _build_sp_bodies(inventory: pd.DataFrame, sources: pd.DataFrame) -> dict[str, list[tuple[int, str]]]:
    """SP_ID -> [(LINE, TEXT), ...] (오름차순)."""
    bodies: dict[str, list[tuple[int, str]]] = {}
    if inventory.empty or sources.empty:
        return bodies

    plain_types = {"PROCEDURE", "FUNCTION", "TRIGGER"}
    plain = inventory[inventory["TYPE"].isin(plain_types)]
    for _, row in plain.iterrows():
        hit = sources[(sources["OWNER"] == row["OWNER"]) & (sources["NAME"] == row["NAME"]) &
                      (sources["TYPE"] == row["TYPE"])].sort_values("LINE")
        if not hit.empty:
            bodies[row["SP_ID"]] = list(zip(hit["LINE"].astype(int), hit["TEXT"].astype(str)))

    pkg_rows = inventory[inventory["TYPE"] == "PACKAGE_SUBPROGRAM"]
    for (owner, pkg), grp in pkg_rows.groupby(["OWNER", "PKG"]):
        pkg_src = sources[(sources["OWNER"] == owner) & (sources["NAME"] == pkg) &
                          (sources["TYPE"] == "PACKAGE BODY")]
        if pkg_src.empty:
            continue
        slices = _package_subprogram_slices(pkg_src)
        pkg_src_sorted = pkg_src.sort_values("LINE")
        for _, row in grp.iterrows():
            bound = slices.get(row["NAME"])
            if not bound:
                continue
            start, end = bound
            sel = pkg_src_sorted[(pkg_src_sorted["LINE"] >= start) & (pkg_src_sorted["LINE"] <= end)]
            bodies[row["SP_ID"]] = list(zip(sel["LINE"].astype(int), sel["TEXT"].astype(str)))

    return bodies


# ---------------------------------------------------------------------------
# 전처리
# ---------------------------------------------------------------------------

def _compute_loc(lines: list[tuple[int, str]]) -> int:
    loc = 0
    for _, text in lines:
        s = text.strip()
        if not s or s.startswith("--"):
            continue
        loc += 1
    return loc


def _strip_comments(joined: str) -> str:
    no_block = BLOCK_COMMENT_RE.sub(" ", joined)
    out_lines = []
    for line in no_block.split("\n"):
        idx = line.find("--")
        out_lines.append(line[:idx] if idx >= 0 else line)
    return "\n".join(out_lines)


def _mask_strings(text: str) -> str:
    return STRING_LITERAL_RE.sub("''", text)


# ---------------------------------------------------------------------------
# 동적 SQL (EXECUTE IMMEDIATE) 분리
# ---------------------------------------------------------------------------

def _find_dynamic_sql(text_nocomment: str) -> list[dict]:
    results = []
    for m in EXEC_IMMEDIATE_RE.finditer(text_nocomment):
        expr = m.group(1).strip()
        kind = "LITERAL" if LITERAL_ONLY_RE.match(expr) else "VARIABLE"
        results.append({"expr": expr, "kind": kind, "pos": m.start()})
    return results


def _extract_dynsql_variables(expr: str) -> list[str]:
    masked = STRING_LITERAL_RE.sub(" ", expr)
    tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_$#]*", masked)
    return [t for t in tokens if t.upper() not in ("USING", "INTO")]


def _line_for_pos(text_nocomment: str, pos: int, line_offsets: list[tuple[int, int]]) -> int:
    for start_off, line_no in line_offsets:
        if pos < start_off:
            return line_no
    return line_offsets[-1][1] if line_offsets else 0


def _build_line_offsets(lines: list[tuple[int, str]]) -> list[tuple[int, int]]:
    """각 라인 시작 문자 오프셋(누적) → LINE 번호. pos 이하 최초 초과 오프셋 탐색용."""
    offsets = []
    cursor = 0
    for line_no, text in lines:
        cursor += len(text) + 1  # +1 = "\n" join separator
        offsets.append((cursor, line_no))
    return offsets


# ---------------------------------------------------------------------------
# SQL 문 인벤토리
# ---------------------------------------------------------------------------

def _extract_tables(stmt: str, dml_type: str) -> list[str]:
    tables: list[str] = []
    dml_type = dml_type.upper()
    if dml_type == "SELECT":
        for m in re.finditer(r"\b(?:FROM|JOIN)\s+([A-Za-z0-9_$#.]+(?:\s*,\s*[A-Za-z0-9_$#.]+)*)",
                             stmt, re.IGNORECASE):
            tables.extend(t.strip() for t in m.group(1).split(","))
    elif dml_type == "INSERT":
        m = re.search(r"\bINTO\s+([A-Za-z0-9_$#.]+)", stmt, re.IGNORECASE)
        if m:
            tables.append(m.group(1))
    elif dml_type == "UPDATE":
        m = re.search(r"\bUPDATE\s+([A-Za-z0-9_$#.]+)", stmt, re.IGNORECASE)
        if m:
            tables.append(m.group(1))
    elif dml_type == "DELETE":
        m = re.search(r"\bFROM\s+([A-Za-z0-9_$#.]+)", stmt, re.IGNORECASE)
        if m:
            tables.append(m.group(1))
        elif (m := re.search(r"\bDELETE\s+([A-Za-z0-9_$#.]+)", stmt, re.IGNORECASE)):
            tables.append(m.group(1))
    elif dml_type == "MERGE":
        m = re.search(r"\bMERGE\s+INTO\s+([A-Za-z0-9_$#.]+)", stmt, re.IGNORECASE)
        if m:
            tables.append(m.group(1))
    elif dml_type == "TRUNCATE":
        m = re.search(r"\bTABLE\s+([A-Za-z0-9_$#.]+)", stmt, re.IGNORECASE)
        if m:
            tables.append(m.group(1))
    return tables


def _sql_statements_static(text_clean: str) -> list[tuple[str, str]]:
    """세미콜론 단위 분절 후 DML 문장 추출 → (dml_type, stmt).

    청크 선두가 아니라 DML 키워드가 처음 등장하는 위치부터 잘라낸다 —
    `PROCEDURE X IS BEGIN INSERT INTO ...` 처럼 헤더/BEGIN 이 같은
    세미콜론 구간에 섞여 있거나 `OPEN cur FOR SELECT ...` 처럼 REF CURSOR
    OPEN 구문 뒤에 SELECT 가 오는 경우도 함께 잡기 위함.
    """
    out = []
    for chunk in text_clean.split(";"):
        m = STMT_START_RE.search(chunk)
        if not m:
            continue
        stmt = chunk[m.start():].strip()
        out.append((m.group(1).upper(), stmt))
    return out


# ---------------------------------------------------------------------------
# 패키지 전역변수
# ---------------------------------------------------------------------------

DECL_VAR_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_$#]*)\s+(?:CONSTANT\s+)?[A-Za-z]", re.IGNORECASE)


def _package_global_vars(pkg_src: pd.DataFrame, first_subprog_line: int) -> set[str]:
    names = set()
    preamble = pkg_src[pkg_src["LINE"] < first_subprog_line].sort_values("LINE")
    for _, row in preamble.iterrows():
        m = DECL_VAR_RE.match(str(row.get("TEXT", "")))
        if m and m.group(1).upper() not in RESERVED_DECL_SKIP:
            names.add(m.group(1).upper())
    return names


# ---------------------------------------------------------------------------
# 메인 지표 계산
# ---------------------------------------------------------------------------

METRIC_COLUMNS = [
    "SP_ID", "LOC", "BRANCH_COUNT",
    "CURSOR_EXPLICIT_COUNT", "CURSOR_FOR_LOOP_COUNT", "REFCURSOR_COUNT", "REF_CURSOR_OUT_COUNT",
    "DYNAMIC_SQL_LITERAL_COUNT", "DYNAMIC_SQL_VARIABLE_COUNT",
    "TX_CONTROL_COUNT", "AUTONOMOUS_TX_FLAG",
    "CONNECT_BY_COUNT", "MERGE_COUNT", "ANALYTIC_OVER_COUNT", "BULK_COLLECT_COUNT",
    "FORALL_COUNT", "UTL_PKG_COUNT", "DBMS_PKG_COUNT", "ORACLE_FEATURE_COUNT",
    "DBLINK_DIRECT_COUNT",
    "EXCEPTION_HANDLER_COUNT", "WHEN_OTHERS_FLAG", "GOTO_COUNT",
    "GLOBAL_PKG_VAR_REF_COUNT", "OUT_PARAM_COUNT", "MUTATING_TRIGGER_RISK",
    "PARSE_MODE", "PARSE_CONFIDENCE",
]


def _arg_counts(arguments: pd.DataFrame) -> tuple[dict[str, int], dict[str, int]]:
    """SP_ID -> (ref_cursor_out_count, out_param_count)."""
    ref_out: dict[str, int] = {}
    out_param: dict[str, int] = {}
    if arguments.empty:
        return ref_out, out_param
    for _, row in arguments.iterrows():
        pkg = _pkg_or_none(row.get("PACKAGE_NAME"))
        sp_id = _make_sp_id(row["OWNER"], pkg, row["OBJECT_NAME"], row.get("OVERLOAD"))
        in_out = str(row.get("IN_OUT", "")).upper()
        pls_type = str(row.get("PLS_TYPE", "")).upper()
        if "OUT" in in_out:
            out_param[sp_id] = out_param.get(sp_id, 0) + 1
            if "CURSOR" in pls_type:
                ref_out[sp_id] = ref_out.get(sp_id, 0) + 1
    return ref_out, out_param


def _trigger_tables(triggers: pd.DataFrame) -> dict[tuple[str, str], str]:
    if triggers.empty:
        return {}
    return {(r["OWNER"], r["TRIGGER_NAME"]): r["TABLE_NAME"] for _, r in triggers.iterrows()}


def run(paths: ProjectPaths, config: Config, logger: logging.Logger) -> dict:
    logger.info("S2 시작")
    s1_out = paths.stage_output("s1_inventory")
    inventory = read_csv(s1_out / "s1_inventory.csv")
    sources = read_csv(paths.input_dir / "in_source.csv")
    arguments = read_csv(paths.input_dir / "in_arguments.csv")
    triggers = read_csv(paths.input_dir / "in_triggers.csv")

    if inventory.empty:
        raise RuntimeError("s1_inventory.csv missing/empty — run stage s1 first")

    if config.parser.primary not in ("regex", "antlr4-plsql"):
        logger.warning("알 수 없는 parser.primary=%s — regex 로 처리", config.parser.primary)
    if config.parser.primary == "antlr4-plsql":
        logger.warning("antlr4-plsql 파서는 아직 미도입 상태 — REGEX/PARTIAL 티어로 폴백")

    bodies = _build_sp_bodies(inventory, sources)
    ref_out_counts, out_param_counts = _arg_counts(arguments)
    trigger_tables = _trigger_tables(triggers)

    arguments_with_spid = arguments.copy()
    if not arguments_with_spid.empty:
        arguments_with_spid["SP_ID"] = arguments_with_spid.apply(
            lambda a: _make_sp_id(a["OWNER"], _pkg_or_none(a.get("PACKAGE_NAME")), a["OBJECT_NAME"], a.get("OVERLOAD")),
            axis=1,
        )

    metric_rows: list[dict] = []
    sql_inv_rows: list[dict] = []
    refcursor_rows: list[dict] = []
    dynsql_hint_rows: list[dict] = []
    parse_failure_rows: list[dict] = []

    for _, inv_row in inventory.iterrows():
        sp_id = inv_row["SP_ID"]
        wrapped = str(inv_row.get("WRAPPED", "N")) == "Y"

        if wrapped:
            metric_rows.append({c: (0 if c not in ("SP_ID", "PARSE_MODE", "PARSE_CONFIDENCE") else None)
                                for c in METRIC_COLUMNS} | {
                "SP_ID": sp_id, "PARSE_MODE": "SKIPPED", "PARSE_CONFIDENCE": PARSE_CONFIDENCE["SKIPPED"],
            })
            parse_failure_rows.append({"SP_ID": sp_id, "REASON": "WRAPPED", "DETAIL": "encrypted source excluded"})
            continue

        lines = bodies.get(sp_id)
        if not lines:
            metric_rows.append({c: (0 if c not in ("SP_ID", "PARSE_MODE", "PARSE_CONFIDENCE") else None)
                                for c in METRIC_COLUMNS} | {
                "SP_ID": sp_id, "PARSE_MODE": "FAILED", "PARSE_CONFIDENCE": PARSE_CONFIDENCE["FAILED"],
            })
            parse_failure_rows.append({"SP_ID": sp_id, "REASON": "NO_SOURCE_FOUND", "DETAIL": ""})
            continue

        loc = _compute_loc(lines)
        joined = "\n".join(t for _, t in lines)
        text_nocomment = _strip_comments(joined)
        text_clean = _mask_strings(text_nocomment)

        begin_ok = bool(re.search(r"\bBEGIN\b", text_clean, re.IGNORECASE))
        end_ok = bool(re.search(r"END\s*[A-Za-z0-9_$#]*\s*;\s*$", text_nocomment.strip(), re.IGNORECASE))
        parse_mode = "REGEX" if (begin_ok and end_ok) else "PARTIAL"
        if parse_mode == "PARTIAL":
            parse_failure_rows.append({"SP_ID": sp_id, "REASON": "UNBALANCED_BLOCK",
                                       "DETAIL": "BEGIN/END 불일치 추정 (regex 휴리스틱)"})

        branch_count = sum(len(KEYWORD_RE[kw].findall(text_clean)) for kw in BRANCH_KEYWORDS)

        dynsql = _find_dynamic_sql(text_nocomment)
        dyn_literal = sum(1 for d in dynsql if d["kind"] == "LITERAL")
        dyn_variable = sum(1 for d in dynsql if d["kind"] == "VARIABLE")

        line_offsets = _build_line_offsets(lines)
        for d in dynsql:
            ln = _line_for_pos(text_nocomment, d["pos"], line_offsets)
            if d["kind"] == "VARIABLE":
                variables = _extract_dynsql_variables(d["expr"])
                dynsql_hint_rows.append({
                    "SP_ID": sp_id, "LINE": ln, "VARIABLES": ";".join(variables),
                    "SNIPPET": d["expr"][:200], "RESOLVED_TABLES": "", "REASON": "",
                })
                sql_inv_rows.append({"SP_ID": sp_id, "STMT_SEQ": 0, "DML_TYPE": "UNKNOWN",
                                     "TABLES_RAW": "", "IS_DYNAMIC": "Y", "DYNAMIC_KIND": "VARIABLE"})
            else:
                literal_sql = STRING_LITERAL_RE.sub(
                    lambda m: m.group(0)[1:-1].replace("''", "'"), d["expr"]
                )
                for dml_type, stmt in _sql_statements_static(literal_sql) or [(None, literal_sql)]:
                    if dml_type is None:
                        continue
                    tables = _extract_tables(stmt, dml_type)
                    sql_inv_rows.append({"SP_ID": sp_id, "STMT_SEQ": 0, "DML_TYPE": dml_type,
                                         "TABLES_RAW": ";".join(tables), "IS_DYNAMIC": "Y",
                                         "DYNAMIC_KIND": "LITERAL"})

        for dml_type, stmt in _sql_statements_static(text_clean):
            tables = _extract_tables(stmt, dml_type)
            sql_inv_rows.append({"SP_ID": sp_id, "STMT_SEQ": 0, "DML_TYPE": dml_type,
                                 "TABLES_RAW": ";".join(tables), "IS_DYNAMIC": "N", "DYNAMIC_KIND": ""})

        tx_control_count = len(TX_CONTROL_RE.findall(text_clean))
        autonomous_tx = 1 if AUTONOMOUS_TX_RE.search(text_clean) else 0

        connect_by = len(CONNECT_BY_RE.findall(text_clean))
        merge_cnt = len(MERGE_RE.findall(text_clean))
        analytic_over = len(ANALYTIC_OVER_RE.findall(text_clean))
        bulk_collect = len(BULK_COLLECT_RE.findall(text_clean))
        forall_cnt = len(FORALL_RE.findall(text_clean))
        utl_cnt = len(UTL_PKG_RE.findall(text_clean))
        dbms_cnt = len(DBMS_PKG_RE.findall(text_clean))
        oracle_feature_count = (connect_by + merge_cnt + analytic_over + bulk_collect +
                                forall_cnt + utl_cnt + dbms_cnt)

        dblink_direct = len(DBLINK_REF_RE.findall(text_nocomment))
        exception_count = len(EXCEPTION_RE.findall(text_clean))
        when_others = 1 if WHEN_OTHERS_RE.search(text_clean) else 0
        goto_count = len(GOTO_RE.findall(text_clean))

        refcursor_count = len(REFCURSOR_DECL_RE.findall(text_clean))
        ref_cursor_out_count = ref_out_counts.get(sp_id, 0)
        out_param_count = out_param_counts.get(sp_id, 0)

        global_var_ref_count = 0
        if clean_str(inv_row.get("PKG")):
            pkg_src = sources[(sources["OWNER"] == inv_row["OWNER"]) & (sources["NAME"] == inv_row["PKG"]) &
                              (sources["TYPE"] == "PACKAGE BODY")]
            if not pkg_src.empty:
                first_line = lines[0][0]
                gvars = _package_global_vars(pkg_src, first_line)
                for v in gvars:
                    global_var_ref_count += len(re.findall(rf"\b{re.escape(v)}\b", text_clean, re.IGNORECASE))

        mutating_risk = 0
        if inv_row["TYPE"] == "TRIGGER":
            table = trigger_tables.get((inv_row["OWNER"], inv_row["NAME"]))
            if table:
                body_from_line2 = "\n".join(t for ln, t in lines if ln > lines[0][0])
                if re.search(rf"\b(FROM|UPDATE|INTO)\s+{re.escape(table)}\b", body_from_line2, re.IGNORECASE):
                    mutating_risk = 1

        # in_arguments 로 OUT REF CURSOR 를 못 잡은 경우, 소스에서 감지한 SYS_REFCURSOR 로 보강
        if ref_cursor_out_count == 0 and refcursor_count > 0 and out_param_count > 0:
            ref_cursor_out_count = min(refcursor_count, out_param_count)

        metric_rows.append({
            "SP_ID": sp_id, "LOC": loc, "BRANCH_COUNT": branch_count,
            "CURSOR_EXPLICIT_COUNT": len(CURSOR_EXPLICIT_RE.findall(text_clean)),
            "CURSOR_FOR_LOOP_COUNT": len(CURSOR_FOR_LOOP_RE.findall(text_clean)),
            "REFCURSOR_COUNT": refcursor_count, "REF_CURSOR_OUT_COUNT": ref_cursor_out_count,
            "DYNAMIC_SQL_LITERAL_COUNT": dyn_literal, "DYNAMIC_SQL_VARIABLE_COUNT": dyn_variable,
            "TX_CONTROL_COUNT": tx_control_count, "AUTONOMOUS_TX_FLAG": autonomous_tx,
            "CONNECT_BY_COUNT": connect_by, "MERGE_COUNT": merge_cnt, "ANALYTIC_OVER_COUNT": analytic_over,
            "BULK_COLLECT_COUNT": bulk_collect, "FORALL_COUNT": forall_cnt,
            "UTL_PKG_COUNT": utl_cnt, "DBMS_PKG_COUNT": dbms_cnt, "ORACLE_FEATURE_COUNT": oracle_feature_count,
            "DBLINK_DIRECT_COUNT": dblink_direct,
            "EXCEPTION_HANDLER_COUNT": exception_count, "WHEN_OTHERS_FLAG": when_others, "GOTO_COUNT": goto_count,
            "GLOBAL_PKG_VAR_REF_COUNT": global_var_ref_count, "OUT_PARAM_COUNT": out_param_count,
            "MUTATING_TRIGGER_RISK": mutating_risk,
            "PARSE_MODE": parse_mode, "PARSE_CONFIDENCE": PARSE_CONFIDENCE[parse_mode],
        })

        for i, row in enumerate([r for r in sql_inv_rows if r["SP_ID"] == sp_id], start=1):
            row["STMT_SEQ"] = i

        if ref_cursor_out_count > 0 and not arguments_with_spid.empty:
            arg_hits = arguments_with_spid[
                (arguments_with_spid["SP_ID"] == sp_id)
                & (arguments_with_spid["PLS_TYPE"].astype(str).str.upper().str.contains("CURSOR"))
            ]
            for _, arg in arg_hits.iterrows():
                refcursor_rows.append({
                    "SP_ID": sp_id, "ARG_POSITION": arg["POSITION"], "RETURN_COLUMNS": "",
                    "IS_ADHOC": "Y",
                })

    metrics_df = pd.DataFrame(metric_rows, columns=METRIC_COLUMNS)
    sql_inv_df = pd.DataFrame(sql_inv_rows, columns=["SP_ID", "STMT_SEQ", "DML_TYPE", "TABLES_RAW",
                                                     "IS_DYNAMIC", "DYNAMIC_KIND"])
    refcursor_df = pd.DataFrame(refcursor_rows, columns=["SP_ID", "ARG_POSITION", "RETURN_COLUMNS", "IS_ADHOC"])
    dynsql_hints_df = pd.DataFrame(dynsql_hint_rows, columns=["SP_ID", "LINE", "VARIABLES", "SNIPPET",
                                                              "RESOLVED_TABLES", "REASON"])
    parse_failures_df = pd.DataFrame(parse_failure_rows, columns=["SP_ID", "REASON", "DETAIL"])

    metrics_df = _apply_metric_overrides(metrics_df, paths.override_dir, logger)
    dynsql_hints_df = _apply_dynsql_resolve(dynsql_hints_df, paths.override_dir, logger)

    total = len(metrics_df)
    success = int(metrics_df["PARSE_MODE"].isin(["AST", "REGEX"]).sum()) if total else 0
    success_rate = success / total if total else 0.0
    bench_df = metrics_df["PARSE_MODE"].value_counts().rename_axis("PARSE_MODE").reset_index(name="COUNT")
    bench_df["SUCCESS_RATE"] = success_rate

    if success_rate < config.parser.target_success_rate:
        logger.warning("파서 성공률 %.1f%% < 목표 %.1f%% (parser.target_success_rate)",
                       success_rate * 100, config.parser.target_success_rate * 100)

    out = paths.stage_output("s2_metrics")
    write_csv(metrics_df, out / "s2_metrics.csv")
    write_csv(sql_inv_df, out / "s2_sql_inventory.csv")
    write_csv(refcursor_df, out / "s2_refcursor_profiles.csv")
    write_csv(dynsql_hints_df, out / "s2_dynsql_hints.csv")
    write_csv(parse_failures_df, out / "s2_parse_failures.csv")
    write_csv(bench_df, out / "s2_parser_bench.csv")

    logger.info("S2 완료 — metrics=%d sql_inventory=%d dynsql_hints=%d parse_failures=%d success_rate=%.1f%%",
               len(metrics_df), len(sql_inv_df), len(dynsql_hints_df), len(parse_failures_df), success_rate * 100)
    if not dynsql_hints_df.empty:
        logger.warning("검토 필수: s2_dynsql_hints.csv (%d rows) → override/s2_dynsql_resolve.csv",
                       len(dynsql_hints_df))
    if not parse_failures_df.empty:
        logger.warning("검토 필수: s2_parse_failures.csv (%d rows)", len(parse_failures_df))

    return {"metrics": len(metrics_df), "parse_failures": len(parse_failures_df), "success_rate": success_rate}


def _apply_metric_overrides(metrics_df: pd.DataFrame, override_dir: Path, logger: logging.Logger) -> pd.DataFrame:
    p = override_dir / OVERRIDE_SCHEMAS["s2_metrics"].filename
    if not p.exists():
        return metrics_df
    ov = read_csv(p)
    if ov.empty:
        return metrics_df
    metrics_df = metrics_df.set_index("SP_ID")
    for _, row in ov.iterrows():
        sp_id, metric, value = row["SP_ID"], row["METRIC_NAME"], row["VALUE"]
        if sp_id not in metrics_df.index or metric not in metrics_df.columns:
            logger.warning("s2_metrics_override: SP_ID/METRIC_NAME 불일치 (%s, %s)", sp_id, metric)
            continue
        try:
            dtype = metrics_df[metric].dtype
            metrics_df.loc[sp_id, metric] = pd.Series([value]).astype(dtype).iloc[0]
        except (ValueError, TypeError):
            metrics_df.loc[sp_id, metric] = value
        logger.info("override 적용: %s.%s = %s (%s)", sp_id, metric, value, row.get("REASON", ""))
    return metrics_df.reset_index()


def _apply_dynsql_resolve(hints_df: pd.DataFrame, override_dir: Path, logger: logging.Logger) -> pd.DataFrame:
    p = override_dir / OVERRIDE_SCHEMAS["s2_dynsql_resolve"].filename
    if not p.exists() or hints_df.empty:
        return hints_df
    ov = read_csv(p)
    if ov.empty:
        return hints_df
    resolve_map = {r["SRC_SP_ID"]: (r["RESOLVED_TABLES"], r.get("REASON", "")) for _, r in ov.iterrows()}
    for idx, row in hints_df.iterrows():
        if row["SP_ID"] in resolve_map:
            tables, reason = resolve_map[row["SP_ID"]]
            hints_df.at[idx, "RESOLVED_TABLES"] = tables
            hints_df.at[idx, "REASON"] = reason
    return hints_df
