"""Markdown 리포트 → 단일 HTML 문서 변환 (의존성 없음).

- 섹션(`## N.`)마다 `<details>` 로 감싸 **접기/펼치기(확장)** 가능
- 상단 고정 TOC 로 **섹션 이동(네비게이션)** + 모두 펼치기/접기 버튼
- 표/이미지(차트 PNG)/코드/blockquote/중첩 리스트 렌더, mermaid 는 mermaid.js(CDN)로 그림
리포트가 쓰는 Markdown 부분집합만 처리하도록 맞춘 경량 변환기다.
"""
from __future__ import annotations

import base64
import html
import re
from pathlib import Path

_MIME = {".png": "image/png", ".svg": "image/svg+xml", ".jpg": "image/jpeg",
         ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp"}

_INLINE_CODE = re.compile(r"`([^`]+)`")
_IMG = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_LIST_ITEM = re.compile(r"^(\s*)-\s+(.*)$")
_TABLE_SEP = re.compile(r"^\s*\|?[\s|:-]+\|?\s*$")


def _data_uri(path: Path) -> str | None:
    mime = _MIME.get(path.suffix.lower())
    if mime is None:
        return None
    try:
        b64 = base64.b64encode(path.read_bytes()).decode("ascii")
    except OSError:
        return None
    return f"data:{mime};base64,{b64}"


def _embed_images(md: str, base_dir: Path) -> str:
    """로컬 이미지 링크 `![alt](rel)` 를 data URI 로 치환해 단일 HTML 로 만든다."""
    def repl(m: re.Match) -> str:
        alt, src = m.group(1), m.group(2).strip()
        if src.startswith(("data:", "http://", "https://")):
            return m.group(0)
        p = (base_dir / src)
        if not p.exists():
            return m.group(0)
        uri = _data_uri(p)
        return f"![{alt}]({uri})" if uri else m.group(0)

    return _IMG.sub(repl, md)


def _inline(text: str) -> str:
    text = html.escape(text, quote=False)
    text = _INLINE_CODE.sub(r"<code>\1</code>", text)
    text = _IMG.sub(r'<img src="\2" alt="\1" loading="lazy">', text)
    text = _LINK.sub(r'<a href="\2">\1</a>', text)
    text = _BOLD.sub(r"<strong>\1</strong>", text)
    return text


def _cell_align(sep_cell: str) -> str:
    c = sep_cell.strip()
    if c.startswith(":") and c.endswith(":"):
        return "center"
    if c.endswith(":"):
        return "right"
    return "left"


def _split_row(row: str) -> list[str]:
    r = row.strip()
    if r.startswith("|"):
        r = r[1:]
    if r.endswith("|"):
        r = r[:-1]
    return [c.strip() for c in r.split("|")]


def _render_table(rows: list[str]) -> str:
    header = _split_row(rows[0])
    aligns = [_cell_align(c) for c in _split_row(rows[1])]

    def al(i: int) -> str:
        return aligns[i] if i < len(aligns) else "left"

    out = ['<div class="tbl"><table>', "<thead><tr>"]
    for i, c in enumerate(header):
        out.append(f'<th style="text-align:{al(i)}">{_inline(c)}</th>')
    out.append("</tr></thead><tbody>")
    for row in rows[2:]:
        cells = _split_row(row)
        out.append("<tr>")
        for i, c in enumerate(cells):
            out.append(f'<td style="text-align:{al(i)}">{_inline(c)}</td>')
        out.append("</tr>")
    out.append("</tbody></table></div>")
    return "".join(out)


def _render_list(items: list[tuple[int, str]]) -> str:
    out: list[str] = []
    levels: list[int] = []
    for indent, text in items:
        lvl = indent // 2
        while levels and levels[-1] > lvl:
            out.append("</li></ul>")
            levels.pop()
        if not levels or levels[-1] < lvl:
            out.append("<ul>")
            levels.append(lvl)
            out.append(f"<li>{_inline(text)}")
        else:
            out.append("</li>")
            out.append(f"<li>{_inline(text)}")
    while levels:
        out.append("</li></ul>")
        levels.pop()
    return "".join(out)


def _render_blocks(lines: list[str]) -> str:
    out: list[str] = []
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        stripped = line.lstrip()

        if stripped.startswith("```"):
            lang = stripped[3:].strip()
            j, buf = i + 1, []
            while j < n and not lines[j].lstrip().startswith("```"):
                buf.append(lines[j])
                j += 1
            content = html.escape("\n".join(buf), quote=False)
            if lang == "mermaid":
                out.append(f'<pre class="mermaid">\n{content}\n</pre>')
            else:
                out.append(f'<pre class="code"><code>{content}</code></pre>')
            i = j + 1
            continue

        if line.startswith("#### "):
            out.append(f"<h4>{_inline(line[5:])}</h4>")
            i += 1
            continue
        if line.startswith("### "):
            out.append(f"<h3>{_inline(line[4:])}</h3>")
            i += 1
            continue

        if stripped.startswith("|") and i + 1 < n and _TABLE_SEP.match(lines[i + 1]):
            rows = [line]
            j = i + 1
            while j < n and lines[j].lstrip().startswith("|"):
                rows.append(lines[j])
                j += 1
            out.append(_render_table(rows))
            i = j
            continue

        if stripped.startswith(">"):
            buf = []
            while i < n and lines[i].lstrip().startswith(">"):
                buf.append(lines[i].lstrip()[1:].strip())
                i += 1
            out.append(f"<blockquote>{_inline(' '.join(buf))}</blockquote>")
            continue

        if _LIST_ITEM.match(line):
            items = []
            while i < n and _LIST_ITEM.match(lines[i]):
                m = _LIST_ITEM.match(lines[i])
                items.append((len(m.group(1)), m.group(2)))
                i += 1
            out.append(_render_list(items))
            continue

        # 문단: 다음 빈 줄/블록 시작 전까지
        buf = [line]
        i += 1
        while i < n and lines[i].strip() and not lines[i].lstrip().startswith(("#", ">", "|", "```", "- ")):
            buf.append(lines[i])
            i += 1
        out.append(f"<p>{_inline(' '.join(s.strip() for s in buf))}</p>")
    return "\n".join(out)


def _parse_sections(md: str) -> tuple[str, list[str], list[tuple[str, list[str]]]]:
    """(title, header_lines, [(section_title, section_lines)])."""
    lines = md.split("\n")
    title = "리포트"
    header: list[str] = []
    sections: list[tuple[str, list[str]]] = []
    cur_title: str | None = None
    cur: list[str] = []
    for line in lines:
        if line.startswith("# ") and cur_title is None and not sections:
            title = line[2:].strip()
            header.append(line)
            continue
        if line.startswith("## "):
            if cur_title is not None:
                sections.append((cur_title, cur))
            cur_title = line[3:].strip()
            cur = []
        elif cur_title is None:
            header.append(line)
        else:
            cur.append(line)
    if cur_title is not None:
        sections.append((cur_title, cur))
    return title, header, sections


_CSS = """
:root{--bg:#ffffff;--fg:#1c2530;--muted:#5b6b7b;--line:#e2e8f0;--accent:#2f6feb;--soft:#f5f8fc;--codebg:#f2f4f7}
@media(prefers-color-scheme:dark){:root{--bg:#0f141a;--fg:#e5edf5;--muted:#9fb0c0;--line:#26313d;--accent:#6ea8fe;--soft:#161f29;--codebg:#1a2029}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font-family:system-ui,-apple-system,"Segoe UI",Roboto,"Malgun Gothic",sans-serif;line-height:1.6}
.wrap{max-width:1080px;margin:0 auto;padding:0 20px 80px}
header.rep{padding:28px 0 8px}
header.rep h1{font-size:1.7rem;margin:.2em 0}
header.rep ul{color:var(--muted);list-style:none;padding:0;margin:.4em 0}
nav.toc{position:sticky;top:0;z-index:10;background:var(--bg);border-bottom:1px solid var(--line);padding:10px 0;margin-bottom:14px;backdrop-filter:blur(6px)}
nav.toc .row{display:flex;flex-wrap:wrap;gap:6px 10px;align-items:center}
nav.toc a{font-size:.86rem;color:var(--accent);text-decoration:none;padding:3px 9px;border:1px solid var(--line);border-radius:999px;white-space:nowrap}
nav.toc a:hover{background:var(--soft)}
nav.toc .btn{cursor:pointer;font-size:.82rem;color:var(--fg);background:var(--soft);border:1px solid var(--line);border-radius:6px;padding:3px 10px}
details.sec{border:1px solid var(--line);border-radius:10px;margin:12px 0;overflow:hidden;background:var(--bg)}
details.sec>summary{cursor:pointer;list-style:none;padding:12px 16px;font-size:1.15rem;font-weight:700;background:var(--soft);display:flex;align-items:center;gap:8px}
details.sec>summary::-webkit-details-marker{display:none}
details.sec>summary::before{content:"▸";color:var(--accent);font-size:.9em}
details.sec[open]>summary::before{content:"▾"}
.sec-body{padding:4px 16px 16px}
h3{margin-top:1.2em;font-size:1.05rem}
h4{margin:.9em 0 .3em;font-size:.98rem}
.tbl{overflow-x:auto;margin:12px 0}
table{border-collapse:collapse;width:100%;font-size:.9rem}
th,td{border:1px solid var(--line);padding:6px 10px}
th{background:var(--soft)}
tbody tr:nth-child(even){background:var(--soft)}
code{background:var(--codebg);padding:1px 5px;border-radius:4px;font-size:.88em}
pre.code{background:var(--codebg);padding:12px 14px;border-radius:8px;overflow-x:auto}
pre.code code{background:none;padding:0}
pre.mermaid{background:var(--bg);text-align:center}
blockquote{margin:10px 0;padding:8px 14px;border-left:3px solid var(--accent);background:var(--soft);color:var(--muted);border-radius:0 6px 6px 0;font-size:.92rem}
img{max-width:100%;height:auto;border:1px solid var(--line);border-radius:8px;margin:8px 0}
a{color:var(--accent)}
"""

_JS = """
function setAll(open){document.querySelectorAll('details.sec').forEach(function(d){d.open=open});}
document.querySelectorAll('nav.toc a[href^="#"]').forEach(function(a){
  a.addEventListener('click',function(e){
    var t=document.querySelector(a.getAttribute('href'));
    if(t){t.open=true;}
  });
});
window.addEventListener('hashchange',function(){
  var t=document.querySelector(location.hash);if(t&&t.tagName==='DETAILS'){t.open=true;t.scrollIntoView();}
});
"""

_MERMAID = ('<script type="module">'
            "import mermaid from 'https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.esm.min.mjs';"
            "mermaid.initialize({startOnLoad:true});</script>")


def to_html(md: str, *, base_dir: Path | str | None = None, embed_images: bool = True,
            has_mermaid: bool | None = None) -> str:
    if embed_images and base_dir is not None:
        md = _embed_images(md, Path(base_dir))
    title, header_lines, sections = _parse_sections(md)
    if has_mermaid is None:
        has_mermaid = "```mermaid" in md

    esc_title = html.escape(title, quote=False)
    parts: list[str] = []
    parts.append("<!DOCTYPE html><html lang='ko'><head><meta charset='utf-8'>")
    parts.append("<meta name='viewport' content='width=device-width,initial-scale=1'>")
    parts.append(f"<title>{esc_title}</title><style>{_CSS}</style></head><body><div class='wrap'>")

    # 헤더 (# 제목 + 메타)
    parts.append("<header class='rep'>")
    parts.append(f"<h1>{esc_title}</h1>")
    body_header = [ln for ln in header_lines if not ln.startswith("# ")]
    parts.append(_render_blocks(body_header))
    parts.append("</header>")

    # TOC
    parts.append("<nav class='toc'><div class='row'>")
    parts.append("<span class='btn' onclick='setAll(true)'>모두 펼치기</span>")
    parts.append("<span class='btn' onclick='setAll(false)'>모두 접기</span>")
    for idx, (stitle, _) in enumerate(sections):
        parts.append(f"<a href='#sec-{idx}'>{_inline(stitle)}</a>")
    parts.append("</div></nav>")

    # 섹션 (details)
    for idx, (stitle, slines) in enumerate(sections):
        parts.append(f"<details class='sec' id='sec-{idx}' open>")
        parts.append(f"<summary>{_inline(stitle)}</summary>")
        parts.append(f"<div class='sec-body'>{_render_blocks(slines)}</div>")
        parts.append("</details>")

    parts.append("</div>")
    parts.append(f"<script>{_JS}</script>")
    if has_mermaid:
        parts.append(_MERMAID)
    parts.append("</body></html>")
    return "".join(parts)
