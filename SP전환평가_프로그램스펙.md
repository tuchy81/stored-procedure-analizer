# SP → 백엔드 전환 평가 자동화 — 입력데이터 정의 및 프로그램 스펙

| 항목 | 내용 |
|---|---|
| 문서 버전 | v1.0 (Draft) |
| 대상 | Oracle 저장프로시저 → Spring Boot(MyBatis) 전환 사전평가 |
| 프로그램명(가칭) | `sp-assessor` |
| 실행 환경 | Python 3.11+, 오프라인 배치 실행 (DB 직접 접속 불필요 — CSV 입력 기반) |

---

## 1. 설계 원칙

- **DB 비접속 분석**: 추출(SQL) 단계와 분석(Python) 단계를 분리. 분석기는 CSV/텍스트 파일만 입력받음 → 운영 DB 부하·보안 이슈 차단
- **단계별 중간 산출 + 사람 보정(Human-in-the-loop)**: 각 단계는 `output/` 에 결과를 쓰고, 사람이 `override/` 에 보정 파일을 두면 다음 실행 시 **보정이 원본보다 우선 적용**
- **멱등성**: 동일 입력 + 동일 override → 동일 출력. 재실행 안전
- **DB Link / 계정 간 Grant 인식**: 원격 객체·타 스키마 객체를 별도 노드 유형으로 분류 (딕셔너리로 자동 해석 불가한 영역 = 보정 대상으로 명시적 표출)

---

## 2. 입력데이터 정의

> 추출 SQL은 별도 문서 『SP전환평가_추출스크립트.md』 참조. 모든 파일은 UTF-8, CSV(헤더 필수), 구분자 `,`, 텍스트 필드 큰따옴표 감쌈.

### 2.1 필수 입력 (DB 딕셔너리 유래)

| 파일명 | 원천 | 스키마(컬럼) | 용도 |
|---|---|---|---|
| `in_objects.csv` | DBA_OBJECTS | OWNER, OBJECT_NAME, OBJECT_TYPE, STATUS, CREATED, LAST_DDL_TIME | SP/함수/패키지/트리거 인벤토리 |
| `in_source.csv` | DBA_SOURCE | OWNER, NAME, TYPE, LINE, TEXT | 소스 전문 (정적 분석 원천) |
| `in_dependencies.csv` | DBA_DEPENDENCIES | OWNER, NAME, TYPE, REFERENCED_OWNER, REFERENCED_NAME, REFERENCED_TYPE, REFERENCED_LINK_NAME, DEPENDENCY_TYPE | 컴파일 시점 의존성 그래프 |
| `in_arguments.csv` | DBA_ARGUMENTS | OWNER, PACKAGE_NAME, OBJECT_NAME, ARGUMENT_NAME, POSITION, DATA_TYPE, IN_OUT | 파라미터 프로파일 (OUT/REF CURSOR 탐지) |
| `in_synonyms.csv` | DBA_SYNONYMS | OWNER, SYNONYM_NAME, TABLE_OWNER, TABLE_NAME, DB_LINK | 시노님 해석 (간접 참조 → 실체 매핑) |
| `in_db_links.csv` | DBA_DB_LINKS | OWNER, DB_LINK, USERNAME, HOST | DB Link 목록 (원격 노드 식별) |
| `in_tab_privs.csv` | DBA_TAB_PRIVS | GRANTEE, OWNER, TABLE_NAME, PRIVILEGE, GRANTABLE, TYPE | 계정 간 Grant — 스키마 경계 넘는 실행/참조 관계 |
| `in_triggers.csv` | DBA_TRIGGERS | OWNER, TRIGGER_NAME, TABLE_OWNER, TABLE_NAME, STATUS, TRIGGER_BODY | 트리거 → SP 호출 탐지 |
| `in_scheduler_jobs.csv` | DBA_SCHEDULER_JOBS + DBA_JOBS | OWNER, JOB_NAME, JOB_TYPE, JOB_ACTION, ENABLED, SCHEDULE_TEXT | 배치 호출 SP 식별 |

### 2.2 필수 입력 (애플리케이션 유래)

