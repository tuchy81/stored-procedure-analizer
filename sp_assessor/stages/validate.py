"""validate 커맨드 — 입력/override 스키마 및 참조 무결성 검증."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from sp_assessor.core.config import Config
from sp_assessor.core.paths import ProjectPaths
from sp_assessor.io.csv_io import read_headers, read_csv
from sp_assessor.io.csv_schemas import INPUT_SCHEMAS, OVERRIDE_SCHEMAS


@dataclass
class Finding:
    level: str          # ERROR | WARN | INFO
    code: str
    message: str


@dataclass
class ValidationReport:
    findings: list[Finding] = field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        return any(f.level == "ERROR" for f in self.findings)

    def add(self, level: str, code: str, message: str) -> None:
        self.findings.append(Finding(level, code, message))

    def summary(self) -> dict[str, int]:
        s = {"ERROR": 0, "WARN": 0, "INFO": 0}
        for f in self.findings:
            s[f.level] = s.get(f.level, 0) + 1
        return s


def _check_schema(path: Path, schema, report: ValidationReport) -> None:
    if not path.exists():
        if schema.required:
            report.add("ERROR", "MISSING_FILE", f"required input missing: {path.name}")
        else:
            report.add("INFO", "OPTIONAL_ABSENT", f"optional input absent: {path.name}")
        return

    headers = read_headers(path)
    missing = [c for c in schema.required_columns if c not in headers]
    if missing:
        report.add("ERROR", "SCHEMA_MISMATCH",
                   f"{path.name}: missing columns {missing}")


def _check_source_objects_consistency(paths: ProjectPaths, report: ValidationReport) -> None:
    obj_path = paths.input_dir / "in_objects.csv"
    src_path = paths.input_dir / "in_source.csv"
    if not obj_path.exists() or not src_path.exists():
        return
    objs = read_csv(obj_path)
    srcs = read_csv(src_path)
    if objs.empty or srcs.empty:
        return
    obj_keys = set(zip(objs["OWNER"], objs["OBJECT_NAME"]))
    src_keys = set(zip(srcs["OWNER"], srcs["NAME"]))
    orphan = src_keys - obj_keys
    if orphan:
        report.add("ERROR", "SOURCE_ORPHAN",
                   f"in_source has {len(orphan)} names not in in_objects (e.g., {list(orphan)[:3]})")


def _check_bodies_files(paths: ProjectPaths, report: ValidationReport) -> None:
    for csv_name, col in (("in_triggers.csv", "BODY_FILE"),
                          ("in_scheduler_jobs.csv", "ACTION_FILE")):
        p = paths.input_dir / csv_name
        if not p.exists():
            continue
        df = read_csv(p)
        if df.empty or col not in df.columns:
            continue
        for rel in df[col].dropna().unique():
            if not rel:
                continue
            if len(rel) > 255:
                continue  # 상대경로로 보기엔 너무 김 — §2.5 위반은 _check_large_fields_inlined 에서 별도 보고
            try:
                target = paths.input_dir / rel
                exists = target.exists()
            except OSError:
                report.add("ERROR", "BODY_FILE_MISSING",
                           f"{csv_name}: body file path invalid: {rel[:80]}...")
                continue
            if not exists:
                report.add("ERROR", "BODY_FILE_MISSING",
                           f"{csv_name}: body file missing: {rel}")


def _check_override_reasons(paths: ProjectPaths, report: ValidationReport) -> None:
    for key, schema in OVERRIDE_SCHEMAS.items():
        p = paths.override_dir / schema.filename
        if not p.exists():
            continue
        df = read_csv(p)
        if df.empty or "REASON" not in df.columns:
            continue
        missing_reason = df["REASON"].isna() | (df["REASON"].astype(str).str.strip() == "")
        if missing_reason.any():
            n = int(missing_reason.sum())
            report.add("ERROR", "OVERRIDE_REASON_MISSING",
                       f"{schema.filename}: {n} rows missing REASON")


def _check_invalid_objects(paths: ProjectPaths, report: ValidationReport) -> None:
    p = paths.input_dir / "in_objects.csv"
    if not p.exists():
        return
    df = read_csv(p)
    if df.empty or "STATUS" not in df.columns:
        return
    invalid = df[df["STATUS"] == "INVALID"]
    if not invalid.empty:
        report.add("WARN", "INVALID_STATUS",
                   f"{len(invalid)} objects with STATUS=INVALID")


def _check_override_sp_id_exists(paths: ProjectPaths, report: ValidationReport) -> None:
    inv_path = paths.stage_output("s1_inventory") / "s1_inventory.csv"
    if not inv_path.exists():
        return  # S1 아직 미실행 — 검증 대상 없음
    inventory = read_csv(inv_path)
    if inventory.empty:
        return
    known_ids = set(inventory["SP_ID"])

    specs = [
        ("s1_inventory", "SP_ID"), ("s2_metrics", "SP_ID"), ("s2_dynsql_resolve", "SRC_SP_ID"),
        ("s3_cluster", "SP_ID"), ("s4_strategy", "SP_ID"), ("pilot_effort", "SP_ID"),
    ]
    for key, col in specs:
        schema = OVERRIDE_SCHEMAS[key]
        df = read_csv(paths.override_dir / schema.filename)
        if df.empty or col not in df.columns:
            continue
        unknown = set(df[col].dropna().astype(str)) - known_ids
        if unknown:
            report.add("ERROR", "OVERRIDE_SP_ID_UNKNOWN",
                       f"{schema.filename}: 인벤토리에 없는 SP_ID {sorted(unknown)[:5]}")

    edges_schema = OVERRIDE_SCHEMAS["s3_edges"]
    edges_df = read_csv(paths.override_dir / edges_schema.filename)
    if not edges_df.empty and {"SRC", "DST"} <= set(edges_df.columns):
        touched = set(edges_df["SRC"].dropna().astype(str)) | set(edges_df["DST"].dropna().astype(str))
        unknown = {t for t in touched if not t.startswith(("TABLE::", "REMOTE::", "APP::", "JOB::", "UNKNOWN::"))
                  and t not in known_ids}
        if unknown:
            report.add("ERROR", "OVERRIDE_SP_ID_UNKNOWN",
                       f"{edges_schema.filename}: 인벤토리에 없는 SP_ID {sorted(unknown)[:5]}")


def _check_large_fields_inlined(paths: ProjectPaths, report: ValidationReport, max_len: int = 500) -> None:
    for csv_name, col in (("in_triggers.csv", "BODY_FILE"), ("in_scheduler_jobs.csv", "ACTION_FILE")):
        p = paths.input_dir / csv_name
        if not p.exists():
            continue
        df = read_csv(p)
        if df.empty or col not in df.columns:
            continue
        too_long = df[df[col].astype(str).str.len() > max_len]
        if not too_long.empty:
            report.add("ERROR", "LARGE_FIELD_INLINED",
                       f"{csv_name}: {col} 컬럼에 {len(too_long)}건이 상대경로가 아닌 "
                       f"본문으로 보이는 긴 텍스트를 담고 있음 (§2.5 위반)")


def _check_dependency_orphans(paths: ProjectPaths, report: ValidationReport) -> None:
    obj_path = paths.input_dir / "in_objects.csv"
    dep_path = paths.input_dir / "in_dependencies.csv"
    if not obj_path.exists() or not dep_path.exists():
        return
    objs = read_csv(obj_path)
    deps = read_csv(dep_path)
    if objs.empty or deps.empty:
        return
    obj_keys = set(zip(objs["OWNER"], objs["OBJECT_NAME"]))
    local = deps[deps["REFERENCED_LINK_NAME"].isna() | (deps["REFERENCED_LINK_NAME"].astype(str).str.len() == 0)]
    ref_keys = set(zip(local["REFERENCED_OWNER"], local["REFERENCED_NAME"]))
    orphan = ref_keys - obj_keys
    if orphan:
        report.add("WARN", "DEPENDENCY_REFERENCED_NOT_IN_OBJECTS",
                   f"in_dependencies 참조 {len(orphan)}건이 in_objects 에 없음 (예: {sorted(orphan)[:3]})")


def _check_dblink_remote_objects_missing(paths: ProjectPaths, report: ValidationReport) -> None:
    dep_path = paths.input_dir / "in_dependencies.csv"
    if not dep_path.exists():
        return
    deps = read_csv(dep_path)
    if deps.empty or "REFERENCED_LINK_NAME" not in deps.columns:
        return
    links = set(deps["REFERENCED_LINK_NAME"].dropna().astype(str)) - {""}
    if not links:
        return
    provided = {p.stem[len("in_remote_objects__"):] for p in paths.input_dir.glob("in_remote_objects__*.csv")}
    missing = links - provided
    if missing:
        report.add("WARN", "REMOTE_OBJECTS_NOT_PROVIDED",
                   f"DB Link 참조가 있으나 in_remote_objects__*.csv 미제공: {sorted(missing)}")


def _check_name_collision(paths: ProjectPaths, report: ValidationReport) -> None:
    p = paths.input_dir / "in_objects.csv"
    if not p.exists():
        return
    df = read_csv(p)
    if df.empty:
        return
    sp_types = df[df["OBJECT_TYPE"].isin(["PROCEDURE", "FUNCTION", "PACKAGE"])]
    counts = sp_types.groupby("OBJECT_NAME")["OWNER"].nunique()
    collisions = counts[counts > 1]
    if not collisions.empty:
        report.add("WARN", "NAME_COLLISION",
                   f"{len(collisions)}개 이름이 다중 스키마에 존재: {sorted(collisions.index)[:5]}")


def _check_wrap_sources(paths: ProjectPaths, report: ValidationReport) -> None:
    p = paths.input_dir / "in_source.csv"
    if not p.exists():
        return
    df = read_csv(p)
    if df.empty or "LINE" not in df.columns:
        return
    first_lines = df[df["LINE"] == 1]
    wrapped = first_lines[first_lines["TEXT"].astype(str).str.upper().str.contains("WRAPPED")]
    if not wrapped.empty:
        report.add("WARN", "WRAP_SOURCE_PRESENT",
                   f"{len(wrapped)}개 SP 가 WRAP(암호화) 소스 — 정적 분석 불가, override 필요")


def _check_parser_success_rate(paths: ProjectPaths, config: Config, report: ValidationReport) -> None:
    p = paths.stage_output("s2_metrics") / "s2_parser_bench.csv"
    if not p.exists():
        return
    bench = read_csv(p)
    if bench.empty or "SUCCESS_RATE" not in bench.columns:
        return
    rate = float(bench["SUCCESS_RATE"].iloc[0])
    if rate < config.parser.target_success_rate:
        report.add("WARN", "PARSER_SUCCESS_RATE_LOW",
                   f"파서 성공률 {rate * 100:.1f}% < 목표 {config.parser.target_success_rate * 100:.0f}%")


def _check_calibration_sample_size(paths: ProjectPaths, config: Config, report: ValidationReport) -> None:
    p = paths.override_dir / OVERRIDE_SCHEMAS["pilot_effort"].filename
    if not p.exists():
        return
    df = read_csv(p)
    if df.empty:
        return
    if len(df) < config.calibration.min_samples:
        report.add("INFO", "CALIBRATION_SAMPLES_INSUFFICIENT",
                   f"pilot_effort.csv 표본 {len(df)}건 < min_samples {config.calibration.min_samples} "
                   f"— 캘리브레이션 미실행 (휴리스틱 공수만 산출됨)")


def validate(paths: ProjectPaths, config: Config) -> ValidationReport:
    report = ValidationReport()
    for schema in INPUT_SCHEMAS.values():
        _check_schema(paths.input_dir / schema.filename, schema, report)
    for schema in OVERRIDE_SCHEMAS.values():
        _check_schema(paths.override_dir / schema.filename, schema, report)
    _check_source_objects_consistency(paths, report)
    _check_bodies_files(paths, report)
    _check_override_reasons(paths, report)
    _check_invalid_objects(paths, report)
    _check_override_sp_id_exists(paths, report)
    _check_large_fields_inlined(paths, report)
    _check_dependency_orphans(paths, report)
    _check_dblink_remote_objects_missing(paths, report)
    _check_name_collision(paths, report)
    _check_wrap_sources(paths, report)
    _check_parser_success_rate(paths, config, report)
    _check_calibration_sample_size(paths, config, report)
    return report
