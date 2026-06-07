# util-autoresearch

랩(PI)·저널 단위로 매주 자동 fetch하는 격리 파이프라인. vault의 watchlist 표를 읽어 OpenAlex + Semantic Scholar API를 호출하고, 인용 많은 신규 논문을 vault inbox에 떨어뜨린다.

```
[월 09:00 launchd]
   ↓
[docker compose run --rm app]
   ↓
container 안 fetch_papers.py
   ├─ 읽기: vault/01 CC/prod-autoresearch/docs-watchlist-{labs,journals}.md
   ├─ 호출: OpenAlex + Semantic Scholar API
   ├─ dedup: ~/.cache/autoresearch/dedup.sqlite (DOI → arxiv → hash cascade)
   └─ 쓰기: vault/00 GTD/03Inbox/auto/paper-<canonical-id>.md
   ↓
[너 검토 → /learn-paper 또는 03 Resources/ 수동 이동]
```

---

## 1. 내가 관리해야 하는 파일 (TLDR)

매일·매주 손대는 모든 파일 한눈에:

| 파일 | 누가 손댐 | 빈도 | 의미 |
|---|---|---|---|
| `vault/01 CC/prod-autoresearch/docs-watchlist-labs.md` | **너** | 시작 + 가끔 | 추적할 PI 표 |
| `vault/01 CC/prod-autoresearch/docs-watchlist-journals.md` | **너** | 시작 + 가끔 | 추적할 저널 표 |
| `vault/00 GTD/03Inbox/auto/paper-*.md` | 스크립트 생성 → **너 검토·삭제·이동** | 매주 | 자동 fetch된 paper |
| `~/GIT/project-mori/util-autoresearch/` | **너** (git push), Claude (코드 변경) | 드물게 | application repo |
| `~/GIT/dotfiles/launchd/agents/claude/com.mori.autoresearch.plist.template` | **너** (스케줄 변경 시) | 드물게 | 주간 cron 설정 |

자동 생성·관리되는 파일 (너 손 X):
- `~/.cache/autoresearch/dedup.sqlite` — dedup 상태
- `~/.claude/autoresearch/_launchd.{out,err}` — launchd 로그
- `~/Library/LaunchAgents/com.mori.autoresearch.plist` — install_agents.sh가 sed 치환 후 복사

---

## 2. 초기 세팅 (한 번만)

각 단계 + 해당 단계가 만드는 영향(=무엇이 생기고, 어떤 행동이 가능해지는지):

### 2-1. Docker 이미지 빌드
```bash
cd ~/GIT/project-mori/util-autoresearch
docker compose build
```
**영향:**
- Docker 이미지 `util-autoresearch:latest` 1개 생성 (~150MB, python:3.12-slim 기반)
- 호스트 pyenv·pip 미오염
- 이후 `docker compose run --rm app` 즉시 실행 가능

### 2-2. dedup state 디렉토리
```bash
mkdir -p ~/.cache/autoresearch
```
**영향:**
- 첫 실행 시 `~/.cache/autoresearch/dedup.sqlite` 자동 생성됨
- 컨테이너가 이 디렉토리를 `/cache`에 마운트해 sqlite 영구화

### 2-3. launchd plist 등록
```bash
cd ~/GIT/dotfiles/launchd && bash install_agents.sh
```
**영향:**
- `dotfiles/launchd/agents/claude/com.mori.autoresearch.plist.template`에서 `{{HOME}}`·`{{VAULT_ROOT}}` 치환 → `~/Library/LaunchAgents/com.mori.autoresearch.plist` 복사
- `launchctl load` 호출 → 매주 월 09:00 자동 실행 활성화
- 다음 월요일부터 인간 개입 없이 작동

### 2-4. watchlist 첫 채움 (Obsidian에서 표 편집)
- `vault/01 CC/prod-autoresearch/docs-watchlist-labs.md` 표에 PI 5-10명 행 추가
- `docs-watchlist-journals.md` 표에 저널 5-10개 행 추가

**영향:**
- 다음 fetch 실행 시 이 목록의 PI/저널 대상으로 API 호출
- 임계값(`citation_min`)·필터(`위치 필터`) 행마다 명시 가능

---

## 3. Entry point (운용 시 실행되는 명령)

### 3-1. 자동: launchd 주간 cron
**명령:** `docker compose run --rm app` (launchd가 자동 호출)
**시기:** 매주 월요일 09:00
**영향:**
- watchlist 표 읽음
- OpenAlex + Semantic Scholar API 호출
- 신규 paper 발견 시 `vault/00 GTD/03Inbox/auto/paper-<canonical-id>.md` 생성
- sqlite dedup 갱신
- stdout → `~/.claude/autoresearch/_launchd.out`
- stderr → `~/.claude/autoresearch/_launchd.err`
- 컨테이너 종료 후 자동 폐기 (`--rm`)

