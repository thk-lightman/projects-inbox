# Identity-Engine

Obsidian Vault의 한국어 문장을 영국식 영어로 번역하여 Anki + Google Sheets로 동기화하는 CLI 파이프라인.

## 개요

학습용 영어 카드를 자동 생성하는 표현-중심 파이프라인. 두 가지 모드 지원:

| 모드 | 입력 | 목적 | 번역 단계 |
|------|------|------|----------|
| **kr** | 내 KR vault (생각, 노트) | 내 사고를 영어로 어떻게 말할지 (output training) | 있음 |
| **en** | 영어 학습 자료 (책 노트, 강의) | native가 어떻게 말하는지 흡수 (input training) | 없음 |

공통 산출물: **EN 표현 + EN 사례 묶음** Anki 카드.

- **scope 필수**: 항상 사용자 지정 파일/폴더만 처리. 전체 vault 모드 없음 (AI 생성 표현 섞임 방지)
- **dedup 2단계**: (1) hash 기반 동일 텍스트 자동 묶음. (2) LLM semantic merge로 의미상 중복 표현 통합
- **review gate 필수**: 자동 추출 후 사람이 TSV로 검수. locked 상태만 번역/sync 진입
- **상태 영속화**: SQLite(WAL). 문장 / 표현 별도 상태머신
- **음성(TTS)은 범위 밖**: Anki AwesomeTTS 애드온이 카드 필드에서 직접 생성

### 파이프라인 (7단계, 명시적 분리)

```
crawl     → 지정 .md → 문장 토큰화 (lang-aware: kr 한글 ≥5자 / en 알파벳 ≥20자)
embed     → sentence-transformers로 각 문장 벡터화 (deterministic, no LLM)
cluster   → incremental nearest-neighbor: 새 문장을 기존 centroid와 비교, 
            cosine ≥ threshold(0.78)면 attach, 아니면 새 cluster 생성
            → cross-batch 자동 일관성 (기존 cluster pool 항상 비교 대상)
label     → LLM이 dirty cluster마다 canonical 패턴 명명 + gloss 생성 (cluster당 1콜)
review    → TSV export → 사람 검수 → import (status=locked)
translate → KR mode: 표현+인스턴스 KR→EN. EN mode: no-op
sync      → Anki 카드 push + Sheets append
```

### 왜 embedding 기반인가

기존 LLM-only 설계의 한계:
- LLM은 context window 안에서만 패턴 발견 → 전역 통계 못함
- 신규 파일 처리할 때 기존 expression과 비교 못함 → cross-batch 불일치
- LLM은 stateless, 매 batch마다 canonical 재발명

Embedding 도입 효과:
- **deterministic**: 같은 입력 같은 출력. clustering 재현 가능
- **cross-batch 자동**: 신규 문장은 기존 centroid와 비교됨 → 동일 패턴이면 자동 attach
- **LLM 비용 절감**: clustering은 deterministic, LLM은 labeling만 (cluster당 1콜)
- **너의 "스타일 사전"** = 누적된 centroid 매트릭스. 시간 지나며 강화됨

### 상태머신

- **문장**: `pending → curated → translated → synced` (실패: `error`)
- **표현**: `pending → locked → translated → synced` (실패: `error`. 거부: `deleted`)
- **dirty flag**: 새/변경된 cluster는 `label_dirty=1` → label 단계가 처리

## 사전 요구사항

