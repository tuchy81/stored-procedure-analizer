"""S4 — 점수화 및 분류.

캘리브레이션(Ridge/VIF/부트스트랩)은 scikit-learn/numpy optional dep
(pyproject `[scoring]`)가 있을 때만 수행하고, 없으면 휴리스틱 공수만
산출한다 — S2 파서 계층과 동일한 "가용하면 사용, 없으면 폴백" 설계.
"""
from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd

from sp_assessor.core.config import Config
from sp_assessor.core.paths import ProjectPaths
from sp_assessor.io.csv_io import read_csv, write_csv
from sp_assessor.io.csv_schemas import OVERRIDE_SCHEMAS

LARGE_SCC_THRESHOLD = 3

DEPENDENCY_METRIC_COLS = ["REF_TABLE", "FANOUT_SP", "FANIN_SP", "FANIN_APP",
                         "DB_LINK", "TRIGGER_CALLER", "SCHEDULER_CALLER", "CROSS_SCHEMA_GRANT"]
COMPLEXITY_METRIC_COLS = ["LOC_PER_100", "BRANCH", "CURSOR", "REF_CURSOR_OUT",
                         "DYNAMIC_SQL_LITERAL", "DYNAMIC_SQL_VARIABLE", "TX_CONTROL",
                         "AUTONOMOUS_TX", "ORACLE_FEATURE", "OUT_PARAM",
                         "EXCEPTION_HANDLER", "GLOBAL_PKG_VAR", "MUTATING_TRIGGER_RISK"]

STRATEGY_RANK = {"AUTO_SIMPLE": 0, "AUTO_ASSISTED": 1, "SEMI": 2, "MANUAL": 3, "DEFER": 4}
EFFORT_BASE_MD = {"AUTO_SIMPLE": 0.5, "AUTO_ASSISTED": 1.5, "SEMI": 3.0, "MANUAL": 5.0, "DEFER": 8.0}


def _build_dependency_features(sp_ids: list[str], edges: pd.DataFrame, node_types: dict[str, str],
                               inventory: pd.DataFrame) -> pd.DataFrame:
    df = pd.DataFrame({"SP_ID": sp_ids})
    for col in DEPENDENCY_METRIC_COLS:
        df[col] = 0

    if not edges.empty:
        out_by_src = edges.groupby("SRC")
        in_by_dst = edges.groupby("DST")

        def count_out(sp_id: str, edge_types: set[str]) -> int:
            if sp_id not in out_by_src.groups:
                return 0
            grp = out_by_src.get_group(sp_id)
            return int(grp["EDGE_TYPE"].isin(edge_types).sum())

        def count_in(sp_id: str, edge_types: set[str], src_type: str | None = None) -> int:
            if sp_id not in in_by_dst.groups:
                return 0
            grp = in_by_dst.get_group(sp_id)
            mask = grp["EDGE_TYPE"].isin(edge_types)
            if src_type is not None:
                mask &= grp["SRC"].map(lambda s: node_types.get(s)) == src_type
            return int(mask.sum())

        ref_table_types = {"REF_TABLE", "DYNAMIC_SQL_LITERAL", "DYNAMIC_SQL_RESOLVED"}
        df["REF_TABLE"] = df["SP_ID"].apply(lambda s: count_out(s, ref_table_types))
        df["FANOUT_SP"] = df["SP_ID"].apply(lambda s: count_out(s, {"CALL"}))
        df["FANIN_SP"] = df["SP_ID"].apply(lambda s: count_in(s, {"CALL"}, src_type="SP"))
        df["FANIN_APP"] = df["SP_ID"].apply(lambda s: count_in(s, {"APP_CALL"}, src_type="APP"))
        df["DB_LINK"] = df["SP_ID"].apply(lambda s: count_out(s, {"REMOTE_REF"}))
        df["TRIGGER_CALLER"] = df["SP_ID"].apply(lambda s: count_in(s, {"CALL"}, src_type="TRIGGER"))
        df["SCHEDULER_CALLER"] = df["SP_ID"].apply(lambda s: count_in(s, {"JOB_CALL"}, src_type="JOB"))

    cross_schema = dict(zip(inventory["SP_ID"], inventory["CROSS_SCHEMA_CALLABLE"]))
    df["CROSS_SCHEMA_GRANT"] = df["SP_ID"].map(lambda s: 1 if cross_schema.get(s) == "Y" else 0)
    return df


