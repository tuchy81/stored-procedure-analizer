CREATE OR REPLACE PACKAGE HR.PKG_ORDER AS
  PROCEDURE create_order(p_cust_id IN NUMBER, p_amount IN NUMBER);
  FUNCTION order_total(p_order_id IN NUMBER) RETURN NUMBER;
END PKG_ORDER;
/
CREATE OR REPLACE PACKAGE BODY HR.PKG_ORDER AS

  PROCEDURE create_order(p_cust_id IN NUMBER, p_amount IN NUMBER) IS
    v_order_id NUMBER;
    v_tax      NUMBER;
  BEGIN
    -- 신규 주문번호 채번
    SELECT SEQ_ORDER.NEXTVAL INTO v_order_id FROM DUAL;

    IF p_amount > 1000 THEN
      v_tax := p_amount * 0.1;
    ELSIF p_amount > 100 THEN
      v_tax := p_amount * 0.05;
    ELSE
      v_tax := 0;
    END IF;

    INSERT INTO ORDERS (order_id, cust_id, amount, tax)
    VALUES (v_order_id, p_cust_id, p_amount, v_tax);

    -- 감사 로그는 공용 프로시저 호출
    LOG_AUDIT('ORDER', v_order_id);

    -- 원격 재고 시스템 반영 (DB Link)
    UPDATE INVENTORY@REMOTE_WMS
       SET reserved = reserved + 1
     WHERE cust_id = p_cust_id;

    DBMS_OUTPUT.PUT_LINE('order created: ' || v_order_id);
    COMMIT;
  EXCEPTION
    WHEN OTHERS THEN
      ROLLBACK;
      RAISE;
  END create_order;

  FUNCTION order_total(p_order_id IN NUMBER) RETURN NUMBER IS
    v_total NUMBER := 0;
  BEGIN
    SELECT SUM(amount + tax) INTO v_total
      FROM ORDERS
     WHERE order_id = p_order_id;
    RETURN v_total;
  END order_total;

END PKG_ORDER;
/
GRANT EXECUTE ON HR.PKG_ORDER TO APP_USER;
