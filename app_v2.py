# -*- coding: utf-8 -*-
"""
FinSight AI v2 — 중앙 집중형 UI (OpenDART 사업보고서 기반 기업 재무·리스크 분석)
================================================================================

app.py(사이드바 버전)는 기록·비교용으로 그대로 두고, 화면만 새로 설계한 버전이다.
분석 로직은 app.py와 똑같이 analyzer.py의 build_analysis() 등 기존 엔진 함수를 그대로 쓴다.

화면 구성 (요청한 순서 그대로)
    ① 상단 히어로 헤더 — FinSight AI 배지 + 한 줄 설명
    ② 가운데 입력 카드
         · 기업 이름 검색/선택
         · ＋ 경쟁사 비교 추가 (체크하면 바로 아래에 비교 기업 검색/선택이 나타남)
         · 기준 연도 (multiselect, 기본 최근 3개년)
         · 맨 아래 가득 찬 🔎 분석 실행 버튼
    ③ 분석 실행 시 결과 영역 상단(#analysis-result)으로 부드러운 스크롤
       (버튼을 눌렀을 때만 — 단순 입력 변경으로는 화면이 튀지 않게 session_state 플래그로 제어)
    ④ 결과 — 종합 점수·공시 위험·한계 고지 + 3개 탭(핵심 재무·추이 / FP&A DuPont·CCC / 리스크·DART 원문)

사이드바는 쓰지 않는다. set_page_config(initial_sidebar_state="collapsed") +
CSS로 사이드바 토글·여백을 없애고, 최대 너비를 가운데 정렬한다.

인증키는 (1) st.secrets["DART_API_KEY"] → (2) 환경변수 DART_API_KEY
→ (3) 키가 없을 때만 중앙 카드 하단 expander 입력 순서로 찾는다. 하드코딩하지 않는다.

실행
    streamlit run app_v2.py
"""

import contextlib
import io
import os
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

# 기업 목록 캐시는 저장소가 아니라 임시 폴더에 둔다. (Streamlit Cloud 파일 시스템은 휘발성)
fs.CORP_CACHE_FILE = os.path.join(tempfile.gettempdir(), "finsight_corp_codes.json")

st.set_page_config(
    page_title="FinSight AI · 기업 재무·리스크 분석",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="collapsed",          # 사이드바 없는 중앙 집중형 레이아웃
)

# ---------------------------------------------------------------------------
# 0. 화면 상수
# ---------------------------------------------------------------------------

PALETTE = ["#2563EB", "#F97316"]                # 비교 모드에서 회사별 색
DART_KEY_URL = "https://opendart.fss.or.kr/uss/umt/EgovMberSbscrbView.do"
ANCHOR_ID = "analysis-result"                    # 결과 영역 스크롤 앵커

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
    "na": ("⚪", "N/A", "#475569", "#F1F5F9"),
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
    ("op_margin", "영업이익률", "#2563EB", "solid"),
    ("net_margin", "순이익률", "#0EA5E9", "solid"),
    ("roe", "ROE", "#7C3AED", "solid"),
    ("debt_ratio", "부채비율", "#F97316", "dot"),
    ("current_ratio", "유동비율", "#10B981", "dot"),
]

