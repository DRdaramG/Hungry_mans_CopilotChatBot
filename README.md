# Hungry Man's Copilot ChatBot 🍽️

**GitHub Copilot**의 채팅 API에 연결하는 Python 기반 데스크탑 챗봇입니다.  
Copilot Pro+ 구독자라면 Claude, Gemini, ChatGPT 등 프리미엄 AI 모델을  
별도 구독료 없이 편리한 GUI로 사용할 수 있습니다.

---

## 주요 기능

| 기능 | 설명 |
|---|---|
| **모델 전환** | 라디오 버튼으로 대화 중에도 **Claude Opus 4.5**, **Gemini 3 Pro**, **GPT-4.1** 간 자유롭게 전환 |
| **GitHub OAuth** | 디바이스 플로우 인증 — 토큰을 직접 복사·붙여넣기할 필요 없음 |
| **다중 대화** | 사이드바에서 대화를 만들고, 이름을 바꾸고, 삭제 가능; 마지막으로 열었던 대화를 자동으로 기억 |
| **대화 기록 저장** | 로컬 SQLite 데이터베이스(`~/.copilot_chatbot.db`)에 저장; 기존 JSON 기록은 자동으로 마이그레이션 |
| **토큰 인식 컨텍스트 창** | tiktoken으로 토큰을 계산하고 히스토리를 잘라 모든 요청이 모델의 컨텍스트 한도 안에 들어오도록 처리 |
| **이미지 첨부** | `.png`, `.jpg`, `.jpeg`, `.gif`, `.webp` 파일을 첨부하면 base64로 멀티모달 모델에 전송 |
| **스프레드시트 첨부** | `.csv`, `.xls`, `.xlsx` 파일을 첨부하면 내용이 텍스트로 변환되어 전송 |
| **개인 프롬프트** | 이름 있는 프롬프트를 저장·수정·삭제; 체크박스로 여러 프롬프트를 동시에 활성화(자동 앞붙임); 드래그로 순서 변경 |
| **시스템 프롬프트** | 프롬프트 매니저에서 대화별 시스템 프롬프트 설정 |
| **프롬프트 가져오기/내보내기** | `.json` 파일로 프롬프트 라이브러리 공유 |
| **대화 저장** | 대화를 JSON 또는 일반 텍스트로 내보내기 |
| **스트리밍 응답** | 응답이 단어 단위로 실시간으로 표시 |
| **기록 지연 로딩** | 채팅 상단으로 스크롤하면 이전 메시지를 필요할 때 불러옴 |

---

## 필요 환경

* Python **3.10** 이상
* **GitHub Copilot Pro+** 구독

### Python 패키지

```
pip install -r requirements.txt
```

| 패키지 | 역할 |
|---|---|
| `requests` | GitHub 및 Copilot API HTTP 통신 |
| `openpyxl` | `.xlsx` 엑셀 파일 읽기 |
| `tiktoken` | 컨텍스트 창 관리를 위한 정확한 토큰 계산 |
| `typing_extensions` | Python 3.11 미만에서 최신 `typing` 기능 지원 |

GUI에는 `tkinter`를 사용하며, Windows·macOS용 Python 공식 설치 프로그램에는 기본 포함되어 있습니다.  
Linux에서는 별도로 설치해야 할 수 있습니다:

```bash
# Debian / Ubuntu
sudo apt-get install python3-tk
```

---

## 사용 방법

### 1단계 — Python 3 설치 (Windows)

> 이미 Python 3.10 이상이 설치되어 있다면 이 단계를 건너뜁니다.  
> 명령 프롬프트(cmd)에서 `python --version`을 입력해 버전을 확인할 수 있습니다.

1. 웹 브라우저에서 **https://www.python.org/downloads/** 에 접속합니다.
2. **"Download Python 3.x.x"** 버튼을 클릭해 설치 프로그램을 다운로드합니다.
3. 다운로드된 `.exe` 파일을 실행합니다.
4. 설치 화면 **하단**에 있는 **"Add Python to PATH"** 체크박스를 반드시 체크합니다.  
   *(이 옵션을 빠뜨리면 이후 `python` 명령이 인식되지 않습니다.)*
