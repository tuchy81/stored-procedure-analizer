# SP → 백엔드 전환 평가 자동화 — 입력데이터 정의 및 프로그램 스펙

| 항목 | 내용 |
|---|---|
| 문서 버전 | v2.0 (Draft, 개선 반영) |
| 대상 | Oracle 저장프로시저 → Spring Boot(MyBatis) 전환 사전평가 |
| 프로그램명(가칭) | `sp-assessor` |
| 실행 환경 | Python 3.11+, 오프라인 배치 실행 (DB 직접 접속 불필요 — CSV 입력 기반) |
| v1.0 대비 변경 | 파서 벤치마크 절차, 동적 SQL 처리 정책, 캘리브레이션 통계 절차, AUTO 2단화, CSV 오버플로 분리, 롤 재귀 전개, .NET Roslyn 분석기, 스냅샷·override 병합·보안 정책 명문화 |

---

## 0. 선행 스파이크 (구현 착수 전 필수 48h 검증)

본 스펙은 아래 3개 검증치가 임계 이상일 때 **원안 그대로 실효성이 담보**됩니다. 임계 미달 시 §4 해당 단계 재설계를 우선합니다.

| # | 스파이크 항목 | 임계값 | 미달 시 대응 |
|---|---|---|---|
| SP-1 | 대상 스키마 100건 샘플에 대한 ANTLR PL/SQL 파서 성공률 | ≥ 90% | §S2 파서 폴백 비율 상향, `PARSE_MODE` 세분화 (`AST` / `PARTIAL` / `REGEX`) |
| SP-2 | `EXECUTE IMMEDIATE` 사용 SP 중 리터럴 조립 비율 | ≥ 60% | §S2-5 후보 추출 대신 override 강제 전환 |
| SP-3 | .NET grep 결과의 `SP_NAME_RAW` 매칭률 | ≥ 70% | §S2-8 Roslyn 정적 분석기 필수 채택 |

스파이크 결과는 `output/_spike/spike_report.md` 에 기록하고, 이후 실행에서 임계 변동 시 알림.

---

## 1. 설계 원칙

- **DB 비접속 분석**: 추출(SQL)과 분석(Python) 완전 분리 — 운영 DB 부하·보안 이슈 차단
- **단계별 중간 산출 + 사람 보정 (Human-in-the-loop)**: `override/` 보정 파일이 원본 데이터보다 우선
- **멱등성**: 동일 입력 + 동일 override + 동일 config → 동일 출력. 실행 스냅샷은 `output/_snapshots/{tag}/` 에 저장
- **DB Link / 계정 간 Grant / 롤 계층 인식**: 원격·타 스키마 참조를 별도 노드 유형으로, 롤은 재귀 전개
- **감사 가능성**: 자동 산출과 사람 판단을 산출물에 `*` 마커로 구분. 모든 override에 `REASON` 필수
- **보안**: 산출물은 원본 소스코드·인프라 정보 포함 → §7 보안 정책 준수

---

## 2. 입력데이터 정의

> 추출 SQL은 별도 문서 『SP전환평가_추출스크립트.md』 참조. 모든 파일은 **UTF-8**, CSV(헤더 필수), 구분자 `,`, RFC 4180 준수 (텍스트 필드 큰따옴표 감쌈, 내부 `"`는 `""`로 이스케이프). 대용량 LONG/CLOB 필드는 CSV 셀에 담지 않고 별도 텍스트 파일로 분리(§2.5).

### 2.1 필수 입력 (DB 딕셔너리 유래)