def _build_complexity_features(metrics: pd.DataFrame) -> pd.DataFrame:
    df = pd.DataFrame({"SP_ID": metrics["SP_ID"]})
    df["LOC_PER_100"] = metrics["LOC"] / 100.0
    df["BRANCH"] = metrics["BRANCH_COUNT"]
    df["CURSOR"] = metrics["CURSOR_EXPLICIT_COUNT"] + metrics["CURSOR_FOR_LOOP_COUNT"] + metrics["REFCURSOR_COUNT"]
    df["REF_CURSOR_OUT"] = metrics["REF_CURSOR_OUT_COUNT"]
    df["DYNAMIC_SQL_LITERAL"] = metrics["DYNAMIC_SQL_LITERAL_COUNT"]
    df["DYNAMIC_SQL_VARIABLE"] = metrics["DYNAMIC_SQL_VARIABLE_COUNT"]
    df["TX_CONTROL"] = metrics["TX_CONTROL_COUNT"]
    df["AUTONOMOUS_TX"] = metrics["AUTONOMOUS_TX_FLAG"]
    df["ORACLE_FEATURE"] = metrics["ORACLE_FEATURE_COUNT"]
    df["OUT_PARAM"] = metrics["OUT_PARAM_COUNT"]
    df["EXCEPTION_HANDLER"] = metrics["EXCEPTION_HANDLER_COUNT"]
    df["GLOBAL_PKG_VAR"] = metrics["GLOBAL_PKG_VAR_REF_COUNT"]
    df["MUTATING_TRIGGER_RISK"] = metrics["MUTATING_TRIGGER_RISK"]
    return df


def _percentile_rank(series: pd.Series) -> pd.Series:
    if series.nunique() <= 1:
        return pd.Series(50.0, index=series.index)
    return series.rank(pct=True) * 100.0


def _weighted_composite(features: pd.DataFrame, cols: list[str], weights: dict[str, float]) -> pd.Series:
    percentiles = pd.DataFrame({c: _percentile_rank(features[c]) for c in cols})
    total_weight = sum(weights.values()) or 1.0
    composite = sum(percentiles[c] * weights[c.lower()] for c in cols) / total_weight
    return composite


def _classify_strategy(row, config: Config) -> str:
    rules = config.strategy_rules
    cursor_total = row["CURSOR"]
    dynamic_total = row["DYNAMIC_SQL_LITERAL"] + row["DYNAMIC_SQL_VARIABLE"]

    if (row["AUTONOMOUS_TX"] >= 1 or row["DB_LINK"] >= 2 or row["MUTATING_TRIGGER_RISK"] >= 1 or
            (row["D_SCORE"] >= config.quadrant.d_threshold_pct and row["C_SCORE"] >= config.quadrant.c_threshold_pct)):
        return "DEFER"

    simple = rules.auto_simple
    if (row["LOC"] <= (simple.max_loc or 0) and row["BRANCH"] <= (simple.max_branch or 0) and
            cursor_total <= (simple.max_cursor or 0) and dynamic_total == 0 and row["DB_LINK"] == 0):
        return "AUTO_SIMPLE"

    assisted = rules.auto_assisted
    if (row["LOC"] <= (assisted.max_loc or 0) and row["BRANCH"] <= (assisted.max_branch or 0) and
            cursor_total <= (assisted.max_cursor or 0) and
            row["DYNAMIC_SQL_LITERAL"] <= (assisted.dynamic_sql_literal or 0) and
            row["DYNAMIC_SQL_VARIABLE"] == 0 and row["DB_LINK"] == 0):
        return "AUTO_ASSISTED"

    if cursor_total > 0:
        return "SEMI"

    return "MANUAL"


def _classify_quadrant(d_score: float, c_score: float, config: Config) -> str:
    high_d = d_score >= config.quadrant.d_threshold_pct
    high_c = c_score >= config.quadrant.c_threshold_pct
    if not high_d and not high_c:
        return "SIMPLE"
    if high_d and not high_c:
        return "COMPLEX_DEPENDENCY"
    if not high_d and high_c:
        return "COMPLEX_CODE"
    return "COMPLEX_BOTH"


