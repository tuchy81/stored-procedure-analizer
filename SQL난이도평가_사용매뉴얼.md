# SQL 난이도 평가 도구 (sql-scorer) 사용 매뉴얼

`.sql` 파일(DDL 스크립트)만으로 DB 프로그램(Stored Procedure / Procedure / Function /
Package / Trigger 등)의 **복잡도·의존성 기반 객관적 난이도**를 수치화하고 Markdown
리포트로 산출하는 독립 실행 프로그램입니다.

> 기존 `sp-assessor` 가 DBA 딕셔너리 CSV 추출본을 입력으로 하는 'DB 비접속' 파이프라인이라면,
> `sql-scorer` 는 **소스 폴더의 `.sql` 파일만** 있으면 바로 동작합니다. 두 도구는 독립적입니다.

## 1. 무엇을 하는가

1. **폴더 스캔** — 지정 폴더(하위 폴더 포함)의 모든 `.sql` 을 읽어 DB 오브젝트를 식별
   (`CREATE [OR REPLACE] PROCEDURE/FUNCTION/PACKAGE[ BODY]/TRIGGER/TYPE[ BODY]/VIEW/...`).
   SQL\*Plus 종결자(`/`) 및 헤더 기준으로 오브젝트 본문 경계를 자동 분해 (패키지 spec/body 구분).
2. **의존성 분석** — 오브젝트 본문에서 참조를 종류별로 식별하고, 스캔 집합 내 오브젝트로 해석:
   - `TABLE` 로컬 테이블/뷰 (FROM/JOIN/INSERT INTO/UPDATE/DELETE/MERGE)
   - `CALL` 타 프로시저/함수 호출 · `PACKAGE` 타 패키지 참조(`PKG.member`)
   - `BUILTIN` 빌트인(`DBMS_*`/`UTL_*`) · `SEQUENCE`(`.NEXTVAL/.CURRVAL`)
   - `DB_LINK` DB Link 직접 참조(`obj@link`) · `GRANT` 외부 노출(`GRANT ... ON obj TO ...`)
3. **가산점수** — 참조 종류별 가중치 × 건수 합 = 의존성 성분.
4. **절대점수** — 코드분량(LOC) + 복잡도(분기/루프/SELECT·서브쿼리 중첩/DML/동적SQL/예외/서브프로그램)
   + 의존성 가산점을 가중 합산해 수치화하고 난이도 밴드로 분류.
5. **리포트(md)** — 요약, 평가 방법론(가중치 표), 오브젝트 리스트+절대점수, 참조 관계(Mermaid
   그래프+상세), 오브젝트별 점수 근거를 출력.

## 2. 설치

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
```

## 3. 실행

```bash
# 가장 기본 (기본 가중치)
sql-scorer --src ./db_sql --out ./report.md

# 저장소 포함 예시로 즉시 확인
sql-scorer --src examples/sql --out examples/sql/sql_difficulty_report.md

# 가중치 커스터마이즈 + 분석용 CSV 병행 출력
sql-scorer --src ./db_sql --out ./report.md \
           --config examples/sql/scoring_config.yaml \
           --csv-dir ./out_csv
```

| 옵션 | 설명 | 기본값 |
|---|---|---|
| `--src, -s` | `.sql` 파일 폴더 (필수) | — |
| `--out, -o` | 리포트(md) 출력 경로 | `sql_difficulty_report.md` |
| `--config, -c` | 가중치/밴드 YAML (선택) | 내장 기본값 |
| `--recursive / --no-recursive` | 하위 폴더 재귀 스캔 | 재귀 |
| `--csv-dir` | `objects.csv`/`references.csv` 병행 출력 폴더 | 미출력 |
| `--effort-sample` | 실측 공수 표본 CSV(`KEY,HOURS`) — 주면 전환 공수(P50/P90) 추정 (§4.3) | 미사용 |
| `--charts / --no-charts` | 점수 산포·공수 캘리브레이션·신뢰구간 차트 생성 (matplotlib 필요) | 생성 |
| `--html / --no-html` | MD 와 함께 섹션 이동(TOC)·확장/접기 되는 HTML 리포트도 생성 | 생성 |
| `--embed-images / --no-embed-images` | 차트 이미지를 HTML 내부에 인라인 임베드(단일 파일) | 임베드 |

## 4. 점수 산정 공식 (객관성·재현성)

```
절대점수 = w_volume·(LOC / loc_divisor)
         + w_complexity·(분기·루프·쿼리·쿼리중첩·DML·동적SQL·예외·서브프로그램 가중합)
         + w_dependency·(참조 종류별 가산점 합)
