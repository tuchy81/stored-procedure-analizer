"""S1 스모크 테스트 — examples 데이터로 end-to-end 실행."""
from __future__ import annotations

from pathlib import Path
import logging
import shutil

import pandas as pd

from sp_assessor.core.config import load_config
from sp_assessor.core.paths import ProjectPaths
from sp_assessor.stages import s1_inventory
from sp_assessor.io.csv_io import read_csv


EXAMPLES = Path(__file__).parent.parent / "examples"


def test_s1_end_to_end(tmp_path: Path) -> None:
    # arrange: copy examples/ into tmp
    for sub in ("input", "override"):
        src = EXAMPLES / sub
        if src.exists():
            shutil.copytree(src, tmp_path / sub)
    shutil.copy(EXAMPLES / "config.yaml", tmp_path / "config.yaml")

    paths = ProjectPaths.from_root(tmp_path)
    paths.ensure()
    cfg = load_config(paths.config)
    logger = logging.getLogger("test")

    # act
    result = s1_inventory.run(paths, cfg, logger)

    # assert
    assert result["inventory"] > 0
    inv = read_csv(paths.stage_output("s1_inventory") / "s1_inventory.csv")
    sp_ids = set(inv["SP_ID"])
    assert "APP_OWNER.GET_ORDER" in sp_ids
    assert "APP_OWNER.CALC_TAX" in sp_ids
    assert any(s.startswith("APP_OWNER.CUSTOMER_PKG.ADD_CUSTOMER") for s in sp_ids)
    assert any(s.startswith("APP_OWNER.CUSTOMER_PKG.GET_NAME") for s in sp_ids)

    excluded = read_csv(paths.stage_output("s1_inventory") / "s1_excluded.csv")
    ex_names = set(excluded["OBJECT_NAME"]) if "OBJECT_NAME" in excluded.columns else set()
    assert "TMP_TEST_PROC" in ex_names
    assert "LEGACY_BAK" in ex_names

    inv_row = inv[inv["SP_ID"] == "APP_OWNER.GET_ORDER"].iloc[0]
    assert int(inv_row["LOC"]) > 0

    daily = inv[inv["SP_ID"] == "BATCH_OWNER.DAILY_JOB"]
    if not daily.empty:
        assert int(daily.iloc[0]["REMOTE_REF_COUNT"]) == 1

    unresolved = read_csv(paths.stage_output("s1_inventory") / "s1_unresolved.csv")
    codes = set(unresolved["REASON_CODE"]) if "REASON_CODE" in unresolved.columns else set()
    assert "UNRESOLVED_REMOTE" in codes


def test_grant_matrix(tmp_path: Path) -> None:
    for sub in ("input", "override"):
        src = EXAMPLES / sub
        if src.exists():
            shutil.copytree(src, tmp_path / sub)
    shutil.copy(EXAMPLES / "config.yaml", tmp_path / "config.yaml")

    paths = ProjectPaths.from_root(tmp_path)
    paths.ensure()
    cfg = load_config(paths.config)
    logger = logging.getLogger("test")
    s1_inventory.run(paths, cfg, logger)

    gm = read_csv(paths.stage_output("s1_inventory") / "s1_grant_matrix.csv")
    assert not gm.empty
    assert "EXECUTE" in set(gm["PRIVILEGE"])
