"""sqlscore end-to-end 및 단위 테스트."""
from __future__ import annotations

from pathlib import Path

import pytest

from sqlscore import dependencies, effort as effort_mod, parser, report, scoring
from sqlscore.config import ScoreConfig, load_config
from sqlscore.metrics import compute_metrics

EXAMPLES = Path(__file__).resolve().parent.parent / "examples" / "sql"


def _analyzed(cfg=None):
    cfg = cfg or ScoreConfig()
    objs, grants, scanned = parser.scan_folder(EXAMPLES)
    metrics_map = {o.key: compute_metrics(o) for o in objs if o.is_program}
    refs = dependencies.analyze(objs, grants)
    scores = scoring.score_all(objs, metrics_map, refs, cfg)
    return objs, grants, scanned, metrics_map, refs, scores, cfg


# ── 파서 ──────────────────────────────────────────────────

def test_parse_folder_identifies_all_objects():
    objs, grants, scanned = parser.scan_folder(EXAMPLES)
    keys = {o.key for o in objs}
    assert "HR.PKG_ORDER" in keys                    # package spec
    assert "HR.PKG_ORDER:PACKAGE_BODY" in keys       # package body 구분
    assert "HR.LOG_AUDIT" in keys
    assert "HR.PRC_SETTLE_DAILY" in keys
    assert "HR.TRG_ORDERS_BI" in keys
    types = {o.key: o.otype for o in objs}
    assert types["HR.PRC_SETTLE_DAILY"] == "PROCEDURE"
    assert types["HR.TRG_ORDERS_BI"] == "TRIGGER"
    assert len(grants) == 2                            # PKG_ORDER, LOG_AUDIT 에 GRANT EXECUTE
    assert scanned


def test_owner_and_name_split():
    objs, _, _ = parser.scan_folder(EXAMPLES)
    body = next(o for o in objs if o.key == "HR.PKG_ORDER:PACKAGE_BODY")
    assert body.owner == "HR"
    assert body.name == "PKG_ORDER"


def test_slash_terminator_splits_spec_and_body():
    objs, _, _ = parser.scan_folder(EXAMPLES)
    pkg_objs = [o for o in objs if o.name == "PKG_ORDER"]
    otypes = sorted(o.otype for o in pkg_objs)
    assert otypes == ["PACKAGE", "PACKAGE_BODY"]


# ── 지표 ──────────────────────────────────────────────────

def _get(objs, key):
    return next(o for o in objs if o.key == key)


def test_metrics_settle_loop_and_subquery_nesting():
    objs, _, _ = parser.scan_folder(EXAMPLES)
    m = compute_metrics(_get(objs, "HR.PRC_SETTLE_DAILY"))
    assert m.loop_count == 1               # FOR 커서 루프 1개 (FOR EACH ROW/이중계수 없음)
    assert m.max_query_nesting == 2        # IN (SELECT ... (SELECT ...))
    assert m.dynamic_sql_count == 1        # EXECUTE IMMEDIATE
    assert m.exception_handler_count == 2  # NO_DATA_FOUND, OTHERS
    assert m.when_others == 1


def test_metrics_branch_no_double_count_end_if():
    objs, _, _ = parser.scan_folder(EXAMPLES)
    m = compute_metrics(_get(objs, "HR.TRG_ORDERS_BI"))
    assert m.branch_count == 1             # IF 1개 (END IF 이중계수 없음)
    assert m.loop_count == 0               # FOR EACH ROW 는 루프 아님


def test_metrics_package_body_branches_and_subprograms():
    objs, _, _ = parser.scan_folder(EXAMPLES)
    m = compute_metrics(_get(objs, "HR.PKG_ORDER:PACKAGE_BODY"))
    assert m.branch_count == 2             # IF + ELSIF
    assert m.subprogram_count == 2         # create_order, order_total
    assert m.dml_count == 2                # INSERT + UPDATE


# ── 의존성 ────────────────────────────────────────────────

def test_dependencies_detects_ref_types():
    objs, grants, _ = parser.scan_folder(EXAMPLES)
    refs = dependencies.analyze(objs, grants)
    body_refs = [r for r in refs if r.src_key == "HR.PKG_ORDER:PACKAGE_BODY"]
    rtypes = {r.rtype for r in body_refs}
    assert "DB_LINK" in rtypes             # INVENTORY@REMOTE_WMS
    assert "BUILTIN" in rtypes             # DBMS_OUTPUT
    assert "SEQUENCE" in rtypes            # SEQ_ORDER.NEXTVAL
    assert "CALL" in rtypes                # LOG_AUDIT
    assert "GRANT" in rtypes               # GRANT EXECUTE ... TO APP_USER
    assert "TABLE" in rtypes               # ORDERS


