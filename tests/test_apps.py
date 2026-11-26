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
