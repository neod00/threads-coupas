"""
검수자 시험지 (PLAN v2 Step 4-C) — 출시 전 필수 관문.

통과 기준
  - 문제 글 20개: 전부 fail 또는 uncertain (하나라도 approved 면 불합격)
  - 정상 글 10개: 8개 이상 approved (너무 깐깐해서 전부 막는 것도 불합격)
합격하면 state/reviewer_exam.json 에 검수 모델·프롬프트 해시·케이스 해시와 함께 기록한다.
검수 모델·프롬프트(PLAN 1장 규칙 전문 포함)·케이스 중 하나라도 바뀌면 exam_status() 가 불합격을 돌려주고,
발행기는 재시험 합격 전까지 실행을 거부한다 (규칙 9·10).

실행 (Claude API 비용 발생 — 30회 안팎 호출):
  python exam.py
"""
from __future__ import annotations

import hashlib
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import calculator
import generator
import reviewer as reviewer_mod
from products import parse_products
from settings import PROJECT_ROOT, Config

log = logging.getLogger("threads_coupas")

CASES_PATH = PROJECT_ROOT / "tests" / "review_cases" / "cases.json"
STATE_PATH = PROJECT_ROOT / "state" / "reviewer_exam.json"
NORMAL_MIN_PASSED = 8               # 문제 글은 전부 막아야 하고, 정상 글은 이 개수 이상 통과해야 한다
ABORT_AFTER_CALL_FAILURES = 3       # 호출 자체가 연속으로 실패하면 시험을 중단한다 (크레딧 부족·인증 오류)


def load_cases(path: Path = CASES_PATH) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def cases_hash(path: Path = CASES_PATH) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def build_case(case: dict, data: dict, cfg: Config) -> tuple[dict, list[dict], list[str]]:
    """(게시물, 상품 행, 최근 글 본문). 생성기와 같은 방식으로 조립한다."""
    tariff = calculator.parse_tariff(data["tariff"])
    by_id = {p.product_id: p for p in parse_products(data["products"])}
    picked = [by_id[pid] for pid in case["product_ids"]]
    post_type = case["type"]
    payload = generator.build_payload(post_type, picked, tariff, case.get("tip_topic"))

    head = f"{case['hook']}\n\n{case['body']}"
    if post_type == "tip":
        post = {"type": "tip", "text": head, "reply_text": None, "has_link": False, "link_url": None, "variant": None}
    else:
        variant = case.get("variant", "link_in_body")
        text = head if case.get("omit_disclosure") else f"{cfg.disclosure_text}\n{head}"
        reply = f"{cfg.disclosure_text}\n\n{case['reply_line']}" if case.get("reply_line") else None
        post = {"type": post_type, "text": text, "reply_text": reply, "has_link": True,
                "link_url": picked[0].link, "variant": variant}
    post["calc_payload"] = payload

    by_case = {c["id"]: c for c in data["cases"]}
    recent = [build_case(by_case[cid], data, cfg)[0]["text"] for cid in case.get("recent_from", [])]
    rows = reviewer_mod.product_rows(picked) if post_type != "tip" else []
    return post, rows, recent


