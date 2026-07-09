"""S1 — 인벤토리 정규화 및 식별자 해석."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from sp_assessor.core.config import Config
from sp_assessor.core.paths import ProjectPaths
from sp_assessor.io.csv_io import read_csv, write_csv
from sp_assessor.io.csv_schemas import OVERRIDE_SCHEMAS


PKG_SUBPROG_RE = re.compile(
    r"^\s*(?:PROCEDURE|FUNCTION)\s+([A-Z_][A-Z0-9_$#]*)",
    re.IGNORECASE,
)


def _make_sp_id(owner: str, pkg: str | None, name: str, overload: str | None) -> str:
    parts = [owner]
    if pkg:
        parts.append(pkg)
    parts.append(name)
    base = ".".join(parts)
    ov = 0 if overload is None or str(overload).strip() in ("", "nan") else int(float(overload))
    if ov > 0 or pkg:
        return f"{base}#{ov}"
    return base


def _apply_exclude_patterns(df: pd.DataFrame, patterns: list[str]) -> tuple[pd.DataFrame, pd.DataFrame]:
    if not patterns or df.empty:
        return df, df.head(0).assign(EXCLUDE_REASON=pd.Series(dtype=str))
    combined = "|".join(f"(?:{p})" for p in patterns)
    rx = re.compile(combined)
    mask = df["OBJECT_NAME"].astype(str).apply(lambda n: bool(rx.search(n)))
    included = df[~mask].copy()
    excluded = df[mask].copy()
    excluded["EXCLUDE_REASON"] = "matched exclude_name_patterns"
    return included, excluded


def _decompose_packages(objects: pd.DataFrame, sources: pd.DataFrame,
                        arguments: pd.DataFrame) -> pd.DataFrame:
    """PACKAGE BODY 내부 서브프로그램 개별 단위 분해. 오버로드는 #N 접미."""
    rows: list[dict] = []
    plain_types = {"PROCEDURE", "FUNCTION", "TRIGGER"}

    for _, obj in objects.iterrows():
        t = obj["OBJECT_TYPE"]
        if t in plain_types:
            rows.append({
                "SP_ID": _make_sp_id(obj["OWNER"], None, obj["OBJECT_NAME"], None),
                "OWNER": obj["OWNER"], "PKG": "", "NAME": obj["OBJECT_NAME"],
                "OVERLOAD_NO": 0, "TYPE": t, "STATUS": obj.get("STATUS", ""),
            })

    if not arguments.empty:
        pkg_args = arguments[arguments["PACKAGE_NAME"].astype(str).str.len() > 0]
        seen: set[tuple] = set()
        for _, arg in pkg_args.iterrows():
            key = (arg["OWNER"], arg["PACKAGE_NAME"], arg["OBJECT_NAME"], arg.get("OVERLOAD") or "")
            if key in seen:
                continue
            seen.add(key)
            ov_raw = arg.get("OVERLOAD")
            ov = 0 if ov_raw is None or str(ov_raw).strip() in ("", "nan") else int(float(ov_raw))
            rows.append({
                "SP_ID": _make_sp_id(arg["OWNER"], arg["PACKAGE_NAME"], arg["OBJECT_NAME"], ov),
                "OWNER": arg["OWNER"], "PKG": arg["PACKAGE_NAME"],
                "NAME": arg["OBJECT_NAME"], "OVERLOAD_NO": ov,
                "TYPE": "PACKAGE_SUBPROGRAM", "STATUS": "",
            })

    if not sources.empty:
        pkg_bodies = sources[sources["TYPE"] == "PACKAGE BODY"]
        existing = {(r["OWNER"], r["PKG"], r["NAME"]) for r in rows if r["PKG"]}
        for (owner, name), grp in pkg_bodies.groupby(["OWNER", "NAME"]):
            for _, line in grp.iterrows():
                m = PKG_SUBPROG_RE.match(str(line.get("TEXT", "")))
                if m:
                    subname = m.group(1).upper()
                    key = (owner, name, subname)
                    if key in existing:
                        continue
                    existing.add(key)
                    rows.append({
                        "SP_ID": _make_sp_id(owner, name, subname, 0),
                        "OWNER": owner, "PKG": name, "NAME": subname,
                        "OVERLOAD_NO": 0, "TYPE": "PACKAGE_SUBPROGRAM", "STATUS": "",
                    })

    return pd.DataFrame(rows)


