# REUSE_MAP — 티스토리 앱에서 무엇을 가져오나

> Step 0 산출물 (2026-09-27). 기존 앱은 읽기만 했고 수정하지 않았다.
> 2026-09-27 PLAN v2 통합본 기준으로 2-7, 3, 4, 5, 7장을 갱신했다.

- 분석 대상: `D:\OneDrive\Business\ai automation\AutoBlogByGoogleAI\AutoBlogByGoogleAI` (루트 = Climate Insight, 최신 커밋 `d089cf1`)
  - 하위 폴더 `DailyEnglishTips/`, `PlantGuide/`, `TistoryAidajigi/`는 루트 코드의 옛 사본이라 루트만 봤다.
- 추가 분석: PLAN 4단계가 지목한 로옥션 가드
  `D:\OneDrive\Business\ai automation\court_scraper\Deploy_StreamlitShare\Court_Scraper_Website_2025\scripts\column_guard.py`
- Threads API 확인 결과는 [THREADS_API_NOTES.md](THREADS_API_NOTES.md)에 따로 정리했다.

---

## 0. 결론 먼저

1. **"그대로 재사용"할 티스토리 코드는 사실상 없다.** 티스토리 앱의 생성부는 TypeScript(Node)이고, 새 프로젝트는 PLAN대로 Python이다. TS 모듈은 Python에서 import할 수 없으므로 LLM 클라이언트는 **로직을 Python으로 옮기는 "수정 후 재사용"**이 된다.
2. **가드는 티스토리 `quality_gate.py`보다 로옥션 `column_guard.py`가 훨씬 가깝다.** 숫자 허용 집합, 실패 사유를 되돌려주는 재생성 루프, `AUTO_PUBLISH_DISABLED` 킬 스위치가 이미 Python으로 있다.
3. **발행부(Selenium + 카카오 로그인), 키워드 발굴, 관리자 화면(React + Redis)은 쓰지 않는다.** 스레드는 공식 API로 발행하고, 글감은 `products.csv`에서 나온다.

PLAN의 예상과 다른 점

| PLAN 예상 | 실제 | 이유 |
|---|---|---|
| LLM 클라이언트 재사용 | Python 이식 (수정 후 재사용) | 원본이 TypeScript |
| 프롬프트 구조 재사용 | 로딩 방식만 재사용, 내용은 새로 작성 | 원본은 4,000자 HTML 블로그용 (이미지·표·출처·SEO) |
| 설정 로딩 재사용 | 로옥션 방식 재사용 + `config.yaml`은 새로 작성 | 티스토리는 런타임 설정을 Redis에 둠. YAML 설정 로더는 두 앱 모두 없음 |
| 가드 새로 작성 | 로옥션 가드 핵심 함수 재사용 | 숫자 검사·재생성 루프가 이미 검증된 코드 |

---

## 1. 티스토리 앱의 현재 구조

```
GitHub Actions (2시간마다) ─► Vercel /api/cron/auto-pilot ─(확률 발행)─► repository_dispatch
                                   │ Upstash Redis: admin:topics_queue, admin:settings
                                   ▼
auto-publish.yml:  generate-content.ts (Gemini 2.5 Flash + Google 검색, OpenAI 대체)
                 → quality_gate.py (HTML 블로그 품질 검사 25종)
                 → publish-to-tistory.py (Selenium, 카카오 로그인·쿠키)
                 → send-result-email.ts (Gmail) → /api/admin/topics 상태 갱신
Vercel Cron (하루 1회): daily-digest.ts (Gemini 키워드 발굴 → 큐 보충)
```

- 사람 승인 단계가 없다. 큐에 들어간 주제는 확률에 따라 자동 발행된다.
- 숫자 검증이 없다. 본문 생성에 Google 검색을 켜 두어 모델이 외부 숫자를 가져온다.

---

## 2. 모듈별 분류