| 파일명 | 원천 | 스키마(컬럼) | 용도 |
|---|---|---|---|
| `in_objects.csv` | DBA_OBJECTS | OWNER, OBJECT_NAME, OBJECT_TYPE, STATUS, CREATED, LAST_DDL_TIME | SP/함수/패키지/트리거 인벤토리 |
| `in_source.csv` | DBA_SOURCE | OWNER, NAME, TYPE, LINE, TEXT | 소스 전문 (정적 분석 원천) |
| `in_dependencies.csv` | DBA_DEPENDENCIES | OWNER, NAME, TYPE, REFERENCED_OWNER, REFERENCED_NAME, REFERENCED_TYPE, REFERENCED_LINK_NAME, DEPENDENCY_TYPE | 컴파일 시점 의존성 그래프 |
| `in_arguments.csv` | DBA_ARGUMENTS | OWNER, PACKAGE_NAME, OBJECT_NAME, **OVERLOAD**, ARGUMENT_NAME, POSITION, DATA_TYPE, IN_OUT, **PLS_TYPE** | 파라미터 프로파일 (오버로드·REF CURSOR OUT 탐지) |
| `in_synonyms.csv` | DBA_SYNONYMS | OWNER, SYNONYM_NAME, TABLE_OWNER, TABLE_NAME, DB_LINK | 시노님 해석 |
| `in_db_links.csv` | DBA_DB_LINKS | OWNER, DB_LINK, USERNAME, HOST | 원격 노드 식별 |
| `in_tab_privs.csv` | DBA_TAB_PRIVS | GRANTEE, OWNER, TABLE_NAME, PRIVILEGE, GRANTABLE, TYPE | 계정 간 Grant |
| `in_role_privs.csv` | DBA_ROLE_PRIVS (재귀 전개) | GRANTEE, GRANTED_ROLE, DEPTH, PATH | **롤 계층 재귀 전개 결과** — Grant 검증 필수 |
| `in_triggers.csv` | DBA_TRIGGERS | OWNER, TRIGGER_NAME, TABLE_OWNER, TABLE_NAME, STATUS, **BODY_FILE** | 트리거 인벤토리 (본문은 §2.5 분리 파일) |
| `in_scheduler_jobs.csv` | DBA_SCHEDULER_JOBS + DBA_JOBS | OWNER, JOB_NAME, JOB_TYPE, **ACTION_FILE**, ENABLED, SCHEDULE_TEXT | 배치 호출 SP (본문은 §2.5 분리 파일) |

### 2.2 필수 입력 (애플리케이션 유래)

| 파일명 | 원천 | 스키마 | 용도 |
|---|---|---|---|
| `in_app_calls.csv` | .NET Roslyn 정적 분석기 (추출 스크립트 §8, 스파이크 SP-3에 따라 grep 폴백 허용) | REPO, FILE_PATH, LINE_NO, SP_NAME_RAW, SP_NAME_RESOLVED, CALL_KIND, CALL_SNIPPET, CONFIDENCE | 앱 → SP 호출 인벤토리 |
| `in_app_constants.csv` | .NET 상수 클래스 스캔 | REPO, FILE_PATH, CONST_NAME, CONST_VALUE | SP명이 상수로 분리된 경우 매핑 |

### 2.3 선택 입력 (있으면 정밀도 향상)

| 파일명 | 원천 | 용도 |
|---|---|---|
| `in_plscope_identifiers.csv` | DBA_IDENTIFIERS (PL/Scope) | 문장 단위 정밀 참조 분석 |
| `in_exec_stats.csv` | DBA_HIST_SQLSTAT / V$DB_OBJECT_CACHE | 실행 빈도 → `SUSPECT_UNUSED` 플래그 |
| `in_remote_objects__{DB_LINK}.csv` | 원격 DB에서 §1/§3 스크립트 동일 실행 | DB Link 건너편 객체 실체 확인 |
| `in_awr_topsql.csv` | DBA_HIST_SQL_PLAN | 고비용 SQL 소유 SP 식별 (성능 리스크 지표) |

### 2.4 설정 입력

| 파일명 | 형식 | 내용 |
|---|---|---|
| `config.yaml` | YAML | 대상 스키마, 지표 가중치, 임계값, 제외 패턴, 분면 경계, 캘리브레이션 정책 |

