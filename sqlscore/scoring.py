"""점수화 — 참조 가산점수(의존성), 복잡도, 코드분량 → 절대점수 및 난이도 밴드.

절대점수 = w_volume·(LOC/loc_divisor)
         + w_complexity·(분기·루프·쿼리중첩·DML·동적SQL·예외·서브프로그램 가중합)
         + w_dependency·(참조 종류별 가산점 합)

모든 성분을 breakdown 에 항목 단위로 남겨 리포트에서 근거를 그대로 제시한다.
"""
from __future__ import annotations

from sqlscore.config import ScoreConfig
from sqlscore.model import Metrics, Reference, Score, SourceObject


def dependency_score(refs: list[Reference], config: ScoreConfig) -> tuple[float, dict]:
    """참조 종류별 가산점수 합 + 종류별 소계."""
    by_type: dict[str, float] = {}
    counts: dict[str, int] = {}
    for r in refs:
        w = config.ref_weights.get(r.rtype, 1.0)
        by_type[r.rtype] = by_type.get(r.rtype, 0.0) + w * r.count
        counts[r.rtype] = counts.get(r.rtype, 0) + r.count
    total = sum(by_type.values())
    return total, {"by_type": by_type, "counts": counts}


def complexity_score(m: Metrics, config: ScoreConfig) -> tuple[float, dict]:
    cw = config.complexity
    items = {
        "branch": cw.branch * m.branch_count,
        "loop": cw.loop * m.loop_count,
        "query": cw.query * m.query_count,
        "query_nesting": cw.query_nesting * m.max_query_nesting,
        "dml": cw.dml * m.dml_count,
        "dynamic_sql": cw.dynamic_sql * m.dynamic_sql_count,
        "exception_handler": cw.exception_handler * m.exception_handler_count,
        "when_others": cw.when_others * m.when_others,
        "subprogram": cw.subprogram * m.subprogram_count,
    }
    return sum(items.values()), items


def score_object(obj: SourceObject, metrics: Metrics, refs: list[Reference],
                 config: ScoreConfig) -> Score:
    vol_raw = metrics.loc / config.loc_divisor
    cx_raw, cx_items = complexity_score(metrics, config)
    dep_raw, dep_detail = dependency_score(refs, config)

    cw = config.composite
    volume = cw.volume * vol_raw
    complexity = cw.complexity * cx_raw
    dependency = cw.dependency * dep_raw
    absolute = volume + complexity + dependency

    return Score(
        key=obj.key,
        volume_score=round(volume, 2),
        complexity_score=round(complexity, 2),
        dependency_score=round(dependency, 2),
        absolute_score=round(absolute, 2),
        band=config.band_for(absolute),
        breakdown={
            "volume": {"loc": metrics.loc, "loc_divisor": config.loc_divisor, "raw": round(vol_raw, 2)},
            "complexity": {"items": {k: round(v, 2) for k, v in cx_items.items() if v}, "raw": round(cx_raw, 2)},
            "dependency": {"by_type": {k: round(v, 2) for k, v in dep_detail["by_type"].items()},
                           "counts": dep_detail["counts"], "raw": round(dep_raw, 2)},
        },
    )


def _impact_scores(objects: list[SourceObject], refs: list[Reference],
                   config: ScoreConfig) -> dict[str, dict]:
    """fan-in 기반 영향도(파급도) 산정.

    영향도 = 들어오는 CALL·PACKAGE 참조(다른 프로그램이 나를 의존) + GRANT 외부노출.
    참조는 (src,target,rtype) 단위로 이미 집계되어 있어 ref 1건 = 서로 다른 호출자 1개로 본다.
    """
    obj_by_key = {o.key: o for o in objects}
    iw = config.impact
    detail: dict[str, dict] = {
        o.key: {"call_in": 0, "package_in": 0, "grant_exposure": 0, "callers": set()}
        for o in objects if o.is_program
    }
    for r in refs:
        if (r.rtype in ("CALL", "PACKAGE") and r.resolved and r.target_key in detail
                and obj_by_key.get(r.target_key) and obj_by_key[r.target_key].is_program):
            d = detail[r.target_key]
            d["call_in" if r.rtype == "CALL" else "package_in"] += 1
            d["callers"].add(r.src_key)
        elif r.rtype == "GRANT" and r.src_key in detail:
            detail[r.src_key]["grant_exposure"] += r.count

    out: dict[str, dict] = {}
    for key, d in detail.items():
        raw = (iw.call_in * d["call_in"] + iw.package_in * d["package_in"]
               + iw.grant_exposure * d["grant_exposure"])
        out[key] = {**d, "callers": sorted(d["callers"]), "raw": round(raw, 2)}
    return out


def _normalize(values: dict[str, float], method: str) -> dict[str, float]:
    """값 사전 → 0~100 정규화.

    rank(기본): 순위 정규화 = (자신보다 작은 값 수)/(n-1)·100. 최소=0, 최대=100.
                동률(특히 영향도의 다수 0)은 같은 하한값을 받아 0 은 0 으로 매핑됨(이상치에 강건).
    minmax    : (v-min)/(max-min)·100. 크기 보존이지만 단일 이상치에 민감.
    전 항목 동일값이면 0(모두 0일 때) 또는 50.
    """
    n = len(values)
    if n == 0:
        return {}
    lo, hi = min(values.values()), max(values.values())
    if hi == lo:
        base = 0.0 if hi == 0 else 50.0
        return {k: base for k in values}
    if method == "minmax":
        return {k: (v - lo) / (hi - lo) * 100.0 for k, v in values.items()}
    vals = list(values.values())
    return {k: sum(1 for x in vals if x < v) / (n - 1) * 100.0 for k, v in values.items()}


def score_all(objects: list[SourceObject], metrics_map: dict[str, Metrics],
              refs: list[Reference], config: ScoreConfig) -> dict[str, Score]:
    refs_by_src: dict[str, list[Reference]] = {}
    for r in refs:
        refs_by_src.setdefault(r.src_key, []).append(r)

    scores: dict[str, Score] = {}
    for o in objects:
        if not o.is_program:
            continue
        m = metrics_map.get(o.key, Metrics())
        scores[o.key] = score_object(o, m, refs_by_src.get(o.key, []), config)

    # 영향도(fan-in)
    impact = _impact_scores(objects, refs, config)
    for key, s in scores.items():
        s.impact_score = impact[key]["raw"]
        s.breakdown["impact"] = impact[key]

    # 최종 단일점수 = 정규화한 전환난이도·영향도의 결합 (0~100)
    fcfg = config.final
    conv_norm = _normalize({k: s.absolute_score for k, s in scores.items()}, fcfg.normalize)
    imp_norm = _normalize({k: s.impact_score for k, s in scores.items()}, fcfg.normalize)
    tw = (fcfg.w_conversion + fcfg.w_impact) or 1.0
    for key, s in scores.items():
        cn, im = conv_norm[key], imp_norm[key]
        if fcfg.method == "geometric":
            final = (cn * im) ** 0.5
        else:  # weighted_sum
            final = (fcfg.w_conversion * cn + fcfg.w_impact * im) / tw
        s.final_score = round(final, 1)
        s.breakdown["final"] = {
            "conv_norm": round(cn, 1), "imp_norm": round(im, 1),
            "method": fcfg.method, "normalize": fcfg.normalize,
            "w_conversion": fcfg.w_conversion, "w_impact": fcfg.w_impact,
        }
    return scores
