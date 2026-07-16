"""Markdown 리포트 생성 — 오브젝트 리스트·참조 관계·절대점수·근거."""
from __future__ import annotations

from sqlscore.config import ScoreConfig
from sqlscore.model import EffortEstimate, Metrics, Reference, Score, SourceObject

_RTYPE_KR = {
    "TABLE": "테이블/뷰", "CALL": "프로시저·함수 호출", "PACKAGE": "타 패키지",
    "BUILTIN": "빌트인 패키지", "SEQUENCE": "시퀀스", "DB_LINK": "DB Link",
    "REMOTE": "원격(시노님)", "GRANT": "GRANT 노출",
}


def _fmt(n: float) -> str:
    return f"{n:.1f}" if isinstance(n, float) else str(n)


def _mermaid_id(key: str) -> str:
    out = "".join(c if c.isalnum() else "_" for c in key)
    return out or "n"


def build_report(
    objects: list[SourceObject],
    metrics_map: dict[str, Metrics],
    refs: list[Reference],
    scores: dict[str, Score],
    config: ScoreConfig,
    *,
    src: str,
    scanned_files: list[str],
    generated_at: str | None = None,
    effort: dict[str, EffortEstimate] | None = None,
    effort_summary: dict | None = None,
    charts: dict[str, str] | None = None,
) -> str:
    charts = charts or {}
    programs = [o for o in objects if o.is_program]
    others = [o for o in objects if not o.is_program]
    obj_by_key = {o.key: o for o in objects}
    refs_by_src: dict[str, list[Reference]] = {}
    for r in refs:
        refs_by_src.setdefault(r.src_key, []).append(r)

    ranked = sorted(programs, key=lambda o: scores.get(o.key, Score(o.key)).final_score, reverse=True)

    lines: list[str] = []
    A = lines.append

    # ── 헤더 ──────────────────────────────────────────────
    A("# DB 프로그램 난이도(복잡도·의존성) 평가 리포트")
    A("")
    if generated_at:
        A(f"- **생성 시각**: {generated_at}")
    A(f"- **분석 대상 폴더**: `{src}`")
    A(f"- **스캔 파일 수**: {len(scanned_files)}개")
    A(f"- **식별 오브젝트**: 총 {len(objects)}개 (프로그램 {len(programs)}개, 기타 {len(others)}개)")
    A("")

    # ── 요약 ──────────────────────────────────────────────
    A("## 1. 요약")
    A("")
    band_dist: dict[str, int] = {}
    for o in programs:
        b = scores.get(o.key, Score(o.key)).band
        band_dist[b] = band_dist.get(b, 0) + 1
    if programs:
        avg = sum(scores[o.key].final_score for o in programs) / len(programs)
        mx = max(scores[o.key].final_score for o in programs)
        A(f"- 프로그램 오브젝트 평균 최종점수: **{avg:.1f}**, 최고 **{mx:.1f}** (0~100)")
    order = [lbl for _, lbl in config.band_thresholds]
    dist_str = ", ".join(f"{b} {band_dist.get(b, 0)}개" for b in order)
    A(f"- 전환난이도 밴드 분포: {dist_str}")
    if effort_summary:
        u = effort_summary["unit"]
        conf_icon = {"HIGH": "🟢", "MEDIUM": "🟡", "LOW": "🔴"}.get(effort_summary["confidence"], "")
        A(f"- **추정 전환 공수: P50 {effort_summary['total_p50']}{u} · P90 {effort_summary['total_p90']}{u}** "
          f"(신뢰도 {conf_icon} {effort_summary['confidence']}, 실측 표본 {effort_summary['n_matched']}개) — 상세 §7")
    A("")
    if ranked:
        A("**최종점수 상위 오브젝트 (Top 5)**")
        A("")
        A("| 순위 | 오브젝트 | 타입 | 최종점수 | 전환난이도 | 영향도 |")
        A("|---:|---|---|---:|---:|---:|")
        for i, o in enumerate(ranked[:5], start=1):
            s = scores[o.key]
            A(f"| {i} | `{o.display}` | {o.otype} | **{s.final_score:.1f}** "
              f"| {s.absolute_score:.1f} ({s.band}) | {s.impact_score:.1f} |")
        A("")

    # ── 방법론 ────────────────────────────────────────────
    A("## 2. 평가 방법론 (점수 산정 기준)")
    A("")
    A("절대점수는 세 성분의 가중합이며, 모든 가중치는 아래 값을 사용한다 (재현 가능).")
    A("")
    A("```")
    A("절대점수 = w_volume·(LOC / loc_divisor)")
    A("         + w_complexity·(분기·루프·쿼리·쿼리중첩·DML·동적SQL·예외·서브프로그램 가중합)")
    A("         + w_dependency·(참조 종류별 가산점 합)")
    A("```")
    A("")
    cw = config.composite
    A(f"- 성분 가중치: `w_volume={cw.volume}`, `w_complexity={cw.complexity}`, "
      f"`w_dependency={cw.dependency}`, `loc_divisor={config.loc_divisor}`")
    A("")
    A("**참조 종류별 가산점 (1건당)**")
    A("")
    A("| 참조 종류 | 설명 | 가산점 |")
    A("|---|---|---:|")
    for rt, w in sorted(config.ref_weights.items(), key=lambda kv: -kv[1]):
        A(f"| {rt} | {_RTYPE_KR.get(rt, rt)} | {w} |")
    A("")
    cx = config.complexity
    A("**복잡도 가중치 (1건당)**")
    A("")
    A("| 지표 | 가중치 |  | 지표 | 가중치 |")
    A("|---|---:|---|---|---:|")
    A(f"| 분기(IF/CASE/WHEN) | {cx.branch} | | 동적SQL | {cx.dynamic_sql} |")
    A(f"| 루프(LOOP/WHILE/FOR) | {cx.loop} | | 예외 핸들러 | {cx.exception_handler} |")
    A(f"| SELECT/서브쿼리 | {cx.query} | | WHEN OTHERS | {cx.when_others} |")
    A(f"| 서브쿼리 중첩깊이 | {cx.query_nesting} | | 서브프로그램 수 | {cx.subprogram} |")
    A(f"| DML | {cx.dml} | | | |")
    A("")
    bands = ", ".join(
        f"{'~' if u == float('inf') else '<'+str(int(u))} → {lbl}" for u, lbl in config.band_thresholds
    )
    A(f"**난이도 밴드**: {bands}")
    A("")
    iw = config.impact
    A("**영향도(파급도) — 별도 축**: 절대점수가 '이 오브젝트를 전환하는 비용'(내보내는 의존 fan-out)이라면, "
      "영향도는 '이 오브젝트를 바꿀 때 깨지는 범위'(들어오는 의존 fan-in)를 잰다.")
    A("")
    A(f"- 영향도 = `call_in={iw.call_in}`·(들어오는 호출 수) + `package_in={iw.package_in}`·(들어오는 패키지참조 수)"
      f" + `grant_exposure={iw.grant_exposure}`·(GRANT 수신자 수)")
    A("")
    fc = config.final
    A("**최종 단일점수 (0~100)** — 서로 다른 척도의 두 축을 각각 0~100 으로 정규화한 뒤 결합한다.")
    A("")
    A("```")
    if fc.method == "geometric":
        A("최종점수 = √( 정규화(전환난이도) × 정규화(영향도) )")
    else:
        A(f"최종점수 = ( {fc.w_conversion}·정규화(전환난이도) + {fc.w_impact}·정규화(영향도) )"
          f" / {round(fc.w_conversion + fc.w_impact, 3)}")
    A("```")
    A("")
    A(f"- 결합 방식 `method={fc.method}` (weighted_sum: 보상적 가중합 / geometric: 둘 다 높아야 상위)")
    A(f"- 정규화 `normalize={fc.normalize}` (rank: 순위기반, 이상치 강건 / minmax: 크기 보존)")
    A("- 전환난이도(fan-out)와 영향도(fan-in)는 단위가 달라 원점수를 그대로 더할 수 없으므로 정규화가 필수다. "
      "기본값은 전환 비용에 0.7, 변경 파급에 0.3 가중 — 실제 작업량은 전환이 지배하되 파급이 큰 허브를 끌어올린다.")
    A("")

    # ── 오브젝트 리스트 ───────────────────────────────────
    A("## 3. 오브젝트 리스트 및 최종점수")
    A("")
    A("| 순위 | 오브젝트 | 타입 | 파일 | LOC | 코드분량 | 복잡도 | 의존성 | 전환난이도 | 밴드 | 영향도 | 최종점수 |")
    A("|---:|---|---|---|---:|---:|---:|---:|---:|:--:|---:|---:|")
    for i, o in enumerate(ranked, start=1):
        s = scores[o.key]
        m = metrics_map.get(o.key, Metrics())
        A(f"| {i} | `{o.display}` | {o.otype} | `{o.file}` | {m.loc} "
          f"| {s.volume_score:.1f} | {s.complexity_score:.1f} | {s.dependency_score:.1f} "
          f"| {s.absolute_score:.1f} | {s.band} | {s.impact_score:.1f} | **{s.final_score:.1f}** |")
    A("")
    A("> 전환난이도 = 내보내는 의존 fan-out(자기 전환 비용). 영향도 = 들어오는 의존 fan-in(변경 파급). "
      "최종점수 = 두 축 정규화 결합(0~100). 리팩토링 관점 상세는 §6.")
    A("")
    if charts.get("score_scatter"):
        A(f"![오브젝트 점수 산포]({charts['score_scatter']})")
        A("")
        A("> 점수 산포: 오른쪽일수록 전환 난이도(작업량)↑, 위쪽일수록 영향도(변경 파급)↑, 점 크기 ∝ 최종점수. "
          "좌상단의 작지만 높은 점 = 전환은 쉬워도 파급이 큰 허브.")
        A("")
    if others:
        A("> 참조 대상으로만 식별된 비프로그램 오브젝트: "
          + ", ".join(f"`{o.display}`({o.otype})" for o in others[:40])
          + (" …" if len(others) > 40 else ""))
        A("")

    # ── 참조 관계 ─────────────────────────────────────────
    A("## 4. 참조(의존) 관계")
    A("")
    internal = [r for r in refs if r.resolved and r.target_key in obj_by_key
                and obj_by_key[r.target_key].is_program]
    if internal:
        A("### 4.1 오브젝트 간 내부 호출/참조 그래프")
        A("")
        if charts.get("dep_graph"):
            # 이미지(PNG)로 임베드 → 단일 HTML 에서 오프라인으로 표시(외부 스크립트 불필요)
            A(f"![오브젝트 간 내부 호출/참조 그래프]({charts['dep_graph']})")
            A("")
            A("> 화살표: 실선=호출(call), 점선=패키지 참조(pkg). 노드 색=난이도 밴드, 크기 ∝ 최종점수.")
            A("")
        else:
            # matplotlib/networkx 미가용 시 mermaid 폴백(GitHub 등에서 렌더)
            A("```mermaid")
            A("graph LR")
            seen_nodes: set[str] = set()
            for r in internal:
                src_o, dst_o = obj_by_key[r.src_key], obj_by_key[r.target_key]
                for node in (src_o, dst_o):
                    nid = _mermaid_id(node.key)
                    if nid not in seen_nodes:
                        A(f'  {nid}["{node.display}"]')
                        seen_nodes.add(nid)
                label = "call" if r.rtype == "CALL" else "pkg"
                A(f"  {_mermaid_id(r.src_key)} -->|{label}| {_mermaid_id(r.target_key)}")
            A("```")
            A("")
    else:
        A("_집합 내부 오브젝트 간 직접 호출/참조는 식별되지 않았습니다._")
        A("")

    A("### 4.2 오브젝트별 참조 상세")
    A("")
    for o in ranked:
        orefs = refs_by_src.get(o.key, [])
        if not orefs:
            continue
        A(f"- **`{o.display}`**")
        by_type: dict[str, list[Reference]] = {}
        for r in orefs:
            by_type.setdefault(r.rtype, []).append(r)
        for rt in sorted(by_type):
            targets = ", ".join(
                f"{r.target}{'✓' if r.resolved else ''}" + (f"×{r.count}" if r.count > 1 else "")
                for r in sorted(by_type[rt], key=lambda x: x.target)
            )
            A(f"  - {_RTYPE_KR.get(rt, rt)} ({rt}): {targets}")
    A("")
    A("> `✓` = 스캔 집합 내에서 실체가 해석된 참조")
    A("")

    # ── 점수 근거 상세 ────────────────────────────────────
    A("## 5. 오브젝트별 점수 근거")
    A("")
    for i, o in enumerate(ranked, start=1):
        s = scores[o.key]
        m = metrics_map.get(o.key, Metrics())
        A(f"### 5.{i} `{o.display}` — 최종점수 {s.final_score:.1f} "
          f"(전환난이도 {s.absolute_score:.1f}/{s.band}, 영향도 {s.impact_score:.1f})")
        A("")
        A(f"- 위치: `{o.file}` L{o.start_line}–{o.end_line}, 타입: {o.otype}")
        A(f"- 지표: LOC={m.loc}, 분기={m.branch_count}, 루프={m.loop_count}, "
          f"SELECT/서브쿼리={m.query_count}(중첩 {m.max_query_nesting}), DML={m.dml_count}, "
          f"동적SQL={m.dynamic_sql_count}, 예외핸들러={m.exception_handler_count}"
          + (", WHEN OTHERS 존재" if m.when_others else "")
          + (f", 서브프로그램={m.subprogram_count}" if m.subprogram_count else ""))
        vb = s.breakdown["volume"]
        A(f"- **코드분량 성분** = {vb['raw']}  (LOC {vb['loc']} / {vb['loc_divisor']})")
        cxb = s.breakdown["complexity"]
        if cxb["items"]:
            terms = " + ".join(f"{k}:{v}" for k, v in cxb["items"].items())
            A(f"- **복잡도 성분** = {cxb['raw']}  ({terms})")
        else:
            A(f"- **복잡도 성분** = {cxb['raw']}")
        db = s.breakdown["dependency"]
        if db["by_type"]:
            terms = " + ".join(f"{k}:{val}(×{db['counts'][k]})" for k, val in db["by_type"].items())
            A(f"- **의존성 성분** = {db['raw']}  ({terms})")
        else:
            A(f"- **의존성 성분** = {db['raw']}")
        A(f"- **합계(절대점수/전환난이도)** = 코드분량 {s.volume_score:.1f} + 복잡도 {s.complexity_score:.1f} "
          f"+ 의존성 {s.dependency_score:.1f} = **{s.absolute_score:.1f}** → {s.band}")
        imp = s.breakdown.get("impact", {})
        callers = imp.get("callers", [])
        caller_disp = ", ".join(obj_by_key[c].display for c in callers if c in obj_by_key)
        parts = [f"피호출 {len(callers)}곳"]
        if caller_disp:
            parts[0] += f": {caller_disp}"
        if imp.get("grant_exposure"):
            parts.append(f"GRANT 외부노출 {imp['grant_exposure']}")
        fb = s.breakdown.get("final", {})
        A(f"- **영향도(파급도)** = {s.impact_score:.1f}  ({', '.join(parts)})")
        A(f"- **최종점수** = {s.final_score:.1f}  "
          f"(정규화 전환난이도 {fb.get('conv_norm', 0)} × {fb.get('w_conversion', '')} "
          f"+ 정규화 영향도 {fb.get('imp_norm', 0)} × {fb.get('w_impact', '')}, {fb.get('method', '')})")
        A("")

    # ── 리팩토링 관점 ─────────────────────────────────────
    A("## 6. 리팩토링 관점 — 영향도(변경 파급)")
    A("")
    A("최종점수(§3)는 두 축을 결합한 종합 지표다. 이 절은 그중 **영향도(fan-in)** 축만 따로 세워 "
      "'많은 곳이 의존해 바꿀 때 파급이 큰' 리팩토링 고위험 오브젝트를 드러낸다 — "
      "전환난이도가 낮아도 여기 상위면 계약(시그니처) 유지가 최우선이다.")
    A("")
    by_impact = sorted(programs, key=lambda o: (scores[o.key].impact_score,
                                                scores[o.key].final_score), reverse=True)
    A("| 순위 | 오브젝트 | 타입 | 피호출수 | 영향도 | 전환난이도 | 밴드 | 최종점수 |")
    A("|---:|---|---|---:|---:|---:|:--:|---:|")
    for i, o in enumerate(by_impact, start=1):
        s = scores[o.key]
        callers = s.breakdown.get("impact", {}).get("callers", [])
        A(f"| {i} | `{o.display}` | {o.otype} | {len(callers)} | {s.impact_score:.1f} "
          f"| {s.absolute_score:.1f} | {s.band} | **{s.final_score:.1f}** |")
    A("")
    hubs = [o for o in by_impact if scores[o.key].impact_score > 0][:3]
    if hubs:
        A("**핵심 허브(변경 시 파급 최상위)**")
        A("")
        for o in hubs:
            s = scores[o.key]
            imp = s.breakdown.get("impact", {})
            callers = [obj_by_key[c].display for c in imp.get("callers", []) if c in obj_by_key]
            grant_exp = imp.get("grant_exposure", 0)
            head = f"- `{o.display}` — 영향도 {s.impact_score:.1f} (전환난이도 {s.absolute_score:.1f}/{s.band})"
            if callers:
                reason = (f"피호출 {len(callers)}곳({', '.join(callers)}) — 시그니처·로직 변경 시 "
                          "이 호출자들이 함께 깨지므로 계약 유지·회귀 테스트가 최우선")
            elif grant_exp:
                reason = (f"직접 호출자는 없으나 GRANT로 외부(스캔 밖) 소비자 {grant_exp}곳에 노출 — "
                          "외부 계약 변경 위험, 인터페이스 동결 후 내부 리팩토링 권장")
            else:
                reason = "파급 요인 있음"
            A(f"{head}: {reason}.")
        A("")

    # ── 전환 공수 추정 ────────────────────────────────────
    if effort and effort_summary:
        _emit_effort_section(A, ranked, scores, effort, effort_summary, charts)

    return "\n".join(lines) + "\n"


