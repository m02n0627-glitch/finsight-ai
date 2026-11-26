# -*- coding: utf-8 -*-
"""
FinSight v2 — 규칙 기반 기업 재무·리스크 분석 엔진 (중앙 집중형 UI)
================================================================================

app.py(사이드바 버전)는 기록·비교용으로 그대로 두고, 화면만 새로 설계한 버전이다.
분석 로직은 app.py와 똑같이 analyzer.py의 build_analysis() 등 기존 엔진 함수를 그대로 쓴다.

화면 구성 (요청한 순서 그대로)
    ① 상단 히어로 헤더 — FinSight 배지 + 한 줄 설명 (규칙 기반 엔진임을 함께 표기)
    ② 가운데 입력 카드
         · 기업 이름 검색/선택
         · ＋ 경쟁사 비교 추가 (체크하면 바로 아래에 비교 기업 검색/선택이 나타남)
         · 기준 연도 (multiselect, 기본 최근 3개년)
         · 맨 아래 가득 찬 🔎 분석 실행 버튼
    ③ 분석 실행 시 결과 영역 상단(#analysis-result)으로 부드러운 스크롤
       (버튼을 눌렀을 때만 — 단순 입력 변경으로는 화면이 튀지 않게 session_state 플래그로 제어)
    ④ 결과 — 종합 점수·공시 위험·한계 고지 + 4개 탭
       (핵심 재무·추이 / FP&A DuPont·CCC + 동종업계 비교 / 리스크·DART 원문 / FP&A 3개년 시나리오)

사이드바는 쓰지 않는다. set_page_config(initial_sidebar_state="collapsed") +
CSS로 사이드바 토글·여백을 없애고, 최대 너비를 가운데 정렬한다.

인증키는 (1) st.secrets["DART_API_KEY"] → (2) 환경변수 DART_API_KEY
→ (3) 키가 없을 때만 중앙 카드 하단 expander 입력 순서로 찾는다. 하드코딩하지 않는다.

실행
    streamlit run app_v2.py
"""

import contextlib
import html as html_lib
import io
import os
import shutil
import sys
import tempfile
import urllib.error
from datetime import datetime

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
import streamlit.components.v1 as components
from plotly.subplots import make_subplots

# 같은 폴더의 analyzer.py 를 엔진으로 불러온다.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import analyzer as fs  # noqa: E402  (FinSight 분석 엔진)
import excel_export as excel  # noqa: E402  (원클릭 엑셀 리포트)

# 기업 목록 캐시는 저장소가 아니라 임시 폴더에 둔다. (Streamlit Cloud 파일 시스템은 휘발성)
fs.CORP_CACHE_FILE = os.path.join(tempfile.gettempdir(), "finsight_corp_codes.json")

# 첫 실행에는 저장소에 동봉한 기업 목록 시드(공개 데이터)를 먼저 캐시로 복사한다.
# → corpCode.xml(3.6MB) 다운로드가 느리거나 막히는 환경(Streamlit Cloud 등)에서도 첫 화면이 멈추지 않는다.
CORP_SEED_PATH = os.path.join(BASE_DIR, "corp_codes_seed.json")
try:
    if os.path.exists(CORP_SEED_PATH) and not os.path.exists(fs.CORP_CACHE_FILE):
        shutil.copyfile(CORP_SEED_PATH, fs.CORP_CACHE_FILE)
except OSError:
    pass

st.set_page_config(
    page_title="FinSight · 규칙 기반 기업 재무·리스크 분석",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="collapsed",          # 사이드바 없는 중앙 집중형 레이아웃
)

# ---------------------------------------------------------------------------
# 0. 화면 상수
# ---------------------------------------------------------------------------

PALETTE = ["#111827", "#4F46E5"]                # 비교 모드에서 회사별 색 (다크 슬레이트 · 블루)
DART_KEY_URL = "https://opendart.fss.or.kr/uss/umt/EgovMberSbscrbView.do"
ANCHOR_ID = "analysis-result"                    # 결과 영역 스크롤 앵커
EXCEL_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
EXCEL_SHEETS_NOTE = "4개 시트 — 요약·리스크 / 재무제표·비율 / DuPont·CCC / 동종업계·공시"

# 검색어가 회사 정식 명칭과 다를 때 통하는 별칭 (초보자용)
ALIASES = {
    "현대차": "현대자동차",
    "현차": "현대자동차",
    "네이버": "NAVER",
    "삼전": "삼성전자",
    "하이닉스": "SK하이닉스",
    "엘지": "LG",
    "기아차": "기아",
    "포스코": "POSCO홀딩스",
}

# 검색어 없이 기본으로 보여 줄 추천 기업
POPULAR = ["삼성전자", "SK하이닉스", "현대자동차", "NAVER", "카카오", "LG에너지솔루션", "셀트리온"]

STATE_STYLE = {
    "safe": ("🟢", "안전", "#15803D", "#DCFCE7"),
    "caution": ("🟡", "주의", "#B45309", "#FEF3C7"),
    "danger": ("🔴", "위험", "#B91C1C", "#FEE2E2"),
    "na": ("⚪", "N/A", "#475569", "#F3F4F6"),
}

CORE_ROWS = [
    ("revenue", "매출액"),
    ("op_income", "영업이익"),
    ("net_income", "당기순이익"),
    ("assets", "자산총계"),
    ("liabilities", "부채총계"),
    ("equity", "자본총계"),
]

EXTRA_ROWS = [
    ("current_assets", "유동자산"),
    ("current_liab", "유동부채"),
    ("cost_of_sales", "매출원가"),
    ("receivables", "매출채권"),
    ("inventory", "재고자산"),
    ("payables", "매입채무"),
    ("op_cash_flow", "영업활동현금흐름"),
    ("interest_expense", "이자비용"),
]

RATIO_SERIES = [
    ("op_margin", "영업이익률", "#4F46E5", "solid"),
    ("net_margin", "순이익률", "#6B7280", "solid"),
    ("roe", "ROE", "#111827", "solid"),
    ("debt_ratio", "부채비율", "#9CA3AF", "dot"),
    ("current_ratio", "유동비율", "#D1D5DB", "dot"),
]

FEATURES = [
    ("리스크 점수 100점", "유동비율·부채비율·이자보상배율·현금흐름·부실예측"),
    ("DuPont ROE · CCC", "ROE 3요인 분해와 현금전환주기"),
    ("FP&A 3개년 시나리오", "매출·이익률·CCC 가정으로 손익·현금 민감도 비교"),
    ("공시 위험 · DART 원문", "최근 1년 공시 키워드와 사업보고서 원문 링크"),
]


# ---------------------------------------------------------------------------
# 1. 공통 유틸 (숫자·금액 표기, 엔진 호출)
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def quiet():
    """엔진의 콘솔 출력(print)을 웹 화면 로그로 흘리지 않게 잡아 둔다."""
    with contextlib.redirect_stdout(io.StringIO()):
        yield


def eok(value):
    """원 → 억원 (표·차트 단위)."""
    return None if value is None else value / 1e8


def row_eok(value):
    return None if value is None else round(value / 1e8)


def human_won(value):
    """금액을 사람이 읽기 좋은 단위로. (예: 333.6조원 / 12,345억원)"""
    if value is None:
        return "-"
    size = abs(value)
    if size >= 1e12:
        return "{:,.1f}조원".format(value / 1e12)
    if size >= 1e8:
        return "{:,.0f}억원".format(value / 1e8)
    return "{:,.0f}원".format(value)


def pct(value, digits=1):
    return "-" if value is None else ("{:,.%df}%%" % digits).format(value)


def num(value, digits=1, suffix=""):
    return "-" if value is None else ("{:,.%df}%s" % (digits, suffix)).format(value)


def account_amount(company, year, key):
    item = company["years"].get(year, {}).get("accounts", {}).get(key)
    return item.get("amount") if item else None


def ratio_of(company, year, key):
    return company["years"].get(year, {}).get("ratios", {}).get(key)


def state_badge(state):
    light, label, _color, _bg = STATE_STYLE.get(state, STATE_STYLE["na"])
    return "%s %s" % (light, label)


def text_labels(values, digits=0, suffix=""):
    """차트 위에 찍을 값 라벨 (None은 빈 문자열)."""
    return ["" if v is None else ("{:,.%df}%s" % (digits, suffix)).format(v) for v in values]


# ---------------------------------------------------------------------------
# 2. 인증키 · 기업 목록 · 분석 실행 (Streamlit 캐시)
# ---------------------------------------------------------------------------

def key_from_config():
    """(1) st.secrets → (2) 환경변수 순으로 인증키를 찾는다. 없으면 ("", "")."""
    try:
        secret = st.secrets.get("DART_API_KEY", "")
    except Exception:                      # secrets.toml 자체가 없을 때
        secret = ""
    if secret:
        return str(secret).strip(), "Streamlit secrets"
    env = os.environ.get("DART_API_KEY", "") or os.environ.get("OPENDART_API_KEY", "")
    if env:
        return env.strip(), "환경변수"
    return "", ""


def resolve_api_key():
    """
    인증키를 찾는 전체 순서.
        (1) st.secrets["DART_API_KEY"] → (2) 환경변수 DART_API_KEY
        → (3) 이 세션에서 중앙 카드 expander로 직접 입력한 키
    """
    api_key, source = key_from_config()
    if api_key:
        return api_key, source
    typed = str(st.session_state.get("fs2_api_key") or "").strip()
    if typed:
        return typed, "직접 입력"
    return "", ""


def remember_api_key(value):
    """expander로 받은 키를 위젯 상태가 아니라 세션 값으로 붙잡아 둔다. (다음 rerun에도 유지)"""
    value = str(value or "").strip()
    if value:
        st.session_state["fs2_api_key"] = value
    return value


@st.cache_data(ttl=6 * 60 * 60, show_spinner=False)
def load_corp_list(api_key):
    """상장사 corp_code 목록 (analyzer.sync_corp_codes). 6시간 캐시."""
    with quiet():
        return fs.sync_corp_codes(api_key, verbose=False)


@st.cache_data(ttl=60 * 60, show_spinner=False)
def run_analysis(api_key, corps, years):
    """
    analyzer.build_analysis 로 분석을 실행한다. (1시간 캐시 → 같은 조건 재조회 시 API 호출 0회)

    corps : (("삼성전자", "00126380", "005930"), ...) 튜플 (캐시 키로 쓰기 위해 불변형)
    """
    corp_dicts = [
        {"corp_name": name, "corp_code": code, "stock_code": stock}
        for name, code, stock in corps
    ]
    with quiet():
        return fs.build_analysis(api_key, corp_dicts, list(years), verbose=False)


def search_corps(corp_list, query, limit=60):
    """회사명 부분 일치 검색. 정확히 일치 → 보통주 → 짧은 이름 순."""
    text = (query or "").strip()
    if not text:
        return []
    text = ALIASES.get(text, text)
    key = fs.clean_name(text)
    if not key:
        return []
    hits = [c for c in corp_list if key in fs.clean_name(c["corp_name"])]
    hits.sort(key=lambda c: (
        fs.clean_name(c["corp_name"]) != key,
        not c["stock_code"].endswith("0"),
        len(c["corp_name"]),
        c["corp_name"],
    ))
    return hits[:limit]


def popular_corps(corp_list):
    """추천 기업 목록 (없는 회사는 건너뛴다)."""
    found = []
    for name in POPULAR:
        try:
            found.append(fs.find_company(corp_list, name))
        except fs.DartError:
            continue
    return found


def company_picker(corp_list, key, query_label, select_label, default_index=0, exclude_code=None):
    """검색어 입력 + 후보 selectbox. 고른 corp dict 를 돌려준다."""
    query = st.text_input(
        query_label,
        value="",
        key="fs2_%s_query" % key,
        placeholder="예: 삼성전자, SK하이닉스, 현대차, NAVER",
        help="이름 일부만 넣어도 됩니다. 비워 두면 추천 기업 목록에서 고를 수 있습니다.",
    )
    if query.strip():
        options = search_corps(corp_list, query)
        if not options:
            st.warning("'%s' 검색 결과가 없습니다. 회사명을 더 정확히 적어 주세요." % query.strip())
            options = popular_corps(corp_list)
    else:
        options = popular_corps(corp_list)

    labels = ["%s (%s)" % (c["corp_name"], c["stock_code"]) for c in options]
    index = min(default_index, len(options) - 1) if options else 0
    if exclude_code:                      # 비교 기업은 처음에 다른 회사가 골라져 있게
        for position, corp in enumerate(options):
            if corp["corp_code"] != exclude_code:
                index = position
                break
    picked = st.selectbox(select_label, labels, index=index, key="fs2_%s_select" % key)
    return options[labels.index(picked)]


def params_changed(current, saved):
    if not saved:
        return True
    if saved["api_key"] != current["api_key"]:
        return True
    if [c["corp_code"] for c in saved["corps"]] != [c["corp_code"] for c in current["corps"]]:
        return True
    return list(saved["years"]) != list(current["years"])