| 파일명 | 원천 | 스키마 | 용도 |
|---|---|---|---|
| `in_app_calls.csv` | .NET 소스 grep (추출 스크립트 §7) | REPO, FILE_PATH, LINE_NO, SP_NAME_RAW, CALL_SNIPPET | 애플리케이션 → SP 호출 인벤토리 (Fan-in의 앱 측면) |

### 2.3 선택 입력 (있으면 정밀도 향상)

| 파일명 | 원천 | 용도 |
|---|---|---|
| `in_plscope_identifiers.csv` | DBA_IDENTIFIERS (PL/Scope) | 문장 단위 정밀 참조 분석 (재컴파일 가능한 환경 한정) |
| `in_exec_stats.csv` | DBA_HIST_SQLSTAT / V$SQLAREA | 실행 빈도 → 사용 여부 판정, 미사용 SP 제외 후보 |
| `in_remote_objects.csv` | 원격 DB에서 동일 추출 실행 | DB Link 건너편 객체 실체 확인 (§4 S1 참조) |

### 2.4 설정 입력

| 파일명 | 형식 | 내용 |
|---|---|---|
| `config.yaml` | YAML | 대상 스키마 목록, 지표 가중치, 임계값, 제외 패턴, 분면 경계값 |

```yaml
# config.yaml 예시
target_schemas: [APP_OWNER, BATCH_OWNER]
exclude_name_patterns: ["^TMP_", "_BAK$", "^TEST_"]
weights:
  dependency:
    ref_table: 1
    fanout_sp: 3
    fanin_sp: 2
    fanin_app: 2
    db_link: 10
    trigger_caller: 5
    scheduler_caller: 3
    cross_schema_grant: 4      # 타 스키마가 EXECUTE grant로 호출하는 경우
  complexity:
    loc_per_100: 1
    branch: 1
    cursor: 2
    dynamic_sql: 8
    tx_control: 5
    autonomous_tx: 10
    oracle_feature: 5          # CONNECT BY, MERGE, UTL_*, DBMS_* 등
    out_param: 2
    exception_handler: 1
    global_pkg_var: 5
quadrant:
  d_threshold_pct: 60          # 백분위 기준 고/저 경계
  c_threshold_pct: 60
calibration:
  pilot_file: override/pilot_effort.csv   # 파일럿 실측 공수 (선택)
```

---

## 3. 디렉터리 구조 및 실행 인터페이스

```
sp-assessor/
├── config.yaml
├── input/            # §2 입력 CSV
├── override/         # 사람 보정 파일 (아래 단계별 정의)
├── output/
│   ├── s1_inventory/
│   ├── s2_metrics/
│   ├── s3_graph/
│   ├── s4_scoring/
│   └── s5_report/
└── logs/
```

CLI:

```bash
sp-assessor run --stage all            # 전체 실행
sp-assessor run --stage s3             # 특정 단계만 재실행 (이전 단계 output 재사용)
sp-assessor validate                   # 입력/override 파일 스키마 검증만 수행
sp-assessor diff --stage s4 --prev v1  # 재실행 간 결과 diff (보정 효과 확인)
```

- 각 단계 실행 시 `logs/s{n}_run_{ts}.log` 에 처리 건수, 스킵, 경고(WARN) 요약 기록
- **WARN 항목은 반드시 사람 검토 대상** — 대응 override 파일 경로를 로그에 함께 출력

---

## 4. 단계별 프로그램 스펙

### S1 — 인벤토리 정규화 및 식별자 해석

**목적**: 분석 대상 SP의 확정 목록 생성, 시노님/DB Link/스키마 경계를 해석하여 모든 참조를 **정규 식별자**(`OWNER.OBJECT[@LINK]`)로 통일

