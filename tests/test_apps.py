# -*- coding: utf-8 -*-
"""
app_v2.py · app.py Streamlit 렌더링 테스트 (AppTest)
===================================================

네트워크 없이 화면이 예외 없이 그려지는지 확인한다.
  · DART 조회(fs.sync_corp_codes / fs.build_analysis)는 가짜 함수로 바꾼다.
  · 자본잠식 기업 데이터를 넣어 요약 카드·리스크 표·비율 표에 '자본잠식'이 보이는지도 본다.

실행
    python3 -m pytest tests/test_apps.py -v
"""

import os
import sys

import pytest

from streamlit.testing.v1 import AppTest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import analyzer as fs  # noqa: E402

APP_V2 = os.path.join(ROOT, "app_v2.py")
APP_V1 = os.path.join(ROOT, "app.py")

FAKE_KEY = "a" * 40
# 앱의 추천 기업(POPULAR) 목록에 있는 이름을 쓴다 — 검색어 없이도 후보가 그려진다.
CORP_LIST = [
    {"corp_name": "삼성전자", "corp_code": "00126380", "stock_code": "005930"},
    {"corp_name": "SK하이닉스", "corp_code": "00164779", "stock_code": "000660"},
]


# ---------------------------------------------------------------------------
# 가짜 분석 결과 — 자본잠식(자본총계 음수) 기업 1곳
# ---------------------------------------------------------------------------

def _company(name, corp_code, stock_code, impaired):
    accounts = {
        2023: {"revenue": {"amount": 1.2e12}, "op_income": {"amount": 8.0e10},
               "net_income": {"amount": 4.0e10}, "assets": {"amount": 2.0e12},
               "current_assets": {"amount": 9.0e11}, "liabilities": {"amount": 1.4e12},
               "current_liab": {"amount": 6.0e11}, "equity": {"amount": 6.0e11},
               "interest_expense": {"amount": 2.0e10}, "op_cash_flow": {"amount": 9.0e10},
               "retained_earnings": {"amount": 2.0e11}},
        2024: {"revenue": {"amount": 1.0e12}, "op_income": {"amount": -6.0e10},
               "net_income": {"amount": -3.0e10}, "assets": {"amount": 1.9e12},
               "current_assets": {"amount": 8.0e11}, "liabilities": {"amount": 1.95e12},
               "current_liab": {"amount": 1.0e12},
               "equity": {"amount": -5.0e10 if impaired else 5.0e10},
               "interest_expense": {"amount": 3.0e10}, "op_cash_flow": {"amount": -2.0e10},
               "retained_earnings": {"amount": -6.0e10}},
    }
    years_data = {}
    for year, acc in accounts.items():
        years_data[year] = {
            "accounts": acc,
            "rcept_no": "2025%08d" % year,
            "fs_div": "CFS",
            "statement_links": {"income": "", "balance": "", "cashflow": "", "main": "",
                                "labels": {}, "fallback": False},
        }
    years = sorted(years_data)
    fs.compute_ratios(years_data, years)
    fs.compute_working_capital(years_data, years)

    company = {
        "name": name, "corp_code": corp_code, "stock_code": stock_code,
        "years": years_data, "fs_div": "CFS", "fs_nm": "연결재무제표",
        "latest_rcept_no": years_data[years[-1]]["rcept_no"],
        "statement_links": {y: years_data[y]["statement_links"] for y in years},
        "disclosure": {"status": "clear", "matches": [], "scanned": 12, "total": 12,
                       "pages": 1, "truncated": False, "oldest": "2025-11-01",
                       "period": "2025-11-26 ~ 2026-11-26", "error": "", "corrections": 0},
    }
    company["risk"] = fs.evaluate_risk(company, years)
    company["dupont_comment"] = fs.make_dupont_comment(company, years[-1])
    company["ccc_comment"] = fs.make_ccc_comment(company, years)
    return company


