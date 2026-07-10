"""S3 — 의존성 그래프 구축.

community_algorithm 기본값 "louvain" 은 networkx>=3.2 내장
`nx.community.louvain_communities` 로 처리한다 (python-louvain 등
optional dep 불필요). "leiden" 지정 시 optional dep(leidenalg/igraph)
을 시도하고, 없으면 louvain 으로 폴백 (parser 계층과 동일한 설계 철학).
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

import networkx as nx
import pandas as pd

from sp_assessor.core.config import Config
from sp_assessor.core.paths import ProjectPaths
from sp_assessor.io.csv_io import read_csv, write_csv
from sp_assessor.io.csv_schemas import OVERRIDE_SCHEMAS
from sp_assessor.stages.s1_inventory import _resolve_synonyms, SynonymMaps
from sp_assessor.util.text import clean_str

SP_LIKE_TYPES = {"PROCEDURE", "FUNCTION", "PACKAGE", "PACKAGE BODY"}
TABLE_LIKE_TYPES = {"TABLE", "VIEW", "MATERIALIZED VIEW"}
LARGE_SCC_THRESHOLD = 3


def _table_node(owner: str, name: str) -> str:
    return f"TABLE::{owner}.{name}"


def _remote_node(owner: str, name: str, link: str) -> str:
    return f"REMOTE::{owner}.{name}@{link}"


def _app_node(repo: str) -> str:
    return f"APP::{repo}"


def _job_node(owner: str, job_name: str) -> str:
    return f"JOB::{owner}.{job_name}"


def _unknown_node(tag: str) -> str:
    return f"UNKNOWN::{tag}"


class NodeRegistry:
    """NODE_ID -> NODE_TYPE 매핑 + 신규 노드 자동 등록."""

    def __init__(self) -> None:
        self.types: dict[str, str] = {}

    def add(self, node_id: str, node_type: str) -> str:
        self.types.setdefault(node_id, node_type)
        return node_id


def _sp_index(inventory: pd.DataFrame) -> tuple[dict[tuple[str, str], list[str]], dict[str, dict]]:
    """(OWNER, PKG_OR_NAME) -> [SP_ID,...] / SP_ID -> row dict."""
    by_object: dict[tuple[str, str], list[str]] = {}
    by_id: dict[str, dict] = {}
    for _, row in inventory.iterrows():
        sp_id = row["SP_ID"]
        by_id[sp_id] = row.to_dict()
        target = clean_str(row["PKG"]) or row["NAME"]
        by_object.setdefault((row["OWNER"], target), []).append(sp_id)
    return by_object, by_id


def _package_agg_node(owner: str, pkg_name: str, registry: NodeRegistry) -> str:
    node_id = f"{owner}.{pkg_name}"
    registry.add(node_id, "SP")
    return node_id


def _build_dependency_edges(deps: pd.DataFrame, inventory: pd.DataFrame,
                            by_object: dict, synonym_maps: SynonymMaps,
                            registry: NodeRegistry) -> list[dict]:
    edges: list[dict] = []
    if deps.empty:
        return edges

    for _, dep in deps.iterrows():
        src_matches = by_object.get((dep["OWNER"], dep["NAME"]))
        if not src_matches:
            continue  # source object not part of analysis scope (or itself a table/etc.)
        ref_type = str(dep.get("REFERENCED_TYPE", "")).upper()
        link = clean_str(dep.get("REFERENCED_LINK_NAME"))

        for src in src_matches:
            if link:
                dst = registry.add(_remote_node(dep["REFERENCED_OWNER"], dep["REFERENCED_NAME"], link), "REMOTE")
                edges.append({"SRC": src, "DST": dst, "EDGE_TYPE": "REMOTE_REF",
                             "EDGE_CONFIDENCE": "HIGH", "IS_DBLINK": "Y"})
                continue

            if ref_type in SP_LIKE_TYPES:
                dst_matches = by_object.get((dep["REFERENCED_OWNER"], dep["REFERENCED_NAME"]))
                if dst_matches and len(dst_matches) == 1:
                    dst = dst_matches[0]
                else:
                    dst = _package_agg_node(dep["REFERENCED_OWNER"], dep["REFERENCED_NAME"], registry)
                edges.append({"SRC": src, "DST": dst, "EDGE_TYPE": "CALL",
                             "EDGE_CONFIDENCE": "HIGH", "IS_DBLINK": "N"})
            elif ref_type in TABLE_LIKE_TYPES:
                dst = registry.add(_table_node(dep["REFERENCED_OWNER"], dep["REFERENCED_NAME"]), "TABLE")
                edges.append({"SRC": src, "DST": dst, "EDGE_TYPE": "REF_TABLE",
                             "EDGE_CONFIDENCE": "HIGH", "IS_DBLINK": "N"})
            elif ref_type == "SYNONYM":
                resolved = synonym_maps.resolve(dep["REFERENCED_OWNER"], dep["REFERENCED_NAME"])
                if resolved is None:
                    continue  # 미해석 시노님 — s1_unresolved 에서 이미 추적됨
                if resolved["IS_REMOTE"]:
                    dst = registry.add(
                        _remote_node(resolved["TABLE_OWNER"], resolved["TABLE_NAME"], resolved["DB_LINK"]), "REMOTE")
                    edges.append({"SRC": src, "DST": dst, "EDGE_TYPE": "REMOTE_REF",
                                 "EDGE_CONFIDENCE": "HIGH", "IS_DBLINK": "Y"})
                else:
                    dst = registry.add(_table_node(resolved["TABLE_OWNER"], resolved["TABLE_NAME"]), "TABLE")
                    edges.append({"SRC": src, "DST": dst, "EDGE_TYPE": "REF_TABLE",
                                 "EDGE_CONFIDENCE": "HIGH", "IS_DBLINK": "N"})
            # 그 외 타입(INDEX 등)은 분석 범위 밖 — 무시
    return edges


def _build_dynamic_sql_edges(sql_inv: pd.DataFrame, dynsql_hints: pd.DataFrame,
                             registry: NodeRegistry) -> list[dict]:
    edges: list[dict] = []
    if not sql_inv.empty:
        literal_rows = sql_inv[(sql_inv["IS_DYNAMIC"] == "Y") & (sql_inv["DYNAMIC_KIND"] == "LITERAL")]
        for _, row in literal_rows.iterrows():
            tables = [t for t in clean_str(row.get("TABLES_RAW")).split(";") if t]
            for t in tables:
                owner, _, name = t.rpartition(".")
                owner = owner or row["SP_ID"].split(".")[0]
                dst = registry.add(_table_node(owner, name), "TABLE")
                edges.append({"SRC": row["SP_ID"], "DST": dst, "EDGE_TYPE": "DYNAMIC_SQL_LITERAL",
                             "EDGE_CONFIDENCE": "MEDIUM", "IS_DBLINK": "N"})

    if not dynsql_hints.empty:
        for _, row in dynsql_hints.iterrows():
            resolved = clean_str(row.get("RESOLVED_TABLES"))
            if resolved:
                for t in resolved.split(";"):
                    if not t:
                        continue
                    owner, _, name = t.rpartition(".")
                    owner = owner or row["SP_ID"].split(".")[0]
                    dst = registry.add(_table_node(owner, name), "TABLE")
                    edges.append({"SRC": row["SP_ID"], "DST": dst, "EDGE_TYPE": "DYNAMIC_SQL_RESOLVED",
                                 "EDGE_CONFIDENCE": "MEDIUM", "IS_DBLINK": "N"})
            else:
                dst = registry.add(_unknown_node(f"DYNAMIC:{row['SP_ID']}:{row['LINE']}"), "UNKNOWN")
                edges.append({"SRC": row["SP_ID"], "DST": dst, "EDGE_TYPE": "DYNAMIC_SQL_VARIABLE",
                             "EDGE_CONFIDENCE": "LOW", "IS_DBLINK": "N"})
    return edges


NAME_KEY_RE = re.compile(r"#\d+$")


def _base_name_lookup(inventory: pd.DataFrame) -> dict[str, list[str]]:
    """앱 코드가 쓰는 정규화 이름(오버로드 접미 `#N` 제외) → SP_ID 매핑.

    `OWNER.NAME` (평범한 SP) 와 `OWNER.PKG.NAME` (패키지 서브프로그램) 모두
    앱에서 그대로 호출 문자열로 쓰이므로, `OWNER.PKG` 만으로는 매칭이 안 된다.
    """
    lookup: dict[str, list[str]] = {}
    for sp_id in inventory["SP_ID"]:
        base = NAME_KEY_RE.sub("", sp_id).upper()
        lookup.setdefault(base, []).append(sp_id)
    return lookup


def _build_app_call_edges(app_calls: pd.DataFrame, inventory: pd.DataFrame, registry: NodeRegistry) -> list[dict]:
    edges: list[dict] = []
    if app_calls.empty:
        return edges

    name_lookup = _base_name_lookup(inventory)

    for _, row in app_calls.iterrows():
        repo_node = registry.add(_app_node(row["REPO"]), "APP")
        name = clean_str(row.get("SP_NAME_RESOLVED")) or clean_str(row.get("SP_NAME_RAW"))
        confidence = str(row.get("CONFIDENCE", "MEDIUM")).upper() or "MEDIUM"
        matches = name_lookup.get(name.upper(), [])
        if matches:
            for sp_id in matches:
                edges.append({"SRC": repo_node, "DST": sp_id, "EDGE_TYPE": "APP_CALL",
                             "EDGE_CONFIDENCE": confidence, "IS_DBLINK": "N"})
        else:
            dst = registry.add(_unknown_node(f"APP_CALL:{name or 'UNKNOWN'}"), "UNKNOWN")
            edges.append({"SRC": repo_node, "DST": dst, "EDGE_TYPE": "APP_CALL_UNRESOLVED",
                         "EDGE_CONFIDENCE": "LOW", "IS_DBLINK": "N"})
    return edges


def _build_job_edges(jobs: pd.DataFrame, bodies_dir: Path, by_object: dict, registry: NodeRegistry) -> list[dict]:
    edges: list[dict] = []
    if jobs.empty:
        return edges
    name_re_cache: dict[str, re.Pattern] = {}

    for _, row in jobs.iterrows():
        job_node = registry.add(_job_node(row["OWNER"], row["JOB_NAME"]), "JOB")
        action_file = clean_str(row.get("ACTION_FILE"))
        if not action_file:
            continue
        candidate = bodies_dir.parent / action_file  # ACTION_FILE 은 input/ 기준 상대경로 (§2.5)
        if not candidate.exists():
            continue
        text = candidate.read_text(encoding="utf-8", errors="ignore").upper()
        for (owner, target), sp_ids in by_object.items():
            key = f"{owner}.{target}".upper()
            pat = name_re_cache.setdefault(key, re.compile(rf"\b{re.escape(key)}\b|\b{re.escape(target)}\b"))
            if pat.search(text):
                for sp_id in sp_ids:
                    edges.append({"SRC": job_node, "DST": sp_id, "EDGE_TYPE": "JOB_CALL",
                                 "EDGE_CONFIDENCE": "HIGH", "IS_DBLINK": "N"})
    return edges


def _apply_grant_validation(edges_df: pd.DataFrame, node_types: dict[str, str],
                            grant_matrix: pd.DataFrame, by_id: dict[str, dict]) -> pd.DataFrame:
    if edges_df.empty:
        edges_df["GRANT_OK"] = pd.Series(dtype=str)
        return edges_df

    exec_grants = set()
    if not grant_matrix.empty:
        exec_rows = grant_matrix[grant_matrix["PRIVILEGE"] == "EXECUTE"]
        exec_grants = set(zip(exec_rows["GRANTEE"], exec_rows["OWNER"], exec_rows["TABLE_NAME"]))

    def check(row) -> str:
        if node_types.get(row["SRC"]) not in ("SP", "TRIGGER") or node_types.get(row["DST"]) != "SP":
            return ""
        src_owner = row["SRC"].split(".")[0]
        dst_row = by_id.get(row["DST"])
        if dst_row is not None:
            dst_owner, dst_obj = dst_row["OWNER"], (clean_str(dst_row["PKG"]) or dst_row["NAME"])
        else:
            dst_owner, _, dst_obj = row["DST"].partition(".")
        if src_owner == dst_owner:
            return "Y"
        if (src_owner, dst_owner, dst_obj) in exec_grants or ("PUBLIC", dst_owner, dst_obj) in exec_grants:
            return "Y"
        return "N"

    edges_df["GRANT_OK"] = edges_df.apply(check, axis=1)
    return edges_df


def _call_subgraph(edges_df: pd.DataFrame, node_types: dict[str, str]) -> nx.DiGraph:
    g = nx.DiGraph()
    for node, ntype in node_types.items():
        if ntype in ("SP", "TRIGGER"):
            g.add_node(node)
    if not edges_df.empty:
        for _, row in edges_df.iterrows():
            if node_types.get(row["SRC"]) in ("SP", "TRIGGER") and node_types.get(row["DST"]) in ("SP", "TRIGGER"):
                g.add_edge(row["SRC"], row["DST"])
    return g


def _full_undirected_graph(edges_df: pd.DataFrame, node_types: dict[str, str]) -> nx.Graph:
    g = nx.Graph()
    g.add_nodes_from(node_types.keys())
    if not edges_df.empty:
        for _, row in edges_df.iterrows():
            g.add_edge(row["SRC"], row["DST"])
    return g


def _detect_communities(graph: nx.Graph, config: Config, logger: logging.Logger) -> dict[str, int]:
    algo = config.graph.community_algorithm
    if algo == "leiden":
        try:
            import igraph as ig
            import leidenalg
        except ImportError:
            logger.warning("leiden 알고리즘 optional dep(igraph/leidenalg) 미설치 — louvain 으로 폴백")
            algo = "louvain"

    cluster_of: dict[str, int] = {}
    if graph.number_of_nodes() == 0:
        return cluster_of

    if algo == "leiden":
        idx_to_node = list(graph.nodes())
        node_to_idx = {n: i for i, n in enumerate(idx_to_node)}
        ig_graph = ig.Graph(n=len(idx_to_node), edges=[(node_to_idx[a], node_to_idx[b]) for a, b in graph.edges()])
        partition = leidenalg.find_partition(ig_graph, leidenalg.ModularityVertexPartition,
                                             seed=config.graph.random_seed)
        for cluster_id, members in enumerate(partition):
            for idx in members:
                cluster_of[idx_to_node[idx]] = cluster_id
    else:
        communities = nx.community.louvain_communities(graph, seed=config.graph.random_seed)
        for cluster_id, members in enumerate(communities):
            for node in members:
                cluster_of[node] = cluster_id
    return cluster_of


def _apply_edge_overrides(edges_df: pd.DataFrame, override_dir: Path, logger: logging.Logger) -> pd.DataFrame:
    p = override_dir / OVERRIDE_SCHEMAS["s3_edges"].filename
    if not p.exists():
        return edges_df
    ov = read_csv(p)
    if ov.empty:
        return edges_df

    for _, row in ov.iterrows():
        src, dst, action = row["SRC"], row["DST"], str(row["ACTION"]).upper()
        mask = (edges_df["SRC"] == src) & (edges_df["DST"] == dst)
        if action == "ADD":
            if not mask.any():
                edges_df = pd.concat([edges_df, pd.DataFrame([{
                    "SRC": src, "DST": dst, "EDGE_TYPE": "OVERRIDE", "EDGE_CONFIDENCE": "HIGH",
                    "IS_DBLINK": "N", "GRANT_OK": "", "CUT_POINT": "N",
                }])], ignore_index=True)
        elif action == "REMOVE":
            edges_df = edges_df[~mask]
        elif action == "CONFIRM":
            edges_df.loc[mask, "EDGE_CONFIDENCE"] = "HIGH"
        elif action == "DOWNGRADE":
            edges_df.loc[mask, "EDGE_CONFIDENCE"] = "LOW"
        logger.info("s3_edges_override 적용: %s %s->%s (%s)", action, src, dst, row.get("REASON", ""))
    return edges_df.reset_index(drop=True)


def _apply_cluster_overrides(nodes_df: pd.DataFrame, override_dir: Path, logger: logging.Logger) -> pd.DataFrame:
    p = override_dir / OVERRIDE_SCHEMAS["s3_cluster"].filename
    if not p.exists():
        return nodes_df
    ov = read_csv(p)
    if ov.empty:
        return nodes_df
    nodes_df = nodes_df.set_index("NODE_ID")
    for _, row in ov.iterrows():
        if row["SP_ID"] in nodes_df.index:
            nodes_df.loc[row["SP_ID"], "CLUSTER_ID"] = row["CLUSTER_ID"]
            logger.info("s3_cluster_override 적용: %s -> cluster %s (%s)",
                       row["SP_ID"], row["CLUSTER_ID"], row.get("REASON", ""))
    return nodes_df.reset_index()


def run(paths: ProjectPaths, config: Config, logger: logging.Logger) -> dict:
    logger.info("S3 시작")
    s1_out = paths.stage_output("s1_inventory")
    s2_out = paths.stage_output("s2_metrics")

    inventory = read_csv(s1_out / "s1_inventory.csv")
    grant_matrix = read_csv(s1_out / "s1_grant_matrix.csv")
    deps = read_csv(paths.input_dir / "in_dependencies.csv")
    synonyms = read_csv(paths.input_dir / "in_synonyms.csv")
    app_calls = read_csv(paths.input_dir / "in_app_calls.csv")
    jobs = read_csv(paths.input_dir / "in_scheduler_jobs.csv")
    sql_inv = read_csv(s2_out / "s2_sql_inventory.csv")
    dynsql_hints = read_csv(s2_out / "s2_dynsql_hints.csv")

    if inventory.empty:
        raise RuntimeError("s1_inventory.csv missing/empty — run stage s1 first")

    registry = NodeRegistry()
    for _, row in inventory.iterrows():
        registry.add(row["SP_ID"], "TRIGGER" if row["TYPE"] == "TRIGGER" else "SP")

    by_object, by_id = _sp_index(inventory)
    synonym_maps = _resolve_synonyms(synonyms)

    edges: list[dict] = []
    edges += _build_dependency_edges(deps, inventory, by_object, synonym_maps, registry)
    edges += _build_dynamic_sql_edges(sql_inv, dynsql_hints, registry)
    edges += _build_app_call_edges(app_calls, inventory, registry)
    edges += _build_job_edges(jobs, paths.bodies_dir, by_object, registry)

    edges_df = pd.DataFrame(edges, columns=["SRC", "DST", "EDGE_TYPE", "EDGE_CONFIDENCE", "IS_DBLINK"])
    if not edges_df.empty:
        edges_df = edges_df.drop_duplicates(subset=["SRC", "DST", "EDGE_TYPE"]).reset_index(drop=True)
    edges_df["CUT_POINT"] = edges_df["IS_DBLINK"] if not edges_df.empty else pd.Series(dtype=str)

    edges_df = _apply_grant_validation(edges_df, registry.types, grant_matrix, by_id)
    edges_df = _apply_edge_overrides(edges_df, paths.override_dir, logger)
    if not edges_df.empty:
        edges_df["CUT_POINT"] = edges_df["IS_DBLINK"]

    call_graph = _call_subgraph(edges_df, registry.types)
    full_graph = _full_undirected_graph(edges_df, registry.types)

    scc_members = [c for c in nx.strongly_connected_components(call_graph) if len(c) > 1]
    scc_of: dict[str, int] = {}
    for scc_id, members in enumerate(scc_members):
        for node in members:
            scc_of[node] = scc_id

    condensation = nx.condensation(call_graph)
    wave_of_super: dict[int, int] = {}
    for wave_no, generation in enumerate(nx.topological_generations(condensation)):
        for super_node in generation:
            wave_of_super[super_node] = wave_no
    wave_of: dict[str, int] = {}
    for super_node, members in condensation.nodes(data="members"):
        for node in members:
            wave_of[node] = wave_of_super[super_node]

    fan_in = dict(call_graph.in_degree())
    fan_out = dict(call_graph.out_degree())
    betweenness = nx.betweenness_centrality(call_graph) if call_graph.number_of_nodes() > 0 else {}

    cluster_of = _detect_communities(full_graph, config, logger)

    node_rows = []
    for node_id, node_type in registry.types.items():
        node_rows.append({
            "NODE_ID": node_id, "NODE_TYPE": node_type,
            "CLUSTER_ID": cluster_of.get(node_id, -1),
            "FAN_IN": fan_in.get(node_id, 0), "FAN_OUT": fan_out.get(node_id, 0),
            "BETWEENNESS": round(betweenness.get(node_id, 0.0), 6),
            "WAVE_NO": wave_of.get(node_id, -1) if node_type in ("SP", "TRIGGER") else -1,
            "SCC_ID": scc_of.get(node_id, -1),
        })
    nodes_df = pd.DataFrame(node_rows, columns=["NODE_ID", "NODE_TYPE", "CLUSTER_ID", "FAN_IN", "FAN_OUT",
                                                "BETWEENNESS", "WAVE_NO", "SCC_ID"])
    nodes_df = _apply_cluster_overrides(nodes_df, paths.override_dir, logger)

    # SCC 멤버는 묶음 내 최고 WAVE_NO(가장 늦은 wave)로 통일 — 단일 작업 단위 승격
    if scc_of:
        scc_max_wave = {}
        for node, scc_id in scc_of.items():
            scc_max_wave[scc_id] = max(scc_max_wave.get(scc_id, -1), wave_of.get(node, -1))
        nodes_df["WAVE_NO"] = nodes_df.apply(
            lambda r: scc_max_wave[scc_of[r["NODE_ID"]]] if r["NODE_ID"] in scc_of else r["WAVE_NO"], axis=1)

    scc_rows = [{"SCC_ID": scc_id, "MEMBER_SP_ID": node} for node, scc_id in scc_of.items()]
    scc_df = pd.DataFrame(scc_rows, columns=["SCC_ID", "MEMBER_SP_ID"])

    waves_df = nodes_df[nodes_df["NODE_TYPE"].isin(["SP", "TRIGGER"])][["NODE_ID", "WAVE_NO"]].rename(
        columns={"NODE_ID": "SP_ID"})

    review_rows = _build_review(edges_df, nodes_df, scc_members)

    mmd = _to_mermaid(edges_df, registry.types)

    out = paths.stage_output("s3_graph")
    write_csv(nodes_df, out / "s3_nodes.csv")
    write_csv(edges_df, out / "s3_edges.csv")
    write_csv(scc_df, out / "s3_scc.csv")
    write_csv(waves_df, out / "s3_waves.csv")
    write_csv(review_rows, out / "s3_review.csv")
    (out / "s3_graph.mmd").write_text(mmd, encoding="utf-8")

    logger.info("S3 완료 — nodes=%d edges=%d scc=%d(members) review=%d",
               len(nodes_df), len(edges_df), len(scc_of), len(review_rows))
    if not review_rows.empty:
        logger.warning("검토 필수: s3_review.csv (%d rows)", len(review_rows))

    return {"nodes": len(nodes_df), "edges": len(edges_df), "scc_members": len(scc_of)}


def _build_review(edges_df: pd.DataFrame, nodes_df: pd.DataFrame, scc_members: list[set]) -> pd.DataFrame:
    rows: list[dict] = []
    if not edges_df.empty:
        low_conf = edges_df[edges_df["EDGE_CONFIDENCE"] == "LOW"]
        for _, r in low_conf.iterrows():
            rows.append({"REASON_CODE": "LOW_CONFIDENCE_EDGE", "SUBJECT": f"{r['SRC']}->{r['DST']}",
                        "DETAIL": r["EDGE_TYPE"]})
        missing_grant = edges_df[edges_df["GRANT_OK"] == "N"]
        for _, r in missing_grant.iterrows():
            rows.append({"REASON_CODE": "GRANT_MISSING", "SUBJECT": f"{r['SRC']}->{r['DST']}", "DETAIL": ""})

    connected = set()
    if not edges_df.empty:
        connected = set(edges_df["SRC"]) | set(edges_df["DST"])
    for _, n in nodes_df.iterrows():
        if n["NODE_TYPE"] in ("SP", "TRIGGER") and n["NODE_ID"] not in connected:
            rows.append({"REASON_CODE": "ORPHAN_NODE", "SUBJECT": n["NODE_ID"], "DETAIL": ""})

    for members in scc_members:
        if len(members) > LARGE_SCC_THRESHOLD:
            rows.append({"REASON_CODE": "LARGE_SCC", "SUBJECT": ";".join(sorted(members)),
                        "DETAIL": f"size={len(members)}"})

    return pd.DataFrame(rows, columns=["REASON_CODE", "SUBJECT", "DETAIL"])


def _to_mermaid(edges_df: pd.DataFrame, node_types: dict[str, str]) -> str:
    lines = ["graph LR"]
    style_map = {"SP": "", "TRIGGER": ":::trigger", "TABLE": ":::table",
                "REMOTE": ":::remote", "APP": ":::app", "JOB": ":::job", "UNKNOWN": ":::unknown"}

    def sanitize(node: str) -> str:
        return re.sub(r"[^A-Za-z0-9_]", "_", node)

    if edges_df.empty:
        return "\n".join(lines)
    for _, row in edges_df.iterrows():
        src_style = style_map.get(node_types.get(row["SRC"], "SP"), "")
        dst_style = style_map.get(node_types.get(row["DST"], "SP"), "")
        lines.append(f'    {sanitize(row["SRC"])}["{row["SRC"]}"]{src_style} '
                    f'-->|{row["EDGE_TYPE"]}| {sanitize(row["DST"])}["{row["DST"]}"]{dst_style}')
    return "\n".join(lines)