**처리 로직**
1. `in_objects.csv` 에서 PROCEDURE / FUNCTION / PACKAGE BODY / TRIGGER 필터 → 대상 후보
2. `config.exclude_name_patterns` 적용 제외 (제외 목록은 산출물에 사유와 함께 기록 — 사일런트 제외 금지)
3. 패키지 해체: `in_source.csv` 파싱으로 PACKAGE BODY 내부 PROCEDURE/FUNCTION 을 개별 단위(`PKG.PROC`)로 분리 — **평가 단위 = 개별 서브프로그램**
4. 시노님 해석: 소스/의존성에 나타난 이름 → `in_synonyms.csv` 로 실체 치환 (PUBLIC 시노님 포함, OWNER별 우선순위: private > public)
5. DB Link 해석: `REFERENCED_LINK_NAME` 존재 또는 소스 내 `@link` 패턴 → 노드 유형 `REMOTE` 부여. `in_remote_objects.csv` 있으면 원격 실체와 매핑, 없으면 `UNRESOLVED_REMOTE`
6. Grant 경계 표시: `in_tab_privs.csv` 에서 EXECUTE grant 를 조인 → SP별 "타 스키마 호출 가능 여부" 플래그
7. 미사용 후보 표시: `in_exec_stats.csv` 존재 시 최근 N개월 실행 0건 SP에 `SUSPECT_UNUSED` 플래그 (자동 제외하지 않음 — 보정으로만 제외 가능)

**산출물**

| 파일 | 내용 |
|---|---|
| `s1_inventory.csv` | SP_ID(정규 식별자), OWNER, PKG, NAME, TYPE, LOC, STATUS, CROSS_SCHEMA_CALLABLE, SUSPECT_UNUSED, REMOTE_REF_COUNT |
| `s1_excluded.csv` | 제외 객체 + 제외 사유 |
| `s1_unresolved.csv` | **[검토 필수]** 해석 실패 항목: 미해석 시노님, UNRESOLVED_REMOTE, INVALID 상태 객체, 이름 충돌(동명 다중 스키마) |

**사람 보정 지점 → `override/s1_inventory_override.csv`**

| 컬럼 | 용도 |
|---|---|
| SP_ID | 대상 |
| ACTION | `INCLUDE` / `EXCLUDE` / `RENAME` |
| RESOLVED_TARGET | 미해석 참조의 수동 실체 지정 (예: `REMOTE_DB.HR.CALC_PAY`) |
| REASON | 보정 사유 (감사 추적용, 필수) |

---

### S2 — 정적 분석 (지표 산출)

**목적**: SP별 복잡성 원시 지표 산출

**처리 로직**
1. `in_source.csv` 를 SP_ID 단위로 재조립 (S1 패키지 해체 경계 사용)
2. 전처리: 주석(`--`, `/* */`) 및 문자열 리터럴 제거본 생성 → 패턴 매칭은 제거본, LOC는 주석 제외 원본 기준
3. 파서: 1차 ANTLR PL/SQL grammar 시도 → 실패 시 정규식 기반 폴백 (폴백 사용 SP는 `PARSE_MODE=REGEX` 표기 → 신뢰도 하향)
4. 지표 추출 (본문 §2 메트릭 정의 준수):
   - LOC, 분기 수(IF/ELSIF/CASE/LOOP/WHILE/FOR), 커서 수, REF CURSOR 수
   - 동적 SQL: `EXECUTE IMMEDIATE`, `DBMS_SQL` 호출 수
   - 트랜잭션 제어: COMMIT/ROLLBACK/SAVEPOINT 수, `PRAGMA AUTONOMOUS_TRANSACTION`
   - Oracle 전용: CONNECT BY, MERGE, 분석함수(OVER), BULK COLLECT, FORALL, `UTL_*`/`DBMS_*` 패키지별 카운트
   - `@link` 직접 사용 수 (S1 해석 결과와 대조)
   - 예외 핸들러 수, GOTO 수, 패키지 전역변수 참조 수
5. 소스 내 SQL 문 인벤토리: DML 유형별 카운트 + 참조 테이블 목록 추출 (S3 엣지 보강용 — DBA_DEPENDENCIES가 놓치는 동적 SQL 내 참조의 후보)

**산출물**

