"""S5 스모크/단위 테스트 — examples 데이터로 end-to-end 실행."""
from __future__ import annotations

from pathlib import Path
import logging
import shutil
import subprocess

import pandas as pd

from sp_assessor.core.config import load_config
from sp_assessor.core.paths import ProjectPaths
from sp_assessor.stages import s1_inventory, s2_metrics, s3_graph, s4_scoring, s5_report
from sp_assessor.io.csv_io import read_csv


EXAMPLES = Path(__file__).parent.parent / "examples"


def _run_s1_to_s5(tmp_path: Path, extra_override_files: dict[str, str] | None = None, init_git: bool = False):
    for sub in ("input", "override"):
        src = EXAMPLES / sub
        if src.exists():
            shutil.copytree(src, tmp_path / sub)
    shutil.copy(EXAMPLES / "config.yaml", tmp_path / "config.yaml")

    if extra_override_files:
        (tmp_path / "override").mkdir(parents=True, exist_ok=True)
        for name, content in extra_override_files.items():
            (tmp_path / "override" / name).write_text(content, encoding="utf-8")

    if init_git:
        (tmp_path / "override").mkdir(parents=True, exist_ok=True)
        (tmp_path / "override" / ".gitkeep").write_text("", encoding="utf-8")
        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
        subprocess.run(["git", "add", "override"], cwd=tmp_path, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "add overrides"], cwd=tmp_path, check=True)

    paths = ProjectPaths.from_root(tmp_path)
    paths.ensure()
    cfg = load_config(paths.config)
    logger = logging.getLogger("test")
    s1_inventory.run(paths, cfg, logger)
    s2_metrics.run(paths, cfg, logger)
    s3_graph.run(paths, cfg, logger)
    s4_scoring.run(paths, cfg, logger)
    s5_report.run(paths, cfg, logger)
    return paths


def test_summary_contains_counts_and_effort(tmp_path: Path) -> None:
    paths = _run_s1_to_s5(tmp_path)
    text = (paths.stage_output("s5_report") / "s5_summary.md").read_text(encoding="utf-8")
    assert "분석 대상 SP 수" in text
    assert "AUTO_SIMPLE" in text
    assert "P50 합계" in text
    assert "P90 합계" in text


def test_roadmap_groups_by_wave_and_cluster(tmp_path: Path) -> None:
    paths = _run_s1_to_s5(tmp_path)
    roadmap = read_csv(paths.stage_output("s5_report") / "s5_roadmap.csv")
    assert not roadmap.empty
    assert {"WAVE_NO", "CLUSTER_ID", "SP_COUNT", "SP_IDS", "PRECONDITION", "DBLINK_CUTPOINT_COUNT"} <= set(roadmap.columns)
    total_sp_count = roadmap["SP_COUNT"].sum()
    assert total_sp_count == 10


def test_roadmap_dblink_cutpoint_count(tmp_path: Path) -> None:
    paths = _run_s1_to_s5(tmp_path)
    roadmap = read_csv(paths.stage_output("s5_report") / "s5_roadmap.csv")
    hit = roadmap[roadmap["SP_IDS"].str.contains("DAILY_JOB")]
    assert not hit.empty
    assert int(hit.iloc[0]["DBLINK_CUTPOINT_COUNT"]) >= 1


def test_dblink_cutpoints_lists_remote_targets(tmp_path: Path) -> None:
    paths = _run_s1_to_s5(tmp_path)
    text = (paths.stage_output("s5_report") / "s5_dblink_cutpoints.md").read_text(encoding="utf-8")
    assert "BATCH_OWNER.DAILY_JOB" in text
    assert "REMOTE_HR.EMPLOYEE" in text
    assert "REST API" in text or "MV" in text or "View" in text


def test_risk_register_flags_wrapped_and_autonomous_tx(tmp_path: Path) -> None:
    paths = _run_s1_to_s5(tmp_path)
    text = (paths.stage_output("s5_report") / "s5_risk_register.md").read_text(encoding="utf-8")
    assert "APP_OWNER.LEGACY_WRAPPED_PROC" in text
    assert "APP_OWNER.COMPLEX_PROC" in text
    assert "WRAP 소스 (1건)" in text
    assert "Autonomous Transaction (1건)" in text


def test_inventory_xlsx_written_with_expected_columns(tmp_path: Path) -> None:
    paths = _run_s1_to_s5(tmp_path)
    xlsx = paths.stage_output("s5_report") / "s5_inventory_full.xlsx"
    assert xlsx.exists()
    df = pd.read_excel(xlsx)
    assert len(df) == 10
    assert {"SP_ID", "D_SCORE", "C_SCORE", "STRATEGY", "OVERRIDDEN"} <= set(df.columns)


def test_quadrant_svg_written(tmp_path: Path) -> None:
    paths = _run_s1_to_s5(tmp_path)
    svg = paths.stage_output("s5_report") / "s5_quadrant.svg"
    assert svg.exists()
    assert svg.read_text(encoding="utf-8").startswith("<?xml")


def test_override_audit_lists_reasons(tmp_path: Path) -> None:
    paths = _run_s1_to_s5(tmp_path, extra_override_files={
        "s4_strategy_override.csv":
            "SP_ID,STRATEGY,REASON\n"
            "APP_OWNER.CALC_TAX,MANUAL,domain expert insists on manual review\n",
    })
    text = (paths.stage_output("s5_report") / "s5_override_audit.md").read_text(encoding="utf-8")
    assert "APP_OWNER.CALC_TAX" in text
    assert "domain expert insists on manual review" in text


def test_override_audit_uses_git_history_when_available(tmp_path: Path) -> None:
    paths = _run_s1_to_s5(tmp_path, init_git=True)
    text = (paths.stage_output("s5_report") / "s5_override_audit.md").read_text(encoding="utf-8")
    assert "git 이력 없음" not in text
    assert "add overrides" in text


def test_overridden_sp_marked_with_asterisk_in_roadmap(tmp_path: Path) -> None:
    paths = _run_s1_to_s5(tmp_path, extra_override_files={
        "s4_strategy_override.csv":
            "SP_ID,STRATEGY,REASON\n"
            "APP_OWNER.CALC_TAX,MANUAL,domain expert insists on manual review\n",
    })
    roadmap = read_csv(paths.stage_output("s5_report") / "s5_roadmap.csv")
    hit = roadmap[roadmap["SP_IDS"].str.contains("CALC_TAX")]
    assert not hit.empty
    assert "APP_OWNER.CALC_TAX*" in hit.iloc[0]["SP_IDS"]
