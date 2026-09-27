"""
모니터링 화면 (PLAN v2 Step 5, 선택 사용). 로컬에서만 실행한다 — service role key 를 쓰므로 공개 배포하지 않는다.

  streamlit run monitor_app.py

- held 글: 사유 확인, 훅 후보 교체, 수동 승인(코드 가드 재통과) / 폐기
- 예정 글: 발행 취소
- 자동 정지 해제 (사유·이름 입력 필수, events 에 기록)
화면의 모든 동작은 actions.py·autostop.py 함수를 부른다 (테스트는 그쪽에서 한다).
"""
from __future__ import annotations

from datetime import timezone

import streamlit as st

import actions
import autostop
import digest
import rules
from settings import KST, load_config, load_secrets
from store import open_store

st.set_page_config(page_title="스레드 모니터링", layout="wide")


@st.cache_resource
def _setup():
    return load_config(), open_store(load_secrets())


cfg, store = _setup()
operator = st.sidebar.text_input("내 이름 (승인·폐기·해제 기록용)")

st.title("스레드 × 쿠팡 모니터링")

# 자동 정지
stop = autostop.status(store)
if stop.get("active"):
    st.error("자동 정지 중 — 발행이 멈춰 있습니다")
    for r in stop.get("reasons", []):
        st.write(f"- {r['at']}: {r['reason']}")
    reason = st.text_input("해제 사유 (필수)")
    if st.button("자동 정지 해제"):
        try:
            autostop.clear(store, reason, operator)
            st.success("해제했습니다")
            st.rerun()
        except ValueError as e:
            st.warning(str(e))
else:
    st.success("자동 정지 없음")

# held
st.header("보류(held) 글")
for post in store.list_posts(["held"]):
    with st.expander(f"{post['type']} · {digest._hook(post) or '(초안 없음)'} · {post['id'][:8]}"):
        st.text(post["text"])
        if post.get("reply_text"):
            st.caption("답글")
            st.text(post["reply_text"])
        st.warning("사유: " + digest.held_reason(post))
        styles = [h["style"] for h in post.get("hook_candidates") or []]
        cols = st.columns(4)
        if styles:
            style = cols[0].selectbox("훅 후보", styles, key=f"s{post['id']}")
            if cols[1].button("훅 교체", key=f"h{post['id']}"):
                _, problems = actions.swap_hook(store, cfg, post["id"], style)
                st.warning(" / ".join(problems)) if problems else st.rerun()
        if cols[2].button("수동 승인", key=f"a{post['id']}"):
            try:
                actions.manual_approve(store, cfg, post["id"], operator)
                st.rerun()
            except actions.ActionError as e:
                st.warning(str(e))
        if cols[3].button("폐기", key=f"d{post['id']}"):
            try:
                actions.discard(store, post["id"], operator)
                st.rerun()
            except actions.ActionError as e:
                st.warning(str(e))

# 예정
st.header("예정 글")
for post in sorted(store.list_posts(["approved"]), key=lambda p: p.get("scheduled_at") or ""):
    when = rules.post_time(post)
    label = when.astimezone(KST).strftime("%m-%d %H:%M") if when else "슬롯 없음"
    cols = st.columns([6, 1])
    cols[0].write(f"{label} · {post['type']} · {digest._hook(post)} · `{post['id']}`")
    if cols[1].button("취소", key=f"c{post['id']}"):
        try:
            actions.cancel_post(store, post["id"], by=operator or "monitor_app")
            st.rerun()
        except actions.ActionError as e:
            st.warning(str(e))
