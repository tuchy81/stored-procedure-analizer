# SP 전환 평가 — 입력데이터 추출 스크립트

| 항목 | 내용 |
|---|---|
| 문서 버전 | v2.0 (Draft, 개선 반영) |
| 대상 DB | Oracle 11g~19c |
| 실행 계정 요건 | `SELECT_CATALOG_ROLE` 또는 `SELECT ANY DICTIONARY` 권장 |
| 출력 | UTF-8 CSV (프로그램 스펙 §2 입력 파일명과 1:1 대응) + LONG/CLOB 은 별도 텍스트 파일 |
| v1.0 대비 변경 | SQL*Plus 12.2+ 한계 대응 (SQLcl/DBeaver 대안), OVERLOAD 컬럼 추가, LONG 필드 별도 파일 분리, 롤 재귀 전개 스크립트, .NET Roslyn 정적 분석기, 문자셋 변환 절차, 보안 정책, 완결성 자가점검 확장 |

---

## 0. 공통 사항

### 0.1 권한별 실행 전략

| 상황 | 사용 뷰 | 비고 |
|---|---|---|
| DBA 권한 확보 | `DBA_*` | **권장** — 전 스키마 일괄 |
| 대상 스키마 계정만 확보 | `ALL_*` + 각 계정 로그인 반복 | 타 계정 미Grant 객체 누락 가능 → 병합 검증 필수 |
| 최소 권한 | `USER_*` | 계정별 실행 후 OWNER 수동 부여, 병합 |

- 이하 스크립트는 `DBA_*` 기준. 권한 부족 시 `DBA_` → `ALL_` 치환 (단, `ALL_*` 은 현재 계정 접근 가능 객체만 반환 → 누락 검증 §10 필수)
- 대상 스키마 바인딩: 전체 스크립트에서 `:SCHEMAS` 자리에 실제 목록 치환

```sql
-- 공통 스키마 필터 (모든 쿼리 WHERE 절에 사용)
-- owner IN ('APP_OWNER','BATCH_OWNER')   -- 실제 값으로 치환
```

### 0.2 CSV 추출 방법 — 도구별 대안

Oracle 버전 및 도구에 따라 방식이 다릅니다. 아래 3가지 중 환경에 맞는 것을 선택:

#### (A) SQL*Plus 12.2 이상 (권장)
```sql
SET MARKUP CSV ON QUOTE ON
SET FEEDBACK OFF HEADING ON PAGESIZE 0 LINESIZE 32767 LONG 2000000 TRIMSPOOL ON
ALTER SESSION SET NLS_DATE_FORMAT = 'YYYY-MM-DD HH24:MI:SS';
SPOOL in_objects.csv
@01_objects.sql
SPOOL OFF
```

#### (B) SQL*Plus 11g (SET MARKUP CSV 미지원) — 대안
- **SQLcl (12c+ 무료 툴)** 사용 권장: 11g DB에도 클라이언트만 SQLcl 설치하면 접속 가능
  ```
  SET SQLFORMAT CSV
  SPOOL in_objects.csv
  @01_objects.sql
  SPOOL OFF
  ```
- 또는 **DBeaver / SQL Developer**의 CSV Export (인코딩 UTF-8, 헤더 포함, RFC 4180 이스케이프 확인)
- 수기 스크립트: `SET COLSEP ','` + `SET QUOTE ON` 조합은 이스케이핑 취약 → 권장 안 함

#### (C) 모든 버전 공통 — Python 추출기
```python
# 참고: 대량 스풀보다 Python cx_Oracle/oracledb 로 pandas.to_csv 가 이스케이핑 신뢰도 최고
# quoting=csv.QUOTE_ALL, lineterminator='\n' 명시
```

### 0.3 문자셋 변환

원본 DB 캐릭터셋 확인:
```sql
SELECT parameter, value FROM nls_database_parameters
WHERE parameter IN ('NLS_CHARACTERSET','NLS_NCHAR_CHARACTERSET');
```

- **AL32UTF8**: 그대로 UTF-8 스풀
- **KO16MSWIN949 / KO16KSC5601 등**: 클라이언트 `NLS_LANG` 을 `.AL32UTF8` 로 세팅 후 스풀
  ```bash
  export NLS_LANG=KOREAN_KOREA.AL32UTF8   # Linux/WSL
  # Windows: setx NLS_LANG "KOREAN_KOREA.AL32UTF8"
  ```