분류: **그대로** = 복사해서 거의 그대로 / **수정** = 로직을 가져와 고쳐 씀 / **새로** = 새로 작성 / **안 씀**

### 2-1. LLM 호출

| 출처 파일 | 분류 | 새 파일 | 가져올 것 / 바꿀 것 |
|---|---|---|---|
| 티스토리 `api/_lib/aiProviders.ts`, `api/_lib/geminiErrors.ts` | 수정 (Python 이식) | `llm_client.py` | 가져올 것: Gemini 한도 초과 시 OpenAI 대체, 503·timeout만 지수 백오프로 재시도, 출력 한도에서 끊긴 응답(`status: incomplete`)은 실패 처리, 오류 분류 함수. 바꿀 것: `google-genai`·`openai` Python SDK, **Google 검색 도구 끔**(숫자 출처를 계산 JSON으로 제한), JSON 출력 강제, 추론 끔(`thinkingBudget: 0`), 출력 토큰 상한 축소 |
| 로옥션 `column_guard.py`의 `llm_generate` | 수정 | `generator.py` | 이전 초안과 실패 사유를 붙여 다시 쓰게 하는 방식, 재시도 때 temperature를 올려 같은 글 반복을 막는 방식, JSON 파싱(`_parse_json_object`) |
| 티스토리 `scripts/generate-content.ts` 본체 | 안 씀 (일부 패턴만) | — | 쓰는 것: 지시문 + 템플릿 + 컨텍스트 블록으로 프롬프트를 조립하는 구조. 안 쓰는 것: Pexels 이미지 주입(**규칙 2 위반**), RSS 내부 링크, 참고자료 섹션, 카테고리 분류, HTML 문단 정리 |

### 2-2. 프롬프트 관리

| 출처 | 분류 | 새 파일 | 내용 |
|---|---|---|---|
| 티스토리 `loadDirective()` (`directives/*.md`를 실행 시 읽음) | 그대로 (방식) | `prompts/threads_base.md`, `prompts/tpl_calc.md`, `tpl_compare.md`, `tpl_tip.md` | 유형별 템플릿 파일 분리 방식을 그대로 쓴다 |
| 티스토리 `directives/blog_instructions.md` 내용 | 수정 (일부 발췌) | `prompts/threads_base.md` | 쓰는 것: 지어낸 1인칭 경험 금지, 결론 먼저, 상투어 금지 목록. 안 쓰는 것: HTML 형식, 표·체크리스트·출처·SEO·분량 규칙 |
| 로옥션 `RULES_BLOCK` (프롬프트와 가드가 규칙 원문을 공유) | 수정 | `config.yaml` → 프롬프트·가드가 함께 읽음 | 금지어·대가성 문구·길이 제한을 `config.yaml` 한 곳에 두고 프롬프트와 가드가 같은 값을 쓴다 |

### 2-3. 설정·시크릿

| 출처 | 분류 | 새 파일 | 내용 |
|---|---|---|---|
| 티스토리 TS `process.env` 직접 읽기, Redis `admin:settings` | 안 씀 | — | 런타임 설정을 Redis에 두는 방식은 이 테스트에 불필요 |
| 티스토리 `tistory_login.py`의 `from_env()` (python-dotenv 선택 로드) | 수정 | `settings.py` | `.env` 로드 → `os.getenv` 패턴 |
| 로옥션 `load_dotenv` + GitHub Secrets/Variables 조합 | 수정 | `settings.py`, 워크플로 | Secrets = 토큰·키, Variables = `AUTO_PUBLISH_DISABLED` |
| — | 새로 | `settings.py`, `config.yaml`, `.env.example` | `config.yaml` 스키마 검증, `DRY_RUN` 안전 기본값 파싱, 토큰 만료 10일 전 경고 |

### 2-4. 주제·키워드