def industry_block():
    """동종업계 비교 결과 묶음(엔진 계산 함수를 그대로 써서 실제 모양과 같게 만든다)."""
    target_values = {"op_margin": 12.0, "roe": 8.0, "debt_ratio": 70.0,
                     "current_ratio": 150.0, "ccc": 70.0}
    peers = [
        {"corp_code": "00066000", "corp_name": "LG전자(주)", "name": "LG전자",
         "stock_code": "066570", "induty_code": "264", "match": "same", "match_label": "동일 코드",
         "metrics": {"op_margin": 6.0, "roe": 5.0, "debt_ratio": 90.0,
                     "current_ratio": 120.0, "ccc": 80.0, "fs_nm": "연결재무제표"}},
        {"corp_code": "00164779", "corp_name": "에스케이하이닉스(주)", "name": "에스케이하이닉스",
         "stock_code": "000660", "induty_code": "2612", "match": "two", "match_label": "같은 2자리",
         "metrics": {"op_margin": 20.0, "roe": 18.0, "debt_ratio": 50.0,
                     "current_ratio": 180.0, "ccc": 60.0, "fs_nm": "연결재무제표"}},
    ]
    rows = fs.compare_peers(target_values, peers)
    return {
        "status": "ok", "name": "삼성전자", "year": 2024, "induty_code": "264",
        "industry_name": "통신 및 방송장비 제조업", "sector": "반도체·전자",
        "code_source": "seed", "levels": {"same": 1, "three": 0, "two": 1},
        "excluded_holdings": 0, "fs_nm": "연결재무제표",
        "target_values": target_values, "rows": rows, "peers": peers, "skipped": [],
        "comment": fs.make_industry_comment(rows, "삼성전자", len(peers), 2024),
    }


def build_analysis(impaired=True, with_industries=True):
    companies = [_company("삼성전자", "00126380", "005930", impaired=impaired)]
    return {
        "companies": companies,
        "years": [2023, 2024],
        "dupont_gap": None,
        "ccc_gap": None,
        "industry_peers": industry_block() if with_industries else None,
    }


# ── 원문 링크(viewer.do)를 넣은 결과 — 상단 중복 링크 정리 검증용 ───────────────────
VIEWER_DOC = ("https://dart.fss.or.kr/report/viewer.do"
              "?rcpNo=20250310002820&dcmNo=11104488&eleId=20&offset=376968&length=55064&dtd=dart4.xsd")


def attach_statement_links(company):
    """연도별 재무제표 직행 링크를 넣는다(시드/라이브 성공 케이스와 같은 모양)."""
    for year in list(company["years"]):
        links = {"income": VIEWER_DOC, "balance": VIEWER_DOC, "cashflow": VIEWER_DOC,
                 "main": "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=%s" % company["years"][year]["rcept_no"],
                 "labels": {"income": "연결손익계산서", "balance": "연결재무상태표",
                            "cashflow": "연결현금흐름표"},
                 "fallback": False}
        company["years"][year]["statement_links"] = links
        company.setdefault("statement_links", {})[year] = links
    return company


def build_analysis_with_links():
    analysis = build_analysis(impaired=False)
    attach_statement_links(analysis["companies"][0])
    return analysis


def build_pair_analysis_with_links():
    """2개사 비교 모드 — 각 기업 카드에 자기 원문 링크가 하나씩 붙는다."""
    first = attach_statement_links(_company("삼성전자", "00126380", "005930", impaired=False))
    second = attach_statement_links(_company("SK하이닉스", "00164779", "000660", impaired=False))
    companies = [first, second]
    years = [2023, 2024]
    return {
        "companies": companies,
        "years": years,
        "dupont_gap": fs.build_dupont_gap(companies, years[-1]),
        "ccc_gap": fs.build_ccc_gap(companies, years[-1]),
        "industry_peers": None,
    }


@pytest.fixture
def patched_engine(monkeypatch):
    """DART 네트워크 호출을 두 함수만 가짜로 바꾼다. (앱은 같은 fs 모듈을 쓴다)"""
    analysis = build_analysis(impaired=True)
    monkeypatch.setattr(fs, "sync_corp_codes", lambda *a, **k: list(CORP_LIST))
    monkeypatch.setattr(fs, "build_analysis", lambda *a, **k: analysis)
    return analysis


def _texts(at):
    parts = [m.value for m in at.markdown]
    parts += [c.value for c in at.caption]
    parts += [w.value for w in at.warning]
    return "\n".join(str(p) for p in parts)