# ---------------------------------------------------------------------------
# 3. 화면 골격 — CSS · HTML 조각
# ---------------------------------------------------------------------------

APP_CSS = """
<style>
  @import url("https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/variable/pretendardvariable-dynamic-subset.min.css");
  /* ── 기본 타이포그래피 ─────────────────────────────────────────── */
  html, body, [class*="css"], [class*="st-"], [data-testid="stAppViewContainer"] {
      font-family: "Pretendard Variable", Pretendard, -apple-system, BlinkMacSystemFont,
                   "Apple SD Gothic Neo", "Malgun Gothic", sans-serif;
  }
  /* ── Streamlit 머티리얼 아이콘 폰트는 Pretendard에서 제외 ─────────────
     위 규칙의 [class*="css"]가 아이콘 span의 emotion 클래스까지 잡아
     폰트를 덮어쓰면 'keyboard_arrow_right' 같은 아이콘 이름이 글자로 노출된다.
     (expander 화살표·버튼 아이콘 등) 아이콘 요소만 아이콘 폰트로 되돌린다. */
  [data-testid="stIconMaterial"],
  [data-testid="stExpanderToggleIcon"],
  [data-testid="stExpanderStepChevron"],
  .material-symbols-rounded,
  .material-symbols-outlined,
  .material-icons {
      font-family: "Material Symbols Rounded", "Material Icons" !important;
      font-style: normal !important;
      font-weight: 400 !important;
      font-feature-settings: "liga" !important;
      -webkit-font-feature-settings: "liga" !important;
      letter-spacing: normal !important;
      line-height: 1 !important;
      text-transform: none !important;
      white-space: nowrap !important;
      word-wrap: normal !important;
      direction: ltr !important;
      -webkit-font-smoothing: antialiased !important;
  }
  .stApp {
      background: #FFFFFF;
  }
  .block-container {
      max-width: 1120px;
      padding-top: 2.2rem;
      padding-bottom: 4rem;
      margin: 0 auto;
  }
  h1, h2, h3, h4 { color: #111827; letter-spacing: -0.02em; }

  /* ── 사이드바 제거 (없는 사이드바의 토글·여백까지) ────────────── */
  [data-testid="stSidebar"],
  [data-testid="stSidebarNav"],
  [data-testid="stSidebarCollapsedControl"],
  [data-testid="collapsedControl"],
  button[data-testid="stSidebarCollapseButton"] { display: none !important; }
  header[data-testid="stHeader"] { background: transparent; }

  /* ── 히어로 헤더 (딥 차콜 카드 · 가운데 정렬) ─────────────────── */
  .fs-hero {
      background: #131315;
      border-radius: 22px;
      padding: 28px 28px 32px;
      margin: 0 0 22px 0;
      text-align: center;
  }
  .fs-hero-top {
      display: flex; align-items: baseline; justify-content: center;
      gap: 10px; margin-bottom: 22px;
  }
  .fs-hero-brand { font-size: 13px; font-weight: 700; color: #F9FAFB; letter-spacing: -0.01em; }
  .fs-hero-count { font-size: 11.5px; color: #6B7280; font-variant-numeric: tabular-nums; }
  .fs-hero-count::before { content: "·"; margin-right: 8px; color: #4B5563; }
  .fs-hero-label { font-size: 12.5px; color: #9CA3AF; margin-bottom: 8px; }
  .fs-hero-title {
      font-size: 32px; font-weight: 700; letter-spacing: -0.025em;
      line-height: 1.25; color: #FFFFFF;
      /* Pretendard Variable은 45~920 가변 폰트 — font-weight와 wght 값을 함께 700으로 맞춘다. */
      font-family: "Pretendard Variable", Pretendard, -apple-system, BlinkMacSystemFont,
                   "Apple SD Gothic Neo", "Malgun Gothic", sans-serif;
      font-variation-settings: "wght" 700;
      text-rendering: optimizeLegibility;
  }
  .fs-hero-title .accent { color: #7C83FF; }
  .fs-hero-sub {
      font-size: 13px; color: #9CA3AF; margin-top: 12px; line-height: 1.7;
  }

  /* ── 입력 카드 ──────────────────────────────────────────────── */
  .st-key-fs2-inputcard,
  .st-key-fs2-keycard {
      background: #FFFFFF;
      border: 1px solid #E5E7EB !important;
      border-radius: 18px !important;
      padding: 22px 24px 18px !important;
  }
  /* 같은 key 클래스가 안쪽 블록에도 붙는 경우 — 카드가 두 겹으로 겹치지 않게 */
  .st-key-fs2-inputcard .st-key-fs2-inputcard,
  .st-key-fs2-keycard .st-key-fs2-keycard,
  .st-key-fs2-inputcard [data-testid="stVerticalBlockBorderWrapper"],
  .st-key-fs2-keycard [data-testid="stVerticalBlockBorderWrapper"] {
      border: none !important; background: transparent !important;
      padding: 0 !important; border-radius: 0 !important; box-shadow: none !important;
  }
  .fs-card-title { font-size: 17px; font-weight: 800; color: #111827; letter-spacing: -0.02em; }
  .fs-card-sub { font-size: 12.5px; color: #6B7280; margin: 4px 0 16px 0; line-height: 1.55; }
  .fs-card-foot { font-size: 11.5px; color: #9CA3AF; margin-top: 10px; text-align: center; }

  /* ── 입력 위젯 다듬기 ────────────────────────────────────────── */
  [data-testid="stWidgetLabel"] p {
      font-size: 12.5px !important; font-weight: 700 !important; color: #374151 !important;
  }
  [data-testid="stTextInput"] div[data-baseweb="input"],
  [data-testid="stSelectbox"] div[data-baseweb="select"] > div,
  [data-testid="stMultiSelect"] div[data-baseweb="select"] > div {
      border-radius: 12px !important;
      border-color: #DEE4EF !important;
      background: #FBFCFE !important;
      min-height: 42px;
  }
  [data-testid="stTextInput"] div[data-baseweb="input"]:focus-within,
  [data-testid="stSelectbox"] div[data-baseweb="select"] > div:focus-within {
      border-color: #4F46E5 !important;
      box-shadow: 0 0 0 3px rgba(79, 70, 229, .12) !important;
  }
  [data-testid="stTextInput"] input { font-size: 14px; }
  [data-baseweb="popover"] li { font-size: 13.5px; }
  [data-testid="stCheckbox"] p { font-weight: 700; font-size: 13px; color: #1E293B; }

  /* ── 분석 실행 버튼 ──────────────────────────────────────────── */
  [data-testid="stBaseButton-primary"], button[kind="primary"] {
      height: 3.05rem;
      border-radius: 14px !important;
      font-size: 16px !important;
      font-weight: 800 !important;
      letter-spacing: -0.01em;
      border: none !important;
      background: #4F46E5 !important;
      box-shadow: none;
      transition: transform .14s ease, box-shadow .14s ease;
  }
  [data-testid="stBaseButton-primary"]:hover, button[kind="primary"]:hover {
      transform: translateY(-1px);
      box-shadow: none;
  }
  [data-testid="stBaseButton-secondary"] {
      border-radius: 12px !important; font-weight: 700 !important;
      border-color: #DEE4EF !important;
  }

  /* ── 랜딩 힌트 카드 ──────────────────────────────────────────── */
  .fs-hint {
      text-align: center; font-size: 13px; color: #6B7280; margin: 22px 0 14px 0;
  }
  .fs-hint-card {
      background: rgba(255,255,255,.72);
      border: 1px dashed #DCE3EF;
      border-radius: 18px;
      padding: 6px 20px;
  }
  .fs-hint-row {
      display: flex; align-items: flex-start; gap: 12px;
      padding: 13px 0; border-bottom: 1px dashed #E5E7EB;
  }
  .fs-hint-row:last-child { border-bottom: none; }
  .fs-hint-row b { display: block; font-size: 13px; color: #1E293B; }
  .fs-hint-row span.txt { font-size: 12px; color: #7A8798; }

  /* ── 결과 헤더 ───────────────────────────────────────────────── */
  .fs-result-head { padding: 2px 2px 0 2px; margin-bottom: 16px; }
  .fs-result-title {
      font-size: 22px; font-weight: 700; color: #111827; letter-spacing: -0.03em;
      line-height: 1.3;
  }
  .fs-result-title .vs { color: #D1D5DB; font-weight: 500; font-size: 17px; margin: 0 7px; }
  .fs-result-meta {
      font-size: 12px; color: #9CA3AF; margin-top: 6px; font-variant-numeric: tabular-nums;
  }
  .fs-result-meta a { color: #4F46E5; text-decoration: none; margin-left: 9px; }
  .fs-result-meta a:hover { text-decoration: underline; }

  /* ── 요약 카드 (기업별 핵심 숫자 4개) ─────────────────────────── */
  .fs-summary {
      background: #FFFFFF; border: 1px solid #E5E7EB; border-radius: 16px;
      padding: 18px 20px 20px; margin: 0 0 10px 0;
  }
  .fs-summary-head {
      display: flex; align-items: baseline; justify-content: space-between;
      gap: 10px; margin-bottom: 14px;
  }
  .fs-summary-name { font-size: 15px; font-weight: 700; color: #111827; }
  .fs-summary-code {
      font-size: 11.5px; font-weight: 500; color: #9CA3AF; margin-left: 7px;
      font-variant-numeric: tabular-nums;
  }
  .fs-mini-link { font-size: 12px; font-weight: 600; color: #4F46E5; text-decoration: none; }
  .fs-mini-link:hover { text-decoration: underline; }
  .fs-summary-grid {
      display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 16px;
  }
  .fs-metric-lbl { font-size: 11.5px; color: #9CA3AF; font-weight: 500; margin-bottom: 4px; }
  .fs-metric-val {
      font-size: 21px; font-weight: 700; color: #111827; letter-spacing: -0.02em;
      line-height: 1.25; font-variant-numeric: tabular-nums;
  }
  .fs-metric-note {
      font-size: 11.5px; color: #9CA3AF; margin-top: 3px; font-variant-numeric: tabular-nums;
  }

  /* ── 지표 카드 ───────────────────────────────────────────────── */
  .fs-kpi {
      background: #FFFFFF; border: 1px solid #E5E7EB; border-top-color: #E5E7EB !important;
      border-radius: 14px; padding: 14px 16px; min-height: 116px;
      display: flex; flex-direction: column; justify-content: space-between;
  }
  .fs-kpi .lbl { font-size: 11.5px; font-weight: 600; color: #9CA3AF; }
  .fs-kpi .val {
      font-size: 23px; font-weight: 700; color: #111827; margin-top: 6px;
      line-height: 1.16; font-variant-numeric: tabular-nums;
  }
  .fs-kpi .bar { height: 5px; border-radius: 999px; background: #F3F4F6; margin-top: 10px; overflow: hidden; }
  .fs-kpi .bar span { display: block; height: 100%; border-radius: 999px; }
  .fs-kpi .sub { font-size: 11.5px; color: #9CA3AF; margin-top: 8px; line-height: 1.5; }
  .fs-company-tag {
      display: inline-flex; align-items: center; gap: 7px;
      font-size: 12.5px; font-weight: 700; color: #374151; margin: 6px 0 10px 2px;
  }
  .fs-company-tag i { display: inline-block; width: 3px; height: 12px; border-radius: 3px; }

  /* ── 알림 · 고지 박스 ────────────────────────────────────────── */
  .fs-alert {
      border-radius: 12px; padding: 11px 14px; font-size: 12.5px; line-height: 1.6;
      margin: 14px 0 0 0; border: 1px solid #E5E7EB; background: #FFFFFF; color: #475569;
  }
  .fs-alert.danger { border-color: #F3DADA; color: #9B1C1C; }
  .fs-alert.info { border-color: #E5E7EB; color: #6B7280; }
  .fs-alert b { color: inherit; }


  /* ── 섹션 제목 · 인용 박스 ───────────────────────────────────── */
  .fs-section-title {
      font-size: 15px; font-weight: 700; color: #111827; margin: 26px 0 10px 0;
      letter-spacing: -0.02em;
  }
  .fs-note { font-size: 12px; color: #9CA3AF; line-height: 1.6; margin-bottom: 6px; }
  .fs-quote {
      border-left: 2px solid #E5E7EB; padding: 2px 0 2px 12px;
      font-size: 13px; color: #475569; line-height: 1.7; margin: 10px 0 4px 0;
  }

  /* ── 탭 ──────────────────────────────────────────────────────── */
  .stTabs [data-baseweb="tab-list"] {
      gap: 6px; border-bottom: 1px solid #E5E7EB; padding-bottom: 0;
  }
  .stTabs [data-baseweb="tab"] {
      height: 44px; padding: 0 16px; border-radius: 12px 12px 0 0;
      font-size: 13px; font-weight: 700; color: #6B7280; background: transparent;
  }
  .stTabs [data-baseweb="tab"]:hover { color: #4338CA; background: #F5F3FF; }
  .stTabs [aria-selected="true"] { color: #4338CA !important; background: #EEF2FF !important; }
  .stTabs [data-baseweb="tab-highlight"] { background-color: #4F46E5; }

  /* ── 표 · 차트 ───────────────────────────────────────────────── */
  div[data-testid="stDataFrame"] { border: 1px solid #E5E7EB; border-radius: 14px; overflow: hidden; }
  [data-testid="stMetricValue"] { font-size: 1.45rem; }

  /* ── 계정 표: 숫자가 DART 원문 링크 ──────────────────────────── */
  .fs-fintable {
      width: 100%; border-collapse: collapse; font-size: 13px;
      font-variant-numeric: tabular-nums; margin: 2px 0 6px 0;
  }
  .fs-fintable th, .fs-fintable td {
      padding: 7px 10px; border-bottom: 1px solid #F3F4F6; white-space: nowrap;
  }
  .fs-fintable thead th {
      background: #F9FAFB; color: #374151; font-size: 12px; font-weight: 800;
  }
  .fs-fintable tbody th { text-align: left; font-weight: 600; color: #374151; }
  .fs-fintable th + th, .fs-fintable td { text-align: right; }
  .fs-fintable tbody tr:hover { background: #F9FAFB; }
  .fs-fintable a.fs-num {
      color: #111827; text-decoration: none; border-bottom: 1px solid #E5E7EB;
      padding-bottom: 1px; transition: color .12s ease, border-color .12s ease;
  }
  .fs-fintable a.fs-num:hover { color: #4F46E5; border-bottom-color: #4F46E5; }
  .fs-fintable a.fs-year {
      color: #6B7280; text-decoration: none; border-bottom: 1px solid #E5E7EB;
  }
  .fs-fintable a.fs-year:hover { color: #4F46E5; border-bottom-color: #4F46E5; }
  .fs-fintable .fs-na { color: #9CA3AF; }

  /* ── 동종업계 비교 표 ──────────────────────────────────── */
  .fs-fintable .fs-peer-tag {
      margin-left: 6px; font-size: 11px; font-weight: 500; color: #9CA3AF;
  }
  .fs-fintable tr.fs-peer-target th, .fs-fintable tr.fs-peer-target td {
      background: #F5F3FF; color: #312E81; font-weight: 800;
  }
  .fs-fintable tr.fs-peer-target th { color: #312E81; }
  .fs-fintable tr.fs-peer-base th, .fs-fintable tr.fs-peer-base td {
      background: #FAFAFF; color: #4338CA; font-weight: 600;
  }
  .fs-fintable tr.fs-peer-base th { color: #4338CA; font-weight: 700; }
  footer, #MainMenu { visibility: hidden; }

  @media (max-width: 640px) {
      .block-container { padding-top: 1.2rem; }
      .fs-hero { padding: 24px 20px 26px; border-radius: 18px; }
      .fs-hero-top { margin-bottom: 18px; }
      .fs-hero-title { font-size: 24px; }
      .fs-result-title { font-size: 20px; }
      .st-key-fs2-inputcard, .st-key-fs2-keycard { padding: 18px 16px 16px !important; }
  }
</style>
"""

