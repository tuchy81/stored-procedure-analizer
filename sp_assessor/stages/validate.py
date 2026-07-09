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
            target = paths.input_dir / rel
            if not target.exists():
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
    return report