- 스풀 후 파일 헤더 인코딩 확인: `file in_source.csv` 또는 `chardetect`
- BOM은 붙이지 말 것 (pandas/Python csv 라이브러리 호환성)

### 0.4 대용량 LONG/CLOB 필드 분리 스풀 규약

프로그램 스펙 §2.5 에 따라 `TRIGGER_BODY`, `JOB_ACTION`, WRAP 소스 등은 CSV 셀에 담지 않고 별도 파일로 저장. 예:

```
input/bodies/trigger/{OWNER}/{TRIGGER_NAME}.txt
input/bodies/job/{OWNER}/{JOB_NAME}.txt
```

CSV 컬럼 `BODY_FILE` / `ACTION_FILE` 에는 위 상대 경로만 기록. 아래 §7 스크립트는 이 규약을 따릅니다.

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

- `STATUS='INVALID'` 도 포함 (분석기가 WARN 처리)

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

- 개행 치환은 CSV 셀 무결성용 (`dba_source.text` 는 이미 LINE 단위 분할됨 → 정보 손실 없음)
- 대용량 시 스키마별 분할 스풀 권장

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
       referenced_link_name,       -- ★ DB Link 경유 참조 식별
       dependency_type
FROM   dba_dependencies
WHERE  (owner IN ('APP_OWNER','BATCH_OWNER')
        OR referenced_owner IN ('APP_OWNER','BATCH_OWNER'))  -- ★ 역방향 포함
ORDER  BY owner, name;
```

**DB Link 관련 딕셔너리 한계**
- `REFERENCED_LINK_NAME` NOT NULL = 원격 참조. `REFERENCED_OWNER` 는 원격 기준이며 로컬 딕셔너리로 실체 검증 불가
- 원격 객체가 시노님 뒤에 숨은 경우 의존성엔 시노님만 잡힘 → §5 시노님 추출 조합 해석
- **역방향 포함 이유**: 타 스키마 객체 → 대상 SP 참조 = Fan-in, 스키마 간 Grant 기반 호출 관계 파악

---

## 4. 파라미터 → `in_arguments.csv`

```sql
-- 04_arguments.sql
SELECT owner,
       package_name,
       object_name,
       overload,                    -- ★ 오버로드 구분 필수 (v2.0 추가)
       argument_name,
       position,
       data_type,
       pls_type,                    -- ★ REF CURSOR 등 세부 타입 식별
       in_out
FROM   dba_arguments
WHERE  owner IN ('APP_OWNER','BATCH_OWNER')
AND    data_level = 0              -- 중첩 타입 하위레벨 제외
ORDER  BY owner, package_name, object_name, overload, position;
```

**오버로드 처리 필수 사유**
- 같은 패키지 내 동명 프로시저가 다른 파라미터 시그니처로 오버로드된 경우, `OVERLOAD` 컬럼(11g부터 존재)을 무시하면 `SP_ID` 충돌 발생
- 분석기는 `PKG.PROC#1`, `PKG.PROC#2` 로 접미 부여 (`OVERLOAD` NULL 이면 `#0`)

---

## 5. 시노님 → `in_synonyms.csv`

```sql
-- 05_synonyms.sql
SELECT owner,
       synonym_name,
       table_owner,
       table_name,
       db_link
FROM   dba_synonyms
WHERE  owner IN ('APP_OWNER','BATCH_OWNER','PUBLIC')
AND    (table_owner IN ('APP_OWNER','BATCH_OWNER')
        OR db_link IS NOT NULL
        OR owner IN ('APP_OWNER','BATCH_OWNER'))
ORDER  BY owner, synonym_name;
```

- PUBLIC 시노님 포함 필수

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

- PUBLIC DB Link 포함 (OWNER='PUBLIC'). `HOST` 는 tnsnames 별칭 또는 접속 문자열
- **보안**: `config.security.mask_dblink_host: true` 이면 분석기가 HOST 를 해시로 마스킹 후 산출물에 반영

### 6.2 계정 간 Grant → `in_tab_privs.csv`