```yaml
# config.yaml 예시
target_schemas: [APP_OWNER, BATCH_OWNER]
exclude_name_patterns: ["^TMP_", "_BAK$", "^TEST_"]
character_set:
  source_db: KO16MSWIN949           # 원본 DB 캐릭터셋 (KO16MSWIN949, AL32UTF8 등)
  target: UTF-8                     # 산출물 인코딩
weights:
  dependency:
    ref_table: 1
    fanout_sp: 3
    fanin_sp: 2
    fanin_app: 2
    db_link: 10
    trigger_caller: 5
    scheduler_caller: 3
    cross_schema_grant: 4
  complexity:
    loc_per_100: 1
    branch: 1
    cursor: 2
    ref_cursor_out: 4              # REF CURSOR OUT (전환 시 페이지네이션·스트리밍 재설계)
    dynamic_sql_literal: 4         # EXECUTE IMMEDIATE 리터럴 조립
    dynamic_sql_variable: 10       # EXECUTE IMMEDIATE 변수 조립 (분석 불가 → override 필수)
    tx_control: 5
    autonomous_tx: 10
    oracle_feature: 5              # CONNECT BY, MERGE, UTL_*, DBMS_* 등
    out_param: 2
    exception_handler: 1
    global_pkg_var: 5
    mutating_trigger_risk: 6       # 트리거 → 자기 테이블 재귀 참조
quadrant:
  d_threshold_pct: 60
  c_threshold_pct: 60
  boundary_band_pct: 5             # 경계선 ±5% → s4_review.csv 자동 편입
strategy_rules:
  auto_simple:                      # 완전 자동 변환 후보
    max_loc: 50
    max_branch: 3
    max_cursor: 0
    dynamic_sql: 0
    db_link: 0
  auto_assisted:                    # 반자동 (템플릿 매크로 + 사람 검토)
    max_loc: 200
    max_branch: 10
    max_cursor: 2
    dynamic_sql_literal: 3
    db_link: 0
calibration:
  pilot_file: override/pilot_effort.csv
  min_samples: 30                   # 최소 표본 (미만이면 회귀 미실행, 안내만)
  model: ridge                      # ridge | lasso | ols (다중공선성 대응)
  ridge_alpha: 1.0
  interval: p50_p90                 # 신뢰구간 산출
parser:
  primary: antlr4-plsql             # 1차 파서 (스파이크 SP-1 결과 반영)
  fallback: regex
  target_success_rate: 0.90         # 미달 시 config validate WARN
graph:
  community_algorithm: leiden       # louvain(비결정) 대체
  random_seed: 42
```

### 2.5 대용량 필드 분리 파일 규약

LONG/CLOB (`TRIGGER_BODY`, `JOB_ACTION`, `PL/SQL WRAPPED`) 은 CSV 셀에 넣지 않고 별도 파일로 분리하여 CSV에는 상대경로만 기록:

```
input/
├── bodies/
│   ├── trigger/{OWNER}/{TRIGGER_NAME}.txt
│   ├── job/{OWNER}/{JOB_NAME}.txt
│   └── wrapped/{OWNER}/{OBJECT_NAME}.txt   # 참고용 원문 보존 (분석 불가)
```

- CSV의 `BODY_FILE` / `ACTION_FILE` 컬럼은 `bodies/...` 상대경로. NULL이면 본문 없음
- 대용량 필드 저장 규칙 미준수 시 `validate` 커맨드에서 ERROR

---

## 3. 디렉터리 구조 및 실행 인터페이스

```
sp-assessor/
├── config.yaml
├── input/
│   ├── in_*.csv
│   └── bodies/         # §2.5 분리 파일
├── override/           # 사람 보정 파일
├── output/
│   ├── _spike/         # 선행 스파이크 결과 (§0)
│   ├── _snapshots/     # 실행 스냅샷 (diff 용, tag별)
│   │   └── {tag}/      # 예: 20260709_v2 / git-sha
│   ├── s1_inventory/
│   ├── s2_metrics/
│   ├── s3_graph/
│   ├── s4_scoring/
│   └── s5_report/
└── logs/
```

CLI:

```bash
sp-assessor spike                              # §0 선행 스파이크만 실행
sp-assessor run --stage all [--tag <name>]     # 전체 실행 + 스냅샷 태깅
sp-assessor run --stage s3                     # 특정 단계 재실행 (이전 output 재사용)
sp-assessor validate                           # 입력/override 파일 스키마 검증
sp-assessor diff --stage s4 --from <tag_a> --to <tag_b>   # 스냅샷 간 diff
sp-assessor override lint                      # override 파일 정합성/충돌 검사
```

- 각 단계 실행 시 `logs/s{n}_run_{ts}.log` 에 처리 건수, WARN 요약, 대응 override 경로 기록
- **WARN 항목 = override 대상**이라는 규약은 전 단계 공통

### 3.1 실행 성능 목표

