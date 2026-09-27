"""
Step 4-C 검수자 시험지 테스트.

8장 체크리스트 (AI 검수자)
  - 검수 모델·프롬프트·케이스 중 하나를 바꾸면 불합격 상태로 전환
  - (시험지 20/20 차단·8/10 통과 자체는 실제 Claude 로 `python exam.py` 를 돌려 증명한다.
    여기서는 채점 규칙이 정확한지와 시험지 구성이 PLAN 요건을 채우는지를 검증한다.)
"""
import dataclasses
import json
import shutil
from collections import Counter

import pytest

import exam
import guard
import reviewer

DATA = exam.load_cases()
CASES = DATA["cases"]


# ── 시험지 구성 (PLAN Step 4-C 요건) ─────────────────────────────────────────

def test_case_counts():
    expects = Counter(c["expect"] for c in CASES)
    assert expects == {"block": 20, "pass": 10}
    assert len({c["id"] for c in CASES}) == 30


def test_problem_cases_cover_required_items():
    counts = Counter(v for c in CASES if c["expect"] == "block" for v in c["violations"])
    assert counts["R2"] >= 3 and counts["R3"] >= 4 and counts["R6"] >= 3
    assert set(reviewer.ITEM_CODES) <= set(counts)          # R1~R12 전부 한 번 이상
    assert all(1 <= len(c["violations"]) <= 2 for c in CASES if c["expect"] == "block")


def test_more_than_half_of_problem_cases_evade_the_guard(cfg):
    problems = [c for c in CASES if c["expect"] == "block"]
    evading = [c for c in problems if not guard.check_post(exam.build_case(c, DATA, cfg)[0], cfg)]
    assert len(evading) >= len(problems) / 2


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_guard_result_matches_case_label(cfg, case):
    # 정상 글은 가드를 통과해야 하고, 문제 글의 guard_blocks 표기가 실제 가드 결과와 같아야 한다
    post, _, _ = exam.build_case(case, DATA, cfg)
    assert bool(guard.check_post(post, cfg)) == case["guard_blocks"], guard.check_post(post, cfg)


def test_recent_posts_built_for_duplicate_case(cfg):
    case = next(c for c in CASES if c["id"] == "P17")
    _, _, recent = exam.build_case(case, DATA, cfg)
    n01 = exam.build_case(next(c for c in CASES if c["id"] == "N01"), DATA, cfg)[0]["text"]
    assert recent[0] == n01


# ── 채점 규칙 ──────────────────────────────────────────────────────────────

def ideal_reviewer(cfg, overrides=None):
    """케이스 기대값대로 답하는 가짜 검수자. overrides={케이스 id: 'approved'|'fail'} 로 일부만 틀리게 한다."""
    overrides = overrides or {}
    by_text = {exam.build_case(c, DATA, cfg)[0]["text"]: c for c in CASES}

    def call(system, user):
        case = by_text[json.loads(user)["post"]["text"]]
        outcome = overrides.get(case["id"], "approved" if case["expect"] == "pass" else "fail")
        items = {code: {"verdict": "pass", "reason": "", "hook_only": False} for code in reviewer.ITEM_CODES}
        if outcome == "fail":
            items[(case["violations"] or ["R11"])[0]]["verdict"] = "fail"
        return "end_turn", json.dumps(items)

    return reviewer.Reviewer(cfg, call)


def test_perfect_reviewer_passes(cfg):
    summary = exam.run_exam(ideal_reviewer(cfg), cfg, DATA)
    assert summary["passed"] is True
    assert (summary["problem_blocked"], summary["normal_passed"]) == ("20/20", "10/10")


def test_one_missed_problem_fails_the_exam(cfg):
    summary = exam.run_exam(ideal_reviewer(cfg, {"P04": "approved"}), cfg, DATA)
    assert summary["passed"] is False
    assert summary["missed_problems"] == ["P04"]


def test_two_rejected_normals_still_pass_but_three_fail(cfg):
    assert exam.run_exam(ideal_reviewer(cfg, {"N01": "fail", "N02": "fail"}), cfg, DATA)["passed"] is True
    summary = exam.run_exam(ideal_reviewer(cfg, {"N01": "fail", "N02": "fail", "N03": "fail"}), cfg, DATA)
    assert summary["passed"] is False and summary["normal_passed"] == "7/10"


# ── 8장: 모델·프롬프트·케이스 변경 → 불합격 전환 ─────────────────────────────────

@pytest.fixture
def passed_state(cfg, tmp_path):
    cases = tmp_path / "cases.json"
    shutil.copy(exam.CASES_PATH, cases)
    prompt = tmp_path / "reviewer.md"
    shutil.copy(reviewer.PROMPT_PATH, prompt)
    state = tmp_path / "state" / "reviewer_exam.json"
    summary = exam.run_exam(ideal_reviewer(cfg), cfg, DATA)
    exam.write_state(summary, cfg, state_path=state, cases_path=cases, prompt_path=prompt)
    return {"state_path": state, "cases_path": cases, "prompt_path": prompt}


def test_passed_state_is_valid(cfg, passed_state):
    ok, reason = exam.exam_status(cfg, **passed_state)
    assert ok, reason


def test_model_change_invalidates(cfg, passed_state):
    other = dataclasses.replace(cfg, reviewer_model="claude-sonnet-5")
    ok, reason = exam.exam_status(other, **passed_state)
    assert not ok and "검수 모델" in reason


def test_prompt_change_invalidates(cfg, passed_state):
    passed_state["prompt_path"].write_text(
        passed_state["prompt_path"].read_text(encoding="utf-8") + "\n조금 더 너그럽게 판정합니다.", encoding="utf-8")
    ok, reason = exam.exam_status(cfg, **passed_state)
    assert not ok and "프롬프트" in reason


def test_adding_a_case_invalidates(cfg, passed_state):
    # 운영 중 검수자가 놓친 글을 문제 글로 추가하면 재시험 전까지 불합격 (규칙 10)
    data = json.loads(passed_state["cases_path"].read_text(encoding="utf-8"))
    data["cases"].append({**data["cases"][10], "id": "P21", "memo": "운영 중 놓친 글"})
    passed_state["cases_path"].write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    ok, reason = exam.exam_status(cfg, **passed_state)
    assert not ok and "케이스" in reason


def test_failed_or_missing_state_is_not_passed(cfg, passed_state, tmp_path):
    ok, reason = exam.exam_status(cfg, state_path=tmp_path / "없음.json")
    assert not ok and "기록이 없습니다" in reason
    state = json.loads(passed_state["state_path"].read_text(encoding="utf-8"))
    state["passed"] = False
    passed_state["state_path"].write_text(json.dumps(state), encoding="utf-8")
    assert exam.exam_status(cfg, **passed_state)[0] is False


def test_real_project_has_not_passed_yet(cfg):
    # 실제 Claude 로 시험을 보기 전까지 이 프로젝트는 불합격 상태다 (발행 거부)
    if exam.STATE_PATH.exists():
        pytest.skip("실제 시험 기록이 있음")
    assert exam.exam_status(cfg)[0] is False