HERO_TMPL = """
<div class="fs-hero">
  <div class="fs-hero-top">
    <div class="fs-hero-brand">FinSight</div>
    <div class="fs-hero-count">OpenDART 사업보고서</div>
  </div>
  <div class="fs-hero-label">규칙 기반 기업 재무 분석</div>
  <div class="fs-hero-title">사업보고서 한 권을,<br><span class="accent">한 화면</span>의 근거로.</div>
  <div class="fs-hero-sub">리스크 점수 · DuPont ROE · CCC · 공시 위험을 DART 원문에서 바로 계산합니다.</div>
</div>
"""

DISCLAIMER_TMPL = (
    '과거 결산 재무제표와 최근 1년 공시 <b>제목 키워드</b>를 훑는 1차 스크리닝입니다. '
    '업종별 구조 차이(금융업·건설업 등), 금리·환율·업황 같은 향후 변수, 주가·시가총액은 반영하지 않습니다. '
    '5대 리스크 점수는 3단계 판정(20/10/0점)이라 같은 등급 안의 미세한 차이는 드러나지 않습니다. '
    '<b>투자 판단·신용평가가 아니며</b>, 최종 판단 전에는 DART 원문과 업종 비교군을 함께 확인하세요. '
    '모든 수치는 <b>연결재무제표 우선</b>(없으면 별도) · <b>기말잔액 기준</b>입니다. '
    '자본잠식(자본총계 0 이하)인 해는 부채비율·ROE가 왜곡되어 계산하지 않고 <b>자본잠식</b>으로 표시합니다.'
)

KPI_TMPL = (
    '<div class="fs-kpi" style="border-top-color:{accent}">'
    '<div class="lbl">{label}</div>'
    '<div class="val">{value}</div>'
    '{bar}'
    '<div class="sub">{sub}</div>'
    '</div>'
)


def card(label, value, sub="", accent="#4F46E5", bar=None, bar_color=None):
    """결과 상단 지표 카드 한 장 (진행 막대는 선택)."""
    bar_html = ""
    if bar is not None:
        width = max(0, min(100, int(round(bar))))
        bar_html = '<div class="bar"><span style="width:%d%%;background:%s"></span></div>' % (
            width, bar_color or accent)
    return KPI_TMPL.format(
        label=label, value=value, sub=sub, accent=accent, bar=bar_html)


def inject_css():
    st.markdown(APP_CSS, unsafe_allow_html=True)


def section_title(text, note=""):
    st.markdown('<div class="fs-section-title">%s</div>' % text, unsafe_allow_html=True)
    if note:
        st.markdown('<div class="fs-note">%s</div>' % note, unsafe_allow_html=True)