```sql
-- 06b_tab_privs.sql
SELECT grantee,
       owner,
       table_name,
       privilege,
       grantable,
       type                           -- 12c+: 객체 타입. 11g는 NULL
FROM   dba_tab_privs
WHERE  (owner IN ('APP_OWNER','BATCH_OWNER')          -- 대상이 부여
        OR grantee IN ('APP_OWNER','BATCH_OWNER'))    -- 대상이 수령
AND    privilege IN ('EXECUTE','SELECT','INSERT','UPDATE','DELETE','READ')
ORDER  BY owner, table_name, grantee;
```

### 6.3 롤 계층 재귀 전개 → `in_role_privs.csv`

**필수** — v1.0 의 1-hop 조인은 계층 롤(롤 안의 롤)을 놓쳐 Grant 검증에서 오탐 발생.

```sql
-- 06c_role_privs.sql
-- 롤 계층 재귀 전개: 사용자/롤이 최종적으로 상속받는 모든 롤 목록
WITH RECURSIVE_ROLES AS (
    -- 시작: 사용자 → 직접 부여 롤
    SELECT grantee AS root_grantee,
           granted_role,
           1        AS depth,
           CAST(granted_role AS VARCHAR2(4000)) AS path
    FROM   dba_role_privs
    WHERE  grantee IN ('APP_OWNER','BATCH_OWNER')
       OR  grantee IN (SELECT username FROM dba_users)   -- 필요 시 확장
    UNION ALL
    -- 재귀: 롤 → 롤에 부여된 롤
    SELECT rr.root_grantee,
           rp.granted_role,
           rr.depth + 1,
           rr.path || ' -> ' || rp.granted_role
    FROM   RECURSIVE_ROLES rr
    JOIN   dba_role_privs rp ON rp.grantee = rr.granted_role
    WHERE  rr.depth < 10                                 -- 무한 재귀 방지
)
-- 최종 결과: 사용자별 도달 가능한 모든 롤 + 각 롤에 부여된 객체 권한 전개
SELECT DISTINCT
       rr.root_grantee AS grantee,
       tp.owner,
       tp.table_name,
       tp.privilege,
       rr.depth,
       rr.path         AS via_roles
FROM   RECURSIVE_ROLES rr
JOIN   dba_tab_privs tp ON tp.grantee = rr.granted_role
WHERE  tp.owner IN ('APP_OWNER','BATCH_OWNER')
AND    tp.privilege IN ('EXECUTE','SELECT','INSERT','UPDATE','DELETE','READ')
ORDER  BY rr.root_grantee, tp.owner, tp.table_name;
```

**Oracle 재귀 CTE 미지원 (11.2 이하) 대안**:
```sql
-- CONNECT BY 재귀
SELECT CONNECT_BY_ROOT grantee AS root_grantee,
       granted_role,
       LEVEL AS depth,
       SYS_CONNECT_BY_PATH(granted_role, ' -> ') AS path
FROM   dba_role_privs
START  WITH grantee IN ('APP_OWNER','BATCH_OWNER')
CONNECT BY NOCYCLE PRIOR granted_role = grantee;
-- 이 결과를 임시 테이블에 넣고 dba_tab_privs 와 조인
```

**분석 활용**
- `PRIVILEGE='EXECUTE'` + 객체가 SP → 스키마 경계 넘는 호출 관계의 물적 증거
- 분석기 S1 은 `in_tab_privs.csv` (직접 Grant) 와 `in_role_privs.csv` (롤 경유) 를 합쳐 `s1_grant_matrix.csv` 생성

---

## 7. 트리거 / 스케줄러 — 본문 별도 파일 분리

### 7.1 트리거 인벤토리 → `in_triggers.csv` + `bodies/trigger/*`

```sql
-- 07a_triggers.sql — 인벤토리만 (본문 제외)
SELECT owner,
       trigger_name,
       table_owner,
       table_name,
       status,
       'bodies/trigger/' || owner || '/' || trigger_name || '.txt' AS body_file
FROM   dba_triggers
WHERE  owner IN ('APP_OWNER','BATCH_OWNER')
ORDER  BY owner, trigger_name;
```