def _is_boundary(d_score: float, c_score: float, config: Config) -> bool:
    band = config.quadrant.boundary_band_pct
    return (abs(d_score - config.quadrant.d_threshold_pct) <= band or
            abs(c_score - config.quadrant.c_threshold_pct) <= band)


def _promote_scc_bundles(scores: pd.DataFrame, scc: pd.DataFrame, logger: logging.Logger) -> tuple[pd.DataFrame, set]:
    if scc.empty:
        return scores, set()
    promoted: set[str] = set()
    scores = scores.set_index("SP_ID")
    for scc_id, grp in scc.groupby("SCC_ID"):
        members = list(grp["MEMBER_SP_ID"])
        present = [m for m in members if m in scores.index]
        if not present:
            continue
        best = max(present, key=lambda m: STRATEGY_RANK.get(scores.loc[m, "STRATEGY"], 0))
        best_strategy = scores.loc[best, "STRATEGY"]
        for m in present:
            if scores.loc[m, "STRATEGY"] != best_strategy:
                logger.info("SCC #%s 상향 통일: %s %s -> %s", scc_id, m, scores.loc[m, "STRATEGY"], best_strategy)
                scores.loc[m, "STRATEGY"] = best_strategy
                promoted.add(m)
    return scores.reset_index(), promoted


def _bootstrap_calibration(features: pd.DataFrame, pilot: pd.DataFrame, feature_cols: list[str],
                           config: Config, logger: logging.Logger) -> tuple[pd.Series, pd.Series, pd.Series, dict] | None:
    try:
        import numpy as np
        from sklearn.linear_model import Ridge, Lasso, LinearRegression
        from sklearn.preprocessing import StandardScaler
    except ImportError:
        logger.warning("scikit-learn/numpy 미설치 — 캘리브레이션 생략 (휴리스틱 공수만 산출)")
        return None

    merged = pilot.merge(features, on="SP_ID", how="inner")
    if len(merged) < config.calibration.min_samples:
        logger.info("캘리브레이션 표본 부족 (%d < %d) — 미실행", len(merged), config.calibration.min_samples)
        return None

    X_train = merged[feature_cols].to_numpy(dtype=float)
    y_train = merged["ACTUAL_MD"].to_numpy(dtype=float)
    scaler = StandardScaler().fit(X_train)
    X_train_s = scaler.transform(X_train)

    model_cls = {"ridge": Ridge, "lasso": Lasso, "ols": LinearRegression}.get(config.calibration.model, Ridge)
    model_kwargs = {"alpha": config.calibration.ridge_alpha} if model_cls in (Ridge, Lasso) else {}
    model = model_cls(**model_kwargs).fit(X_train_s, y_train)

    vif = {}
    for i, col in enumerate(feature_cols):
        others = [j for j in range(len(feature_cols)) if j != i]
        if not others:
            vif[col] = 1.0
            continue
        r2 = LinearRegression().fit(X_train_s[:, others], X_train_s[:, i]).score(X_train_s[:, others], X_train_s[:, i])
        vif[col] = float("inf") if r2 >= 0.999 else round(1.0 / (1.0 - r2), 3)

    rng = np.random.default_rng(config.graph.random_seed)
    n_boot = 200
    X_all = scaler.transform(features[feature_cols].to_numpy(dtype=float))
    boot_preds = np.zeros((n_boot, len(features)))
    n = len(merged)
    for b in range(n_boot):
        idx = rng.integers(0, n, size=n)
        boot_model = model_cls(**model_kwargs).fit(X_train_s[idx], y_train[idx])
        boot_preds[b] = boot_model.predict(X_all)

    point_pred = pd.Series(model.predict(X_all), index=features.index).clip(lower=0.1)
    p50 = pd.Series(pd.DataFrame(boot_preds).quantile(0.5).to_numpy(), index=features.index).clip(lower=0.1)
    p90 = pd.Series(pd.DataFrame(boot_preds).quantile(0.9).to_numpy(), index=features.index).clip(lower=0.1)

    weight_suggestion = {col: round(float(coef), 4) for col, coef in zip(feature_cols, model.coef_)}
    diagnostics = {"model": config.calibration.model, "samples": len(merged),
                  "vif": vif, "weight_suggestion": weight_suggestion}
    return point_pred, p50, p90, diagnostics


