"""S2 스모크/단위 테스트 — examples 데이터로 end-to-end 실행."""
from __future__ import annotations

from pathlib import Path
import logging
import shutil

from sp_assessor.core.config import load_config
from sp_assessor.core.paths import ProjectPaths
from sp_assessor.stages import s1_inventory, s2_metrics
from sp_assessor.io.csv_io import read_csv


EXAMPLES = Path(__file__).parent.parent / "examples"


def _run_s1_s2(tmp_path: Path, extra_override_files: dict[str, str] | None = None):
    for sub in ("input", "override"):
        src = EXAMPLES / sub
        if src.exists():
            shutil.copytree(src, tmp_path / sub)
    shutil.copy(EXAMPLES / "config.yaml", tmp_path / "config.yaml")

    if extra_override_files:
        (tmp_path / "override").mkdir(parents=True, exist_ok=True)
        for name, content in extra_override_files.items():
            (tmp_path / "override" / name).write_text(content, encoding="utf-8")

    paths = ProjectPaths.from_root(tmp_path)
    paths.ensure()
    cfg = load_config(paths.config)
    logger = logging.getLogger("test")
    s1_inventory.run(paths, cfg, logger)
    s2_metrics.run(paths, cfg, logger)
    return paths


def _metric_row(paths: ProjectPaths, sp_id: str):
    df = read_csv(paths.stage_output("s2_metrics") / "s2_metrics.csv")
    hit = df[df["SP_ID"] == sp_id]
    assert not hit.empty, f"{sp_id} missing from s2_metrics.csv"
    return hit.iloc[0]


def test_get_order_refcursor_and_branch(tmp_path: Path) -> None:
    paths = _run_s1_s2(tmp_path)
    row = _metric_row(paths, "APP_OWNER.GET_ORDER")
    assert int(row["REFCURSOR_COUNT"]) == 1
    assert int(row["REF_CURSOR_OUT_COUNT"]) == 1
    assert int(row["OUT_PARAM_COUNT"]) == 1
    assert row["PARSE_MODE"] == "REGEX"

    sql_inv = read_csv(paths.stage_output("s2_metrics") / "s2_sql_inventory.csv")
    hit = sql_inv[sql_inv["SP_ID"] == "APP_OWNER.GET_ORDER"]
    assert set(hit["DML_TYPE"]) == {"SELECT"}
    assert "ORDERS" in set(hit["TABLES_RAW"])

    refcursor = read_csv(paths.stage_output("s2_metrics") / "s2_refcursor_profiles.csv")
    assert "APP_OWNER.GET_ORDER" in set(refcursor["SP_ID"])


def test_daily_job_dynamic_sql_literal(tmp_path: Path) -> None:
    paths = _run_s1_s2(tmp_path)
    row = _metric_row(paths, "BATCH_OWNER.DAILY_JOB")
    assert int(row["DYNAMIC_SQL_LITERAL_COUNT"]) == 1
    assert int(row["DYNAMIC_SQL_VARIABLE_COUNT"]) == 0

    sql_inv = read_csv(paths.stage_output("s2_metrics") / "s2_sql_inventory.csv")
    hit = sql_inv[sql_inv["SP_ID"] == "BATCH_OWNER.DAILY_JOB"]
    assert set(hit["DML_TYPE"]) == {"TRUNCATE"}
    assert set(hit["IS_DYNAMIC"]) == {"Y"}
    assert set(hit["DYNAMIC_KIND"]) == {"LITERAL"}


