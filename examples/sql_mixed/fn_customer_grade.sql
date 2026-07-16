CREATE OR REPLACE FUNCTION FIN.FN_CUSTOMER_GRADE(p_ref_id IN NUMBER) RETURN VARCHAR2 IS
  v_score  NUMBER := 0;
  v_grade  VARCHAR2(2);
BEGIN
  -- 최근 12개월 결제 이력 점수화 (서브쿼리 1단)
  SELECT NVL(SUM(CASE WHEN paid >= amount THEN 10 ELSE -5 END), 0)
    INTO v_score
    FROM INVOICE
   WHERE cust_id = (SELECT cust_id FROM CONTRACT WHERE contract_id = p_ref_id)
     AND yyyymm >= TO_CHAR(ADD_MONTHS(SYSDATE, -12), 'YYYYMM');

  IF v_score >= 80 THEN
    v_grade := 'A';
  ELSIF v_score >= 40 THEN
    v_grade := 'B';
  ELSIF v_score >= 0 THEN
    v_grade := 'C';
  ELSE
    v_grade := 'D';
  END IF;

  RETURN v_grade;
END FN_CUSTOMER_GRADE;
/
