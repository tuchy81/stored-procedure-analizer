"""sqlscore 데이터 모델."""
from __future__ import annotations

from dataclasses import dataclass, field

# PL/SQL '프로그램' 오브젝트 (난이도 점수 대상)
PROGRAM_TYPES = {"PROCEDURE", "FUNCTION", "PACKAGE", "PACKAGE_BODY", "TRIGGER", "TYPE_BODY"}
# 스캔은 하되 점수 대상은 아닌 오브젝트 (참조 대상/정의로만 취급)
NONPROGRAM_TYPES = {"VIEW", "MATERIALIZED_VIEW", "TYPE", "TABLE", "SEQUENCE", "INDEX", "SYNONYM"}

# 참조(reference) 종류
REF_TYPES = ("TABLE", "CALL", "PACKAGE", "DB_LINK", "GRANT", "BUILTIN", "SEQUENCE", "REMOTE")


@dataclass
class SourceObject:
    """.sql 에서 식별한 단일 DB 오브젝트."""
    owner: str | None
    name: str
    otype: str                       # 정규화된 타입 (PROCEDURE, PACKAGE_BODY, ...)
    file: str                        # 스캔 루트 기준 상대경로
    start_line: int
    end_line: int
    lines: list[tuple[int, str]] = field(default_factory=list)  # (원본 라인번호, 텍스트)

    @property
    def key(self) -> str:
        """오브젝트 고유 식별자. PACKAGE / PACKAGE_BODY 는 같은 이름이라도 구분."""
        base = f"{self.owner}.{self.name}" if self.owner else self.name
        base = base.upper()
        if self.otype in ("PACKAGE_BODY", "TYPE_BODY"):
            return f"{base}:{self.otype}"
        return base

    @property
    def display(self) -> str:
        base = f"{self.owner}.{self.name}" if self.owner else self.name
        return base.upper()

    @property
    def is_program(self) -> bool:
        return self.otype in PROGRAM_TYPES


@dataclass
class Reference:
    """오브젝트 하나가 밖으로 내보내는 참조(의존) 1건."""
    src_key: str
    target: str                      # 참조 대상 표시명
    rtype: str                       # REF_TYPES 중 하나
    resolved: bool = False           # 스캔 집합 내 오브젝트로 해석됐는지
    target_key: str | None = None    # resolved 인 경우 대상 오브젝트 key
    detail: str = ""
    count: int = 1


@dataclass
class Metrics:
    """오브젝트 정적 지표."""
    loc: int = 0
    branch_count: int = 0            # IF/ELSIF/CASE/WHEN
    loop_count: int = 0              # LOOP/WHILE/FOR
    query_count: int = 0             # SELECT (서브쿼리 포함) 개수
    max_query_nesting: int = 0       # 서브쿼리 중첩 깊이
    dml_count: int = 0               # INSERT/UPDATE/DELETE/MERGE
    dynamic_sql_count: int = 0       # EXECUTE IMMEDIATE / DBMS_SQL
    exception_handler_count: int = 0
    when_others: int = 0
    subprogram_count: int = 0        # 패키지/타입 바디 내부 프로시저·함수 수
    call_count: int = 0              # 타 프로그램 호출 수 (참조에서 파생)

    @property
    def cyclomatic(self) -> int:
        """분기/루프/예외 기반 근사 순환복잡도 (기저 1)."""
        return 1 + self.branch_count + self.loop_count + self.exception_handler_count


@dataclass
class Score:
    """오브젝트별 점수 및 산정 근거.

    두 축을 분리한다:
      - absolute_score  : 전환 난이도 (이 오브젝트가 내보내는 의존 fan-out 기반) = 자기 전환 비용
      - impact_score    : 영향도/파급도 (이 오브젝트로 들어오는 의존 fan-in 기반) = 변경 시 blast radius
      - final_score     : 두 축을 정규화·결합한 단일 최종점수 (0~100)
    """
    key: str
    volume_score: float = 0.0        # 코드분량 성분
    complexity_score: float = 0.0    # 분기/루프/쿼리 성분
    dependency_score: float = 0.0    # 참조 가산점수 성분 (fan-out)
    absolute_score: float = 0.0      # = volume + complexity + dependency (전환 난이도)
    band: str = ""
    impact_score: float = 0.0        # fan-in 가중합 (영향도)
    final_score: float = 0.0         # 0~100 (전환난이도·영향도 정규화 결합 단일점수)
    breakdown: dict = field(default_factory=dict)  # 근거 상세 (성분별 항목)


@dataclass
class EffortEstimate:
    """표본 캘리브레이션 기반 오브젝트별 공수 추정."""
    key: str
    p50: float | None = None         # 중앙 추정(시간)
    p90: float | None = None         # 상한 추정(리스크 버퍼 포함)
    basis: str = ""                  # 산출 근거(실측/밴드/선형보간/추정불가)
    measured: float | None = None    # 실측 표본이면 그 값
