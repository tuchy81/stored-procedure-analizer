"""S3 스모크/단위 테스트 — examples 데이터로 end-to-end 실행."""
from __future__ import annotations

from pathlib import Path
import logging
import shutil

from sp_assessor.core.config import load_config
from sp_assessor.core.paths import ProjectPaths
from sp_assessor.stages import s1_inventory, s2_metrics, s3_graph
from sp_assessor.io.csv_io import read_csv


EXAMPLES = Path(__file__).parent.parent / "examples"


def _run_s1_s2_s3(tmp_path: Path, extra_override_files: dict[str, str] | None = None):
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
    s3_graph.run(paths, cfg, logger)
    return paths


def test_ref_table_edges_from_dependencies(tmp_path: Path) -> None:
    paths = _run_s1_s2_s3(tmp_path)
    edges = read_csv(paths.stage_output("s3_graph") / "s3_edges.csv")
    hit = edges[(edges["SRC"] == "APP_OWNER.GET_ORDER") & (edges["DST"] == "TABLE::APP_OWNER.ORDERS")]
    assert not hit.empty
    assert hit.iloc[0]["EDGE_TYPE"] == "REF_TABLE"
    assert hit.iloc[0]["EDGE_CONFIDENCE"] == "HIGH"


def test_synonym_remote_edge_resolved(tmp_path: Path) -> None:
    """SYNC_HR -> HR_EMP(synonym) -> REMOTE_HR.EMPLOYEE@HR_LINK 로 정상 해석되어 REMOTE 엣지 생성."""
    paths = _run_s1_s2_s3(tmp_path)
    edges = read_csv(paths.stage_output("s3_graph") / "s3_edges.csv")
    hit = edges[edges["SRC"] == "APP_OWNER.SYNC_HR"]
    assert not hit.empty
    assert hit.iloc[0]["DST"] == "REMOTE::REMOTE_HR.EMPLOYEE@HR_LINK"
    assert hit.iloc[0]["IS_DBLINK"] == "Y"
    assert hit.iloc[0]["CUT_POINT"] == "Y"


def test_mutual_scc_detected(tmp_path: Path) -> None:
    """GET_ORDER <-> CALC_TAX 상호 의존이 SCC 로 탐지되어야 함."""
    paths = _run_s1_s2_s3(tmp_path)
    scc = read_csv(paths.stage_output("s3_graph") / "s3_scc.csv")
    members = set(scc[scc["SCC_ID"] == scc["SCC_ID"].iloc[0]]["MEMBER_SP_ID"]) if not scc.empty else set()
    assert {"APP_OWNER.GET_ORDER", "APP_OWNER.CALC_TAX"} <= members


def test_scc_members_share_wave(tmp_path: Path) -> None:
    paths = _run_s1_s2_s3(tmp_path)
    waves = read_csv(paths.stage_output("s3_graph") / "s3_waves.csv")
    w1 = waves[waves["SP_ID"] == "APP_OWNER.GET_ORDER"].iloc[0]["WAVE_NO"]
    w2 = waves[waves["SP_ID"] == "APP_OWNER.CALC_TAX"].iloc[0]["WAVE_NO"]
    assert w1 == w2


def test_cross_schema_grant_missing_flagged(tmp_path: Path) -> None:
    """BATCH_OWNER.DAILY_JOB 가 APP_OWNER.CUSTOMER_PKG 를 호출하지만 EXECUTE grant 없음 -> GRANT_OK=N."""
    paths = _run_s1_s2_s3(tmp_path)
    edges = read_csv(paths.stage_output("s3_graph") / "s3_edges.csv")
    hit = edges[(edges["SRC"] == "BATCH_OWNER.DAILY_JOB") & (edges["DST"] == "APP_OWNER.CUSTOMER_PKG")]
    assert not hit.empty
    assert hit.iloc[0]["GRANT_OK"] == "N"

    review = read_csv(paths.stage_output("s3_graph") / "s3_review.csv")
    assert "GRANT_MISSING" in set(review["REASON_CODE"])


def test_same_schema_call_grant_ok(tmp_path: Path) -> None:
    paths = _run_s1_s2_s3(tmp_path)
    edges = read_csv(paths.stage_output("s3_graph") / "s3_edges.csv")
    hit = edges[(edges["SRC"] == "APP_OWNER.GET_ORDER") & (edges["DST"] == "APP_OWNER.CALC_TAX")]
    assert not hit.empty
    assert hit.iloc[0]["GRANT_OK"] == "Y"