def _emit_effort_section(A, ranked: list[SourceObject], scores: dict[str, Score],
                         effort: dict[str, EffortEstimate], es: dict,
                         charts: dict[str, str]) -> None:
    unit = es["unit"]
    A("## 7. 전환 공수(Man-hour) 추정")
    A("")
    A(f"소수 실측 표본을 점수에 통계 보정해 전체 공수를 추정한다 (표본 {es['n_matched']}개).")
    A("")
    A("**P50 / P90 의미**")
    A("")
    A("- **P50** = 중앙(50 백분위) 추정. \"절반의 확률로 이 시간 이하\"인 **계획 기준선**(most-likely).")
    A("- **P90** = 보수(90 백분위) 상한. \"90% 확률로 이 시간 이하\"인 **리스크 포함 상한**. "
      "실제 공수는 대개 P50~P90 사이에 들어오며, 버퍼/컨틴전시는 P90 기준으로 잡는다.")
    A(f"- P90 에는 표본 분포(또는 ×{es.get('p90_multiplier', 1.5)} 배수)에 더해 영향도 리스크 버퍼"
      f"(최대 +{int(es.get('impact_buffer', 0) * 100)}%)가 포함된다 — 영향도는 base(P50)엔 더하지 않아 이중계상이 없다.")
    A("")
    # 신뢰도 근거
    sampled_bands = set(es.get("band_calib", {}).keys())
    prog_bands = [scores[o.key].band for o in ranked]
    band_order = []
    for b in prog_bands:
        if b not in band_order:
            band_order.append(b)
    unsampled = [b for b in band_order if b not in sampled_bands]
    conf = es["confidence"]
    conf_icon = {"HIGH": "🟢", "MEDIUM": "🟡", "LOW": "🔴"}.get(conf, "")
    A("**신뢰도**")
    A("")
    A(f"- **{conf_icon} {conf}** — 판정 기준: HIGH(표본 ≥8 & 표본 있는 밴드 ≥3) · MEDIUM(표본 ≥3) · LOW(그 외).")
    A(f"- 현재: 실측 표본 {es['n_matched']}개, 표본 있는 밴드 {len(sampled_bands)}개"
      + (f", **표본 없는 밴드(선형보간 의존): {', '.join(unsampled)}**" if unsampled else ", 모든 밴드에 표본 있음"))
    A("- 신뢰도를 높이려면: 표본 수를 늘리고, 특히 **표본 없는 밴드마다 최소 1개**씩 실측을 추가하라 "
      "(보간 의존이 줄어 P50/P90 정확도가 오른다).")
    A("")
    A(f"- 추정 방식: `method={es['method']}` / 회귀피처 `feature={es['feature']}` (크기量, 순위값 아님)")
    if es.get("fixed_overhead_hours") or es.get("per_object_overhead_pct"):
        A(f"- 오버헤드: 고정 {es['fixed_overhead_hours']}{unit} + per-object {es['per_object_overhead_pct']}%")
    if es.get("unmatched"):
        A(f"- ⚠ 매칭 실패 표본(무시): {', '.join(es['unmatched'])}")
    if es.get("n_unknown"):
        A(f"- ⚠ 추정 불가 오브젝트 {es['n_unknown']}개(해당 밴드 표본 없음 & 보간 불가) — 총합에서 제외")
    A("")
    if es.get("band_calib"):
        A("**밴드별 보정 (실측 표본 집계)**")
        A("")
        A(f"| 밴드 | 표본수 | 평균 실측(P50, {unit}) | P90({unit}) |")
        A("|---|---:|---:|---:|")
        for b, c in es["band_calib"].items():
            A(f"| {b} | {c['n']} | {c['p50']} | {c['p90']} |")
        A("")
    if es.get("fit"):
        f = es["fit"]
        A(f"> 표본 없는 밴드 보간용 선형회귀: 공수 ≈ {f['a']} + {f['b']}·{es['feature']} "
          f"(잔차σ={f['resid_std']})")
        A("")
    if charts.get("effort_calibration"):
        A(f"![공수 캘리브레이션·예측구간]({charts['effort_calibration']})")
        A("")
        A("> 검은 마름모=실측 표본, 붉은 선=회귀(예상 P50), 음영=80% 예측구간(P10~P90, 통계적 신뢰구간), "
          "빈 원=전체 오브젝트 예상 P50. 표본이 늘수록 음영(불확실성)이 좁아진다.")
        A("")
    if charts.get("effort_ranges"):
        A(f"![오브젝트별 P50–P90 범위]({charts['effort_ranges']})")
        A("")
        A("> 각 오브젝트의 예상 공수 구간(P50→P90). 검정=실측(확정), 파랑=추정(P90 여유 폭이 곧 불확실성).")
        A("")
    A("**오브젝트별 공수 추정**")
    A("")
    A(f"| 오브젝트 | 밴드 | 최종점수 | P50({unit}) | P90({unit}) | 근거 |")
    A("|---|:--:|---:|---:|---:|---|")
    for o in ranked:
        e = effort.get(o.key)
        s = scores[o.key]
        if e is None:
            continue
        p50 = "—" if e.p50 is None else f"{e.p50}"
        p90 = "—" if e.p90 is None else f"{e.p90}"
        A(f"| `{o.display}` | {s.band} | {s.final_score:.1f} | {p50} | {p90} | {e.basis} |")
    A("")
    conf_icon = {"HIGH": "🟢", "MEDIUM": "🟡", "LOW": "🔴"}.get(es["confidence"], "")
    A(f"### 총 전환 공수: **P50 {es['total_p50']}{unit} · P90 {es['total_p90']}{unit}**  "
      f"(신뢰도 {conf_icon} {es['confidence']})")
    A("")
    A("> 총합 = Σ 오브젝트 P50/P90 + 고정 오버헤드. **계획은 P50, 예산·버퍼는 P90** 기준 권장. "
      "표본을 더 넣을수록(특히 표본 없는 밴드) 신뢰도가 올라간다.")
    A("")
