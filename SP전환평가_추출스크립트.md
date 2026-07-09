# SP 전환 평가 — 입력데이터 추출 스크립트

| 항목 | 내용 |
|---|---|
| 문서 버전 | v1.0 (Draft) |
| 대상 DB | Oracle 11g~19c |
| 실행 계정 요건 | `SELECT_CATALOG_ROLE` 또는 `SELECT ANY DICTIONARY` 권장 |
| 출력 | UTF-8 CSV (프로그램 스펙 §2 입력 파일명과 1:1 대응) |

---

## 0. 공통 사항

### 0.1 권한별 실행 전략

| 상황 | 사용 뷰 | 비고 |
|---|---|---|
| DBA 권한 확보 | `DBA_*` | **권장** — 전 스키마 일괄 추출 |
| 대상 스키마 계정만 확보 | `ALL_*` + 각 계정 로그인 반복 | 타 계정 미Grant 객체 누락 가능 → 결과 병합 필요 |
| 최소 권한 | `USER_*` | 계정별 실행 후 OWNER 컬럼 수동 부여, 병합 |

- 이하 스크립트는 `DBA_*` 기준. 권한 부족 시 `DBA_` → `ALL_` 치환 (단, `ALL_*` 은 **현재 계정이 접근 가능한 객체만** 반환 — 계정 간 Grant가 없는 객체는 보이지 않으므로 누락 검증 필수, §8 참조)
- 대상 스키마 바인딩: 전체 스크립트에서 `:SCHEMAS` 자리에 실제 목록 치환

```sql
-- 공통 스키마 필터 (모든 쿼리 WHERE 절에 사용)
-- owner IN ('APP_OWNER','BATCH_OWNER')   -- 실제 값으로 치환
```

### 0.2 CSV 추출 방법 (SQL*Plus 예시)

```sql
SET MARKUP CSV ON QUOTE ON
SET FEEDBACK OFF HEADING ON PAGESIZE 0 LINESIZE 32767 LONG 2000000 TRIMSPOOL ON
ALTER SESSION SET NLS_DATE_FORMAT = 'YYYY-MM-DD HH24:MI:SS';
SPOOL in_objects.csv
@01_objects.sql
SPOOL OFF
```

- SQL Developer / DBeaver의 CSV Export 사용 가능 (인코딩 UTF-8, 헤더 포함 확인)
- `TRIGGER_BODY`, `JOB_ACTION` 등 LONG/CLOB 컬럼은 `SET LONG` 값 충분히 확보

---

## 1. 객체 인벤토리 → `in_objects.csv`

```sql
-- 01_objects.sql
SELECT owner,
       object_name,
       object_type,
       status,
       created,
       last_ddl_time
FROM   dba_objects
WHERE  owner IN ('APP_OWNER','BATCH_OWNER')
AND    object_type IN ('PROCEDURE','FUNCTION','PACKAGE','PACKAGE BODY',
                       'TRIGGER','TABLE','VIEW','MATERIALIZED VIEW','SYNONYM')
ORDER  BY owner, object_type, object_name;
```

- `STATUS='INVALID'` 객체도 포함 추출 (분석기가 WARN 처리)

---

## 2. 소스 코드 → `in_source.csv`

```sql
-- 02_source.sql
SELECT owner,
       name,
       type,
       line,
       REPLACE(REPLACE(text, CHR(13), ' '), CHR(10), ' ') AS text
FROM   dba_source
WHERE  owner IN ('APP_OWNER','BATCH_OWNER')
AND    type IN ('PROCEDURE','FUNCTION','PACKAGE','PACKAGE BODY','TRIGGER','TYPE BODY')
ORDER  BY owner, name, type, line;
```

- 개행 치환은 CSV 무결성용. 분석기는 LINE 순서로 재조립
- 대용량(수십만 라인) 시 스키마별 분할 스풀 권장

---

## 3. 의존성 → `in_dependencies.csv`

```sql
-- 03_dependencies.sql
SELECT owner,
       name,
       type,
       referenced_owner,
       referenced_name,
       referenced_type,
       referenced_link_name,       -- ★ DB Link 경유 참조 식별 핵심 컬럼
       dependency_type
FROM   dba_dependencies
WHERE  (owner IN ('APP_OWNER','BATCH_OWNER')
        OR referenced_owner IN ('APP_OWNER','BATCH_OWNER'))  -- ★ 역방향 포함
ORDER  BY owner, name;
```

