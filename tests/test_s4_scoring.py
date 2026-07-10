"""S4 스모크/단위 테스트 — examples 데이터로 end-to-end 실행."""
from __future__ import annotations

from pathlib import Path
import logging
import shutil

from sp_assessor.core.config import load_config
from sp_assessor.core.paths import ProjectPaths
from sp_assessor.stages import s1_inventory, s2_metrics, s3_graph, s4_scoring
from sp_assessor.io.csv_io import read_csv


EXAMPLES = Path(__file__).parent.parent / "examples"


def _run_s1_to_s4(tmp_path: Path, extra_override_files: dict[str, str] | None = None,
                  config_overrides: dict | None = None):
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
    if config_overrides:
        for key, value in config_overrides.items():
            obj = cfg
            parts = key.split(".")
            for p in parts[:-1]:
                obj = getattr(obj, p)
            setattr(obj, parts[-1], value)

    logger = logging.getLogger("test")
    s1_inventory.run(paths, cfg, logger)
    s2_metrics.run(paths, cfg, logger)
    s3_graph.run(paths, cfg, logger)
    s4_scoring.run(paths, cfg, logger)
    return paths


def _score_row(paths: ProjectPaths, sp_id: str):
    df = read_csv(paths.stage_output("s4_scoring") / "s4_scores.csv")
    hit = df[df["SP_ID"] == sp_id]
    assert not hit.empty, f"{sp_id} missing from s4_scores.csv"
    return hit.iloc[0]


def test_autonomous_tx_forces_defer(tmp_path: Path) -> None:
    paths = _run_s1_to_s4(tmp_path)
    row = _score_row(paths, "APP_OWNER.COMPLEX_PROC")
    assert row["STRATEGY"] == "DEFER"


def test_simple_sp_classified_auto_simple(tmp_path: Path) -> None:
    paths = _run_s1_to_s4(tmp_path)
    row = _score_row(paths, "APP_OWNER.CUSTOMER_PKG.ADD_CUSTOMER#0")
    assert row["STRATEGY"] == "AUTO_SIMPLE"


def test_dblink_exposure_excludes_auto_tier(tmp_path: Path) -> None:
    """DB Link 를 직접 참조하는 SP 는 auto_simple/auto_assisted 규칙(db_link==0)을 통과할 수 없다."""
    paths = _run_s1_to_s4(tmp_path)
    row = _score_row(paths, "BATCH_OWNER.DAILY_JOB")
    assert row["STRATEGY"] not in ("AUTO_SIMPLE", "AUTO_ASSISTED")


def test_scc_members_promoted_to_same_strategy(tmp_path: Path) -> None:
    paths = _run_s1_to_s4(tmp_path)
    get_order = _score_row(paths, "APP_OWNER.GET_ORDER")
    calc_tax = _score_row(paths, "APP_OWNER.CALC_TAX")
    assert get_order["STRATEGY"] == calc_tax["STRATEGY"]
    assert get_order["WAVE_NO"] == calc_tax["WAVE_NO"]


def test_boundary_sp_flagged_in_review(tmp_path: Path) -> None:
    paths = _run_s1_to_s4(tmp_path)
    review = read_csv(paths.stage_output("s4_scoring") / "s4_review.csv")
    assert "BOUNDARY" in set(review["REASON_CODE"])
    assert "DEFER_AUTO_CLASSIFIED" in set(review["REASON_CODE"])
    assert "PARSE_MODE_NOT_AST" in set(review["REASON_CODE"])


def test_wrapped_sp_gets_manual_or_higher_not_auto(tmp_path: Path) -> None:
    """LOC=0(스킵됨)인 WRAPPED SP 는 지표 부재로 auto 로 오분류되면 안전하지 않음 — 최소 검토 대상."""
    paths = _run_s1_to_s4(tmp_path)
    review = read_csv(paths.stage_output("s4_scoring") / "s4_review.csv")
    hit = review[(review["SP_ID"] == "APP_OWNER.LEGACY_WRAPPED_PROC") & (review["REASON_CODE"] == "PARSE_MODE_NOT_AST")]
    assert not hit.empty
    assert hit.iloc[0]["DETAIL"] == "SKIPPED"


def test_strategy_override_applied(tmp_path: Path) -> None:
    paths = _run_s1_to_s4(tmp_path, extra_override_files={
        "s4_strategy_override.csv":
            "SP_ID,STRATEGY,REASON\n"
            "APP_OWNER.CALC_TAX,MANUAL,domain expert insists on manual review\n",
    })
    row = _score_row(paths, "APP_OWNER.CALC_TAX")
    assert row["STRATEGY"] == "MANUAL"


def test_effort_columns_present_and_ordered(tmp_path: Path) -> None:
    paths = _run_s1_to_s4(tmp_path)
    row = _score_row(paths, "APP_OWNER.GET_ORDER")
    assert row["EFFORT_P90"] >= row["EFFORT_P50"] >= 0
    assert row["CONFIDENCE"] == "LOW"


def test_calibration_runs_with_sufficient_pilot_samples(tmp_path: Path) -> None:
    pilot_csv = (
        "SP_ID,ACTUAL_MD\n"
        "APP_OWNER.CALC_TAX,1.0\n"
        "APP_OWNER.COMPLEX_PROC,12.0\n"
        "APP_OWNER.CUSTOMER_PKG.ADD_CUSTOMER#0,0.8\n"
        "APP_OWNER.CUSTOMER_PKG.GET_NAME#0,0.9\n"
        "APP_OWNER.DIRECT_LINK_CALL,2.0\n"
        "APP_OWNER.GET_ORDER,2.5\n"
        "APP_OWNER.LEGACY_WRAPPED_PROC,0.5\n"
        "APP_OWNER.ORDER_TRG,1.2\n"
        "APP_OWNER.SYNC_HR,3.0\n"
        "BATCH_OWNER.DAILY_JOB,3.5\n"
    )
    paths = _run_s1_to_s4(tmp_path, extra_override_files={"pilot_effort.csv": pilot_csv},
                          config_overrides={"calibration.min_samples": 8})
    row = _score_row(paths, "APP_OWNER.GET_ORDER")
    assert row["CONFIDENCE"] == "CALIBRATED"

    weight_yaml = paths.stage_output("s4_scoring") / "s4_weight_suggestion.yaml"
    assert weight_yaml.exists()
    content = weight_yaml.read_text(encoding="utf-8")
    assert "vif" in content
    assert "weight_suggestion" in content


def test_calibration_skipped_below_min_samples(tmp_path: Path) -> None:
    pilot_csv = "SP_ID,ACTUAL_MD\nAPP_OWNER.CALC_TAX,1.0\nAPP_OWNER.GET_ORDER,2.0\n"
    paths = _run_s1_to_s4(tmp_path, extra_override_files={"pilot_effort.csv": pilot_csv})
    row = _score_row(paths, "APP_OWNER.GET_ORDER")
    assert row["CONFIDENCE"] == "LOW"
    assert not (paths.stage_output("s4_scoring") / "s4_weight_suggestion.yaml").exists()
