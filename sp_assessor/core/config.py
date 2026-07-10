"""Config 로더 및 스키마 검증."""
from __future__ import annotations

import typing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class DependencyWeights:
    ref_table: float = 1
    fanout_sp: float = 3
    fanin_sp: float = 2
    fanin_app: float = 2
    db_link: float = 10
    trigger_caller: float = 5
    scheduler_caller: float = 3
    cross_schema_grant: float = 4


@dataclass
class ComplexityWeights:
    loc_per_100: float = 1
    branch: float = 1
    cursor: float = 2
    ref_cursor_out: float = 4
    dynamic_sql_literal: float = 4
    dynamic_sql_variable: float = 10
    tx_control: float = 5
    autonomous_tx: float = 10
    oracle_feature: float = 5
    out_param: float = 2
    exception_handler: float = 1
    global_pkg_var: float = 5
    mutating_trigger_risk: float = 6


@dataclass
class Weights:
    dependency: DependencyWeights = field(default_factory=DependencyWeights)
    complexity: ComplexityWeights = field(default_factory=ComplexityWeights)


@dataclass
class QuadrantConfig:
    d_threshold_pct: float = 60
    c_threshold_pct: float = 60
    boundary_band_pct: float = 5


@dataclass
class StrategyRule:
    max_loc: int | None = None
    max_branch: int | None = None
    max_cursor: int | None = None
    dynamic_sql: int | None = None
    dynamic_sql_literal: int | None = None
    db_link: int | None = None


@dataclass
class StrategyRules:
    auto_simple: StrategyRule = field(default_factory=lambda: StrategyRule(
        max_loc=50, max_branch=3, max_cursor=0, dynamic_sql=0, db_link=0
    ))
    auto_assisted: StrategyRule = field(default_factory=lambda: StrategyRule(
        max_loc=200, max_branch=10, max_cursor=2, dynamic_sql_literal=3, db_link=0
    ))


@dataclass
class CalibrationConfig:
    pilot_file: str = "override/pilot_effort.csv"
    min_samples: int = 30
    model: str = "ridge"
    ridge_alpha: float = 1.0
    interval: str = "p50_p90"


@dataclass
class ParserConfig:
    primary: str = "regex"           # v0.1 은 regex 단독. antlr4-plsql 는 별 스파이크
    fallback: str = "regex"
    target_success_rate: float = 0.90


@dataclass
class GraphConfig:
    community_algorithm: str = "louvain"   # leiden 은 optional dep
    random_seed: int = 42


@dataclass
class CharacterSetConfig:
    source_db: str = "AL32UTF8"
    target: str = "UTF-8"


@dataclass
class SecurityConfig:
    mask_dblink_host: bool = False


@dataclass
class Config:
    target_schemas: list[str] = field(default_factory=list)
    exclude_name_patterns: list[str] = field(default_factory=list)
    character_set: CharacterSetConfig = field(default_factory=CharacterSetConfig)
    weights: Weights = field(default_factory=Weights)
    quadrant: QuadrantConfig = field(default_factory=QuadrantConfig)
    strategy_rules: StrategyRules = field(default_factory=StrategyRules)
    calibration: CalibrationConfig = field(default_factory=CalibrationConfig)
    parser: ParserConfig = field(default_factory=ParserConfig)
    graph: GraphConfig = field(default_factory=GraphConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)


def _dict_to_dataclass(data: dict[str, Any], cls: type) -> Any:
    """중첩 dataclass 로 변환.

    `from __future__ import annotations` 로 인해 dataclass 필드의 `.type` 은
    런타임에 문자열("ParserConfig")로만 남는다. `typing.get_type_hints` 로
    실제 클래스 객체를 복원해야 중첩 dataclass 가 dict 그대로 남지 않는다.
    """
    if data is None:
        return cls()
    kwargs = {}
    resolved_hints = typing.get_type_hints(cls)
    for name, f in cls.__dataclass_fields__.items():
        if name not in data:
            continue
        value = data[name]
        field_type = resolved_hints.get(name, f.type)
        if hasattr(field_type, "__dataclass_fields__"):
            kwargs[name] = _dict_to_dataclass(value, field_type)
        else:
            kwargs[name] = value
    return cls(**kwargs)


def load_config(path: str | Path) -> Config:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"config not found: {path}")
    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    return _dict_to_dataclass(raw, Config)


def default_config() -> Config:
    return Config()