본문 스풀 (SQLcl/PL/SQL 블록 예시):
```sql
-- 07a2_trigger_bodies.sql
SET LONG 2000000 LONGCHUNKSIZE 32767 PAGESIZE 0
DECLARE
    v_dir  VARCHAR2(4000) := '&&BODY_DIR';   -- 실행 시 파라미터
    v_file UTL_FILE.FILE_TYPE;
BEGIN
    FOR t IN (SELECT owner, trigger_name, trigger_body
              FROM   dba_triggers
              WHERE  owner IN ('APP_OWNER','BATCH_OWNER')) LOOP
        v_file := UTL_FILE.FOPEN(v_dir, t.owner || '_' || t.trigger_name || '.txt', 'W', 32767);
        UTL_FILE.PUT(v_file, t.trigger_body);
        UTL_FILE.FCLOSE(v_file);
    END LOOP;
END;
/
```
- UTL_FILE 디렉터리 오브젝트 사전 생성 필요. 권한 없으면 SQLcl `SET SQLFORMAT DELIMITED` + 파일별 분할 스풀로 대체
- 스풀 후 로컬 파일 시스템으로 이동하여 `input/bodies/trigger/{OWNER}/{TRIGGER_NAME}.txt` 배치

### 7.2 스케줄러 잡 → `in_scheduler_jobs.csv` + `bodies/job/*`

```sql
-- 07b_scheduler.sql — 인벤토리
SELECT owner, job_name, job_type, enabled,
       repeat_interval AS schedule_text,
       'bodies/job/' || owner || '/' || job_name || '.txt' AS action_file
FROM   dba_scheduler_jobs
WHERE  owner IN ('APP_OWNER','BATCH_OWNER')
UNION ALL
SELECT schema_user AS owner,
       TO_CHAR(job) AS job_name,
       'LEGACY_JOB' AS job_type,
       CASE WHEN broken = 'N' THEN 'TRUE' ELSE 'FALSE' END AS enabled,
       interval AS schedule_text,
       'bodies/job/' || schema_user || '/' || TO_CHAR(job) || '.txt' AS action_file
FROM   dba_jobs
WHERE  schema_user IN ('APP_OWNER','BATCH_OWNER');

-- 본문(job_action, what)은 07b2_job_bodies.sql 에서 UTL_FILE 로 별도 스풀
```

---

## 8. 애플리케이션 호출 인벤토리 → `in_app_calls.csv`

.NET 저장소에서 SP 호출을 추출합니다. 스파이크 SP-3 (grep 매칭률) 결과에 따라 (A) Roslyn 정적 분석기 or (B) grep 폴백 선택.

### 8.1 (A) Roslyn 정적 분석기 (권장, 매칭률 90%+)

`.csproj` 프로젝트로 별도 도구 빌드. `Microsoft.CodeAnalysis.CSharp` NuGet 사용:

```csharp
// SpCallExtractor.cs (핵심 로직)
using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.CSharp;
using Microsoft.CodeAnalysis.CSharp.Syntax;

// 1. 솔루션 로드 후 각 SyntaxTree 순회
// 2. 다음 패턴 탐지:
//    - AssignmentExpression: CommandText = "PKG.PROC"
//    - MemberAccessExpression: CommandType.StoredProcedure 인접 컨텍스트
//    - InvocationExpression: db.Execute("PKG.PROC", ...) / QueryAsync<T>("...") (Dapper)
//    - Attribute: [StoredProcedure("PKG.PROC")]  (커스텀 어노테이션)
// 3. SP명이 상수 참조(IdentifierName) 이면 SemanticModel.GetConstantValue() 로 실제 값 해석
// 4. 결과 CSV:
//    REPO, FILE_PATH, LINE_NO, SP_NAME_RAW, SP_NAME_RESOLVED, CALL_KIND, CALL_SNIPPET, CONFIDENCE
```

**감지 대상 CALL_KIND**:

| CALL_KIND | 대응 패턴 | CONFIDENCE |
|---|---|---|
| ADO_STORED_PROCEDURE | `CommandType.StoredProcedure` + `CommandText` | HIGH |
| ADO_TEXT_CALL | `CommandType.Text` + `"BEGIN X(...); END;"` | HIGH |
| DAPPER_SP | `db.Execute(sql, commandType: StoredProcedure)` | HIGH |
| DAPPER_INLINE_CALL | `db.Query("BEGIN X; END", ...)` | MEDIUM |
| EF_RAW_SQL | `FromSqlRaw("EXEC X ...")` | MEDIUM |
| CUSTOM_ATTRIBUTE | `[StoredProcedure("X")]` 커스텀 어노테이션 | HIGH |
| UNRESOLVED_VARIABLE | SP명이 runtime 변수 | LOW → override |

