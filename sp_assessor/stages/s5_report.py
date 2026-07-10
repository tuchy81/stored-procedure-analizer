"""S5 — 리포트 생성 (의사결정용 최종 산출물)."""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import pandas as pd

from sp_assessor.core.config import Config
from sp_assessor.core.paths import ProjectPaths
from sp_assessor.io.csv_io import read_csv, write_csv
from sp_assessor.util.text import clean_str

LARGE_SCC_THRESHOLD = 3
LOW_CONFIDENCE_CLUSTER_THRESHOLD = 2
PII_NAME_PATTERNS = ("RRN", "SSN", "주민번호", "PASSWORD", "PASSWD", "CARD_NO", "CARDNO", "계좌번호", "ACCOUNT_NO")


def _collect_overridden_sp_ids(override_dir: Path) -> set[str]:
    ids: set[str] = set()
    specs = [
        ("s1_inventory_override.csv", ["SP_ID"]),
        ("s2_metrics_override.csv", ["SP_ID"]),
        ("s2_dynsql_resolve.csv", ["SRC_SP_ID"]),
        ("s3_edges_override.csv", ["SRC", "DST"]),
        ("s3_cluster_override.csv", ["SP_ID"]),
        ("s4_strategy_override.csv", ["SP_ID"]),
    ]
    for filename, cols in specs:
        df = read_csv(override_dir / filename)
        if df.empty:
            continue
        for col in cols:
            if col in df.columns:
                ids.update(df[col].dropna().astype(str))
    return ids


def _mark(sp_id: str, overridden: set[str]) -> str:
    return f"{sp_id}*" if sp_id in overridden else sp_id


def _write_summary(scores: pd.DataFrame, overridden: set[str], out: Path) -> None:
    lines = ["# S5 총괄 요약", ""]
    lines.append(f"- 분석 대상 SP 수: **{len(scores)}**")
    lines.append(f"- Override 반영 SP 수: **{len(overridden & set(scores['SP_ID']))}** (`*` 마커 참조)")
    lines.append("")

    lines.append("## 4분면 분포")
    lines.append("")
    lines.append("| QUADRANT | 건수 |")
    lines.append("|---|---|")
    for q, cnt in scores["QUADRANT"].value_counts().items():
        lines.append(f"| {q} | {cnt} |")
    lines.append("")

    lines.append("## 전략 분포")
    lines.append("")
    lines.append("| STRATEGY | 건수 |")
    lines.append("|---|---|")
    for s, cnt in scores["STRATEGY"].value_counts().items():
        lines.append(f"| {s} | {cnt} |")
    lines.append("")

    total_p50 = scores["EFFORT_P50"].sum()
    total_p90 = scores["EFFORT_P90"].sum()
    lines.append("## 총 예상 공수")
    lines.append("")
    lines.append(f"- P50 합계: **{total_p50:.1f} MD**")
    lines.append(f"- P90 합계: **{total_p90:.1f} MD**")
    lines.append("")
    lines.append("## 신뢰도 분포")
    lines.append("")
    for conf, cnt in scores["CONFIDENCE"].value_counts().items():
        lines.append(f"- {conf}: {cnt}건")
    if (scores["CONFIDENCE"] == "LOW").all():
        lines.append("")
        lines.append("> 캘리브레이션 미실행 상태 — 휴리스틱 공수 추정치임 (`override/pilot_effort.csv` 로 정밀화 가능)")

    out.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_roadmap(scores: pd.DataFrame, edges: pd.DataFrame, overridden: set[str], out_csv: Path) -> None:
    if scores.empty:
        write_csv(pd.DataFrame(columns=["WAVE_NO", "CLUSTER_ID", "SP_COUNT", "SP_IDS",
                                        "PRECONDITION", "DBLINK_CUTPOINT_COUNT"]), out_csv)
        return

    dblink_srcs: set[str] = set()
    if not edges.empty and "CUT_POINT" in edges.columns:
        dblink_srcs = set(edges[edges["CUT_POINT"] == "Y"]["SRC"])

    rows = []
    for (wave, cluster), grp in scores.groupby(["WAVE_NO", "CLUSTER_ID"]):
        sp_ids = sorted(grp["SP_ID"])
        marked = [_mark(s, overridden) for s in sp_ids]
        cutpoints = sum(1 for s in sp_ids if s in dblink_srcs)
        precondition = "선행 wave 없음" if wave <= 0 else f"WAVE_NO < {wave} 전환 완료 필요"
        rows.append({
            "WAVE_NO": wave, "CLUSTER_ID": cluster, "SP_COUNT": len(sp_ids),
            "SP_IDS": ";".join(marked), "PRECONDITION": precondition,
            "DBLINK_CUTPOINT_COUNT": cutpoints,
        })
    roadmap = pd.DataFrame(rows).sort_values(["WAVE_NO", "CLUSTER_ID"]).reset_index(drop=True)
    write_csv(roadmap, out_csv)


