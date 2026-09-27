유형: calc (상품 1개의 한 달 전기요금)

훅: 계산 결과 숫자(monthly_kwh 또는 est_added_cost_label)가 훅 자체가 되게 씁니다.
    예: "2,000W 히터 하루 4시간이면 한 달 240kWh"

본문 구성
- 추가 전기요금 추정치(est_added_cost_label)와 가정 문구(assumption_text)를 씁니다. 가정 문구는 글자 하나 바꾸지 않고 넣습니다.
- product.note 에 적힌 특징을 참고해 사용 상황을 한두 문장으로 씁니다. note 에 없는 제품 특징은 쓰지 않습니다.
- 마지막 줄: 이 사용량이 어떤 상황에 맞는지 결론 한 문장.
