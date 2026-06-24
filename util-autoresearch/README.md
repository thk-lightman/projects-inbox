# util-autoresearch

매주 자동으로 외부 정보를 vault에 수집하는 두 계층 파이프라인. **paper layer**는 PI·저널 단위로 OpenAlex + Semantic Scholar로 논문을 fetch, **dev layer**는 토픽 단위로 last30days-skill을 invoke해서 Reddit/HN/X/YouTube/TikTok/Polymarket/GitHub 시그널을 끌어온다. 결과는 모두 같은 vault inbox에 떨어져 `/learn-paper`·material-absorb·Zotero 같은 후속 흐름이 일관 처리.

```
[월 09:00 launchd]
   ↓
[bash run_autoFetcher.sh]   (오케스트레이터: 두 fetcher 독립 실행, failure isolation)
   │
   ├─ run_paper.sh — paper layer (Docker)
   │     docker compose run --rm app
   │     → fetch_papers.py (PI/venue + topics[classic/recent/keyword] → OpenAlex/S2)
   │       → raws/paper-W<주>-<title>.md + (zotero push) + 01Inbox-paper.md
   │       + briefings/paper-W<주>-<bucket>.md
   │
   └─ run_dev.sh — dev layer (host)
         python3 fetch_dev.py
         → claude -p "/last30days <topic>" per active topic
         → dev-W<주>-<topic>.md + dev-W<주>-briefing.md
```

## 1. 내가 관리하는 파일 (TLDR)

| 파일 | 누가 손댐 | 빈도 | 의미 |
|---|---|---|---|
| `vault/00 GTD/03Inbox/auto-research/docs/docs-watchlist-labs.md` | **너** | 시작 + 가끔 | 추적할 PI 표 (paper layer 입력) |
| `vault/00 GTD/03Inbox/auto-research/docs/docs-watchlist-journals.md` | **너** | 시작 + 가끔 | 추적할 저널·컨퍼런스 표 (paper layer 입력) |
| `vault/00 GTD/03Inbox/auto-research/docs/docs-watchlist-paper-topics.md` | **너** | 시작 + 가끔 | 추적할 paper 토픽 표 (주제·전략 classic/recent/keyword) |
| `vault/00 GTD/03Inbox/auto-research/docs/docs-watchlist-topics.md` | **너** | 시작 + 가끔 | 추적할 dev 토픽 표 (dev layer 입력) |
| `vault/00 GTD/03Inbox/auto-research/raws/paper-W*-*.md` | 스크립트 생성 → **너 검토** | 매주 | paper layer 산출물 |
| `vault/00 GTD/03Inbox/auto-research/raws/dev-W*-*.md` | 스크립트 생성 → **너 검토** | 매주 | dev layer 산출물 (토픽별 + briefing) |
| `vault/00 GTD/03Inbox/01Inbox-paper.md` | 스크립트 append → paper-absorb 입력 | 매주 | fetch된 논문 링크리스트 (zotero key 포함) |
| `vault/00 GTD/03Inbox/auto-research/briefings/paper-W*-*.md` | 스크립트 생성 → **너 검토** | 매주 | paper bucket별 템플릿 브리핑 |
| `~/GIT/project-mori/util-autoresearch/` | **너** (git), Claude (코드 변경) | 드물게 | application repo SSOT |

자동 생성·관리 (너 손 X):
- `~/.cache/autoresearch/dedup.sqlite` — 양 layer 공용 dedup. 컬럼 `source_kind` (paper|article|video|social) + `canonical_id` prefix 로 종류 분리.
- `~/.claude/autoresearch/_launchd.{out,err}` — launchd 로그
- `~/Library/LaunchAgents/com.mori.autoresearch.plist` — wrapper 호출 (v1.0.0 이후 직접 docker 호출 안 함)

## 2. 초기 세팅 (한 번만)

### 2-1. Docker 이미지 빌드 (paper layer)
```bash
cd ~/GIT/project-mori/util-autoresearch
docker compose build
```
**영향**: 이미지 `util-autoresearch:latest` 생성 (python:3.12-slim 기반). paper layer만 컨테이너 사용.

