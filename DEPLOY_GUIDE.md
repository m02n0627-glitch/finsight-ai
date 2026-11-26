# 🚀 FinSight AI — 5분 배포 가이드 (코딩 처음이어도 됩니다)

이 문서는 `app.py`(FinSight AI 웹 앱)를 **GitHub에 올리고 Streamlit Community Cloud에 배포해서
인터넷 주소(URL) 하나로 열리는 앱**으로 만드는 순서입니다.
터미널 명령을 몰라도 **① GitHub 웹 화면 + ② Streamlit 웹 화면**만으로 끝낼 수 있게 썼습니다.

> 최종 결과물: `https://finsight-ai-xxxx.streamlit.app` 같은 주소
> (이력서·포트폴리오에 `프로젝트 링크`로 넣을 수 있는 URL입니다)

---

## 0. 준비물 (2개)

| 준비물 | 걸리는 시간 | 비고 |
| --- | --- | --- |
| OpenDART 인증키 (40자리) | 2분 | 무료 · 하루 20,000건 호출 가능 |
| GitHub 계정 | 이미 있으면 0분 | 없으면 github.com에서 무료 가입 |

> 저장소는 **Public(공개)** 으로 만드는 것을 권합니다. 무료 플랜에서 배포가 가장 간단하고,
> 포트폴리오로 보여 주기에도 좋습니다. (인증키는 저장소에 올라가지 않습니다 — 6번 보안 체크 참고)

---

## 1. OpenDART 인증키 발급 (2분)

1. <https://opendart.fss.or.kr> 접속 → 회원가입 / 로그인
2. 상단 메뉴 **인증키 신청/관리** → **인증키 신청**
3. 신청 후 목록에 나오는 **40자리 영문·숫자 키**를 복사해 둡니다.

```
예) c1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0   ← 40자리
```

> ⚠️ 이 키는 **비밀번호처럼** 다루세요. 코드에 직접 적지 말고, 뒤에서 설명하는
> **Streamlit Secrets** 에 넣습니다. (앱은 키를 코드에서 읽지 않습니다)

---

## 2. GitHub에 저장소 만들기 (1분)

1. <https://github.com/new> 접속
2. 아래처럼 입력합니다.

| 항목 | 값 |
| --- | --- |
| Repository name | `finsight-ai` |
| Description | `OpenDART 사업보고서 기반 기업 재무·리스크 분석 웹 앱 (Streamlit)` |
| 공개 범위 | **Public** ✅ |
| Add a README file | 체크 해제 (직접 올립니다) |

3. **Create repository** 클릭

---

## 3. 파일 올리기 (2분)

저장소 화면에서 **Add file → Upload files** 를 누르고, 아래 파일을 끌어다 놓습니다.

| 올릴 파일 | 설명 |
| --- | --- |
| `app.py` | 👈 **Streamlit 앱 (배포 대상)** |
| `analyzer.py` | 분석 엔진 (OpenDART 호출·재무비율·리스크 계산) |
| `requirements.txt` | 필요한 패키지 목록 (streamlit, plotly, pandas) |
| `.gitignore` | 인증키·캐시 파일이 올라가지 않게 하는 목록 |
| `README.md` | 프로젝트 설명 (선택) |
| `DEPLOY_GUIDE.md` | 이 문서 (선택) |
| `.streamlit/config.toml` | 화면 테마 (선택) |
| `.streamlit/secrets.toml.example` | 인증키 형식 예시 (선택) |

> **`.streamlit/` 폴더는 폴더째 끌어다 놓으면 경로가 유지됩니다.**
> 폴더 업로드가 안 되면 `Add file → Create new file` 로 파일 이름 칸에
> `.streamlit/secrets.toml.example` 처럼 **슬래시를 포함해** 입력하고 내용을 붙여 넣으면 됩니다.
>
> 💡 **올리면 안 되는 파일**: `corp_codes.json`(자동 생성 캐시), `report_*.html`(실행 결과물),
> `.streamlit/secrets.toml`(실제 키). `.gitignore` 가 걸러 주지만 웹 업로드에서는 직접 고르지 마세요.

마지막에 **Commit changes** 를 누르면 저장소 완성입니다.

### (선택) 터미널이 편하다면 — git 명령으로 올리기

```bash
cd dart-analyzer                       # 파일이 있는 폴더
git init
git add app.py analyzer.py requirements.txt .gitignore README.md DEPLOY_GUIDE.md .streamlit
git commit -m "FinSight AI: OpenDART 기반 기업 재무·리스크 분석 웹 앱"
git branch -M main
git remote add origin https://github.com/<내아이디>/finsight-ai.git
git push -u origin main
```

> `git add .` 대신 **파일을 하나씩 지정**하면 캐시·결과물이 실수로 올라가지 않습니다.

---

## 4. Streamlit Community Cloud에 배포 (2분)

1. <https://share.streamlit.io> 접속 → **Continue with GitHub** 로 로그인
   (처음이면 GitHub 권한 승인 화면이 뜹니다 → **Authorize**)
2. 오른쪽 위 **Create app** → **Deploy a public app from GitHub**
3. 아래처럼 채웁니다.

| 항목 | 값 |
| --- | --- |
| Repository | `<내아이디>/finsight-ai` |
| Branch | `main` |
| Main file path | `app.py` |
| App URL | 원하는 주소 (예: `finsight-ai-moon`) — 비워 두면 자동 생성 |

