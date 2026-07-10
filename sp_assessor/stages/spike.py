"""§0 선행 스파이크 — SP-1/SP-2/SP-3 임계 검증.

운영 시나리오(§10)상 최초 실행 커맨드이므로 S1/S2 산출물이 없으면 그 자리에서
계산해 `output/_spike/spike_report.md` 를 만든다 (별도 표본 추출 인프라는 없어
현재 입력 전체를 대상으로 계산 — 실제 100건 표본 대신 "가용한 전체" 사용).
"""
from __future__ import annotations

import logging

import pandas as pd

from sp_assessor.core.config import Config
from sp_assessor.core.paths import ProjectPaths
from sp_assessor.io.csv_io import read_csv
from sp_assessor.stages import s1_inventory, s2_metrics
from sp_assessor.stages.s3_graph import _base_name_lookup
from sp_assessor.util.text import clean_str

THRESHOLDS = {
    "SP-1": {"label": "파서 성공률 (AST/REGEX 완주)", "threshold": 0.90,
            "fallback": "§S2 파서 폴백 비율 상향, PARSE_MODE 세분화 (AST/PARTIAL/REGEX)"},
    "SP-2": {"label": "EXECUTE IMMEDIATE 리터럴 조립 비율", "threshold": 0.60,
            "fallback": "§S2-5 후보 추출 대신 override 강제 전환"},
    "SP-3": {"label": ".NET grep 결과의 SP_NAME_RAW 매칭률", "threshold": 0.70,
            "fallback": "§S2-8 Roslyn 정적 분석기 필수 채택"},
}


def _ensure_s1_s2(paths: ProjectPaths, config: Config, logger: logging.Logger) -> tuple[pd.DataFrame, pd.DataFrame]:
    s1_csv = paths.stage_output("s1_inventory") / "s1_inventory.csv"
    if not s1_csv.exists():
        s1_inventory.run(paths, config, logger)
    inventory = read_csv(s1_csv)

    s2_csv = paths.stage_output("s2_metrics") / "s2_metrics.csv"
    if not s2_csv.exists() and not inventory.empty:
        s2_metrics.run(paths, config, logger)
    metrics = read_csv(s2_csv)
    return inventory, metrics


def _sp1_parser_success_rate(metrics: pd.DataFrame) -> tuple[float | None, str]:
    if metrics.empty:
        return None, "s2_metrics 없음 (in_source.csv 미제공?)"
    total = len(metrics)
    success = int(metrics["PARSE_MODE"].isin(["AST", "REGEX"]).sum())
    note = ("ANTLR PL/SQL 파서 미도입 (v0.1 은 config.parser.primary=regex 고정) — "
           "본 수치는 실제 AST 성공률이 아니라 regex 폴백 완주 비율의 대리 관측치")
    return success / total, note


def _sp2_dynamic_sql_literal_ratio(metrics: pd.DataFrame) -> tuple[float | None, str]:
    if metrics.empty:
        return None, "s2_metrics 없음"
    literal = int(metrics["DYNAMIC_SQL_LITERAL_COUNT"].sum())
    variable = int(metrics["DYNAMIC_SQL_VARIABLE_COUNT"].sum())
    total = literal + variable
    if total == 0:
        return None, "EXECUTE IMMEDIATE 사용 SP 없음"
    return literal / total, f"literal={literal}, variable={variable}"


def _sp3_app_call_match_rate(paths: ProjectPaths, inventory: pd.DataFrame) -> tuple[float | None, str]:
    app_calls = read_csv(paths.input_dir / "in_app_calls.csv")
    if app_calls.empty or inventory.empty:
        return None, "in_app_calls.csv 또는 s1_inventory 없음"
    lookup = _base_name_lookup(inventory)
    total = len(app_calls)
    matched = 0
    for _, row in app_calls.iterrows():
        name = clean_str(row.get("SP_NAME_RESOLVED")) or clean_str(row.get("SP_NAME_RAW"))
        if lookup.get(name.upper()):
            matched += 1
    return matched / total, f"matched={matched}/{total}"


def run(paths: ProjectPaths, config: Config, logger: logging.Logger) -> dict:
    logger.info("스파이크 시작 (§0)")
    inventory, metrics = _ensure_s1_s2(paths, config, logger)

    sp1_rate, sp1_note = _sp1_parser_success_rate(metrics)
    sp2_rate, sp2_note = _sp2_dynamic_sql_literal_ratio(metrics)
    sp3_rate, sp3_note = _sp3_app_call_match_rate(paths, inventory)

    results = {
        "SP-1": (sp1_rate, sp1_note),
        "SP-2": (sp2_rate, sp2_note),
        "SP-3": (sp3_rate, sp3_note),
    }

    lines = ["# 선행 스파이크 결과 (§0)", ""]
    lines.append("| # | 항목 | 측정값 | 임계값 | 판정 | 비고 |")
    lines.append("|---|---|---|---|---|---|")
    for key, meta in THRESHOLDS.items():
        rate, note = results[key]
        if rate is None:
            verdict = "N/A"
            rate_str = "N/A"
        else:
            verdict = "PASS" if rate >= meta["threshold"] else "FAIL"
            rate_str = f"{rate * 100:.1f}%"
        lines.append(f"| {key} | {meta['label']} | {rate_str} | ≥{meta['threshold'] * 100:.0f}% | "
                     f"{verdict} | {note} |")
    lines.append("")

    lines.append("## 임계 미달 시 대응")
    lines.append("")
    below_threshold = [key for key, meta in THRESHOLDS.items()
                       if results[key][0] is not None and results[key][0] < meta["threshold"]]
    for key in below_threshold:
        lines.append(f"- **{key}**: {THRESHOLDS[key]['fallback']}")
    if not below_threshold:
        lines.append("_모든 항목 임계 충족 또는 측정 불가(N/A)_")

    out_dir = paths.output_dir / "_spike"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "spike_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    logger.info("스파이크 완료 — SP-1=%s SP-2=%s SP-3=%s",
               f"{sp1_rate:.1%}" if sp1_rate is not None else "N/A",
               f"{sp2_rate:.1%}" if sp2_rate is not None else "N/A",
               f"{sp3_rate:.1%}" if sp3_rate is not None else "N/A")

    return {"sp1": sp1_rate, "sp2": sp2_rate, "sp3": sp3_rate}