**주의 — DB Link 관련 딕셔너리 한계**
- `REFERENCED_LINK_NAME` 이 NOT NULL 인 행 = 원격 참조. 이때 `REFERENCED_OWNER` 는 원격 기준이며 로컬 딕셔너리로 실체 검증 불가
- 원격 객체가 시노님 뒤에 숨은 경우(`CREATE SYNONYM s FOR t@link`) 의존성에는 시노님만 잡힘 → §5 시노님 추출과 조합해 분석기가 해석
- **역방향 포함 이유**: 타 스키마(대상 외) 객체가 대상 SP를 참조하는 경우 = Fan-in. 계정 간 Grant 기반 호출 관계 파악에 필수

---

## 4. 파라미터 → `in_arguments.csv`

```sql
-- 04_arguments.sql
SELECT owner,
       package_name,
       object_name,
       argument_name,
       position,
       data_type,
       in_out
FROM   dba_arguments
WHERE  owner IN ('APP_OWNER','BATCH_OWNER')
AND    data_level = 0                 -- 중첩 타입 하위레벨 제외
ORDER  BY owner, package_name, object_name, position;
```

---

## 5. 시노님 → `in_synonyms.csv`

```sql
-- 05_synonyms.sql
SELECT owner,
       synonym_name,
       table_owner,
       table_name,
       db_link                        -- ★ NOT NULL이면 원격 실체
FROM   dba_synonyms
WHERE  owner IN ('APP_OWNER','BATCH_OWNER','PUBLIC')
AND    (table_owner IN ('APP_OWNER','BATCH_OWNER')
        OR db_link IS NOT NULL
        OR owner IN ('APP_OWNER','BATCH_OWNER'))
ORDER  BY owner, synonym_name;
```

- PUBLIC 시노님 포함 필수 — 소스에서 스키마 없이 참조하는 객체의 실체 해석에 사용

---

## 6. DB Link 및 계정 간 Grant

### 6.1 DB Link 목록 → `in_db_links.csv`

```sql
-- 06a_db_links.sql
SELECT owner,
       db_link,
       username,
       host
FROM   dba_db_links
ORDER  BY owner, db_link;
```

- PUBLIC DB Link 포함됨(OWNER='PUBLIC'). `HOST` 는 tnsnames 별칭 또는 접속 문자열 — 원격 시스템 식별에 사용
- 보안: `username` 은 접속 계정명만 노출(패스워드 미포함)이나, 산출 파일 접근 통제 필요

### 6.2 계정 간 Grant → `in_tab_privs.csv`

```sql
-- 06b_tab_privs.sql
SELECT grantee,
       owner,
       table_name,                    -- 객체명 (SP/테이블 공통 컬럼명)
       privilege,
       grantable,
       type                           -- 12c+: 객체 타입. 11g는 컬럼 없음 → NULL 처리
FROM   dba_tab_privs
WHERE  (owner IN ('APP_OWNER','BATCH_OWNER')          -- 대상이 부여한 권한
        OR grantee IN ('APP_OWNER','BATCH_OWNER'))    -- 대상이 받은 권한 (양방향)
AND    privilege IN ('EXECUTE','SELECT','INSERT','UPDATE','DELETE','READ')
ORDER  BY owner, table_name, grantee;
```

**분석 활용 포인트**
- `PRIVILEGE='EXECUTE'` + 객체가 SP → **스키마 경계를 넘는 호출 관계의 물적 증거**. DBA_DEPENDENCIES 엣지와 대조해 GRANT_MISSING 검증
- `GRANTEE` 가 ROLE 인 경우 → 롤 전개 필요 (아래 6.3)
- 대상 스키마가 **받은** SELECT/EXECUTE = 타 스키마 객체 의존 (전환 시 데이터 접근 경로 재설계 대상)

### 6.3 롤 경유 권한 전개 → `in_role_privs.csv` (보조)

```sql
-- 06c_role_privs.sql
-- 롤에 부여된 객체 권한
SELECT rp.grantee   AS role_name,
       tp.owner,
       tp.table_name,
       tp.privilege
FROM   dba_tab_privs tp
JOIN   dba_roles r ON r.role = tp.grantee
JOIN   dba_role_privs rp ON rp.granted_role = r.role
WHERE  tp.owner IN ('APP_OWNER','BATCH_OWNER')
AND    tp.privilege IN ('EXECUTE','SELECT','INSERT','UPDATE','DELETE');
```

- 계층 롤(롤 안의 롤)은 필요 시 `CONNECT BY` 로 재귀 전개 — 대상 환경 롤 구조 확인 후 결정