| 출처 | 분류 | 이유 |
|---|---|---|
| `api/cron/daily-digest.ts`, `api/admin/discover-keywords.ts`, `api/admin/recommend-seeds.ts`, `directives/seo_keyword_discovery.md` | 안 씀 | 글감은 `products.csv` × 유형(calc/compare/tip)으로 정해진다. 검색 기반 키워드 발굴은 "숫자는 입력 데이터만" 원칙과 충돌 |
| `api/_lib/climateSeeds.ts`의 `selectSeedsForRun` (날짜 기반 순환 + 최근 사용 제외) | 수정 (아이디어) | `generator.py`의 상품 선택: 최근에 쓴 `product_id`를 빼고 돌아가며 고른다 |
| `directives/auto_publish_pipeline.md`의 계절성 메모 (난방비 11~1월 수요) | 참고만 | 테스트 시기 판단 근거 |

### 2-5. 가드

| 출처 함수 | 분류 | 새 위치 | 가져올 것 / 바꿀 것 |
|---|---|---|---|
| 로옥션 `allowed_numbers`, `_add_number`, `extract_numbers`, `_norm` | 수정 | `guard.py` 숫자 검사 (규칙 7) | 바꿀 것: **0~12를 무조건 허용하는 규칙을 없앤다.** PLAN 규칙 7은 "입력 데이터와 계산 결과에 있는 값만"이므로 하루 사용시간·월 사용일수·누진 단계도 계산 JSON에 넣어 허용 근거로 삼는다. 사건번호 처리는 뺀다 |
| 로옥션 `generate_with_guard`, `GuardFailed`, `attempts_log` | 수정 | `generator.py` | 최초 1회 + 재생성 최대 2회(PLAN 4단계). 끝내 실패하면 `status=rejected`, `attempts_log`를 `guard_log`에 저장 |
| 로옥션 `auto_publish_disabled()` | 수정 | `settings.py` | 규칙 8 킬 스위치. `1/true/yes/on`을 켜짐으로 해석. 바꾼 점: 빈 값·`0/false/no/off` 외의 알 수 없는 값(예: 오타 `ture`)도 켜짐으로 본다. 오타 때문에 킬 스위치가 무시되지 않게 하기 위해서다 |
| 로옥션 `won_label()` (예: 4228161 → "422만 8,161원") | 그대로 | `calculator.py` | 요금 표기 라벨. 계산기가 라벨을 미리 만들어 LLM은 옮기기만 한다 |
| 로옥션 `llm_factcheck` | 선택 (기본 끔) | — | compare 글에서 "A가 B보다 싸다"의 방향 오류처럼 숫자 밖의 오류를 잡을 수 있다. 비용이 들고 검수 UI에서 사람이 보므로 처음엔 끈다 |
| 로옥션 이름 마스킹·`CAUSAL_PATTERNS` | 안 씀 | — | 법원 공고 도메인 전용 |
| 티스토리 `QualityReport` (✅/❌/⚠️ + JSON 결과 저장) | 수정 | `guard.py` → `guard_log` 형식 | 항목별 통과/실패와 사유를 JSON으로 남긴다 |
| 티스토리 `AI_SPEAK_PATTERNS`, `FABRICATED_EXPERIENCE_PATTERNS`, `BANNED_EXPRESSIONS` | 수정 | `config.yaml` `banned_words` 보조 목록 | 짧은 글에도 해당. 지어낸 경험 패턴에 "써보니", "직접 써봤" 등을 더해 규칙 2(남의 후기 금지)를 보조한다 |
| 티스토리 HTML·SEO 검사 (이미지 최소 2장, 표, 체크리스트, 출처 링크, 태그 5개 등) | 안 씀 | — | 이미지 최소 검사는 규칙 2와 정반대 |
| — | 새로 | `guard.py` | 500자, 이모지, 대가성 문구 첫 줄, 답글 문구, URL 개수, `assumption_text` 포함, 가격 표현 |

### 2-6. 발행부