def run_exam(rev: reviewer_mod.Reviewer, cfg: Config, data: dict | None = None) -> dict:
    """모든 케이스를 검수하고 합격 여부를 계산한다. 가드는 거치지 않는다 (검수자 자체의 실력을 본다)."""
    data = data or load_cases()
    results, call_failures = [], 0
    for case in data["cases"]:
        post, rows, recent = build_case(case, data, cfg)
        r = rev.review(post, rows, recent)
        # 판정을 내리지 못한 형식 오류는 '차단'이 아니다 → 문제 글·정상 글 모두 실패로 센다
        ok = r.status in ("fail", "uncertain") if case["expect"] == "block" else r.status == "approved"
        results.append({"id": case["id"], "expect": case["expect"], "violations": case["violations"],
                        "status": r.status, "ok": ok, "problems": r.problems()})
        log.info(f"[시험] {case['id']} 기대={case['expect']} 판정={r.status} {'O' if ok else 'X'}")
        # 크레딧 부족·인증 오류처럼 호출 자체가 계속 실패하면 남은 케이스를 부르지 않고 멈춘다
        call_failures = call_failures + 1 if r.status == "format_error" and all("호출 실패" in e for e in r.errors) else 0
        if call_failures >= ABORT_AFTER_CALL_FAILURES:
            log.error(f"[시험] API 호출이 {call_failures}개 케이스 연속으로 실패해 시험을 중단합니다: {r.errors[-1][:200]}")
            break

    problems = [r for r in results if r["expect"] == "block"]
    normals = [r for r in results if r["expect"] == "pass"]
    blocked = sum(r["ok"] for r in problems)
    passed_normals = sum(r["ok"] for r in normals)
    completed = len(results) == len(data["cases"])
    return {
        "passed": completed and blocked == len(problems) and passed_normals >= NORMAL_MIN_PASSED,
        "completed": completed,
        "problem_blocked": f"{blocked}/{len(problems)}",
        "normal_passed": f"{passed_normals}/{len(normals)}",
        "missed_problems": [r["id"] for r in problems if not r["ok"]],
        "rejected_normals": [r["id"] for r in normals if not r["ok"]],
        "results": results,
    }


def write_state(summary: dict, cfg: Config, state_path: Path = STATE_PATH, cases_path: Path = CASES_PATH,
                **prompt_paths) -> dict:
    state = {
        "passed": summary["passed"],
        "passed_at": datetime.now(timezone.utc).isoformat() if summary["passed"] else None,
        "reviewer_model": cfg.reviewer_model,
        "prompt_hash": reviewer_mod.prompt_hash(cfg, **prompt_paths),
        "cases_hash": cases_hash(cases_path),
        "summary": {k: v for k, v in summary.items() if k != "results"},
    }
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return state


def exam_status(cfg: Config, state_path: Path = STATE_PATH, cases_path: Path = CASES_PATH,
                **prompt_paths) -> tuple[bool, str]:
    """(합격 여부, 사유). 합격 기록이 있고 모델·프롬프트·케이스 해시가 지금과 모두 같아야 합격이다."""
    try:
        state = json.loads(Path(state_path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False, "시험지 기록이 없습니다 (python exam.py 로 시험을 보세요)"
    except ValueError:
        return False, "시험지 기록 파일이 손상됐습니다"
    if not state.get("passed"):
        return False, f"시험지 불합격 상태입니다: {state.get('summary')}"
    if state.get("reviewer_model") != cfg.reviewer_model:
        return False, f"검수 모델이 바뀌었습니다 ({state.get('reviewer_model')} → {cfg.reviewer_model}). 재시험이 필요합니다"
    if state.get("prompt_hash") != reviewer_mod.prompt_hash(cfg, **prompt_paths):
        return False, "검수 프롬프트(또는 PLAN 1장 규칙)가 바뀌었습니다. 재시험이 필요합니다"
    if state.get("cases_hash") != cases_hash(cases_path):
        return False, "시험 케이스가 바뀌었습니다(케이스 추가 포함). 재시험이 필요합니다"
    return True, f"합격 ({state.get('passed_at')}, {state['summary'].get('problem_blocked')} 차단, {state['summary'].get('normal_passed')} 통과)"


def main() -> int:
    from settings import ConfigError, load_config, load_secrets

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stderr)
    try:
        cfg, secrets = load_config(), load_secrets()
        secrets.require("anthropic_api_key")
    except ConfigError as e:
        print(f"오류: {e}")
        return 1
    rev = reviewer_mod.Reviewer(cfg, reviewer_mod.make_anthropic_call(secrets.anthropic_api_key, cfg.reviewer_model))
    summary = run_exam(rev, cfg)
    write_state(summary, cfg)
    print(json.dumps({k: v for k, v in summary.items() if k != "results"}, ensure_ascii=False, indent=2))
    for r in summary["results"]:
        if not r["ok"]:
            print(f"- {r['id']} (기대 {r['expect']}, 판정 {r['status']}): {r['problems'][:3]}")
    print("합격" if summary["passed"] else "불합격 — 발행 불가 상태가 유지됩니다")
    return 0 if summary["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