---

## 7. 트리거 / 스케줄러

### 7.1 트리거 → `in_triggers.csv`

```sql
-- 07a_triggers.sql
SELECT owner,
       trigger_name,
       table_owner,
       table_name,
       status,
       trigger_body                   -- LONG: SET LONG 2000000 필수
FROM   dba_triggers
WHERE  owner IN ('APP_OWNER','BATCH_OWNER')
ORDER  BY owner, trigger_name;
```

### 7.2 스케줄러 잡 → `in_scheduler_jobs.csv`

```sql
-- 07b_scheduler.sql
SELECT owner, job_name, job_type, job_action, enabled,
       repeat_interval AS schedule_text
FROM   dba_scheduler_jobs
WHERE  owner IN ('APP_OWNER','BATCH_OWNER')
UNION ALL
-- 구형 DBMS_JOB 병행 환경 대비
SELECT schema_user AS owner,
       TO_CHAR(job) AS job_name,
       'LEGACY_JOB' AS job_type,
       what AS job_action,
       CASE WHEN broken = 'N' THEN 'TRUE' ELSE 'FALSE' END AS enabled,
       interval AS schedule_text
FROM   dba_jobs
WHERE  schema_user IN ('APP_OWNER','BATCH_OWNER');
```

---

## 8. 애플리케이션 호출 인벤토리 → `in_app_calls.csv`

.NET 소스 저장소 대상. Git Bash / WSL 에서 실행:

```bash
#!/bin/bash
# 08_extract_app_calls.sh <repo_root> <output.csv>
REPO_ROOT=$1; OUT=$2
echo 'REPO,FILE_PATH,LINE_NO,SP_NAME_RAW,CALL_SNIPPET' > "$OUT"

# 패턴 1: CommandType.StoredProcedure 인접 CommandText
grep -rn --include='*.cs' --include='*.vb' --include='*.config' --include='*.xml' \
  -E 'CommandType\.StoredProcedure|CommandText\s*=|\.ExecuteNonQuery|CALL\s+[A-Z0-9_\.]+|BEGIN\s+[A-Z0-9_\.]+\s*\(' \
  "$REPO_ROOT" | while IFS=: read -r file line content; do
    # SP명 후보 추출: "PKG.PROC" 또는 "SCHEMA.PKG.PROC" 문자열 리터럴
    sp=$(echo "$content" | grep -oE '"[A-Za-z0-9_]+(\.[A-Za-z0-9_]+){0,2}"' | head -1 | tr -d '"')
    snippet=$(echo "$content" | sed 's/"/""/g' | cut -c1-200)
    echo "$(basename $REPO_ROOT),\"$file\",$line,\"$sp\",\"$snippet\"" >> "$OUT"
done
```

**보완 필수 사항 (수동 검토 지점)**
- SP명이 변수/상수 클래스에 정의된 경우 grep 1회로 미탐 → 상수 정의 파일 별도 grep 후 매핑
- ORM(Dapper 등) 확장 메서드 사용 패턴은 프로젝트 컨벤션에 맞춰 패턴 추가
- 결과의 `SP_NAME_RAW` 공백 행 = 후보만 잡힌 행 → S1 override 대상

---

## 9. 선택 추출

### 9.1 PL/Scope → `in_plscope_identifiers.csv` (재컴파일 가능 환경 한정)

```sql
-- 09a_plscope.sql  ※ 사전 준비: 세션 설정 후 대상 재컴파일 (운영 반영 절차 필요 — 개발/스테이징 권장)
-- ALTER SESSION SET PLSCOPE_SETTINGS='IDENTIFIERS:ALL, STATEMENTS:ALL';
-- ALTER PROCEDURE ... COMPILE;  (스키마 일괄: DBMS_UTILITY.COMPILE_SCHEMA)

SELECT owner, object_name, object_type, name, type,
       usage, usage_id, line, col, usage_context_id
FROM   dba_identifiers
WHERE  owner IN ('APP_OWNER','BATCH_OWNER')
ORDER  BY owner, object_name, usage_id;
```

### 9.2 실행 통계 → `in_exec_stats.csv`