### 2-2. last30days-skill 설치 (dev layer)
Claude Code 안에서:
```
/plugin marketplace add mvanhorn/last30days-skill
/plugin install last30days
```
**영향**: Claude Code 슬래시 명령 `/last30days <topic>` 사용 가능. dev layer는 `claude -p --permission-mode bypassPermissions "/last30days ..."` 형태로 cron 안에서 호출.

### 2-3. dedup state 디렉토리
`~/.cache/autoresearch/dedup.sqlite`가 없으면 첫 실행 시 자동 생성. 기존 paper-only 스키마는 2026-06-10에 `source_kind` 컬럼 추가로 마이그레이션 (`ALTER TABLE seen ADD COLUMN source_kind TEXT NOT NULL DEFAULT 'paper'`).

### 2-4. launchd plist 등록
plist는 host `~/Library/LaunchAgents/com.mori.autoresearch.plist`. `ProgramArguments`가 `bash run_autoFetcher.sh`를 호출. dotfiles SSOT 이전은 v2.3 작업 (v2-roadmap 참조).

### 2-5. watchlist 첫 채움
네 표(labs/journals/paper-topics/dev topics) vault에서 직접 행 추가. 각 표 상단 "표 작성 가이드" 참고.

### 2-6. 환경변수 · Zotero (.env)
모든 vault 경로는 env-driven: `VAULT_ROOT` + per-file `AR_PAPER_*` / `AR_DEV_*` `_REL` (vault 폴더 이동 시 코드 수정 0). 전체 키는 `.env.example` 참고 → `.env`(gitignored)로 복사.

Zotero 자동 저장 켜려면 `.env`에:
```
ZOTERO_API_KEY=<zotero.org/settings/keys 발급, library read+write>
ZOTERO_USER_ID=<같은 페이지 숫자 ID>
ZOTERO_COLLECTION=<8자 키>   # 선택, 분류용
OPENALEX_EMAIL=<이메일>       # Unpaywall OA PDF 조회 contact
```
`run_paper.sh`가 `--env-file .env`로 컨테이너에 전달 (launchd CWD=/ 대응). 기존 backlog 일괄 저장: `docker compose --env-file .env run --rm zotero --backfill "<raws 상대경로>"`.

## 3. Entry point (운용 시 실행되는 명령)

### 3-1. 자동: launchd 주간 cron
- 매주 월요일 09:00 발동
- `bash run_autoFetcher.sh` 호출 → run_paper.sh(Docker) + run_dev.sh(host) 순차
- 한 layer 실패해도 다른 layer는 진행 (failure isolation)

### 3-2. 수동: 즉시 fetch (전체)
```bash
bash ~/GIT/project-mori/util-autoresearch/run_autoFetcher.sh
```

### 3-3. 수동: 단일 layer
```bash
# paper만
bash ~/GIT/project-mori/util-autoresearch/run_paper.sh

# dev만
bash ~/GIT/project-mori/util-autoresearch/run_dev.sh
```

### 3-4. Dry-run
```bash
docker compose run --rm app --dry-run     # paper
python3 fetch_dev.py --dry-run             # dev
```

### 3-5. 테스트
```bash
docker compose run --rm test    # 24 pytest
```

### 3-6. 임의 watchlist·inbox로 실험
```bash
python3 fetch_dev.py \
  --watchlist-topics /tmp/my-topics.md \
  --inbox-dir /tmp/sandbox-inbox
```

## 4. 참조 파일 (스크립트가 읽는 파일)

### 4-1. paper layer
- `vault/00 GTD/03Inbox/auto-research/docs/docs-watchlist-labs.md` — PI 표
- `vault/00 GTD/03Inbox/auto-research/docs/docs-watchlist-journals.md` — venue 표
- 외부 API: OpenAlex (이메일 등록 polite pool, 10/s) + Semantic Scholar (≤100/5min unauthed)

### 4-2. dev layer
- `vault/00 GTD/03Inbox/auto-research/docs/docs-watchlist-topics.md` — 토픽 표
- Claude Code CLI (`claude -p`) → 설치된 last30days-skill plugin
- last30days가 내부적으로 호출: Reddit RSS, HN Algolia, YouTube (yt-dlp), GitHub API, Polymarket API, WebSearch

### 4-3. 공용
- `~/.cache/autoresearch/dedup.sqlite` — 양 layer canonical_id 누적

## 5. 산출물