### 3-2. 수동: 즉시 fetch
```bash
cd ~/GIT/project-mori/util-autoresearch
docker compose run --rm app
```
**영향:** 자동 cron과 동일. 임의 시점 너가 강제 trigger.

### 3-3. Dry-run: 파일 생성 없이 시뮬레이션
```bash
docker compose run --rm app --dry-run
```
**영향:**
- API 호출 정상 (서버 응답 받음)
- canonical_id 계산
- **파일 쓰기 X, dedup mark X**
- stdout에 `seen=N new=M ... dry_run=True` 1줄 출력
- 신규 paper 몇 개 떨어질지 미리 확인할 때 사용

### 3-4. 테스트: pytest in container
```bash
docker compose run --rm test
```
**영향:**
- 컨테이너 안 24개 pytest 케이스 실행 (watchlist parser, dedup, frontmatter writer, dry-run integration)
- vault 미접근 (mocked API + tempdir)
- 코드 변경 후 회귀 검증용

### 3-5. 임의 watchlist·inbox로 실험
```bash
docker compose run --rm app \
  --watchlist-labs /vault/path/to/other-labs.md \
  --watchlist-journals /vault/path/to/other-journals.md \
  --inbox-dir /vault/path/to/other-inbox \
  --dedup-db /cache/other-dedup.sqlite
```
**영향:** 디폴트 경로 override. fork된 watchlist 실험 가능.

---

## 4. 참조 파일 (스크립트가 읽는 파일·디렉토리)

### 4-1. `vault/01 CC/prod-autoresearch/docs-watchlist-labs.md`
**읽기 방법:** markdown 표 파서가 헤더 `PI 이름`, `OpenAlex authorId` 인식해 각 행 추출
**읽는 컬럼:** PI 이름, OpenAlex authorId, S2 authorId, 위치 필터, citation_min, 분야, 메모
**예시 행 (이탤릭·언더스코어 `_예: ..._` 둘러쌈) skip 처리** — 진짜 행은 일반 텍스트로 쓰면 됨
**영향:** 이 표가 비면 PI fetch 0건. 행 추가하면 다음 실행에서 자동 반영.

### 4-2. `vault/01 CC/prod-autoresearch/docs-watchlist-journals.md`
같은 패턴. 컬럼: 저널·컨퍼런스, OpenAlex source id, S2 venue 이름, citation_min, lookback_months, 분야, 메모
**영향:** 저널 단위 신규 논문 fetch 대상.

### 4-3. `~/.cache/autoresearch/dedup.sqlite`
**스키마:** `seen(canonical_id TEXT PRIMARY KEY, first_seen TEXT, source TEXT, title TEXT)`
**읽기:** canonical_id 본 적 있는지 lookup
**쓰기:** 새 paper 생성 시 canonical_id 기록
**영향:** 이 sqlite를 지우면 모든 paper "신규"로 간주, 다음 실행에서 inbox 재폭주.

### 4-4. 외부 API (네트워크)
- **OpenAlex**: `https://api.openalex.org/works` — author.id / source.id 필터
- **Semantic Scholar**: `https://api.semanticscholar.org/graph/v1/...`

API key 둘 다 불필요. `OPENALEX_EMAIL` env 설정 시 polite pool로 약간 빠름 (plist에 박혀있음).

---

## 5. 산출물 (스크립트가 쓰는 파일)

### 5-1. `vault/00 GTD/03Inbox/auto/paper-<canonical-id>.md`
**스키마 (frontmatter):**
```yaml
---
source: arxiv-paper
title: <논문 제목>
authors: [<list>]
published_date: YYYY-MM-DD
venue: <저널·conf>
citation_count: <int>
arxiv_id: <id (있을 때만)>
doi: <doi (있을 때만)>
status_file: False
---

## Abstract
<full abstract text>
```
**영향:** M1 material-absorb 엔진의 source_type 컨벤션과 호환. 이후 `/learn-paper`로 깊이 학습하거나 사용자 수동으로 `03 Resources/<topic>/`로 이동.

### 5-2. `~/.cache/autoresearch/dedup.sqlite`
새 canonical_id마다 row 추가. 영구.

### 5-3. `~/.claude/autoresearch/_launchd.{out,err}`
launchd 자동 실행 로그. 수동 실행 시는 stdout 직접.

---

## 6. 디버깅 절차

### 증상 1: paper-*.md 생성 안 됨 (seen=0)

**1-a. watchlist 파서가 행 인식하나?**
```bash
docker compose run --rm --entrypoint python3 app -c "
import sys; sys.path.insert(0, '.')
from fetch_papers import parse_labs_watchlist
text = open('/vault/01 Command Center/prod-autoresearch/docs-watchlist-labs.md').read()
rows = parse_labs_watchlist(text)
print('parsed rows:', len(rows))
for r in rows: print(' ', r.pi_name, '|', r.openalex_author_id, '|', r.effective_citation_min())
"
```
`parsed rows: 0` → 표 헤더 못 찾았거나 모든 행이 예시처럼 보임(이탤릭·언더스코어 둘러쌈).

