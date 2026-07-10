"""spike / diff / override lint 스모크 테스트."""
from __future__ import annotations

from pathlib import Path
import logging
import shutil

from sp_assessor.core.config import load_config
from sp_assessor.core.paths import ProjectPaths
from sp_assessor.stages import s1_inventory, s2_metrics, s3_graph, s4_scoring, spike, diff, override_lint


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


# ---------------------------------------------------------------------------
# spike
# ---------------------------------------------------------------------------

def test_spike_runs_s1_s2_automatically_and_writes_report(tmp_path: Path) -> None:
    paths = _setup(tmp_path)
    cfg = load_config(paths.config)
    logger = logging.getLogger("test")

    result = spike.run(paths, cfg, logger)

    assert result["sp1"] is not None
    assert result["sp2"] is not None
    assert result["sp3"] is not None
    report = (paths.output_dir / "_spike" / "spike_report.md").read_text(encoding="utf-8")
    assert "SP-1" in report and "SP-2" in report and "SP-3" in report
    assert "PASS" in report or "FAIL" in report


def test_spike_sp2_below_threshold_lists_fallback(tmp_path: Path) -> None:
    """examples 데이터는 literal=1, variable=1 -> 50% < 60% 임계 미달."""
    paths = _setup(tmp_path)
    cfg = load_config(paths.config)
    logger = logging.getLogger("test")
    spike.run(paths, cfg, logger)
    report = (paths.output_dir / "_spike" / "spike_report.md").read_text(encoding="utf-8")
    assert "FAIL" in report
    assert "override 강제 전환" in report


# ---------------------------------------------------------------------------
# diff
# ---------------------------------------------------------------------------

def _run_all_and_snapshot(paths: ProjectPaths, cfg, tag: str) -> None:
    import shutil as sh
    logger = logging.getLogger("test")
    s1_inventory.run(paths, cfg, logger)
    s2_metrics.run(paths, cfg, logger)
    s3_graph.run(paths, cfg, logger)
    s4_scoring.run(paths, cfg, logger)
    snap = paths.snapshot_dir(tag)
    snap.mkdir(parents=True, exist_ok=True)
    sh.copytree(paths.output_dir, snap / "output", dirs_exist_ok=True,
               ignore=sh.ignore_patterns("_snapshots"))


def test_diff_detects_strategy_change_between_tags(tmp_path: Path) -> None:
    paths = _setup(tmp_path)
    cfg = load_config(paths.config)
    _run_all_and_snapshot(paths, cfg, "v1")

    override_dir = paths.override_dir
    (override_dir / "s4_strategy_override.csv").write_text(
        "SP_ID,STRATEGY,REASON\nAPP_OWNER.CALC_TAX,DEFER,forced for diff test\n", encoding="utf-8")
    s4_scoring.run(paths, cfg, logging.getLogger("test"))
    import shutil as sh
    snap = paths.snapshot_dir("v2")
    snap.mkdir(parents=True, exist_ok=True)
    sh.copytree(paths.output_dir, snap / "output", dirs_exist_ok=True, ignore=sh.ignore_patterns("_snapshots"))

    result = diff.compute_diff(paths, "s4", "v1", "v2")
    assert not result.new_ids
    assert not result.removed_ids
    changed_sp_ids = {c.key for c in result.changed}
    assert "APP_OWNER.CALC_TAX" in changed_sp_ids
    strategy_change = [c for c in result.changed if c.key == "APP_OWNER.CALC_TAX" and c.column == "STRATEGY"]
    assert strategy_change
    assert strategy_change[0].new == "DEFER"


def test_diff_missing_snapshot_raises(tmp_path: Path) -> None:
    paths = _setup(tmp_path)
    cfg = load_config(paths.config)
    _run_all_and_snapshot(paths, cfg, "v1")
    try:
        diff.compute_diff(paths, "s4", "v1", "does_not_exist")
        assert False, "should have raised"
    except FileNotFoundError:
        pass


def test_diff_rejects_s5(tmp_path: Path) -> None:
    paths = _setup(tmp_path)
    try:
        diff.compute_diff(paths, "s5", "v1", "v2")
        assert False, "should have raised"
    except ValueError:
        pass


# ---------------------------------------------------------------------------
# override lint
# ---------------------------------------------------------------------------

def test_override_lint_clean_reports_no_findings(tmp_path: Path) -> None:
    paths = _setup(tmp_path)
    report = override_lint.lint(paths)
    assert not report.has_errors


def test_override_lint_detects_conflicting_include_exclude(tmp_path: Path) -> None:
    paths = _setup(tmp_path)
    (paths.override_dir / "s1_inventory_override.csv").write_text(
        "SP_ID,ACTION,RESOLVED_TARGET,REASON\n"
        "APP_OWNER.CALC_TAX,EXCLUDE,,dead code candidate\n"
        "APP_OWNER.CALC_TAX,INCLUDE,,actually still used by batch\n",
        encoding="utf-8",
    )
    report = override_lint.lint(paths)
    assert report.has_errors
    assert "CONFLICTING_ACTION" in {f.code for f in report.findings}


def test_override_lint_detects_conflicting_strategy(tmp_path: Path) -> None:
    paths = _setup(tmp_path)
    (paths.override_dir / "s4_strategy_override.csv").write_text(
        "SP_ID,STRATEGY,REASON\n"
        "APP_OWNER.CALC_TAX,MANUAL,first reviewer opinion\n"
        "APP_OWNER.CALC_TAX,DEFER,second reviewer disagreement\n",
        encoding="utf-8",
    )
    report = override_lint.lint(paths)
    assert "CONFLICTING_STRATEGY" in {f.code for f in report.findings}


def test_override_lint_detects_blank_reason(tmp_path: Path) -> None:
    paths = _setup(tmp_path)
    (paths.override_dir / "s4_strategy_override.csv").write_text(
        "SP_ID,STRATEGY,REASON\nAPP_OWNER.CALC_TAX,MANUAL,\n", encoding="utf-8")
    report = override_lint.lint(paths)
    assert "REASON_BLANK" in {f.code for f in report.findings}
