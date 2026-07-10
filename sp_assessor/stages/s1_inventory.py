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
from sp_assessor.util.text import clean_str


PKG_SUBPROG_RE = re.compile(
    r"^\s*(?:PROCEDURE|FUNCTION)\s+([A-Z_][A-Z0-9_$#]*)",
    re.IGNORECASE,
)

# `SCHEMA.OBJECT@DB_LINK` / `OBJECT@DB_LINK` 직접 참조 탐지 (§S1-5)
DBLINK_REF_RE = re.compile(
    r"\b[A-Za-z_][A-Za-z0-9_$#.]*@([A-Za-z_][A-Za-z0-9_$#]*)\b"
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
        # NOTE: `.astype(str).str.len() > 0` 는 NaN -> "nan" 변환 여부가 pandas 버전/문자열
        # dtype 백엔드에 따라 달라지는 미정의 동작에 의존해 위험함 (일부 환경에서 NaN 이
        # "nan" 문자열로 바뀌어 길이>0 이 되면서 비-패키지 인자 행까지 잘못 포함되어
        # 이후 _make_sp_id 에서 NaN(float) 을 문자열 join 하려다 TypeError 발생).
        # NaN-안전한 clean_str() 로 명시적으로 판정한다.
        pkg_args = arguments[arguments["PACKAGE_NAME"].map(lambda v: bool(clean_str(v)))]
        seen: set[tuple] = set()
        for _, arg in pkg_args.iterrows():
            key = (arg["OWNER"], arg["PACKAGE_NAME"], arg["OBJECT_NAME"], clean_str(arg.get("OVERLOAD")))
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


def _compute_remote_ref_count(inventory: pd.DataFrame, deps: pd.DataFrame,
                              dblink_refs: pd.DataFrame | None = None) -> pd.DataFrame:
    inventory["REMOTE_REF_COUNT"] = 0
    if inventory.empty:
        return inventory

    lookup: dict[tuple, int] = {}

    if not deps.empty and "REFERENCED_LINK_NAME" in deps.columns:
        remote_deps = deps[deps["REFERENCED_LINK_NAME"].map(lambda v: bool(clean_str(v)))]
        if not remote_deps.empty:
            counts = remote_deps.groupby(["OWNER", "NAME"]).size().rename("cnt").reset_index()
            for _, r in counts.iterrows():
                key = (r["OWNER"], r["NAME"])
                lookup[key] = lookup.get(key, 0) + int(r["cnt"])

    if dblink_refs is not None and not dblink_refs.empty:
        counts = dblink_refs.groupby(["OWNER", "NAME"]).size().rename("cnt").reset_index()
        for _, r in counts.iterrows():
            key = (r["OWNER"], r["NAME"])
            lookup[key] = lookup.get(key, 0) + int(r["cnt"])

    def lookup_fn(row):
        target = row["PKG"] if row["PKG"] else row["NAME"]
        return lookup.get((row["OWNER"], target), 0)

    inventory["REMOTE_REF_COUNT"] = inventory.apply(lookup_fn, axis=1)
    return inventory


@dataclass
class SynonymMaps:
    private: dict[tuple[str, str], dict]
    public: dict[str, dict]

    def resolve(self, schema: str, name: str) -> dict | None:
        """private > public 우선순위로 시노님 실체 해석."""
        hit = self.private.get((schema, name))
        if hit is not None:
            return hit
        return self.public.get(name)


def _resolve_synonyms(synonyms: pd.DataFrame) -> SynonymMaps:
    """PUBLIC + private 시노님 병합. db_link 존재 시 REMOTE 실체로 태깅 (§S1-4)."""
    private: dict[tuple[str, str], dict] = {}
    public: dict[str, dict] = {}
    if synonyms.empty:
        return SynonymMaps(private, public)

    for _, row in synonyms.iterrows():
        db_link = clean_str(row.get("DB_LINK"))
        entry = {
            "TABLE_OWNER": row["TABLE_OWNER"],
            "TABLE_NAME": row["TABLE_NAME"],
            "DB_LINK": db_link,
            "IS_REMOTE": bool(db_link),
        }
        owner = str(row["OWNER"]).strip()
        name = str(row["SYNONYM_NAME"]).strip()
        if owner.upper() == "PUBLIC":
            public[name] = entry
        else:
            private[(owner, name)] = entry
    return SynonymMaps(private, public)


def _load_remote_objects(input_dir: Path) -> dict[str, set[tuple[str, str]]]:
    """`in_remote_objects__{DB_LINK}.csv` 로딩 → {DB_LINK: {(OWNER, OBJECT_NAME), ...}} (§2.3/§9.4)."""
    result: dict[str, set[tuple[str, str]]] = {}
    if not input_dir.exists():
        return result
    for p in sorted(input_dir.glob("in_remote_objects__*.csv")):
        link_name = p.stem[len("in_remote_objects__"):]
        df = read_csv(p)
        if df.empty or "OWNER" not in df.columns or "OBJECT_NAME" not in df.columns:
            continue
        result[link_name] = set(zip(df["OWNER"], df["OBJECT_NAME"]))
    return result


def _scan_source_dblinks(sources: pd.DataFrame) -> pd.DataFrame:
    """소스 텍스트 내 `OBJECT@DB_LINK` 직접 참조 탐지 (§S1-5)."""
    rows: list[dict] = []
    if sources.empty:
        return pd.DataFrame(rows, columns=["OWNER", "NAME", "LINE", "DB_LINK"])
    for _, line in sources.iterrows():
        for m in DBLINK_REF_RE.finditer(str(line.get("TEXT", ""))):
            rows.append({
                "OWNER": line["OWNER"], "NAME": line["NAME"],
                "LINE": line["LINE"], "DB_LINK": m.group(1),
            })
    return pd.DataFrame(rows, columns=["OWNER", "NAME", "LINE", "DB_LINK"])


def _build_unresolved(deps: pd.DataFrame, synonyms: pd.DataFrame,
                     objects: pd.DataFrame, target_schemas: list[str],
                     synonym_maps: SynonymMaps | None = None,
                     remote_objects: dict[str, set[tuple[str, str]]] | None = None,
                     db_links: pd.DataFrame | None = None,
                     dblink_refs: pd.DataFrame | None = None) -> pd.DataFrame:
    rows: list[dict] = []
    synonym_maps = synonym_maps or SynonymMaps({}, {})
    remote_objects = remote_objects or {}

    def _remote_resolved(link: str, owner: str, name: str) -> bool:
        return (owner, name) in remote_objects.get(link, set())

    if not deps.empty and "REFERENCED_LINK_NAME" in deps.columns:
        remote = deps[deps["REFERENCED_LINK_NAME"].map(lambda v: bool(clean_str(v)))]
        for _, r in remote.iterrows():
            link = r["REFERENCED_LINK_NAME"]
            if _remote_resolved(link, r["REFERENCED_OWNER"], r["REFERENCED_NAME"]):
                continue
            rows.append({
                "SP_ID": _make_sp_id(r["OWNER"], None, r["NAME"], None),
                "REASON_CODE": "UNRESOLVED_REMOTE",
                "DETAIL": f"{r['REFERENCED_OWNER']}.{r['REFERENCED_NAME']}@{link}",
            })

    if not deps.empty and "REFERENCED_TYPE" in deps.columns:
        syn_deps = deps[deps["REFERENCED_TYPE"].astype(str).str.upper() == "SYNONYM"]
        for _, r in syn_deps.iterrows():
            resolved = synonym_maps.resolve(r["REFERENCED_OWNER"], r["REFERENCED_NAME"])
            sp_id = _make_sp_id(r["OWNER"], None, r["NAME"], None)
            if resolved is None:
                rows.append({
                    "SP_ID": sp_id,
                    "REASON_CODE": "UNRESOLVED_SYNONYM",
                    "DETAIL": f"{r['REFERENCED_OWNER']}.{r['REFERENCED_NAME']}",
                })
            elif resolved["IS_REMOTE"]:
                if not _remote_resolved(resolved["DB_LINK"], resolved["TABLE_OWNER"], resolved["TABLE_NAME"]):
                    rows.append({
                        "SP_ID": sp_id,
                        "REASON_CODE": "UNRESOLVED_REMOTE",
                        "DETAIL": f"via synonym {r['REFERENCED_OWNER']}.{r['REFERENCED_NAME']} -> "
                                  f"{resolved['TABLE_OWNER']}.{resolved['TABLE_NAME']}@{resolved['DB_LINK']}",
                    })

    if dblink_refs is not None and not dblink_refs.empty:
        known_links = set()
        if db_links is not None and not db_links.empty:
            known_links = set(db_links["DB_LINK"].astype(str))
        unknown = dblink_refs[~dblink_refs["DB_LINK"].isin(known_links)]
        for (owner, name), grp in unknown.groupby(["OWNER", "NAME"]):
            rows.append({
                "SP_ID": _make_sp_id(owner, None, name, None),
                "REASON_CODE": "UNKNOWN_DB_LINK",
                "DETAIL": f"@{sorted(grp['DB_LINK'].unique())[0]} (source line {int(grp['LINE'].iloc[0])})",
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

    dblink_refs = _scan_source_dblinks(sources)
    inventory = _compute_remote_ref_count(inventory, deps, dblink_refs)

    override_path = paths.override_dir / OVERRIDE_SCHEMAS["s1_inventory"].filename
    inventory, excluded = _apply_overrides(inventory, excluded, override_path, logger)

    synonym_maps = _resolve_synonyms(synonyms)
    remote_objects = _load_remote_objects(paths.input_dir)
    db_links = read_csv(paths.input_dir / "in_db_links.csv")
    unresolved = _build_unresolved(deps, synonyms, targets, config.target_schemas,
                                   synonym_maps=synonym_maps, remote_objects=remote_objects,
                                   db_links=db_links, dblink_refs=dblink_refs)
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
