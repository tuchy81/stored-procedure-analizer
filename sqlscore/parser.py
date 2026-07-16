"""파서 — .sql 파일 스캔 → DB 오브젝트 식별 및 본문 경계 산정.

전략:
  1) 파일 내용을 SQL*Plus 종결자('/' 단독 라인) 기준으로 statement 블록으로 분할.
  2) 각 블록에서 CREATE 오브젝트 헤더를 찾고, 헤더~다음 헤더(또는 블록 끝)를
     하나의 오브젝트 본문으로 슬라이스 ('/' 미사용 스크립트도 헤더 기준으로 분해).
헤더 매칭은 주석/문자열을 제거한 사본에서 수행하되, LOC/라인번호는 원본 기준으로 센다.
"""
from __future__ import annotations

import re
from pathlib import Path

from sqlscore.model import SourceObject
from sqlscore.textutil import strip_comments

# CREATE [OR REPLACE] [ [NON]EDITIONABLE ] <type> [schema.]name ...
OBJECT_HEADER_RE = re.compile(
    r"""^\s*CREATE\s+(?:OR\s+REPLACE\s+)?
        (?:(?:NON)?EDITIONABLE\s+)?
        (?:PUBLIC\s+)?
        (?P<type>PACKAGE\s+BODY|TYPE\s+BODY|MATERIALIZED\s+VIEW|
                 PACKAGE|PROCEDURE|FUNCTION|TRIGGER|TYPE|VIEW|
                 TABLE|SEQUENCE|INDEX|SYNONYM)
        \s+
        (?P<name>"[^"]+"|[A-Za-z_][A-Za-z0-9_$#]*)
        (?:\s*\.\s*(?P<name2>"[^"]+"|[A-Za-z_][A-Za-z0-9_$#]*))?
    """,
    re.IGNORECASE | re.VERBOSE,
)

# GRANT <privs> ON [schema.]<obj> TO <grantee>
GRANT_RE = re.compile(
    r"\bGRANT\s+(?P<privs>[A-Za-z_,\s]+?)\s+ON\s+"
    r"(?P<obj>(?:\"[^\"]+\"|[A-Za-z_][A-Za-z0-9_$#]*)"
    r"(?:\s*\.\s*(?:\"[^\"]+\"|[A-Za-z_][A-Za-z0-9_$#]*))?)"
    r"\s+TO\s+(?P<grantee>[A-Za-z_][A-Za-z0-9_$#]*|PUBLIC)",
    re.IGNORECASE,
)

_TYPE_NORMALIZE = {
    "PACKAGE BODY": "PACKAGE_BODY",
    "TYPE BODY": "TYPE_BODY",
    "MATERIALIZED VIEW": "MATERIALIZED_VIEW",
}


def _norm_type(raw: str) -> str:
    key = re.sub(r"\s+", " ", raw.strip().upper())
    return _TYPE_NORMALIZE.get(key, key)


def _unquote(ident: str | None) -> str | None:
    if ident is None:
        return None
    ident = ident.strip()
    if ident.startswith('"') and ident.endswith('"'):
        return ident[1:-1]
    return ident.upper()


def _split_blocks(lines: list[str]) -> list[tuple[int, list[str]]]:
    """'/' 단독 라인 종결자 기준 블록 분할. 반환: [(시작 라인번호(1-base), 라인들)]."""
    blocks: list[tuple[int, list[str]]] = []
    cur: list[str] = []
    cur_start = 1
    for i, line in enumerate(lines, start=1):
        if line.strip() == "/":
            if cur:
                blocks.append((cur_start, cur))
            cur = []
            cur_start = i + 1
        else:
            if not cur:
                cur_start = i
            cur.append(line)
    if cur:
        blocks.append((cur_start, cur))
    return blocks


def _find_header(line: str):
    """주석 제거 후 오브젝트 헤더 매칭 시도."""
    stripped = strip_comments(line)
    return OBJECT_HEADER_RE.match(stripped)


def parse_file(path: Path, root: Path) -> tuple[list[SourceObject], list[dict]]:
    """단일 .sql 파일 → (오브젝트 목록, GRANT 목록)."""
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.split("\n")
    rel = str(path.relative_to(root))

    objects: list[SourceObject] = []
    for block_start, block_lines in _split_blocks(lines):
        # 블록 내 헤더 위치(블록 상대 인덱스) 수집
        header_idxs: list[tuple[int, re.Match]] = []
        for idx, bl in enumerate(block_lines):
            m = _find_header(bl)
            if m:
                header_idxs.append((idx, m))
        if not header_idxs:
            continue
        for h, (idx, m) in enumerate(header_idxs):
            end_idx = header_idxs[h + 1][0] - 1 if h + 1 < len(header_idxs) else len(block_lines) - 1
            body_lines = block_lines[idx:end_idx + 1]
            abs_start = block_start + idx
            numbered = [(abs_start + k, t) for k, t in enumerate(body_lines)]

            name1 = _unquote(m.group("name"))
            name2 = _unquote(m.group("name2"))
            if name2:
                owner, name = name1, name2
            else:
                owner, name = None, name1
            objects.append(SourceObject(
                owner=owner, name=name, otype=_norm_type(m.group("type")),
                file=rel, start_line=abs_start, end_line=abs_start + len(body_lines) - 1,
                lines=numbered,
            ))

    grants = _scan_grants(strip_comments(text), rel)
    return objects, grants


def _scan_grants(text_nocomment: str, rel: str) -> list[dict]:
    grants: list[dict] = []
    for m in GRANT_RE.finditer(text_nocomment):
        obj = re.sub(r"\s+", "", m.group("obj")).upper().replace('"', "")
        grants.append({
            "object": obj,
            "privs": re.sub(r"\s+", " ", m.group("privs").strip().upper()),
            "grantee": m.group("grantee").upper(),
            "file": rel,
        })
    return grants


def scan_folder(src: Path, recursive: bool = True) -> tuple[list[SourceObject], list[dict], list[str]]:
    """폴더 내 .sql 스캔 → (오브젝트, GRANT, 처리한 파일 목록)."""
    src = src.resolve()
    if not src.exists():
        raise FileNotFoundError(f"source folder not found: {src}")
    pattern = "**/*.sql" if recursive else "*.sql"
    files = sorted(p for p in src.glob(pattern) if p.is_file())

    all_objs: list[SourceObject] = []
    all_grants: list[dict] = []
    scanned: list[str] = []
    for f in files:
        objs, grants = parse_file(f, src)
        all_objs.extend(objs)
        all_grants.extend(grants)
        scanned.append(str(f.relative_to(src)))
    return all_objs, all_grants, scanned