**1-b. authorId 정확한가?**
```bash
curl -s "https://api.openalex.org/authors?search=<PI 이름>&per-page=3" | python3 -m json.tool | head -30
```
검색 결과의 `id` 마지막 토큰(`A...`)이 정확한지 affiliation·works_count로 검증.

**1-c. citation_min 너무 빡센가?**
임계값 낮춰 재시도. 통계 분야 = 5-15가 적당, ML conf = 50+.

**1-d. API 응답 직접 확인:**
```bash
curl -s 'https://api.openalex.org/works?filter=author.id:A5074093929,from_publication_date:2025-06-01,cited_by_count:>4&per-page=3' | python3 -m json.tool | grep -E '"title"|"cited_by_count"|"meta"' | head -20
```

### 증상 2: 중복 inbox 항목 (같은 paper 두 번)

**2-a. dedup sqlite 상태:**
```bash
sqlite3 ~/.cache/autoresearch/dedup.sqlite 'SELECT canonical_id, source, title FROM seen ORDER BY first_seen DESC LIMIT 10'
```

**2-b. 의도적 reset:**
```bash
rm ~/.cache/autoresearch/dedup.sqlite  # 주의: 다음 실행에서 inbox 폭주 가능
```

### 증상 3: launchd cron 안 돌아감

**3-a. plist load 상태:**
```bash
launchctl list | grep autoresearch
# 출력 있으면 load됨. exit code 마지막 컬럼 0이면 성공
```

**3-b. plist 강제 즉시 실행:**
```bash
launchctl kickstart -k gui/$UID/com.mori.autoresearch
```

**3-c. 로그 확인:**
```bash
tail -50 ~/.claude/autoresearch/_launchd.err
tail -50 ~/.claude/autoresearch/_launchd.out
```

**3-d. plist 재등록:**
```bash
launchctl unload ~/Library/LaunchAgents/com.mori.autoresearch.plist
cd ~/GIT/dotfiles/launchd && bash install_agents.sh
```

### 증상 4: 컨테이너 빌드·실행 실패

**4-a. Docker daemon 살아있나:**
```bash
orb status   # Running 확인
docker ps    # daemon 응답 확인
```

**4-b. 이미지 재빌드:**
```bash
cd ~/GIT/project-mori/util-autoresearch
docker compose build --no-cache
```

**4-c. 컨테이너 안 쉘 진입:**
```bash
docker compose run --rm --entrypoint /bin/bash app
# 컨테이너 안에서 ls /vault, ls /cache, python3 -c "..." 등 자유 검증
```

### 증상 5: 호스트 vault·cache mount 안 됨

**5-a. compose env 확인:**
```bash
docker compose config   # 치환된 yaml 보임 — volumes 경로 정확한지
```

**5-b. VAULT_ROOT env override:**
```bash
VAULT_ROOT=/path/to/other-vault docker compose run --rm app
```

### 증상 6: API rate limit·timeout

OpenAlex polite pool 10/s, Semantic Scholar ≤ 100/5min (unauthed). 스크립트가 `time.sleep`으로 안 넘게 페이싱. 그래도 503 받으면 fetch 실패 → `WARN openalex/<PI>: HTTP 503` stderr. dedup·다른 행은 정상 진행 (전체 run은 crash 안 함).

---

## 7. 운용 노트

- **citation_min 가이드:** 통계·전통 과학 = 5-15, ML 컨퍼런스 = 50+, 사회과학 = 10-20
- **lookback_months 가이드:** 빠른 분야 = 12, 발행 주기 긴 저널(JRSSB 등) = 24-36
- **위치 필터:** PI = supervisor 의미면 `last`, 학생 주도 ML이면 `any` 또는 `first_or_last`
- **첫 운용 시:** dry-run으로 신규 paper 개수 미리 보고 임계값 조정 → 실제 run

---

## 8. 외부 SSOT 의존 관계

- vault watchlist 2개 = 인간 큐레이션 SSOT (이 repo 코드보다 우선)
- dotfiles launchd plist = 잡 카탈로그 (이 repo의 path만 참조)
- ~/.cache/autoresearch = 영구 dedup state (백업 권장? 사라져도 다음 실행에서 재구축, 단 inbox 폭주)

이 repo가 SSOT인 것:
- application 코드 (`fetch_papers.py` + tests)
- 컨테이너 정의 (Dockerfile, docker-compose.yml)

---

## 9. 비-목표 / 향후 검토

- LinkedIn 자동 수집 — Browser Use MCP 도입 시 별도 source_type
- Zotero 연동 — `/zotero-save <paper-md>` 별도 entry로 검토 중
- arXiv firehose 수집 — 명시적 안 함 (noise)