```sql
-- 09b_exec_stats.sql  ※ AWR 라이선스(Diagnostics Pack) 확인 후 사용
SELECT p.owner,
       p.object_name,
       NVL(SUM(s.executions_delta), 0) AS exec_count_period
FROM   dba_procedures p
LEFT   JOIN dba_hist_sqlstat s
       ON UPPER(s.module) LIKE '%' || p.object_name || '%'   -- 환경별 매칭 규칙 조정 필요
GROUP  BY p.owner, p.object_name;

-- 라이선스 없으면 대안: V$DB_OBJECT_CACHE 의 EXECUTIONS (인스턴스 기동 이후 누적)
SELECT owner, name, type, executions
FROM   v$db_object_cache
WHERE  owner IN ('APP_OWNER','BATCH_OWNER')
AND    type IN ('PROCEDURE','FUNCTION','PACKAGE');
```

- 두 방식 모두 완전하지 않음(캐시 aging, 매칭 부정확) → `SUSPECT_UNUSED` 는 참고 플래그로만 사용, 제외는 반드시 사람 보정

### 9.3 원격 DB 객체 → `in_remote_objects.csv`

- `in_db_links.csv` 의 HOST 별 원격 DB에 접속해 **본 문서 §1(objects), §3(dependencies) 스크립트를 동일 실행**
- 파일명에 링크명 접두: `in_remote_objects__{DB_LINK}.csv`
- 원격 접속 불가 시: 분석기의 `UNRESOLVED_REMOTE` 로 남기고 S1 override에서 수동 지정

---

## 10. 추출 완결성 자가 점검 (추출 직후 실행)

```sql
-- 10_sanity_check.sql
-- (1) 대상 스키마별 객체 수 요약 — 분석기 로그의 로딩 건수와 대조
SELECT owner, object_type, COUNT(*) cnt
FROM   dba_objects
WHERE  owner IN ('APP_OWNER','BATCH_OWNER')
GROUP  BY owner, object_type ORDER BY 1,2;

-- (2) 소스 없는 PROCEDURE (wrap 처리 등) — 정적 분석 불가 대상 사전 식별
SELECT o.owner, o.object_name
FROM   dba_objects o
WHERE  o.owner IN ('APP_OWNER','BATCH_OWNER')
AND    o.object_type IN ('PROCEDURE','FUNCTION','PACKAGE BODY')
AND    NOT EXISTS (SELECT 1 FROM dba_source s
                   WHERE s.owner = o.owner AND s.name = o.object_name);

-- (3) WRAP(암호화) 소스 탐지 — 분석 불가, override 필수 대상
SELECT DISTINCT owner, name
FROM   dba_source
WHERE  owner IN ('APP_OWNER','BATCH_OWNER')
AND    line = 1 AND UPPER(text) LIKE '%WRAPPED%';

-- (4) DB Link 참조 요약 — 원격 추출 대상 링크 목록
SELECT referenced_link_name, COUNT(*) ref_cnt
FROM   dba_dependencies
WHERE  owner IN ('APP_OWNER','BATCH_OWNER')
AND    referenced_link_name IS NOT NULL
GROUP  BY referenced_link_name;

-- (5) 대상 SP에 EXECUTE grant 받은 외부 계정 목록 — Fan-in 스키마 경계 요약
SELECT tp.grantee, COUNT(*) sp_cnt
FROM   dba_tab_privs tp
JOIN   dba_objects o ON o.owner = tp.owner AND o.object_name = tp.table_name
WHERE  tp.owner IN ('APP_OWNER','BATCH_OWNER')
AND    tp.privilege = 'EXECUTE'
AND    o.object_type IN ('PROCEDURE','FUNCTION','PACKAGE')
AND    tp.grantee NOT IN ('APP_OWNER','BATCH_OWNER')
GROUP  BY tp.grantee ORDER BY 2 DESC;
```

---

## 11. 산출 파일 체크리스트

| # | 파일 | 필수 | 원천 스크립트 |
|---|---|---|---|
| 1 | in_objects.csv | ● | §1 |
| 2 | in_source.csv | ● | §2 |
| 3 | in_dependencies.csv | ● | §3 |
| 4 | in_arguments.csv | ● | §4 |
| 5 | in_synonyms.csv | ● | §5 |
| 6 | in_db_links.csv | ● | §6.1 |
| 7 | in_tab_privs.csv | ● | §6.2 |
| 8 | in_role_privs.csv | ○ | §6.3 |
| 9 | in_triggers.csv | ● | §7.1 |
| 10 | in_scheduler_jobs.csv | ● | §7.2 |
| 11 | in_app_calls.csv | ● | §8 |
| 12 | in_plscope_identifiers.csv | ○ | §9.1 |
| 13 | in_exec_stats.csv | ○ | §9.2 |
| 14 | in_remote_objects__*.csv | ○ | §9.3 |
