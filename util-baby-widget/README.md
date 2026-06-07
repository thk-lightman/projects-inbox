# util-baby-widget

Android 홈화면 위젯. 밥 / 잠 / 로션 — 마지막 탭 이후 경과 시간. 임계치 도달 임박/초과 시 색상 경고.

- **밥** 기본 임계치 4h
- **잠** 기본 임계치 3.5h
- **로션** 기본 임계치 12h
- **노랑** = 임계치 1시간 전, **빨강** = 30분 전/초과

## Architecture

- Kotlin + Jetpack Compose (메인 액티비티)
- Glance + AppWidget (홈화면 위젯) — 위젯 셀에서 직접 탭으로 기록
- SharedPreferences 로컬 저장 (`baby_widget_state`)
- 색상 전환을 위해 AlarmManager로 5분 주기 broadcast 트리거
  - 탭 입력 시 즉시 갱신
  - Android 시스템 자체 위젯 갱신 최소 주기는 30분이므로 별도 알람 사용
  - 부팅 시 `BootReceiver`로 알람 재등록

## Project layout

```
util-baby-widget/
├── settings.gradle.kts
├── build.gradle.kts
├── gradle.properties
├── gradle/libs.versions.toml
└── app/
    ├── build.gradle.kts
    └── src/main/
        ├── AndroidManifest.xml
        ├── java/com/mori/babywidget/
        │   ├── MainActivity.kt
        │   ├── ui/MainScreen.kt
        │   ├── data/{EventType,EventRepo,Formatting}.kt
        │   └── widget/{BabyWidget,BabyWidgetReceiver,WidgetSync,BootReceiver}.kt
        └── res/
            ├── values/{strings,colors,themes}.xml
            ├── xml/{baby_widget_info,backup_rules,data_extraction_rules}.xml
            ├── mipmap-anydpi-v26/ic_launcher.xml
            └── drawable/ic_launcher_{background,foreground}.xml
```

## Build / install

요건: Android Studio Hedgehog 이상 (또는 JDK 17 + Android SDK 34 + Gradle 8.5+).

```bash
# 처음 한 번: Gradle wrapper 생성
gradle wrapper --gradle-version 8.7

# 디버그 빌드
./gradlew assembleDebug

# 폰을 USB로 연결 (USB 디버깅 ON) 후 설치
./gradlew installDebug
```

또는 Android Studio에서 프로젝트 열고 ▶ Run 'app'.

## 홈화면에 위젯 추가

1. 홈화면 빈 공간 길게 누름 → "위젯"
2. **BabyWidget** 찾아서 드래그
3. 위젯 셀 탭 = 해당 이벤트 기록 (전체 위젯 즉시 갱신)

## 데이터 / 임계치 수정

- 위젯에서는 입력만. 임계치 변경 / 기록 삭제 / 초기화는 앱 본체(런처 아이콘) 열어서.
- 앱 화면에서 카드 옆 `↶` = 해당 이벤트 마지막 기록 삭제.
- 임계치는 시간 단위 (예: 4 = 4시간, 3.5 = 3시간 30분).

## Permissions

- `SCHEDULE_EXACT_ALARM` / `USE_EXACT_ALARM` — 5분 정확 알람용. Android 12+ 에서 권한 없으면 `setAndAllowWhileIdle` 폴백.
- `RECEIVE_BOOT_COMPLETED` — 재부팅 후 알람 복구.

## Roadmap

- v0.1 (현재): 3 이벤트, 색상 경고 위젯
- v0.2: 위젯 길게 누르기 = 마지막 기록 시간 수정 다이얼로그 (실수 복구)
- v0.3: 이벤트 추가/삭제 커스텀
- v0.4: 일일 통계 / JSON export