def quote_box(text):
    st.markdown('<div class="fs-quote">%s</div>' % text, unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# 4. 히어로 · 가운데 입력 카드
# ---------------------------------------------------------------------------

def render_hero():
    st.markdown(HERO_TMPL, unsafe_allow_html=True)


def card_container(key):
    """
    가운데 카드 컨테이너. key를 주면 Streamlit이 `st-key-<key>` CSS 클래스를 붙여
    CSS로 카드 모양(둥근 모서리·그림자·여백)을 입힐 수 있다.
    key 인자를 모르는 옛 Streamlit에서는 스타일만 빠지고 화면은 그대로 동작한다.
    """
    try:
        return st.container(key=key)
    except TypeError:
        return st.container()


def center_columns():
    """가운데 정렬용 3분할 (가운데 열에서 입력 카드를 그린다)."""
    return st.columns([1, 2.5, 1])


def render_landing_hint():
    """첫 화면 — 무엇을 해 주는 앱인지 짧게."""
    _left, middle, _right = center_columns()
    with middle:
        st.markdown('<div class="fs-hint">기업을 고르고 <b>분석 실행</b>을 누르면 결과가 아래에 펼쳐집니다.</div>',
                    unsafe_allow_html=True)
        rows = "".join(
            '<div class="fs-hint-row"><div><b>%s</b><span class="txt">%s</span></div></div>'
            % (title, note)
            for title, note in FEATURES
        )
        st.markdown('<div class="fs-hint-card">%s</div>' % rows, unsafe_allow_html=True)


def render_key_gate():
    """인증키가 secrets·환경변수 어디에도 없을 때 — 중앙 카드 하단 expander에서 직접 입력."""
    _left, middle, _right = center_columns()
    with middle:
        with card_container("fs2-keycard"):
            st.markdown(
                '<div class="fs-card-title">OpenDART 인증키가 필요합니다</div>'
                '<div class="fs-card-sub">금융감독원 OpenDART 인증키가 필요합니다. 무료로 발급받아 아래에 붙여 넣어 주세요.</div>',
                unsafe_allow_html=True,
            )
            with st.expander("OpenDART 인증키 입력", expanded=True):
                api_key = st.text_input(
                    "인증키 (40자리)",
                    type="password",
                    placeholder="40자리 인증키를 붙여 넣으세요",
                    key="fs2_key_input",
                    help="Streamlit Cloud에서는 App settings → Secrets 에 DART_API_KEY 를 넣어 두면 이 칸이 필요 없습니다.",
                ).strip()
                if api_key and len(api_key) != 40:
                    st.warning("인증키는 보통 40자리입니다. 지금 입력한 키는 %d자리입니다." % len(api_key))
                st.caption("발급: [OpenDART 인증키 신청](%s) · 키는 저장하지 않고 이 브라우저 세션에서만 씁니다." % DART_KEY_URL)
            st.markdown('<div class="fs-card-foot">키를 넣으면 기업 검색이 열립니다.</div>',
                        unsafe_allow_html=True)
    return api_key


def render_corp_load_error(error):
    _left, middle, _right = center_columns()
    with middle:
        with card_container("fs2-keycard"):
            st.markdown('<div class="fs-card-title">기업 목록을 불러오지 못했습니다</div>'
                        '<div class="fs-card-sub">인증키 상태나 네트워크를 확인해 주세요. '
                        'OpenDART는 하루 20,000건까지 호출할 수 있습니다.</div>',
                        unsafe_allow_html=True)
            st.error(str(error))
            if st.button("↻ 다시 시도", key="fs2_retry_corp"):
                load_corp_list.clear()
                st.rerun()


def render_input_card(corp_list, api_key, key_source):
    """
    가운데 입력 카드를 그리고 (설정, 버튼 눌림, 유효 여부)를 돌려준다.

    카드 안 순서 (사용자 지정 구조)
        ① 기업 이름 검색/선택
        ② ＋ 경쟁사 비교 추가 → 체크하면 바로 아래에 비교 기업 검색/선택
        ③ 기준 연도 (기본 최근 3개년)
        ④ 맨 아래 🔎 분석 실행 (가득 찬 primary 버튼)
        ⑤ 카드 하단 — 인증키 상태(없을 때만 입력 expander)
    """
    this_year = datetime.now().year
    year_options = list(range(this_year - 9, this_year))
    default_years = [y for y in (this_year - 3, this_year - 2, this_year - 1) if y in year_options]

    clicked = False
    valid = True
    main_corp, second_corp, years = None, None, []

    _left, middle, _right = center_columns()
    with middle:
        with card_container("fs2-inputcard"):
            st.markdown(
                '<div class="fs-card-title">분석할 기업을 골라 주세요</div>'
                '<div class="fs-card-sub">한 기업만 봐도 되고, 경쟁사와 나란히 비교할 수도 있습니다.</div>',
                unsafe_allow_html=True,
            )

            main_corp = company_picker(corp_list, "main", "기업명 검색", "기업 선택")

            compare = st.checkbox(
                "＋ 경쟁사 비교 추가",
                value=False,
                key="fs2_compare",
                help="켜면 비교 기업 1곳을 골라 두 회사를 나란히 분석합니다.",
            )
            if compare:
                second_corp = company_picker(
                    corp_list, "peer", "비교 기업명 검색", "비교 기업 선택",
                    exclude_code=main_corp["corp_code"],
                )

            years = st.multiselect(
                "기준 연도 (2개 이상)",
                year_options,
                default=default_years,
                key="fs2_years",
                help="사업보고서는 보통 다음 해 3월에 공시됩니다. 공시가 없는 연도는 자동으로 제외됩니다.",
            )

            _space, button_column, _space2 = st.columns([1, 2, 1])
            with button_column:
                clicked = st.button(
                    "분석 실행",
                    type="primary",
                    use_container_width=True,
                    key="fs2_run",
                )

            # ── 카드 하단: 인증키 상태(없을 때만 입력) ───────────────────
            if api_key:
                st.markdown(
                    '<div class="fs-card-foot">🔐 인증키 연결됨 · %s</div>' % key_source,
                    unsafe_allow_html=True,
                )
            else:
                with st.expander("OpenDART 인증키 직접 입력", expanded=False):
                    typed = st.text_input(
                        "인증키 (40자리)",
                        type="password",
                        placeholder="40자리 인증키를 붙여 넣으세요",
                        key="fs2_key_input",
                        help="Streamlit Cloud에서는 App Secrets 에 DART_API_KEY 를 넣어 두면 이 칸이 필요 없습니다.",
                    ).strip()
                    st.caption("발급: [OpenDART 인증키 신청](%s) · 인증키를 넣고 다시 분석 실행을 눌러 주세요." % DART_KEY_URL)
                if typed and len(typed) != 40:
                    st.warning("인증키는 보통 40자리입니다. 지금 입력한 키는 %d자리입니다." % len(typed))
                if typed:
                    api_key = remember_api_key(typed)

    corps = [main_corp] + ([second_corp] if second_corp else [])
    if second_corp and second_corp["corp_code"] == main_corp["corp_code"]:
        st.error("비교 기업이 같습니다. 다른 회사를 골라 주세요.")
        valid = False
    if len(years) < 2:
        st.error("기준 연도를 2개 이상 선택해 주세요. (연도별 추이·증감률 계산에 필요합니다)")
        valid = False
    if not api_key:
        st.warning("OpenDART 인증키를 입력해야 분석할 수 있습니다.")
        valid = False

    settings = {
        "api_key": api_key,
        "corps": corps,
        "years": sorted(years),
    }
    return settings, clicked, valid


# ---------------------------------------------------------------------------
# 5. 결과 헤더 · 종합 점수 · 공시 위험 알림
# ---------------------------------------------------------------------------

def render_result_head(analysis):
    companies = analysis["companies"]
    years = analysis["years"]

    title = ' <span class="vs">vs</span> '.join(html_lib.escape(c["name"]) for c in companies)
    fs_names = sorted({c.get("fs_nm") or "-" for c in companies})
    meta = "%d–%d 사업보고서 · %s" % (years[0], years[-1], " / ".join(fs_names))

    # 원문 링크는 여기에 두지 않는다 — 바로 아래 기업 카드 헤더의 '{최신연도} 원문 ↗'
    # (재무제표 직행 viewer.do) 하나로 모아, 같은 원문 링크가 두 번 뜨지 않게 한다.
    st.markdown(
        '<div class="fs-result-head">'
        '<div class="fs-result-title">%s</div>'
        '<div class="fs-result-meta">%s</div>'
        '</div>' % (title, meta),
        unsafe_allow_html=True,
    )


def disclosure_headline(company):
    """공시 위험 요약 문구 (표시용)."""
    data = company.get("disclosure") or {}
    status = data.get("status")
    matches = data.get("matches") or []
    if status == "clear":
        return "0건", "최근 1년 위험 키워드 공시 없음", "#15803D"
    if status == "ok":
        severe = sum(1 for m in matches if m["severity"] == "severe")
        caution = len(matches) - severe
        sub = "심각 %d건 · 주의 %d건" % (severe, caution)
        accent = "#B91C1C" if severe else "#B45309"
        return "%d건" % len(matches), sub, accent
    return "조회 실패", (data.get("error") or "")[:60] or "공시검색 API 호출 실패", "#6B7280"


def metric_block(label, value, note="", note_color=""):
    """요약 카드 안 숫자 하나 — 라벨(작게) / 숫자(크게) / 한 줄 설명(작게)."""
    note_style = ' style="color:%s"' % note_color if note_color else ""
    note_html = '<div class="fs-metric-note"%s>%s</div>' % (note_style, note) if note else ""
    return ('<div class="fs-metric"><div class="fs-metric-lbl">%s</div>'
            '<div class="fs-metric-val">%s</div>%s</div>' % (label, value, note_html))


def render_summary(analysis):
    """상단 요약 — 기업별 핵심 숫자 4개와 원문 링크만 남긴다."""
    companies = analysis["companies"]
    years = analysis["years"]
    latest = years[-1]

    for company in companies:
        risk = company["risk"]
        light, grade_label, grade_color, _bg = STATE_STYLE[risk["grade"]]
        d_value, d_sub, _d_accent = disclosure_headline(company)
        revenue = account_amount(company, latest, "revenue")
        revenue_growth = ratio_of(company, latest, "revenue_growth")
        op_margin = ratio_of(company, latest, "op_margin")
        prev_margin = ratio_of(company, years[-2], "op_margin") if len(years) >= 2 else None
        margin_diff = (op_margin - prev_margin) if (op_margin is not None and prev_margin is not None) else None

        # 리스크는 위험 신호(🔴/🟡)에만 색을 준다. 안전은 무채색.
        risky = risk["grade"] in ("danger", "caution")
        risk_note = ("%s %s" % (light, grade_label)) if risky else grade_label
        if risk.get("capital_impaired"):        # 자본잠식은 점수와 별개로 먼저 보이게
            risk_note = "자본잠식 · " + risk_note
        disclosure_note = d_sub if (company.get("disclosure") or {}).get("status") == "ok" else ""

        # 카드 헤더의 원문 링크 한 개로 모았다(상단 헤더의 같은 링크는 제거).
        # 손익계산서 직행(viewer.do) → 시드·라이브 실패 시 사업보고서 표지(main.do) 순서로 물러난다.
        url, tip = statement_target(company, latest, "income")
        if url:
            source = ('<a class="fs-mini-link" href="%s" target="_blank" rel="noopener noreferrer" '
                      'title="%s">%d 원문 ↗</a>'
                      % (html_lib.escape(url, quote=True),
                         html_lib.escape(tip, quote=True),
                         latest))
        else:
            source = ""

        grid = "".join([
            metric_block("%d년 리스크" % latest, "%d점" % risk["score"], risk_note,
                         grade_color if risky else ""),
            metric_block("%d년 매출액" % latest, human_won(revenue),
                         "전년 대비 %s" % (num(revenue_growth, 1, "%") if revenue_growth is not None else "-")),
            metric_block("영업이익률", pct(op_margin),
                         "전년 대비 %s" % (num(margin_diff, 1, "%p") if margin_diff is not None else "-")),
            metric_block("최근 1년 공시 위험", d_value, disclosure_note),
        ])

        st.markdown(
            '<div class="fs-summary">'
            '<div class="fs-summary-head">'
            '<div class="fs-summary-name">%s<span class="fs-summary-code">%s</span></div>'
            '%s</div>'
            '<div class="fs-summary-grid">%s</div>'
            '</div>' % (html_lib.escape(company["name"]), html_lib.escape(company.get("stock_code") or ""),
                        source, grid),
            unsafe_allow_html=True,
        )
    st.write("")


def render_disclosure_alert(companies):
    """공시 위험 알림 배너 — 잡힌 게 있을 때만, 조용히 한 줄."""
    hits, failures = [], []
    for company in companies:
        data = company.get("disclosure") or {}
        if data.get("status") == "ok":
            matches = data.get("matches") or []
            severe = sum(1 for m in matches if m["severity"] == "severe")
            hits.append("%s %d건(심각 %d건)" % (company["name"], len(matches), severe))
        elif data.get("status") == "error":
            failures.append(company["name"])

    if hits:
        danger = any(
            (m["severity"] == "severe")
            for company in companies
            for m in ((company.get("disclosure") or {}).get("matches") or [])
        )
        st.markdown(
            '<div class="fs-alert%s"><b>공시 위험 알림</b> — %s. '
            '<b>③ 리스크 상세</b> 탭에서 원문을 확인하세요.</div>'
            % (" danger" if danger else "", ", ".join(hits)),
            unsafe_allow_html=True,
        )
    elif failures:
        st.markdown(
            '<div class="fs-alert info"><b>공시 위험 조회 실패</b> — %s. 재무 분석은 그대로 진행되었습니다.</div>'
            % ", ".join(failures),
            unsafe_allow_html=True,
        )


# ---------------------------------------------------------------------------
# 6. 표 만들기
# ---------------------------------------------------------------------------

def build_table(companies, years, rows, value_getter, digits=1):
    """companies × years × rows 를 한 표로. (비교 모드면 열 이름이 '회사 연도')"""
    data = {}
    for company in companies:
        for year in years:
            column = "%s %d" % (company["name"], year) if len(companies) > 1 else str(year)
            data[column] = [value_getter(company, year, key) for key, _label in rows]
    index = [label for _key, label in rows]
    return pd.DataFrame(data, index=index)


def show_table(df, digits=1, height=None):
    # 값이 없는 칸이 'None'으로 보이지 않게 숫자형으로 맞춘다 → 화면에는 빈칸(－ 없음)으로 표시된다.
    # (한 열이 전부 None이면 pandas가 object 열로 잡아 'None' 글자가 그대로 찍히던 문제)
    df = df.apply(pd.to_numeric, errors="coerce")
    # 천 단위 구분자: 억원 금액 2,589,355 / 비율 13.1 / 배수 1.30 모두 쉼표 형식으로 보여 준다.
    number_format = "%,.0f" if digits == 0 else ("%,." + str(digits) + "f")
    config = {
        column: st.column_config.NumberColumn(column, format=number_format)
        for column in df.columns
    }
    if height:
        st.dataframe(df, column_config=config, height=height)
    else:
        st.dataframe(df, column_config=config)


def table_financials(companies, years, rows, title="재무제표 (단위: 억원)"):
    # title을 비우면(None) 제목 없이 표만 그린다 — 위에서 section_title()로 이미 제목을 낸 경우.
    if title:
        st.markdown("**%s**" % title)
    st.markdown(financial_table_html(companies, years, rows), unsafe_allow_html=True)
    st.caption("단위 억원 · 숫자를 클릭하면 그 해 DART 원문이 새 탭으로 열립니다")


# ---------------------------------------------------------------------------
# 6-1. 계정 표 — 숫자를 클릭하면 그 해 DART 재무제표 원문이 새 탭으로 열린다
# ---------------------------------------------------------------------------

# 계정 → 어느 재무제표 원문(손익계산서 / 재무상태표 / 현금흐름표)으로 보낼지
ACCOUNT_STATEMENT = {
    "revenue": "income", "op_income": "income", "net_income": "income",
    "cost_of_sales": "income", "interest_expense": "income",
    "assets": "balance", "current_assets": "balance", "liabilities": "balance",
    "current_liab": "balance", "equity": "balance", "retained_earnings": "balance",
    "receivables": "balance", "inventory": "balance", "payables": "balance",
    "op_cash_flow": "cashflow",
}


# 표에 보여 줄 재무제표 이름 — 링크에 실린 목차 이름, 없으면 기본 이름
STATEMENT_FALLBACK_LABEL = {"income": "손익계산서", "balance": "재무상태표", "cashflow": "현금흐름표"}


def statements_of(company, year):
    """그 해 DART 재무제표 원문 링크 묶음 (없으면 빈 dict)."""
    if not company:
        return {}
    links = (company.get("statement_links") or {}).get(year)
    if links:
        return links
    return ((company.get("years") or {}).get(year) or {}).get("statement_links") or {}


def statement_target(company, year, key):
    """계정 키 → (원문 URL, 툴팁). 링크를 만들 수 없으면 ('', '').

    재무제표 한 장 직행(viewer.do)이 있으면 그 링크를, 없으면 사업보고서 표지(main.do)를 돌려준다.
    표지로 물러난 경우에는 툴팁도 '사업보고서 원문'으로 맞춘다(손익계산서라고 적지 않는다).
    """
    kind = ACCOUNT_STATEMENT.get(key, "income")
    links = statements_of(company, year)
    url = links.get(kind) or links.get("main") or ""
    if url:
        found_label = ((links.get("labels") or {}).get(kind) or "").strip()
        main_url = links.get("main") or ""
        if found_label:
            tip = "%d %s 원문 열기" % (year, found_label)
        elif main_url and url == main_url:      # 시드·라이브 실패 → 표지(main.do)
            tip = "%d 사업보고서 원문 열기" % year
        else:
            tip = "%d %s 원문 열기" % (year, STATEMENT_FALLBACK_LABEL.get(kind, "재무제표"))
        return url, tip
    # 재무제표별 링크가 없으면(구버전 엔진·목차 파싱 실패) 사업보고서 첫 화면으로
    rcept_no = ((company.get("years") or {}).get(year) or {}).get("rcept_no") or ""
    url = fs.dart_link(rcept_no)
    if not url:
        return "", ""
    return url, "%d 사업보고서 원문 열기" % year


def statement_anchor(company, year, key, text):
    """숫자 하나를 원문 링크로 감싼다. 링크가 없으면 숫자만 둔다."""
    url, tip = statement_target(company, year, key)
    if not url:
        return text
    return ('<a class="fs-num" href="%s" target="_blank" rel="noopener noreferrer" title="%s">%s</a>'
            % (html_lib.escape(url, quote=True), html_lib.escape(tip, quote=True), text))


def financial_table_html(companies, years, rows):
    """핵심 재무제표·추가 계정 표 — 모든 숫자가 그 해 DART 원문 링크다."""
    multi = len(companies) > 1
    head = ['<tr><th class="fs-rowhead">계정 (단위: 억원)</th>']
    for company in companies:
        for year in years:
            links = statements_of(company, year)
            rcept_no = ((company.get("years") or {}).get(year) or {}).get("rcept_no") or ""
            main_url = links.get("main") or fs.dart_link(rcept_no)
            name = "%s %d" % (company["name"], year) if multi else "%d" % year
            if main_url:
                tip = "%d %s 사업보고서 원문 열기" % (year, company["name"])
                head.append('<th><a class="fs-year" href="%s" target="_blank" '
                            'rel="noopener noreferrer" title="%s">%s</a></th>'
                            % (html_lib.escape(main_url, quote=True),
                               html_lib.escape(tip, quote=True), html_lib.escape(name)))
            else:
                head.append("<th>%s</th>" % html_lib.escape(name))
    head.append("</tr>")

    body = []
    for key, label in rows:
        cells = ['<th scope="row">%s</th>' % html_lib.escape(label)]
        for company in companies:
            for year in years:
                value = row_eok(account_amount(company, year, key))
                if value is None:
                    cells.append('<td><span class="fs-na">-</span></td>')
                else:
                    cells.append('<td>%s</td>' % statement_anchor(
                        company, year, key, "{:,.0f}".format(value)))
        body.append("<tr>" + "".join(cells) + "</tr>")

    return ('<table class="fs-fintable"><thead>' + "".join(head) + "</thead><tbody>"
            + "".join(body) + "</tbody></table>")


def table_ratios(companies, years):
    rows = [(key, "%s · %s" % (group, label))
            for key, label, _unit, group in fs.RATIO_LABELS]
    df = build_table(companies, years, rows, ratio_of)
    show_table(df, digits=1)
    st.caption("단위 % · 성장률은 전년 대비")
    impaired = ["%s %d" % (c["name"], y) for c in companies for y in years
                if fs.capital_impaired_info(c, y)["impaired"]]
    if impaired:
        st.caption("자본잠식(자본총계 0 이하): %s — 부채비율·ROE는 음수 자본으로 왜곡되어 비워 둡니다."
                   % ", ".join(impaired))


def table_dupont(companies, years):
    rows = [
        ("net_margin", "순이익률 (%)"),
        ("asset_turnover", "총자산회전율 (회)"),
        ("equity_multiplier", "재무레버리지 (배)"),
        ("roe", "DuPont ROE (%)"),
        ("roe_simple", "단순 ROE (%) — 검산"),
    ]
    df = build_table(companies, years, rows,
                     lambda company, year, key: (company["years"][year].get("dupont") or {}).get(key))
    show_table(df, digits=2)
    st.caption("총자산회전율은 기말 자산 기준 · DuPont ROE는 단순 ROE와 0.01%p 이내로 일치해야 정상")


def table_ccc(companies, years):
    rows = [
        ("dso", "DSO — 매출채권 회수 (일)"),
        ("dio", "DIO — 재고 보유 (일)"),
        ("dpo", "DPO — 매입채무 지급 (일)"),
        ("ccc", "CCC — 현금전환주기 (일)"),
    ]
    df = build_table(companies, years, rows,
                     lambda company, year, key: (company["years"][year].get("ccc") or {}).get(key))
    show_table(df, digits=0)
    st.caption("기말잔액 · 365일 기준 · 금융업 등은 비어 있을 수 있습니다")


def peer_metric_cells(values):
    """지표 하나하나를 표 셀 문자열로. 값이 없으면 흐린 '–'."""
    cells = []
    for key, _label, unit, _better, digits in fs.PEER_METRICS:
        text = fs.format_peer_value((values or {}).get(key), unit, digits)
        if text == "-":
            cells.append('<td><span class="fs-na">-</span></td>')
        else:
            cells.append("<td>%s</td>" % html_lib.escape(text))
    return "".join(cells)


def industry_peer_table(peer_block):
    """
    동종업계 비교 표 — 행: 대상 · 피어 평균 · 피어 중앙값 · 개별 피어 / 열: 5개 지표.

    피어가 2~3개사뿐이라 한 곳의 이상치가 평균을 끌고 간다. 그래서 중앙값을 함께 둔다.
    """
    head = ['<tr><th class="fs-rowhead">기업 (%d년)</th>' % peer_block["year"]]
    for _key, label, unit, _better, _digits in fs.PEER_METRICS:
        head.append("<th>%s%s</th>" % (html_lib.escape(label), "" if unit == "%" else " <span class=\"fs-peer-tag\">%s</span>" % html_lib.escape(unit)))
    head.append("</tr>")

    count = len(peer_block["peers"])
    body = []
    body.append('<tr class="fs-peer-target"><th scope="row">%s<span class="fs-peer-tag">대상</span></th>%s</tr>'
                % (html_lib.escape(peer_block["name"]), peer_metric_cells(peer_block["target_values"])))

    average = {row["key"]: row["mean"] for row in peer_block["rows"]}
    median = {row["key"]: row["median"] for row in peer_block["rows"]}
    body.append('<tr class="fs-peer-base"><th scope="row">피어 평균<span class="fs-peer-tag">%d개사</span></th>%s</tr>'
                % (count, peer_metric_cells(average)))
    body.append('<tr class="fs-peer-base"><th scope="row">피어 중앙값<span class="fs-peer-tag">%d개사</span></th>%s</tr>'
                % (count, peer_metric_cells(median)))

    for peer in peer_block["peers"]:
        body.append('<tr><th scope="row">%s<span class="fs-peer-tag">%s</span></th>%s</tr>'
                    % (html_lib.escape(peer["name"]), html_lib.escape(peer["match_label"]),
                       peer_metric_cells(peer.get("metrics"))))

    return ('<table class="fs-fintable fs-peertable"><thead>' + "".join(head)
            + "</thead><tbody>" + "".join(body) + "</tbody></table>")


def table_risk(company):
    rows = []
    for item in company["risk"]["items"]:
        rows.append({
            "항목": item["label"],
            "판정": state_badge(item["state"]),
            "측정값": item["value_text"],
            "판정 기준": item["criteria"],
            "메모": item["note"] or "-",
        })
    st.dataframe(pd.DataFrame(rows), hide_index=True)


def table_reports(company, years):
    rows = []
    for year in years:
        entry = company["years"][year]
        rcept_no = entry.get("rcept_no") or ""
        rows.append({
            "사업연도": year,
            "재무제표": "연결" if entry.get("fs_div") == "CFS" else ("별도" if entry.get("fs_div") == "OFS" else "-"),
            "접수번호": rcept_no or "-",
            "공시일": fs.rcept_date(rcept_no) or "-",
            "원문": fs.dart_link(rcept_no),
        })
    st.dataframe(
        pd.DataFrame(rows),
        hide_index=True,
        column_config={"원문": st.column_config.LinkColumn("원문", display_text="DART에서 열기")},
    )


# ---------------------------------------------------------------------------
# 7. 차트 만들기 (Plotly)
# ---------------------------------------------------------------------------

def style_fig(fig, height=380, legend=True):
    fig.update_layout(
        template="plotly_white",
        height=height,
        # 범례를 차트 아래로 내리고 위·아래 여백을 넓혀 소제목("매출액 추이" 등)과 겹치지 않게 한다.
        margin=dict(l=10, r=14, t=58, b=84),
        font=dict(family="'Pretendard Variable', Pretendard, -apple-system, BlinkMacSystemFont, "
                         "'Apple SD Gothic Neo', 'Malgun Gothic', sans-serif",
                  size=12, color="#374151"),
        hoverlabel=dict(bgcolor="white", font_size=12),
        plot_bgcolor="white",
        bargap=0.28,
    )
    if legend:
        fig.update_layout(legend=dict(orientation="h", yanchor="top", y=-0.22, x=0,
                                      font=dict(size=11), bgcolor="rgba(0,0,0,0)"))
    fig.update_xaxes(showgrid=False, linecolor="#E5E7EB")
    # 억원 단위 값이 '3.5M' 같은 축약 표기 대신 '3,500,000'으로 보이게 한다.
    fig.update_yaxes(gridcolor="#EEF2F6", zerolinecolor="#D1D5DB",
                     tickformat=",", exponentformat="none", separatethousands=True)
    return fig


def chart_income(companies, years):
    """손익 추이 — 1개사는 막대, 비교 모드는 선 그래프."""
    xs = [str(y) for y in years]

    if len(companies) == 1:
        company = companies[0]
        revenue = [eok(account_amount(company, y, "revenue")) for y in years]
        op_income = [eok(account_amount(company, y, "op_income")) for y in years]
        net_income = [eok(account_amount(company, y, "net_income")) for y in years]

        fig = make_subplots(rows=1, cols=2, horizontal_spacing=0.13,
                            subplot_titles=("매출액 (억원)", "영업이익 · 당기순이익 (억원)"))
        fig.add_trace(go.Bar(x=xs, y=revenue, name="매출액", marker_color="#111827",
                             text=text_labels(revenue), textposition="outside",
                             cliponaxis=False), row=1, col=1)
        fig.add_trace(go.Bar(x=xs, y=op_income, name="영업이익", marker_color="#4F46E5",
                             text=text_labels(op_income), textposition="outside",
                             cliponaxis=False), row=1, col=2)
        fig.add_trace(go.Bar(x=xs, y=net_income, name="당기순이익", marker_color="#9CA3AF",
                             text=text_labels(net_income), textposition="outside",
                             cliponaxis=False), row=1, col=2)
        fig.update_layout(barmode="group")
        return style_fig(fig, height=400)

    fig = make_subplots(rows=1, cols=2, horizontal_spacing=0.12,
                        subplot_titles=("매출액 추이 (억원)", "수익성 (%)"))
    for index, company in enumerate(companies):
        color = PALETTE[index % 2]
        fig.add_trace(go.Scatter(
            x=xs, y=[eok(account_amount(company, y, "revenue")) for y in years],
            name="%s 매출액" % company["name"], mode="lines+markers",
            line=dict(color=color, width=3), marker=dict(size=8)), row=1, col=1)
        fig.add_trace(go.Scatter(
            x=xs, y=[ratio_of(company, y, "op_margin") for y in years],
            name="%s 영업이익률" % company["name"], mode="lines+markers",
            line=dict(color=color, width=2.5), marker=dict(size=7)), row=1, col=2)
        fig.add_trace(go.Scatter(
            x=xs, y=[ratio_of(company, y, "net_margin") for y in years],
            name="%s 순이익률" % company["name"], mode="lines+markers",
            line=dict(color=color, width=2, dash="dot"), marker=dict(size=6)), row=1, col=2)
    return style_fig(fig, height=400)


def chart_balance(company, years):
    """재무상태 — 부채·자본 누적 막대 + 부채비율 선."""
    xs = [str(y) for y in years]
    liabilities = [eok(account_amount(company, y, "liabilities")) for y in years]
    equity = [eok(account_amount(company, y, "equity")) for y in years]
    debt_ratio = [ratio_of(company, y, "debt_ratio") for y in years]

    fig = make_subplots(specs=[[{"secondary_y": True}]])
    fig.add_trace(go.Bar(x=xs, y=liabilities, name="부채총계", marker_color="#D1D5DB"))
    fig.add_trace(go.Bar(x=xs, y=equity, name="자본총계", marker_color="#6B7280"))
    fig.add_trace(
        go.Scatter(x=xs, y=debt_ratio, name="부채비율 (%)", mode="lines+markers+text",
                   text=text_labels(debt_ratio, 1, "%"), textposition="top center",
                   line=dict(color="#111827", width=2.5, dash="dash"), marker=dict(size=8)),
        secondary_y=True)
    fig.update_layout(barmode="stack", title=dict(text="자산 구성과 부채비율", x=0.01, font=dict(size=13)))
    fig.update_yaxes(title_text="억원", secondary_y=False)
    fig.update_yaxes(title_text="부채비율 (%)", secondary_y=True, showgrid=False)
    return style_fig(fig, height=400)


def chart_ratios(companies, years):
    """7대 재무비율 추이 (비교 모드는 영업이익률·ROE만)."""
    xs = [str(y) for y in years]
    fig = go.Figure()

    if len(companies) == 1:
        company = companies[0]
        for key, label, color, dash in RATIO_SERIES:
            fig.add_trace(go.Scatter(
                x=xs, y=[ratio_of(company, y, key) for y in years],
                name=label, mode="lines+markers",
                line=dict(color=color, width=2.4, dash=dash), marker=dict(size=7)))
    else:
        for index, company in enumerate(companies):
            color = PALETTE[index % 2]
            for key, label, dash in (("op_margin", "영업이익률", "solid"), ("roe", "ROE", "dot")):
                fig.add_trace(go.Scatter(
                    x=xs, y=[ratio_of(company, y, key) for y in years],
                    name="%s %s" % (company["name"], label), mode="lines+markers",
                    line=dict(color=color, width=2.6, dash=dash), marker=dict(size=7)))
    fig.update_layout(title=dict(text="재무비율 추이 (%)", x=0.01, font=dict(size=13)))
    fig.update_yaxes(title_text="%")
    return style_fig(fig, height=380)


def chart_dupont(company, years):
    """DuPont 3요인 + ROE (연도별)."""
    xs = [str(y) for y in years]

    def series(key):
        return [(company["years"][y].get("dupont") or {}).get(key) for y in years]

    net_margin, turnover, multiplier, roe = (
        series("net_margin"), series("asset_turnover"), series("equity_multiplier"), series("roe"))

    fig = make_subplots(rows=2, cols=2, horizontal_spacing=0.12, vertical_spacing=0.18,
                        subplot_titles=("① 수익성 — 순이익률 (%)", "② 효율성 — 총자산회전율 (회)",
                                        "③ 레버리지 — 재무레버리지 (배)", "ROE = ① × ② × ③ (%)"))
    panels = [
        (net_margin, "#4F46E5", 1, 1, 1, "%"),
        (turnover, "#6B7280", 1, 2, 2, "회"),
        (multiplier, "#9CA3AF", 2, 1, 2, "배"),
        (roe, "#111827", 2, 2, 1, "%"),
    ]
    for values, color, row, col, digits, suffix in panels:
        fig.add_trace(go.Bar(x=xs, y=values, name=suffix, marker_color=color, showlegend=False,
                             text=text_labels(values, digits, suffix), textposition="outside",
                             cliponaxis=False), row=row, col=col)
    fig.update_layout(barmode="group")
    return style_fig(fig, height=470, legend=False)


def chart_dupont_gap(gap):
    """두 회사 ROE 격차의 요인별 기여도 (가로 막대)."""
    rows = gap["rows"]
    labels = [row["label"] for row in rows] + ["ROE 격차 합계"]
    values = [row["contribution"] for row in rows] + [gap["total"]]
    colors = ["#4F46E5" if v >= 0 else "#9CA3AF" for v in values]

    fig = go.Figure(go.Bar(
        x=values, y=labels, orientation="h", marker_color=colors,
        text=["%+.1f%%p" % v for v in values], textposition="outside", cliponaxis=False,
    ))
    fig.update_layout(title=dict(text="ROE 격차 요인 분해 (순차 대체법)", x=0.01, font=dict(size=13)))
    fig.update_xaxes(title_text="ROE 기여도 (%p)", zeroline=True, zerolinecolor="#9CA3AF")
    return style_fig(fig, height=330, legend=False)


def chart_ccc(companies, years):
    """운전자본 주기 — 1개사는 DSO·DIO·DPO 누적 + CCC 선, 비교 모드는 CCC 선."""
    xs = [str(y) for y in years]
    fig = go.Figure()

    if len(companies) == 1:
        company = companies[0]

        def series(key):
            return [(company["years"][y].get("ccc") or {}).get(key) for y in years]

        dso, dio, dpo, ccc = series("dso"), series("dio"), series("dpo"), series("ccc")
        fig.add_trace(go.Bar(x=xs, y=dso, name="DSO (매출채권 회수)", marker_color="#6B7280"))
        fig.add_trace(go.Bar(x=xs, y=dio, name="DIO (재고 보유)", marker_color="#9CA3AF"))
        fig.add_trace(go.Bar(x=xs, y=[None if v is None else -v for v in dpo],
                             name="DPO (매입채무 지급)", marker_color="#D1D5DB",
                             customdata=dpo,
                             hovertemplate="DPO %{customdata:.0f}일<extra></extra>"))
        fig.add_trace(go.Scatter(
            x=xs, y=ccc, name="CCC (현금전환주기)", mode="lines+markers+text",
            text=text_labels(ccc, 0, "일"), textposition="top center",
            line=dict(color="#111827", width=3), marker=dict(size=9)))
        fig.update_layout(barmode="relative",
                          title=dict(text="DSO + DIO − DPO = CCC (일)", x=0.01, font=dict(size=13)))
        fig.update_yaxes(title_text="일")
    else:
        for index, company in enumerate(companies):
            color = PALETTE[index % 2]
            values = [(company["years"][y].get("ccc") or {}).get("ccc") for y in years]
            fig.add_trace(go.Scatter(
                x=xs, y=values,
                name="%s CCC" % company["name"], mode="lines+markers+text",
                text=text_labels(values, 0, "일"),
                textposition="top center",
                line=dict(color=color, width=3), marker=dict(size=9)))
        fig.update_layout(title=dict(text="현금전환주기(CCC) 비교 (일)", x=0.01, font=dict(size=13)))
        fig.update_yaxes(title_text="일")

    return style_fig(fig, height=400)


def chart_fpa_scenarios(result):
    """시나리오별 매출 경로 — 출발 연도 실적 한 점에 Y+1~Y+3 추정을 이어 붙인다."""
    baseline = result.get("baseline") or {}
    scenarios = result.get("scenarios") or []
    if not baseline or not scenarios:
        return None

    base_year = result.get("base_year")
    start = ("%d년" % base_year) if isinstance(base_year, int) else "출발 연도"
    steps = scenarios[0].get("years") or []
    xs = [start] + [("%d년" % row["year"]) if isinstance(row.get("year"), int) else row["label"]
                    for row in steps]

    colors = {"bear": "#9CA3AF", "base": "#6B7280", "bull": "#4F46E5", "custom": "#111827"}
    fig = go.Figure()
    for scenario in scenarios:
        values = [eok(baseline.get("revenue"))] + [eok(row.get("revenue")) for row in scenario["years"]]
        name = (scenario["label"] if scenario.get("is_custom")
                else "%s (%s)" % (scenario["label"], scenario.get("short") or scenario["key"]))
        labels = [""] * (len(values) - 1)
        labels.append("" if values[-1] is None else format(round(values[-1]), ","))
        fig.add_trace(go.Scatter(
            x=xs, y=values, name=name, mode="lines+markers+text",
            # 마지막 점(Y+3) 값만 표시 — 오른쪽 여백까지 넘겨 잘리지 않게 cliponaxis=False
            text=labels, textposition="middle right", textfont=dict(size=11),
            cliponaxis=False,
            line=dict(color=colors.get(scenario["key"], "#4F46E5"), width=3,
                      dash="dash" if scenario.get("is_custom") else "solid"),
            marker=dict(size=8),
        ))
    fig.update_layout(title=dict(text="매출 경로 (억원)", x=0.01, font=dict(size=13)))
    figure = style_fig(fig, height=380)
    figure.update_layout(margin=dict(l=10, r=96, t=58, b=84))   # 오른쪽 값 라벨 자리
    return figure


# ---------------------------------------------------------------------------
# 8. 탭 1 — 핵심 재무 & 추이
# ---------------------------------------------------------------------------

def render_tab1(analysis):
    companies, years = analysis["companies"], analysis["years"]
    single = len(companies) == 1

    # 제목은 여기서 한 번만 낸다. (표 함수에 같은 제목을 넘겨 두 줄로 겹치던 문제)
    section_title("핵심 재무제표")
    table_financials(companies, years, CORE_ROWS, title=None)
    st.plotly_chart(chart_income(companies, years))
    st.plotly_chart(chart_ratios(companies, years))

    with st.expander("추가 계정 · 세부 차트 보기"):
        table_financials(companies, years, EXTRA_ROWS, title="추가 계정 (단위: 억원)")
        if single:
            st.plotly_chart(chart_balance(companies[0], years))
        table_ratios(companies, years)
        for company in companies:
            quote_box("<b>%s</b> · %s" % (company["name"], company["dupont_comment"]))

    with st.expander("지표 계산식 보기"):
        st.markdown(
            "| 지표 | 계산식 | 읽는 법 |\n"
            "| --- | --- | --- |\n"
            "| 영업이익률 | 영업이익 ÷ 매출액 × 100 | 매출 100원 중 본업에서 남긴 이익 |\n"
            "| 순이익률 | 당기순이익 ÷ 매출액 × 100 | 이자·세금까지 반영한 최종 이익률 |\n"
            "| ROE | 당기순이익 ÷ 기말 자본총계 × 100 | 주주 자본으로 번 수익률 |\n"
            "| 부채비율 | 부채총계 ÷ 자본총계 × 100 | 100% 이하면 안정, 200% 초과는 주의 |\n"
            "| 유동비율 | 유동자산 ÷ 유동부채 × 100 | 100% 미만이면 단기 지급 부담 |\n"
            "| 매출액·영업이익 증가율 | (당기 − 전기) ÷ \\|전기\\| × 100 | 전기가 적자면 부호가 뒤집히지 않게 절댓값으로 나눔 |\n"
        )
        st.caption("ROE는 기말 자본 기준입니다. (기초+기말)/2 평균자본 기준과는 값이 조금 다를 수 있습니다. 자본잠식(자본총계 0 이하)인 해는 부채비율·ROE를 계산하지 않습니다.")


# ---------------------------------------------------------------------------
# 9. 탭 2 — FP&A 심층 (DuPont & CCC)
# ---------------------------------------------------------------------------

def render_dupont_block(analysis):
    companies, years = analysis["companies"], analysis["years"]
    latest = years[-1]

    section_title("DuPont ROE 3단계 분해",
                  "ROE = 순이익률 × 총자산회전율 × 재무레버리지 (기말 잔액 기준)")

    if len(companies) == 1:
        company = companies[0]
        dupont = company["years"][latest].get("dupont") or {}
        columns = st.columns(5)
        columns[0].markdown(
            card("① 순이익률", pct(dupont.get("net_margin")), "매출 100원당 최종이익", "#4F46E5"),
            unsafe_allow_html=True)
        columns[1].markdown(
            card("② 총자산회전율", num(dupont.get("asset_turnover"), 2, "회"), "기말 자산 1원이 만든 매출", "#6B7280"),
            unsafe_allow_html=True)
        columns[2].markdown(
            card("③ 재무레버리지", num(dupont.get("equity_multiplier"), 2, "배"), "자기자본 1원에 대응하는 자산", "#9CA3AF"),
            unsafe_allow_html=True)
        columns[3].markdown(
            card("DuPont ROE", pct(dupont.get("roe")), "① × ② × ③", "#111827"),
            unsafe_allow_html=True)
        columns[4].markdown(
            card("검산 (단순 ROE)", pct(dupont.get("roe_simple")),
                 "차이 %s" % num(dupont.get("diff"), 2, "%p"), "#D1D5DB"),
            unsafe_allow_html=True)
        st.write("")
        if dupont.get("roe") is None:
            st.info("DuPont 분해에 필요한 계정(매출액·당기순이익·자산총계·자본총계)이 일부 없어 분해할 수 없습니다. "
                    "표에 계산 가능한 요인만 표시합니다.")
        else:
            st.plotly_chart(chart_dupont(company, years))
        table_dupont(companies, years)
        quote_box("<b>%s</b> · %s" % (company["name"], company["dupont_comment"]))
        return

    # 비교 모드
    table_dupont(companies, years)
    gap = analysis.get("dupont_gap")
    if gap:
        st.plotly_chart(chart_dupont_gap(gap))
        st.caption("%s 기준으로 %s의 요인을 하나씩 대입한 결과입니다. 세 기여도의 합이 ROE 격차와 같습니다."
                   % (gap["a"], gap["b"]))
        quote_box(gap["text"])
    else:
        st.info("두 회사 모두 DuPont 요인(매출액·순이익·자산·자본)이 있어야 격차 분해를 계산할 수 있습니다. "
                "한쪽에 계정이 없어 표만 표시합니다.")
    for company in companies:
        quote_box("<b>%s</b> · %s" % (company["name"], company["dupont_comment"]))


def render_ccc_block(analysis):
    companies, years = analysis["companies"], analysis["years"]

    section_title("운전자본 · 현금전환주기(CCC)",
                  "DSO + DIO − DPO = CCC · 짧을수록 운전자본 부담이 작습니다")
    has_ccc = any(
        ((company["years"][year].get("ccc") or {}).get("ccc") is not None)
        for company in companies for year in years
    )
    if has_ccc:
        st.plotly_chart(chart_ccc(companies, years))
    else:
        st.info("운전자본 지표(DSO·DIO·DPO)를 계산할 계정이 없습니다. "
                "금융업처럼 재고자산·매출원가를 공시하지 않는 업종에서는 정상입니다.")
    table_ccc(companies, years)

    latest = years[-1]
    if len(companies) == 1:
        quote_box("<b>%s</b> · %s" % (companies[0]["name"], companies[0]["ccc_comment"]))
        current = companies[0]["years"][latest].get("ccc") or {}
        if current.get("ccc") is None:
            missing = [label for key, label in fs.CCC_INPUTS
                       if (current.get("inputs") or {}).get(key) is None]
            st.caption("계산 불가 사유 — 미공시 계정: %s" % (", ".join(missing) if missing else "관련 계정 부족"))
        return

    gap = analysis.get("ccc_gap")
    if gap:
        quote_box(gap["text"])
    else:
        st.info("두 회사 모두 매출원가·매출채권·재고자산·매입채무가 공시되어야 CCC 비교 문장을 만들 수 있습니다.")

    rows = []
    for company in companies:
        current = company["years"][latest].get("ccc") or {}
        rows.append({
            "기업": company["name"],
            "%d년 DSO" % latest: None if current.get("dso") is None else round(current["dso"]),
            "DIO": None if current.get("dio") is None else round(current["dio"]),
            "DPO": None if current.get("dpo") is None else round(current["dpo"]),
            "CCC": None if current.get("ccc") is None else round(current["ccc"]),
        })
    st.dataframe(
        pd.DataFrame(rows), hide_index=True,
        column_config={column: st.column_config.NumberColumn(column, format="%d일")
                       for column in ["%d년 DSO" % latest, "DIO", "DPO", "CCC"]},
    )
    for company in companies:
        quote_box("<b>%s</b> · %s" % (company["name"], company["ccc_comment"]))


def industry_basis(peer_block):
    """'업종코드 264 · 반도체·전자 · 동일 코드 1개사 + 같은 2자리 2개사 · 2025년 연결' 같은 한 줄."""
    parts = ["업종코드 %s" % peer_block["induty_code"]]
    label = peer_block.get("sector") or peer_block.get("industry_name")
    if label:
        parts.append(label)
    counts = {}
    for peer in peer_block["peers"]:
        counts[peer["match_label"]] = counts.get(peer["match_label"], 0) + 1
    if counts:
        parts.append("피어 " + " + ".join("%s %d개사" % (match, count) for match, count in counts.items()))
    basis = "연결" if "연결" in (peer_block.get("fs_nm") or "") else "재무제표"
    parts.append("%d년 %s" % (peer_block["year"], basis))
    return " · ".join(parts)


def render_industry_block(analysis):
    """동종업계 비교 — 한 기업만 분석했을 때만 표를 그린다(직접 고른 경쟁사와 섞이지 않게)."""
    companies = analysis.get("companies") or []
    peer_block = analysis.get("industry_peers") or {}

    if len(companies) > 1:
        section_title("동종업계 비교")
        st.caption("동종업계 자동 비교는 기업 한 곳만 골랐을 때 표시됩니다. "
                   "지금은 직접 고른 %d개사 비교입니다." % len(companies))
        return

    if peer_block.get("status") != "ok":
        section_title("동종업계 비교")
        reason = {
            "no_code": "업종코드를 찾지 못해",
            "no_peers": "같은 업종 피어를 찾지 못해",
            "no_data": "피어 재무제표를 받지 못해",
            "error": "업종 정보를 확인하지 못해",
        }.get(peer_block.get("status"), "업종 정보를 확인하지 못해")
        st.caption("%s 동종업계 비교를 건너뛰었습니다. 다른 지표와 분석은 그대로입니다." % reason)
        return

    section_title("동종업계 비교", industry_basis(peer_block))
    quote_box(html_lib.escape(peer_block["comment"]))
    st.markdown(industry_peer_table(peer_block), unsafe_allow_html=True)

    notes = ["영업이익률·ROE·부채비율·유동비율은 %, CCC는 일",
             "피어 평균·중앙값은 값이 있는 피어만으로 계산"]
    if peer_block.get("skipped"):
        notes.append("지표를 받지 못해 뺀 피어: %s" % ", ".join(peer_block["skipped"]))
    st.caption(" · ".join(notes) + ".")
    st.caption("업종코드는 DART 기업개황의 신고값(KSIC 10차 기준)이라 실제 사업 구성과 다를 수 있습니다. "
               "피어 선정은 동일 코드 → 같은 3자리 → 같은 2자리 순이고, 지주회사(64992)는 지주회사끼리만 비교합니다.")


def render_tab2(analysis):
    render_dupont_block(analysis)
    st.divider()
    render_ccc_block(analysis)
    st.divider()
    render_industry_block(analysis)


# ---------------------------------------------------------------------------
# 9-2. 탭 4 — FP&A 3개년 시나리오 시뮬레이터
# ---------------------------------------------------------------------------

FPA_ACCENT = {"bear": "#9CA3AF", "base": "#6B7280", "bull": "#4F46E5", "custom": "#111827"}
FPA_MONEY_COLUMNS = ["Y+1 매출", "Y+2 매출", "Y+3 매출", "Y+3 영업이익", "3년 누적 현금 영향"]
FPA_DETAIL_MONEY_COLUMNS = ["매출액", "영업이익", "운전자본", "운전자본 증감(+=묶임)", "누적 현금 영향"]


def fpa_assumption_inputs(company, latest):
    """가정 입력 3개(매출 성장률·영업이익률 변동·CCC 변동) — 기업별로 값이 남도록 key를 나뉜다."""
    suffix = company.get("corp_code") or company.get("name") or "corp"
    growth_range = fs.FPNA_RANGES["revenue_growth_pct"]
    margin_range = fs.FPNA_RANGES["op_margin_delta_pp"]
    ccc_range = fs.FPNA_RANGES["ccc_delta_days"]

    columns = st.columns(3)
    with columns[0]:
        growth = st.slider(
            "매출 성장률 (연, %)",
            min_value=float(growth_range[0]), max_value=float(growth_range[1]),
            value=0.0, step=float(growth_range[2]), format="%.1f",
            key="fs2_fpa_growth_%s" % suffix,
            help="매년 같은 성장률이 %d년 반복된다고 가정합니다. 0%%면 %d년 매출이 그대로 이어집니다."
                 % (fs.FPNA_HORIZON_YEARS, latest),
        )
    with columns[1]:
        margin_delta = st.slider(
            "영업이익률 변동 (%p)",
            min_value=float(margin_range[0]), max_value=float(margin_range[1]),
            value=0.0, step=float(margin_range[2]), format="%.1f",
            key="fs2_fpa_margin_%s" % suffix,
            help="출발 연도 영업이익률에 더할 값입니다. 3년 내내 같은 이익률이 유지된다고 봅니다.",
        )
    with columns[2]:
        ccc_delta = st.slider(
            "CCC 변동 (일)",
            min_value=int(ccc_range[0]), max_value=int(ccc_range[1]),
            value=0, step=int(ccc_range[2]), format="%d",
            key="fs2_fpa_ccc_%s" % suffix,
            help="+면 현금이 더 오래 묶이고(유출), −면 회수가 빨라집니다(유입).",
        )
    return float(growth), float(margin_delta), float(ccc_delta)


def render_fpa_simulator(company, latest):
    baseline = fs.fpa_baseline(company, latest)
    if not baseline or not baseline.get("revenue") or baseline["revenue"] <= 0:
        st.info("매출액이 공시되지 않았거나 0 이하라 시나리오를 계산할 수 없습니다. 다른 기업이나 연도를 선택해 보세요.")
        return

    # 출발점 — 최신 결산 연도의 실제 수치
    columns = st.columns(4)
    columns[0].markdown(
        card("%d년 매출액" % latest, human_won(baseline["revenue"]), "시뮬레이션 출발점(실제 공시)", "#111827"),
        unsafe_allow_html=True)
    columns[1].markdown(
        card("영업이익률", pct(baseline.get("op_margin")),
             "영업이익 %s" % human_won(baseline.get("op_income")), "#4F46E5"),
        unsafe_allow_html=True)
    if baseline.get("ccc") is None:
        ccc_value, ccc_sub = "-", "금융업 등 미공시"
    else:
        ccc_value = num(baseline.get("ccc"), 0, "일")
        ccc_sub = "DSO %s · DIO %s · DPO %s" % (
            num(baseline.get("dso"), 0), num(baseline.get("dio"), 0), num(baseline.get("dpo"), 0))
    columns[2].markdown(card("현금전환주기 (CCC)", ccc_value, ccc_sub, "#6B7280"), unsafe_allow_html=True)
    columns[3].markdown(
        card("운전자본 (근사)",
             "-" if baseline.get("working_capital") is None else human_won(baseline["working_capital"]),
             "매출 1일분 × CCC", "#9CA3AF"),
        unsafe_allow_html=True)

    st.write("")
    growth, margin_delta, ccc_delta = fpa_assumption_inputs(company, latest)
    result = fs.simulate_fpa_scenarios(company, latest, growth, margin_delta, ccc_delta)
    if result.get("status") != "ok":
        st.info(result.get("message") or "시뮬레이션을 계산할 수 없습니다.")
        st.caption(result.get("note") or fs.FPNA_DISCLAIMER)
        return

    st.caption("가정을 움직이면 ‘내 시나리오’와 표가 바로 다시 계산됩니다 — 실행 버튼을 누를 필요가 없습니다. "
               "금액 단위: 억원, 기간: Y+1~Y+3.")

    # 시나리오 카드 4장 — 큰 숫자는 3년 뒤(Y+3) 추정 매출액
    cards = st.columns(4)
    for column, scenario in zip(cards, result["scenarios"]):
        cash = scenario.get("cum_cash_impact")
        title = (scenario["label"] if scenario.get("is_custom")
                 else "%s (%s)" % (scenario["label"], scenario.get("short") or scenario["key"]))
        sub = "Y+3 영업이익 %s · 누적 현금 %s" % (
            human_won(scenario.get("end_op_income")),
            "-" if cash is None else human_won(cash))
        column.markdown(
            card(title, human_won(scenario.get("end_revenue")), sub,
                 FPA_ACCENT.get(scenario["key"], "#4F46E5")),
            unsafe_allow_html=True)
    st.caption("카드의 큰 숫자는 3년 뒤(%d년) 추정 매출액입니다." % (latest + fs.FPNA_HORIZON_YEARS))

    figure = chart_fpa_scenarios(result)
    if figure is not None:
        st.plotly_chart(figure)

    # 3개 기본 시나리오 + 내 시나리오 비교
    section_title("시나리오 비교", "세 가정만 다르고 나머지 규칙은 같습니다 · 금액 단위: 억원")
    rows = []
    for scenario in result["scenarios"]:
        row = {
            "시나리오": (scenario["label"] if scenario.get("is_custom")
                         else "%s (%s)" % (scenario["label"], scenario.get("short") or scenario["key"])),
            "가정": "성장 %+.1f%% · 이익률 %+.1f%%p · CCC %+.0f일" % (
                scenario["revenue_growth_pct"], scenario["op_margin_delta_pp"], scenario["ccc_delta_days"]),
        }
        for item in scenario["years"]:
            row["Y+%d 매출" % item["step"]] = row_eok(item.get("revenue"))
        row["Y+3 영업이익"] = row_eok(scenario.get("end_op_income"))
        row["3년 누적 현금 영향"] = row_eok(scenario.get("cum_cash_impact"))
        rows.append(row)
    comparison = pd.DataFrame(rows)
    for column_name in FPA_MONEY_COLUMNS:
        if column_name in comparison.columns:
            comparison[column_name] = pd.to_numeric(comparison[column_name], errors="coerce").astype("Int64")
    st.dataframe(
        comparison, hide_index=True,
        column_config={column_name: st.column_config.NumberColumn(column_name, format="%d")
                       for column_name in FPA_MONEY_COLUMNS if column_name in comparison.columns},
    )
    st.caption("누적 현금 영향 = Σ(영업이익 − 운전자본 증감). 운전자본 증감이 +면 현금이 묶인 것이라 영업이익에서 빼서 반영합니다. "
               "법인세·이자비용·설비투자(CapEx)는 빼지 않았습니다.")

    # 내 시나리오 연도별 상세
    custom = result["custom"]
    section_title("내 시나리오 연도별 상세", "Y+1~Y+3은 세 가정에 따른 추정입니다 · 금액 단위: 억원")
    detail = []
    for item in custom["years"]:
        detail.append({
            "연도": (("%s · %d년" % (item["label"], item["year"])) if isinstance(item.get("year"), int)
                     else item["label"]),
            "매출액": row_eok(item.get("revenue")),
            "영업이익": row_eok(item.get("op_income")),
            "영업이익률(%)": None if item.get("op_margin") is None else round(item["op_margin"], 1),
            "CCC(일)": None if item.get("ccc") is None else round(item["ccc"]),
            "운전자본": row_eok(item.get("working_capital")),
            "운전자본 증감(+=묶임)": row_eok(item.get("delta_working_capital")),
            "누적 현금 영향": row_eok(item.get("cum_cash_impact")),
        })
    detail_frame = pd.DataFrame(detail)
    for column_name in FPA_DETAIL_MONEY_COLUMNS:
        if column_name in detail_frame.columns:
            detail_frame[column_name] = pd.to_numeric(detail_frame[column_name], errors="coerce").astype("Int64")
    st.dataframe(
        detail_frame, hide_index=True,
        column_config={
            **{column_name: st.column_config.NumberColumn(column_name, format="%d")
               for column_name in FPA_DETAIL_MONEY_COLUMNS if column_name in detail_frame.columns},
            "영업이익률(%)": st.column_config.NumberColumn("영업이익률(%)", format="%.1f"),
            "CCC(일)": st.column_config.NumberColumn("CCC(일)", format="%d일"),
        },
    )

    # 핵심 인사이트 2줄
    if result.get("insights"):
        quote_box("<br>".join(html_lib.escape(line) for line in result["insights"]))

    if result.get("capital_impaired"):
        st.warning("자본잠식(자본총계 0 이하) 상태입니다 — 이 시뮬레이션은 손익·운전자본만 보여 주고, "
                   "자본이 언제 회복되는지는 계산하지 않습니다.")
    if result.get("op_loss"):
        st.caption("출발점이 영업적자(영업이익률 %s)입니다 — 이익률 가정을 올려 흑자 전환 여부를 확인해 보세요."
                   % pct(baseline.get("op_margin")))
    if not result.get("wc_available"):
        st.caption("CCC를 공시하지 않는 업종(금융업 등)이라 운전자본·현금 영향 칸이 비어 있습니다. 손익 칸은 그대로 계산됩니다.")

    with st.expander("ⓘ 시뮬레이션 계산식 보기"):
        st.markdown(
            "| 항목 | 계산식 |\n| --- | --- |\n"
            "| 매출액 | 출발 연도 매출액 × (1 + 매출 성장률)^n |\n"
            "| 영업이익률 | 출발 연도 영업이익률 + 변동(%p) |\n"
            "| 영업이익 | 매출액 × 영업이익률 ÷ 100 |\n"
            "| CCC | 출발 연도 CCC + 변동(일) |\n"
            "| 운전자본 | 매출액 ÷ 365 × CCC |\n"
            "| 운전자본 증감 | 운전자본(n) − 운전자본(n−1) |\n"
            "| 현금 영향 | 영업이익 − 운전자본 증감 |\n"
        )
        st.caption("운전자본은 공시된 CCC(DSO+DIO−DPO)에서 ‘매출 1일분 × 회전일수’로 근사합니다. "
                   "매출채권·재고자산·매입채무 잔액을 해마다 따로 추정하지 않는 단순법입니다.")
    st.caption(result.get("note") or fs.FPNA_DISCLAIMER)


def render_tab4(analysis):
    companies, years = analysis["companies"], analysis["years"]
    latest = years[-1]

    section_title(
        "FP&A 3개년 시나리오 시뮬레이터",
        "%d년 결산 실적을 출발점으로 세 가지만 바꿔 봅니다 — 매출 성장률 · 영업이익률 변동 · CCC 변동. "
        "Y+1~Y+3 손익과 운전자본 현금 영향을 같은 규칙으로 계산합니다." % latest)

    company = companies[0]
    if len(companies) > 1:
        names = [item["name"] for item in companies]
        picked = st.selectbox("시뮬레이션할 기업", names, key="fs2_fpa_company")
        company = companies[names.index(picked)]

    # 보조 기능 하나가 실패해도 다른 탭의 분석 결과를 가리지 않게 감싼다.
    # (엔진을 빠뜨린 옛 모듈이 물려 있는 배포에서는 여기서 안내만 남기고 나머지는 그대로 그린다)
    try:
        render_fpa_simulator(company, latest)
    except Exception as error:
        st.info("FP&A 시나리오 시뮬레이터를 그리지 못했습니다. 다른 탭의 분석 결과는 그대로입니다.")
        st.caption("%s: %s — 앱을 다시 배포(reboot)하면 해결되는 경우가 많습니다." % (type(error).__name__, error))


# ---------------------------------------------------------------------------
# 10. 탭 3 — 리스크 상세 & DART 원문
# ---------------------------------------------------------------------------

def render_disclosure(company):
    data = company.get("disclosure") or {}
    status = data.get("status")
    matches = data.get("matches") or []

    if status == "ok":
        severe = sum(1 for m in matches if m["severity"] == "severe")
        st.warning("최근 1년 공시에서 위험 키워드 **%d건** (심각 %d · 주의 %d)"
                   % (len(matches), severe, len(matches) - severe))
    elif status == "clear":
        st.caption("최근 1년 공시에서 위험 키워드가 잡히지 않았습니다.")
    else:
        st.error("공시 위험 조회에 실패했습니다 — %s" % (data.get("error") or "알 수 없는 오류"))
        st.caption("비재무 참고 정보라 재무 분석은 그대로 진행됩니다. 잠시 후 다시 실행해 보세요.")

    coverage = fs.disclosure_coverage(data)
    if coverage:
        st.caption("조회 범위: %s · 기간 %s" % (coverage, data.get("period", "")))
    if data.get("truncated"):
        st.caption("API 페이지 상한(%d페이지)까지만 확인했습니다." % fs.DISCLOSURE_MAX_PAGES)

    if matches:
        rows = []
        for match in matches:
            flags = []
            if match.get("correction"):
                flags.append("정정공시")
            if match.get("subsidiary"):
                flags.append("종속회사")
            rows.append({
                "심각도": "🔴 심각" if match["severity"] == "severe" else "🟡 주의",
                "공시일": match["rcept_dt"] or "-",
                "공시명": match["report_nm"],
                "구분": ", ".join(flags) or "-",
                "원문": match["url"],
            })
        st.dataframe(
            pd.DataFrame(rows), hide_index=True,
            column_config={"원문": st.column_config.LinkColumn("원문", display_text="DART에서 열기")},
        )
        st.caption("정정공시는 같은 사건이 여러 건으로 늘어나고, ‘종속회사’ 공시는 종속회사 사건일 수 있습니다.")


def risk_compare_table(companies):
    """비교 모드 — 5개 항목을 두 회사 나란히."""
    rows = []
    for index, item in enumerate(companies[0]["risk"]["items"]):
        row = {"항목": item["label"], "판정 기준": item["criteria"]}
        for company in companies:
            peer = company["risk"]["items"][index]
            row[company["name"]] = "%s · %s" % (state_badge(peer["state"]), peer["value_text"])
        rows.append(row)
    st.dataframe(pd.DataFrame(rows), hide_index=True)


def render_tab3(analysis):
    companies, years = analysis["companies"], analysis["years"]

    section_title("5대 리스크 상세",
                  "항목당 20점 · 데이터 없는 항목은 분모에서 제외 · 85점 이상 안전 / 60점 이상 주의 / 60점 미만 위험")
    for index, company in enumerate(companies):
        risk = company["risk"]
        if len(companies) > 1:
            st.markdown("**%s** — %d점 %s" % (company["name"], risk["score"], STATE_STYLE[risk["grade"]][0]))
        else:
            st.markdown("**%s · %d점 · %s**" % (company["name"], risk["score"], STATE_STYLE[risk["grade"]][1]))
        table_risk(company)
        quote_box(risk["comment"])
        if risk.get("capital_impaired"):
            st.warning("%s — 부채비율·ROE·DuPont은 음수 자본으로 왜곡되어 계산하지 않습니다."
                       % (risk.get("capital_impaired_text") or "자본잠식 상태입니다"))
        if risk.get("z_score") is not None:
            st.caption("Altman Z''-Score(장부가 기준) %.2f — 2.6 이상 안전 / 1.1~2.6 회색 / 1.1 미만 부실 위험 · 연속 영업적자 %d년"
                       % (risk["z_score"], risk["op_loss_streak"]))
        else:
            st.caption("Altman Z''-Score 계산에 필요한 계정이 일부 없습니다 · 연속 영업적자 %d년" % risk["op_loss_streak"])
        if index < len(companies) - 1:
            st.write("")

    if len(companies) > 1:
        st.markdown("**항목별 나란히 비교**")
        risk_compare_table(companies)

    st.divider()
    section_title("최근 1년 공시 위험", "공시 제목의 위험 키워드 탐지 · 리스크 점수에는 반영하지 않습니다")
    for company in companies:
        if len(companies) > 1:
            st.markdown("**%s**" % company["name"])
        render_disclosure(company)
        st.write("")

    st.divider()
    section_title("사업보고서 원문", "표의 링크를 누르면 DART 원문이 새 탭으로 열립니다")
    for company in companies:
        if len(companies) > 1:
            st.markdown("**%s** — %s" % (company["name"], company.get("fs_nm") or "-"))
        table_reports(company, years)
        st.write("")


# ---------------------------------------------------------------------------
# 11. 결과 묶음 · 부드러운 스크롤
# ---------------------------------------------------------------------------

def scroll_script(anchor_id=ANCHOR_ID):
    """결과 영역 상단 앵커로 부드럽게 이동하는 스크립트 (height=0 iframe에서 실행)."""
    return """
<script>
(function () {
  function jumpToResult() {
    try {
      var doc = window.parent.document;
      var target = doc.getElementById("%(id)s");
      if (target) {
        target.scrollIntoView({ behavior: "smooth", block: "start" });
      }
    } catch (error) { /* iframe이 격리된 환경이면 조용히 넘어간다 */ }
  }
  // 결과가 다 그려진 뒤 한 번, 늦게 그려지는 경우를 위해 한 번 더
  setTimeout(jumpToResult, 60);
  setTimeout(jumpToResult, 450);
})();
</script>
""" % {"id": anchor_id}


def render_excel_download(analysis):
    """결과 상단 — 한 번에 받아 보는 엑셀(.xlsx) 리포트 다운로드 (딥블랙·인디고 톤 유지)."""
    try:
        payload = excel.build_excel_report(analysis)
        filename = excel.excel_filename(analysis)
    except Exception as error:              # 리포트를 못 만들어도 화면 분석 결과는 그대로 둔다
        st.caption("엑셀 리포트를 만들지 못했습니다 — %s" % error)
        return
    left, right = st.columns([2.4, 1])
    with left:
        st.caption(EXCEL_SHEETS_NOTE)
    with right:
        st.download_button(
            "📥 엑셀(.xlsx) 리포트 다운로드",
            data=payload,
            file_name=filename,
            mime=EXCEL_MIME,
            key="fs2_excel_download",
            use_container_width=True,
        )


def render_results(analysis):
    # 결과 영역 앵커 — 부드러운 스크롤의 목적지
    st.markdown('<div id="%s" style="scroll-margin-top:14px"></div>' % ANCHOR_ID, unsafe_allow_html=True)

    render_result_head(analysis)
    render_excel_download(analysis)
    render_summary(analysis)
    render_disclosure_alert(analysis["companies"])
    with st.expander("ⓘ 분석 기준 및 한계 보기"):
        st.markdown(DISCLAIMER_TMPL, unsafe_allow_html=True)
    st.write("")

    tab1, tab2, tab3, tab4 = st.tabs([
        "① 핵심 재무 & 추이",
        "② FP&A 심층 · DuPont & CCC",
        "③ 리스크 상세 & DART 원문",
        "④ FP&A 시나리오",
    ])
    with tab1:
        render_tab1(analysis)
    with tab2:
        render_tab2(analysis)
    with tab3:
        render_tab3(analysis)
    with tab4:
        render_tab4(analysis)

    st.divider()
    footer_left, footer_middle, footer_right = st.columns([1, 1.1, 1])
    with footer_middle:
        if st.button("다른 기업 분석하기", use_container_width=True, key="fs2_reset"):
            st.session_state.pop("fs2_params", None)
            st.session_state["fs2_scroll"] = False
            st.rerun()
    st.caption("데이터: 금융감독원 OpenDART · 계산: 결정론적 Python 규칙 · 투자 권유가 아닙니다.")


# ---------------------------------------------------------------------------
# 12. 실행
# ---------------------------------------------------------------------------

def render_change_notice(settings, saved):
    st.info("설정이 바뀌었습니다. **분석 실행**을 누르면 이 설정으로 다시 분석합니다. "
            "지금 아래 결과는 직전 설정(%s · %s~%s)의 것입니다."
            % (" vs ".join(c["corp_name"] for c in saved["corps"]),
               saved["years"][0], saved["years"][-1]))


def run_with_feedback(settings):
    """캐시된 엔진 실행 + 사람 말로 된 오류 안내. 실패하면 None."""
    corps_key = tuple((c["corp_name"], c["corp_code"], c["stock_code"]) for c in settings["corps"])
    years_key = tuple(settings["years"])

    with st.spinner("DART에서 사업보고서를 가져와 분석하는 중입니다… (회사·연도당 1~2초, 첫 실행만)"):
        try:
            return run_analysis(settings["api_key"], corps_key, years_key)
        except fs.DartError as error:
            if getattr(error, "status", None) == "years":
                st.error("분석할 수 있는 연도가 부족합니다. 사업보고서가 공시된 연도를 2개 이상 선택해 주세요. "
                         "(예: %d, %d)" % (datetime.now().year - 2, datetime.now().year - 1))
            else:
                st.error("분석 중 문제가 생겼습니다.\n\n%s" % error)
                st.caption("인증키·조회 연도·회사명을 확인해 주세요. OpenDART는 하루 20,000건까지 호출할 수 있습니다.")
        except (urllib.error.URLError, OSError) as error:
            st.error("네트워크 오류로 OpenDART에 접속하지 못했습니다 — %s" % error)
        except Exception as error:                     # 예상 못 한 오류도 화면에 안내
            st.error("분석 중 예상하지 못한 오류가 발생했습니다.\n\n%s: %s" % (type(error).__name__, error))
    return None


def main():
    inject_css()
    render_hero()

    api_key, key_source = resolve_api_key()

    # 인증키가 secrets·환경변수·세션 어디에도 없으면 중앙 카드 expander에서 받는다.
    if not api_key:
        typed = remember_api_key(render_key_gate())
        if not typed:
            render_landing_hint()
            return
        # 키를 방금 받았다면 한 번 다시 그려서, 키 입력 카드 없이 기업 검색 카드만 보이게 한다.
        st.rerun()

    try:
        with st.spinner("기업 목록을 불러오는 중…"):
            corp_list = load_corp_list(api_key)
    except Exception as error:      # 키 오류·네트워크 오류를 화면에 그대로 안내
        render_corp_load_error(error)
        return

    settings, clicked, valid = render_input_card(corp_list, api_key, key_source)

    if clicked and valid:
        st.session_state["fs2_params"] = settings
        st.session_state["fs2_scroll"] = True          # 버튼을 눌렀을 때만 결과로 스크롤

    saved = st.session_state.get("fs2_params")
    if not saved:
        render_landing_hint()
        return

    if not valid:
        return

    if params_changed(settings, saved):
        render_change_notice(settings, saved)

    analysis = run_with_feedback(saved)
    if analysis is None:
        st.session_state["fs2_scroll"] = False
        return

    pending_scroll = bool(st.session_state.pop("fs2_scroll", False))
    render_results(analysis)
    if pending_scroll:
        components.html(scroll_script(), height=0)


main()