# ---------------------------------------------------------------------------
# app_v2.py
# ---------------------------------------------------------------------------

def test_app_v2_renders_key_gate_without_exception(monkeypatch):
    monkeypatch.delenv("DART_API_KEY", raising=False)
    at = AppTest.from_file(APP_V2, default_timeout=60)
    at.run()
    assert not at.exception
    assert _texts(at)             # 화면에 안내 문구가 그려진다


def test_app_v2_renders_analysis_and_marks_capital_impairment(monkeypatch, patched_engine):
    monkeypatch.setenv("DART_API_KEY", FAKE_KEY)
    at = AppTest.from_file(APP_V2, default_timeout=60)
    at.run()
    assert not at.exception

    at.text_input(key="fs2_main_query").set_value("삼성")
    at.run()
    select = at.selectbox(key="fs2_main_select")
    assert select.options                       # 검색한 회사가 후보로 나온다
    at.get_by_key("fs2_run").click()
    at.run()

    assert not at.exception
    text = _texts(at)
    assert "자본잠식" in text                    # 요약 카드·리스크 표·비율 표 안내
    assert "삼성전자" in text


def test_app_v2_offers_excel_report_download(monkeypatch, patched_engine):
    """결과 상단에 엑셀(.xlsx) 다운로드 버튼과 4개 시트 안내가 그려진다."""
    monkeypatch.setenv("DART_API_KEY", FAKE_KEY)
    at = AppTest.from_file(APP_V2, default_timeout=60)
    at.run()
    assert not at.exception

    at.text_input(key="fs2_main_query").set_value("삼성")
    at.run()
    at.get_by_key("fs2_run").click()
    at.run()

    assert not at.exception
    downloads = at.download_button
    assert len(downloads) == 1
    assert "엑셀(.xlsx) 리포트 다운로드" in downloads[0].label
    assert "4개 시트" in _texts(at)


def test_app_v2_shows_one_statement_link_per_company(monkeypatch):
    """단일 기업 모드 — 원문 링크는 기업당 하나, 재무제표 직행(viewer.do)으로 간다."""
    analysis = build_analysis_with_links()
    monkeypatch.setattr(fs, "sync_corp_codes", lambda *a, **k: list(CORP_LIST))
    monkeypatch.setattr(fs, "build_analysis", lambda *a, **k: analysis)
    monkeypatch.setenv("DART_API_KEY", "d" * 40)     # 실행 캐시를 타지 않게 다른 키

    at = AppTest.from_file(APP_V2, default_timeout=60)
    at.run()
    at.text_input(key="fs2_main_query").set_value("삼성")
    at.run()
    at.get_by_key("fs2_run").click()
    at.run()

    assert not at.exception
    markdown = "\n".join(m.value for m in at.markdown)
    assert markdown.count("원문 ↗") == 1              # 위·아래 두 번 뜨지 않는다
    assert "2024 원문 ↗" in markdown                  # 남은 하나는 기업 카드 헤더
    assert "삼성전자 원문 ↗" not in markdown           # 헤더의 중복 링크는 제거
    assert "report/viewer.do" in markdown              # menu.do 표지가 아니라 재무제표 직행
    assert "2024 연결손익계산서 원문 열기" in markdown


def test_app_v2_compare_mode_links_each_card_to_viewer(monkeypatch):
    """2개사 비교 모드 — 각 기업 카드의 '{최신연도} 원문 ↗'이 viewer.do 로 간다."""
    single = build_analysis_with_links()
    pair = build_pair_analysis_with_links()

    def fake_build(api_key, corps, years, verbose=True):
        return pair if len(corps) > 1 else single

    monkeypatch.setattr(fs, "sync_corp_codes", lambda *a, **k: list(CORP_LIST))
    monkeypatch.setattr(fs, "build_analysis", fake_build)
    monkeypatch.setenv("DART_API_KEY", "e" * 40)

    at = AppTest.from_file(APP_V2, default_timeout=60)
    at.run()
    at.text_input(key="fs2_main_query").set_value("삼성")
    at.run()
    at.checkbox(key="fs2_compare").check()
    at.run()
    at.get_by_key("fs2_run").click()
    at.run()

    assert not at.exception
    markdown = "\n".join(m.value for m in at.markdown)
    assert markdown.count("원문 ↗") == 2               # 기업 카드마다 하나씩
    assert markdown.count("report/viewer.do") >= 2      # 둘 다 viewer.do 직행
    assert "SK하이닉스" in markdown
    assert "삼성전자 원문 ↗" not in markdown