def test_call_resolution_is_internal():
    objs, grants, _ = parser.scan_folder(EXAMPLES)
    refs = dependencies.analyze(objs, grants)
    call = next(r for r in refs if r.src_key == "HR.PRC_SETTLE_DAILY" and r.rtype == "CALL")
    assert call.resolved is True
    assert call.target_key == "HR.LOG_AUDIT"
    pkg = next(r for r in refs if r.src_key == "HR.PRC_SETTLE_DAILY" and r.rtype == "PACKAGE")
    assert pkg.target == "PKG_ORDER"
    assert pkg.resolved is True


def test_dblink_not_double_counted_as_table():
    objs, grants, _ = parser.scan_folder(EXAMPLES)
    refs = dependencies.analyze(objs, grants)
    body_tables = [r.target for r in refs
                   if r.src_key == "HR.PKG_ORDER:PACKAGE_BODY" and r.rtype == "TABLE"]
    assert not any("@" in t or "INVENTORY" == t for t in body_tables)


# ── 스코어링 ──────────────────────────────────────────────

def test_scoring_absolute_is_sum_of_components():
    objs, grants, _ = parser.scan_folder(EXAMPLES)
    cfg = ScoreConfig()
    metrics_map = {o.key: compute_metrics(o) for o in objs if o.is_program}
    refs = dependencies.analyze(objs, grants)
    scores = scoring.score_all(objs, metrics_map, refs, cfg)
    s = scores["HR.PKG_ORDER:PACKAGE_BODY"]
    assert abs(s.absolute_score - (s.volume_score + s.complexity_score + s.dependency_score)) < 0.01
    assert s.dependency_score > 0          # DB_LINK 등으로 의존성 성분 존재
    assert s.band in ("낮음", "보통", "높음", "매우높음")


def test_impact_is_fanin_based_hub_scores_highest():
    """caller 는 절대점수(fan-out)가 높고, 많이 호출되는 callee(허브)는 영향도(fan-in)가 높아야 한다."""
    objs, grants, _ = parser.scan_folder(EXAMPLES)
    cfg = ScoreConfig()
    metrics_map = {o.key: compute_metrics(o) for o in objs if o.is_program}
    refs = dependencies.analyze(objs, grants)
    scores = scoring.score_all(objs, metrics_map, refs, cfg)

    # LOG_AUDIT 는 PKG_ORDER, PRC_SETTLE_DAILY 두 곳에서 호출되는 허브
    log_audit = scores["HR.LOG_AUDIT"]
    assert sorted(log_audit.breakdown["impact"]["callers"]) == [
        "HR.PKG_ORDER:PACKAGE_BODY", "HR.PRC_SETTLE_DAILY"
    ]
    assert log_audit.impact_score > 0

    # 아무도 호출하지 않는 PRC_SETTLE_DAILY 는 절대점수는 높아도 영향도(직접 fan-in)는 낮다
    settle = scores["HR.PRC_SETTLE_DAILY"]
    assert settle.absolute_score > log_audit.absolute_score      # 전환난이도: caller 우위
    assert log_audit.impact_score >= settle.impact_score         # 영향도: callee(허브) 우위


def test_impact_and_final_present_in_report():
    objs, grants, scanned = parser.scan_folder(EXAMPLES)
    cfg = ScoreConfig()
    metrics_map = {o.key: compute_metrics(o) for o in objs if o.is_program}
    refs = dependencies.analyze(objs, grants)
    scores = scoring.score_all(objs, metrics_map, refs, cfg)
    md = report.build_report(objs, metrics_map, refs, scores, cfg,
                             src="examples/sql", scanned_files=scanned)
    assert "## 6. 리팩토링 관점" in md
    assert "영향도" in md
    assert "최종점수" in md