def test_complex_proc_metrics(tmp_path: Path) -> None:
    paths = _run_s1_s2(tmp_path)
    row = _metric_row(paths, "APP_OWNER.COMPLEX_PROC")
    assert int(row["BRANCH_COUNT"]) > 0
    assert int(row["CURSOR_EXPLICIT_COUNT"]) == 1
    assert int(row["CURSOR_FOR_LOOP_COUNT"]) == 1
    assert int(row["DYNAMIC_SQL_VARIABLE_COUNT"]) == 1
    assert int(row["DYNAMIC_SQL_LITERAL_COUNT"]) == 0
    assert int(row["TX_CONTROL_COUNT"]) == 2  # COMMIT + ROLLBACK
    assert int(row["AUTONOMOUS_TX_FLAG"]) == 1
    assert int(row["MERGE_COUNT"]) == 1
    assert int(row["DBMS_PKG_COUNT"]) == 1
    assert int(row["ORACLE_FEATURE_COUNT"]) == 2
    assert int(row["EXCEPTION_HANDLER_COUNT"]) == 1
    assert int(row["WHEN_OTHERS_FLAG"]) == 1
    assert int(row["GOTO_COUNT"]) == 1

    hints = read_csv(paths.stage_output("s2_metrics") / "s2_dynsql_hints.csv")
    hit = hints[hints["SP_ID"] == "APP_OWNER.COMPLEX_PROC"]
    assert not hit.empty
    assert "v_sql" in hit.iloc[0]["VARIABLES"]


def test_dblink_direct_count_from_source(tmp_path: Path) -> None:
    paths = _run_s1_s2(tmp_path)
    row = _metric_row(paths, "APP_OWNER.DIRECT_LINK_CALL")
    assert int(row["DBLINK_DIRECT_COUNT"]) == 1


def test_mutating_trigger_risk_not_flagged_for_unrelated_table(tmp_path: Path) -> None:
    paths = _run_s1_s2(tmp_path)
    row = _metric_row(paths, "APP_OWNER.ORDER_TRG")
    assert int(row["MUTATING_TRIGGER_RISK"]) == 0


def test_wrapped_sp_skipped(tmp_path: Path) -> None:
    paths = _run_s1_s2(tmp_path)
    row = _metric_row(paths, "APP_OWNER.LEGACY_WRAPPED_PROC")
    assert row["PARSE_MODE"] == "SKIPPED"
    assert int(row["LOC"]) == 0

    failures = read_csv(paths.stage_output("s2_metrics") / "s2_parse_failures.csv")
    hit = failures[failures["SP_ID"] == "APP_OWNER.LEGACY_WRAPPED_PROC"]
    assert not hit.empty
    assert hit.iloc[0]["REASON"] == "WRAPPED"


def test_metric_override_applied(tmp_path: Path) -> None:
    paths = _run_s1_s2(tmp_path, extra_override_files={
        "s2_metrics_override.csv":
            "SP_ID,METRIC_NAME,VALUE,REASON\n"
            "APP_OWNER.CALC_TAX,BRANCH_COUNT,99,manual recount after review\n",
    })
    row = _metric_row(paths, "APP_OWNER.CALC_TAX")
    assert int(row["BRANCH_COUNT"]) == 99


def test_dynsql_resolve_override_applied(tmp_path: Path) -> None:
    paths = _run_s1_s2(tmp_path, extra_override_files={
        "s2_dynsql_resolve.csv":
            "SRC_SP_ID,RESOLVED_TABLES,REASON\n"
            "APP_OWNER.COMPLEX_PROC,CUSTOMER,confirmed via manual code review\n",
    })
    hints = read_csv(paths.stage_output("s2_metrics") / "s2_dynsql_hints.csv")
    hit = hints[hints["SP_ID"] == "APP_OWNER.COMPLEX_PROC"].iloc[0]
    assert hit["RESOLVED_TABLES"] == "CUSTOMER"
    assert hit["REASON"] == "confirmed via manual code review"


def test_parser_bench_success_rate(tmp_path: Path) -> None:
    paths = _run_s1_s2(tmp_path)
    bench = read_csv(paths.stage_output("s2_metrics") / "s2_parser_bench.csv")
    assert not bench.empty
    assert "SUCCESS_RATE" in bench.columns
