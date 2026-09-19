# Artist Sorter

Violet 한국어 SQLite DB를 이용해 Hitomi 다운로드 자료를 작품 번호로 매칭하고, 작가별 폴더로 정리하는 Windows GUI 프로그램입니다.

## 기능

- 다운로드 폴더의 **최상위 파일/폴더명**에서 Hitomi 작품 번호 추출
- Violet DB의 `HitomiColumnModel.Id`와 번호 매칭
- `Artists` 컬럼을 이용한 작가별 분류
- 실행 전 전체 결과 미리보기
- **이동 / 복사** 선택
- 작가가 여러 명인 경우 `첫 번째 작가` 또는 `작가명 합치기` 선택
- 동일한 대상이 이미 있으면 덮어쓰지 않고 건너뜀
- 로컬 Violet DB 직접 선택
- Pi에 공개된 DB 다운로드 URL을 입력해 `rawdata-korean.db` 갱신
- 입력 경로와 DB URL은 사용자 PC의 로컬 설정에만 저장

## 사용 방법

1. `ArtistSorter.exe` 실행
2. `원본 폴더`에 정리할 Hitomi 자료가 있는 폴더 선택
3. `정리 폴더` 선택
4. Violet DB를 직접 선택하거나 `Pi DB URL`을 입력하고 `DB 내려받기`
5. `미리보기`로 매칭 결과 확인
6. 이동/복사 방식을 선택하고 `정리 실행`

자료 이름은 `4192094`, `[4192094] 제목`, `4192094.zip` 같은 형태를 인식합니다. 번호가 없는 항목이나 DB에서 찾지 못한 항목은 자동으로 건너뜁니다.

## DB 형식

현재 Violet DB의 `HitomiColumnModel` 테이블에서 `Id`, `Title`, `Artists` 컬럼을 사용합니다. `Artists`는 `|artist1|artist2|` 형태를 지원합니다.

## EXE 빌드

GitHub Actions의 **Build Windows EXE** 워크플로가 Windows에서 PyInstaller로 단일 EXE를 생성합니다. Actions 실행 결과의 `ArtistSorter-windows` artifact에서 받을 수 있습니다.

로컬 Windows에서도 `requirements-dev.txt` 설치 후 PyInstaller로 `dist/ArtistSorter.exe`를 만들 수 있습니다.

## 주의

- `이동`은 실제 원본 위치를 변경합니다. 먼저 미리보기 결과를 확인하세요.
- 대상 경로에 같은 이름이 이미 있으면 덮어쓰지 않습니다.
- DB URL, 사설 IP, 토큰 같은 개인 설정은 저장소에 커밋하지 마세요.