| 출처 | 분류 | 이유 / 가져올 교훈 |
|---|---|---|
| `services/execution/tistory_publisher.py`, `tistory_login.py`, `scripts/publish-to-tistory.py`, 쿠키 갱신 워크플로 2개 | 안 씀 | 브라우저 자동화 + 카카오 로그인. 스레드는 공식 Graph API |
| 발행 결과를 JSON 파일로 남겨 다음 단계가 읽는 패턴 | 수정 | 워크플로 로그·아티팩트에 DRY_RUN 요청 내용 저장 |
| 커밋 `d089cf1` 사례: 상태 갱신 호출이 실패해 큐가 `publishing`에 멈춤 | 교훈 | 중간 상태(`publishing`)를 두지 않는다. 본문 발행 직후 `threads_media_id`를 즉시 DB에 쓰고, 이 값을 멱등성 키로 쓴다 |
| — | 새로 | `publisher.py`: 컨테이너 생성 → 상태 확인 → 발행 → (link_in_reply면) 답글. 발행 직전 1장 규칙 재검사 |

### 2-7. AI 검수자·모니터링 (PLAN v2: 사람 승인 단계 없음)

| 출처 | 분류 | 이유 / 내용 |
|---|---|---|
| `api/admin/topics.ts` (Redis JSON 배열 큐), `components/admin/AdminDashboard.tsx` | 안 씀 | React + Vercel + Redis 스택 |
| 로옥션 `llm_factcheck` (문장별 claim/data 대조, JSON 판정) | 수정 | `reviewer.py`: R1~R12 항목별 pass/fail/uncertain. 생성과 다른 회사 모델(Claude) |
| 로옥션 `_selftest` (규칙이 실제로 잡히는지 스스로 점검) | 수정 | Step 4-C 검수자 시험지 (`tests/review_cases/`, `state/reviewer_exam.json`) |
| 티스토리 `scripts/send-result-email.ts` | 안 씀 | 알림은 텔레그램으로 결정 (`digest.py`) |
| — | 새로 | `digest.py` (일일 요약·자동 정지 알림), `monitor_app.py` (Streamlit, 선택), `cancel_post.yml` |

### 2-8. 스케줄러

| 출처 | 분류 | 새 파일 | 내용 |
|---|---|---|---|
| 티스토리 GitHub cron → Vercel API → repository_dispatch, 확률 발행 (`api/cron/auto-pilot.ts`) | 안 씀 | — | Vercel 경유가 필요 없고, 확률 발행은 "승인 + 예약" 모델과 맞지 않는다 |
| 로옥션 `.github/workflows/weekly_column.yml` | 수정 | `publish.yml`, `metrics.yml`, `weekly.yml` | `workflow_dispatch` + `dry_run` 입력, `vars.AUTO_PUBLISH_DISABLED`, 입력값 형식 검사, 실패 산출물 업로드 |
| 티스토리 `auto-publish.yml`의 `concurrency: cancel-in-progress: false` | 그대로 | `publish.yml` | 두 실행이 겹쳐 같은 글을 두 번 발행하는 것을 막는다 |

### 2-9. DB·알림·지표

| 출처 | 분류 | 새 파일 | 내용 |
|---|---|---|---|
| 티스토리 Upstash Redis (`api/_lib/redis.ts`) | 안 씀 | — | 큐 전체를 JSON 배열 하나에 저장하는 구조 |
| 로옥션 `supabase-py` `create_client` + service role key | 수정 | `store.py` | 환경변수 이름은 PLAN 기준(`SUPABASE_URL`). 로옥션은 `NEXT_PUBLIC_SUPABASE_URL`을 쓴다 |
| — | 새로 | `db/schema.sql`, `store.py`의 `MemoryStore` | PLAN 4장 테이블 3개. 테스트와 로컬 DRY_RUN은 네트워크 없는 메모리 저장소로 돌린다 |
| 티스토리 `scripts/send-result-email.ts` (Gmail) | 보류 | — | PLAN에 없음. 실패 알림이 필요해지면 Python `smtplib`로 옮긴다 |
| — | 새로 | `calculator.py` | 티스토리 앱에 전기요금 계산 코드는 없다 (지시문에 "월 350kWh를 쓰면 ~원" 예시 문장만 있음) |
| — | 새로 | `metrics.py`, `import_coupang.py`, `weekly_report.py` | 티스토리 앱에 해당 기능 없음 |

