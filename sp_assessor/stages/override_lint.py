"""override lint 커맨드 — override 파일 정합성/충돌 검사 (§6)."""
from __future__ import annotations

from sp_assessor.core.paths import ProjectPaths
from sp_assessor.io.csv_io import read_csv
from sp_assessor.io.csv_schemas import OVERRIDE_SCHEMAS
from sp_assessor.stages.validate import ValidationReport


def _check_reason_blank(df, filename: str, report: ValidationReport) -> None:
    if "REASON" not in df.columns:
        return
    missing = df["REASON"].isna() | (df["REASON"].astype(str).str.strip() == "")
    if missing.any():
        report.add("ERROR", "REASON_BLANK", f"{filename}: {int(missing.sum())} rows missing REASON")


def _check_conflicting(df, filename: str, key_cols: list[str], value_col: str,
                       report: ValidationReport, code: str = "CONFLICTING_VALUE") -> None:
    if df.empty or value_col not in df.columns:
        return
    for key, grp in df.groupby(key_cols):
        values = grp[value_col].astype(str).unique()
        if len(values) > 1:
            key_str = key if isinstance(key, str) else "/".join(str(k) for k in key)
            report.add("ERROR", code, f"{filename}: {key_str} 에 상충하는 {value_col} 값 {list(values)}")


def _check_duplicate_rows(df, filename: str, subset: list[str], report: ValidationReport) -> None:
    if df.empty:
        return
    dup = df[df.duplicated(subset=subset, keep=False)]
    if not dup.empty:
        report.add("WARN", "DUPLICATE_ROW",
                   f"{filename}: {len(dup)} rows 중복 ({subset} 기준)")


def lint(paths: ProjectPaths) -> ValidationReport:
    report = ValidationReport()

    s1 = read_csv(paths.override_dir / OVERRIDE_SCHEMAS["s1_inventory"].filename)
    _check_reason_blank(s1, "s1_inventory_override.csv", report)
    if not s1.empty and "ACTION" in s1.columns:
        for sp_id, grp in s1.groupby("SP_ID"):
            actions = set(grp["ACTION"].astype(str).str.upper())
            if {"EXCLUDE", "INCLUDE"} <= actions:
                report.add("ERROR", "CONFLICTING_ACTION",
                          f"s1_inventory_override.csv: {sp_id} 에 EXCLUDE 와 INCLUDE 동시 지정")
        _check_duplicate_rows(s1, "s1_inventory_override.csv", ["SP_ID", "ACTION"], report)

    s2m = read_csv(paths.override_dir / OVERRIDE_SCHEMAS["s2_metrics"].filename)
    _check_reason_blank(s2m, "s2_metrics_override.csv", report)
    _check_conflicting(s2m, "s2_metrics_override.csv", ["SP_ID", "METRIC_NAME"], "VALUE", report)

    s2d = read_csv(paths.override_dir / OVERRIDE_SCHEMAS["s2_dynsql_resolve"].filename)
    _check_reason_blank(s2d, "s2_dynsql_resolve.csv", report)
    _check_conflicting(s2d, "s2_dynsql_resolve.csv", ["SRC_SP_ID"], "RESOLVED_TABLES", report,
                      code="CONFLICTING_RESOLUTION")

    s3e = read_csv(paths.override_dir / OVERRIDE_SCHEMAS["s3_edges"].filename)
    _check_reason_blank(s3e, "s3_edges_override.csv", report)
    if not s3e.empty and "ACTION" in s3e.columns:
        for (src, dst), grp in s3e.groupby(["SRC", "DST"]):
            actions = set(grp["ACTION"].astype(str).str.upper())
            if {"ADD", "REMOVE"} <= actions:
                report.add("ERROR", "CONFLICTING_ACTION",
                          f"s3_edges_override.csv: {src}->{dst} 에 ADD 와 REMOVE 동시 지정")

    s3c = read_csv(paths.override_dir / OVERRIDE_SCHEMAS["s3_cluster"].filename)
    _check_reason_blank(s3c, "s3_cluster_override.csv", report)
    _check_conflicting(s3c, "s3_cluster_override.csv", ["SP_ID"], "CLUSTER_ID", report,
                      code="CONFLICTING_CLUSTER")

    s4 = read_csv(paths.override_dir / OVERRIDE_SCHEMAS["s4_strategy"].filename)
    _check_reason_blank(s4, "s4_strategy_override.csv", report)
    _check_conflicting(s4, "s4_strategy_override.csv", ["SP_ID"], "STRATEGY", report,
                      code="CONFLICTING_STRATEGY")

    pilot = read_csv(paths.override_dir / OVERRIDE_SCHEMAS["pilot_effort"].filename)
    _check_conflicting(pilot, "pilot_effort.csv", ["SP_ID"], "ACTUAL_MD", report,
                      code="CONFLICTING_PILOT_SAMPLE")

    return report
