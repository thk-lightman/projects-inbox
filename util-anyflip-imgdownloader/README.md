# AnyFlip 이미지 다운로더

AnyFlip 웹 뷰어에서 전체 페이지 이미지를 ZIP으로 한번에 다운로드하는 브라우저 콘솔 스크립트.

## 사용법

1. AnyFlip 뷰어 페이지를 브라우저에서 연다
2. `F12` 키를 눌러 개발자 도구를 열고 **Console** 탭으로 이동
3. `download.js`의 내용을 전부 복사하여 콘솔에 붙여넣기
4. Enter → 콘솔에 진행 로그가 출력되며, 완료 시 `images.zip` 자동 다운로드

> 다른 책에 적용하려면 `CONFIG_URL`과 `BASE_URL`을 해당 책의 값으로 수정한다.
> config.js URL은 개발자 도구 **Network** 탭에서 `config.js`를 검색하면 찾을 수 있다.

---

## 이 스크립트가 만들어지기까지 — 무엇이 문제였고, 왜 어려웠나

### 배경: 처음 시도한 코드

```javascript
const images = document.querySelectorAll('img[src*="../files/large/"]');
```

이 코드는 **8장만 다운로드**되고 멈췄다. 두 가지 문제가 있었다.

---

### 문제 1: "화면에 보이는 것"과 "실제로 존재하는 것"은 다르다

#### 개념: DOM (Document Object Model)

브라우저에서 웹페이지를 열면, HTML 코드가 **DOM**이라는 트리 구조로 변환된다.
`document.querySelectorAll('img')`는 이 DOM 안에 있는 `<img>` 태그만 찾는다.

```
[실제 데이터]                    [화면에 그려진 것 = DOM]
config.js 안에 171개 경로 → → → 현재 보이는 8개만 <img> 태그로 존재
```

AnyFlip 같은 뷰어는 **성능을 위해** 171페이지를 한번에 다 그리지 않는다.
현재 보고 있는 페이지 근처의 몇 장만 `<img>` 태그로 만들고, 나머지는 스크롤하면 그때 만든다.
이것을 **지연 로딩(Lazy Loading)** 이라고 한다.

**결론:** DOM을 뒤져서는 8장밖에 못 찾는다. 원본 데이터가 있는 곳을 찾아야 한다.

---

### 문제 2: 원본 데이터는 JavaScript 파일(config.js) 안에 있다

171개 이미지 경로는 `config.js`라는 별도의 JavaScript 파일에 저장되어 있었다.
이 파일은 페이지가 로드될 때 서버에서 받아오지만, 그 안의 데이터가 항상 전역 변수로 노출되지는 않는다.

```
config.js 내부 구조 (일부):
{
  "n": ["../files/large/7dc57e63725a8bc5fa524c3a952f0f7a.webp"],
  "t": "../files/thumb/5479cccc46c5e9323eb3b86fd85f2752.webp"
}
// 이런 객체가 171개
```

우리 해결법: config.js를 텍스트로 통째로 받아서, 정규식으로 이미지 경로를 직접 추출했다.

---

### 문제 3: 브라우저는 연속 다운로드를 차단한다

처음 코드가 8장에서 멈춘 또 다른 이유.
브라우저(Chrome 등)는 보안상 **프로그래밍 방식의 연속 다운로드를 약 10회 이후 차단**한다.
이것은 악성 사이트가 수백 개 파일을 강제 다운로드하는 것을 막기 위한 보호 장치이다.

**해결법:** 171개를 개별 다운로드하는 대신, 메모리에서 ZIP 하나로 묶어서 **1번만** 다운로드한다.
이를 위해 `JSZip`이라는 외부 라이브러리를 사용했다.

---

## 핵심 개념 정리

### 1. DOM vs 데이터 소스


|       | DOM (화면)                      | 데이터 소스 (config.js) |
| ----- | ----------------------------- | ------------------ |
| 무엇인가  | 브라우저가 화면에 그린 HTML 요소들         | 서버에서 받아온 원본 데이터    |
| 접근 방법 | `document.querySelectorAll()` | `fetch()` → 텍스트 파싱 |
| 이미지 수 | 현재 보이는 ~8개                    | 전체 171개            |


