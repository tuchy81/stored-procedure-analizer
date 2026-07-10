# SP → 백엔드 전환 평가 자동화 — 사용 매뉴얼

| 항목 | 내용 |
|---|---|
| 대상 | `sp-assessor` 를 실제로 실행하는 담당자 (DBA / 분석가 / PM) |
| 전제 문서 | 『SP전환평가_추출스크립트.md』(DB에서 CSV 뽑는 법), 『SP전환평가_프로그램스펙.md』(설계 원문) |
| 요약 | 이 문서는 "무엇을, 어떤 순서로 실행하는가"에 집중합니다. 각 단계의 상세 로직/산출 컬럼은 프로그램 스펙 문서를 참조하세요. |

---

## 1. 사전 준비

- Python **3.11 이상**
- (선택) DB 접속 계정 — `sp-assessor` 자체는 CSV만 읽으므로 실행 시점엔 DB 접속이 필요 없습니다. CSV 추출은 별도로 『SP전환평가_추출스크립트.md』를 따라 진행하세요.

### 1.1 설치

```bash
git clone <this-repo>
cd stored-procedure-analizer

python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

# 기본 설치 (validate, S1~S3 까지 필수 의존성만)
pip install -e .

# 권장: 전체 기능(캘리브레이션·리포트) 포함 설치
pip install -e ".[scoring,viz,dev]"
```

| extra | 필요 시점 | 미설치 시 동작 |
|---|---|---|
| `scoring` (scikit-learn, numpy, scipy) | S4 캘리브레이션(Ridge/VIF/부트스트랩) | 캘리브레이션 생략, 휴리스틱 공수만 산출 (WARN 로그) |
| `viz` (matplotlib, openpyxl) | S5 `s5_inventory_full.xlsx`, `s5_quadrant.svg` | 해당 파일만 생략 (WARN 로그), 나머지 리포트는 정상 생성 |
| `community` (python-igraph, leidenalg) | `config.graph.community_algorithm: leiden` 지정 시 | 미설치면 자동으로 `louvain`(networkx 내장)으로 폴백 |
| `dev` (pytest, ruff) | 개발/테스트 | — |

설치 후 확인:

```bash
sp-assessor --help
```

`pip install -e .` 를 안 했다면 `python -m sp_assessor.cli.main --help` 로 동일하게 실행할 수 있습니다. 이하 모든 예시는 `sp-assessor` 로 표기합니다.

---

## 2. 프로젝트 디렉터리 준비

분석 대상마다 별도 작업 디렉터리를 하나 둡니다 (이 저장소의 `examples/` 가 최소 예시입니다).

```
my-project/
├── config.yaml
├── input/
│   ├── in_objects.csv
│   ├── in_source.csv
│   ├── in_dependencies.csv
│   ├── in_arguments.csv
│   ├── in_synonyms.csv
│   ├── in_db_links.csv
│   ├── in_tab_privs.csv
│   ├── in_role_privs.csv
│   ├── in_triggers.csv
│   ├── in_scheduler_jobs.csv
│   ├── in_app_calls.csv
│   ├── (선택) in_app_constants.csv, in_plscope_identifiers.csv, in_exec_stats.csv,
│   │            in_remote_objects__{DB_LINK}.csv
│   └── bodies/               # 트리거/잡 본문 등 대용량 필드 (§2.5)
│       ├── trigger/{OWNER}/{TRIGGER_NAME}.txt
│       └── job/{OWNER}/{JOB_NAME}.txt
└── override/                  # 처음엔 비어 있어도 됨 — 검토 후 채워나감
```

- `input/*.csv` 는 『SP전환평가_추출스크립트.md』의 SQL을 실행해 만듭니다.
- `output/`, `logs/` 는 최초 실행 시 자동 생성됩니다 (`paths.ensure()`).
- 최소 예시가 필요하면 이 저장소의 `examples/` 디렉터리를 복사해서 구조를 참고하세요:

```bash
cp -r examples/input  my-project/input
cp -r examples/override my-project/override   # 없으면 생략 가능
cp examples/config.yaml my-project/config.yaml
```

---

## 3. `config.yaml` 설정

`examples/config.yaml` 을 복사해 다음 항목을 프로젝트에 맞게 수정합니다.