---

## 3. 새 프로젝트 파일 구성 (제안)

```
Threads_COUPAS/
├─ PLAN.md, REUSE_MAP.md, THREADS_API_NOTES.md
├─ .env.example            # 값 없는 키 목록 (DRY_RUN=true)
├─ config.yaml             # 발행 규칙·슬롯·금지어·대가성 문구·판정 기준
├─ requirements.txt
├─ data/products.csv, data/tariff.yaml      # 사람이 입력
├─ prompts/threads_base.md, tpl_calc.md, tpl_compare.md, tpl_tip.md
├─ db/schema.sql
├─ settings.py      # Step 1  .env·config 로드, 모델 제공사 분리, DRY_RUN, 킬 스위치, 토큰 만료 경고
├─ calculator.py    # Step 2
├─ products.py      # Step 3  products.csv 로더
├─ llm_client.py    # Step 3  생성용 (aiProviders.ts 이식)
├─ generator.py     # Step 3  훅 후보 3개, 훅 선택·교체, 재생성
├─ guard.py         # Step 4  (column_guard.py 숫자 검사 이식)
├─ reviewer.py      # Step 4-B  AI 검수자 (Claude)
├─ rules.py         # Step 4-B~6  슬롯 배정·발행 규칙 공용 (개수·간격·링크 비율)
├─ store.py         # Supabase / 메모리 저장소
├─ digest.py, monitor_app.py   # Step 5
├─ publisher.py     # Step 6
├─ metrics.py, watch_replies.py, import_coupang.py, weekly_report.py   # Step 8~9
├─ state/reviewer_exam.json    # Step 4-C 시험지 합격 기록
├─ tests/ (review_cases/ 포함)
└─ .github/workflows/generate.yml, publish.yml, digest.yml, metrics.yml, weekly.yml, cancel_post.yml   # Step 7
```

---

## 4. 1장 절대 규칙 → 코드 강제 지점

