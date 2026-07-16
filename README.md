# sp-assessor

Oracle 저장프로시저(SP) → Spring Boot(MyBatis) 백엔드 전환을 위한 **DB 비접속 사전평가 자동화 도구**입니다. DBA 딕셔너리/앱 소스에서 뽑은 CSV만으로 SP 인벤토리 정규화, 정적 분석, 의존성 그래프, 전환 난이도 점수화, 의사결정용 리포트까지 한 번에 산출합니다.

> 상세 설계는 [`SP전환평가_프로그램스펙.md`](./SP전환평가_프로그램스펙.md), 입력 데이터 추출 SQL 은 [`SP전환평가_추출스크립트.md`](./SP전환평가_추출스크립트.md), 실행 절차는 [`SP전환평가_사용매뉴얼.md`](./SP전환평가_사용매뉴얼.md) 를 참조하세요.

## 왜 필요한가

- **DB 비접속 분석**: 운영 DB에 직접 붙지 않고, 미리 추출한 CSV만으로 분석 (부하·보안 이슈 차단)
- **사람 판단 개입(Human-in-the-loop)**: 자동 분석이 놓치는 부분(동적 SQL, 미해석 시노님 등)을 `override/` 파일로 보정하고 감사 이력을 남김
- **재현 가능**: 동일 입력 + 동일 override + 동일 config → 동일 출력 (스냅샷/diff 로 변경 추적)

## 파이프라인

```
config.yaml + input/*.csv
        │
        ▼
  ┌─────────┐   인벤토리 정규화, 시노님/DB Link 해석,
  │   S1    │   패키지 해체(#오버로드), Grant 매트릭스
  └────┬────┘
       ▼
  ┌─────────┐   LOC/분기/커서/동적SQL/트랜잭션/Oracle 전용기능
  │   S2    │   지표 산출 (regex 파서 티어), SQL 문 인벤토리
  └────┬────┘
       ▼
  ┌─────────┐   SP/TABLE/REMOTE/APP/TRIGGER/JOB 그래프,
  │   S3    │   Grant 검증, SCC/Wave, 커뮤니티 탐지(Louvain)
  └────┬────┘
       ▼
  ┌─────────┐   (D,C) 백분위 가중합 점수화, 4분면 분류,
  │   S4    │   AUTO_SIMPLE~DEFER 전략 태깅, 공수 예측(P50/P90)
  └────┬────┘
       ▼
  ┌─────────┐   총괄 요약, 로드맵, 전체 인벤토리 xlsx, 4분면 산점도,
  │   S5    │   DB Link 절단점, override 감사, 리스크 레지스터
  └─────────┘
```

각 단계 산출물 중 **"검토 필수"로 표시된 파일**(`s1_unresolved.csv`, `s2_dynsql_hints.csv`, `s3_review.csv`, `s4_review.csv`)은 `override/*.csv` 로 보정하고 재실행하는 것이 정상 워크플로우입니다.

## 빠른 시작

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[scoring,viz,dev]"

# 저장소에 포함된 최소 예시로 전체 파이프라인 실행
sp-assessor validate --root examples
sp-assessor run --stage all --root examples --tag demo
```

`examples/output/` 아래 S1~S5 전 산출물이 생성됩니다. 실제 프로젝트에 적용하는 법은 [사용 매뉴얼](./SP전환평가_사용매뉴얼.md) 을 따라가세요.

## 커맨드

| 커맨드 | 용도 |
|---|---|
| `sp-assessor spike` | §0 선행 스파이크 — 파서 성공률/동적SQL 리터럴 비율/.NET 매칭률 임계 검증 |
| `sp-assessor validate` | 입력·override CSV 스키마 및 정합성 검증 (§5 규칙) |
| `sp-assessor run --stage all\|s1..s5 [--tag]` | 단계별 실행 + 스냅샷 태깅 |
| `sp-assessor diff --stage s1..s4 --from --to` | 스냅샷 간 신규/삭제/전략변경/공수변동 비교 |
| `sp-assessor override lint` | override 파일 중복 SP_ID·상충 ACTION·REASON 누락 검사 |

## 부가 도구: sql-scorer (.sql 직접 스캔 난이도 평가)

CSV 추출본이 아니라 **`.sql` 파일 폴더만으로** DB 프로그램(SP/Procedure/Function/Package/Trigger)의
복잡도·의존성 기반 난이도를 수치화하고 Markdown 리포트를 산출하는 **독립 실행 프로그램**입니다.

```bash
sql-scorer --src ./db_sql --out ./report.md            # 기본 가중치
sql-scorer --src examples/sql --out examples/sql/sql_difficulty_report.md
```

오브젝트 식별 → 참조 종류(테이블/호출/패키지/DB링크/GRANT/빌트인/시퀀스)별 가산점수 →
코드분량(LOC)·분기/루프/서브쿼리 중첩·동적SQL·예외 복잡도 → 절대점수/난이도 밴드 →
참조 관계(Mermaid) + 점수 근거 리포트. 상세는
[`SQL난이도평가_사용매뉴얼.md`](./SQL난이도평가_사용매뉴얼.md) 참조.

## 프로젝트 구조

```
sp_assessor/
├── cli/main.py          # Typer CLI 진입점
├── core/                # config 로더, 경로 레이아웃, 로깅
├── io/                   # CSV 스키마 정의, 로드/저장
├── stages/               # s1_inventory ~ s5_report, spike, diff, override_lint, validate
└── util/                 # 공용 유틸 (NaN-안전 문자열 정규화 등)
sqlscore/                 # 부가 도구: .sql 직접 스캔 난이도 평가 (sql-scorer)
├── cli.py               # Typer CLI 진입점
├── parser.py            # .sql 스캔·오브젝트 식별·본문 경계
├── metrics.py           # LOC/분기/루프/서브쿼리/동적SQL/예외 지표
├── dependencies.py      # 참조 종류별 의존성 분석
├── scoring.py           # 가산점수 + 절대점수 + 영향도 + 최종점수
├── effort.py            # 표본 캘리브레이션 공수(P50/P90) 추정
├── viz.py               # 점수 산포·공수 캘리브레이션·신뢰구간 차트(matplotlib, optional)
├── report.py            # Markdown 리포트
└── htmlreport.py        # MD→HTML(섹션 이동/확장) 변환
examples/                 # 최소 재현용 입력 데이터 + config.yaml (테스트 픽스처 겸용)
examples/sql/             # sql-scorer 예시 .sql + 샘플 리포트/설정
tests/                    # pytest — 각 stage end-to-end 검증
```

## 현재 범위 (v0.1)

- **파서**: ANTLR PL/SQL 문법은 아직 미도입 — `config.parser.primary=regex` 고정, 3단 신뢰도(AST/PARTIAL/REGEX) 중 REGEX/PARTIAL 만 실동작. `spike` 실행 시 이 사실이 리포트에 명시됩니다.
- **커뮤니티 탐지**: 기본값 `louvain` 은 networkx 내장 구현 사용 (추가 설치 불필요). `leiden` 은 `pip install -e ".[community]"` 필요, 미설치 시 자동 폴백.
- **캘리브레이션**: `scikit-learn`/`numpy` 설치 시에만 Ridge 회귀 + VIF + 부트스트랩 P50/P90 수행, 미설치·표본부족 시 휴리스틱 공수 추정치로 대체.
- **리포트 xlsx/svg**: `openpyxl`/`matplotlib` 설치 시에만 생성.

## 개발

```bash
pip install -e ".[dev,scoring,viz]"
pytest -q            # 71+ tests
ruff check sp_assessor/ tests/
```

## 라이선스

MIT