FEATURES = [
    ("🚦", "5대 리스크 100점", "유동비율·부채비율·이자보상배율·현금흐름·부실예측을 신호등으로"),
    ("🔺", "DuPont ROE · CCC", "ROE를 3요인으로 분해하고 현금전환주기로 운전자본을 봅니다"),
    ("📰", "공시 위험 · DART 원문", "최근 1년 공시 제목의 위험 키워드와 사업보고서 원문 링크까지"),
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
  /* ── 기본 타이포그래피 ─────────────────────────────────────────── */
  html, body, [class*="st-"], [data-testid="stAppViewContainer"] {
      font-family: "Pretendard", "Inter", -apple-system, BlinkMacSystemFont,
                   "Apple SD Gothic Neo", "Malgun Gothic", "Noto Sans KR", sans-serif;
  }
  .stApp {
      background:
          radial-gradient(1100px 520px at 50% -10%, #E6EDFF 0%, rgba(247,249,252,0) 62%),
          #F7F9FC;
  }
  .block-container {
      max-width: 1120px;
      padding-top: 2.2rem;
      padding-bottom: 4rem;
      margin: 0 auto;
  }
  h1, h2, h3, h4 { color: #0F172A; letter-spacing: -0.02em; }

  /* ── 사이드바 제거 (없는 사이드바의 토글·여백까지) ────────────── */
  [data-testid="stSidebar"],
  [data-testid="stSidebarNav"],
  [data-testid="stSidebarCollapsedControl"],
  [data-testid="collapsedControl"],
  button[data-testid="stSidebarCollapseButton"] { display: none !important; }
  header[data-testid="stHeader"] { background: transparent; }

  /* ── 히어로 헤더 ─────────────────────────────────────────────── */
  .fs-hero {
      position: relative; overflow: hidden;
      background: linear-gradient(135deg, #0B1220 0%, #162B5B 46%, #2563EB 100%);
      border-radius: 24px;
      padding: 30px 32px 28px;
      color: #FFFFFF;
      margin: 4px 0 20px 0;
      box-shadow: 0 24px 48px -30px rgba(15, 23, 42, .75);
  }
  .fs-hero::after {
      content: ""; position: absolute; right: -80px; top: -90px;
      width: 260px; height: 260px; border-radius: 50%;
      background: radial-gradient(circle at 30% 30%, rgba(147,197,253,.45), rgba(37,99,235,0) 68%);
  }
  .fs-hero-badge {
      display: inline-flex; align-items: center; gap: 8px;
      font-size: 11.5px; font-weight: 800; letter-spacing: .16em;
      color: #BFDBFE; background: rgba(255,255,255,.10);
      border: 1px solid rgba(191,219,254,.35);
      padding: 5px 12px; border-radius: 999px;
  }
  .fs-hero-title {
      font-size: 32px; font-weight: 800; letter-spacing: -0.03em;
      line-height: 1.22; margin: 14px 0 0 0;
  }
  .fs-hero-title .thin { color: #BFDBFE; font-weight: 600; }
  .fs-hero-sub {
      font-size: 13.5px; color: #D6E4FF; margin-top: 10px; line-height: 1.65;
      max-width: 640px;
  }

  /* ── 가운데 입력 카드 ────────────────────────────────────────── */
  .st-key-fs2-inputcard,
  .st-key-fs2-keycard {
      background: #FFFFFF;
      border: 1px solid #E6EBF4 !important;
      border-radius: 22px !important;
      padding: 24px 26px 20px !important;
      box-shadow: 0 22px 50px -34px rgba(15, 23, 42, .55), 0 2px 8px rgba(15, 23, 42, .04);
  }
  /* 같은 key 클래스가 안쪽 블록에도 붙는 경우 — 카드가 두 겹으로 겹치지 않게 */
  .st-key-fs2-inputcard .st-key-fs2-inputcard,
  .st-key-fs2-keycard .st-key-fs2-keycard,
  .st-key-fs2-inputcard [data-testid="stVerticalBlockBorderWrapper"],
  .st-key-fs2-keycard [data-testid="stVerticalBlockBorderWrapper"] {
      border: none !important; background: transparent !important;
      padding: 0 !important; border-radius: 0 !important; box-shadow: none !important;
  }
  .fs-card-title { font-size: 17px; font-weight: 800; color: #0F172A; letter-spacing: -0.02em; }
  .fs-card-sub { font-size: 12.5px; color: #64748B; margin: 4px 0 16px 0; line-height: 1.55; }
  .fs-card-foot { font-size: 11.5px; color: #94A3B8; margin-top: 10px; text-align: center; }

  /* ── 입력 위젯 다듬기 ────────────────────────────────────────── */
  [data-testid="stWidgetLabel"] p {
      font-size: 12.5px !important; font-weight: 700 !important; color: #334155 !important;
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
      border-color: #2563EB !important;
      box-shadow: 0 0 0 3px rgba(37, 99, 235, .12) !important;
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
      background: linear-gradient(135deg, #2563EB 0%, #1D4ED8 100%) !important;
      box-shadow: 0 14px 30px -16px rgba(37, 99, 235, .95);
      transition: transform .14s ease, box-shadow .14s ease;
  }
  [data-testid="stBaseButton-primary"]:hover, button[kind="primary"]:hover {
      transform: translateY(-1px);
      box-shadow: 0 18px 34px -16px rgba(37, 99, 235, 1);
  }
  [data-testid="stBaseButton-secondary"] {
      border-radius: 12px !important; font-weight: 700 !important;
      border-color: #DEE4EF !important;
  }

  /* ── 랜딩 힌트 카드 ──────────────────────────────────────────── */
  .fs-hint {
      text-align: center; font-size: 13px; color: #64748B; margin: 22px 0 14px 0;
  }
  .fs-hint-card {
      background: rgba(255,255,255,.72);
      border: 1px dashed #DCE3EF;
      border-radius: 18px;
      padding: 6px 20px;
  }
  .fs-hint-row {
      display: flex; align-items: flex-start; gap: 12px;
      padding: 13px 0; border-bottom: 1px dashed #E6EBF4;
  }
  .fs-hint-row:last-child { border-bottom: none; }
  .fs-hint-row .ico { font-size: 17px; line-height: 1.5; }
  .fs-hint-row b { display: block; font-size: 13px; color: #1E293B; }
  .fs-hint-row span.txt { font-size: 12px; color: #7A8798; }

  /* ── 결과 헤더 ───────────────────────────────────────────────── */
  .fs-result-head {
      background: #FFFFFF; border: 1px solid #E6EBF4; border-radius: 20px;
      padding: 22px 24px; margin-bottom: 14px;
      box-shadow: 0 18px 40px -36px rgba(15, 23, 42, .6);
  }
  .fs-eyebrow {
      font-size: 11px; font-weight: 800; letter-spacing: .16em; color: #2563EB;
  }
  .fs-result-title {
      font-size: 26px; font-weight: 800; color: #0F172A; letter-spacing: -0.03em;
      margin: 8px 0 12px 0; line-height: 1.25;
  }
  .fs-result-title .vs { color: #94A3B8; font-weight: 600; font-size: 20px; margin: 0 6px; }
  .fs-chips { display: flex; flex-wrap: wrap; gap: 7px; }
  .fs-chip {
      display: inline-flex; align-items: center; gap: 5px;
      font-size: 11.5px; font-weight: 600; color: #475569;
      background: #F4F7FC; border: 1px solid #E6EBF4;
      padding: 4px 10px; border-radius: 999px;
  }
  .fs-chip b { color: #1E293B; font-weight: 800; }
  .fs-chip-link { color: #1D4ED8; background: #EEF4FF; border-color: #D7E3FF; text-decoration: none; }
  .fs-chip-link:hover { background: #E2ECFF; }

  /* ── 지표 카드 ───────────────────────────────────────────────── */
  .fs-kpi {
      background: #FFFFFF; border: 1px solid #E6EBF4; border-top-width: 3px;
      border-radius: 16px; padding: 15px 17px 16px; min-height: 132px;
      box-shadow: 0 12px 30px -30px rgba(15, 23, 42, .7);
      display: flex; flex-direction: column; justify-content: space-between;
  }
  .fs-kpi .lbl { font-size: 11.5px; font-weight: 800; color: #64748B; letter-spacing: .02em; }
  .fs-kpi .val { font-size: 25px; font-weight: 800; color: #0F172A; margin-top: 7px; line-height: 1.16; }
  .fs-kpi .bar { height: 6px; border-radius: 999px; background: #EEF2F8; margin-top: 10px; overflow: hidden; }
  .fs-kpi .bar span { display: block; height: 100%; border-radius: 999px; }
  .fs-kpi .sub { font-size: 11.5px; color: #64748B; margin-top: 9px; line-height: 1.5; }
  .fs-company-tag {
      display: inline-flex; align-items: center; gap: 7px;
      font-size: 12.5px; font-weight: 800; color: #0F172A; margin: 6px 0 10px 2px;
  }
  .fs-company-tag i { display: inline-block; width: 4px; height: 14px; border-radius: 3px; }

  /* ── 알림 · 고지 박스 ────────────────────────────────────────── */
  .fs-alert {
      border-radius: 14px; padding: 13px 16px; font-size: 12.8px; line-height: 1.65;
      margin: 14px 0 0 0; border: 1px solid #FDE3C8; background: #FFF8EE; color: #8A4B0A;
  }
  .fs-alert.danger { border-color: #FBD5D5; background: #FFF4F4; color: #9B1C1C; }
  .fs-alert.info { border-color: #DCE3EF; background: #F8FAFC; color: #475569; }
  .fs-alert b { color: inherit; }

  .fs-disclaimer {
      background: #F8FAFC; border: 1px solid #E6EBF4; border-radius: 16px;
      padding: 15px 18px; font-size: 12.3px; color: #475569; line-height: 1.75;
      margin-top: 16px;
  }
  .fs-disclaimer b { color: #0F172A; }

  /* ── 섹션 제목 · 인용 박스 ───────────────────────────────────── */
  .fs-section-title {
      font-size: 16px; font-weight: 800; color: #0F172A; margin: 20px 0 3px 0;
      display: flex; align-items: center; gap: 8px; letter-spacing: -0.02em;
  }
  .fs-note { font-size: 12.3px; color: #64748B; line-height: 1.65; margin-bottom: 6px; }
  .fs-quote {
      background: #FFFFFF; border: 1px solid #E6EBF4; border-left: 4px solid #2563EB;
      border-radius: 12px; padding: 13px 16px; font-size: 13px; color: #1E293B;
      line-height: 1.7; margin: 10px 0 4px 0;
  }

  /* ── 탭 ──────────────────────────────────────────────────────── */
  .stTabs [data-baseweb="tab-list"] {
      gap: 6px; border-bottom: 1px solid #E6EBF4; padding-bottom: 0;
  }
  .stTabs [data-baseweb="tab"] {
      height: 44px; padding: 0 16px; border-radius: 12px 12px 0 0;
      font-size: 13px; font-weight: 700; color: #64748B; background: transparent;
  }
  .stTabs [data-baseweb="tab"]:hover { color: #1D4ED8; background: #F4F7FF; }
  .stTabs [aria-selected="true"] { color: #1D4ED8 !important; background: #EFF4FF !important; }
  .stTabs [data-baseweb="tab-highlight"] { background-color: #2563EB; }

  /* ── 표 · 차트 ───────────────────────────────────────────────── */
  div[data-testid="stDataFrame"] { border: 1px solid #E6EBF4; border-radius: 14px; overflow: hidden; }
  [data-testid="stMetricValue"] { font-size: 1.45rem; }
  footer, #MainMenu { visibility: hidden; }

  @media (max-width: 640px) {
      .block-container { padding-top: 1.2rem; }
      .fs-hero { padding: 22px 20px 20px; border-radius: 18px; }
      .fs-hero-title { font-size: 23px; }
      .fs-result-title { font-size: 20px; }
      .st-key-fs2-inputcard, .st-key-fs2-keycard { padding: 18px 16px 16px !important; }
  }
</style>
"""

HERO_TMPL = """
<div class="fs-hero">
  <div class="fs-hero-badge">◆&nbsp; FINSIGHT AI &nbsp;·&nbsp; 기업 심층 분석</div>
  <div class="fs-hero-title">재무제표와 공시를 읽어,<br>기업의 체력을 한 장으로 <span class="thin">정리합니다</span></div>
  <div class="fs-hero-sub">
    OpenDART 사업보고서를 그대로 불러와 5대 리스크 점수 · DuPont ROE · 현금전환주기(CCC) ·
    최근 1년 공시 위험까지 한 화면에 담습니다. 기업을 고르고 분석 실행만 누르면 됩니다.
  </div>
</div>
"""

DISCLAIMER_TMPL = (
    '<div class="fs-disclaimer">'
    '<b>🧪 1차 건전성 스크리닝입니다</b> — 과거 결산 재무제표와 최근 1년 공시 <b>제목 키워드</b>를 '
    '훑는 수준의 점검입니다. 업종별 구조 차이(금융업·건설업 등), 금리·환율·업황 같은 향후 시장 변수, '
    '주가·시가총액은 반영하지 않았습니다. 5대 리스크 점수는 3단계 판정(🟢20점 / 🟡10점 / 🔴0점)이라 '
    '같은 등급 안의 미세한 차이는 드러나지 않습니다. <b>투자 판단·신용평가가 아니며</b>, 최종 판단 전에는 '
    'DART 원문과 업종 비교군을 함께 확인하세요. 모든 수치는 <b>연결재무제표 우선</b>(없으면 별도) · '
    '<b>기말잔액 기준</b>입니다.'
    '</div>'
)

KPI_TMPL = (
    '<div class="fs-kpi" style="border-top-color:{accent}">'
    '<div class="lbl">{label}</div>'
    '<div class="val">{value}</div>'
    '{bar}'
    '<div class="sub">{sub}</div>'
    '</div>'
)


def card(label, value, sub="", accent="#2563EB", bar=None, bar_color=None):
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


def center_columns():
    """가운데 정렬용 3분할 (가운데 열에서 입력 카드를 그린다)."""
    return st.columns([1, 2.5, 1])


def render_landing_hint():
    """첫 화면 — 무엇을 해 주는 앱인지 한 번 더 짧게."""
    _left, middle, _right = center_columns()
    with middle:
        st.markdown('<div class="fs-hint">기업을 고르고 <b>🔎 분석 실행</b>을 누르면 결과가 아래에 펼쳐집니다.</div>',
                    unsafe_allow_html=True)
        rows = "".join(
            '<div class="fs-hint-row"><span class="ico">%s</span>'
            '<div><b>%s</b><span class="txt">%s</span></div></div>' % (icon, title, note)
            for icon, title, note in FEATURES
        )
        st.markdown('<div class="fs-hint-card">%s</div>' % rows, unsafe_allow_html=True)


def render_key_gate():
    """인증키가 secrets·환경변수 어디에도 없을 때 — 중앙 카드 하단 expander에서 직접 입력."""
    _left, middle, _right = center_columns()
    with middle:
        with st.container(key="fs2-keycard"):
            st.markdown(
                '<div class="fs-card-title">OpenDART 인증키가 필요합니다</div>'
                '<div class="fs-card-sub">공시 데이터를 받아오려면 금융감독원 OpenDART 인증키가 있어야 합니다. '
                '무료로 발급받아(하루 20,000건) 아래에 붙여 넣어 주세요.</div>',
                unsafe_allow_html=True,
            )
            with st.expander("🔑 OpenDART 인증키 입력", expanded=True):
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
        with st.container(key="fs2-keycard"):
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
        with st.container(key="fs2-inputcard"):
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
                    "🔎 분석 실행",
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
                with st.expander("🔑 OpenDART 인증키 직접 입력", expanded=False):
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

    names = []
    for index, company in enumerate(companies):
        if index:
            names.append('<span class="vs">vs</span>')
        names.append(company["name"])

    chips = []
    for company in companies:
        chips.append('<span class="fs-chip"><b>%s</b> %s</span>'
                     % (company["name"], company["stock_code"] or "-"))
    fs_names = sorted({c.get("fs_nm") or "-" for c in companies})
    chips.append('<span class="fs-chip">%s</span>' % " / ".join(fs_names))
    chips.append('<span class="fs-chip">%d–%d 사업보고서(연간)</span>' % (years[0], years[-1]))
    for company in companies:
        link = fs.dart_link(company.get("latest_rcept_no") or "")
        if link:
            chips.append('<a class="fs-chip fs-chip-link" href="%s" target="_blank">%s 최신 원문 ↗</a>'
                         % (link, company["name"]))

    st.markdown(
        '<div class="fs-result-head">'
        '<div class="fs-eyebrow">ANALYSIS RESULT</div>'
        '<div class="fs-result-title">%s</div>'
        '<div class="fs-chips">%s</div>'
        '</div>' % ("".join(names), "".join(chips)),
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
    return "조회 실패", (data.get("error") or "")[:60] or "공시검색 API 호출 실패", "#64748B"


def render_summary(analysis):
    companies = analysis["companies"]
    years = analysis["years"]
    latest = years[-1]

    for company_index, company in enumerate(companies):
        if len(companies) > 1:
            st.markdown(
                '<div class="fs-company-tag"><i style="background:%s"></i>%s</div>'
                % (PALETTE[company_index % 2], company["name"]),
                unsafe_allow_html=True,
            )
        risk = company["risk"]
        light, grade_label, grade_color, _bg = STATE_STYLE[risk["grade"]]
        d_value, d_sub, d_accent = disclosure_headline(company)
        revenue = account_amount(company, latest, "revenue")
        revenue_growth = ratio_of(company, latest, "revenue_growth")
        op_margin = ratio_of(company, latest, "op_margin")
        prev_margin = ratio_of(company, years[-2], "op_margin") if len(years) >= 2 else None
        margin_diff = (op_margin - prev_margin) if (op_margin is not None and prev_margin is not None) else None

        col1, col2, col3, col4 = st.columns(4)
        col1.markdown(
            card(
                "5대 리스크 종합 점수",
                "%d점 %s" % (risk["score"], light),
                "%s · 평가 %d/5 항목 (N/A %d개 제외)"
                % (grade_label, risk["available_count"], 5 - risk["available_count"]),
                grade_color,
                bar=risk["score"],
            ),
            unsafe_allow_html=True,
        )
        col2.markdown(card("최근 1년 공시 위험", d_value, d_sub, d_accent), unsafe_allow_html=True)
        col3.markdown(
            card(
                "%d년 매출액" % latest,
                human_won(revenue),
                "전년 대비 %s" % (num(revenue_growth, 1, "%") if revenue_growth is not None else "-"),
                "#2563EB",
            ),
            unsafe_allow_html=True,
        )
        col4.markdown(
            card(
                "%d년 영업이익률" % latest,
                pct(op_margin),
                "전년 대비 %s" % (num(margin_diff, 1, "%p") if margin_diff is not None else "-"),
                "#0EA5E9",
            ),
            unsafe_allow_html=True,
        )


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
            '<div class="fs-alert%s"><b>⚠️ 공시 위험 알림</b> — 최근 1년 공시 제목에서 위험 키워드가 잡혔습니다: %s. '
            '정정공시·종속회사 공시일 수 있으니 <b>③ 리스크 상세 &amp; DART 원문</b> 탭에서 원문을 확인하세요. '
            '(공시 위험은 5대 리스크 점수에 반영하지 않습니다)</div>' % (" danger" if danger else "", ", ".join(hits)),
            unsafe_allow_html=True,
        )
    elif failures:
        st.markdown(
            '<div class="fs-alert info"><b>ℹ️ 공시 위험 조회 실패</b> — %s의 공시 목록을 가져오지 못했습니다. '
            '재무 분석은 그대로 진행되었습니다.</div>' % ", ".join(failures),
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
    config = {
        column: st.column_config.NumberColumn(column, format=("%.0f" if digits == 0 else "%.1f"))
        for column in df.columns
    }
    if height:
        st.dataframe(df, column_config=config, height=height)
    else:
        st.dataframe(df, column_config=config)


def table_financials(companies, years, rows):
    df = build_table(companies, years, rows,
                     lambda company, year, key: row_eok(account_amount(company, year, key)))
    show_table(df, digits=0)
    st.caption("단위: 억원 · 연결재무제표 우선(없으면 별도) · 사업보고서(연간) 기준")


def table_ratios(companies, years):
    rows = [(key, "%s · %s" % (group, label))
            for key, label, _unit, group in fs.RATIO_LABELS]
    df = build_table(companies, years, rows, ratio_of)
    show_table(df, digits=1)
    st.caption("단위: % · 성장률은 전년 대비(YoY) · 첫 연도 성장률은 전년 데이터가 없어 비어 있습니다")


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
    st.caption(
        "총자산회전율 = 매출액 ÷ **기말** 자산총계 · 재무레버리지 = 자산총계 ÷ 자본총계 · "
        "DuPont ROE = 순이익률 × 총자산회전율 × 재무레버리지 (단순 ROE와 0.01%p 이내로 일치해야 정상)"
    )


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
    st.caption("기말잔액·365일 기준 · 매출원가·재고자산 등이 공시되지 않은 회사(금융업 등)는 비어 있습니다")


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
        margin=dict(l=10, r=14, t=54, b=10),
        font=dict(family="Pretendard, 'Apple SD Gothic Neo', 'Malgun Gothic', sans-serif",
                  size=12, color="#334155"),
        hoverlabel=dict(bgcolor="white", font_size=12),
        plot_bgcolor="white",
        bargap=0.28,
    )
    if legend:
        fig.update_layout(legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0,
                                      font=dict(size=11), bgcolor="rgba(0,0,0,0)"))
    fig.update_xaxes(showgrid=False, linecolor="#E2E8F0")
    fig.update_yaxes(gridcolor="#EEF2F6", zerolinecolor="#CBD5E1")
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
        fig.add_trace(go.Bar(x=xs, y=revenue, name="매출액", marker_color="#2563EB",
                             text=text_labels(revenue), textposition="outside",
                             cliponaxis=False), row=1, col=1)
        fig.add_trace(go.Bar(x=xs, y=op_income, name="영업이익", marker_color="#0EA5E9",
                             text=text_labels(op_income), textposition="outside",
                             cliponaxis=False), row=1, col=2)
        fig.add_trace(go.Bar(x=xs, y=net_income, name="당기순이익", marker_color="#94A3B8",
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
    fig.add_trace(go.Bar(x=xs, y=liabilities, name="부채총계", marker_color="#F87171"))
    fig.add_trace(go.Bar(x=xs, y=equity, name="자본총계", marker_color="#60A5FA"))
    fig.add_trace(
        go.Scatter(x=xs, y=debt_ratio, name="부채비율 (%)", mode="lines+markers+text",
                   text=text_labels(debt_ratio, 1, "%"), textposition="top center",
                   line=dict(color="#0F172A", width=2.5, dash="dash"), marker=dict(size=8)),
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
        (net_margin, "#2563EB", 1, 1, 1, "%"),
        (turnover, "#10B981", 1, 2, 2, "회"),
        (multiplier, "#F97316", 2, 1, 2, "배"),
        (roe, "#7C3AED", 2, 2, 1, "%"),
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
    colors = ["#2563EB" if v >= 0 else "#DC2626" for v in values]

    fig = go.Figure(go.Bar(
        x=values, y=labels, orientation="h", marker_color=colors,
        text=["%+.1f%%p" % v for v in values], textposition="outside", cliponaxis=False,
    ))
    fig.update_layout(title=dict(text="ROE 격차 요인 분해 (순차 대체법)", x=0.01, font=dict(size=13)))
    fig.update_xaxes(title_text="ROE 기여도 (%p)", zeroline=True, zerolinecolor="#94A3B8")
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
        fig.add_trace(go.Bar(x=xs, y=dso, name="DSO (매출채권 회수)", marker_color="#60A5FA"))
        fig.add_trace(go.Bar(x=xs, y=dio, name="DIO (재고 보유)", marker_color="#34D399"))
        fig.add_trace(go.Bar(x=xs, y=[None if v is None else -v for v in dpo],
                             name="DPO (매입채무 지급)", marker_color="#FBBF24",
                             customdata=dpo,
                             hovertemplate="DPO %{customdata:.0f}일<extra></extra>"))
        fig.add_trace(go.Scatter(
            x=xs, y=ccc, name="CCC (현금전환주기)", mode="lines+markers+text",
            text=text_labels(ccc, 0, "일"), textposition="top center",
            line=dict(color="#0F172A", width=3), marker=dict(size=9)))
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


# ---------------------------------------------------------------------------
# 8. 탭 1 — 핵심 재무 & 추이
# ---------------------------------------------------------------------------

def render_tab1(analysis):
    companies, years = analysis["companies"], analysis["years"]
    single = len(companies) == 1

    section_title("📋 핵심 재무제표", "매출·영업이익·순이익과 재무상태(자산·부채·자본)를 연도별로 봅니다.")
    table_financials(companies, years, CORE_ROWS)

    with st.expander("추가 계정 보기 (운전자본·현금흐름·이자비용)"):
        table_financials(companies, years, EXTRA_ROWS)
        st.caption("CCC(운전자본)와 5대 리스크 진단에 쓰이는 계정입니다. 공시되지 않은 계정은 비어 있습니다.")

    section_title("📈 추이 차트", "막대·선을 눌러 값을 확인하세요. 범례를 클릭하면 계열을 켜고 끌 수 있습니다.")
    st.plotly_chart(chart_income(companies, years))

    if single:
        st.plotly_chart(chart_balance(companies[0], years))

    section_title("🧮 7대 재무비율", "수익성 3 · 안정성 2 · 성장성 2 — 업종 평균이 아니라 같은 회사의 시간 변화를 보는 지표입니다.")
    table_ratios(companies, years)

    st.plotly_chart(chart_ratios(companies, years))
    if not single:
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
        st.caption("ROE는 기말 자본 기준입니다. 기업분석 보고서의 (기초+기말)/2 평균자본 기준과는 값이 조금 다를 수 있습니다.")


# ---------------------------------------------------------------------------
# 9. 탭 2 — FP&A 심층 (DuPont & CCC)
# ---------------------------------------------------------------------------

def render_dupont_block(analysis):
    companies, years = analysis["companies"], analysis["years"]
    latest = years[-1]

    section_title(
        "🔺 DuPont ROE 3단계 분해",
        "ROE 한 줄로는 ‘빚으로 올린 ROE’와 ‘이익을 많이 남겨서 올린 ROE’를 구분할 수 없습니다. "
        "ROE = 순이익률 × 총자산회전율 × 재무레버리지 로 쪼개서 어디서 나온 수익인지 봅니다. "
        "<b>총자산회전율은 매출액 ÷ 기말 자산총계</b>(평균잔액 아님) 기준입니다.",
    )

    if len(companies) == 1:
        company = companies[0]
        dupont = company["years"][latest].get("dupont") or {}
        columns = st.columns(5)
        columns[0].markdown(
            card("① 순이익률", pct(dupont.get("net_margin")), "매출 100원당 최종이익", "#2563EB"),
            unsafe_allow_html=True)
        columns[1].markdown(
            card("② 총자산회전율", num(dupont.get("asset_turnover"), 2, "회"), "기말 자산 1원이 만든 매출", "#10B981"),
            unsafe_allow_html=True)
        columns[2].markdown(
            card("③ 재무레버리지", num(dupont.get("equity_multiplier"), 2, "배"), "자기자본 1원에 대응하는 자산", "#F97316"),
            unsafe_allow_html=True)
        columns[3].markdown(
            card("DuPont ROE", pct(dupont.get("roe")), "① × ② × ③", "#7C3AED"),
            unsafe_allow_html=True)
        columns[4].markdown(
            card("검산 (단순 ROE)", pct(dupont.get("roe_simple")),
                 "차이 %s" % num(dupont.get("diff"), 2, "%p"), "#64748B"),
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
        st.caption(
            "왼쪽 회사(%s) 기준으로 오른쪽 회사(%s)의 요인을 하나씩 대입한 결과입니다. "
            "막대가 (+)면 오른쪽 회사가 그 요인에서 ROE를 더 높인 것이고, 세 기여도의 합이 ROE 격차와 같습니다."
            % (gap["a"], gap["b"])
        )
        quote_box(gap["text"])
    else:
        st.info("두 회사 모두 DuPont 요인(매출액·순이익·자산·자본)이 있어야 격차 분해를 계산할 수 있습니다. "
                "한쪽에 계정이 없어 표만 표시합니다.")
    for company in companies:
        quote_box("<b>%s</b> · %s" % (company["name"], company["dupont_comment"]))


def render_ccc_block(analysis):
    companies, years = analysis["companies"], analysis["years"]

    section_title(
        "🔄 운전자본 · 현금전환주기(CCC)",
        "DSO(매출채권 회수일) + DIO(재고 보유일) − DPO(매입채무 지급일) = CCC. "
        "현금이 사업 안에 묶여 있는 기간이라 <b>짧을수록 운전자본 부담이 작습니다</b>. "
        "분자는 모두 기말잔액 기준입니다.",
    )
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


def render_tab2(analysis):
    render_dupont_block(analysis)
    st.divider()
    render_ccc_block(analysis)


# ---------------------------------------------------------------------------
# 10. 탭 3 — 리스크 상세 & DART 원문
# ---------------------------------------------------------------------------

def render_disclosure(company):
    data = company.get("disclosure") or {}
    status = data.get("status")
    matches = data.get("matches") or []

    if status == "ok":
        severe = sum(1 for m in matches if m["severity"] == "severe")
        st.warning("⚠️ 최근 1년 공시에서 위험 키워드 **%d건**이 잡혔습니다 (심각 %d · 주의 %d)."
                   % (len(matches), severe, len(matches) - severe))
    elif status == "clear":
        st.success("🟢 최근 1년 공시에서 위험 키워드가 잡히지 않았습니다.")
    else:
        st.error("공시 위험 조회에 실패했습니다 — %s" % (data.get("error") or "알 수 없는 오류"))
        st.caption("비재무 참고 정보라 재무 분석은 그대로 진행됩니다. 잠시 후 다시 실행해 보세요.")

    coverage = fs.disclosure_coverage(data)
    if coverage:
        st.caption("조회 범위: %s · 기간 %s" % (coverage, data.get("period", "")))
    if data.get("truncated"):
        st.caption("⚠️ API 페이지 상한(%d페이지)에 걸려 최근 공시 일부만 확인했습니다." % fs.DISCLOSURE_MAX_PAGES)

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
        st.caption(
            "키워드 필터 결과입니다 — 정정공시는 같은 사건이 여러 건으로 늘어나고, "
            "‘종속회사’가 붙은 공시는 모회사가 아닌 연결 종속회사의 사건일 수 있습니다. "
            "이 항목은 5대 리스크 점수에 반영하지 않습니다."
        )


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

    section_title("🚦 5대 리스크 상세",
                  "항목당 20점(🟢 20 / 🟡 10 / 🔴 0), 데이터가 없는 항목은 분모에서 빼고 100점으로 환산합니다. "
                  "85점 이상 🟢 안전 · 60점 이상 🟡 주의 · 60점 미만 🔴 위험.")
    for index, company in enumerate(companies):
        risk = company["risk"]
        if len(companies) > 1:
            st.markdown("**%s** — %d점 %s" % (company["name"], risk["score"], STATE_STYLE[risk["grade"]][0]))
        else:
            st.markdown("**%s · %d점 · %s**" % (company["name"], risk["score"], STATE_STYLE[risk["grade"]][1]))
        table_risk(company)
        quote_box(risk["comment"])
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
    section_title("📰 최근 1년 공시 위험 탐지",
                  "공시 제목(report_nm)에 소송·횡령·감사의견·유상증자 같은 키워드가 있는지 봅니다. "
                  "대기업은 1년 공시가 수천 건이라 페이지를 끝까지 넘겨 조회합니다.")
    for company in companies:
        if len(companies) > 1:
            st.markdown("**%s**" % company["name"])
        render_disclosure(company)
        st.write("")

    st.divider()
    section_title("📄 사업보고서 원문 (DART)",
                  "모든 수치의 출처입니다. 표의 링크를 누르면 DART 원문 뷰어가 열립니다.")
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


def render_results(analysis):
    # 결과 영역 앵커 — 부드러운 스크롤의 목적지
    st.markdown('<div id="%s" style="scroll-margin-top:14px"></div>' % ANCHOR_ID, unsafe_allow_html=True)

    render_result_head(analysis)
    render_summary(analysis)
    render_disclosure_alert(analysis["companies"])
    st.markdown(DISCLAIMER_TMPL, unsafe_allow_html=True)
    st.write("")

    tab1, tab2, tab3 = st.tabs([
        "① 핵심 재무 & 추이",
        "② FP&A 심층 · DuPont & CCC",
        "③ 리스크 상세 & DART 원문",
    ])
    with tab1:
        render_tab1(analysis)
    with tab2:
        render_tab2(analysis)
    with tab3:
        render_tab3(analysis)

    st.divider()
    footer_left, footer_middle, footer_right = st.columns([1, 1.1, 1])
    with footer_middle:
        if st.button("↺ 다른 기업 분석하기", use_container_width=True, key="fs2_reset"):
            st.session_state.pop("fs2_params", None)
            st.session_state["fs2_scroll"] = False
            st.rerun()
    st.caption(
        "데이터: 금융감독원 전자공시시스템(DART) Open API · 엔진: analyzer.py · "
        "학습·포트폴리오 목적의 1차 스크리닝 도구이며 투자 권유가 아닙니다."
    )


# ---------------------------------------------------------------------------
# 12. 실행
# ---------------------------------------------------------------------------

def render_change_notice(settings, saved):
    st.info("설정이 바뀌었습니다. **🔎 분석 실행**을 누르면 이 설정으로 다시 분석합니다. "
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
