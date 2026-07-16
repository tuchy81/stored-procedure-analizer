CREATE OR REPLACE PROCEDURE FIN.PRC_EXPIRE_CONTRACTS(p_asof IN DATE) IS
  CURSOR c_expired IS
    SELECT contract_id, cust_id
      FROM CONTRACT
     WHERE end_date < p_asof
       AND status = 'ACTIVE'
       AND contract_id NOT IN (SELECT contract_id FROM CONTRACT_HOLD
                                WHERE hold_until >= p_asof);
  v_grade VARCHAR2(2);
  v_cnt   NUMBER := 0;
BEGIN
  FOR r IN c_expired LOOP
    v_grade := FN_CUSTOMER_GRADE(r.contract_id);

    UPDATE CONTRACT
       SET status = 'EXPIRED', updated_at = SYSDATE
     WHERE contract_id = r.contract_id;

    IF SQL%ROWCOUNT > 0 THEN
      INSERT INTO CONTRACT_HIST (contract_id, action, grade)
      VALUES (r.contract_id, 'EXPIRE', v_grade);
      v_cnt := v_cnt + 1;
    END IF;

    -- 우량 등급은 만료 예고 통지 대상으로 별도 적재
    IF v_grade IN ('A', 'B') THEN
      INSERT INTO NOTIFY_QUEUE (cust_id, kind) VALUES (r.cust_id, 'EXPIRE_WARN');
    ELSIF v_grade = 'D' THEN
      DELETE FROM AUTO_RENEW WHERE contract_id = r.contract_id;
    END IF;
  END LOOP;

  UPDATE BATCH_LOG SET expired_cnt = v_cnt WHERE batch_dt = TRUNC(p_asof);
  COMMIT;
EXCEPTION
  WHEN OTHERS THEN
    ROLLBACK;
    RAISE;
END PRC_EXPIRE_CONTRACTS;
/