**교훈:** 화면에 보이는 것이 전부가 아니다. 웹페이지의 데이터는 여러 파일에 분산되어 있다.

### 2. fetch() — 서버에 데이터를 요청하는 함수

```javascript
var response = await fetch('https://example.com/data.js');  // 서버에 요청
var text = await response.text();                            // 응답을 텍스트로 변환
```

브라우저가 서버에 파일을 달라고 요청하는 것. 이미지든, JS 파일이든, 뭐든 가져올 수 있다.
`await`는 "이 작업이 끝날 때까지 기다려"라는 뜻이다.

### 3. Blob — 바이너리 데이터 덩어리

이미지, 동영상 같은 파일은 텍스트가 아니라 바이너리(0과 1의 나열)이다.
`fetch`로 이미지를 받으면 `response.blob()`으로 바이너리 형태로 변환한다.
이 Blob을 ZIP에 넣거나, 다운로드 링크로 만들 수 있다.

### 4. 정규식 (Regular Expression) — 텍스트에서 패턴 찾기

```javascript
var re = /files\/large\/[a-f0-9]+\.webp/g;
```

이 정규식은 `files/large/` 로 시작하고 16진수 해시와 `.webp`로 끝나는 모든 문자열을 찾는다.
config.js 전체 텍스트에서 이미지 경로만 골라내는 데 사용했다.


| 패턴               | 의미                                       |
| ---------------- | ---------------------------------------- |
| `files\/large\/` | 문자 그대로 `files/large/` (`/`는 `\/`로 이스케이프) |
| `[a-f0-9]+`      | 16진수 문자(a~~f, 0~~9)가 1개 이상               |
| `\.webp`         | 문자 그대로 `.webp`                           |
| `g`              | 전체 텍스트에서 모두 찾기 (global)                  |


### 5. 이스케이프 (Escape) — `\/` 가 뭔가?

JSON이나 JavaScript 문자열에서 특수문자 앞에 `\`를 붙이는 것.
config.js 안의 경로가 `..\/files\/large\/abc.webp`로 저장되어 있었다.
`\/`는 실제로는 그냥 `/`와 같은 의미이지만, 정규식이 이것을 인식 못할 수 있어서
먼저 `text.replace(/\\\//g, '/')`로 정리해준 것이다.

### 6. 절대 경로 vs 상대 경로

```
상대 경로: ../files/large/abc.webp        (← "현재 위치 기준으로 한 칸 위의 files 폴더")
절대 경로: https://online.anyflip.com/itmik/xxrk/files/large/abc.webp
```

config.js 안의 경로는 상대 경로였다. 이것을 `fetch()`에 쓰려면 절대 경로로 바꿔야 한다.
`BASE_URL + 'files/large/abc.webp'` 형태로 직접 조합했다.

### 7. Console의 Promise

- Reject는 거절된 상태, Pending은 작업중인 상태.

---

## 다음에 비슷한 상황이 오면 — 최소 경로 질문법

비슷한 웹 스크래핑 문제를 만났을 때, 아래 순서로 질문하면 가장 빠르게 해결할 수 있다:

### Step 1: "이 사이트에서 이미지를 전부 다운로드하고 싶다"

- 대상 사이트 URL을 함께 제공

### Step 2: "이미지 목록이 HTML에 없고 별도 JS 파일에 있다"

- 개발자 도구 **Network** 탭에서 `config`, `data`, `pages` 등을 검색
- 해당 JS 파일의 **전체 URL**을 제공

### Step 3: (선택) JS 파일의 처음 부분을 보여주기

- 콘솔에서 `fetch('URL').then(function(r){return r.text()}).then(function(t){document.title=t.substring(0,500)})` 실행
- 브라우저 탭 제목이 바뀌면 그것을 복사해서 제공

이 세 단계면 첫 번째 시도에서 작동하는 코드를 받을 수 있다.

핵심은 **"데이터가 어디에 있는지"를 알려주는 것**이다.

---

## 파일 구조

```
anyflip-image-downloader/
├── download.js   ← 브라우저 콘솔에 붙여넣을 스크립트
└── README.md     ← 이 문서
```

