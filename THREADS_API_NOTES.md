# Threads API 확인 노트

> 2026-09-27에 Meta 공식 문서에서 확인했다. Step 6(발행기)·Step 8(지표) 코드는 이 값을 기준으로 쓴다.
> 발행 전에 문서가 바뀌었을 수 있으면 아래 출처를 다시 확인한다.

## 기본

- 기본 URL: `https://graph.threads.net/v1.0`
- 권한: `threads_basic`, `threads_content_publish`, `threads_manage_replies`, `threads_read_replies`, `threads_manage_insights` (PLAN v2 2-1)
  - `threads_read_replies`는 PLAN v2의 답글 감시(자동 정지)용이다. 답글에 자동으로 대응하는 기능은 만들지 않는다 (규칙 6).

## 답글 조회 (Step 8, 2026-09-27 확인)

- `GET /{media-id}/replies`: 바로 아래 답글만. `GET /{media-id}/conversation`: 깊이와 상관없이 모든 답글 (루트 글에만 사용)
- 이 프로젝트는 `conversation` 을 쓴다: `fields=id,text,username,permalink,timestamp,is_reply_owned_by_me` (permalink 은 2026-10-16 추가, 공식 문서에서 conversation 필드로 확인. 일일 요약의 "답해 줄 답글" 바로가기용)
- `reverse` 기본값 true (최신순). 응답은 `data` 목록 + `paging`
- 내가 단 링크 답글(`is_reply_owned_by_me=true`)은 감시 대상에서 뺀다
- 출처: [Replies and Conversations](https://developers.facebook.com/docs/threads/retrieve-and-manage-replies/replies-and-conversations)

## 발행 (Step 6)

### 1) 컨테이너 생성 — `POST /{threads-user-id}/threads`

이 프로젝트에서 쓰는 파라미터만 적는다.

| 파라미터 | 값 | 비고 |
|---|---|---|
| `media_type` | `TEXT`, `IMAGE`, `VIDEO`, `CAROUSEL` | 이미지·영상은 `allow_media` 계정의 자기 앱 캡처·시연 영상만 (규칙 2, 아래 "이미지·영상") |
| `text` | 본문 | 텍스트 글은 필수. 500자 제한. **이모지는 UTF-8 바이트 수로 셈** |
| `link_attachment` | 쿠팡 간편링크 | **텍스트 글에서만** 동작(이미지·영상·캐러셀 불가). 미리보기 카드로 표시. 본문 URL과 같은 주소면 링크 1개로 셈, 다르면 추가로 셈 |
| `reply_to_id` | 내 본문 미디어 ID | 답글일 때만. 루트 글 작성자는 자기 글에 답글을 달 수 있음 |
| `topic_tag` | 선택 | 1~50자, 마침표(.)·앰퍼샌드(&) 금지, 글당 1개. 본문에 해시태그가 있으면 첫 번째 유효 태그가 태그로 잡힘 |

쓰지 않는 파라미터: `alt_text`, `quote_post_id`, `poll_attachment`, `gif_attachment`, `auto_publish_text` 등.

### 이미지·영상 (2026-10-30 Meta 문서 확인, `media.py`)

| 경우 | 요청 |
|---|---|
| 1개 | `media_type=IMAGE`+`image_url` 또는 `media_type=VIDEO`+`video_url`, `text`(선택이지만 이 프로젝트는 항상 넣음), `topic_tag` |
| 2~20개 | 항목마다 `media_type=IMAGE|VIDEO`, `image_url|video_url`, `is_carousel_item=true` 컨테이너 → 모두 `FINISHED` 확인 → `media_type=CAROUSEL`, `children`(쉼표로 이은 컨테이너 ID), `text` → 캐러셀 컨테이너만 발행. 이미지·영상 섞어도 됨 |

- Threads 가 공개 주소에서 파일을 받아 간다. 발행기가 발행 직전에 Supabase Storage 공개 버킷 `post-images` 에 `<계정>/<내용 해시>.<확장자>` 로 올린다 (버킷이 없으면 만든다. 2026-10-30 실제 업로드·공개 주소 받기 확인).
- 이미지 조건: JPEG·PNG, 8MB 이하, 가로 320~1440px 밖이면 Threads 가 늘리거나 줄임, 비율 10:1 이하, sRGB 로 변환됨.
- 영상 조건: MP4·MOV(moov 앞쪽), H.264·HEVC, 4:2:0, 23~60fps, 가로 1920px 이하, 비율 0.01:1~10:1(9:16 권장), 5분·1GB 이하, 소리는 AAC(없어도 됨). 이 프로젝트 영상(`capture.py`)은 780x1688 H.264 yuv420p 30fps faststart, 소리 없음, 30초 이하. Supabase 무료 한도 때문에 50MB 이하만 받는다.
- 영상은 처리가 오래 걸려 컨테이너 상태를 최대 30번(5분 반) 확인한다 (`VIDEO_MAX_POLLS`). 이미지는 6번.
- **`link_attachment` 는 텍스트 글에서만 동작한다.** 이미지·영상 글의 링크는 답글(`link_in_reply`)에 둔다 (가드 G9).
- 답글은 계속 `TEXT` 만 쓴다.
- 실제 영상 발행은 아직 확인하지 못했다 (openbrain 발행이 멈춰 있음). 첫 영상 글에서 처리 시간과 재생을 확인한다.
`auto_publish_text`는 컨테이너 생성과 발행을 한 번에 하는 옵션이다. 상태 확인 → 발행 2단계를 PLAN대로 유지하려고 쓰지 않는다.

### 2) 상태 확인 — `GET /{container-id}?fields=status,error_message`

| status | 뜻 | 발행기 처리 |
|---|---|---|
| `FINISHED` | 발행 가능 | 발행 진행 |
| `IN_PROGRESS` | 처리 중 | 대기 후 재확인 |
| `PUBLISHED` | 이미 발행됨 | 발행하지 않음 (중복 방지) |
| `ERROR` | 실패 | `status=failed` + 로그 |
| `EXPIRED` | 24시간 안에 발행 안 돼 만료 | `status=failed` + 로그 |

- 문서 권장: 컨테이너 생성 후 **평균 30초 기다렸다가** 발행.

### 3) 발행 — `POST /{threads-user-id}/threads_publish`

- `creation_id` = 컨테이너 ID → 응답의 `id`가 미디어 ID (`threads_media_id`로 저장)

### 한도 (24시간)

| 항목 | 한도 |
|---|---|
| 게시 | 250 |
| 답글 | 1,000 |
| 링크 | 글당 고유 링크 최대 5개. 2025-12-22부터 5개 초과 글은 발행 실패 |

- 현재 사용량은 `GET /{threads-user-id}/threads_publishing_limit`로 확인할 수 있다 (`quota_usage`, `config`, `reply_quota_usage`, `reply_config`).
- 자체 한도(하루 5개, 간격 2시간)가 훨씬 낮아 API 한도에 닿을 일은 없다.

## 인사이트 (Step 8)

### 게시물 단위 — `GET /{threads-media-id}/insights`

metric: `views`, `likes`, `replies`, `reposts`, `quotes`, `shares` → **PLAN 4장 `metrics_post` 컬럼과 일치**

주의:
- 문서에 "`views`와 `shares`는 개발 중"이라고 적혀 있다. 값이 비어 오면 0이 아니라 null로 저장한다.
- 중첩 답글의 지표는 포함되지 않는다.
- **게시물 단위 링크 클릭 metric은 없다.**

### 계정 단위 — `GET /{threads-user-id}/threads_insights`

metric: `views`, `likes`, `replies`, `reposts`, `quotes`, `clicks`, `followers_count`, `follower_demographics`

- `clicks`(계정 단위 링크 클릭)는 주간 리포트에서 쿠팡 클릭 수와 비교할 보조 지표로 쓸 수 있다. PLAN에는 없어 **선택 사항**이다.
- `follower_demographics`는 팔로워 100명 이상부터 조회된다.

## 토큰 (Step 1)

| 항목 | 값 |
|---|---|
| 단기 → 장기 교환 | `GET https://graph.threads.net/access_token?grant_type=th_exchange_token&client_secret=...&access_token=...` |
| 장기 토큰 유효기간 | 60일 |
| 갱신 | `GET https://graph.threads.net/refresh_access_token?grant_type=th_refresh_token&access_token=...`. 발급 후 24시간 이상, 만료 전이어야 함. 갱신하면 그 시점부터 60일 |
| 응답 필드 | `access_token`, `token_type`, `expires_in`(초) |

- PLAN대로 테스트 기간(4주)에는 자동 갱신을 만들지 않고, 만료 10일 전부터 경고만 낸다.
- `expires_in`이 응답에 있으므로 `THREADS_TOKEN_ISSUED_AT` + 60일 대신 실제 만료일을 기록해 둘 수도 있다.

## PLAN과 대조

| PLAN 항목 | 문서 확인 결과 |
|---|---|
| 링크 첨부 파라미터 `link_attachment` | 맞음. 텍스트 글 전용 |
| `reply_to_id` | 맞음 |
| `topic_tag` | 맞음. 1~50자, `.`·`&` 금지, 글당 1개 |
| 인사이트 metric 6종 | 맞음. `views`·`shares`는 "개발 중" 표기 |
| API 한도 24시간 250개 | 맞음 |
| 5개 초과 링크 시 발행 실패 | 맞음 (고유 링크 기준, 2025-12-22부터) |
| 이모지 글자 수 문제 | 맞음 (UTF-8 바이트로 셈) |
| 장기 토큰 60일, `th_exchange_token` | 맞음 |

## 문서로 확인하지 못한 것 (DRY_RUN 해제 후 첫 실제 발행에서 확인)

- `link_attachment` URL이 500자 제한에 포함되는지. 본문 텍스트에 URL을 넣지 않으므로 영향은 작다.
- 답글에 `link_attachment`를 붙였을 때 미리보기 카드가 표시되는지.

## 출처

- [Publishing Reference](https://developers.facebook.com/docs/threads/reference/publishing)
- [Posts](https://developers.facebook.com/docs/threads/posts) (이미지·캐러셀 2026-10-30 확인)
- [Insights](https://developers.facebook.com/docs/threads/insights)
- [Troubleshooting (한도·컨테이너 상태)](https://developers.facebook.com/docs/threads/troubleshooting)
- [Long-Lived Access Tokens](https://developers.facebook.com/docs/threads/get-started/long-lived-tokens)
- [Create Replies](https://developers.facebook.com/docs/threads/retrieve-and-manage-replies/create-replies)
- [Reply Management](https://developers.facebook.com/docs/threads/reply-management/)