| 대상 SP 수 | 스테이지 별 목표 시간 (단일 노드, 8코어/32GB) |
|---|---|
| ~ 100 | S1~S5 합계 ≤ 30초 |
| ~ 1,000 | S1~S5 합계 ≤ 5분 |
| ~ 10,000 | S1~S5 합계 ≤ 45분 (S3 그래프 알고리즘이 병목) |
| > 10,000 | S3에 대해 graph-tool 대체 or 스키마 분할 실행 검토 |

---

## 4. 단계별 프로그램 스펙

### S1 — 인벤토리 정규화 및 식별자 해석

**목적**: 분석 대상 SP 확정 목록 + 정규 식별자 통일 (`OWNER.PKG.PROC[#OVERLOAD][@LINK]`)

**처리 로직**
1. `in_objects.csv` 에서 PROCEDURE / FUNCTION / PACKAGE BODY / TRIGGER 필터
2. `config.exclude_name_patterns` 적용 → 제외 목록은 `s1_excluded.csv` 에 사유 명시 (사일런트 제외 금지)
3. **패키지 해체**: `in_source.csv` 파싱으로 PACKAGE BODY 내부 서브프로그램 개별 단위(`PKG.PROC`) 분리. **오버로드는 `PKG.PROC#N` 접미**로 구분 (`in_arguments.csv` 의 OVERLOAD 활용). Nested subprogram / forward declaration 은 로컬로 분류(별도 지표 노드 아님)
4. **시노님 해석**: PUBLIC + private 병합, 우선순위 `private > public`. `db_link` 존재 시 REMOTE 태깅
5. **DB Link 해석**: `REFERENCED_LINK_NAME` 또는 소스 내 `@link` 패턴 → 노드 유형 `REMOTE`. `in_remote_objects__*.csv` 매칭 시 실체 부여, 없으면 `UNRESOLVED_REMOTE`
6. **Grant 경계 표시**: `in_tab_privs.csv` + `in_role_privs.csv` (재귀 전개 완료본) 조인 → SP별 "타 스키마 호출 가능 여부(`CROSS_SCHEMA_CALLABLE`)" 플래그
7. **동명 다중 스키마 SP 처리**: `SP_ID` 는 OWNER 포함이므로 물리적 충돌은 없음. 그러나 시노님/앱 호출에서 스키마 없이 호출되는 이름 충돌은 `s1_unresolved.csv` 에 `NAME_COLLISION` 사유로 기록 → override 강제
8. **미사용 후보 표시**: `in_exec_stats.csv` 존재 시 최근 N개월 실행 0건 SP → `SUSPECT_UNUSED` (자동 제외 금지)
9. **WRAP 소스 처리**: 추출 §10-(3) 에서 검출된 WRAP SP는 `WRAPPED=Y` 로 표시, S2 파싱 대상에서 제외, override 필수

**산출물**

| 파일 | 내용 |
|---|---|
| `s1_inventory.csv` | SP_ID, OWNER, PKG, NAME, OVERLOAD_NO, TYPE, LOC, STATUS, WRAPPED, CROSS_SCHEMA_CALLABLE, SUSPECT_UNUSED, REMOTE_REF_COUNT |
| `s1_excluded.csv` | 제외 객체 + 사유 |
| `s1_unresolved.csv` | **[검토 필수]** 미해석 시노님, UNRESOLVED_REMOTE, NAME_COLLISION, INVALID 상태, WRAP |
| `s1_grant_matrix.csv` | 롤 재귀 전개 후 최종 유효 Grant (GRANTEE, OWNER, OBJECT, PRIVILEGE, VIA_ROLES) |

**사람 보정 지점 → `override/s1_inventory_override.csv`**

| 컬럼 | 용도 |
|---|---|
| SP_ID | 대상 |
| ACTION | `INCLUDE` / `EXCLUDE` / `RENAME` / `RESOLVE_REMOTE` / `RESOLVE_NAME_COLLISION` |
| RESOLVED_TARGET | 실체 지정 (예: `REMOTE_DB.HR.CALC_PAY@FIN_LINK`) |
| REASON | 필수 (감사 추적용) |

---

### S2 — 정적 분석 (지표 산출)

**목적**: SP별 복잡성 원시 지표 산출 + 동적 SQL 참조 후보 추출