| 키 | 의미 | 자주 바꾸는 경우 |
|---|---|---|
| `target_schemas` | 분석 대상 스키마 목록 | 항상 프로젝트에 맞게 수정 |
| `exclude_name_patterns` | 정규식 제외 패턴 (임시/백업/테스트 객체) | 사내 네이밍 컨벤션에 맞게 |
| `character_set.source_db` | 원본 DB 캐릭터셋 | KO16MSWIN949 환경이면 반드시 지정 |
| `weights.*` | S4 D/C 점수 가중치 | 파일럿 캘리브레이션 후 `s4_weight_suggestion.yaml` 참고해 조정 |
| `quadrant.*` | 4분면 경계 및 boundary band | 조직의 리스크 감내도에 따라 |
| `strategy_rules.*` | AUTO_SIMPLE/AUTO_ASSISTED 판정 룰 | 템플릿 변환기 역량에 맞게 |
| `calibration.min_samples` | 캘리브레이션 최소 표본 | 파일럿 SP 확보량에 맞게 (기본 30) |
| `parser.primary` | `regex` 고정 (v0.1) | ANTLR 통합 전까지 변경 불필요 |
| `graph.community_algorithm` | `louvain`(기본) / `leiden` | `leiden` 선택 시 `pip install -e ".[community]"` 필요 |

---

## 4. 실행 순서 (권장 워크플로우)

### 4.1 최초 1회 — 스파이크

```bash
sp-assessor spike --root my-project
```

`output/_spike/spike_report.md` 를 열어 SP-1(파서)/SP-2(동적SQL 리터럴 비율)/SP-3(.NET 매칭률) 이 임계를 통과하는지 확인합니다. 미달 시 리포트에 표시된 대응 방안(§0 표)을 검토하세요. *(v0.1 은 ANTLR 이 아직 없어 SP-1 은 regex 폴백 완주율의 대리 지표입니다 — 리포트에 이 사실이 각주로 남습니다.)*

### 4.2 입력 검증

```bash
sp-assessor validate --root my-project
```

- `ERROR` 가 하나라도 있으면 이후 단계 실행 전에 반드시 해결하세요 (헤더 누락, body 파일 부재, override REASON 누락 등).
- `WARN`/`INFO` 는 진행은 가능하나 검토를 권장하는 항목입니다 (INVALID 객체, WRAP 소스, dblink 원격 객체 미제공 등).

### 4.3 전체 파이프라인 실행

```bash
sp-assessor run --stage all --root my-project --tag baseline
```

- `--tag` 를 주면 `output/_snapshots/baseline/` 에 결과가 스냅샷으로 저장됩니다 (이후 `diff` 에 사용).
- 특정 단계만 재실행하려면 `--stage s3` 처럼 지정합니다 (이전 단계 산출물을 그대로 읽습니다).

### 4.4 산출물 검토 → override 작성

각 단계는 "검토 필수" 파일을 남깁니다. **이 파일들을 먼저 확인**하세요:

| 파일 | 의미 | 대응 override |
|---|---|---|
| `output/s1_inventory/s1_unresolved.csv` | 미해석 시노님/원격/이름충돌/WRAP | `override/s1_inventory_override.csv` |
| `output/s2_metrics/s2_dynsql_hints.csv` | 변수 조립 동적 SQL (분석 불가) | `override/s2_dynsql_resolve.csv` |
| `output/s2_metrics/s2_parse_failures.csv` | 파싱 실패/WRAP | (수동 계측 시) `override/s2_metrics_override.csv` |
| `output/s3_graph/s3_review.csv` | LOW confidence 엣지, GRANT_MISSING, 고아 노드, 대형 SCC | `override/s3_edges_override.csv`, `override/s3_cluster_override.csv` |
| `output/s4_scoring/s4_review.csv` | 경계선 SP, PARSE_MODE≠AST, SCC 상향, DEFER 자동판정 | `override/s4_strategy_override.csv` |

override 작성 예시 (`override/s1_inventory_override.csv`):

```csv
SP_ID,ACTION,RESOLVED_TARGET,REASON
APP_OWNER.SYNC_HR,RESOLVE_REMOTE,REMOTE_HR.EMPLOYEE@HR_LINK,담당자 확인 결과 실제 존재하는 원격 테이블
```

> **모든 override 행은 `REASON` 필수**입니다 (누락 시 `validate`/해당 stage 에서 ERROR).

override 작성 후 재실행:

```bash
sp-assessor run --stage all --root my-project --tag baseline_reviewed
sp-assessor override lint --root my-project    # 충돌/중복 점검
```

### 4.5 (선택) 공수 캘리브레이션

`AUTO_SIMPLE`/`AUTO_ASSISTED` 후보 중 실제 전환을 마친 SP 의 실적 공수를 `override/pilot_effort.csv` 에 누적합니다:

