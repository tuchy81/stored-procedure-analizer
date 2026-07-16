"""의존성(참조) 분석 — 오브젝트 본문에서 외부 참조를 종류별로 식별하고 집합 내 오브젝트로 해석.

참조 종류(REF_TYPES):
  TABLE    로컬 테이블/뷰 (FROM/JOIN/INSERT INTO/UPDATE/DELETE/MERGE)
  CALL     타 스탠드얼론 프로시저/함수 호출
  PACKAGE  타 패키지 참조 (PKG.member)
  BUILTIN  Oracle 빌트인 패키지 (DBMS_*/UTL_*)
  SEQUENCE 시퀀스 (.NEXTVAL/.CURRVAL)
  DB_LINK  DB Link 직접 참조 (obj@link)
  REMOTE   (예약) 시노님 경유 원격 — 현 버전은 DB_LINK 로 통합 집계
  GRANT    GRANT 문으로 외부 노출된 오브젝트
"""
from __future__ import annotations

import re

from sqlscore.model import Reference, SourceObject
from sqlscore.textutil import clean

FROM_JOIN_RE = re.compile(r"\b(?:FROM|JOIN)\s+([A-Za-z0-9_$#.@\"]+)", re.IGNORECASE)
INSERT_INTO_RE = re.compile(r"\bINSERT\s+INTO\s+([A-Za-z0-9_$#.@\"]+)", re.IGNORECASE)
UPDATE_RE = re.compile(r"\bUPDATE\s+([A-Za-z0-9_$#.@\"]+)", re.IGNORECASE)
DELETE_RE = re.compile(r"\bDELETE\s+(?:FROM\s+)?([A-Za-z0-9_$#.@\"]+)", re.IGNORECASE)
MERGE_RE = re.compile(r"\bMERGE\s+INTO\s+([A-Za-z0-9_$#.@\"]+)", re.IGNORECASE)

QUALIFIED_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_$#]*)\s*\.\s*([A-Za-z_][A-Za-z0-9_$#]*)")
DBLINK_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_$#.]*)@([A-Za-z_][A-Za-z0-9_$#]*)")

# FROM 절 등에서 테이블이 아닌 것으로 취급해 제외
NON_TABLE = {"DUAL", "TABLE", "THE", "LATERAL", "XMLTABLE", "JSON_TABLE"}
SQL_NOISE = {
    "SELECT", "WHERE", "FROM", "GROUP", "ORDER", "HAVING", "AND", "OR", "SET",
    "VALUES", "INTO", "ON", "USING", "WHEN", "THEN", "ELSE", "END", "BEGIN",
}


class ObjectIndex:
    """이름 기반 해석 인덱스."""

    def __init__(self, objects: list[SourceObject]) -> None:
        self.standalone: dict[str, SourceObject] = {}   # PROCEDURE/FUNCTION
        self.packages: dict[str, SourceObject] = {}      # PACKAGE/PACKAGE_BODY (name -> 대표)
        self.tabviews: dict[str, SourceObject] = {}      # TABLE/VIEW/MVIEW
        for o in objects:
            nm = o.name.upper()
            if o.otype in ("PROCEDURE", "FUNCTION"):
                self.standalone.setdefault(nm, o)
            if o.otype in ("PACKAGE", "PACKAGE_BODY"):
                # BODY 를 우선(호출 대상 실체) — 없으면 spec
                if nm not in self.packages or o.otype == "PACKAGE_BODY":
                    self.packages[nm] = o
            if o.otype in ("TABLE", "VIEW", "MATERIALIZED_VIEW"):
                self.tabviews.setdefault(nm, o)


def _norm_token(tok: str) -> str:
    return re.sub(r"[^A-Za-z0-9_$#.@]", "", tok).upper()


def _base_name(qualified: str) -> str:
    """SCHEMA.NAME → NAME (링크/스키마 제거)."""
    core = qualified.split("@", 1)[0]
    return core.split(".")[-1]