**처리 로직**
1. `in_source.csv` 를 SP_ID 단위 재조립 (S1 패키지 해체 경계 사용)
2. **전처리**: 주석(`--`, `/* */`) 및 문자열 리터럴 제거본 생성. LOC는 주석 제외 원본, 패턴 매칭은 제거본
3. **파서 계층**:
   - 1차: ANTLR PL/SQL grammar (config `parser.primary`) → 성공 시 `PARSE_MODE=AST`
   - 2차: 부분 성공 (선언부만 파싱, 본문 실패) → `PARSE_MODE=PARTIAL` (신뢰도 중)
   - 3차: 정규식 폴백 → `PARSE_MODE=REGEX` (신뢰도 하)
   - 파싱 성공률 < `parser.target_success_rate` 시 `logs/s2_run_*.log` 에 STAGE-WARN
4. **지표 추출**:
   - LOC, 분기 (IF/ELSIF/CASE/LOOP/WHILE/FOR)
   - **커서 세분화**: 명시적 커서 / 커서 FOR 루프 / SYS_REFCURSOR
   - **REF CURSOR OUT 지표**: `ref_cursor_out_count`, 반환 컬럼 프로파일 (파싱 가능한 경우)
   - **동적 SQL 이원 분리**:
     - `dynamic_sql_literal`: `EXECUTE IMMEDIATE '리터럴'` — 리터럴 파싱해 §S3 CANDIDATE 엣지로 활용
     - `dynamic_sql_variable`: `EXECUTE IMMEDIATE v_sql`, `EXECUTE IMMEDIATE 'X' || v` — 문자열 조립 참여 변수 이름을 `s2_dynsql_hints.csv` 에 기록 (분석 불가, override 대상)
   - 트랜잭션 제어: COMMIT/ROLLBACK/SAVEPOINT, `PRAGMA AUTONOMOUS_TRANSACTION`
   - Oracle 전용: CONNECT BY, MERGE, 분석함수 OVER, BULK COLLECT, FORALL, `UTL_*` / `DBMS_*` 패키지별 카운트
   - `@link` 직접 사용 (S1 결과와 대조)
   - 예외 핸들러, GOTO, 패키지 전역변수 참조
   - **트리거 재귀 리스크**: 트리거 본문이 자기 테이블 참조 → `mutating_trigger_risk=1`
5. **SQL 문 인벤토리**: DML 유형별 카운트 + 참조 테이블 추출 (S3 엣지 보강)

**산출물**

| 파일 | 내용 |
|---|---|
| `s2_metrics.csv` | SP_ID + 지표 40여 컬럼 + PARSE_MODE + PARSE_CONFIDENCE(0-1) |
| `s2_sql_inventory.csv` | SP_ID, STMT_SEQ, DML_TYPE, TABLES_RAW, IS_DYNAMIC, DYNAMIC_KIND(LITERAL/VARIABLE) |
| `s2_refcursor_profiles.csv` | SP_ID, ARG_POSITION, RETURN_COLUMNS(파싱 가능시), IS_ADHOC |
| `s2_dynsql_hints.csv` | **[검토 필수]** 변수 조립 동적 SQL의 참여 변수·근처 라인 스니펫 |
| `s2_parse_failures.csv` | **[검토 필수]** 파싱 실패/폴백 SP + 실패 지점 |
| `s2_parser_bench.csv` | 파서 성공률 자체 리포트 (스파이크 SP-1의 지속 관측 지표) |

**사람 보정 지점 → `override/s2_metrics_override.csv`**
- 컬럼: SP_ID, METRIC_NAME, VALUE, REASON
- 용도: 파싱 실패 SP 수동 계측, 동적 SQL 실참조 지정 (연동: `override/s2_dynsql_resolve.csv` 로 SRC_SP_ID, RESOLVED_TABLES, REASON)

---

### S3 — 의존성 그래프 구축

**목적**: 전환 순서·묶음 결정을 위한 그래프 분석

**처리 로직**
1. **노드**: S1 SP + 테이블/뷰 + REMOTE + APP(호출처 리포 단위) + TRIGGER / JOB
2. **엣지 소스 3원 통합 (신뢰도 표기)**:
   - `in_dependencies.csv` → `EDGE_CONFIDENCE=HIGH`
   - S2 동적 SQL 리터럴 파싱 결과 → `MEDIUM`
   - S2 변수 조립 힌트 → `LOW` (override 확정 전까지 잠정 엣지)
   - `in_app_calls.csv` (Roslyn) → `HIGH`, grep 폴백 → `MEDIUM`
   - 트리거/잡 본문 매칭 → `HIGH`