| 파일 | 내용 |
|---|---|
| `s2_metrics.csv` | SP_ID + 지표 30여 컬럼 + PARSE_MODE |
| `s2_sql_inventory.csv` | SP_ID, STMT_SEQ, DML_TYPE, TABLES_RAW, IS_DYNAMIC |
| `s2_parse_failures.csv` | **[검토 필수]** 파싱 실패/폴백 SP + 실패 지점 스니펫 |

**사람 보정 지점 → `override/s2_metrics_override.csv`**
- 컬럼: SP_ID, METRIC_NAME, VALUE, REASON
- 용도: 파싱 실패 SP의 수동 계측값 입력, 오탐 지표 수정 (예: 문자열 안 키워드 오인식)

---

### S3 — 의존성 그래프 구축

**목적**: 전환 순서·묶음 결정을 위한 그래프 분석

**처리 로직**
1. 노드: S1 인벤토리 SP + 테이블/뷰 + REMOTE 노드 + APP 노드(호출처 리포 단위) + TRIGGER/JOB 노드
2. 엣지 소스 3원 통합 (우선순위 순):
   - `in_dependencies.csv` (정적, 신뢰도 상)
   - S2 SQL 인벤토리의 동적 SQL 참조 후보 (신뢰도 중 — `EDGE_CONFIDENCE=CANDIDATE` 표기)
   - `in_app_calls.csv` + 트리거/잡 본문 매칭 (신뢰도 상)
3. Grant 필터 검증: SP→SP 호출 엣지에 대해 `in_tab_privs.csv` 로 EXECUTE 권한 실재 확인 → 권한 없는데 의존성만 있는 경우 `GRANT_MISSING` 경고 (죽은 코드 또는 권한 오류 후보)
4. DB Link 엣지: 원격 노드로 향하는 엣지는 절단점(cut point)으로 별도 태깅 — 전환 시 REST/이벤트 인터페이스 치환 대상
5. 그래프 분석:
   - SCC 탐지 → 상호 재귀 묶음 = 단일 작업 단위 승격
   - 위상 정렬 (SCC 축약 후) → Wave 번호 부여
   - Fan-in / Fan-out / 매개 중심성 산출
   - Louvain 커뮤니티 → 도메인 클러스터 후보 (MyBatis Mapper/서비스 경계 제안)

**산출물**

| 파일 | 내용 |
|---|---|
| `s3_nodes.csv` | NODE_ID, NODE_TYPE(SP/TABLE/REMOTE/APP/TRIGGER/JOB), CLUSTER_ID |
| `s3_edges.csv` | SRC, DST, EDGE_TYPE, EDGE_CONFIDENCE, IS_DBLINK, GRANT_OK |
| `s3_scc.csv` | SCC_ID, MEMBER_SP_ID 목록 |
| `s3_waves.csv` | SP_ID, WAVE_NO |
| `s3_graph.mmd` / `s3_graph.svg` | Mermaid/Graphviz 시각화 (클러스터별 분할, 단색) |
| `s3_review.csv` | **[검토 필수]** CANDIDATE 엣지 전체, GRANT_MISSING 엣지, 고아 노드 |

**사람 보정 지점 → `override/s3_edges_override.csv`**

| 컬럼 | 용도 |
|---|---|
| SRC, DST | 대상 엣지 |
| ACTION | `ADD` / `REMOVE` / `CONFIRM`(CANDIDATE→확정) |
| REASON | 사유 (예: "동적 SQL 실참조 확인", "레거시 죽은 코드") |

- 커뮤니티 재배정: `override/s3_cluster_override.csv` (SP_ID, CLUSTER_ID, REASON) — 도메인 지식 반영

---

### S4 — 점수화 및 분류

**목적**: (D, C) 종합 점수 산출, 4분면 분류, 전환 전략 태깅

