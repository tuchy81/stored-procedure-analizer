CREATE OR REPLACE PROCEDURE FIN.PRC_RECONCILE(p_invoice_id IN NUMBER) IS
  v_grade    VARCHAR2(2);
  v_ext_amt  NUMBER;
  v_diff     NUMBER;
  v_sql      VARCHAR2(2000);
  v_cnt      NUMBER;
BEGIN
  -- 외부 정산 시스템 대사 (DB Link)
  SELECT settle_amount INTO v_ext_amt
    FROM SETTLE_RESULT@ERP_LINK
   WHERE invoice_id = p_invoice_id;

  SELECT amount + penalty INTO v_diff
    FROM INVOICE
   WHERE invoice_id = p_invoice_id;

  v_diff := v_diff - v_ext_amt;
  v_grade := FN_CUSTOMER_GRADE(p_invoice_id);

  IF ABS(v_diff) < 1 THEN
    UPDATE INVOICE SET recon_status = 'OK' WHERE invoice_id = p_invoice_id;
  ELSIF v_diff > 0 THEN
    UPDATE INVOICE SET recon_status = 'SHORT' WHERE invoice_id = p_invoice_id;
    INSERT INTO RECON_DIFF (invoice_id, diff, grade) VALUES (p_invoice_id, v_diff, v_grade);
  ELSE
    UPDATE INVOICE SET recon_status = 'OVER' WHERE invoice_id = p_invoice_id;
  END IF;

  -- 등급이 VIP 면 별도 로그 테이블(동적)
  IF v_grade IN ('A', 'B') THEN
    v_sql := 'INSERT INTO RECON_VIP_' || v_grade || ' (invoice_id, diff) VALUES (:1, :2)';
    EXECUTE IMMEDIATE v_sql USING p_invoice_id, v_diff;
  END IF;

  SELECT COUNT(*) INTO v_cnt FROM RECON_DIFF WHERE invoice_id = p_invoice_id;
  DBMS_OUTPUT.PUT_LINE('recon done diff=' || v_diff);
EXCEPTION
  WHEN NO_DATA_FOUND THEN
    INSERT INTO RECON_DIFF (invoice_id, diff, grade) VALUES (p_invoice_id, NULL, '?');
  WHEN OTHERS THEN
    RAISE;
END PRC_RECONCILE;
/
GRANT EXECUTE ON FIN.PRC_RECONCILE TO ERP_APP;