| # | 규칙 | 강제 위치 | 방식 |
|---|---|---|---|
| 1 | 계정 1개 | `settings.py` | `THREADS_USER_ID`는 단일 값만 허용(쉼표·공백 포함 시 로드 실패). 계정 목록·계정 선택 인자를 어떤 함수에도 두지 않는다 |
| 2 | 남의 이미지·영상·후기 금지 | `publisher.py`, `guard.py` | 컨테이너 요청은 `media_type=TEXT` 고정. 요청 생성 함수가 `image_url`·`video_url`·`children`·`gif_attachment` 등 텍스트 외 키를 받으면 예외. 가드는 후기 인용 표현("구매자", "리뷰에 따르면", "써보니")을 차단 |
| 3 | 대가성 문구 | `guard.py` + `publisher.py` | 본문이나 답글 어디든 링크(본문 URL 또는 `link_attachment`)가 있으면 본문 첫 줄 == `disclosure_text`가 아닐 때 예외. link_in_reply면 답글에도 문구가 없을 때 예외. 가드 통과 후에도 발행 직전에 다시 검사 |
| 4 | 승인 조건 | `generator.py` + `reviewer.py` + `publisher.py` | 코드 가드와 AI 검수자를 모두 통과한 글만 `approved`. 발행기는 `status != approved`면 예외, 발행 직전 가드 재실행 |
| 5 | 속도 제한 | `rules.py` | 하드캡 5는 코드 상수. `config.yaml`의 `max_posts_per_day`가 5를 넘으면 설정 로드 실패. 마지막 발행 후 2시간 미만이면 거부 |
| 6 | 타인 상호작용 금지 | `publisher.py` + 테스트 | 답글의 `reply_to_id`는 같은 글 행에 저장된 내 본문 `threads_media_id`만 허용. 멘션·검색·팔로우·남의 답글 관리 엔드포인트는 코드에 두지 않고, 소스에 해당 엔드포인트 문자열이 없는지 테스트로 검사 |
| 7 | 가격 표현 금지 | `guard.py` | 금지어(판매가, 최저가, 할인, 특가, 가격 등)를 `config.yaml`에서 관리. 본문 숫자는 `calc_payload`·입력 데이터 허용 집합에 있어야 통과 |
| 8 | 킬 스위치 | `publisher.py` 진입점 | 가장 먼저 `AUTO_PUBLISH_DISABLED`를 확인하고, 켜져 있으면 DB·API를 건드리지 않고 exit 0 |
| 9 | DRY_RUN 기본 true | `settings.py` + `publisher.py` | 변수가 없거나 해석할 수 없으면 true. `false`여도 `state/reviewer_exam.json`이 합격 + 해시 일치가 아니면 발행기가 거부 |
| 10 | 자동 정지 | `watch_replies.py`, `publisher.py`, `generator.py`, `reviewer.py` → `digest.py` 알림 | 답글 키워드, 발행 실패 2회 연속, 하루 held 50% 초과, 검수자 형식 오류 3회 연속, 시험지 케이스 추가 → 킬 스위치와 같은 정지 상태 |
| — | 모델 분리 | `settings.py` | 생성 모델(대체 포함)과 검수 모델의 제공사가 같으면 config 로드 실패 |

---

## 5. Step별 증명 계획 (8장 체크리스트)

| Step | 끝날 때 테스트로 증명할 8장 항목 (PLAN v2) | 상태 |
|---|---|---|
| 1 설정 | 토큰 만료 경고 / 생성·검수 모델 제공사가 같으면 실행 거부 | 통과 |
| 2 계산기 | 계산기 단위 테스트 3케이스 | 통과 |
| 3 생성기 | 훅 후보 3개 저장 / 훅 선택이 최근 7일 최소 사용 유형을 따름 | 통과 |
| 4 가드 | 문구 누락·금지어·없는 숫자·501자·이모지·URL 2개 차단 / 숫자 없는 훅·31자 훅·금지 훅 문구 차단 / 실패 시 다른 훅 후보로 먼저 교체 | 통과 |
| 4-B·4-C 검수자 | 시험지 20/20 차단·정상 8/10 통과 / 모델·프롬프트·케이스 변경 시 불합격 전환 / 형식 오류 → 재요청 후 held / uncertain → held + 일일 요약 표시 | 코드 통과 (변경 시 불합격 전환·형식 오류·uncertain). **시험지 채점은 실제 Claude 로 `python exam.py` 실행 필요.** "일일 요약 표시"는 Step 5 에서 증명 |
| 5 요약·모니터링 | 일일 요약 발송 + `cancel_post`로 예정 글 취소 (+ 4-B의 uncertain 요약 표시) | 통과 (가짜 알림기·메모리 저장소). 실제 텔레그램·Supabase 연결은 Step 7 dry_run 실행 때 확인 |
| 6 발행기 | 미승인 거부 / 시험지 불합격 거부 / 개수·간격 초과 거부 / 킬 스위치 / 링크 확인 실패 → held / DRY_RUN 로그 / 두 번째 발행 건너뜀 | 통과 (가짜 API). 자동 정지 조건 2(발행 실패 2회)·5(시험지 케이스 추가)도 발행 중단까지 통과 |
| 7 스케줄러 | (8장 항목 없음) generate·publish·digest·cancel_post 워크플로, 모든 워크플로 dry_run 입력 | 통과. GitHub Actions 에서 digest 실제 실행 성공 (Supabase 읽기·쓰기, 텔레그램). metrics·weekly 워크플로는 Step 8·9 |
| 8 지표 | 자동 정지 5개 조건 / 실제 1건 발행 후 media_id 저장·24시간 뒤 인사이트 | 조건 2~5 통과, 조건 1(답글) 은 Step 8 |
| 9 주간 리포트 | 훅 유형별 중앙값·검수 통계 / 규칙 위반 제안 필터 / 10개 미만이면 "데이터 부족" | |

