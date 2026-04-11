## Prerequisites

- Python 3.11+
- Google Cloud SDK (`gcloud`)
- 파이프라인에서 읽을 구글 시트 (데이터가 이미 존재해야 함)

---

## Step 1. Python 가상환경 생성 및 패키지 설치

```bash
cd /Users/mori/GIT/forMori/etl-ai-pipeline
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

---

## Step 2. GCP 서비스 계정 생성 (구글 시트 접근용)

구글 시트 API는 **서비스 계정(Service Account)** 이라는 봇 전용 계정으로 인증한다. OAuth 로그인 없이 서버에서 자동 실행 가능.

### 방법 A. gcloud CLI (권장)

> GUI 대비 장점: 재현 가능, 자동화 가능, 빠름

#### 2-1. gcloud 로그인 (최초 1회)

```bash
gcloud auth login
```

브라우저가 열리고 구글 계정 로그인 → 터미널에 인증 완료 메시지 출력.

#### 2-2. 프로젝트 생성 + 기본 프로젝트 설정

```bash
# 프로젝트 생성 (프로젝트 ID는 전 세계 유일해야 함)
gcloud projects create etl-ai-pipeline-$(date +%s) --name="etl-ai-pipeline"

# 출력된 실제 프로젝트 ID로 기본 프로젝트 설정
gcloud config set project <PROJECT_ID>
```

#### 2-3. API 활성화

```bash
gcloud services enable sheets.googleapis.com drive.googleapis.com
```

#### 2-4. 서비스 계정 생성

```bash
gcloud iam service-accounts create sheet-bot \
  --display-name="Sheet Bot for ETL Pipeline"
```

#### 2-5. JSON 키 파일 생성

```bash
mkdir -p /Users/mori/GIT/forMori/etl-ai-pipeline/credentials

gcloud iam service-accounts keys create \
  /Users/mori/GIT/forMori/etl-ai-pipeline/credentials/service_account.json \
  --iam-account=sheet-bot@$(gcloud config get-value project).iam.gserviceaccount.com
```

#### 2-6. 서비스 계정 이메일 확인

```bash
gcloud iam service-accounts list --format="value(email)" --filter="displayName:Sheet Bot"
```

---

### 방법 B. GCP 콘솔 (GUI)

#### 2-1. GCP 콘솔 접속

- [console.cloud.google.com](https://console.cloud.google.com) 접속
- 상단 프로젝트 선택 → **새 프로젝트** → 이름: `etl-ai-pipeline` → 만들기

#### 2-2. API 활성화

- 좌측 메뉴 → **API 및 서비스** → **라이브러리**
- `Google Sheets API` 검색 → **사용**
- `Google Drive API` 검색 → **사용**

#### 2-3. 서비스 계정 생성

- 좌측 메뉴 → **API 및 서비스** → **사용자 인증 정보**
- **+ 사용자 인증 정보 만들기** → **서비스 계정**
- 이름: `sheet-bot` → 만들기 및 계속 → 완료

#### 2-4. JSON 키 파일 다운로드

- 생성된 `sheet-bot` 서비스 계정 클릭
- **키** 탭 → **키 추가** → **새 키 만들기** → **JSON** → 만들기
- 다운로드된 파일을 프로젝트 폴더로 이동:

```bash
mkdir -p /Users/mori/GIT/forMori/etl-ai-pipeline/credentials
mv ~/Downloads/etl-ai-pipeline-*.json \
   /Users/mori/GIT/forMori/etl-ai-pipeline/credentials/service_account.json
```

---

## Step 3. 구글 시트에 서비스 계정 공유

> 이 단계는 CLI로 대체할 수 없다. 브라우저에서 수행.

1. JSON 파일 내 `"client_email"` 필드에서 이메일 확인
   - 형식: `sheet-bot@<PROJECT_ID>.iam.gserviceaccount.com`
2. 파이프라인에서 읽을 구글 시트를 브라우저에서 열기
3. 우측 상단 **공유** → 서비스 계정 이메일 입력 → **편집자** → 보내기

---

## Step 4. Gemini API 키 발급

1. [aistudio.google.com/apikey](https://aistudio.google.com/apikey) 접속
2. **Create API Key** → Step 2에서 만든 프로젝트 선택 → 키 생성
3. 생성된 키 복사

---

## Step 5. .env 파일 작성

```bash
cp .env.example .env
```

`.env` 파일을 열어 실제 값으로 수정:

```dotenv
GCP_SERVICE_ACCOUNT_FILE=./credentials/service_account.json
SHEET_NAME=실제_구글_시트_문서_제목
INPUT_WORKSHEET=실제_입력_탭_이름
OUTPUT_WORKSHEET=Output_Data
GEMINI_API_KEY=발급받은_API_키
LLM_MODEL=gemini-2.5-flash
GROUP_COLUMN=실제_그룹핑_컬럼명
TEXT_COLUMN=실제_텍스트_컬럼명
API_DELAY_SECONDS=1.0
```

---

## Step 6. 실행

```bash
cd /Users/mori/GIT/forMori/etl-ai-pipeline
source .venv/bin/activate
python pipeline.py
```

---

## 체크리스트

| # | 작업 | 소요 시간 | 비용 |
|---|------|-----------|------|
| 1 | venv + pip install | 2분 | 무료 |
| 2 | GCP 서비스 계정 + JSON 키 | 5분 | 무료 |
| 3 | 구글 시트에 서비스 계정 이메일 공유 | 1분 | 무료 |
| 4 | Gemini API 키 발급 | 2분 | 무료 (무료 티어) |
| 5 | .env 작성 | 1분 | - |
| 6 | `python pipeline.py` 실행 | 데이터량 비례 | Gemini 토큰 비용 |

---

## 프로젝트 구조

```
etl-ai-pipeline/
├── .env.example          ← 환경변수 템플릿
├── .env                  ← 실제 비밀 값 (git 제외)
├── .gitignore
├── credentials/
│   └── service_account.json  ← GCP 서비스 계정 키 (git 제외)
├── config.py             ← .env 로더
├── pipeline.py           ← 메인 파이프라인 (Extract → Transform → Load)
├── requirements.txt
└── SETUP_GUIDE.md        ← 이 문서
```