**처리 로직**
1. S2 지표 + S3 그래프 지표(Fan-in/out, DB Link 수, 트리거/잡 호출 여부, 타 스키마 grant 호출)를 결합
2. 정규화: 지표별 백분위(default) 또는 Min-Max (config 선택)
3. 가중합: `D = Σ wi·di`, `C = Σ wj·cj`
4. 캘리브레이션(선택): `override/pilot_effort.csv` (SP_ID, ACTUAL_MD) 존재 시 선형회귀로 가중치 보정 제안치 산출 → `s4_weight_suggestion.yaml` 출력 (자동 적용하지 않음 — 사람이 config에 반영)
5. 4분면 분류 + 전환 전략 태그:
   - `AUTO`: 자동 변환 후보 (룰: 단일 DML, 커서 0, 분기 ≤3, 동적 SQL 0, DB Link 0)
   - `SEMI`: 반자동 (커서 루프, 임시테이블 등)
   - `MANUAL`: 수동 재작성
   - `DEFER`: 잔존 검토 (고D·고C, DB Link 다수, 자율 트랜잭션 등)
6. SCC 멤버는 묶음 내 최고 등급으로 상향 통일

**산출물**

| 파일 | 내용 |
|---|---|
| `s4_scores.csv` | SP_ID, D_SCORE, C_SCORE, QUADRANT, STRATEGY, WAVE_NO, CLUSTER_ID, EFFORT_EST_MD, CONFIDENCE |
| `s4_weight_suggestion.yaml` | 회귀 기반 가중치 제안 (파일럿 있을 때) |
| `s4_review.csv` | **[검토 필수]** 경계선 ±5% 이내 SP, PARSE_MODE=REGEX 로 신뢰도 낮은 SP, SCC 상향 조정된 SP |

**사람 보정 지점 → `override/s4_strategy_override.csv`**
- 컬럼: SP_ID, STRATEGY, QUADRANT(선택), EFFORT_MD(선택), REASON
- 용도: 도메인 판단으로 전략 강제 지정 (예: 정산 SP는 점수 무관 DEFER)

---

### S5 — 리포트 생성

**목적**: 의사결정용 최종 산출물

**처리 로직 및 산출물**

| 파일 | 내용 |
|---|---|
| `s5_summary.md` | 총괄: 대상 수, 분면별/전략별 분포, 총 예상 공수, 신뢰도 주석 |
| `s5_roadmap.csv` | Wave × Cluster 매트릭스 — Wave별 전환 대상, 선행 조건, DB Link 절단점 목록 |
| `s5_inventory_full.xlsx` | 전 SP 상세 (필터/정렬 가능한 통합 시트) — 검토 회의용 |
| `s5_quadrant.svg` | (D, C) 산점도, 4분면, 전략 색상 |
| `s5_dblink_cutpoints.md` | DB Link 의존 SP 목록 + 대체 인터페이스 설계 필요 항목 |
| `s5_override_audit.md` | 전체 override 이력 감사 요약 (누가·무엇을·왜 — REASON 집계) |

- 모든 리포트에 override 반영 여부 표기 (`*` 마커) → 자동 산출 vs 사람 판단 구분 가능

---

## 5. 데이터 품질 검증 규칙 (validate 커맨드)

| 규칙 | 수준 |
|---|---|
| 입력 CSV 헤더/타입 스키마 일치 | ERROR |
| `in_source.csv` 의 SP가 `in_objects.csv` 에 존재 | ERROR |
| override 파일의 SP_ID가 인벤토리에 존재 | ERROR |
| override에 REASON 누락 | ERROR |
| INVALID 상태 객체 존재 | WARN |
| 의존성에 등장하나 objects에 없는 참조 (drop된 객체) | WARN |
| DB Link 참조 있으나 `in_remote_objects.csv` 미제공 | WARN |
| 동명 SP 다중 스키마 존재 | WARN |

---

## 6. 운영 시나리오 (권장 반복 주기)

1. **초회**: 전 단계 실행 → S1/S2/S3 review 파일 검토 → override 작성 → 재실행
2. **파일럿**: AUTO 후보 중 10건 실전환 → 실측 공수를 `pilot_effort.csv` 에 기록 → S4 가중치 보정
3. **정기(분기)**: 추출 스크립트 재실행 → diff 커맨드로 신규/변경 SP만 검토
4. **CI 연계(선택)**: 추출→분석→diff 를 파이프라인화, 신규 SP 등록 시 자동 평가 리포트 발행