```csv
SP_ID,ACTUAL_MD
APP_OWNER.GET_ORDER,2.5
...
```

`config.calibration.min_samples`(기본 30) 이상 쌓이면 `sp-assessor run --stage s4` 재실행 시 자동으로 Ridge 회귀 기반 캘리브레이션이 수행되고, `output/s4_scoring/s4_weight_suggestion.yaml` 에 가중치 제안치와 VIF(다중공선성) 진단이 남습니다. 표본 부족 시엔 INFO 로그만 남기고 휴리스틱 추정치를 그대로 씁니다.

### 4.6 최종 리포트 확인

`output/s5_report/` 아래:

- `s5_summary.md` — 총괄 (분면/전략 분포, 총 공수 P50/P90)
- `s5_roadmap.csv` — Wave × Cluster 전환 로드맵
- `s5_inventory_full.xlsx` — 전 SP 상세 (필터/정렬용)
- `s5_quadrant.svg` — (D,C) 산점도
- `s5_dblink_cutpoints.md` — DB Link 절단점 + 대체 인터페이스 제안
- `s5_override_audit.md` — override 이력 감사 (git 이력 포함)
- `s5_risk_register.md` — WRAP/autonomous_tx/mutating trigger/대형 SCC 등 리스크 집계

### 4.7 정기 재평가 (분기 등)

```bash
# 추출 스크립트 재실행 후 input/ 갱신 → 재실행
sp-assessor run --stage all --root my-project --tag 2026q3

# 이전 분기 대비 변경점만 확인
sp-assessor diff --stage s4 --from 2026q2 --to 2026q3 --root my-project
```

---

## 5. 커맨드 레퍼런스

```bash
sp-assessor validate --root <dir> [--config <path>]
sp-assessor run --stage all|s1|s2|s3|s4|s5 --root <dir> [--tag <name>] [--config <path>]
sp-assessor spike --root <dir> [--config <path>]
sp-assessor diff --stage s1|s2|s3|s4 --from <tag> --to <tag> --root <dir>
sp-assessor override lint --root <dir>
```

- `--root` 생략 시 현재 디렉터리.
- `--config` 생략 시 `<root>/config.yaml` (그마저 없으면 기본값 + 경고).
- `diff` 는 `s5` 는 지원하지 않습니다 (리포트라 SP 단위 비교 대상이 아님).
- `validate`/`override lint` 는 `ERROR` 가 있으면 종료코드 1을 반환합니다 (CI 게이팅에 활용 가능).

---

## 6. 트러블슈팅

| 증상 | 원인 | 조치 |
|---|---|---|
| `run --stage s1` 에서 `in_objects.csv is empty or missing` | 입력 CSV 미배치 | `input/` 경로/파일명 확인, 추출 스크립트 재확인 |
| `s2 ... s1_inventory.csv missing/empty — run stage s1 first` | 단계 순서 위반 | S1 → S2 → S3 → S4 → S5 순서로 실행 (또는 `--stage all`) |
| `validate` 에서 `BODY_FILE_MISSING` | 트리거/잡 본문 파일 경로 불일치 | `input/bodies/trigger|job/{OWNER}/{NAME}.txt` 실제 존재 확인 |
| `s4_weight_suggestion.yaml` 이 안 생김 | scikit-learn 미설치 또는 표본 부족 | `pip install -e ".[scoring]"`, `pilot_effort.csv` 표본 수 확인 |
| `s5_inventory_full.xlsx`/`s5_quadrant.svg` 없음 | openpyxl/matplotlib 미설치 | `pip install -e ".[viz]"` |
| `diff` 에서 `snapshot not found` | 해당 태그로 `run --tag` 를 안 함 | 먼저 `run --stage all --tag <name>` 실행 |
| `override lint` 에러 | 같은 SP_ID 에 상충 ACTION/STRATEGY, REASON 공백 | 리포트에 찍힌 파일/행 수정 |

---

## 7. 개발자용 — 테스트 실행

```bash
pip install -e ".[dev,scoring,viz]"
pytest -q
ruff check sp_assessor/ tests/
```

`tests/` 는 `examples/` 데이터로 S1~S5, spike/diff/override lint, validate 규칙을 end-to-end 로 검증합니다. 새 규칙/지표를 추가할 때는 `examples/input/*.csv` 에 해당 케이스를 최소 단위로 추가하고 대응 테스트를 함께 작성하세요.