def test_app_v2_renders_industry_comparison(monkeypatch, patched_engine):
    """동종업계 비교 — 요약 한 줄 · 비교 표(대상/피어 평균/피어 중앙값/개별 피어)가 그려진다."""
    monkeypatch.setenv("DART_API_KEY", FAKE_KEY)
    at = AppTest.from_file(APP_V2, default_timeout=60)
    at.run()
    assert not at.exception

    at.text_input(key="fs2_main_query").set_value("삼성")
    at.run()
    at.get_by_key("fs2_run").click()
    at.run()

    assert not at.exception
    text = _texts(at)
    assert "동종업계 비교" in text
    assert "피어 중앙값" in text                  # 표 행
    assert "LG전자" in text                       # 개별 피어
    assert "같은 2자리" in text                   # 피어 선정 기준 표기
    assert "업종코드 264" in text


def test_app_v2_industry_block_missing_is_quiet(monkeypatch):
    """동종업계 비교가 없어도(구버전 결과·업종 미확인) 화면은 그대로 그려진다."""
    analysis = build_analysis(impaired=False, with_industries=False)
    monkeypatch.setattr(fs, "sync_corp_codes", lambda *a, **k: list(CORP_LIST))
    monkeypatch.setattr(fs, "build_analysis", lambda *a, **k: analysis)
    # run_analysis 는 (키, 기업, 연도)로 캐시된다 — 앞선 테스트의 캐시를 타지 않게 다른 키를 쓴다.
    monkeypatch.setenv("DART_API_KEY", "b" * 40)

    at = AppTest.from_file(APP_V2, default_timeout=60)
    at.run()
    at.text_input(key="fs2_main_query").set_value("삼성")
    at.run()
    at.get_by_key("fs2_run").click()
    at.run()

    assert not at.exception
    assert "동종업계 비교를 건너뛰었습니다" in _texts(at)


# ---------------------------------------------------------------------------
# app.py (사이드바 버전)
# ---------------------------------------------------------------------------

def test_app_v1_renders_analysis_and_marks_capital_impairment(monkeypatch, patched_engine):
    monkeypatch.setenv("DART_API_KEY", FAKE_KEY)
    at = AppTest.from_file(APP_V1, default_timeout=60)
    at.run()
    assert not at.exception

    # 사이드바 '🔎 분석 실행' 버튼 (이 화면의 유일한 버튼)
    buttons = at.sidebar.button
    assert len(buttons) >= 1
    buttons[0].click()
    at.run()

    assert not at.exception
    text = _texts(at)
    assert "자본잠식" in text
    assert "삼성전자" in text


# ---------------------------------------------------------------------------
# 탭 ④ — FP&A 3개년 시나리오 시뮬레이터
# ---------------------------------------------------------------------------

def ccc_analysis():
    """운전자본 지표(DSO·DIO·DPO·CCC)가 있는 1개사 분석 — 시뮬레이터 숫자까지 그려 본다."""
    company = _company("삼성전자", "00126380", "005930", impaired=False)
    company["years"][2024]["accounts"].update({
        "cost_of_sales": {"amount": 7.0e11},
        "receivables": {"amount": 2.0e11},
        "inventory": {"amount": 1.0e11},
        "payables": {"amount": 8.0e10},
    })
    fs.compute_working_capital(company["years"], [2023, 2024])
    company["ccc_comment"] = fs.make_ccc_comment(company, [2023, 2024])
    return {"companies": [company], "years": [2023, 2024], "dupont_gap": None,
            "ccc_gap": None, "industry_peers": None}


def _run_analysis_app(at):
    at.run()
    assert not at.exception
    at.text_input(key="fs2_main_query").set_value("삼성")
    at.run()
    at.get_by_key("fs2_run").click()
    at.run()
    return at


