"""diff 커맨드 — 스냅샷 간 비교 (§9)."""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from sp_assessor.core.paths import ProjectPaths
from sp_assessor.io.csv_io import read_csv

STAGE_FILES: dict[str, tuple[str, str, str]] = {
    "s1": ("s1_inventory", "s1_inventory.csv", "SP_ID"),
    "s2": ("s2_metrics", "s2_metrics.csv", "SP_ID"),
    "s3": ("s3_graph", "s3_nodes.csv", "NODE_ID"),
    "s4": ("s4_scoring", "s4_scores.csv", "SP_ID"),
}

HIGHLIGHT_COLUMNS = {"s4": ["STRATEGY", "QUADRANT", "EFFORT_EST_MD"]}


@dataclass
class ChangedRow:
    key: str
    column: str
    old: object
    new: object


@dataclass
class DiffResult:
    stage: str
    from_tag: str
    to_tag: str
    new_ids: list[str] = field(default_factory=list)
    removed_ids: list[str] = field(default_factory=list)
    changed: list[ChangedRow] = field(default_factory=list)


def _load_stage_df(paths: ProjectPaths, tag: str, stage: str) -> pd.DataFrame:
    stage_dir, filename, key_col = STAGE_FILES[stage]
    snap_path = paths.snapshot_dir(tag) / "output" / stage_dir / filename
    if not snap_path.exists():
        raise FileNotFoundError(f"snapshot not found: {snap_path} (태그 '{tag}' 로 run --tag 를 먼저 실행했는지 확인)")
    df = read_csv(snap_path)
    if stage == "s3" and not df.empty:
        df = df[df["NODE_TYPE"].isin(["SP", "TRIGGER"])].reset_index(drop=True)
    return df


def compute_diff(paths: ProjectPaths, stage: str, from_tag: str, to_tag: str) -> DiffResult:
    if stage not in STAGE_FILES:
        raise ValueError(f"diff 는 s1~s4 만 지원 (요청: {stage}). s5 는 리포트라 SP 단위 비교 대상이 아님")

    _, _, key_col = STAGE_FILES[stage]
    from_df = _load_stage_df(paths, from_tag, stage)
    to_df = _load_stage_df(paths, to_tag, stage)

    from_keys = set(from_df[key_col]) if not from_df.empty else set()
    to_keys = set(to_df[key_col]) if not to_df.empty else set()

    result = DiffResult(stage=stage, from_tag=from_tag, to_tag=to_tag,
                        new_ids=sorted(to_keys - from_keys), removed_ids=sorted(from_keys - to_keys))

    common = sorted(from_keys & to_keys)
    if common:
        from_idx = from_df.set_index(key_col)
        to_idx = to_df.set_index(key_col)
        columns = HIGHLIGHT_COLUMNS.get(stage) or [c for c in from_df.columns if c != key_col]
        for key in common:
            for col in columns:
                if col not in from_idx.columns or col not in to_idx.columns:
                    continue
                old_val, new_val = from_idx.loc[key, col], to_idx.loc[key, col]
                if pd.isna(old_val) and pd.isna(new_val):
                    continue
                if old_val != new_val:
                    result.changed.append(ChangedRow(key=key, column=col, old=old_val, new=new_val))
    return result