- Python 3.10+ (pyenv 권장)
- Anki 데스크톱 + [AnkiConnect](https://ankiweb.net/shared/info/2055492159) 애드온 (코드 `2055492159`)
- 인증 자원 2종 (아래 "인증 설정" 참조)

## 설치

```bash
cd /Users/mori/GIT/forMori/project-mori/util-englishIdentity
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## 인증 설정

인증은 **2개 독립 트랙 + 로컬 1개**. 서로 순서 의존 없음.

### 트랙 1 — Gemini (번역). 백엔드 2종 택1

- **API 백엔드** (`TRANSLATION_BACKEND=api`, 기본): 문장당 1콜. 빠르지만 RPM/RPD 한도 작음
	- AI Studio API Key 발급: https://aistudio.google.com/apikey → `.env`의 `GEMINI_API_KEY` 입력
- **CLI 백엔드** (`TRANSLATION_BACKEND=cli`, 권장 — 대량 처리): 메가 배치(기본 100문장/콜), OAuth 사용 → Google 계정 구독 쿼터 활용
	- 공식 Gemini CLI 설치 (PATH에 `gemini` 잡혀야 함)
	- `gemini auth login` 한 번 실행 → 브라우저로 Google 계정 인증 (refresh token 캐시됨)
	- 확인: `gemini --version` + 짧은 프롬프트 dry-run
	- `.env`의 `GEMINI_API_KEY`는 비워도 됨 (cli 모드일 때는 미사용)

### 트랙 2 — Google Cloud SA (Sheets 동기화)

GCP Service Account JSON 키. Sheets API만 필요 (billing 계정 불필요).

```bash
# 0. gcloud 로그인 + 프로젝트 (없으면: gcloud projects create <PROJECT_ID>)
gcloud auth login
export PROJECT_ID=$(gcloud config get-value project)

# 1. Sheets API 활성화
gcloud services enable sheets.googleapis.com

# 2. Service Account 생성
gcloud iam service-accounts create identity-engine-bot \
  --display-name="Identity Engine Bot"

# 3. SA 키 파일(JSON) 생성
mkdir -p ~/.gcp
gcloud iam service-accounts keys create ~/.gcp/identity-engine-sa.json \
  --iam-account="identity-engine-bot@${PROJECT_ID}.iam.gserviceaccount.com"
chmod 600 ~/.gcp/identity-engine-sa.json

# 4. SA 이메일 출력 → 이 이메일을 대상 Google Sheet에 "편집자"로 공유
echo "identity-engine-bot@${PROJECT_ID}.iam.gserviceaccount.com"

# 5. (선택) ADC quota project 경고 정리
gcloud auth application-default set-quota-project ${PROJECT_ID}
```

- 4번에서 출력된 SA 이메일을 대상 스프레드시트에 **편집자**로 직접 공유 (IAM 권한 아님)
- `.env`의 `GOOGLE_APPLICATION_CREDENTIALS`, `GSPREAD_SPREADSHEET_ID` 입력

### 로컬 — Anki

- Anki 실행 + AnkiConnect 설치
- 확인: `curl -X POST http://localhost:8765 -d '{"action":"version","version":6}'`

## .env 설정

`.env.example`를 `.env`로 복사 후 채움.

- `TRANSLATION_BACKEND` — `api` 또는 `cli`. 기본 `api`. `cli`는 OAuth 기반 Gemini CLI 사용 (구독 쿼터)
- `GEMINI_API_KEY` — 트랙 1 키. `TRANSLATION_BACKEND=cli`면 빈 값 허용
- `GEMINI_CLI_PATH` — CLI 실행 파일 경로. 기본 `gemini` (PATH 검색). 절대 경로 가능
- `MEGA_BATCH_SIZE` — CLI 백엔드 1콜당 문장 수. 기본 100. 출력 token 한도 고려해 50~300 범위 권장
- `CLI_TIMEOUT` — CLI subprocess 타임아웃 초. 기본 300
- `GEMINI_MODEL` — 사용 모델. 기본 `gemini-3-flash-preview`. 후보: `gemini-3-pro-preview`, `gemini-2.5-flash`, `gemini-2.5-pro`
- `EMBEDDING_MODEL` — sentence-transformer 모델. 기본 `paraphrase-multilingual-MiniLM-L12-v2` (KR+EN, 384d, ~120MB)
- `CLUSTER_THRESHOLD` — cosine 임계값. 기본 0.78. 높이면 cluster 더 작고 더 많이 생성. 낮추면 더 큰 cluster 적게
- `EMBED_BATCH_SIZE` — sentence-transformer encode 배치. 기본 128 (CPU)
- `LABEL_SAMPLE_SIZE` — cluster당 LLM에 보내는 대표 문장 수. 기본 8
- `GOOGLE_APPLICATION_CREDENTIALS` — SA JSON 경로. `${HOME}` 등 변수 보간 가능 (python-dotenv가 로드 시 치환). `~`는 확장 안 됨
- `GSPREAD_SPREADSHEET_ID` — 대상 스프레드시트 ID (URL의 `/d/<ID>/` 부분)
- `GSPREAD_WORKSHEET_NAME` — 워크시트 탭 이름 (없으면 자동 생성)
- `VAULT_PATH` — Obsidian vault 절대 경로
- `PERSONA_PATH` — Identity-Persona.md 경로 (VAULT_PATH 기준 상대)
- `ANKICONNECT_URL` — 기본 `http://localhost:8765`
- `ANKI_DECK_NAME` / `ANKI_MODEL_NAME` — 덱/노트 모델명 (없으면 자동 생성)
- `BATCH_SIZE` — API 백엔드 배치 크기 (기본 10)
- `MAX_RETRIES` — API 재시도 횟수 (기본 3)

## 사용법

### 시나리오 A — KR mode (내 사고 → EN 표현 학습)

```bash
# 1. crawl + embed + cluster + label (자동 chained)
python run.py run --mode kr -p "01 Command Center/proj-MindOS"

# 2. 검수용 TSV 출력
python run.py review export -o review-kr.tsv --mode kr --min-frequency 2

# 3. TSV 편집 (스프레드시트/에디터)
#    - keep=0 으로 노이즈 행 drop
#    - merge_into_id=<id> 로 수동 merge
#    - expr 컬럼 수정 가능

# 4. 검수본 import → status=locked
python run.py review import review-kr.tsv

# 5. locked 표현 + 인스턴스 KR→EN 번역
python run.py translate --mode kr

# 6. Anki + Sheets push
python run.py sync --mode kr
```

### 시나리오 B — EN mode (영어 자료 → 패턴 흡수)

```bash
python run.py run --mode en -p "03 Resources/EN-learning"
python run.py review export -o review-en.tsv --mode en --min-frequency 2
# (편집)
python run.py review import review-en.tsv
python run.py translate --mode en   # no-op: canonical이 이미 EN
python run.py sync --mode en
```

### 신규 파일 추가 시 (incremental)

같은 명령 그대로 재실행. embedding cluster가 cross-batch 일관성 자동 보장:
```bash
# 새 파일이 vault에 추가됨
python run.py run --mode kr -p "01 Command Center/proj-MindOS"
# → 새 문장만 embed (delta tracking)
# → 새 문장 각각이 기존 centroid와 비교됨
#   - 매치되면 기존 expression에 attach (centroid running mean 업데이트, label_dirty=1)
#   - 안 매치면 새 expression 생성
# → label이 dirty cluster만 재명명
# → 검수 TSV에는 새/변경된 row만 표시
```

### 단일 명령 사용

```bash
python run.py crawl     -p "..." --mode kr|en
python run.py embed     --mode kr|en             # sentence-transformer 벡터화
python run.py cluster   --mode kr|en [-p "..."]  # incremental nearest-neighbor
python run.py label     --mode kr|en             # LLM canonical 명명
python run.py review export -o FILE --mode kr|en --min-frequency N
python run.py review import FILE
python run.py translate --mode kr|en --min-frequency N
python run.py sync --mode kr|en --min-frequency N
python run.py status                              # 두 lang + embedding/dirty stats
python run.py export --kind expressions --mode kr --stage locked -o ...
```

### 운용 원칙

- **scope 명시 필수** (`-p`/`--paths`). 학습 fragment 신중히 선별
- **mode 명시 필수** (`--mode kr|en`). 잘못 지정하면 토크나이저가 다 버려 silent fail
- **review gate 통과 강제** — locked 아닌 행은 translate/sync 진입 불가
- **dedup 2단계** — hash 자동 + LLM semantic merge + 사람 검수 = 3중 dedup
- **빈도 임계값** — `--min-frequency 2` 이상 권장. 1회만 등장한 패턴은 노이즈 가능성 큼
- **중단 안전** — Ctrl+C 시 체크포인트 저장. 재실행 시 이어감 (DB가 SSOT)
- **재시도** — 단계 분리됨. 특정 단계만 다시 돌릴 수 있음 (예: merge만 재실행)

### TSV review 포맷

```
id  lang  freq  keep  merge_into_id  expr                  instance_preview
12  kr    7     1                    ~할 수밖에 없다       어쩔 수 없는 ... | 결국 ...
13  kr    3     1     12             ~수 밖에 없는 거다    (typo 변형 → 12로 merge)
14  kr    2     0                    뭔가 그런 거          (드롭)
15  en    5     1                    It stands to reason that ~  ...
```

편집 규칙:
- `keep=0` (또는 `n`, `no`, `false`) → 행 삭제
- `merge_into_id=N` → 이 행을 N번 표현에 통합 (인스턴스 이전, freq 합산, 자기 삭제)
- `expr` 수정 → text + hash 재계산
- 비워두면 단순 lock (검수 통과)

## 알려진 미검증 리스크

- **토크나이저**: 마크다운 리스트/표 안의 한국어 문장 분할이 부정확할 수 있음 → 필요시 `vault_crawler._tokenize_korean()` 보강
- **Gemini 응답 후처리**: 코드 블록/quote/prefix가 응답에 섞이면 정제 누락 가능 → `_translate_one()`에서 추가 strip 필요할 수 있음
- **CLI 백엔드 JSON 파싱**: CLI는 structured output 보장 약함. prose가 섞이면 `_parse_json_array()`의 `[`/`]` slice가 실패 → 해당 mega-batch 전체가 `error` 상태로 마킹됨. 다음 실행 시 재시도되지 않으므로 status가 `error`인 행을 별도 처리 필요 (현재 DB에 재시도 로직 없음)
- **CLI ToS**: 공식 Gemini CLI를 스크립트로 자동 호출하는 것은 회색지대. 대량/상업적 자동화 전 ToS 확인 권장
- **AnkiConnect 모델 충돌**: 기존 `Identity-Engine` 노트 모델이 다른 필드 스키마(구 Front/Back/Source 또는 Audio 필드 포함 등)면 첫 실행 시 충돌 → `.env`의 `ANKI_MODEL_NAME`을 새 값(예: `Identity-Engine-Expr`)으로 바꾸거나 Anki에서 구 모델 삭제 후 재실행
- **Curate 품질**: 클러스터링은 LLM 1회 호출 결과에 의존. 짧은 batch에선 패턴 잡기 어려울 수 있음. `MEGA_BATCH_SIZE` 50~100 유지 권장
- **표현 dedup 한계**: 정규화는 `strip().lower()` + sha256. 의미상 동일하지만 표기 다른 두 표현은 별도 row로 남음 → 운영 중 표현 TSV export → 수동 merge 보강 필요
