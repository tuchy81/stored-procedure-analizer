"""표본 기반 전환 공수(Man-hour) 추정.

소수 오브젝트의 실측 시간(표본)으로 통계 보정한 뒤, 전체 오브젝트의 점수로 총 공수를 추정한다.

설계 원칙(사용자 협의):
  - base 공수(P50)는 '크기量'(전환난이도/LOC/복잡도/의존성)에서만 뽑는다. 최종점수(순위값)는 배제.
  - 표본이 속한 밴드는 그 평균으로, 표본 없는 밴드는 전 표본 선형회귀로 보간.
  - 영향도(fan-in)는 base 에 더하지 않고 P90 리스크 버퍼로만 반영(이중계상 방지).
  - 표본 수에 따라 신뢰도(HIGH/MEDIUM/LOW)를 명시.
"""
from __future__ import annotations

import csv
from pathlib import Path

from sqlscore.config import EffortConfig
from sqlscore.model import EffortEstimate, Metrics, Score, SourceObject

_FEATURE_MIN = 0.1  # 0-나눗셈/음수 예측 방지 하한


def load_samples(path: Path) -> list[dict]:
    """실측 표본 CSV 로드. 컬럼: KEY(또는 NAME), HOURS [, CATEGORY(무시-참고용)]."""
    rows: list[dict] = []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        cols = {c.lower(): c for c in (reader.fieldnames or [])}
        key_col = cols.get("key") or cols.get("name") or cols.get("object")
        hour_col = cols.get("hours") or cols.get("hour") or cols.get("mh") or cols.get("md")
        if not key_col or not hour_col:
            raise ValueError("표본 CSV 에 KEY(또는 NAME)/HOURS 컬럼이 필요합니다")
        for r in reader:
            raw_key = (r.get(key_col) or "").strip()
            raw_hour = (r.get(hour_col) or "").strip()
            if not raw_key or not raw_hour:
                continue
            try:
                hours = float(raw_hour)
            except ValueError:
                continue
            rows.append({"key": raw_key, "hours": hours})
    return rows


def _match_object(raw_key: str, objects: list[SourceObject], scores: dict[str, Score]) -> str | None:
    """표본 KEY 를 오브젝트 key 로 해석. 정확일치 → display 일치(다중이면 절대점수 최대=본문)."""
    up = raw_key.strip().upper()
    for o in objects:
        if o.key.upper() == up:
            return o.key
    cands = [o for o in objects if o.is_program and o.display == up]
    if not cands:
        # NAME 만 준 경우 (스키마 생략)
        cands = [o for o in objects if o.is_program and o.name.upper() == up.split(".")[-1]]
    if not cands:
        return None
    return max(cands, key=lambda o: scores.get(o.key, Score(o.key)).absolute_score).key


def _feature_value(cfg: EffortConfig, s: Score, m: Metrics) -> float:
    v = {"absolute": s.absolute_score, "loc": float(m.loc),
         "complexity": s.complexity_score, "dependency": s.dependency_score}.get(cfg.feature, s.absolute_score)
    return max(v, _FEATURE_MIN)


def _quantile(vals: list[float], q: float) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    if len(s) == 1:
        return s[0]
    pos = q * (len(s) - 1)
    lo = int(pos)
    frac = pos - lo
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * frac


def _linfit(xs: list[float], ys: list[float]) -> tuple[float, float, float] | None:
    """단변량 최소제곱 (a, b, 잔차표준편차). 표본<2 또는 x 분산 0 이면 None."""
    n = len(xs)
    if n < 2:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return None
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    a = my - b * mx
    resid = [y - (a + b * x) for x, y in zip(xs, ys)]
    rstd = (sum(r * r for r in resid) / (n - 2)) ** 0.5 if n > 2 else 0.0
    return a, b, rstd


