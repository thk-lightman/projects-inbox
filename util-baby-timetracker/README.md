# util-baby-timetracker

육아 인지부하 감소용 시간 추적 앱. Expo (React Native + TypeScript).

## What it does

- **HUD 카운트다운** — "다음 밥까지", "다음 잠까지" 한눈에. 시간 지나면 빨강.
- **4 버튼** — 밥 / 잠(토글) / 기저귀 / 로션. 1 탭 = 현재 시각 기록.
- **잠 토글** — 마지막 상태에 따라 "잠들기" ↔ "깸"으로 자동 전환.
- **되돌리기** — 마지막 입력 1건 즉시 취소.
- **로그 행** — 탭 = 시간 수정 (HH:MM), 길게 누르기 = 삭제.

## Rules

- 다음 밥 = 마지막 밥 + 4h
- 다음 잠 = 마지막 깸 + 3.5h (wake window 3~4h 중간값)
- 자는 중에는 잠 카운트다운 숨김, 깸 입력 대기 표시

## Storage

- `AsyncStorage` 키 `baby-timetracker:events:v1`
- 기기 로컬. JSON 배열 `{id, type, ts}`.

## Run

```bash
npm install
npm run start          # Expo Dev Tools — QR 스캔으로 폰에서 실행
npm run ios            # iOS 시뮬레이터
npm run android        # Android 에뮬레이터
npm run web            # 브라우저 미리보기
npm run typecheck      # tsc --noEmit
```

폰에 [Expo Go](https://expo.dev/client) 설치 후 QR 스캔.

## Roadmap

- v0.1 (현재): 시간 추적 4 버튼 + 룰 + 로컬 저장
- v0.2: 일일 통계 / JSON export
- v0.3: 재고관리 (기저귀/분유) — threshold 알림
- v0.4: Obsidian Daily Note 연동
