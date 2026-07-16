"""시각화 — matplotlib 가 있으면 점수 산포·공수 캘리브레이션(예측구간)·P50/P90 범위 차트를 PNG 로 생성.

이 저장소의 다른 optional 기능과 동일한 규약: matplotlib(numpy) 미설치 시 None 을 반환하고
리포트는 그래프 없이 정상 생성된다 (pip install -e ".[viz]" 로 활성화).
한글 폰트가 있으면 한글 라벨, 없으면 영문 라벨로 폴백해 글자 깨짐(두부)을 방지한다.
"""
from __future__ import annotations

import math
from pathlib import Path

from sqlscore.config import ScoreConfig
from sqlscore.model import EffortEstimate, Metrics, Reference, Score, SourceObject

# 밴드별 색(난이도 상승 = 초록→빨강)
_BAND_COLOR = {"낮음": "#4c9f70", "보통": "#e0b13a", "높음": "#e07b39", "매우높음": "#d1495b"}
_BAND_EN = {"낮음": "Low", "보통": "Mid", "높음": "High", "매우높음": "VeryHigh"}

# t 분포 0.90 분위(상단 0.10) — 80% 예측구간(P10~P90)용, df 별 근사표
_T90 = {1: 3.078, 2: 1.886, 3: 1.638, 4: 1.533, 5: 1.476, 6: 1.440, 7: 1.415,
        8: 1.397, 9: 1.383, 10: 1.372, 12: 1.356, 15: 1.341, 20: 1.325, 30: 1.310}


def _t90(df: int) -> float:
    if df <= 0 or df > 30:
        return 1.282
    for k in sorted(_T90):
        if df <= k:
            return _T90[k]
    return 1.282


def _setup_font() -> bool:
    from matplotlib import font_manager, rcParams
    rcParams["axes.unicode_minus"] = False
    for name in ("Malgun Gothic", "AppleGothic", "NanumGothic", "NanumBarunGothic",
                 "Noto Sans CJK KR", "Noto Sans KR"):
        try:
            path = font_manager.findfont(name, fallback_to_default=False)
        except Exception:
            path = None
        if path:
            rcParams["font.family"] = font_manager.FontProperties(fname=path).get_name()
            return True
    return False


def _feature_value(cfg, s: Score, m: Metrics) -> float:
    return {"absolute": s.absolute_score, "loc": float(m.loc),
            "complexity": s.complexity_score, "dependency": s.dependency_score}.get(
        cfg.feature, s.absolute_score)