def _apply_strategy_override(scores: pd.DataFrame, override_dir: Path, logger: logging.Logger) -> pd.DataFrame:
    p = override_dir / OVERRIDE_SCHEMAS["s4_strategy"].filename
    if not p.exists():
        return scores
    ov = read_csv(p)
    if ov.empty:
        return scores
    scores = scores.set_index("SP_ID")
    for _, row in ov.iterrows():
        sp_id = row["SP_ID"]
        if sp_id not in scores.index:
            logger.warning("s4_strategy_override: 알 수 없는 SP_ID %s", sp_id)
            continue
        scores.loc[sp_id, "STRATEGY"] = row["STRATEGY"]
        if "QUADRANT" in ov.columns and pd.notna(row.get("QUADRANT")) and str(row.get("QUADRANT")).strip():
            scores.loc[sp_id, "QUADRANT"] = row["QUADRANT"]
        if "EFFORT_MD" in ov.columns and pd.notna(row.get("EFFORT_MD")) and str(row.get("EFFORT_MD")).strip():
            scores.loc[sp_id, "EFFORT_EST_MD"] = float(row["EFFORT_MD"])
        logger.info("s4_strategy_override 적용: %s -> %s (%s)", sp_id, row["STRATEGY"], row.get("REASON", ""))
    return scores.reset_index()