def test_final_score_combines_axes_and_bounded():
    """최종점수는 0~100 이며, 전환·영향 두 축을 정규화 결합한다."""
    objs, grants, _ = parser.scan_folder(EXAMPLES)
    cfg = ScoreConfig()
    metrics_map = {o.key: compute_metrics(o) for o in objs if o.is_program}
    refs = dependencies.analyze(objs, grants)
    scores = scoring.score_all(objs, metrics_map, refs, cfg)
    for s in scores.values():
        assert 0.0 <= s.final_score <= 100.0
        assert "final" in s.breakdown
    # 두 축 모두 높은 PKG_ORDER 본문이 최종 1위
    top = max(scores.values(), key=lambda s: s.final_score)
    assert top.key == "HR.PKG_ORDER:PACKAGE_BODY"
    # 허브(LOG_AUDIT)는 전환난이도 최하위권이지만 영향도가 최종점수를 끌어올려
    # 아무 의존도 없는 단순 트리거보다 훨씬 높다 (fan-in 이 최종점수에 반영됨)
    hub = scores["HR.LOG_AUDIT"]
    trg = scores["HR.TRG_ORDERS_BI"]
    fb = hub.breakdown["final"]
    assert hub.final_score > fb["conv_norm"] * fb["w_conversion"]  # 영향도가 가산됨
    assert hub.final_score > trg.final_score


def test_final_score_weight_shift_to_impact_lifts_hub():
    """영향도 가중치를 키우면 허브의 최종점수가 상대적으로 올라간다."""
    objs, grants, _ = parser.scan_folder(EXAMPLES)
    metrics_map = {o.key: compute_metrics(o) for o in objs if o.is_program}
    refs = dependencies.analyze(objs, grants)

    base = scoring.score_all(objs, metrics_map, refs, ScoreConfig())
    cfg2 = ScoreConfig()
    cfg2.final.w_conversion, cfg2.final.w_impact = 0.2, 0.8
    shifted = scoring.score_all(objs, metrics_map, refs, cfg2)
    assert shifted["HR.LOG_AUDIT"].final_score > base["HR.LOG_AUDIT"].final_score


def test_impact_and_final_weights_yaml_override(tmp_path):
    p = tmp_path / "cfg.yaml"
    p.write_text("impact:\n  call_in: 20\nfinal:\n  method: geometric\n  w_impact: 0.5\n",
                 encoding="utf-8")
    cfg = load_config(p)
    assert cfg.impact.call_in == 20
    assert cfg.impact.package_in == 3.0   # 미지정 기본값 유지
    assert cfg.final.method == "geometric"
    assert cfg.final.w_impact == 0.5
    assert cfg.final.normalize == "rank"  # 미지정 기본값 유지


def test_effort_measured_samples_are_exact():
    """실측 표본은 P50=P90=실측값(버퍼 미적용)."""
    objs, _, _, metrics_map, _, scores, cfg = _analyzed()
    samples = [{"key": "HR.PKG_ORDER:PACKAGE_BODY", "hours": 40.0},
               {"key": "HR.LOG_AUDIT", "hours": 5.0},
               {"key": "HR.PRC_SETTLE_DAILY", "hours": 30.0}]
    est, summary = effort_mod.estimate(objs, scores, metrics_map, samples, cfg.effort)
    assert est["HR.LOG_AUDIT"].p50 == 5.0
    assert est["HR.LOG_AUDIT"].p90 == 5.0
    assert est["HR.LOG_AUDIT"].basis == "실측"
    assert summary["n_matched"] == 3


def test_effort_band_and_interpolation_fill_all():
    """표본 없는 밴드는 선형보간으로 채워 전체 오브젝트가 추정된다."""
    objs, _, _, metrics_map, _, scores, cfg = _analyzed()
    samples = [{"key": "HR.PKG_ORDER:PACKAGE_BODY", "hours": 40.0},
               {"key": "HR.LOG_AUDIT", "hours": 5.0}]
    est, summary = effort_mod.estimate(objs, scores, metrics_map, samples, cfg.effort)
    programs = [o for o in objs if o.is_program]
    assert all(est[o.key].p50 is not None for o in programs)   # 전부 추정됨
    assert summary["total_p50"] > 0
    assert summary["total_p90"] >= summary["total_p50"]
    assert summary["confidence"] in ("LOW", "MEDIUM", "HIGH")