def _write_inventory_xlsx(inventory: pd.DataFrame, metrics: pd.DataFrame, nodes: pd.DataFrame,
                          scores: pd.DataFrame, overridden: set[str], out_xlsx: Path,
                          logger: logging.Logger) -> bool:
    try:
        import openpyxl  # noqa: F401
    except ImportError:
        logger.warning("openpyxl 미설치 — s5_inventory_full.xlsx 생략")
        return False

    merged = inventory.merge(metrics, on="SP_ID", how="left", suffixes=("", "_S2"))
    if not nodes.empty:
        node_cols = nodes[nodes["NODE_TYPE"].isin(["SP", "TRIGGER"])][
            ["NODE_ID", "FAN_IN", "FAN_OUT", "BETWEENNESS", "CLUSTER_ID", "WAVE_NO", "SCC_ID"]
        ].rename(columns={"NODE_ID": "SP_ID"})
        merged = merged.merge(node_cols, on="SP_ID", how="left", suffixes=("", "_S3"))
    if not scores.empty:
        score_cols = scores[["SP_ID", "D_SCORE", "C_SCORE", "QUADRANT", "STRATEGY",
                             "EFFORT_EST_MD", "EFFORT_P50", "EFFORT_P90", "CONFIDENCE"]]
        merged = merged.merge(score_cols, on="SP_ID", how="left", suffixes=("", "_S4"))

    merged["OVERRIDDEN"] = merged["SP_ID"].isin(overridden).map({True: "Y", False: "N"})
    merged.to_excel(out_xlsx, sheet_name="inventory_full", index=False)
    return True


def _write_quadrant_svg(scores: pd.DataFrame, config: Config, out_svg: Path, logger: logging.Logger) -> bool:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        logger.warning("matplotlib 미설치 — s5_quadrant.svg 생략")
        return False

    if scores.empty:
        return False

    fig, ax = plt.subplots(figsize=(8, 6))
    strategy_colors = {
        "AUTO_SIMPLE": "#2ca02c", "AUTO_ASSISTED": "#98df8a", "SEMI": "#ff7f0e",
        "MANUAL": "#d62728", "DEFER": "#7f0000",
    }
    for strategy, grp in scores.groupby("STRATEGY"):
        ax.scatter(grp["D_SCORE"], grp["C_SCORE"], label=strategy,
                  color=strategy_colors.get(strategy, "#1f77b4"), alpha=0.8)
    ax.axvline(config.quadrant.d_threshold_pct, color="gray", linestyle="--", linewidth=0.8)
    ax.axhline(config.quadrant.c_threshold_pct, color="gray", linestyle="--", linewidth=0.8)
    ax.set_xlabel("D_SCORE (dependency)")
    ax.set_ylabel("C_SCORE (complexity)")
    ax.set_title("SP Conversion Assessment Quadrant")
    ax.legend(loc="best", fontsize=8)
    fig.tight_layout()
    fig.savefig(out_svg, format="svg")
    plt.close(fig)
    return True


REMOTE_TYPE_SUGGESTIONS = {
    "TABLE": "Materialized View 복제 또는 REST API 배치 동기화 검토",
    "VIEW": "로컬 View 재정의 또는 REST API 조회 프록시 검토",
    "MATERIALIZED VIEW": "MV Refresh 주기 재설계 또는 이벤트 기반 동기화 검토",
}
DEFAULT_REMOTE_SUGGESTION = "REST API 연동 · 이벤트 기반 동기화 · 로컬 캐시(View/MV) 중 택1 검토 필요"


