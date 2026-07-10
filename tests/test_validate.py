"""validate 커맨드 테스트 — §5 데이터 품질 규칙 전수 점검."""
from __future__ import annotations

from pathlib import Path
import logging
import shutil

from sp_assessor.core.config import load_config
from sp_assessor.core.paths import ProjectPaths
from sp_assessor.stages import s1_inventory, s2_metrics, validate as validate_stage


EXAMPLES = Path(__file__).parent.parent / "examples"


def _setup(tmp_path: Path) -> ProjectPaths:
    for sub in ("input", "override"):
        src = EXAMPLES / sub
        if src.exists():
            shutil.copytree(src, tmp_path / sub)
    shutil.copy(EXAMPLES / "config.yaml", tmp_path / "config.yaml")
    paths = ProjectPaths.from_root(tmp_path)
    paths.ensure()
    return paths


def _codes(report) -> set[str]:
    return {f.code for f in report.findings}


def test_clean_examples_have_no_errors(tmp_path: Path) -> None:
    paths = _setup(tmp_path)
    cfg = load_config(paths.config)
    report = validate_stage.validate(paths, cfg)
    assert not report.has_errors


def test_missing_required_file_is_error(tmp_path: Path) -> None:
    paths = _setup(tmp_path)
    (paths.input_dir / "in_objects.csv").unlink()
    cfg = load_config(paths.config)
    report = validate_stage.validate(paths, cfg)
    assert report.has_errors
    assert "MISSING_FILE" in _codes(report)


def test_dependency_orphan_reference_warns(tmp_path: Path) -> None:
    """예제 in_objects.csv 는 절차형 오브젝트만 담고 TABLE 은 없어 -- 의존성 참조 테이블이 orphan 으로 잡혀야 함."""
    paths = _setup(tmp_path)
    cfg = load_config(paths.config)
    report = validate_stage.validate(paths, cfg)
    assert "DEPENDENCY_REFERENCED_NOT_IN_OBJECTS" in _codes(report)


def test_dblink_without_remote_objects_file_warns(tmp_path: Path) -> None:
    paths = _setup(tmp_path)
    cfg = load_config(paths.config)
    report = validate_stage.validate(paths, cfg)
    assert "REMOTE_OBJECTS_NOT_PROVIDED" in _codes(report)


def test_dblink_remote_objects_file_present_suppresses_warning(tmp_path: Path) -> None:
    paths = _setup(tmp_path)
    (paths.input_dir / "in_remote_objects__HR_LINK.csv").write_text(
        "OWNER,OBJECT_NAME,OBJECT_TYPE,STATUS,CREATED,LAST_DDL_TIME\n"
        "REMOTE_HR,EMPLOYEE,TABLE,VALID,2024-01-01 00:00:00,2024-01-01 00:00:00\n",
        encoding="utf-8",
    )
    cfg = load_config(paths.config)
    report = validate_stage.validate(paths, cfg)
    assert "REMOTE_OBJECTS_NOT_PROVIDED" not in _codes(report)


def test_wrap_source_warns(tmp_path: Path) -> None:
    """examples 에 APP_OWNER.LEGACY_WRAPPED_PROC 가 WRAPPED 마커로 등록되어 있음."""
    paths = _setup(tmp_path)
    cfg = load_config(paths.config)
    report = validate_stage.validate(paths, cfg)
    assert "WRAP_SOURCE_PRESENT" in _codes(report)


def test_name_collision_across_schemas_warns(tmp_path: Path) -> None:
    paths = _setup(tmp_path)
    objects_csv = paths.input_dir / "in_objects.csv"
    content = objects_csv.read_text(encoding="utf-8")
    content += "BATCH_OWNER,CALC_TAX,PROCEDURE,VALID,2024-01-01 00:00:00,2024-01-01 00:00:00\n"
    objects_csv.write_text(content, encoding="utf-8")
    cfg = load_config(paths.config)
    report = validate_stage.validate(paths, cfg)
    assert "NAME_COLLISION" in _codes(report)