def test_effort_p90_impact_buffer_only_on_estimated():
    """영향도 리스크 버퍼는 추정 오브젝트의 P90 에만 붙고 base(P50)엔 안 붙는다."""
    objs, _, _, metrics_map, _, scores, cfg = _analyzed()
    # 허브 LOG_AUDIT 를 표본에서 빼고, 다른 표본으로 밴드/보간을 채움
    samples = [{"key": "HR.PKG_ORDER:PACKAGE_BODY", "hours": 40.0},
               {"key": "HR.TRG_ORDERS_BI", "hours": 4.0}]
    est, _ = effort_mod.estimate(objs, scores, metrics_map, samples, cfg.effort)
    hub = est["HR.LOG_AUDIT"]
    assert hub.p50 is not None and hub.p90 > hub.p50   # 영향도 있는 허브는 P90>P50


def test_effort_config_yaml_override(tmp_path):
    p = tmp_path / "cfg.yaml"
    p.write_text("effort:\n  method: linear\n  feature: loc\n  p90_multiplier: 2.0\n  unit: MD\n",
                 encoding="utf-8")
    cfg = load_config(p)
    assert cfg.effort.method == "linear"
    assert cfg.effort.feature == "loc"
    assert cfg.effort.p90_multiplier == 2.0
    assert cfg.effort.unit == "MD"
    assert cfg.effort.impact_buffer == 0.3   # 미지정 기본값 유지


def test_effort_section_in_report():
    objs, _, scanned, metrics_map, refs, scores, cfg = _analyzed()
    samples = [{"key": "HR.PKG_ORDER:PACKAGE_BODY", "hours": 40.0},
               {"key": "HR.LOG_AUDIT", "hours": 5.0}]
    est, summary = effort_mod.estimate(objs, scores, metrics_map, samples, cfg.effort)
    md = report.build_report(objs, metrics_map, refs, scores, cfg,
                             src="examples/sql", scanned_files=scanned,
                             effort=est, effort_summary=summary)
    assert "## 7. 전환 공수" in md
    assert "총 전환 공수" in md


def test_viz_generates_charts(tmp_path):
    """matplotlib 가 있으면 3종 차트 PNG 를 생성한다(없으면 skip)."""
    pytest.importorskip("matplotlib")
    from sqlscore import viz
    objs, _, _, metrics_map, _, scores, cfg = _analyzed()
    samples = [{"key": "HR.PKG_ORDER:PACKAGE_BODY", "hours": 40.0},
               {"key": "HR.LOG_AUDIT", "hours": 5.0}]
    est, summary = effort_mod.estimate(objs, scores, metrics_map, samples, cfg.effort)
    refs = dependencies.analyze(objs, [])
    charts = viz.render_charts(tmp_path, objs, scores, metrics_map, est, summary, cfg, refs=refs)
    assert charts is not None
    assert "score_scatter" in charts
    assert "effort_calibration" in charts and "effort_ranges" in charts
    assert "dep_graph" in charts                 # 의존성 그래프 PNG(단일파일용)
    for rel in charts.values():
        assert (tmp_path / rel).exists()


def test_viz_scatter_only_without_effort(tmp_path):
    pytest.importorskip("matplotlib")
    from sqlscore import viz
    objs, _, _, metrics_map, _, scores, cfg = _analyzed()
    charts = viz.render_charts(tmp_path, objs, scores, metrics_map, None, None, cfg)
    assert charts is not None
    assert "score_scatter" in charts
    assert "effort_calibration" not in charts   # 표본 없으면 캘리브레이션 차트 없음


def test_report_embeds_chart_images():
    objs, _, scanned, metrics_map, refs, scores, cfg = _analyzed()
    charts = {"score_scatter": "charts/score_scatter.png"}
    md = report.build_report(objs, metrics_map, refs, scores, cfg,
                             src="examples/sql", scanned_files=scanned, charts=charts)
    assert "![오브젝트 점수 산포](charts/score_scatter.png)" in md


def test_html_report_sections_and_wellformed():
    """MD → HTML 변환: 섹션이 details 로, TOC 링크로, 표/코드가 렌더되고 파싱 가능해야 한다."""
    from html.parser import HTMLParser

    from sqlscore import htmlreport
    objs, _, scanned, metrics_map, refs, scores, cfg = _analyzed()
    md = report.build_report(objs, metrics_map, refs, scores, cfg,
                             src="examples/sql", scanned_files=scanned,
                             charts={"score_scatter": "charts/score_scatter.png"})
    doc = htmlreport.to_html(md)
    assert doc.startswith("<!DOCTYPE html>")
    assert "<details class='sec'" in doc            # 섹션 확장 가능
    assert "id='sec-0'" in doc and "href='#sec-0'" in doc  # TOC 이동
    assert "모두 펼치기" in doc and "모두 접기" in doc
    assert "<table>" in doc                          # 표 렌더
    assert 'class="mermaid"' in doc                  # 의존성 그래프
    assert '<img src="charts/score_scatter.png"' in doc  # 차트 이미지

    class _P(HTMLParser):
        def error(self, msg):  # pragma: no cover
            raise AssertionError(msg)

    _P().feed(doc)  # 파싱 중 예외 없어야 함