def _write_dblink_cutpoints(edges: pd.DataFrame, remote_objects_types: dict[tuple[str, str], str],
                            overridden: set[str], out_md: Path) -> None:
    lines = ["# DB Link 절단점 및 대체 인터페이스 후보", ""]
    if edges.empty or "IS_DBLINK" not in edges.columns:
        lines.append("DB Link 의존 SP 없음.")
        out_md.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return

    dblink_edges = edges[edges["IS_DBLINK"] == "Y"]
    if dblink_edges.empty:
        lines.append("DB Link 의존 SP 없음.")
        out_md.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return

    lines.append("| SP_ID | 원격 대상 | 대체 인터페이스 후보 |")
    lines.append("|---|---|---|")
    for _, row in dblink_edges.iterrows():
        dst = row["DST"]
        remote_key = dst.replace("REMOTE::", "").split("@")[0]
        owner, _, name = remote_key.rpartition(".")
        remote_type = remote_objects_types.get((owner, name), "")
        suggestion = REMOTE_TYPE_SUGGESTIONS.get(remote_type.upper(), DEFAULT_REMOTE_SUGGESTION)
        lines.append(f"| {_mark(row['SRC'], overridden)} | {dst} | {suggestion} |")

    out_md.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _git_override_history(root: Path) -> list[str]:
    try:
        result = subprocess.run(
            ["git", "log", "--follow", "--date=short", "--pretty=format:%ad %an %s", "--", "override"],
            cwd=root, capture_output=True, text=True, timeout=10,
        )
    except (FileNotFoundError, OSError):
        return []
    if result.returncode != 0:
        return []
    return [line for line in result.stdout.splitlines() if line.strip()]