3. **Grant 검증**: SP→SP 호출 엣지에 대해 `s1_grant_matrix.csv` (롤 재귀 전개 후) 로 EXECUTE 권한 검증 → `GRANT_OK=Y/N`. `N` 인 경우 `GRANT_MISSING` 경고 → `s3_review.csv`
4. **DB Link 절단점**: 원격 노드 향 엣지는 `IS_DBLINK=Y` + `CUT_POINT=Y` 태깅
5. **그래프 분석**:
   - SCC 탐지 → 상호 재귀 묶음 = 단일 작업 단위 승격
   - 위상 정렬 (SCC 축약 후) → Wave 번호
   - Fan-in / Fan-out / 매개 중심성
   - **커뮤니티 탐지**: Leiden (config `graph.community_algorithm`, seed 고정) — 재현성 확보

**산출물**

| 파일 | 내용 |
|---|---|
| `s3_nodes.csv` | NODE_ID, NODE_TYPE(SP/TABLE/REMOTE/APP/TRIGGER/JOB), CLUSTER_ID |
| `s3_edges.csv` | SRC, DST, EDGE_TYPE, EDGE_CONFIDENCE(HIGH/MEDIUM/LOW), IS_DBLINK, GRANT_OK, CUT_POINT |
| `s3_scc.csv` | SCC_ID, MEMBER_SP_ID |
| `s3_waves.csv` | SP_ID, WAVE_NO |
| `s3_graph.mmd` / `s3_graph.svg` | 시각화 (클러스터별 분할) |
| `s3_review.csv` | **[검토 필수]** LOW confidence 엣지, GRANT_MISSING, 고아 노드, 대형 SCC |

**사람 보정 지점**

| 파일 | 용도 |
|---|---|
| `override/s3_edges_override.csv` | SRC, DST, ACTION(`ADD`/`REMOVE`/`CONFIRM`/`DOWNGRADE`), REASON |
| `override/s3_cluster_override.csv` | SP_ID, CLUSTER_ID, REASON (도메인 지식 반영) |

---

### S4 — 점수화 및 분류

**목적**: (D, C) 종합 점수 + 4분면 분류 + 전환 전략 태깅 + 신뢰구간 있는 공수 예측

**처리 로직**
1. S2 지표 + S3 그래프 지표 결합
2. **정규화**: 백분위(default) 또는 Min-Max
3. **가중합**: `D = Σ wi·di`, `C = Σ wj·cj`
4. **캘리브레이션 (선택)**:
   - `override/pilot_effort.csv` (SP_ID, ACTUAL_MD) 존재하고 `len(pilot) ≥ config.calibration.min_samples` 시 실행. 미달 시 미실행 + 안내 로그
   - 모델: Ridge (default) / Lasso — **다중공선성 대응** 필수. OLS 는 명시적 선택 시만
   - **공선성 진단 리포트**: VIF (Variance Inflation Factor) 상위 지표 자동 출력
   - 결과: 가중치 제안치 + Bootstrap 기반 P50/P90 예측 구간
5. **4분면 분류 + 전환 전략 태그** (2단화):
   - `AUTO_SIMPLE`: `strategy_rules.auto_simple` 룰 통과 (변환 템플릿 즉시 적용)
   - `AUTO_ASSISTED`: `strategy_rules.auto_assisted` 룰 통과 (템플릿 + 검토)
   - `SEMI`: 반자동 (커서 루프, 임시 테이블)
   - `MANUAL`: 수동 재작성
   - `DEFER`: 잔존 검토 (고D·고C, DB Link 다수, autonomous_tx, 대형 SCC)
6. SCC 멤버는 묶음 내 최고 등급으로 상향 통일
7. **경계선 자동 편입**: 4분면 경계 ±`config.quadrant.boundary_band_pct` 이내 SP는 `s4_review.csv` 자동 편입

**산출물**