```

- 모든 가중치는 `--config` YAML 로 조정 가능하며, **실제 사용된 값이 리포트 §2 에 그대로 표기**되어
  점수 근거가 항상 재현/감사 가능합니다.
- 기본 참조 가산점: `DB_LINK 10 > REMOTE 8 > CALL 4 > PACKAGE 3 = GRANT 3 > BUILTIN 2 >
  TABLE 1 > SEQUENCE 0.5` (전환 위험·해석 난이도가 큰 참조일수록 높음).
- 기본 난이도 밴드: `<20 낮음, <50 보통, <100 높음, 그 이상 매우높음`.
- 설정 예시는 [`examples/sql/scoring_config.yaml`](./examples/sql/scoring_config.yaml) 참조.

### 4.1 두 개의 축 — 전환 난이도 vs 영향도(파급도)

절대점수는 **내보내는 의존(fan-out)** 기반이라 "이 오브젝트 하나를 옮기는 비용(전환 난이도)"을 잰다.
반면 리팩토링에서는 **들어오는 의존(fan-in)** 이 중요하다 — 많은 곳이 의존하는 오브젝트는 바꿀 때
파급(blast radius)이 크기 때문이다. 그래서 별도로 **영향도**를 계산한다.

```
영향도 = call_in·(들어오는 호출 수) + package_in·(들어오는 패키지참조 수) + grant_exposure·(GRANT 수신자 수)
리팩토링 우선순위 = √(절대점수 백분위 × 영향도 백분위)   # 두 축이 모두 높은 것을 상위로
```

- 소프트웨어 메트릭의 efferent(Ce)/afferent(Ca) coupling 에 대응한다. 절대점수=Ce 관점, 영향도=Ca 관점.
- 예: 4곳에서 호출되는 공용 함수는 전환난이도(코드분량)는 낮아도 **영향도는 최상위** — 시그니처를
  바꾸면 호출자 전부가 깨지므로 계약 유지·회귀 테스트가 최우선이다. 리포트 §6 에서 이 관점을 제시한다.

### 4.2 단일 최종점수 (0~100)

전환난이도(2~수백)와 영향도(0~수십)는 척도가 달라 그대로 더할 수 없다. 두 축을 각각 0~100 으로
정규화한 뒤 결합해 **하나의 최종점수**를 만든다 (리포트의 기본 정렬 기준).

```
최종점수 = ( w_conversion·정규화(전환난이도) + w_impact·정규화(영향도) ) / (w_conversion + w_impact)
기본값: w_conversion=0.7, w_impact=0.3, normalize=rank, method=weighted_sum
```

- **normalize** `rank`(기본): 순위 기반 정규화라 소수 초대형 오브젝트(이상치)에 강건하고, 영향도 0 은 0 으로 매핑. `minmax`: 크기 보존이지만 이상치 민감.
- **method** `weighted_sum`(기본): 보상적 가중합. `geometric`(√(전환·영향)): 두 축이 **모두** 높은 것만 상위로 올리는 비보상적 결합.
- 가중치를 `w_impact` 쪽으로 옮기면 리팩토링 위험(파급) 관점이, `w_conversion` 쪽으로 옮기면 순수 이관 공수 관점이 강해진다. 모두 `--config` 로 조정 가능.

### 4.3 전환 공수(Man-hour) 추정 — 표본 캘리브레이션

소수 오브젝트의 **실측 시간**만 주면, 그 표본으로 점수를 통계 보정해 **전체 공수**를 추정한다.

```bash
sql-scorer --src ./db_sql --out report.md --effort-sample actual_hours.csv
```

표본 CSV 형식 (`KEY,HOURS` — KEY 는 `OWNER.NAME` 또는 이름만, 패키지는 본문에 매칭):

```csv
KEY,HOURS
FIN.PKG_BILLING,48
FIN.PRC_RECONCILE,24
FIN.PRC_UPDATE_MEMO,3
```

산정 원칙 (통계적으로 합리적이도록):

- **P50(중앙 추정)** = `method=band`(기본) 이면 실측 표본의 **밴드별 평균**을 그 밴드 전체에 적용, 표본이 없는 밴드는 **점수 선형회귀로 보간**. (`linear`/`ratio` 방식도 선택 가능)
- **회귀 피처는 '크기量'(전환난이도·LOC 등)만** 쓴다 — 최종점수는 순위값이라 공수 회귀에 부적합.
- **실측 오브젝트는 P50=P90=실측값**(확정, 버퍼 미적용).
- **영향도(fan-in)는 base 공수에 더하지 않고 P90 리스크 버퍼로만** 반영(이중계상 방지, 최대 +`impact_buffer`).
- 총합 = Σ P50/P90 + 고정 오버헤드. 표본 수에 따라 신뢰도(HIGH/MEDIUM/LOW)를 표기.

> ⚠ **최종점수를 그대로 합산해 공수로 쓰지 말 것** — 최종점수는 0~100 순위값이라 크기·비례가 없고
> 영향도(리스크)가 섞여 있어 합산이 무의미하다. 공수는 반드시 실측 표본으로 시간 단위로 환산해야 한다.

## 5. 리포트 구성

1. **요약** — 오브젝트 수, 평균/최고 절대점수, 밴드 분포, Top 5
2. **평가 방법론** — 공식과 모든 가중치 표 (전환난이도·영향도 양축)
3. **오브젝트 리스트** — 절대점수 내림차순, 성분별(코드분량/복잡도/의존성) 분해 + 영향도 컬럼
4. **참조 관계** — 내부 호출 Mermaid 그래프 + 오브젝트별 참조 상세
5. **점수 근거** — 오브젝트마다 지표·성분별 산식·합계 + 영향도(피호출·GRANT노출) 명시
6. **리팩토링 관점** — 영향도(fan-in) 랭킹, 리팩토링 우선순위, 핵심 허브 콜아웃
7. **전환 공수 추정** — (`--effort-sample` 제공 시) 밴드별 보정, 오브젝트별 P50/P90, 총 공수

### 시각화 (matplotlib 설치 시 자동)

`pip install -e ".[viz]"` 후 실행하면 리포트에 PNG 차트가 임베드된다 (리포트 옆 `charts/` 폴더).
`--no-charts` 로 끌 수 있고, matplotlib 미설치 시 그래프 없이 리포트만 생성된다.

- **점수 산포도** (§3): x=전환난이도, y=영향도, 점 크기 ∝ 최종점수, 밴드별 색 — 좌상단=파급 큰 허브
- **의존성 그래프** (§4.1): 오브젝트 간 내부 호출/참조 방향 그래프(색=밴드, 실선=호출, 점선=패키지참조)
- **공수 캘리브레이션 + 예측구간** (§7): 실측 표본·회귀선·**80% 예측구간(통계적 신뢰구간)**·전체 예상 P50
- **오브젝트별 P50–P90 범위** (§7): 오브젝트마다 예상 공수 구간(검정=실측 확정, 파랑=추정)

### 출력 형식 (MD + HTML)

기본으로 **Markdown(`--out` 경로)** 과 **HTML(같은 이름 `.html`)** 을 함께 생성한다 (`--no-html` 로 끔).

- **HTML**: 상단 고정 **TOC 로 섹션 이동**, 섹션마다 **접기/펼치기(확장)**, `모두 펼치기/접기` 버튼,
  라이트/다크 자동 대응, 표·차트 포함.
- **완전 오프라인 단일 파일**: 차트는 물론 **의존성 그래프까지 이미지로 HTML 내부에 base64 임베드**되어
  외부 링크/스크립트가 전혀 없다 (`http(s)` 참조 0). **HTML 한 파일만** 공유하면 오프라인에서 그대로 열린다.
  외부 링크 방식이 필요하면 `--no-embed-images`.
- 의존성 그래프는 matplotlib+networkx 로 PNG 렌더(색=밴드, 실선=호출, 점선=패키지참조). 두 라이브러리가
  없으면 mermaid 코드블록으로 폴백(GitHub 등에서 렌더, HTML 은 CDN 필요).
- MD 파일은 `charts/` 를 상대경로로 참조하므로 MD 공유 시엔 `charts/` 폴더를 함께 옮긴다.

## 6. 한계 (v0.1)

- ANTLR PL/SQL AST 미도입 — 정규식 휴리스틱 기반이라 매우 난해한 동적 SQL·매크로성
  코드에서 일부 참조/분기가 누락·근사될 수 있습니다.
- 예외 핸들러 `WHEN`, `CASE WHEN` arm 은 정규식으로 근사 구분합니다.
- 스키마 한정(`SCHEMA.OBJ`) 없이 이름만 겹치는 오브젝트는 이름 기준으로 해석합니다.
- 스캔 집합 밖(외부 스키마) 테이블/프로시저는 '미해석(외부)' 참조로 집계됩니다(`✓` 없음).