def test_html_embeds_images_as_data_uri(tmp_path):
    """base_dir 의 실제 이미지가 있으면 data URI 로 인라인되어 단일 HTML 이 된다."""
    from sqlscore import htmlreport
    (tmp_path / "charts").mkdir()
    # 최소 PNG(1x1) 바이트
    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
        "0000000a49444154789c6360000002000154a24f6e0000000049454e44ae426082")
    (tmp_path / "charts" / "score_scatter.png").write_bytes(png)
    md = "# T\n\n## 1. 요약\n\n![산포](charts/score_scatter.png)\n"
    doc = htmlreport.to_html(md, base_dir=tmp_path, embed_images=True)
    assert 'src="data:image/png;base64,' in doc
    assert "charts/score_scatter.png" not in doc          # 외부 링크 없음(완전 임베드)


def test_html_no_embed_keeps_link(tmp_path):
    from sqlscore import htmlreport
    (tmp_path / "charts").mkdir()
    (tmp_path / "charts" / "x.png").write_bytes(b"x")
    md = "# T\n\n## 1. 요약\n\n![a](charts/x.png)\n"
    doc = htmlreport.to_html(md, base_dir=tmp_path, embed_images=False)
    assert 'src="charts/x.png"' in doc
    assert "data:image" not in doc


def test_html_missing_image_left_as_link(tmp_path):
    from sqlscore import htmlreport
    md = "# T\n\n## 1. 요약\n\n![a](charts/none.png)\n"
    doc = htmlreport.to_html(md, base_dir=tmp_path, embed_images=True)
    assert 'src="charts/none.png"' in doc                 # 없는 파일은 링크 유지


def test_html_no_mermaid_script_when_absent():
    from sqlscore import htmlreport
    md = "# 제목\n\n## 1. 요약\n\n- a\n- b\n\n| x | y |\n|---|---|\n| 1 | 2 |\n"
    doc = htmlreport.to_html(md)
    assert "mermaid.esm" not in doc                  # mermaid 블록 없으면 CDN 스크립트 미포함
    assert "<details class='sec'" in doc


def test_band_thresholds():
    cfg = ScoreConfig()
    assert cfg.band_for(10) == "낮음"
    assert cfg.band_for(30) == "보통"
    assert cfg.band_for(70) == "높음"
    assert cfg.band_for(150) == "매우높음"


def test_config_yaml_override(tmp_path):
    p = tmp_path / "cfg.yaml"
    p.write_text(
        "ref_weights:\n  db_link: 99\ncomposite:\n  dependency: 2\nloc_divisor: 5\n",
        encoding="utf-8",
    )
    cfg = load_config(p)
    assert cfg.ref_weights["DB_LINK"] == 99
    assert cfg.composite.dependency == 2
    assert cfg.loc_divisor == 5
    # 미지정 항목은 기본값 유지
    assert cfg.ref_weights["TABLE"] == 1.0


# ── 리포트 ────────────────────────────────────────────────

def test_report_contains_sections():
    objs, grants, scanned = parser.scan_folder(EXAMPLES)
    cfg = ScoreConfig()
    metrics_map = {o.key: compute_metrics(o) for o in objs if o.is_program}
    refs = dependencies.analyze(objs, grants)
    scores = scoring.score_all(objs, metrics_map, refs, cfg)
    md = report.build_report(objs, metrics_map, refs, scores, cfg,
                             src="examples/sql", scanned_files=scanned)
    assert "# DB 프로그램 난이도" in md
    assert "## 3. 오브젝트 리스트 및 최종점수" in md
    assert "## 4. 참조(의존) 관계" in md
    assert "```mermaid" in md
    assert "## 5. 오브젝트별 점수 근거" in md
    assert "HR.PKG_ORDER" in md