산출물:
- `in_app_calls.csv` — 위 컬럼
- `in_app_constants.csv` — SP명 상수 클래스 목록 (const/readonly string, 상수 값 → SP명 매핑)

### 8.2 (B) grep 폴백 (Roslyn 미가용 시)

```bash
#!/bin/bash
# 08b_extract_app_calls_grep.sh <repo_root> <output.csv>
REPO_ROOT=$1; OUT=$2
echo 'REPO,FILE_PATH,LINE_NO,SP_NAME_RAW,SP_NAME_RESOLVED,CALL_KIND,CALL_SNIPPET,CONFIDENCE' > "$OUT"

grep -rn --include='*.cs' --include='*.vb' --include='*.config' --include='*.xml' \
  -E 'CommandType\.StoredProcedure|CommandText\s*=|\.ExecuteNonQuery|\.QueryAsync|\.Execute\(|CALL\s+[A-Z0-9_\.]+|BEGIN\s+[A-Z0-9_\.]+\s*\(|FromSqlRaw|\[StoredProcedure' \
  "$REPO_ROOT" | while IFS=: read -r file line content; do
    sp=$(echo "$content" | grep -oE '"[A-Za-z0-9_]+(\.[A-Za-z0-9_]+){0,2}"' | head -1 | tr -d '"')
    snippet=$(echo "$content" | sed 's/"/""/g' | cut -c1-200)
    echo "$(basename $REPO_ROOT),\"$file\",$line,\"$sp\",\"\",UNKNOWN,\"$snippet\",MEDIUM" >> "$OUT"
done
```

### 8.3 상수 클래스 매핑 (grep 폴백 보완)

```bash
# 08c_extract_constants.sh — SP명 상수 정의 스캔
grep -rn --include='*.cs' -E \
  'const\s+string\s+\w+\s*=\s*"[A-Za-z0-9_\.]+"|readonly\s+string\s+\w+\s*=\s*"[A-Za-z0-9_\.]+"' \
  "$REPO_ROOT" > constants_raw.txt
# → in_app_constants.csv 형식으로 후처리
```

- 결과의 `SP_NAME_RESOLVED` 는 분석기 S1 에서 `in_app_constants.csv` 와 조인하여 후처리

---

## 9. 선택 추출

### 9.1 PL/Scope → `in_plscope_identifiers.csv`

```sql
-- 09a_plscope.sql  ※ 사전 준비: ALTER SESSION SET PLSCOPE_SETTINGS='IDENTIFIERS:ALL, STATEMENTS:ALL'; 후 대상 재컴파일
SELECT owner, object_name, object_type, name, type,
       usage, usage_id, line, col, usage_context_id
FROM   dba_identifiers
WHERE  owner IN ('APP_OWNER','BATCH_OWNER')
ORDER  BY owner, object_name, usage_id;
```

### 9.2 실행 통계 → `in_exec_stats.csv`

```sql
-- 09b_exec_stats.sql  ※ AWR (Diagnostics Pack 라이선스) 필요
SELECT p.owner,
       p.object_name,
       NVL(SUM(s.executions_delta), 0) AS exec_count_period
FROM   dba_procedures p
LEFT   JOIN dba_hist_sqlstat s
       ON UPPER(s.module) LIKE '%' || p.object_name || '%'
GROUP  BY p.owner, p.object_name;

-- 라이선스 없으면 대안
SELECT owner, name, type, executions
FROM   v$db_object_cache
WHERE  owner IN ('APP_OWNER','BATCH_OWNER')
AND    type IN ('PROCEDURE','FUNCTION','PACKAGE');
```

### 9.3 AWR Top SQL → `in_awr_topsql.csv` (신규)