def render_charts(out_dir: Path, objects: list[SourceObject], scores: dict[str, Score],
                  metrics_map: dict[str, Metrics], effort: dict[str, EffortEstimate] | None,
                  effort_summary: dict | None, config: ScoreConfig,
                  refs: list[Reference] | None = None) -> dict[str, str] | None:
    """차트 PNG 생성 → {name: 리포트기준 상대경로}. matplotlib 미설치 시 None."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt  # noqa: F401
        import numpy as np
    except Exception:
        return None

    kr = _setup_font()

    def L(ko: str, en: str) -> str:
        return ko if kr else en

    def band_label(b: str) -> str:
        return b if kr else _BAND_EN.get(b, b)

    charts_dir = out_dir / "charts"
    charts_dir.mkdir(parents=True, exist_ok=True)
    programs = [o for o in objects if o.is_program]
    result: dict[str, str] = {}

    _chart_scatter(charts_dir, programs, scores, np, L, band_label, result)
    if refs:
        _chart_dep_graph(charts_dir, objects, refs, scores, L, band_label, result)
    if effort and effort_summary:
        _chart_calibration(charts_dir, programs, scores, metrics_map, effort, effort_summary,
                           config.effort, np, L, result)
        _chart_ranges(charts_dir, programs, scores, effort, effort_summary, np, L, result)
    return result


def _chart_dep_graph(charts_dir, objects, refs, scores, L, band_label, result) -> None:
    """오브젝트 간 내부 호출/참조 방향 그래프(PNG). networkx 없으면 생략(→ mermaid 폴백)."""
    try:
        import networkx as nx
    except Exception:
        return
    import matplotlib.patches as mpatches
    import matplotlib.pyplot as plt

    obj_by_key = {o.key: o for o in objects}
    edges = [r for r in refs if r.rtype in ("CALL", "PACKAGE") and r.resolved
             and r.target_key in obj_by_key and obj_by_key[r.target_key].is_program]
    if not edges:
        return

    g = nx.DiGraph()
    for r in edges:
        g.add_edge(r.src_key, r.target_key, rtype=r.rtype)
    pos = nx.spring_layout(g, seed=42, k=1.2)

    fig, ax = plt.subplots(figsize=(9, 6.5))
    node_colors = [_BAND_COLOR.get(scores[k].band, "#888888") if k in scores else "#888888"
                   for k in g.nodes]
    node_sizes = [400 + (scores[k].final_score * 12 if k in scores else 0) for k in g.nodes]
    nx.draw_networkx_nodes(g, pos, ax=ax, node_color=node_colors, node_size=node_sizes,
                           edgecolors="white", linewidths=1.2)
    call_e = [(u, v) for u, v, d in g.edges(data=True) if d["rtype"] == "CALL"]
    pkg_e = [(u, v) for u, v, d in g.edges(data=True) if d["rtype"] == "PACKAGE"]
    nx.draw_networkx_edges(g, pos, ax=ax, edgelist=call_e, edge_color="#3366cc",
                           arrows=True, arrowsize=16, width=1.6, connectionstyle="arc3,rad=0.06")
    nx.draw_networkx_edges(g, pos, ax=ax, edgelist=pkg_e, edge_color="#e07b39",
                           arrows=True, arrowsize=16, width=1.6, style="dashed",
                           connectionstyle="arc3,rad=0.06")
    labels = {k: _short(obj_by_key[k].display) for k in g.nodes}
    nx.draw_networkx_labels(g, pos, labels, ax=ax, font_size=8)

    ax.legend(handles=[mpatches.Patch(color="#3366cc", label=L("호출(call)", "call")),
                       mpatches.Patch(color="#e07b39", label=L("패키지 참조(pkg)", "package ref"))],
              fontsize=8, loc="best")
    ax.set_title(L("오브젝트 간 내부 호출/참조 그래프 (색=밴드, 크기 ∝ 최종점수)",
                   "Internal call/reference graph (color=band, size ∝ final score)"))
    ax.axis("off")
    fig.tight_layout()
    p = charts_dir / "dep_graph.png"
    fig.savefig(p, dpi=130)
    plt.close(fig)
    result["dep_graph"] = f"charts/{p.name}"


def _short(display: str) -> str:
    return display.split(".", 1)[-1]


def _chart_scatter(charts_dir, programs, scores, np, L, band_label, result) -> None:
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(8, 6))
    bands_seen: dict[str, bool] = {}
    for o in programs:
        s = scores[o.key]
        color = _BAND_COLOR.get(s.band, "#888888")
        lbl = band_label(s.band) if s.band not in bands_seen else None
        bands_seen[s.band] = True
        ax.scatter(s.absolute_score, s.impact_score, s=60 + s.final_score * 3,
                   color=color, alpha=0.75, edgecolors="white", linewidths=0.8, label=lbl)
    # 상위(최종점수) 라벨링
    for o in sorted(programs, key=lambda o: scores[o.key].final_score, reverse=True)[:6]:
        s = scores[o.key]
        ax.annotate(_short(o.display), (s.absolute_score, s.impact_score),
                    fontsize=8, xytext=(5, 4), textcoords="offset points")
    ax.set_xlabel(L("전환난이도 (absolute, fan-out)", "Conversion difficulty (absolute, fan-out)"))
    ax.set_ylabel(L("영향도 (impact, fan-in)", "Impact (fan-in)"))
    ax.set_title(L("오브젝트 점수 산포 · 점 크기 ∝ 최종점수",
                   "Object score distribution · size ∝ final score"))
    ax.grid(True, alpha=0.3)
    ax.legend(title=L("난이도 밴드", "Band"), fontsize=8)
    fig.tight_layout()
    p = charts_dir / "score_scatter.png"
    fig.savefig(p, dpi=130)
    plt.close(fig)
    result["score_scatter"] = f"charts/{p.name}"


def _chart_calibration(charts_dir, programs, scores, metrics_map, effort, es, ecfg,
                       np, L, result) -> None:
    import matplotlib.pyplot as plt
    feats = {o.key: _feature_value(ecfg, scores[o.key], metrics_map.get(o.key, Metrics()))
             for o in programs}
    m_keys = [o.key for o in programs if effort.get(o.key) and effort[o.key].measured is not None]
    if len(m_keys) < 2:
        return
    mx = [feats[k] for k in m_keys]
    my = [effort[k].measured for k in m_keys]

    fig, ax = plt.subplots(figsize=(8, 6))
    # 전체 예상(P50)
    px = [feats[o.key] for o in programs if effort.get(o.key) and effort[o.key].p50 is not None]
    py = [effort[o.key].p50 for o in programs if effort.get(o.key) and effort[o.key].p50 is not None]
    ax.scatter(px, py, s=30, facecolors="none", edgecolors="#4477aa",
               label=L("예상 P50 (전체)", "Predicted P50 (all)"))

    fit = es.get("fit")
    if fit and len(mx) >= 2:
        a, b, s = fit["a"], fit["b"], fit["resid_std"]
        n = len(mx)
        xbar = sum(mx) / n
        sxx = sum((x - xbar) ** 2 for x in mx)
        xs = np.linspace(min(feats.values()), max(feats.values()), 100)
        yline = a + b * xs
        ax.plot(xs, yline, color="#cc6677",
                label=L(f"회귀선 y={a}+{b}·x", f"fit y={a}+{b}·x"))
        if sxx > 0 and s > 0:
            t = _t90(n - 2)
            hw = np.array([t * s * math.sqrt(1 + 1 / n + (x - xbar) ** 2 / sxx) for x in xs])
            ax.fill_between(xs, yline - hw, yline + hw, color="#cc6677", alpha=0.18,
                            label=L("80% 예측구간 (P10~P90)", "80% prediction interval"))

    ax.scatter(mx, my, s=90, color="#222222", zorder=5, marker="D",
               label=L("실측 표본", "Measured samples"))
    for k in m_keys:
        ax.annotate(_short(next(o.display for o in programs if o.key == k)),
                    (feats[k], effort[k].measured), fontsize=8, xytext=(5, 4),
                    textcoords="offset points")
    unit = es["unit"]
    ax.set_xlabel(L(f"{ecfg.feature} (점수)", f"{ecfg.feature} (score)"))
    ax.set_ylabel(L(f"공수 ({unit})", f"Effort ({unit})"))
    ax.set_title(L(f"공수 캘리브레이션 · 표본 {n}개 · 신뢰도 {es['confidence']}",
                   f"Effort calibration · n={n} · confidence {es['confidence']}"))
    ax.grid(True, alpha=0.3)
    ax.legend(fontsize=8)
    fig.tight_layout()
    p = charts_dir / "effort_calibration.png"
    fig.savefig(p, dpi=130)
    plt.close(fig)
    result["effort_calibration"] = f"charts/{p.name}"


def _chart_ranges(charts_dir, programs, scores, effort, es, np, L, result) -> None:
    import matplotlib.pyplot as plt
    items = [(o, effort[o.key]) for o in programs
             if effort.get(o.key) and effort[o.key].p50 is not None]
    items.sort(key=lambda t: t[1].p50)
    if not items:
        return
    ys = range(len(items))
    fig, ax = plt.subplots(figsize=(8, max(3, len(items) * 0.42)))
    for i, (o, e) in zip(ys, items):
        measured = e.measured is not None
        color = "#222222" if measured else "#4477aa"
        ax.plot([e.p50, e.p90], [i, i], color=color, alpha=0.5, linewidth=2, zorder=1)
        ax.scatter(e.p50, i, color=color, s=45, zorder=3)
        if e.p90 > e.p50:
            ax.scatter(e.p90, i, color=color, s=30, marker="|", zorder=3)
    ax.set_yticks(list(ys))
    ax.set_yticklabels([_short(o.display) for o, _ in items], fontsize=8)
    unit = es["unit"]
    ax.set_xlabel(L(f"공수 ({unit})", f"Effort ({unit})"))
    ax.set_title(L("오브젝트별 예상 공수 P50–P90 (검정=실측, 파랑=추정)",
                   "Per-object effort P50–P90 (black=measured, blue=estimated)"))
    ax.grid(True, axis="x", alpha=0.3)
    fig.tight_layout()
    p = charts_dir / "effort_ranges.png"
    fig.savefig(p, dpi=130)
    plt.close(fig)
    result["effort_ranges"] = f"charts/{p.name}"