def test_large_field_inlined_flags_error(tmp_path: Path) -> None:
    paths = _setup(tmp_path)
    triggers_csv = paths.input_dir / "in_triggers.csv"
    long_body = "BEGIN " + ("X" * 600) + " END;"
    triggers_csv.write_text(
        "OWNER,TRIGGER_NAME,TABLE_OWNER,TABLE_NAME,STATUS,BODY_FILE\n"
        f'APP_OWNER,ORDER_TRG,APP_OWNER,ORDERS,ENABLED,"{long_body}"\n',
        encoding="utf-8",
    )
    cfg = load_config(paths.config)
    report = validate_stage.validate(paths, cfg)
    assert "LARGE_FIELD_INLINED" in _codes(report)


def test_override_sp_id_unknown_is_error(tmp_path: Path) -> None:
    paths = _setup(tmp_path)
    cfg = load_config(paths.config)
    logger = logging.getLogger("test")
    s1_inventory.run(paths, cfg, logger)

    (paths.override_dir / "s4_strategy_override.csv").write_text(
        "SP_ID,STRATEGY,REASON\nAPP_OWNER.NO_SUCH_SP,MANUAL,test\n", encoding="utf-8")
    report = validate_stage.validate(paths, cfg)
    assert "OVERRIDE_SP_ID_UNKNOWN" in _codes(report)


def test_override_sp_id_known_passes(tmp_path: Path) -> None:
    paths = _setup(tmp_path)
    cfg = load_config(paths.config)
    logger = logging.getLogger("test")
    s1_inventory.run(paths, cfg, logger)

    (paths.override_dir / "s4_strategy_override.csv").write_text(
        "SP_ID,STRATEGY,REASON\nAPP_OWNER.CALC_TAX,MANUAL,test\n", encoding="utf-8")
    report = validate_stage.validate(paths, cfg)
    assert "OVERRIDE_SP_ID_UNKNOWN" not in _codes(report)


def test_override_sp_id_check_skipped_before_s1_run(tmp_path: Path) -> None:
    """S1 을 아직 실행하지 않았으면 검증 대상 인벤토리가 없어 이 규칙은 건너뜀."""
    paths = _setup(tmp_path)
    (paths.override_dir / "s4_strategy_override.csv").write_text(
        "SP_ID,STRATEGY,REASON\nAPP_OWNER.NO_SUCH_SP,MANUAL,test\n", encoding="utf-8")
    cfg = load_config(paths.config)
    report = validate_stage.validate(paths, cfg)
    assert "OVERRIDE_SP_ID_UNKNOWN" not in _codes(report)


def test_parser_success_rate_low_warns(tmp_path: Path) -> None:
    paths = _setup(tmp_path)
    cfg = load_config(paths.config)
    logger = logging.getLogger("test")
    s1_inventory.run(paths, cfg, logger)
    s2_metrics.run(paths, cfg, logger)

    bench_csv = paths.stage_output("s2_metrics") / "s2_parser_bench.csv"
    bench_csv.write_text("PARSE_MODE,COUNT,SUCCESS_RATE\nREGEX,5,0.5\n", encoding="utf-8")
    report = validate_stage.validate(paths, cfg)
    assert "PARSER_SUCCESS_RATE_LOW" in _codes(report)


def test_calibration_sample_size_insufficient_is_info(tmp_path: Path) -> None:
    paths = _setup(tmp_path)
    (paths.override_dir / "pilot_effort.csv").write_text(
        "SP_ID,ACTUAL_MD\nAPP_OWNER.CALC_TAX,1.0\n", encoding="utf-8")
    cfg = load_config(paths.config)
    report = validate_stage.validate(paths, cfg)
    hits = [f for f in report.findings if f.code == "CALIBRATION_SAMPLES_INSUFFICIENT"]
    assert hits
    assert hits[0].level == "INFO"
    assert not report.has_errors


def test_override_reason_missing_is_error(tmp_path: Path) -> None:
    paths = _setup(tmp_path)
    (paths.override_dir / "s4_strategy_override.csv").write_text(
        "SP_ID,STRATEGY,REASON\nAPP_OWNER.CALC_TAX,MANUAL,\n", encoding="utf-8")
    cfg = load_config(paths.config)
    report = validate_stage.validate(paths, cfg)
    assert "OVERRIDE_REASON_MISSING" in _codes(report)