def _compute_loc(inventory: pd.DataFrame, sources: pd.DataFrame) -> pd.DataFrame:
    if sources.empty or inventory.empty:
        inventory["LOC"] = 0
        return inventory

    def strip_comment(t: str) -> bool:
        s = str(t).strip()
        if not s:
            return False
        if s.startswith("--"):
            return False
        return True

    src = sources.copy()
    src["_is_code"] = src["TEXT"].apply(strip_comment)

    loc_by_obj = (src[src["_is_code"]]
                  .groupby(["OWNER", "NAME"])["LINE"].count()
                  .rename("LOC").reset_index())

    def lookup(row):
        owner, pkg, name = row["OWNER"], row["PKG"], row["NAME"]
        target = pkg if pkg else name
        hit = loc_by_obj[(loc_by_obj["OWNER"] == owner) & (loc_by_obj["NAME"] == target)]
        return int(hit["LOC"].iloc[0]) if not hit.empty else 0

    inventory["LOC"] = inventory.apply(lookup, axis=1)
    return inventory


def _detect_wrapped(inventory: pd.DataFrame, sources: pd.DataFrame) -> pd.DataFrame:
    inventory["WRAPPED"] = "N"
    if sources.empty:
        return inventory
    first_lines = sources[sources["LINE"] == 1]
    wrapped_names = first_lines[first_lines["TEXT"].astype(str).str.upper().str.contains("WRAPPED")]
    wrapped_set = set(zip(wrapped_names["OWNER"], wrapped_names["NAME"]))

    def is_wrapped(row):
        target = row["PKG"] if row["PKG"] else row["NAME"]
        return "Y" if (row["OWNER"], target) in wrapped_set else "N"

    inventory["WRAPPED"] = inventory.apply(is_wrapped, axis=1)
    return inventory


def _compute_cross_schema_callable(inventory: pd.DataFrame,
                                   tab_privs: pd.DataFrame,
                                   role_privs: pd.DataFrame,
                                   target_schemas: list[str]) -> pd.DataFrame:
    inventory["CROSS_SCHEMA_CALLABLE"] = "N"
    if inventory.empty:
        return inventory

    frames = []
    if not tab_privs.empty:
        frames.append(tab_privs[["GRANTEE", "OWNER", "TABLE_NAME", "PRIVILEGE"]])
    if not role_privs.empty:
        frames.append(role_privs[["GRANTEE", "OWNER", "TABLE_NAME", "PRIVILEGE"]])
    if not frames:
        return inventory

    all_privs = pd.concat(frames, ignore_index=True)
    exec_privs = all_privs[all_privs["PRIVILEGE"] == "EXECUTE"]
    target_set = set(target_schemas)
    external = exec_privs[~exec_privs["GRANTEE"].isin(target_set | {"PUBLIC"})]
    callable_keys = set(zip(external["OWNER"], external["TABLE_NAME"]))

    def check(row):
        target = row["PKG"] if row["PKG"] else row["NAME"]
        return "Y" if (row["OWNER"], target) in callable_keys else "N"

    inventory["CROSS_SCHEMA_CALLABLE"] = inventory.apply(check, axis=1)
    return inventory


def _compute_suspect_unused(inventory: pd.DataFrame, exec_stats: pd.DataFrame) -> pd.DataFrame:
    inventory["SUSPECT_UNUSED"] = "N"
    if exec_stats.empty:
        return inventory
    zero_exec = exec_stats[exec_stats["EXEC_COUNT_PERIOD"].astype(float) == 0]
    zero_set = set(zip(zero_exec["OWNER"], zero_exec["OBJECT_NAME"]))

    def check(row):
        target = row["PKG"] if row["PKG"] else row["NAME"]
        return "Y" if (row["OWNER"], target) in zero_set else "N"

    inventory["SUSPECT_UNUSED"] = inventory.apply(check, axis=1)
    return inventory