5. **"Install Now"** 를 클릭하고 설치가 완료될 때까지 기다립니다.
6. 설치 완료 후 **"Close"** 를 클릭합니다.

### 2단계 — 코드 다운로드

1. 이 저장소 페이지 오른쪽 위의 초록색 **"Code"** 버튼을 클릭합니다.
2. **"Download ZIP"** 을 선택해 ZIP 파일을 다운로드합니다.
3. 다운로드된 ZIP 파일을 원하는 위치에 압축 해제합니다.  
   예) `C:\Users\사용자이름\Documents\Hungry_mans_CopilotChatBot`

### 3단계 — 코드 폴더에서 터미널 열기 (Windows)

압축을 해제한 폴더를 **파일 탐색기**로 엽니다.  
다음 방법 중 하나를 사용해 그 폴더에서 바로 터미널을 열 수 있습니다.

**방법 A — 주소 표시줄 이용 (가장 빠름)**

1. 파일 탐색기 상단의 주소 표시줄을 클릭합니다.
2. 주소가 선택된 상태에서 `cmd`를 입력하고 **Enter**를 누릅니다.
3. 해당 폴더 경로에서 명령 프롬프트가 열립니다.

**방법 B — 우클릭 메뉴 이용**

1. 압축 해제된 폴더 안의 빈 공간을 **Shift + 우클릭**합니다.
2. **"여기서 PowerShell 창 열기"** 또는 **"여기서 터미널 열기"** 를 선택합니다.

### 4단계 — 패키지 설치

열린 터미널(명령 프롬프트 또는 PowerShell)에서 아래 명령을 입력하고 **Enter**를 누릅니다:

```
pip install -r requirements.txt
```

설치가 완료될 때까지 기다립니다. (인터넷 속도에 따라 수십 초 정도 걸립니다.)

### 5단계 — 프로그램 실행

```
python main.py
```

처음 실행하면 **설정 → GitHub 인증…** 메뉴로 이동해 화면의 안내에 따라 GitHub 계정을 연동합니다.

---

## 프로젝트 구조

```
Hungry_mans_CopilotChatBot/
├── main.py                 # 진입점
├── requirements.txt
├── README.md
└── src/
    ├── __init__.py
    ├── app.py              # tkinter GUI (메인 창 + 모든 다이얼로그)
    ├── auth.py             # GitHub 디바이스 플로우 OAuth + 토큰 저장
    ├── chat_store.py       # SQLite 기반 대화 및 메시지 저장
    ├── context_manager.py  # 토큰 인식 컨텍스트 창 트리밍 (tiktoken)
    ├── copilot_api.py      # Copilot Chat API 클라이언트 (스트리밍)
    ├── file_handler.py     # 이미지 / CSV / 엑셀 파일 처리
    ├── paths.py            # 앱 데이터 파일 경로 관리
    └── prompt_manager.py   # 개인 프롬프트 CRUD + 가져오기/내보내기
```

---

## 인증 방식

1. 앱이 GitHub에 **디바이스 코드**를 요청합니다 (공개 Copilot OAuth App 클라이언트 ID `Iv1.b507a08c87ecfe98` 사용).
2. 브라우저에서 `https://github.com/login/device` 가 열리면 — 화면에 표시된 코드를 입력해 승인합니다.
3. 앱이 승인이 완료될 때까지 GitHub를 폴링하며, 완료 후 GitHub 토큰을 `~/.copilot_chatbot_token.json`에 저장합니다.
4. API를 호출할 때마다 GitHub 토큰을 단기 **Copilot API 베어러 토큰**(유효 시간 ~30분, 자동 갱신)과 교환합니다.

---

## 키보드 단축키

| 키 | 동작 |
|---|---|
| `Enter` | 메시지 전송 |
| `Shift + Enter` | 입력 창에서 줄 바꿈 |

---

## 개발 동기

작성자는 GitHub Copilot Pro+ 구독자(월 39달러 — 점심을 건너뛰며 낸 구독료)로,  
해당 구독에 이미 포함된 Claude, Gemini, GPT-4.1 모델을  
별도 유료 서비스에 가입하지 않고 편리한 데스크탑 GUI로 쓰고 싶어서 만들었습니다.