```sql
-- 09c_awr_topsql.sql  ※ Diagnostics Pack 필요, 성능 리스크 지표용
SELECT sql_id,
       module,
       action,
       SUM(elapsed_time_delta)/1e6 AS elapsed_sec,
       SUM(executions_delta)       AS execs,
       SUM(buffer_gets_delta)      AS buffer_gets
FROM   dba_hist_sqlstat
WHERE  snap_id BETWEEN &STARTSNAP AND &ENDSNAP
AND    module IS NOT NULL
GROUP  BY sql_id, module, action
HAVING SUM(elapsed_time_delta) > 1e9   -- 1000초 이상 누적
ORDER  BY 4 DESC
FETCH  FIRST 500 ROWS ONLY;
```
- module/action 이 PL/SQL 호출자 정보를 담는 경우 SP 매칭 가능 (환경 컨벤션 확인)

### 9.4 원격 DB 객체 → `in_remote_objects__{DB_LINK}.csv`

- `in_db_links.csv` 의 각 링크 원격 DB 접속 → §1, §3 스크립트 동일 실행
- 파일명 규칙: `in_remote_objects__{DB_LINK}.csv`
- 원격 접속 불가 시 분석기의 `UNRESOLVED_REMOTE` 로 남기고 S1 override 로 수동 지정

---

## 10. 추출 완결성 자가 점검 (추출 직후 실행)

```sql
-- 10_sanity_check.sql

-- (1) 대상 스키마별 객체 수 요약 — 분석기 로딩 건수와 대조
SELECT owner, object_type, COUNT(*) cnt
FROM   dba_objects
WHERE  owner IN ('APP_OWNER','BATCH_OWNER')
GROUP  BY owner, object_type ORDER BY 1,2;

-- (2) 소스 없는 PROCEDURE (WRAP 등)
SELECT o.owner, o.object_name
FROM   dba_objects o
WHERE  o.owner IN ('APP_OWNER','BATCH_OWNER')
AND    o.object_type IN ('PROCEDURE','FUNCTION','PACKAGE BODY')
AND    NOT EXISTS (SELECT 1 FROM dba_source s
                   WHERE s.owner = o.owner AND s.name = o.object_name);

-- (3) WRAP(암호화) 소스 탐지
SELECT DISTINCT owner, name
FROM   dba_source
WHERE  owner IN ('APP_OWNER','BATCH_OWNER')
AND    line = 1 AND UPPER(text) LIKE '%WRAPPED%';

-- (4) DB Link 참조 요약
SELECT referenced_link_name, COUNT(*) ref_cnt
FROM   dba_dependencies
WHERE  owner IN ('APP_OWNER','BATCH_OWNER')
AND    referenced_link_name IS NOT NULL
GROUP  BY referenced_link_name;

-- (5) EXECUTE grant 받은 외부 계정 목록 — Fan-in 스키마 경계
SELECT tp.grantee, COUNT(*) sp_cnt
FROM   dba_tab_privs tp
JOIN   dba_objects o ON o.owner = tp.owner AND o.object_name = tp.table_name
WHERE  tp.owner IN ('APP_OWNER','BATCH_OWNER')
AND    tp.privilege = 'EXECUTE'
AND    o.object_type IN ('PROCEDURE','FUNCTION','PACKAGE')
AND    tp.grantee NOT IN ('APP_OWNER','BATCH_OWNER')
GROUP  BY tp.grantee ORDER BY 2 DESC;

-- (6) 오버로드 SP 목록 (v2.0 추가) — 분석기 SP_ID 접미 규칙 검증용
SELECT owner, package_name, object_name, COUNT(DISTINCT overload) overload_cnt
FROM   dba_arguments
WHERE  owner IN ('APP_OWNER','BATCH_OWNER')
AND    overload IS NOT NULL
GROUP  BY owner, package_name, object_name
HAVING COUNT(DISTINCT overload) > 1;

-- (7) 재귀 트리거 후보 (v2.0 추가) — 자기 테이블 참조 여부 사전 확인
SELECT owner, trigger_name, table_owner, table_name
FROM   dba_triggers t
WHERE  owner IN ('APP_OWNER','BATCH_OWNER')
AND    EXISTS (
    SELECT 1 FROM dba_dependencies d
    WHERE  d.owner = t.owner
    AND    d.name = t.trigger_name
    AND    d.referenced_owner = t.table_owner
    AND    d.referenced_name = t.table_name
);

-- (8) autonomous_transaction 사전 스캔 (v2.0 추가)
SELECT owner, name, type
FROM   dba_source
WHERE  owner IN ('APP_OWNER','BATCH_OWNER')
AND    UPPER(text) LIKE '%AUTONOMOUS_TRANSACTION%'
GROUP  BY owner, name, type;

-- (9) 롤 계층 깊이 점검 (v2.0 추가) — 재귀 CTE 무한루프 방지 임계 확인
SELECT MAX(LEVEL) max_depth
FROM   dba_role_privs
START  WITH grantee IN ('APP_OWNER','BATCH_OWNER')
CONNECT BY NOCYCLE PRIOR granted_role = grantee;
```