def _compute_remote_ref_count(inventory: pd.DataFrame, deps: pd.DataFrame) -> pd.DataFrame:
    inventory["REMOTE_REF_COUNT"] = 0
    if deps.empty or "REFERENCED_LINK_NAME" not in deps.columns:
        return inventory
    remote_deps = deps[deps["REFERENCED_LINK_NAME"].astype(str).str.len() > 0]
    if remote_deps.empty:
        return inventory
    counts = remote_deps.groupby(["OWNER", "NAME"]).size().rename("cnt").reset_index()
    lookup = {(r["OWNER"], r["NAME"]): int(r["cnt"]) for _, r in counts.iterrows()}

    def lookup_fn(row):
        target = row["PKG"] if row["PKG"] else row["NAME"]
        return lookup.get((row["OWNER"], target), 0)

    inventory["REMOTE_REF_COUNT"] = inventory.apply(lookup_fn, axis=1)
    return inventory


def _build_unresolved(deps: pd.DataFrame, synonyms: pd.DataFrame,
                     objects: pd.DataFrame, target_schemas: list[str]) -> pd.DataFrame:
    rows: list[dict] = []

    if not deps.empty and "REFERENCED_LINK_NAME" in deps.columns:
        remote = deps[deps["REFERENCED_LINK_NAME"].astype(str).str.len() > 0]
        for _, r in remote.iterrows():
            rows.append({
                "SP_ID": _make_sp_id(r["OWNER"], None, r["NAME"], None),
                "REASON_CODE": "UNRESOLVED_REMOTE",
                "DETAIL": f"{r['REFERENCED_OWNER']}.{r['REFERENCED_NAME']}@{r['REFERENCED_LINK_NAME']}",
            })

    if not objects.empty:
        invalid = objects[objects["STATUS"] == "INVALID"]
        for _, r in invalid.iterrows():
            rows.append({
                "SP_ID": _make_sp_id(r["OWNER"], None, r["OBJECT_NAME"], None),
                "REASON_CODE": "INVALID_STATUS",
                "DETAIL": r["OBJECT_TYPE"],
            })
        name_counts = objects[objects["OBJECT_TYPE"].isin(
            ["PROCEDURE", "FUNCTION", "PACKAGE"]
        )].groupby("OBJECT_NAME").size()
        collisions = name_counts[name_counts > 1].index.tolist()
        for name in collisions:
            owners = objects[objects["OBJECT_NAME"] == name]["OWNER"].unique().tolist()
            rows.append({
                "SP_ID": name,
                "REASON_CODE": "NAME_COLLISION",
                "DETAIL": f"owners={owners}",
            })

    return pd.DataFrame(rows)


def _build_grant_matrix(tab_privs: pd.DataFrame,
                        role_privs: pd.DataFrame) -> pd.DataFrame:
    frames = []
    if not tab_privs.empty:
        d = tab_privs.copy()
        d["VIA_ROLES"] = ""
        d["DEPTH"] = 0
        frames.append(d[["GRANTEE", "OWNER", "TABLE_NAME", "PRIVILEGE", "DEPTH", "VIA_ROLES"]])
    if not role_privs.empty:
        frames.append(role_privs[["GRANTEE", "OWNER", "TABLE_NAME", "PRIVILEGE", "DEPTH", "VIA_ROLES"]])
    if not frames:
        return pd.DataFrame(columns=["GRANTEE", "OWNER", "TABLE_NAME",
                                     "PRIVILEGE", "DEPTH", "VIA_ROLES"])
    result = pd.concat(frames, ignore_index=True).drop_duplicates(
        subset=["GRANTEE", "OWNER", "TABLE_NAME", "PRIVILEGE"], keep="first"
    )
    return result