def estimate(objects: list[SourceObject], scores: dict[str, Score], metrics_map: dict[str, Metrics],
             samples: list[dict], config: EffortConfig) -> tuple[dict[str, EffortEstimate], dict]:
    programs = [o for o in objects if o.is_program]

    # 표본 매칭
    measured: dict[str, float] = {}
    unmatched: list[str] = []
    for smp in samples:
        key = _match_object(smp["key"], objects, scores)
        if key is None:
            unmatched.append(smp["key"])
        else:
            measured[key] = smp["hours"]  # 동일 오브젝트 중복 시 마지막 값

    feat = {o.key: _feature_value(config, scores[o.key], metrics_map.get(o.key, Metrics())) for o in programs}

    # 전 표본 선형회귀(밴드 보간/ linear·ratio 방식용)
    sx = [feat[k] for k in measured]
    sy = [measured[k] for k in measured]
    fit = _linfit(sx, sy)
    ratio = (sum(sy[i] / sx[i] for i in range(len(sx))) / len(sx)) if sx else None

    # 밴드별 실측 집계
    band_hours: dict[str, list[float]] = {}
    for k, h in measured.items():
        band_hours.setdefault(scores[k].band, []).append(h)
    band_p50 = {b: sum(v) / len(v) for b, v in band_hours.items()}
    band_p90 = {b: (_quantile(v, 0.9) if len(v) >= 4 else (sum(v) / len(v)) * config.p90_multiplier)
                for b, v in band_hours.items()}

    def _base(key: str) -> tuple[float | None, str]:
        if key in measured:
            return measured[key], "실측"
        if config.method == "band":
            band = scores[key].band
            if band in band_p50:
                return band_p50[band], f"밴드:{band}(표본{len(band_hours[band])})"
            if fit is not None:
                a, b, _ = fit
                return max(a + b * feat[key], _FEATURE_MIN), "선형보간"
            return None, "추정불가(표본부족)"
        if config.method == "ratio":
            if ratio is not None:
                return ratio * feat[key], "비례추정"
            return None, "추정불가(표본부족)"
        # linear
        if fit is not None:
            a, b, _ = fit
            return max(a + b * feat[key], _FEATURE_MIN), "선형회귀"
        return None, "추정불가(표본부족)"

    estimates: dict[str, EffortEstimate] = {}
    for o in programs:
        # 실측 오브젝트는 실제값 확정 — 오버헤드/버퍼 미적용, P90=P50=실측
        if o.key in measured:
            mh = round(measured[o.key], 1)
            estimates[o.key] = EffortEstimate(o.key, mh, mh, "실측", measured[o.key])
            continue
        p50, basis = _base(o.key)
        if p50 is None:
            estimates[o.key] = EffortEstimate(o.key, None, None, basis, None)
            continue
        # per-object 오버헤드
        p50 *= (1 + config.per_object_overhead_pct / 100.0)
        # P90: 밴드분포 또는 배수, + 영향도 리스크 버퍼
        if config.method == "band" and scores[o.key].band in band_p90:
            p90 = band_p90[scores[o.key].band] * (1 + config.per_object_overhead_pct / 100.0)
        else:
            p90 = p50 * config.p90_multiplier
        imp_norm = scores[o.key].breakdown.get("final", {}).get("imp_norm", 0.0) / 100.0
        p90 *= (1 + config.impact_buffer * imp_norm)
        estimates[o.key] = EffortEstimate(o.key, round(p50, 1), round(max(p90, p50), 1), basis, None)

    known = [e for e in estimates.values() if e.p50 is not None]
    total_p50 = round(sum(e.p50 for e in known) + config.fixed_overhead_hours, 1)
    total_p90 = round(sum(e.p90 for e in known) + config.fixed_overhead_hours, 1)
    n_samples = len(measured)
    n_bands_sampled = len(band_hours)
    if n_samples >= 8 and n_bands_sampled >= 3:
        confidence = "HIGH"
    elif n_samples >= 3:
        confidence = "MEDIUM"
    else:
        confidence = "LOW"

    summary = {
        "method": config.method, "feature": config.feature, "unit": config.unit,
        "n_samples": n_samples, "n_matched": len(measured), "unmatched": unmatched,
        "n_unknown": len(estimates) - len(known),
        "confidence": confidence,
        "band_calib": {b: {"n": len(band_hours[b]), "p50": round(band_p50[b], 1),
                           "p90": round(band_p90[b], 1)} for b in band_hours},
        "fit": ({"a": round(fit[0], 3), "b": round(fit[1], 3), "resid_std": round(fit[2], 2)}
                if fit else None),
        "total_p50": total_p50, "total_p90": total_p90,
        "fixed_overhead_hours": config.fixed_overhead_hours,
        "per_object_overhead_pct": config.per_object_overhead_pct,
        "impact_buffer": config.impact_buffer,
    }
    return estimates, summary