def test_app_call_resolves_package_qualified_name(tmp_path: Path) -> None:
    """in_app_calls.csv 의 'APP_OWNER.CUSTOMER_PKG.ADD_CUSTOMER' (3-part) 가 오버로드 접미 없이도 매칭되어야 함."""
    paths = _run_s1_s2_s3(tmp_path)
    edges = read_csv(paths.stage_output("s3_graph") / "s3_edges.csv")
    hit = edges[(edges["SRC"] == "APP::webapp") & (edges["DST"] == "APP_OWNER.CUSTOMER_PKG.ADD_CUSTOMER#0")]
    assert not hit.empty
    assert hit.iloc[0]["EDGE_TYPE"] == "APP_CALL"
    assert hit.iloc[0]["EDGE_CONFIDENCE"] == "HIGH"


def test_dynamic_sql_variable_creates_low_confidence_unknown_edge(tmp_path: Path) -> None:
    paths = _run_s1_s2_s3(tmp_path)
    edges = read_csv(paths.stage_output("s3_graph") / "s3_edges.csv")
    hit = edges[edges["SRC"] == "APP_OWNER.COMPLEX_PROC"]
    assert not hit.empty
    assert hit.iloc[0]["EDGE_CONFIDENCE"] == "LOW"
    assert hit.iloc[0]["EDGE_TYPE"] == "DYNAMIC_SQL_VARIABLE"

    review = read_csv(paths.stage_output("s3_graph") / "s3_review.csv")
    assert "LOW_CONFIDENCE_EDGE" in set(review["REASON_CODE"])


def test_dynsql_resolved_upgrades_to_medium_edge(tmp_path: Path) -> None:
    """s2_dynsql_resolve.csv 로 확정되면 s3 는 MEDIUM 확신도 REF_TABLE-급 엣지를 생성해야 함."""
    paths = _run_s1_s2_s3(tmp_path, extra_override_files={
        "s2_dynsql_resolve.csv":
            "SRC_SP_ID,RESOLVED_TABLES,REASON\n"
            "APP_OWNER.COMPLEX_PROC,CUSTOMER,confirmed via manual code review\n",
    })
    edges = read_csv(paths.stage_output("s3_graph") / "s3_edges.csv")
    hit = edges[(edges["SRC"] == "APP_OWNER.COMPLEX_PROC") & (edges["DST"] == "TABLE::APP_OWNER.CUSTOMER")]
    assert not hit.empty
    assert hit.iloc[0]["EDGE_CONFIDENCE"] == "MEDIUM"
    assert hit.iloc[0]["EDGE_TYPE"] == "DYNAMIC_SQL_RESOLVED"


def test_orphan_nodes_flagged(tmp_path: Path) -> None:
    paths = _run_s1_s2_s3(tmp_path)
    review = read_csv(paths.stage_output("s3_graph") / "s3_review.csv")
    orphans = set(review[review["REASON_CODE"] == "ORPHAN_NODE"]["SUBJECT"])
    assert "APP_OWNER.LEGACY_WRAPPED_PROC" in orphans


def test_edge_override_add_and_remove(tmp_path: Path) -> None:
    paths = _run_s1_s2_s3(tmp_path, extra_override_files={
        "s3_edges_override.csv":
            "SRC,DST,ACTION,REASON\n"
            "APP_OWNER.DIRECT_LINK_CALL,APP_OWNER.CALC_TAX,ADD,manually confirmed call via dynamic SQL review\n"
            "APP_OWNER.GET_ORDER,APP_OWNER.CALC_TAX,REMOVE,false positive from stale dependency metadata\n",
    })
    edges = read_csv(paths.stage_output("s3_graph") / "s3_edges.csv")
    added = edges[(edges["SRC"] == "APP_OWNER.DIRECT_LINK_CALL") & (edges["DST"] == "APP_OWNER.CALC_TAX")]
    assert not added.empty
    assert added.iloc[0]["EDGE_CONFIDENCE"] == "HIGH"

    removed = edges[(edges["SRC"] == "APP_OWNER.GET_ORDER") & (edges["DST"] == "APP_OWNER.CALC_TAX")]
    assert removed.empty


def test_cluster_override_applied(tmp_path: Path) -> None:
    paths = _run_s1_s2_s3(tmp_path, extra_override_files={
        "s3_cluster_override.csv":
            "SP_ID,CLUSTER_ID,REASON\n"
            "APP_OWNER.CALC_TAX,999,domain expert grouping override\n",
    })
    nodes = read_csv(paths.stage_output("s3_graph") / "s3_nodes.csv")
    row = nodes[nodes["NODE_ID"] == "APP_OWNER.CALC_TAX"].iloc[0]
    assert int(row["CLUSTER_ID"]) == 999


def test_mermaid_file_written(tmp_path: Path) -> None:
    paths = _run_s1_s2_s3(tmp_path)
    mmd = (paths.stage_output("s3_graph") / "s3_graph.mmd").read_text(encoding="utf-8")
    assert mmd.startswith("graph LR")
    assert "-->" in mmd