---

## 11. 산출 파일 체크리스트

| # | 파일 | 필수 | 원천 스크립트 | 비고 |
|---|---|---|---|---|
| 1 | in_objects.csv | ● | §1 |  |
| 2 | in_source.csv | ● | §2 |  |
| 3 | in_dependencies.csv | ● | §3 | 역방향 포함 |
| 4 | in_arguments.csv | ● | §4 | **OVERLOAD, PLS_TYPE 컬럼 필수** |
| 5 | in_synonyms.csv | ● | §5 |  |
| 6 | in_db_links.csv | ● | §6.1 |  |
| 7 | in_tab_privs.csv | ● | §6.2 |  |
| 8 | in_role_privs.csv | ● | §6.3 | **재귀 전개 결과** |
| 9 | in_triggers.csv | ● | §7.1 | 본문은 `bodies/trigger/` |
| 10 | in_scheduler_jobs.csv | ● | §7.2 | 본문은 `bodies/job/` |
| 11 | in_app_calls.csv | ● | §8 | Roslyn 우선 / grep 폴백 |
| 12 | in_app_constants.csv | ● (grep 사용시) | §8.3 |  |
| 13 | in_plscope_identifiers.csv | ○ | §9.1 |  |
| 14 | in_exec_stats.csv | ○ | §9.2 |  |
| 15 | in_awr_topsql.csv | ○ | §9.3 | 성능 리스크 |
| 16 | in_remote_objects__*.csv | ○ | §9.4 |  |

---

## 12. 보안 및 배포 정책

- **추출 산출물 저장소**: 사내 통제 저장소만 허용. 개발자 개인 PC 로컬 장기 보관 금지 (프로젝트 종료 시 삭제)
- **`in_db_links.csv` 의 HOST 컬럼**: 필요 시 SHA-256 해시로 마스킹 (분석기 config `mask_dblink_host`)
- **`in_source.csv` / `bodies/*`**: IP 지분 및 개인정보 컬럼명 노출 가능성 — 사전 masking rule 적용 여부 검토
- **원격 DB 접속 자격증명**: `in_db_links.csv` 의 `username` 만 노출, 비밀번호는 딕셔너리에 원래 저장 안 됨. tnsnames.ora 는 별도 관리
- 추출 실행 로그(`logs/extract_*.log`)에 SQL 원문 남기지 말 것 (SELECT 결과에 소스 로직이 흘러들어갈 수 있음)

---

## 부록 A. v1.0 → v2.0 변경 요약

| 항목 | v1.0 | v2.0 |
|---|---|---|
| SQL*Plus 12.2+ 의존 | 명시 안 됨 | §0.2 SQLcl/DBeaver/Python 3가지 대안 명시 |
| 문자셋 변환 | 언급만 | §0.3 NLS_LANG 설정 절차 |
| LONG/CLOB 필드 | CSV 셀 안 담음 | §0.4 별도 파일 분리 규약 + §7.1/7.2 UTL_FILE 스풀 |
| OVERLOAD 컬럼 | 누락 | §4 필수 컬럼 (오버로드 SP 구분) |
| PLS_TYPE 컬럼 | 누락 | §4 REF CURSOR 세부 타입 식별 |
| 롤 전개 | 1-hop 조인 | §6.3 재귀 CTE + CONNECT BY 대안 |
| .NET 호출 추출 | grep 단독 | §8.1 Roslyn 정적 분석기 (권장) + §8.2 grep 폴백 + §8.3 상수 스캔 |
| AWR Top SQL | 없음 | §9.3 성능 리스크 지표 |
| 완결성 점검 | 5개 항목 | §10 9개 항목 (오버로드/재귀트리거/autonomous/롤 깊이 추가) |
| 보안 정책 | 없음 | §12 산출물 취급 규칙 |
