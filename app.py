# -*- coding: utf-8 -*-
"""
FinSight — 규칙 기반 기업 재무·리스크 분석 엔진 (OpenDART 사업보고서 기반 Streamlit 웹 앱)
=====================================================================

analyzer.py(OpenDART 분석 엔진)를 그대로 재사용하고 Streamlit 화면만 입힌 앱이다.
분석 로직은 한 줄도 새로 만들지 않고 엔진 함수를 호출한다.

화면 구성
    · 사이드바 : OpenDART 인증키 · 기업 검색/선택 · 경쟁사 비교(선택) · 기준 연도
    · 상단 요약 : 회사명·종목코드 · 5대 리스크 종합점수 · 최근 1년 공시 위험 · 1차 스크리닝 한계 고지
    · 탭 1 : 핵심 재무 & 추이 (재무제표 표 + Plotly 인터랙티브 차트 + 7대 재무비율)
    · 탭 2 : FP&A 심층 — DuPont ROE 3단계 분해 · 운전자본 현금전환주기(DSO/DIO/DPO/CCC)
    · 탭 3 : 리스크 상세 & DART 원문 (5대 리스크 신호등 · 최근 1년 공시 위험 · 사업보고서 링크)

인증키는 (1) st.secrets["DART_API_KEY"] → (2) 환경변수 DART_API_KEY → (3) 사이드바 입력
순서로 찾는다. 코드에 키를 하드코딩하지 않는다.

실행
    streamlit run app.py
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
from plotly.subplots import make_subplots

# 같은 폴더의 analyzer.py 를 엔진으로 불러온다. (Streamlit Cloud에서도 동일 폴더)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import analyzer as fs  # noqa: E402  (FinSight 분석 엔진)

# 기업 목록 캐시는 저장소 폴더가 아니라 임시 폴더에 둔다.
# (Streamlit Cloud는 파일 시스템이 휘발성이고, corp_codes.json 은 .gitignore 대상이다)
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
    initial_sidebar_state="expanded",
)

# ---------------------------------------------------------------------------
# 0. 화면 상수
# ---------------------------------------------------------------------------

PALETTE = ["#2563EB", "#F97316"]          # 비교 모드에서 회사별 색
DART_KEY_URL = "https://opendart.fss.or.kr/uss/umt/EgovMberSbscrbView.do"

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

# 검색어 없이 사이드바에 기본으로 보여 줄 추천 기업
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


def company_picker(corp_list, key, label, default_index=0, exclude_code=None):
    """검색어 입력 + 후보 selectbox. 고른 corp dict 를 돌려준다."""
    query = st.text_input(
        "기업명 검색",
        value="",
        key=key + "_query",
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
    picked = st.selectbox(label, labels, index=index, key=key + "_select")
    return options[labels.index(picked)]


# ---------------------------------------------------------------------------
# 3. HTML 조각 (카드·헤더·고지)
# ---------------------------------------------------------------------------

CARD_TMPL = (
    '<div style="background:#FFFFFF;border:1px solid #E2E8F0;border-left:5px solid %(accent)s;'
    'border-radius:14px;padding:14px 16px;min-height:118px;box-shadow:0 1px 3px rgba(15,23,42,.06)">'
    '<div style="font-size:12px;font-weight:700;color:#64748B;letter-spacing:.02em">%(label)s</div>'
    '<div style="font-size:26px;font-weight:800;color:#0F172A;margin-top:8px;line-height:1.15">%(value)s</div>'
    '<div style="font-size:12px;color:#64748B;margin-top:7px;line-height:1.4">%(sub)s</div>'
    "</div>"
)


def card(label, value, sub="", accent="#2563EB"):
    return CARD_TMPL % {"label": label, "value": value, "sub": sub, "accent": accent}


HERO_TMPL = (
    '<div style="background:linear-gradient(120deg,#0F172A 0%%,#1E3A8A 58%%,#2563EB 100%%);'
    'border-radius:18px;padding:22px 26px;color:#FFFFFF;margin:2px 0 14px 0">'
    '<div style="font-size:12px;font-weight:800;letter-spacing:.14em;color:#93C5FD">FINSIGHT · 규칙 기반 기업 재무·리스크 분석</div>'
    '<div style="font-size:29px;font-weight:800;margin-top:8px;line-height:1.25">%(title)s</div>'
    '<div style="font-size:13px;color:#DBEAFE;margin-top:9px;line-height:1.5">%(sub)s</div>'
    "</div>"
)

DISCLAIMER_TMPL = (
    '<div style="background:#F8FAFC;border:1px solid #E2E8F0;border-radius:14px;'
    'padding:14px 18px;font-size:12.5px;color:#475569;line-height:1.7">'
    '<span style="font-weight:800;color:#0F172A">🧪 1차 건전성 스크리닝입니다</span> — '
    "과거 결산 재무제표와 최근 1년 공시 <b>제목 키워드</b>를 훑는 수준의 점검입니다. "
    "업종별 구조 차이(금융업·건설업 등), 금리·환율·업황 같은 향후 시장 변수, 주가·시가총액은 반영하지 않았습니다. "
    "5대 리스크 점수는 3단계 판정(🟢20점 / 🟡10점 / 🔴0점)이라 같은 등급 안의 미세한 차이는 드러나지 않습니다. "
    "<b>투자 판단·신용평가가 아니며</b>, 최종 판단 전에는 DART 원문과 업종 비교군을 함께 확인하세요. "
    "모든 수치는 <b>연결재무제표 우선</b>(없으면 별도) · <b>기말잔액 기준</b>입니다. "
    "자본잠식(자본총계 0 이하)인 해는 부채비율·ROE가 왜곡되어 계산하지 않고 <b>자본잠식</b>으로 표시합니다."
    "</div>"
)


def inject_css():
    st.markdown(
        """
        <style>
          .block-container { padding-top: 2.2rem; padding-bottom: 3rem; max-width: 1280px; }
          h1, h2, h3, h4 { color: #0F172A; letter-spacing: -0.01em; }
          [data-testid="stMetricValue"] { font-size: 1.5rem; }
          div[data-testid="stDataFrame"] { border: 1px solid #E2E8F0; border-radius: 12px; }
          .fs-section-title {
              font-size: 17px; font-weight: 800; color: #0F172A; margin: 18px 0 4px 0;
              display: flex; align-items: center; gap: 8px;
          }
          .fs-note { font-size: 12.5px; color: #64748B; line-height: 1.6; }
          .fs-quote {
              background: #FFFFFF; border: 1px solid #E2E8F0; border-left: 4px solid #2563EB;
              border-radius: 12px; padding: 13px 16px; font-size: 13.5px; color: #1E293B;
              line-height: 1.7; margin: 10px 0 4px 0;
          }
          .fs-pill {
              display: inline-block; padding: 2px 9px; border-radius: 999px;
              font-size: 11.5px; font-weight: 700; margin-right: 6px;
          }
          /* 계정 표: 숫자가 DART 원문 링크 */
          .fs-fintable {
              width: 100%; border-collapse: collapse; font-size: 13px;
              font-variant-numeric: tabular-nums; margin: 2px 0 6px 0;
          }
          .fs-fintable th, .fs-fintable td {
              padding: 7px 10px; border-bottom: 1px solid #E2E8F0; white-space: nowrap;
          }
          .fs-fintable thead th {
              background: #F1F5F9; color: #334155; font-size: 12px; font-weight: 800;
          }
          .fs-fintable tbody th { text-align: left; font-weight: 600; color: #334155; }
          .fs-fintable th + th, .fs-fintable td { text-align: right; }
          .fs-fintable tbody tr:hover { background: #F8FAFC; }
          .fs-fintable a.fs-num {
              color: #0F172A; text-decoration: none; border-bottom: 1px dotted #94A3B8;
          }
          .fs-fintable a.fs-num:hover { color: #2563EB; border-bottom-color: #2563EB; }
          .fs-fintable a.fs-num::after {
              content: "↗"; font-size: 9px; color: #94A3B8; margin-left: 3px; vertical-align: super;
          }
          .fs-fintable a.fs-year {
              color: #475569; text-decoration: none; border-bottom: 1px dotted #CBD5E1;
          }
          .fs-fintable a.fs-year:hover { color: #2563EB; }
          .fs-fintable .fs-na { color: #94A3B8; }
          .fs-srclinks { margin: -2px 0 10px 2px; font-size: 12.5px; }
          .fs-srclinks a { color: #2563EB; text-decoration: none; font-weight: 700; }
          .fs-srclinks a:hover { text-decoration: underline; }
          .fs-srclinks .fs-sep { color: #CBD5E1; margin: 0 7px; }
          footer { visibility: hidden; }
        </style>
        """,
        unsafe_allow_html=True,
    )


def section_title(text, note=""):
    st.markdown('<div class="fs-section-title">%s</div>' % text, unsafe_allow_html=True)
    if note:
        st.markdown('<div class="fs-note">%s</div>' % note, unsafe_allow_html=True)


def quote_box(text):
    st.markdown('<div class="fs-quote">%s</div>' % text, unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# 4. 사이드바
# ---------------------------------------------------------------------------

def render_sidebar():
    """
    사이드바를 그리고, 이번 화면에서 분석할 설정을 돌려준다.
    반환값: {"api_key":..., "corps":[...], "years":[...]} 또는 None(중단)
    """
    with st.sidebar:
        st.markdown("## 📊 FinSight")
        st.caption("규칙 기반 기업 재무·리스크 분석 엔진 · OpenDART 사업보고서")
        st.divider()

        api_key, source = key_from_config()
        if api_key:
            st.success("🔐 인증키 설정됨 · %s" % source)
        else:
            api_key = st.text_input(
                "OpenDART 인증키",
                type="password",
                placeholder="40자리 인증키",
                help="OpenDART에서 발급받은 인증키. Streamlit Cloud에서는 App settings → Secrets 에 넣어 두면 이 칸을 쓰지 않습니다.",
            ).strip()
            if not api_key:
                st.info(
                    "인증키가 필요합니다. [OpenDART](%s)에서 무료로 발급받아 "
                    "(하루 20,000건) 아래에 붙여 넣어 주세요." % DART_KEY_URL
                )
                st.stop()
        if len(api_key) != 40:
            st.warning("인증키는 보통 40자리입니다. 지금 입력한 키는 %d자리입니다." % len(api_key))

        try:
            with st.spinner("기업 목록 불러오는 중…"):
                corp_list = load_corp_list(api_key)
        except Exception as error:      # 키 오류·네트워크 오류를 화면에 그대로 안내
            st.error("기업 목록을 불러오지 못했습니다.\n\n%s" % error)
            st.stop()

        st.divider()
        st.markdown("##### 1️⃣ 기업 선택")
        main_corp = company_picker(corp_list, "main", "기업")

        st.markdown("##### 2️⃣ 경쟁사 비교")
        compare = st.checkbox("＋ 경쟁사 비교 추가", value=False,
                              help="켜면 비교 기업 1곳을 골라 두 회사를 나란히 비교합니다.")
        second_corp = None
        if compare:
            second_corp = company_picker(corp_list, "peer", "비교 기업",
                                         exclude_code=main_corp["corp_code"])
            if second_corp["corp_code"] == main_corp["corp_code"]:
                st.warning("비교 기업이 같습니다. 다른 회사를 골라 주세요.")

        st.divider()
        st.markdown("##### 3️⃣ 기준 연도")
        this_year = datetime.now().year
        year_options = list(range(this_year - 9, this_year))
        default_years = [this_year - 3, this_year - 2, this_year - 1]
        years = st.multiselect(
            "분석 연도 (2개 이상)",
            year_options,
            default=[y for y in default_years if y in year_options],
            help="사업보고서는 보통 다음 해 3월에 공시됩니다. 공시가 없는 연도는 자동으로 제외됩니다.",
        )

        st.divider()
        clicked = st.button("🔎 분석 실행", type="primary")

    corps = [main_corp] + ([second_corp] if second_corp else [])
    if len(years) < 2:
        st.error("기준 연도를 2개 이상 선택해 주세요. (연도별 추이·증감률 계산에 필요합니다)")
        return None
    if second_corp and second_corp["corp_code"] == main_corp["corp_code"]:
        st.error("비교 기업이 같습니다. 사이드바에서 다른 회사를 골라 주세요.")
        return None

    settings = {
        "api_key": api_key,
        "corps": corps,
        "years": sorted(years),
    }
    # 처음 접속했거나 '분석 실행'을 누른 경우 → 이 설정으로 분석
    if clicked or "params" not in st.session_state:
        st.session_state["params"] = settings
        st.session_state["auto_ran"] = True
    return settings


def params_changed(current, saved):
    if not saved:
        return True
    if saved["api_key"] != current["api_key"]:
        return True
    if [c["corp_code"] for c in saved["corps"]] != [c["corp_code"] for c in current["corps"]]:
        return True
    return list(saved["years"]) != list(current["years"])


# ---------------------------------------------------------------------------
# 5. 상단 요약
# ---------------------------------------------------------------------------

def render_hero(analysis):
    companies = analysis["companies"]
    years = analysis["years"]
    title = "  vs  ".join(c["name"] for c in companies)
    tags = " · ".join(
        "%s <span style='opacity:.75'>(%s)</span>" % (c["name"], c["stock_code"] or "-")
        for c in companies
    )
    fs_names = sorted({c.get("fs_nm") or "-" for c in companies})
    sub = (
        "%s<br>기준: %d~%d 사업보고서(연간) · %s · corp_code %s"
        % (
            tags,
            years[0],
            years[-1],
            " / ".join(fs_names),
            ", ".join(c["corp_code"] for c in companies),
        )
    )
    st.markdown(HERO_TMPL % {"title": title, "sub": sub}, unsafe_allow_html=True)


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
                '<div style="font-size:13px;font-weight:800;color:%s;margin:6px 0 6px 2px">▍%s</div>'
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
                ("자본잠식 · " if risk.get("capital_impaired") else "")
                + "%s · 평가 %d/5 항목 (N/A %d개 제외)"
                % (grade_label, risk["available_count"], 5 - risk["available_count"]),
                grade_color,
            ),
            unsafe_allow_html=True,
        )
        col2.markdown(
            card("최근 1년 공시 위험", d_value, d_sub, d_accent),
            unsafe_allow_html=True,
        )
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
        # 요약 카드 아래: 그 해 재무제표 원문 바로가기
        links_html = summary_source_links(company, latest)
        if links_html:
            st.markdown(links_html, unsafe_allow_html=True)
        st.write("")

    st.markdown(DISCLAIMER_TMPL, unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# 6. 표 만들기
# ---------------------------------------------------------------------------

def build_table(companies, years, rows, value_getter, digits=1):
    """
    companies × years × rows 를 한 표로. (비교 모드면 열 이름이 '회사 연도')
    value_getter(company, year, key) → 숫자 또는 None
    """
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
    st.markdown("**%s**" % title)
    st.markdown(financial_table_html(companies, years, rows), unsafe_allow_html=True)
    st.caption("단위: 억원 · **숫자를 클릭하면 그 해 DART 원문 재무제표가 새 탭으로 열립니다** "
               "· 연결재무제표 우선(없으면 별도) · 사업보고서(연간) 기준")


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


def statement_label(links, kind):
    label = ((links or {}).get("labels") or {}).get(kind) or ""
    return label or STATEMENT_FALLBACK_LABEL.get(kind, "재무제표")


def statements_of(company, year):
    """그 해 DART 재무제표 원문 링크 묶음 (없으면 빈 dict)."""
    if not company:
        return {}
    links = (company.get("statement_links") or {}).get(year)
    if links:
        return links
    return ((company.get("years") or {}).get(year) or {}).get("statement_links") or {}


def statement_target(company, year, key):
    """계정 키 → (원문 URL, 툴팁). 링크를 만들 수 없으면 ('', '')."""
    kind = ACCOUNT_STATEMENT.get(key, "income")
    links = statements_of(company, year)
    url = links.get(kind) or links.get("main") or ""
    if url:
        return url, "%d %s 원문 열기" % (year, statement_label(links, kind))
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


def summary_source_links(company, year):
    """요약 카드 아래 한 줄 — 그 해 재무제표 원문 바로가기(손익계산서 먼저)."""
    links = statements_of(company, year)
    parts = []
    for kind in ("income", "balance", "cashflow"):
        url = links.get(kind) or links.get("main") or ""
        if not url:
            continue
        label = statement_label(links, kind)
        parts.append('<a href="%s" target="_blank" rel="noopener noreferrer" title="%s">'
                     '📄 %d %s 원문 ↗</a>'
                     % (html_lib.escape(url, quote=True),
                        html_lib.escape("%d %s 원문 열기" % (year, label), quote=True),
                        year, html_lib.escape(label)))
    if not parts:
        return ""
    return '<div class="fs-srclinks">%s</div>' % '<span class="fs-sep">·</span>'.join(parts)


def table_ratios(companies, years):
    rows = [(key, "%s · %s" % (group, label))
            for key, label, _unit, group in fs.RATIO_LABELS]
    df = build_table(companies, years, rows, ratio_of)
    show_table(df, digits=1)
    st.caption("단위: % · 성장률은 전년 대비(YoY) · 첫 연도 성장률은 전년 데이터가 없어 비어 있습니다")
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
        # 범례를 차트 아래로 내리고 위·아래 여백을 넓혀 소제목("매출액 추이" 등)과 겹치지 않게 한다.
        margin=dict(l=10, r=14, t=58, b=84),
        font=dict(family="Pretendard, 'Apple SD Gothic Neo', 'Malgun Gothic', sans-serif",
                  size=12, color="#334155"),
        hoverlabel=dict(bgcolor="white", font_size=12),
        plot_bgcolor="white",
        bargap=0.28,
    )
    if legend:
        fig.update_layout(legend=dict(orientation="h", yanchor="top", y=-0.22, x=0,
                                      font=dict(size=11), bgcolor="rgba(0,0,0,0)"))
    fig.update_xaxes(showgrid=False, linecolor="#E2E8F0")
    # 억원 단위 값이 '3.5M' 같은 축약 표기 대신 '3,500,000'으로 보이게 한다.
    fig.update_yaxes(gridcolor="#EEF2F6", zerolinecolor="#CBD5E1",
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
            fig.add_trace(go.Scatter(
                x=xs,
                y=[(company["years"][y].get("ccc") or {}).get("ccc") for y in years],
                name="%s CCC" % company["name"], mode="lines+markers+text",
                text=text_labels([(company["years"][y].get("ccc") or {}).get("ccc") for y in years],
                                 0, "일"),
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
    table_financials(companies, years, CORE_ROWS, title="핵심 재무제표 (단위: 억원)")

    with st.expander("추가 계정 보기 (운전자본·현금흐름·이자비용)"):
        table_financials(companies, years, EXTRA_ROWS, title="추가 계정 (단위: 억원)")
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
        st.caption("ROE는 기말 자본 기준입니다. 기업분석 보고서의 (기초+기말)/2 평균자본 기준과는 값이 조금 다를 수 있습니다. 자본잠식(자본총계 0 이하)인 해는 부채비율·ROE를 계산하지 않습니다.")


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
# 11. 실행
# ---------------------------------------------------------------------------

def main():
    inject_css()
    settings = render_sidebar()
    if settings is None:
        return

    saved = st.session_state.get("params") or settings
    if params_changed(settings, saved):
        st.info("사이드바 설정이 바뀌었습니다. **🔎 분석 실행**을 누르면 이 설정으로 다시 분석합니다. "
                "지금 화면은 직전 설정(%s · %s~%s)의 결과입니다."
                % (" vs ".join(c["corp_name"] for c in saved["corps"]),
                   saved["years"][0], saved["years"][-1]))

    corps_key = tuple((c["corp_name"], c["corp_code"], c["stock_code"]) for c in saved["corps"])
    years_key = tuple(saved["years"])

    with st.spinner("DART에서 사업보고서를 가져와 분석하는 중입니다… (회사·연도당 1~2초, 첫 실행만)"):
        try:
            analysis = run_analysis(saved["api_key"], corps_key, years_key)
        except fs.DartError as error:
            if getattr(error, "status", None) == "years":
                st.error("분석할 수 있는 연도가 부족합니다. 사업보고서가 공시된 연도를 2개 이상 선택해 주세요. "
                         "(예: %d, %d)" % (datetime.now().year - 2, datetime.now().year - 1))
            else:
                st.error("분석 중 문제가 생겼습니다.\n\n%s" % error)
                st.caption("인증키·조회 연도·회사명을 확인해 주세요. OpenDART는 하루 20,000건까지 호출할 수 있습니다.")
            return
        except (urllib.error.URLError, OSError) as error:
            st.error("네트워크 오류로 OpenDART에 접속하지 못했습니다 — %s" % error)
            return
        except Exception as error:                     # 예상 못 한 오류도 화면에 안내
            st.error("분석 중 예상하지 못한 오류가 발생했습니다.\n\n%s: %s"
                     % (type(error).__name__, error))
            return

    render_hero(analysis)
    render_summary(analysis)
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
    st.caption(
        "데이터: 금융감독원 전자공시시스템(DART) Open API · 엔진: analyzer.py · "
        "모든 수치는 결정론적 Python 규칙으로 계산합니다(LLM 추론 미사용) · "
        "학습·포트폴리오 목적의 1차 스크리닝 도구이며 투자 권유가 아닙니다."
    )


main()