- **"실제 1건 발행" 항목은 사용자가 DRY_RUN을 직접 false로 바꾼 뒤에만 증명할 수 있다.** 그 전까지는 가짜 API 응답으로 저장·수집 로직만 테스트한다.

---

## 6. 기본값으로 정한 것 (바꾸려면 말씀해 주세요)

1. **프로젝트 루트는 `Threads_COUPAS/` 자체**로 한다. PLAN의 `threads_test/` 하위 폴더는 따로 만들지 않는다. 이미 티스토리 앱과 분리된 새 폴더다.
2. **숫자 0~12 무조건 허용을 없앤다** (2-5 참고). 가드가 더 엄격해지는 대신 재생성이 조금 늘 수 있다.
3. **"하루"는 KST 달력일**로 센다. 슬롯 배정과 같은 기준이다.
4. **link_in_reply 답글은 본문 게시물의 일부**로 보고 일일 개수에 따로 세지 않는다.
5. **link_in_body의 링크는 `link_attachment`로만 붙이고 본문 텍스트에는 URL을 넣지 않는다.** 미리보기 카드가 뜨고 링크 수는 1개로 셀 수 있다. 가드는 `link_attachment`도 링크로 센다.
6. **슬롯과 cron 시각이 어긋난다.** 슬롯은 08:10·12:30·18:40·21:30인데 `publish.yml`은 매시 5분에 돈다. 그래서 실제 발행은 09:05·13:05·19:05·22:05 전후가 된다. Step 5·7에서 슬롯을 정시 직전으로 옮길지 정한다.

## 7. 결정 사항

1. **생성 LLM — 결정 (2026-09-27): Gemini 기본 + OpenAI 대체.** 티스토리 앱과 같은 구조이고 기존 `GEMINI_API_KEY`를 쓴다. `OPENAI_API_KEY`는 선택.
2. **Supabase — 결정 (2026-09-27): 새 프로젝트.** 로옥션 DB와 섞지 않는다.
3. **검수 LLM — 결정 (2026-09-27, PLAN v2): Claude (`claude-opus-5`).** 생성(Google)·대체(OpenAI)와 회사가 다르다. Opus 5는 temperature 파라미터를 받지 않으므로(보내면 400), PLAN의 "temperature 0" 대신 JSON 스키마 출력과 고정 프롬프트로 판정을 일정하게 한다 (Step 4-B).
4. **알림 — 결정 (2026-09-27): 텔레그램.**
5. **가정 문구 — 결정: "기본요금·감면 제외 추정".** 금지어 "할인"과 겹치지 않게 한다. PLAN v2 본문도 고쳤다.
6. **대가성 문구·훅·링크는 코드가 조립한다.** LLM은 `hook_candidates`(3개)·`body`·`reply_line`·`topic_tag`만 JSON으로 준다. 훅을 다른 후보로 바꿔 끼우려면 훅과 본문이 분리돼 있어야 하기 때문이다. DB에는 조립된 `text`·`reply_text`를 저장한다. `posts`에는 PLAN 표에 없는 `topic_tag` 칸을 추가한다.
7. **GitHub 저장소 (Step 7 전까지, 미정)**: GitHub Actions를 쓰려면 이 폴더용 비공개 저장소가 필요하다. 지금 이 폴더는 git 저장소가 아니다.