def _extract_tables(text: str) -> list[str]:
    raw: list[str] = []
    for rx in (FROM_JOIN_RE, INSERT_INTO_RE, UPDATE_RE, DELETE_RE, MERGE_RE):
        for m in rx.finditer(text):
            raw.append(_norm_token(m.group(1)))
    return [t for t in raw if t]


def analyze(objects: list[SourceObject], grants: list[dict]) -> list[Reference]:
    idx = ObjectIndex(objects)
    refs: list[Reference] = []

    for o in objects:
        if not o.is_program:
            continue
        refs.extend(_object_refs(o, idx))

    refs.extend(_grant_refs(objects, grants))
    return _aggregate(refs)


def _object_refs(o: SourceObject, idx: ObjectIndex) -> list[Reference]:
    joined = "\n".join(t for _, t in o.lines)
    text = clean(joined)
    self_name = o.name.upper()
    self_pkg = o.name.upper() if o.otype in ("PACKAGE", "PACKAGE_BODY") else None
    out: list[Reference] = []

    # 1) DB Link 직접 참조
    for m in DBLINK_RE.finditer(text):
        out.append(Reference(o.key, f"{_base_name(m.group(1))}@{m.group(2)}", "DB_LINK",
                             detail=f"link={m.group(2)}"))

    # 2) 테이블/뷰 (DML)
    for tab in _extract_tables(text):
        if "@" in tab:
            continue  # DB_LINK 로 이미 집계
        base = _base_name(tab)
        if not base or base in NON_TABLE or base in SQL_NOISE:
            continue
        # 서브프로그램/패키지 이름이 FROM 위치에 올 일은 드묾 → 테이블로 취급
        hit = idx.tabviews.get(base)
        out.append(Reference(o.key, base, "TABLE", resolved=hit is not None,
                             target_key=hit.key if hit else None))

    # 3) 한정 참조 (PKG.member / DBMS_*/UTL_* / SEQ.NEXTVAL)
    for m in QUALIFIED_RE.finditer(text):
        a, b = m.group(1).upper(), m.group(2).upper()
        if a.startswith("DBMS_") or a.startswith("UTL_"):
            out.append(Reference(o.key, f"{a}.{b}", "BUILTIN", detail=a))
        elif b in ("NEXTVAL", "CURRVAL"):
            out.append(Reference(o.key, a, "SEQUENCE"))
        elif a in idx.packages and a != self_pkg:
            pkg = idx.packages[a]
            out.append(Reference(o.key, a, "PACKAGE", resolved=True, target_key=pkg.key,
                                 detail=f"member={b}"))

    # 4) 스탠드얼론 프로시저/함수 호출
    for name, target in idx.standalone.items():
        if name == self_name:
            continue
        if re.search(rf"\b{re.escape(name)}\s*[(;]", text):
            out.append(Reference(o.key, name, "CALL", resolved=True, target_key=target.key))

    return out


def _grant_refs(objects: list[SourceObject], grants: list[dict]) -> list[Reference]:
    by_display = {o.display: o for o in objects}
    by_name = {o.name.upper(): o for o in objects}
    out: list[Reference] = []
    for g in grants:
        obj = g["object"]
        target_obj = by_display.get(obj) or by_name.get(obj.split(".")[-1])
        if target_obj is None or not target_obj.is_program:
            continue
        out.append(Reference(target_obj.key, g["grantee"], "GRANT",
                             detail=f"{g['privs']} TO {g['grantee']}"))
    return out


def _aggregate(refs: list[Reference]) -> list[Reference]:
    """(src, target, rtype) 중복을 count 로 합산."""
    merged: dict[tuple[str, str, str], Reference] = {}
    for r in refs:
        k = (r.src_key, r.target, r.rtype)
        if k in merged:
            merged[k].count += 1
        else:
            merged[k] = Reference(r.src_key, r.target, r.rtype, resolved=r.resolved,
                                  target_key=r.target_key, detail=r.detail, count=1)
    return list(merged.values())