def test_app_v2_renders_fpa_scenario_tab_without_exception(monkeypatch, patched_engine):
    """탭 ④·가정 슬라이더 3개·시나리오 표·한계 고지가 그려진다(CCC 미공시 기업)."""
    monkeypatch.setenv("DART_API_KEY", FAKE_KEY)
    at = _run_analysis_app(AppTest.from_file(APP_V2, default_timeout=60))

    assert not at.exception
    assert [tab.label for tab in at.tabs] == [
        "① 핵심 재무 & 추이",
        "② FP&A 심층 · DuPont & CCC",
        "③ 리스크 상세 & DART 원문",
        "④ FP&A 시나리오",
    ]
    assert [slider.label for slider in at.slider] == [
        "매출 성장률 (연, %)", "영업이익률 변동 (%p)", "CCC 변동 (일)"]

    text = _texts(at)
    assert "FP&A 3개년 시나리오 시뮬레이터" in text
    assert "시나리오 비교" in text
    assert "내 시나리오" in text
    assert "투자 권유가 아니" in text                          # 한계 고지
    assert "CCC를 공시하지 않는 업종" in text                    # 금융업 등 폴백 안내
    assert any("시뮬레이션 계산식 보기" in e.label for e in at.expander)

    comparison = [d.value for d in at.dataframe if "시나리오" in list(d.value.columns)]
    assert len(comparison) == 1
    assert list(comparison[0]["시나리오"]) == ["보수적 (Bear)", "기준 (Base)", "낙관적 (Bull)", "내 시나리오"]
    assert "Y+1 매출" in comparison[0].columns and "3년 누적 현금 영향" in comparison[0].columns


def test_app_v2_fpa_simulator_recalculates_when_slider_moves(monkeypatch):
    """슬라이더를 움직이면 내 시나리오 숫자가 다시 계산된다(실행 버튼 불필요)."""
    analysis = ccc_analysis()
    monkeypatch.setattr(fs, "sync_corp_codes", lambda *a, **k: list(CORP_LIST))
    monkeypatch.setattr(fs, "build_analysis", lambda *a, **k: analysis)
    monkeypatch.setenv("DART_API_KEY", "f" * 40)

    at = _run_analysis_app(AppTest.from_file(APP_V2, default_timeout=60))
    assert not at.exception

    comparison = [d.value for d in at.dataframe if "시나리오" in list(d.value.columns)][0]
    custom_row = comparison.iloc[3]
    assert custom_row["시나리오"] == "내 시나리오"
    assert custom_row["Y+3 매출"] == 10000                      # 1조원 = 10,000억 (가정 0%)
    base_row = comparison.iloc[1]
    assert base_row["Y+3 매출"] == 10000

    # 매출 성장률 +20%로 움직이면 10,000 × 1.2³ = 17,280억
    at.slider(key="fs2_fpa_growth_00126380").set_value(20.0)
    at.run()
    assert not at.exception

    moved = [d.value for d in at.dataframe if "시나리오" in list(d.value.columns)][0]
    assert moved.iloc[3]["Y+3 매출"] == 17280
    assert moved.iloc[3]["가정"].startswith("성장 +20.0%")
    # 상세 표에 Y+1~Y+3 행과 CCC가 그대로 있다
    detail = [d.value for d in at.dataframe if "연도" in list(d.value.columns)][0]
    assert list(detail["연도"]) == ["Y+1 · 2025년", "Y+2 · 2026년", "Y+3 · 2027년"]
    assert detail["CCC(일)"].tolist() == [83, 83, 83]             # 73.0 + 52.1 − 41.7 ≈ 83.4일


def test_app_v2_fpa_simulator_survives_missing_industry_and_single_year(monkeypatch):
    """업종 비교가 없고 연도가 2개뿐인 결과에서도 탭 ④는 예외 없이 그려진다."""
    analysis = ccc_analysis()
    monkeypatch.setattr(fs, "sync_corp_codes", lambda *a, **k: list(CORP_LIST))
    monkeypatch.setattr(fs, "build_analysis", lambda *a, **k: analysis)
    monkeypatch.setenv("DART_API_KEY", "g" * 40)

    at = _run_analysis_app(AppTest.from_file(APP_V2, default_timeout=60))
    assert not at.exception
    assert "시나리오 비교" in _texts(at)