def _apply_overrides(inventory: pd.DataFrame, excluded: pd.DataFrame,
                     override_path: Path, logger: logging.Logger) -> tuple[pd.DataFrame, pd.DataFrame]:
    if not override_path.exists():
        return inventory, excluded
    ov = read_csv(override_path)
    if ov.empty:
        return inventory, excluded
    for _, row in ov.iterrows():
        sp_id = row["SP_ID"]
        action = str(row["ACTION"]).upper()
        if action == "EXCLUDE":
            hit = inventory[inventory["SP_ID"] == sp_id]
            if not hit.empty:
                removed = hit.copy()
                removed["EXCLUDE_REASON"] = f"override: {row.get('REASON', '')}"
                excluded = pd.concat([excluded, removed], ignore_index=True)
                inventory = inventory[inventory["SP_ID"] != sp_id]
                logger.info("override EXCLUDE applied: %s", sp_id)
        elif action == "INCLUDE":
            was_excluded = excluded[excluded["SP_ID"] == sp_id]
            if not was_excluded.empty:
                restored = was_excluded.drop(columns=["EXCLUDE_REASON"], errors="ignore")
                inventory = pd.concat([inventory, restored], ignore_index=True)
                excluded = excluded[excluded["SP_ID"] != sp_id]
                logger.info("override INCLUDE applied: %s", sp_id)
    return inventory, excluded


def run(paths: ProjectPaths, config: Config, logger: logging.Logger) -> dict:
    logger.info("S1 시작")
    objects = read_csv(paths.input_dir / "in_objects.csv")
    sources = read_csv(paths.input_dir / "in_source.csv")
    args_ = read_csv(paths.input_dir / "in_arguments.csv")
    deps = read_csv(paths.input_dir / "in_dependencies.csv")
    synonyms = read_csv(paths.input_dir / "in_synonyms.csv")
    tab_privs = read_csv(paths.input_dir / "in_tab_privs.csv")
    role_privs = read_csv(paths.input_dir / "in_role_privs.csv")
    exec_stats = read_csv(paths.input_dir / "in_exec_stats.csv")

    if objects.empty:
        raise RuntimeError("in_objects.csv is empty or missing")

    targets = objects[objects["OWNER"].isin(config.target_schemas)] if config.target_schemas else objects
    sp_types = ["PROCEDURE", "FUNCTION", "PACKAGE BODY", "TRIGGER"]
    candidates = targets[targets["OBJECT_TYPE"].isin(sp_types)].copy()
    logger.info("대상 후보: %d", len(candidates))

    included, excluded = _apply_exclude_patterns(candidates, config.exclude_name_patterns)
    logger.info("패턴 제외: %d, 잔여: %d", len(excluded), len(included))

    inventory = _decompose_packages(included, sources, args_)
    logger.info("서브프로그램 분해 후: %d", len(inventory))

    inventory = _compute_loc(inventory, sources)
    inventory = _detect_wrapped(inventory, sources)
    inventory = _compute_cross_schema_callable(inventory, tab_privs, role_privs, config.target_schemas)
    inventory = _compute_suspect_unused(inventory, exec_stats)
    inventory = _compute_remote_ref_count(inventory, deps)

    override_path = paths.override_dir / OVERRIDE_SCHEMAS["s1_inventory"].filename
    inventory, excluded = _apply_overrides(inventory, excluded, override_path, logger)

    unresolved = _build_unresolved(deps, synonyms, targets, config.target_schemas)
    grant_matrix = _build_grant_matrix(tab_privs, role_privs)

    inv_cols = ["SP_ID", "OWNER", "PKG", "NAME", "OVERLOAD_NO", "TYPE", "LOC",
                "STATUS", "WRAPPED", "CROSS_SCHEMA_CALLABLE",
                "SUSPECT_UNUSED", "REMOTE_REF_COUNT"]
    for c in inv_cols:
        if c not in inventory.columns:
            inventory[c] = ""
    inventory = inventory[inv_cols].sort_values("SP_ID").reset_index(drop=True)

    out = paths.stage_output("s1_inventory")
    write_csv(inventory, out / "s1_inventory.csv")
    write_csv(excluded, out / "s1_excluded.csv")
    write_csv(unresolved, out / "s1_unresolved.csv")
    write_csv(grant_matrix, out / "s1_grant_matrix.csv")

    logger.info("S1 완료 — inventory=%d excluded=%d unresolved=%d grants=%d",
                len(inventory), len(excluded), len(unresolved), len(grant_matrix))
    if not unresolved.empty:
        logger.warning("검토 필수: s1_unresolved.csv (%d rows) → override/s1_inventory_override.csv",
                       len(unresolved))

    return {
        "inventory": len(inventory),
        "excluded": len(excluded),
        "unresolved": len(unresolved),
    }
