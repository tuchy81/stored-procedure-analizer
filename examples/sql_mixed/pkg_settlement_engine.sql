CREATE OR REPLACE PACKAGE FIN.PKG_SETTLEMENT_ENGINE AS
  -- 그룹사 통합 정산 엔진 (다국가/다통화/다시스템)
  PROCEDURE run_global_settlement(p_period IN VARCHAR2, p_region IN VARCHAR2 DEFAULT 'ALL');
  PROCEDURE consolidate_hierarchy(p_root_org IN NUMBER);
  PROCEDURE distribute_charges(p_batch_id IN NUMBER);
  FUNCTION resolve_fx(p_ccy IN VARCHAR2, p_dt IN DATE) RETURN NUMBER;
  PROCEDURE reconcile_all(p_batch_id IN NUMBER);
  PROCEDURE archive_and_purge(p_period IN VARCHAR2);
END PKG_SETTLEMENT_ENGINE;
/
CREATE OR REPLACE PACKAGE BODY FIN.PKG_SETTLEMENT_ENGINE AS

  g_batch_id   NUMBER;
  g_region     VARCHAR2(10);
  g_err_cnt    NUMBER := 0;

  -- ============================================================
  -- 환율 해석 (여러 원천 폴백: 로컬 → 그룹 허브 → 외부 벤더)
  -- ============================================================
  FUNCTION resolve_fx(p_ccy IN VARCHAR2, p_dt IN DATE) RETURN NUMBER IS
    v_rate NUMBER;
  BEGIN
    SELECT rate INTO v_rate
      FROM FX_RATE
     WHERE ccy = p_ccy AND rate_dt = p_dt;
    RETURN v_rate;
  EXCEPTION
    WHEN NO_DATA_FOUND THEN
      BEGIN
        -- 그룹 FX 허브 (DB Link)
        SELECT rate INTO v_rate
          FROM FX_RATE@GROUP_HUB
         WHERE ccy = p_ccy AND rate_dt = p_dt;
        RETURN v_rate;
      EXCEPTION
        WHEN NO_DATA_FOUND THEN
          -- 외부 벤더 (또 다른 DB Link)
          SELECT rate INTO v_rate
            FROM VENDOR_FX@BLOOMBERG_LINK
           WHERE symbol = p_ccy || 'USD'
             AND quote_dt = p_dt;
          RETURN v_rate;
      END;
  END resolve_fx;

  -- ============================================================
  -- 조직 계층 통합 (CONNECT BY 계층 + 재귀적 롤업)
  -- ============================================================
  PROCEDURE consolidate_hierarchy(p_root_org IN NUMBER) IS
    CURSOR c_org IS
      SELECT org_id, parent_id, LEVEL AS lvl
        FROM ORG_UNIT
       START WITH org_id = p_root_org
     CONNECT BY PRIOR org_id = parent_id
       ORDER BY LEVEL DESC;
    v_sum   NUMBER;
    v_dyn   VARCHAR2(4000);
  BEGIN
    FOR o IN c_org LOOP
      -- 하위 조직 합산 (3단 중첩 서브쿼리)
      SELECT NVL(SUM(amount), 0) INTO v_sum
        FROM SETTLE_LINE
       WHERE org_id IN (SELECT org_id FROM ORG_UNIT
                         START WITH parent_id = o.org_id
                       CONNECT BY PRIOR org_id = parent_id)
         AND batch_id = g_batch_id
         AND line_type IN (SELECT code FROM CODE_MST
                            WHERE grp = 'LINE_TYPE'
                              AND code NOT IN (SELECT excl FROM CONSOL_EXCLUDE
                                                WHERE region = g_region));

      -- 레벨별 롤업 테이블로 동적 적재
      v_dyn := 'MERGE INTO ROLLUP_L' || o.lvl ||
               ' t USING (SELECT :1 org, :2 amt FROM DUAL) s' ||
               ' ON (t.org_id = s.org) ' ||
               ' WHEN MATCHED THEN UPDATE SET t.amount = s.amt' ||
               ' WHEN NOT MATCHED THEN INSERT (org_id, amount) VALUES (s.org, s.amt)';
      EXECUTE IMMEDIATE v_dyn USING o.org_id, v_sum;
    END LOOP;
  EXCEPTION
    WHEN OTHERS THEN
      g_err_cnt := g_err_cnt + 1;
      RAISE;
  END consolidate_hierarchy;

  -- ============================================================
  -- 과금 배분 (BULK COLLECT + FORALL + 동적 파티션)
  -- ============================================================
  PROCEDURE distribute_charges(p_batch_id IN NUMBER) IS
    TYPE t_line IS TABLE OF SETTLE_LINE%ROWTYPE INDEX BY PLS_INTEGER;
    v_lines  t_line;
    v_alloc  NUMBER;
    v_ccy    VARCHAR2(3);
    v_fx     NUMBER;
    v_sql    VARCHAR2(4000);
    v_target VARCHAR2(30);
  BEGIN
    SELECT * BULK COLLECT INTO v_lines
      FROM SETTLE_LINE
     WHERE batch_id = p_batch_id
       AND status = 'PENDING';

    FOR i IN 1 .. v_lines.COUNT LOOP
      v_ccy := v_lines(i).ccy;
      v_fx  := resolve_fx(v_ccy, v_lines(i).line_dt);

      -- 배분 규칙 (다중 분기 + 중첩 서브쿼리 4단)
      IF v_lines(i).amount > 1000000 THEN
        SELECT ratio INTO v_alloc FROM ALLOC_RULE
         WHERE rule_id = (SELECT MAX(rule_id) FROM ALLOC_RULE
                           WHERE tier = (SELECT tier FROM TIER_MAP
                                          WHERE amt_from <= v_lines(i).amount
                                            AND amt_to >= (SELECT MIN(amount) FROM SETTLE_LINE
                                                            WHERE batch_id = p_batch_id)));
      ELSIF v_lines(i).amount > 100000 THEN
        v_alloc := 0.5;
      ELSE
        v_alloc := 1.0;
      END IF;

      v_target := 'CHARGE_' || TO_CHAR(v_lines(i).line_dt, 'YYYYMM');
      v_sql := 'INSERT INTO ' || v_target ||
               ' (line_id, org_id, amount_usd, alloc) VALUES (:1, :2, :3, :4)';
      EXECUTE IMMEDIATE v_sql
        USING v_lines(i).line_id, v_lines(i).org_id, v_lines(i).amount * v_fx, v_alloc;
    END LOOP;

    -- 원장 반영 (원격 ERP)
    FORALL i IN 1 .. v_lines.COUNT
      INSERT INTO GL_STAGE@ERP_LINK (line_id, amount_usd)
      VALUES (v_lines(i).line_id, v_lines(i).amount * resolve_fx(v_lines(i).ccy, v_lines(i).line_dt));

    UPDATE SETTLE_LINE SET status = 'ALLOCATED' WHERE batch_id = p_batch_id;
    DBMS_OUTPUT.PUT_LINE('distributed: ' || v_lines.COUNT);
  EXCEPTION
    WHEN OTHERS THEN
      g_err_cnt := g_err_cnt + 1;
      ROLLBACK;
      RAISE;
  END distribute_charges;

  -- ============================================================
  -- 대사 (외부 시스템 3종 교차 검증 + 동적 SQL + GOTO)
  -- ============================================================
  PROCEDURE reconcile_all(p_batch_id IN NUMBER) IS
    CURSOR c_line IS
      SELECT line_id, org_id, amount, ccy FROM SETTLE_LINE WHERE batch_id = p_batch_id;
    v_erp   NUMBER;
    v_wms   NUMBER;
    v_diff  NUMBER;
    v_sql   VARCHAR2(2000);
    v_skip  BOOLEAN;
  BEGIN
    FOR r IN c_line LOOP
      v_skip := FALSE;
      BEGIN
        SELECT amount INTO v_erp FROM GL_POSTED@ERP_LINK WHERE line_id = r.line_id;
      EXCEPTION
        WHEN NO_DATA_FOUND THEN
          v_skip := TRUE;
      END;

      IF v_skip THEN
        GOTO next_line;
      END IF;

      SELECT NVL(qty * unit_price, 0) INTO v_wms
        FROM SHIP_FACT@WMS_LINK
       WHERE ref_line = r.line_id;

      v_diff := ABS(NVL(v_erp, 0) - NVL(v_wms, 0));

      IF v_diff > 100 THEN
        v_sql := 'INSERT INTO RECON_EXC_' || g_region ||
                 ' (line_id, diff, checked_at) VALUES (:1, :2, SYSDATE)';
        EXECUTE IMMEDIATE v_sql USING r.line_id, v_diff;
        PRC_RECONCILE(r.line_id);
      END IF;

      <<next_line>>
      NULL;
    END LOOP;

    DBMS_OUTPUT.PUT_LINE('reconcile errors: ' || g_err_cnt);
  END reconcile_all;

  -- ============================================================
  -- 아카이브 + 퍼지 (자율 트랜잭션 + 동적 파티션 DROP)
  -- ============================================================
  PROCEDURE archive_and_purge(p_period IN VARCHAR2) IS
    PRAGMA AUTONOMOUS_TRANSACTION;
    v_sql   VARCHAR2(2000);
    v_old   VARCHAR2(6);
  BEGIN
    v_old := TO_CHAR(ADD_MONTHS(TO_DATE(p_period, 'YYYYMM'), -36), 'YYYYMM');

    INSERT INTO SETTLE_ARCHIVE
    SELECT * FROM SETTLE_LINE WHERE TO_CHAR(line_dt, 'YYYYMM') = v_old;

    v_sql := 'ALTER TABLE SETTLE_LINE DROP PARTITION P_' || v_old;
    BEGIN
      EXECUTE IMMEDIATE v_sql;
    EXCEPTION
      WHEN OTHERS THEN
        DBMS_OUTPUT.PUT_LINE('no partition: ' || v_old);
    END;

    DBMS_STATS.GATHER_TABLE_STATS('FIN', 'SETTLE_ARCHIVE');
    COMMIT;
  END archive_and_purge;

  -- ============================================================
  -- 메인 오케스트레이션
  -- ============================================================
  PROCEDURE run_global_settlement(p_period IN VARCHAR2, p_region IN VARCHAR2 DEFAULT 'ALL') IS
    CURSOR c_region IS
      SELECT region_code, root_org FROM REGION_MST
       WHERE (p_region = 'ALL' OR region_code = p_region)
         AND active = 'Y';
    v_grade VARCHAR2(2);
  BEGIN
    SELECT SEQ_SETTLE_BATCH.NEXTVAL INTO g_batch_id FROM DUAL;
    UTL_FILE.FGETATTR('LOG_DIR', 'settle.log', g_err_cnt, g_err_cnt, g_err_cnt);

    FOR rg IN c_region LOOP
      g_region := rg.region_code;
      DBMS_OUTPUT.PUT_LINE('region: ' || g_region);

      -- 지역별 계약 채번/적재
      INSERT INTO SETTLE_LINE (line_id, org_id, batch_id, amount, ccy, line_dt, status)
      SELECT SEQ_SETTLE_LINE.NEXTVAL, c.org_id, g_batch_id, u.charge, c.ccy, SYSDATE, 'PENDING'
        FROM CONTRACT c
        JOIN USAGE_LOG@GROUP_HUB u ON u.contract_id = c.contract_id
       WHERE c.region = g_region
         AND c.contract_id IN (SELECT contract_id FROM ELIGIBLE_CONTRACT
                                WHERE period = p_period);

      consolidate_hierarchy(rg.root_org);
      distribute_charges(g_batch_id);
      reconcile_all(g_batch_id);

      -- 등급 기반 후처리 (외부 함수/패키지 재사용)
      v_grade := FN_CUSTOMER_GRADE(rg.root_org);
      IF v_grade IN ('A', 'B') THEN
        PKG_BILLING.POST_LEDGER(g_batch_id, TRUE);
      END IF;
    END LOOP;

    archive_and_purge(p_period);
    COMMIT;
  EXCEPTION
    WHEN NO_DATA_FOUND THEN
      ROLLBACK;
    WHEN OTHERS THEN
      g_err_cnt := g_err_cnt + 1;
      ROLLBACK;
      RAISE;
  END run_global_settlement;

END PKG_SETTLEMENT_ENGINE;
/
GRANT EXECUTE ON FIN.PKG_SETTLEMENT_ENGINE TO ERP_APP;
GRANT EXECUTE ON FIN.PKG_SETTLEMENT_ENGINE TO BATCH_USER;
GRANT EXECUTE ON FIN.PKG_SETTLEMENT_ENGINE TO GROUP_CONSOL;