| 파일 | 내용 |
|---|---|
| `s4_scores.csv` | SP_ID, D_SCORE, C_SCORE, QUADRANT, STRATEGY, WAVE_NO, CLUSTER_ID, EFFORT_EST_MD, EFFORT_P50, EFFORT_P90, CONFIDENCE |
| `s4_weight_suggestion.yaml` | 회귀 기반 가중치 제안 + VIF 리포트 (파일럿 있을 때) |
| `s4_review.csv` | **[검토 필수]** 경계선 SP, PARSE_MODE≠AST SP, SCC 상향 조정, DEFER 자동 판정 |

**사람 보정 지점 → `override/s4_strategy_override.csv`**
- 컬럼: SP_ID, STRATEGY, QUADRANT(선택), EFFORT_MD(선택), REASON

---

### S5 — 리포트 생성

**목적**: 의사결정용 최종 산출물

| 파일 | 내용 |
|---|---|
| `s5_summary.md` | 총괄: 대상 수, 분면/전략 분포, 총 예상 공수(P50/P90), 신뢰도 주석 |
| `s5_roadmap.csv` | Wave × Cluster 매트릭스 — Wave별 대상, 선행 조건, DB Link 절단점 |
| `s5_inventory_full.xlsx` | 전 SP 상세 (필터/정렬 가능한 통합 시트) |
| `s5_quadrant.svg` | (D, C) 산점도, 4분면, 전략 색상 |
| `s5_dblink_cutpoints.md` | DB Link 의존 SP + **대체 인터페이스 후보 제안** (REST/이벤트/View/Materialized View — 원격 객체 유형별 룰 매칭) |
| `s5_override_audit.md` | 전체 override 이력 감사 (누가·무엇을·왜) |
| `s5_risk_register.md` | WRAP, autonomous_tx, mutating trigger, 대형 SCC, LOW confidence 다수 클러스터 등 리스크 항목 집계 |

- 모든 리포트에 override 반영 여부 `*` 마커 표기

---

## 5. 데이터 품질 검증 규칙 (`validate` 커맨드)

| 규칙 | 수준 |
|---|---|
| 입력 CSV 헤더/타입 스키마 일치 | ERROR |
| `in_source.csv` 의 SP가 `in_objects.csv` 에 존재 | ERROR |
| `in_arguments.csv` 의 `OVERLOAD` 컬럼 존재 (없으면 오버로드 미구분) | ERROR |
| `in_role_privs.csv` 가 재귀 전개 완료본 | ERROR |
| override 파일의 SP_ID가 인벤토리에 존재 | ERROR |
| override에 REASON 누락 | ERROR |
| `in_triggers.csv` / `in_scheduler_jobs.csv` 의 `BODY_FILE` 경로 실재 | ERROR |
| 대용량 필드 CSV 셀 직접 담김 (§2.5 위반) | ERROR |
| INVALID 상태 객체 존재 | WARN |
| 의존성에 있으나 objects에 없는 참조 | WARN |
| DB Link 참조 있으나 `in_remote_objects__*.csv` 미제공 | WARN |
| 동명 SP 다중 스키마 존재 | WARN |
| WRAP 소스 SP 존재 | WARN |
| 파서 성공률 < `parser.target_success_rate` | WARN |
| 캘리브레이션 표본 < `min_samples` | INFO |

---

## 6. Override 파일 관리 정책

- **저장소**: 전체 `sp-assessor/` 디렉터리를 Git 리포지토리로 관리. `override/` 는 PR 리뷰 필수 경로
- **정합성 검사**: `sp-assessor override lint` — 중복 SP_ID, 상충 ACTION(예: EXCLUDE + INCLUDE), REASON 공백 검출
- **병합 규칙**: 여러 사람이 동시 편집 시 Git 병합. Semantic conflict (같은 SP_ID 서로 다른 ACTION) 는 lint 로 조기 검출
- **감사**: `s5_override_audit.md` 는 `git log` + `override/` 변경 이력 통합

---

## 7. 보안 및 데이터 취급 정책

| 산출물 | 민감도 | 취급 |
|---|---|---|
| `in_source.csv`, `input/bodies/` | 高 (IP 지분·비즈니스 로직 노출) | 사내 네트워크 한정, 저장소 접근 통제 (읽기 로그 감사) |
| `in_db_links.csv` | 高 (인프라 노출) | 저장 시 `HOST` 마스킹 옵션 (`config.security.mask_dblink_host: true`) |
| `in_tab_privs.csv`, `in_role_privs.csv` | 中 (권한 구조 노출) | 사내 한정 |
| `s5_*` 리포트 | 中 | 이해관계자 배포 시 워터마크, 배포 로그 관리 |

