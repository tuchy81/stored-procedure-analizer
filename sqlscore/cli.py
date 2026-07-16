"""sqlscore CLI — .sql 폴더 분석 → 난이도 리포트(md) 생성."""
from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from sqlscore import parser as parser_mod
from sqlscore import dependencies, effort as effort_mod, htmlreport, report, scoring, viz
from sqlscore.config import load_config
from sqlscore.metrics import compute_metrics
from sqlscore.model import EffortEstimate, Score

app = typer.Typer(add_completion=False, help="DB 프로그램(.sql) 난이도(복잡도·의존성) 평가 도구")
console = Console()


@app.command()
def analyze(
    src: Path = typer.Option(..., "--src", "-s", help="DB 프로그램 .sql 파일이 있는 폴더"),
    out: Path = typer.Option(Path("sql_difficulty_report.md"), "--out", "-o", help="리포트(md) 출력 경로"),
    config_path: Optional[Path] = typer.Option(None, "--config", "-c", help="가중치 YAML (선택)"),
    recursive: bool = typer.Option(True, "--recursive/--no-recursive", help="하위 폴더 재귀 스캔"),
    csv_dir: Optional[Path] = typer.Option(None, "--csv-dir", help="objects/references/scores CSV 병행 출력 폴더"),
    effort_sample: Optional[Path] = typer.Option(
        None, "--effort-sample", help="실측 공수 표본 CSV(KEY,HOURS) — 주면 전환 공수(P50/P90) 추정"),
    charts: bool = typer.Option(True, "--charts/--no-charts", help="점수 산포·공수 캘리브레이션 차트 생성(matplotlib 필요)"),
    html: bool = typer.Option(True, "--html/--no-html", help="MD 와 함께 섹션 이동/확장 가능한 HTML 리포트도 생성"),
    embed_images: bool = typer.Option(
        True, "--embed-images/--no-embed-images", help="차트 이미지를 HTML 내부에 인라인 임베드(단일 파일)"),
) -> None:
    """폴더의 .sql 을 스캔해 오브젝트·의존성을 분석하고 난이도 리포트를 생성한다."""
    cfg = load_config(config_path)

    objects, grants, scanned = parser_mod.scan_folder(src, recursive=recursive)
    if not objects:
        console.print(f"[yellow]식별된 DB 오브젝트가 없습니다 (스캔 파일 {len(scanned)}개): {src}[/yellow]")
        raise typer.Exit(code=1)

    metrics_map = {o.key: compute_metrics(o) for o in objects if o.is_program}
    refs = dependencies.analyze(objects, grants)
    scores = scoring.score_all(objects, metrics_map, refs, cfg)

    estimates: dict[str, EffortEstimate] = {}
    effort_summary = None
    if effort_sample is not None:
        samples = effort_mod.load_samples(effort_sample)
        estimates, effort_summary = effort_mod.estimate(objects, scores, metrics_map, samples, cfg.effort)

    out.parent.mkdir(parents=True, exist_ok=True)
    chart_paths = None
    if charts:
        chart_paths = viz.render_charts(out.parent, objects, scores, metrics_map,
                                        estimates or None, effort_summary, cfg, refs=refs)
        if chart_paths is None:
            console.print("[yellow]차트 생략: matplotlib 미설치 — pip install -e \".[viz]\" 로 활성화[/yellow]")

    md = report.build_report(
        objects, metrics_map, refs, scores, cfg,
        src=str(src), scanned_files=scanned,
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
        effort=estimates or None, effort_summary=effort_summary,
        charts=chart_paths,
    )
    out.write_text(md, encoding="utf-8")

    html_out = None
    if html:
        html_out = out.with_suffix(".html")
        html_out.write_text(
            htmlreport.to_html(md, base_dir=out.parent, embed_images=embed_images),
            encoding="utf-8")

    if csv_dir is not None:
        _write_csvs(csv_dir, objects, metrics_map, refs, scores, estimates)

    _print_summary(objects, scores, scanned, out)
    if html_out is not None:
        console.print(f"[green]✓ HTML 리포트: {html_out}[/green]")
    if effort_summary is not None:
        u = effort_summary["unit"]
        console.print(f"[bold]전환 공수 추정: P50 {effort_summary['total_p50']}{u} · "
                      f"P90 {effort_summary['total_p90']}{u}[/bold] "
                      f"(표본 {effort_summary['n_matched']}, 신뢰도 {effort_summary['confidence']})")


def _print_summary(objects, scores, scanned, out: Path) -> None:
    programs = [o for o in objects if o.is_program]
    ranked = sorted(programs, key=lambda o: scores[o.key].final_score, reverse=True)
    table = Table(title=f"최종점수 상위 (전체 {len(programs)}개 프로그램 / 파일 {len(scanned)}개)")
    table.add_column("오브젝트")
    table.add_column("타입")
    table.add_column("최종점수", justify="right")
    table.add_column("전환난이도", justify="right")
    table.add_column("밴드")
    table.add_column("영향도", justify="right")
    for o in ranked[:10]:
        s = scores[o.key]
        table.add_row(o.display, o.otype, f"{s.final_score:.1f}",
                      f"{s.absolute_score:.1f}", s.band, f"{s.impact_score:.1f}")
    console.print(table)
    console.print(f"[green]✓ 리포트 생성: {out}[/green]")


def _write_csvs(csv_dir: Path, objects, metrics_map, refs, scores, estimates=None) -> None:
    csv_dir.mkdir(parents=True, exist_ok=True)
    estimates = estimates or {}

    with (csv_dir / "objects.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["KEY", "OWNER", "NAME", "TYPE", "FILE", "START_LINE", "END_LINE",
                    "LOC", "CYCLOMATIC", "VOLUME", "COMPLEXITY", "DEPENDENCY", "ABSOLUTE", "BAND",
                    "IMPACT", "FAN_IN", "FINAL_SCORE", "EFFORT_P50", "EFFORT_P90", "EFFORT_BASIS"])
        for o in objects:
            if not o.is_program:
                continue
            m = metrics_map.get(o.key)
            s = scores.get(o.key, Score(o.key))
            fan_in = len(s.breakdown.get("impact", {}).get("callers", []))
            e = estimates.get(o.key)
            ep50 = "" if e is None or e.p50 is None else e.p50
            ep90 = "" if e is None or e.p90 is None else e.p90
            ebasis = "" if e is None else e.basis
            w.writerow([o.key, o.owner or "", o.name, o.otype, o.file, o.start_line, o.end_line,
                        m.loc, m.cyclomatic, s.volume_score, s.complexity_score,
                        s.dependency_score, s.absolute_score, s.band,
                        s.impact_score, fan_in, s.final_score, ep50, ep90, ebasis])

    with (csv_dir / "references.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["SRC_KEY", "REF_TYPE", "TARGET", "RESOLVED", "TARGET_KEY", "COUNT", "DETAIL"])
        for r in refs:
            w.writerow([r.src_key, r.rtype, r.target, "Y" if r.resolved else "N",
                        r.target_key or "", r.count, r.detail])

    console.print(f"[cyan]CSV 병행 출력: {csv_dir}[/cyan]")


if __name__ == "__main__":
    app()
