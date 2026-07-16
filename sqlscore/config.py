"""sqlscore 설정 — 참조 가산 가중치, 복잡도 가중치, 절대점수 합성 가중치, 난이도 밴드.

모든 수치는 YAML 로 오버라이드 가능(선택). 미지정 시 아래 기본값을 쓰며,
리포트에 실제 사용된 가중치를 명시하므로 점수 근거가 항상 투명하게 재현된다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


def _default_ref_weights() -> dict[str, float]:
    # 참조 종류별 1건당 가산점 — '해석 난이도/전환 위험'이 큰 참조일수록 높게.
    return {
        "TABLE": 1.0,      # 로컬 테이블/뷰 참조
        "SEQUENCE": 0.5,   # 시퀀스 NEXTVAL/CURRVAL
        "BUILTIN": 2.0,    # DBMS_*/UTL_* 빌트인 패키지
        "PACKAGE": 3.0,    # 타 패키지 참조
        "CALL": 4.0,       # 타 프로시저/함수 호출
        "GRANT": 3.0,      # GRANT 로 외부 노출(크로스 스키마 결합)
        "REMOTE": 8.0,     # 시노님/원격 오브젝트 경유 참조
        "DB_LINK": 10.0,   # DB Link 직접 참조
    }


@dataclass
class ComplexityWeights:
    branch: float = 1.0            # 분기(IF/CASE/WHEN) 1건당
    loop: float = 2.0             # 루프(LOOP/WHILE/FOR) 1건당
    query: float = 1.0           # SELECT/서브쿼리 1건당
    query_nesting: float = 3.0   # 서브쿼리 최대 중첩 깊이 1단계당
    dml: float = 1.0             # INSERT/UPDATE/DELETE/MERGE 1건당
    dynamic_sql: float = 5.0     # 동적 SQL 1건당
    exception_handler: float = 1.5  # 예외 핸들러 1건당
    when_others: float = 2.0     # WHEN OTHERS 존재 시(광범위 예외 은폐)
    subprogram: float = 1.0      # 패키지 내부 서브프로그램 1개당


@dataclass
class CompositeWeights:
    """절대점수 = w_volume·volume + w_complexity·complexity + w_dependency·dependency."""
    volume: float = 1.0
    complexity: float = 1.0
    dependency: float = 1.0


@dataclass
class FinalScoreConfig:
    """단일 최종점수 = 정규화(전환난이도)·정규화(영향도) 결합, 0~100.

    method    : weighted_sum(기본) = w_conversion·convN + w_impact·impN (보상적: 한쪽이 높으면 상쇄)
                geometric          = √(convN·impN)                      (비보상적: 둘 다 높아야 상위)
    normalize : rank(기본, 이상치 강건) | minmax(크기 보존)
    """
    method: str = "weighted_sum"
    normalize: str = "rank"
    w_conversion: float = 0.7
    w_impact: float = 0.3


@dataclass
class EffortConfig:
    """표본 기반 공수(Man-hour) 추정 설정.

    소수 오브젝트의 실측 시간(표본)으로 보정한 뒤 전체 점수로 총 공수를 통계 추정한다.
      - P50(중앙값): band(밴드별 평균) | linear(점수 선형회귀) | ratio(점당 시간 비례)
      - 표본 없는 밴드는 전 표본 선형회귀로 보간
      - 영향도는 base 공수가 아니라 P90 리스크 버퍼에만 반영(이중계상 방지)
    feature 는 '크기量'만 허용(absolute/loc/complexity/dependency) — 순위값 final 은 회귀 부적합.
    """
    method: str = "band"                 # band | linear | ratio
    feature: str = "absolute"            # absolute | loc | complexity | dependency
    p90_multiplier: float = 1.5          # 표본 부족 시 P90 = P50 × 이 값
    impact_buffer: float = 0.3           # P90 추가버퍼 = ×(1 + impact_buffer·정규화영향도)
    fixed_overhead_hours: float = 0.0    # 프로젝트 고정 셋업(총합에 1회 가산)
    per_object_overhead_pct: float = 0.0  # per-object 통합/테스트 오버헤드(%)
    unit: str = "MH"                     # 표기 단위 라벨(MH/MD 등)


@dataclass
class ImpactWeights:
    """영향도(파급도) = call_in·(들어오는 호출 수) + package_in·(들어오는 패키지참조 수)
                      + grant_exposure·(GRANT 수신자 수).

    fan-in 이 클수록(=많은 오브젝트가 이걸 의존) 변경 시 파급이 크다 → 리팩토링 고위험.
    GRANT 는 스캔 밖 외부 소비자 존재를 뜻하므로 파급 요인으로 함께 계산."""
    call_in: float = 4.0
    package_in: float = 3.0
    grant_exposure: float = 3.0


@dataclass
class ScoreConfig:
    ref_weights: dict[str, float] = field(default_factory=_default_ref_weights)
    complexity: ComplexityWeights = field(default_factory=ComplexityWeights)
    composite: CompositeWeights = field(default_factory=CompositeWeights)
    impact: ImpactWeights = field(default_factory=ImpactWeights)
    final: FinalScoreConfig = field(default_factory=FinalScoreConfig)
    effort: EffortConfig = field(default_factory=EffortConfig)
    loc_divisor: float = 10.0      # volume_score = LOC / loc_divisor
    # 절대점수 난이도 밴드 (오름차순 상한, 마지막은 그 이상)
    band_thresholds: list[tuple[float, str]] = field(default_factory=lambda: [
        (20.0, "낮음"),
        (50.0, "보통"),
        (100.0, "높음"),
        (float("inf"), "매우높음"),
    ])

    def band_for(self, score: float) -> str:
        for upper, label in self.band_thresholds:
            if score < upper:
                return label
        return self.band_thresholds[-1][1]


def _merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: str | Path | None) -> ScoreConfig:
    """YAML 설정 로드. 경로 미지정/파일 부재 시 기본값."""
    cfg = ScoreConfig()
    if path is None:
        return cfg
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"config not found: {p}")
    import yaml
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}

    if "ref_weights" in raw and isinstance(raw["ref_weights"], dict):
        cfg.ref_weights = _merge(cfg.ref_weights, {str(k).upper(): float(v)
                                                   for k, v in raw["ref_weights"].items()})
    if "complexity" in raw and isinstance(raw["complexity"], dict):
        for k, v in raw["complexity"].items():
            if hasattr(cfg.complexity, k):
                setattr(cfg.complexity, k, float(v))
    if "composite" in raw and isinstance(raw["composite"], dict):
        for k, v in raw["composite"].items():
            if hasattr(cfg.composite, k):
                setattr(cfg.composite, k, float(v))
    if "impact" in raw and isinstance(raw["impact"], dict):
        for k, v in raw["impact"].items():
            if hasattr(cfg.impact, k):
                setattr(cfg.impact, k, float(v))
    if "final" in raw and isinstance(raw["final"], dict):
        for k, v in raw["final"].items():
            if k in ("method", "normalize"):
                setattr(cfg.final, k, str(v))
            elif hasattr(cfg.final, k):
                setattr(cfg.final, k, float(v))
    if "effort" in raw and isinstance(raw["effort"], dict):
        for k, v in raw["effort"].items():
            if k in ("method", "feature", "unit"):
                setattr(cfg.effort, k, str(v))
            elif hasattr(cfg.effort, k):
                setattr(cfg.effort, k, float(v))
    if "loc_divisor" in raw:
        cfg.loc_divisor = float(raw["loc_divisor"]) or 1.0
    if "band_thresholds" in raw and isinstance(raw["band_thresholds"], list):
        bands: list[tuple[float, str]] = []
        for item in raw["band_thresholds"]:
            upper = float(item.get("upper", "inf")) if str(item.get("upper")) != "inf" else float("inf")
            bands.append((upper, str(item.get("label", "?"))))
        if bands:
            cfg.band_thresholds = bands
    return cfg
