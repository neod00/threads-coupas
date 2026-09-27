유형: compare (두 제품의 한 달 전기요금 차이)

훅: 두 제품의 요금 차이(cost_diff_label) 또는 사용량 차이(kwh_diff)가 훅 자체가 되게 씁니다.
    예: "온수매트 vs 전기요, 한 달 전기료 차이 약 ○○원"

본문 구성
- 두 제품 각각의 소비전력(power_w), 하루 사용시간(hours_per_day), 월 사용량(monthly_kwh), 추정 요금(est_added_cost_label)을 한 문장씩 씁니다.
- 요금이 더 나오는 쪽은 higher_cost_name 입니다. 방향을 바꿔 쓰지 않습니다. higher_cost_name 이 null 이면 두 제품의 추정 요금이 같다고 씁니다.
- 가정 문구(assumption_text)를 글자 하나 바꾸지 않고 넣습니다.
- 마지막 줄: 어떤 경우에 어느 쪽이 나은지 결론 한 문장. 제품 품질이나 성능을 평가하지 않고 전기요금 기준으로만 씁니다.
