## Prerequisites

- Python 3.11+
- Google Cloud SDK (`gcloud`)
- 파이프라인에서 읽을 구글 시트 (데이터가 이미 존재해야 함)

---

## Step 1. Python 가상환경 생성 및 패키지 설치

```bash
cd /Users/mori/GIT/forMori/project-mori/util-sheetprocessing
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
# 프로젝트 생성 (프로젝트 ID는 전 세계 유일해야 함, unix timestamp as sec)
gcloud projects create util-sheetprocessing --name="util-sheetprocessing"
# RESULT: Enabling service [cloudapis.googleapis.com] on project [util-sheetprocessing]...
# 출력된 실제 프로젝트 ID로 기본 프로젝트 설정(ID: util-sheetprocessing)
gcloud config set project <PROJECT_ID>
```

#### 2-3. API 활성화

```bash
gcloud services enable sheets.googleapis.com drive.googleapis.com
```

#### 2-4. 서비스 계정 생성

```bash
gcloud iam service-accounts create sheet-bot --display-name="Sheet Bot for sheet processing"
```

#### 2-5. JSON 키 파일 생성

```bash
mkdir -p /Users/mori/GIT/forMori/project-mori/util-sheetprocessing/credentials

gcloud iam service-accounts keys create \
  /Users/mori/GIT/forMori/project-mori/util-sheetprocessing/credentials/service_account.json \
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
mkdir -p /Users/mori/GIT/forMori/project-mori/util-sheetprocessing/credentials
mv ~/Downloads/*.json \
   /Users/mori/GIT/forMori/project-mori/util-sheetprocessing/credentials/service_account.json
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

## Step 5. 실행 (레고 조립)

설정 프레임워크 없음 — 값은 인자로 넘긴다. 척추(read/write) + 옵션 transform 블록.

### 흔한 경우: 그룹 병합만 (LLM 없음)

```bash
cd /Users/mori/GIT/project-mori/util-sheetprocessing
source .venv/bin/activate
python run.py --sa credentials/service_account.json --sheet 실제_시트_제목 \
    --in 입력탭 --out 출력탭 --group-col Group_ID --text-col Raw_Text
```

### LLM 등 특정 기능 붙이기: 자기 스크립트로 조립

```python
from sheet_io import auth, read_sheet, write_sheet
from transforms import group_merge, llm_map

client = auth("credentials/service_account.json")
rows = read_sheet(client, "실제_시트_제목", "입력탭")
merged = group_merge(rows, "Group_ID", "Raw_Text")
out = llm_map(merged, my_classifier)          # my_classifier(text)->dict, provider 무관
write_sheet(client, "실제_시트_제목", "출력탭", out, mode="overwrite")
```

`llm_map`은 provider 무관 — Gemini/OpenAI/정규식 등 어떤 `fn(text)->dict`든 받는다.
시크릿(API 키)이 필요하면 환경변수로 읽어 `my_classifier` 안에서 쓴다.

---

## 체크리스트

> 완료 시 `[ ]` → `[x]` 로 변경

- [x] Step 1 — venv + pip install
- [x] Step 2-1 — gcloud auth login
- [x] Step 2-2 — 프로젝트 생성 (`util-sheetprocessing`)
- [x] Step 2-3 — API 활성화 (Sheets, Drive)
- [x] Step 2-4 — 서비스 계정 생성
- [x] Step 2-5 — JSON 키 파일 생성
- [x] Step 2-6 — 서비스 계정 이메일 확인
- [x] Step 3 — 구글 시트에 서비스 계정 이메일 공유
- [ ] Step 4 — Gemini API 키 발급 (LLM 단계 붙일 때만)
- [ ] Step 5 — `python run.py ...` 실행 (또는 자기 스크립트로 조립)


---

## 프로젝트 구조

```
util-sheetprocessing/
├── credentials/
│   └── service_account.json  ← GCP 서비스 계정 키 (git 제외)
├── sheet_io.py           ← 재사용 척추: auth / read_sheet / write_sheet
├── transforms.py         ← 옵션 블록(rows→rows): group_merge / llm_map
├── run.py                ← 얇은 조립 CLI (read→group_merge→write)
├── requirements.txt
└── README.MD             ← 이 문서
```

