CREATE OR REPLACE PROCEDURE HR.PRC_SETTLE_DAILY(p_dt IN DATE) IS
  CURSOR c_cust IS
    SELECT cust_id
      FROM CUSTOMERS
     WHERE status = 'ACTIVE';
  v_total NUMBER;
  v_sql   VARCHAR2(4000);
BEGIN
  FOR r IN c_cust LOOP
    -- 고객별 합계 (패키지 함수 재사용)
    v_total := PKG_ORDER.ORDER_TOTAL(r.cust_id);

    -- 중첩 서브쿼리 집계
    SELECT COUNT(*)
      INTO v_total
      FROM ORDERS o
     WHERE o.cust_id = r.cust_id
       AND o.order_id IN (SELECT order_id
                            FROM ORDER_ITEMS
                           WHERE qty > (SELECT AVG(qty) FROM ORDER_ITEMS));

    -- 동적 SQL 로 월별 파티션 테이블 갱신
    v_sql := 'UPDATE SETTLE_' || TO_CHAR(p_dt, 'YYYYMM') || ' SET total = :1 WHERE cust_id = :2';
    EXECUTE IMMEDIATE v_sql USING v_total, r.cust_id;

    LOG_AUDIT('SETTLE', r.cust_id);
  END LOOP;
  COMMIT;
EXCEPTION
  WHEN NO_DATA_FOUND THEN
    NULL;
  WHEN OTHERS THEN
    ROLLBACK;
    RAISE;
END PRC_SETTLE_DAILY;
/