### 5-1. paper layer
입력: PI/저널 표 + **주제 표**(`docs-watchlist-paper-topics.md`, 전략 classic/recent/keyword).
- `vault/00 GTD/03Inbox/auto-research/raws/paper-W<주>-<title-slug>.md` — 같은 slug 충돌 시 canonical 접미사로 disambiguate
  - frontmatter: `source: arxiv-paper`, `canonical_id`, `title`, `authors`, `venue`, `doi`, `arxiv_id`, `citation_count`, `pdf_url`(OA시), `status_file: False`
- `vault/00 GTD/03Inbox/01Inbox-paper.md` — fetch된 논문 링크리스트 (paper-absorb 입력). 형식: `- YYYYMMDD - [auto-research-paper/<bucket>] <title> <url> (zotero:<KEY>)`
- `vault/00 GTD/03Inbox/auto-research/briefings/paper-W<주>-<bucket>.md` — bucket(topic/pi/venue)당 1개 템플릿 브리핑 (인용 순)
- **Zotero item** — 수집 시 자동 push (creds 있을 때). OA PDF는 arXiv → 캡처된 pdf_url → Unpaywall(DOI) 순으로 첨부. creds 없으면 graceful skip

### 5-2. dev layer
- `vault/00 GTD/03Inbox/auto-research/briefings/dev-W<주>-<topic-slug>.md` — 토픽당 1개, last30days 종합 (한국어 요약 + 영문 본문)
- `vault/00 GTD/03Inbox/01Inbox-scrap.md` 에 인용 URL append (형식: `- YYYYMMDD - [auto-research-dev/<topic>] <title> <URL>`). 이후 `/material-absorb`가 처리
- frontmatter: `source: last30days`, `source_kind: article`, `track: dev`, `topic`, `week`, `fetched_at`

### 5-3. 공용
- `~/.cache/autoresearch/dedup.sqlite` 누적 row
- `~/.claude/autoresearch/_launchd.{out,err}` 로그

## 6. 디버깅

증상별 처음 볼 곳:
- paper-*.md 안 생김 → `docker compose run --rm app --dry-run` + `~/.claude/autoresearch/_launchd.err`
- dev-*.md 안 생김 → `python3 fetch_dev.py --dry-run` + `claude -p "/last30days test"` 단독 호출 시 작동 여부
- 같은 논문 중복 → dedup.sqlite의 `source_kind`·`canonical_id` 직접 SELECT
- 권한 prompt → cron은 plist 통해 호출되므로 `--permission-mode bypassPermissions` 자동 적용 (인터랙티브 실행 시는 직접 승인)

상세 운용 매뉴얼: `vault/01 CC/prod-autoresearch/docs-handoff.md`

## 7. 관련 문서

- `vault/01 CC/prod-autoresearch/prod-autoresearch.md` — 프로젝트 MOC
- `vault/01 CC/prod-autoresearch/docs-handoff.md` — 운용 매뉴얼
- `vault/01 CC/prod-autoresearch/docs-v2-roadmap.md` — 향후 작업 (paper enrichment, concept gap-fill, dotfiles 정합 등)
- `vault/00 GTD/03Inbox/auto-research/docs/docs-watchlist-{labs,journals,topics}.md` — 입력 SSOT

## 8. Watchlist 설계 원칙

### 8-1. paper layer — 왜 PI/저널 큐레이션

arXiv 신규 카테고리를 통째 fetch하면 하루 수백 건의 noise가 도착. 신호는 없다. 목표가 "유명하고 인용 많은 논문"이라면 **PI 단위 큐레이션 + citation 임계값**이 정답. labs는 "사람" 중심, journals는 "분야 게이트키퍼" 중심으로 상호보완.

### 8-2. API 2개 (OpenAlex + Semantic Scholar)

한 API가 모든 논문·venue를 다 갖지 않는다. 특히 conference는 API 간 coverage 차이가 큼.
- **OpenAlex** — 광범위, 무료, key 없음. 이메일 등록 시 polite pool (10/s).
- **Semantic Scholar** — ML/CS/통계 강함. 무료, key 없음 (≤100/5min unauthed). key 받으면 rate-limit 완화.

두 API 결합 + dedup으로 coverage 최대화.

### 8-3. Dedup cascade

같은 논문이 OpenAlex/S2 양쪽에서 또는 labs/journals 양쪽에서 동시에 잡힐 수 있음. dedup 키 우선순위:

1. **DOI** — 양쪽 API ≥95% 채워 있음
2. **arXiv id** — DOI 없는 preprint 대비
3. **title + first-author normalized hash** — 둘 다 없을 때 최후

`canonical_id` 1개로 sqlite `seen` 테이블에 저장. 양 layer + 양 API 모두 같은 dedup 테이블 공유. dev layer 행은 `source_kind='article'`로 paper와 분리 보존.

### 8-4. Author 위치 필터 (paper 만)

PI 추적 의미 = 누구의 어떤 위치가 신호인가.
- 통계·전통과학: PI = **last author** (supervisor 컨벤션)
- ML·CS 일부: **first author**도 의미 큼
- 디폴트는 **any** — noise 많아지면 PI별로 좁힘

값: `last` | `first` | `any` | `first_or_last`.

### 8-5. Citation 임계값 + lookback

- 디폴트: **지난 12개월 + citation ≥ 20**
- 운용하며 조정. 0건이면 임계값 낮춤, noise 많으면 높임.
- PI/venue별 override는 `citation_min` 컬럼에 명시 (예: NeurIPS는 50, niche venue는 10).
- 발행 주기 긴 저널(JRSSB 등)은 `lookback_months` 컬럼으로 18~24까지 늘림.

### 8-6. dev layer — 왜 topic-driven

RSS·channel 단위 source-driven 수집은 어떤 토픽이 어디서 터지는지 모름. dev 동향은 multi-source 동시 등장(Reddit + HN + YouTube + X 같은 얘기)이 진짜 신호. last30days-skill이 토픽 1개로 다중 소스 cluster-merge + engagement 스코어링까지 처리.

### 8-7. dev layer — engagement-weighted vs recency

recency·random 픽은 야크 쉐이빙 위험. upvote/like/views/real-money(Polymarket) = 커뮤니티 검증 신호. citation 임계값(paper) ≈ engagement 임계값(dev). 후자는 last30days가 내장.

## 9. 표 작성 가이드

### 9-1. OpenAlex authorId

```
https://api.openalex.org/authors?search=<PI 이름>
```
응답 `results[0].id` = `https://openalex.org/A5009543412` → id 부분 `A5009543412`만 표에 입력. 동명이인 있으면 affiliation·works 확인 후 정확한 사람 고름.

### 9-2. Semantic Scholar authorId

```
https://api.semanticscholar.org/graph/v1/author/search?query=<PI 이름>
```
응답 `data[0].authorId`를 그대로 입력.

### 9-3. OpenAlex source id (venue)

```
https://api.openalex.org/sources?search=Journal+of+the+American+Statistical+Association
```
응답 `results[0].id` = `https://openalex.org/S148538149` → `S148538149`. 이름 모호하면 issn·publisher로 비교.

### 9-4. Semantic Scholar venue 이름

S2는 venue를 표준 string으로 받음. 공식 venue 이름을 그대로 적으면 됨 (예: `Journal of the American Statistical Association`, `NeurIPS`, `ICML`).

### 9-5. citation_min 운용 가이드

- 비워두면 글로벌 디폴트 20
- top conference·journal은 높게 (50+), 신생·niche venue는 낮게 (10)
- 처음 행 추가 시 비워두고 첫 주 결과 보고 조정

### 9-6. lookback_months 운용 가이드 (venue 만)

- 비워두면 디폴트 12
- 발행 주기 긴 저널(JRSSB, Annals of Statistics 등)은 18~24

### 9-7. dev topic 정하는 가이드

- 카테고리 키워드 우선 (예: `agentic coding harness`). 단일 도구 이름(`Cursor IDE`)은 hit 적음.
- 너무 넓으면 noise. `AI`처럼 메타는 X. 1-2 단어로 구체.
- 명사구 (예: `Claude Code skills`, `MCP servers`). 의문문 X.
- 고유명사 1개 (예: `Ouroboros`)도 OK — last30days 엔진이 자동 확장.

### 9-8. 추가·삭제 절차 (3 표 공통)

1. 새 행 추가 → 저장 → 다음 cron 자동 반영
2. 첫 주 결과 보고 임계값 조정
3. 삭제는 행 제거. dedup.sqlite 누적분은 보존 (재추가 시 즉시 dedup)
4. 별도 재시작·동기화 명령 불필요