4. **Advanced settings** 를 펼쳐서 두 가지를 설정합니다.

   **① Secrets** 칸에 아래를 그대로 붙여 넣고 `...` 부분을 내 키로 바꿉니다.

   ```toml
   DART_API_KEY = "여기에_발급받은_40자리_인증키"
   ```

   **② Python version** 은 `3.12` 를 고릅니다. (요즘 기본값이면 그대로 두어도 됩니다)

5. **Deploy** 클릭 → 1~3분 기다리면 앱이 뜹니다.

> 🔑 **Secrets 에 넣은 키는 GitHub에 올라가지 않습니다.** 앱은 실행될 때
> `st.secrets["DART_API_KEY"]` 로만 읽습니다. (키가 없으면 앱이 사이드바에 입력칸을 보여 줍니다)

---

## 5. 배포 확인 (30초)

1. 앱이 열리면 사이드바에 **🔐 인증키 설정됨 · Streamlit secrets** 이 보이는지 확인
2. 기본값(삼성전자 · 최근 3개년)으로 분석이 자동 실행됩니다.
   **첫 실행은 30초~1분** 걸립니다(기업 목록 3.6MB 내려받기 + 사업보고서 조회).
3. 아래 3개 탭이 모두 열리면 성공입니다.
   - ① 핵심 재무 & 추이 · ② FP&A 심층(DuPont & CCC) · ③ 리스크 상세 & DART 원문
4. 주소를 복사해 둡니다. (`https://<앱이름>.streamlit.app`)

> 앱을 며칠 안 쓰면 **절전 모드**로 들어갑니다. 다시 접속하면 깨어나는 데 10~30초 걸립니다. (정상)

---

## 6. 보안 체크 (꼭 한 번)

- [ ] 저장소에 `.streamlit/secrets.toml` 파일이 **없다** (예시 파일만 있다)
- [ ] `app.py` 안에 40자리 인증키가 **없다** (`grep -n "crtfc_key\|DART_API_KEY" app.py` 로 확인)
- [ ] `corp_codes.json`, `report_*.html` 이 저장소에 **없다**
- [ ] `.gitignore` 에 `secrets.toml`, `corp_codes.json`, `*.html`, `__pycache__/` 가 들어 있다

키가 실수로 올라갔다면 → OpenDART에서 **키 재발급** 후, Streamlit Secrets 값도 새 키로 바꿔 주세요.
(GitHub 기록에 남은 값은 지워도 복구 가능성이 있어, 재발급이 가장 안전합니다)

---

## 7. 자주 막히는 곳

| 증상 | 원인과 해결 |
| --- | --- |
| `ModuleNotFoundError: No module named 'streamlit'` | `requirements.txt` 를 안 올렸습니다. 파일을 추가하고 **Reboot app** |
| `인증키가 필요합니다` 안내가 뜸 | Secrets 이름이 `DART_API_KEY` 인지, 따옴표로 감쌌는지 확인 → **Reboot app** |
| `OpenDART 오류 [010] 등록되지 않은 인증키` | 키 앞뒤 공백·복사 누락. 값 다시 붙여넣기 |
| `오류 [020] 요청 제한 초과` | 하루 20,000건 초과. 다음 날 다시 (앱은 1시간 캐시라 보통 문제 없음) |
| 분석할 수 있는 연도가 부족합니다 | 사업보고서는 보통 **다음 해 3월** 공시. 연도를 하나 낮춰 선택 |
| 앱이 아주 느리거나 멈춘 듯 | 첫 실행(기업 목록 다운로드) 또는 절전 모드 해제 중. 30초~1분 기다리기 |
| 수정했는데 화면이 그대로 | 오른쪽 위 **Manage app → Reboot app** 또는 브라우저 새로고침(Ctrl+Shift+R) |
| 로그를 보고 싶다 | **Manage app** → 오른쪽 아래 터미널 로그 |

---

## 8. 이력서·포트폴리오에 쓰기 (예시)

```
FinSight AI — 상장사 재무·리스크 분석 웹 앱 (개인 프로젝트)
· 금융감독원 OpenDART API로 사업보고서 3개년 재무제표를 수집·정규화 (파이썬 표준 라이브러리)
· 5대 재무 리스크 신호등·100점 스코어, DuPont ROE 3단계 분해, 운전자본 CCC 자동 산출
· 최근 1년 공시 3,000건을 키워드로 스캔해 소송·횡령·감사의견 리스크 탐지
· Streamlit + Plotly 인터랙티브 대시보드로 배포 (단일 기업 심층 분석 + 경쟁사 비교 모드)
· 링크: https://<앱이름>.streamlit.app   코드: https://github.com/<아이디>/finsight-ai
```

> 면접에서 물어보기 좋은 포인트: ① 공시 100건 페이지 제한을 끝까지 넘긴 이유,
> ② 총자산회전율을 기말자산 기준으로 잡은 이유, ③ N/A 항목을 분모에서 뺀 점수 환산 방식.

---

## 9. 수정하고 다시 배포하기

1. GitHub에서 파일을 고치거나 새로 올립니다 (**Add file → Upload files** 로 덮어쓰기 가능)
2. Streamlit Cloud는 `main` 브랜치 변경을 감지해 **자동 재배포**합니다. (안 되면 **Reboot app**)

---

## 참고 링크

- OpenDART 오픈 API: <https://opendart.fss.or.kr/iam/api/main.do>
- Streamlit Community Cloud 문서: <https://docs.streamlit.io/deploy/streamlit-community-cloud>
- Streamlit Secrets 관리: <https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/secrets-management>