- 산출물 보관 기간: 프로젝트 종료 후 최대 1년, 이후 자동 삭제 스크립트
- 산출물에 개인정보 컬럼명(`RRN`, `SSN` 등) 노출 시 `s5_risk_register.md` 에 별도 경고

---

## 8. 문자셋 및 로케일

- 원본 DB 캐릭터셋이 `AL32UTF8` 이 아닌 경우 (KO16MSWIN949 등), 추출 단계에서 UTF-8 변환 필수 (『추출스크립트.md』 §0.3 참조)
- `config.character_set.source_db` 값과 실제 CSV BOM/인코딩이 불일치하면 `validate` ERROR
- 한글 식별자(Quoted Identifier `"고객정보"`) 등장 시 정규 식별자 정책에 따라 원문 보존 + Base64 별도 컬럼 병행 (테스트 필요 항목)

---

## 9. 스냅샷 및 재실행 (`diff` 커맨드)

- `--tag <name>` 옵션으로 실행 결과를 `output/_snapshots/{tag}/` 에 아카이브
- `sp-assessor diff --stage s4 --from v1 --to v2` : 두 스냅샷 비교. SP_ID 기준 신규/삭제/전략변경/공수변동 출력
- CI 연계 시 `--tag ${GIT_SHA}` 사용 권장

---

## 10. 운영 시나리오 (권장 반복 주기)

1. **스파이크(초회)**: `sp-assessor spike` → `output/_spike/spike_report.md` 검토 → 임계 미달 항목 재설계
2. **초회 전체**: 전 단계 실행 → S1/S2/S3/S4 review 파일 검토 → override 작성 → 재실행
3. **파일럿**: `AUTO_SIMPLE` + `AUTO_ASSISTED` 후보 각 15건 이상(총 ≥ 30) 실전환 → `pilot_effort.csv` 기록 → 가중치 캘리브레이션
4. **정기(분기)**: 추출 스크립트 재실행 → `diff --from prev_quarter --to current` 로 신규/변경 SP만 검토
5. **CI 연계(선택)**: 추출→분석→diff 파이프라인화, 신규 SP 등록 시 자동 평가 리포트 발행. PR 코멘트로 diff 요약 게시

---

## 부록 A. v1.0 → v2.0 변경 요약

| 항목 | v1.0 | v2.0 |
|---|---|---|
| 파서 신뢰도 | 이진(AST/REGEX) | 3단(AST/PARTIAL/REGEX) + 성공률 목표 명시 |
| 동적 SQL | 단일 지표 | 리터럴/변수 이원 분리 + 힌트 파일 |
| 파서 사전 검증 | 없음 | §0 스파이크 SP-1 |
| .NET 호출 인벤토리 | grep 단독 | Roslyn 우선 + grep 폴백 + 상수 클래스 매핑 |
| 오버로드 처리 | 미명시 | `SP_ID` 에 `#N` 접미 + `OVERLOAD` 컬럼 필수 |
| REF CURSOR OUT | out_param 통합 | 별도 지표 + 반환 프로파일 산출 |
| 롤 Grant 전개 | 1-hop | 재귀 전개 결과를 `in_role_privs.csv` 로 명시 |
| CSV 대용량 필드 | LONG 그대로 | 별도 파일 분리 (§2.5) |
| AUTO 전략 | 단일 | AUTO_SIMPLE / AUTO_ASSISTED 2단 |
| 공수 예측 | 점추정 | P50/P90 신뢰구간 |
| 캘리브레이션 | OLS 10건 | Ridge/Lasso + VIF, 최소 30건 |
| 커뮤니티 탐지 | Louvain | Leiden + seed 고정 (재현성) |
| 스냅샷/diff | 미정의 | `output/_snapshots/{tag}/` |
| 성능 목표 | 없음 | 규모별 처리시간 목표 |
| 문자셋 | 언급 | 원본 캐릭터셋 명시 + 변환 정책 |
| 보안 정책 | 없음 | §7 산출물 민감도별 취급 |
| Override 병합 | 없음 | Git + lint 커맨드 |
