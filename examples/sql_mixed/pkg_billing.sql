CREATE OR REPLACE PACKAGE FIN.PKG_BILLING AS
  PROCEDURE run_monthly_billing(p_yyyymm IN VARCHAR2);
  PROCEDURE post_ledger(p_batch_id IN NUMBER, p_force IN BOOLEAN DEFAULT FALSE);
  FUNCTION calc_penalty(p_contract_id IN NUMBER, p_overdue_days IN NUMBER) RETURN NUMBER;
END PKG_BILLING;
/
CREATE OR REPLACE PACKAGE BODY FIN.PKG_BILLING AS

  g_run_seq NUMBER;

  -- ===== 내부 유틸 =====
  FUNCTION calc_penalty(p_contract_id IN NUMBER, p_overdue_days IN NUMBER) RETURN NUMBER IS
    v_rate    NUMBER;
    v_balance NUMBER;
    v_penalty NUMBER := 0;
  BEGIN
    SELECT rate INTO v_rate
      FROM PENALTY_POLICY
     WHERE grade = FN_CUSTOMER_GRADE(p_contract_id);

    SELECT NVL(SUM(amount - paid), 0) INTO v_balance
      FROM INVOICE
     WHERE contract_id = p_contract_id
       AND status IN (SELECT code FROM CODE_MST
                       WHERE grp = 'INV_STAT'
                         AND code IN (SELECT open_code FROM POLICY_OPEN));

    IF p_overdue_days > 90 THEN
      v_penalty := v_balance * v_rate * 3;
    ELSIF p_overdue_days > 30 THEN
      v_penalty := v_balance * v_rate * 2;
    ELSIF p_overdue_days > 0 THEN
      v_penalty := v_balance * v_rate;
    ELSE
      v_penalty := 0;
    END IF;

    RETURN v_penalty;
  END calc_penalty;

  -- ===== 월 청구 배치 =====
  PROCEDURE run_monthly_billing(p_yyyymm IN VARCHAR2) IS
    CURSOR c_contract IS
      SELECT c.contract_id, c.cust_id, c.plan_id
        FROM CONTRACT c
       WHERE c.status = 'ACTIVE'
         AND EXISTS (SELECT 1 FROM USAGE_LOG@DWLINK u
                      WHERE u.contract_id = c.contract_id
                        AND u.yyyymm = p_yyyymm);
    v_invoice_id NUMBER;
    v_amount     NUMBER;
    v_penalty    NUMBER;
    v_overdue    NUMBER;
    v_sql        VARCHAR2(4000);
  BEGIN
    SELECT SEQ_BILLING_RUN.NEXTVAL INTO g_run_seq FROM DUAL;
    DBMS_OUTPUT.PUT_LINE('billing run started: ' || g_run_seq);

    FOR r IN c_contract LOOP
      -- 원격 사용량 집계 (DB Link)
      SELECT NVL(SUM(charge), 0) INTO v_amount
        FROM USAGE_LOG@DWLINK
       WHERE contract_id = r.contract_id
         AND yyyymm = p_yyyymm;

      -- 연체일수 산정
      SELECT NVL(MAX(TRUNC(SYSDATE) - due_date), 0) INTO v_overdue
        FROM INVOICE
       WHERE contract_id = r.contract_id
         AND paid < amount;

      v_penalty := calc_penalty(r.contract_id, v_overdue);

      SELECT SEQ_INVOICE.NEXTVAL INTO v_invoice_id FROM DUAL;

      INSERT INTO INVOICE (invoice_id, contract_id, cust_id, yyyymm, amount, penalty, status, run_seq)
      VALUES (v_invoice_id, r.contract_id, r.cust_id, p_yyyymm, v_amount, v_penalty, 'OPEN', g_run_seq);

      -- 등급별 파티션 테이블로 동적 분개
      v_sql := 'INSERT INTO CHARGE_' || p_yyyymm ||
               ' (invoice_id, amount) VALUES (:1, :2)';
      EXECUTE IMMEDIATE v_sql USING v_invoice_id, v_amount + v_penalty;

      -- ERP 전송 큐 반영 (또 다른 DB Link)
      MERGE INTO ERP_QUEUE@ERP_LINK q
      USING (SELECT v_invoice_id AS inv FROM DUAL) s
         ON (q.invoice_id = s.inv)
       WHEN MATCHED THEN UPDATE SET q.amount = v_amount + v_penalty
       WHEN NOT MATCHED THEN INSERT (invoice_id, amount) VALUES (v_invoice_id, v_amount + v_penalty);

      PRC_RECONCILE(v_invoice_id);
    END LOOP;

    post_ledger(g_run_seq);
    COMMIT;
  EXCEPTION
    WHEN NO_DATA_FOUND THEN
      DBMS_OUTPUT.PUT_LINE('no usage found');
      ROLLBACK;
    WHEN OTHERS THEN
      ROLLBACK;
      RAISE;
  END run_monthly_billing;

  -- ===== 원장 전기 =====
  PROCEDURE post_ledger(p_batch_id IN NUMBER, p_force IN BOOLEAN DEFAULT FALSE) IS
    PRAGMA AUTONOMOUS_TRANSACTION;
    TYPE t_inv IS TABLE OF INVOICE%ROWTYPE;
    v_rows  t_inv;
    v_cnt   NUMBER := 0;
    v_dyn   VARCHAR2(2000);
  BEGIN
    SELECT * BULK COLLECT INTO v_rows
      FROM INVOICE
     WHERE run_seq = p_batch_id;

    FORALL i IN 1 .. v_rows.COUNT
      INSERT INTO LEDGER (invoice_id, amount, posted_at)
      VALUES (v_rows(i).invoice_id, v_rows(i).amount + v_rows(i).penalty, SYSDATE);

    FOR i IN 1 .. v_rows.COUNT LOOP
      IF v_rows(i).penalty > 0 OR p_force THEN
        v_dyn := 'UPDATE LEDGER SET flag = ''P'' WHERE invoice_id = :1';
        EXECUTE IMMEDIATE v_dyn USING v_rows(i).invoice_id;
        v_cnt := v_cnt + 1;
      END IF;
    END LOOP;

    UPDATE BILLING_RUN
       SET posted_cnt = v_cnt, status = 'POSTED'
     WHERE run_seq = p_batch_id;

    DELETE FROM STAGE_LEDGER WHERE run_seq = p_batch_id;
    COMMIT;
  EXCEPTION
    WHEN OTHERS THEN
      ROLLBACK;
      RAISE;
  END post_ledger;

END PKG_BILLING;
/
GRANT EXECUTE ON FIN.PKG_BILLING TO ERP_APP;
GRANT EXECUTE ON FIN.PKG_BILLING TO BATCH_USER;