def run(paths: ProjectPaths, config: Config, logger: logging.Logger) -> dict:
    logger.info("S4 시작")
    s1_out = paths.stage_output("s1_inventory")
    s2_out = paths.stage_output("s2_metrics")
    s3_out = paths.stage_output("s3_graph")

    inventory = read_csv(s1_out / "s1_inventory.csv")
    metrics = read_csv(s2_out / "s2_metrics.csv")
    nodes = read_csv(s3_out / "s3_nodes.csv")
    edges = read_csv(s3_out / "s3_edges.csv")
    scc = read_csv(s3_out / "s3_scc.csv")

    if inventory.empty or metrics.empty:
        raise RuntimeError("s1_inventory.csv/s2_metrics.csv missing — run stages s1,s2,s3 first")

    node_types = dict(zip(nodes["NODE_ID"], nodes["NODE_TYPE"])) if not nodes.empty else {}
    sp_ids = list(metrics["SP_ID"])

    dep_features = _build_dependency_features(sp_ids, edges, node_types, inventory)
    cx_features = _build_complexity_features(metrics)
    features = dep_features.merge(cx_features, on="SP_ID")

    dep_weights = {k: getattr(config.weights.dependency, k) for k in
                  ("ref_table", "fanout_sp", "fanin_sp", "fanin_app", "db_link",
                   "trigger_caller", "scheduler_caller", "cross_schema_grant")}
    cx_weights = {k: getattr(config.weights.complexity, k) for k in
                 ("loc_per_100", "branch", "cursor", "ref_cursor_out", "dynamic_sql_literal",
                  "dynamic_sql_variable", "tx_control", "autonomous_tx", "oracle_feature",
                  "out_param", "exception_handler", "global_pkg_var", "mutating_trigger_risk")}

    d_score = _weighted_composite(features, DEPENDENCY_METRIC_COLS, dep_weights)
    c_score = _weighted_composite(features, COMPLEXITY_METRIC_COLS, cx_weights)

    scores = pd.DataFrame({"SP_ID": features["SP_ID"], "D_SCORE": d_score.round(2), "C_SCORE": c_score.round(2)})
    scores = scores.merge(features, on="SP_ID")
    scores = scores.merge(metrics[["SP_ID", "LOC", "PARSE_MODE"]], on="SP_ID")

    scores["QUADRANT"] = scores.apply(lambda r: _classify_quadrant(r["D_SCORE"], r["C_SCORE"], config), axis=1)
    scores["STRATEGY"] = scores.apply(lambda r: _classify_strategy(r, config), axis=1)
    scores["IS_BOUNDARY"] = scores.apply(lambda r: _is_boundary(r["D_SCORE"], r["C_SCORE"], config), axis=1)

    scores["EFFORT_EST_MD"] = (scores["STRATEGY"].map(EFFORT_BASE_MD) * (1 + scores["C_SCORE"] / 100.0)).round(2)
    scores["EFFORT_P50"] = scores["EFFORT_EST_MD"]
    scores["EFFORT_P90"] = (scores["EFFORT_EST_MD"] * 1.5).round(2)
    scores["CONFIDENCE"] = "LOW"

    pilot_path = paths.override_dir / OVERRIDE_SCHEMAS["pilot_effort"].filename
    pilot = read_csv(pilot_path)
    calibration_diagnostics = None
    all_metric_cols = DEPENDENCY_METRIC_COLS + COMPLEXITY_METRIC_COLS
    if not pilot.empty:
        result = _bootstrap_calibration(features, pilot, all_metric_cols, config, logger)
        if result is not None:
            point_pred, p50, p90, calibration_diagnostics = result
            scores["EFFORT_EST_MD"] = point_pred.round(2).values
            scores["EFFORT_P50"] = p50.round(2).values
            scores["EFFORT_P90"] = p90.round(2).values
            scores["CONFIDENCE"] = "CALIBRATED"
            logger.info("캘리브레이션 완료 (표본=%d, 모델=%s)", len(pilot), config.calibration.model)
    else:
        logger.info("캘리브레이션 미실행 — override/pilot_effort.csv 없음")

    scores, promoted = _promote_scc_bundles(scores, scc, logger)

    wave_cluster = nodes[nodes["NODE_TYPE"].isin(["SP", "TRIGGER"])][["NODE_ID", "WAVE_NO", "CLUSTER_ID"]].rename(
        columns={"NODE_ID": "SP_ID"}) if not nodes.empty else pd.DataFrame(columns=["SP_ID", "WAVE_NO", "CLUSTER_ID"])
    scores = scores.merge(wave_cluster, on="SP_ID", how="left")

    scores = _apply_strategy_override(scores, paths.override_dir, logger)

    out_cols = ["SP_ID", "D_SCORE", "C_SCORE", "QUADRANT", "STRATEGY", "WAVE_NO", "CLUSTER_ID",
               "EFFORT_EST_MD", "EFFORT_P50", "EFFORT_P90", "CONFIDENCE"]
    final_scores = scores[out_cols].sort_values("SP_ID").reset_index(drop=True)

    review_rows = []
    for _, r in scores.iterrows():
        if r.get("IS_BOUNDARY"):
            review_rows.append({"SP_ID": r["SP_ID"], "REASON_CODE": "BOUNDARY",
                               "DETAIL": f"D={r['D_SCORE']} C={r['C_SCORE']}"})
        if r["PARSE_MODE"] != "AST":
            review_rows.append({"SP_ID": r["SP_ID"], "REASON_CODE": "PARSE_MODE_NOT_AST",
                               "DETAIL": r["PARSE_MODE"]})
        if r["SP_ID"] in promoted:
            review_rows.append({"SP_ID": r["SP_ID"], "REASON_CODE": "SCC_STRATEGY_PROMOTED",
                               "DETAIL": r["STRATEGY"]})
        if r["STRATEGY"] == "DEFER":
            review_rows.append({"SP_ID": r["SP_ID"], "REASON_CODE": "DEFER_AUTO_CLASSIFIED", "DETAIL": ""})
    review_df = pd.DataFrame(review_rows, columns=["SP_ID", "REASON_CODE", "DETAIL"])

    out = paths.stage_output("s4_scoring")
    write_csv(final_scores, out / "s4_scores.csv")
    write_csv(review_df, out / "s4_review.csv")
    if calibration_diagnostics is not None:
        import yaml
        (out / "s4_weight_suggestion.yaml").write_text(
            yaml.safe_dump(calibration_diagnostics, allow_unicode=True, sort_keys=False), encoding="utf-8")

    strategy_counts = final_scores["STRATEGY"].value_counts().to_dict()
    logger.info("S4 완료 — scores=%d strategy=%s review=%d", len(final_scores), strategy_counts, len(review_df))
    if not review_df.empty:
        logger.warning("검토 필요: s4_review.csv (%d rows)", len(review_df))

    return {"scores": len(final_scores), "strategy_counts": strategy_counts}