def _write_override_audit(paths: ProjectPaths, out_md: Path) -> None:
    lines = ["# Override 이력 감사", ""]

    history = _git_override_history(paths.root)
    lines.append("## Git 변경 이력 (`override/`)")
    lines.append("")
    if history:
        for entry in history:
            lines.append(f"- {entry}")
    else:
        lines.append("_git 이력 없음 (git 저장소가 아니거나 override/ 변경 이력 없음)_")
    lines.append("")

    lines.append("## 현재 Override 파일 스냅샷")
    lines.append("")
    for filename in ("s1_inventory_override.csv", "s2_metrics_override.csv", "s2_dynsql_resolve.csv",
                     "s3_edges_override.csv", "s3_cluster_override.csv", "s4_strategy_override.csv",
                     "pilot_effort.csv"):
        df = read_csv(paths.override_dir / filename)
        lines.append(f"### {filename}")
        lines.append("")
        if df.empty:
            lines.append("_없음_")
        else:
            lines.append(f"- {len(df)} 건")
            if "REASON" in df.columns:
                for _, row in df.iterrows():
                    subject = row.get("SP_ID") or row.get("SRC_SP_ID") or f"{row.get('SRC','')}->{row.get('DST','')}"
                    lines.append(f"  - `{subject}`: {row.get('REASON', '')}")
        lines.append("")

    out_md.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_risk_register(inventory: pd.DataFrame, metrics: pd.DataFrame, scc: pd.DataFrame,
                         nodes: pd.DataFrame, edges: pd.DataFrame, args_df: pd.DataFrame,
                         out_md: Path) -> None:
    lines = ["# 리스크 레지스터", ""]

    wrapped = inventory[inventory["WRAPPED"] == "Y"] if not inventory.empty else pd.DataFrame()
    lines.append(f"## WRAP 소스 ({len(wrapped)}건)")
    lines.append("")
    for sp_id in wrapped.get("SP_ID", []):
        lines.append(f"- `{sp_id}` — 암호화된 소스, 정적 분석 불가. 수동 검토/재작성 필요")
    if wrapped.empty:
        lines.append("_없음_")
    lines.append("")

    autonomous = metrics[metrics["AUTONOMOUS_TX_FLAG"] >= 1] if not metrics.empty else pd.DataFrame()
    lines.append(f"## Autonomous Transaction ({len(autonomous)}건)")
    lines.append("")
    for sp_id in autonomous.get("SP_ID", []):
        lines.append(f"- `{sp_id}` — 독립 트랜잭션. Spring @Transactional(REQUIRES_NEW) 등 별도 설계 필요")
    if autonomous.empty:
        lines.append("_없음_")
    lines.append("")

    mutating = metrics[metrics["MUTATING_TRIGGER_RISK"] >= 1] if not metrics.empty else pd.DataFrame()
    lines.append(f"## Mutating Trigger 위험 ({len(mutating)}건)")
    lines.append("")
    for sp_id in mutating.get("SP_ID", []):
        lines.append(f"- `{sp_id}` — 트리거가 자기 테이블 재참조. ORA-04091 리스크")
    if mutating.empty:
        lines.append("_없음_")
    lines.append("")

    large_scc: list[tuple[int, int]] = []
    if not scc.empty:
        for scc_id, grp in scc.groupby("SCC_ID"):
            if len(grp) > LARGE_SCC_THRESHOLD:
                large_scc.append((scc_id, len(grp)))
    lines.append(f"## 대형 SCC ({len(large_scc)}건, 기준 크기 > {LARGE_SCC_THRESHOLD})")
    lines.append("")
    for scc_id, size in large_scc:
        members = ";".join(sorted(scc[scc["SCC_ID"] == scc_id]["MEMBER_SP_ID"]))
        lines.append(f"- SCC #{scc_id} (size={size}): {members}")
    if not large_scc:
        lines.append("_없음_")
    lines.append("")

    low_conf_clusters: list[tuple[int, int]] = []
    if not edges.empty and not nodes.empty and "CLUSTER_ID" in nodes.columns:
        cluster_of = dict(zip(nodes["NODE_ID"], nodes["CLUSTER_ID"]))
        low_edges = edges[edges["EDGE_CONFIDENCE"] == "LOW"]
        counts = low_edges["SRC"].map(cluster_of).value_counts()
        low_conf_clusters = [(int(c), int(n)) for c, n in counts.items()
                            if n >= LOW_CONFIDENCE_CLUSTER_THRESHOLD]
    lines.append(f"## LOW confidence 엣지 다수 클러스터 ({len(low_conf_clusters)}건, 기준 >= {LOW_CONFIDENCE_CLUSTER_THRESHOLD})")
    lines.append("")
    for cluster_id, cnt in low_conf_clusters:
        lines.append(f"- CLUSTER {cluster_id}: LOW confidence 엣지 {cnt}건")
    if not low_conf_clusters:
        lines.append("_없음_")
    lines.append("")

    pii_hits = []
    if not args_df.empty and "ARGUMENT_NAME" in args_df.columns:
        for name in args_df["ARGUMENT_NAME"].dropna().unique():
            upper = str(name).upper()
            if any(p in upper for p in PII_NAME_PATTERNS):
                pii_hits.append(name)
    lines.append(f"## 개인정보 의심 컬럼/파라미터명 ({len(pii_hits)}건)")
    lines.append("")
    for name in pii_hits:
        lines.append(f"- `{name}` — 마스킹/암호화 정책 검토 필요")
    if not pii_hits:
        lines.append("_없음_")

    out_md.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(paths: ProjectPaths, config: Config, logger: logging.Logger) -> dict:
    logger.info("S5 시작")
    s1_out = paths.stage_output("s1_inventory")
    s2_out = paths.stage_output("s2_metrics")
    s3_out = paths.stage_output("s3_graph")
    s4_out = paths.stage_output("s4_scoring")

    inventory = read_csv(s1_out / "s1_inventory.csv")
    metrics = read_csv(s2_out / "s2_metrics.csv")
    nodes = read_csv(s3_out / "s3_nodes.csv")
    edges = read_csv(s3_out / "s3_edges.csv")
    scc = read_csv(s3_out / "s3_scc.csv")
    scores = read_csv(s4_out / "s4_scores.csv")
    args_df = read_csv(paths.input_dir / "in_arguments.csv")

    if scores.empty:
        raise RuntimeError("s4_scores.csv missing/empty — run stages s1-s4 first")

    overridden = _collect_overridden_sp_ids(paths.override_dir)

    remote_objects_types: dict[tuple[str, str], str] = {}
    for p in sorted(paths.input_dir.glob("in_remote_objects__*.csv")):
        df = read_csv(p)
        if df.empty or "OWNER" not in df.columns or "OBJECT_NAME" not in df.columns:
            continue
        for _, row in df.iterrows():
            remote_objects_types[(row["OWNER"], row["OBJECT_NAME"])] = clean_str(row.get("OBJECT_TYPE"))

    out = paths.stage_output("s5_report")
    _write_summary(scores, overridden, out / "s5_summary.md")
    _write_roadmap(scores, edges, overridden, out / "s5_roadmap.csv")
    xlsx_written = _write_inventory_xlsx(inventory, metrics, nodes, scores, overridden,
                                        out / "s5_inventory_full.xlsx", logger)
    svg_written = _write_quadrant_svg(scores, config, out / "s5_quadrant.svg", logger)
    _write_dblink_cutpoints(edges, remote_objects_types, overridden, out / "s5_dblink_cutpoints.md")
    _write_override_audit(paths, out / "s5_override_audit.md")
    _write_risk_register(inventory, metrics, scc, nodes, edges, args_df, out / "s5_risk_register.md")

    logger.info("S5 완료 — summary/roadmap/dblink_cutpoints/override_audit/risk_register 작성 "
               "(xlsx=%s, svg=%s)", xlsx_written, svg_written)

    return {"scores": len(scores), "xlsx_written": xlsx_written, "svg_written": svg_written}
