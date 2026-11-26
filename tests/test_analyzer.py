# -*- coding: utf-8 -*-
"""
analyzer.py 계산 함수 단위 테스트 (pytest)
===========================================

네트워크(DART API) 호출 없이, 손으로 만든 작은 입력값만으로 계산 로직을 검증한다.

    · compute_ratios()            — 7대 재무비율 + DuPont 3단계 분해
    · compute_working_capital()   — DSO / DIO / DPO / CCC
    · safe_ratio() / growth_rate() — 위 두 함수가 쓰는 나눗셈·증감률 규칙

실행
    python3 -m pytest tests/ -v

각 테스트의 기댓값은 주석에 계산 과정을 적어 두었다(계산기 검산값).
"""

import json
import os
import re
import sys
import threading
import time
import urllib.error
from io import BytesIO

import pytest
from openpyxl import load_workbook

# 저장소 루트의 analyzer.py 를 불러온다. (tests/ 의 부모 폴더)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import analyzer as fs  # noqa: E402
import excel_export as xl  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_statement_links(monkeypatch):
    """
    테스트가 저장소에 동봉된 실제 시드(statement_links_seed.json)에 좌우되지 않게 격리한다.

    시드 조회를 검증하는 테스트는 이 안에서 시드를 직접 채운다.
    (실제 시드 파일 내용은 test_statement_links_seed_file_shipped 가 따로 확인한다)
    """
    monkeypatch.setattr(fs, "_STATEMENT_LINKS_SEED", {})
    fs._STATEMENT_LINKS_CACHE.clear()
    yield
    fs._STATEMENT_LINKS_CACHE.clear()


# ---------------------------------------------------------------------------
# 도우미
# ---------------------------------------------------------------------------

def make_years(spec):
    """
    {연도: {계정키: 금액}} → (엔진이 쓰는 years_data, 정렬된 연도 리스트)

    엔진은 year["accounts"][key]["amount"] 형태로 읽는다.
    """
    data = {}
    for year, accounts in spec.items():
        data[year] = {"accounts": {key: {"amount": value} for key, value in accounts.items()}}
    return data, sorted(spec)


# 정상값 2개년 — 2024년 비율을 손으로 검산하기 좋은 숫자로 골랐다.
NORMAL = {
    2023: {"revenue": 1000, "op_income": 100, "net_income": 80,
           "assets": 2000, "current_assets": 800, "liabilities": 600,
           "current_liab": 400, "equity": 1400},
    2024: {"revenue": 1200, "op_income": 150, "net_income": 120,
           "assets": 2200, "current_assets": 900, "liabilities": 700,
           "current_liab": 450, "equity": 1500},
}

# 전기(2023) 영업이익이 적자인 케이스
NEGATIVE_BASE = {
    2023: {"revenue": 800, "op_income": -200, "net_income": -50,
           "assets": 1000, "current_assets": 300, "liabilities": 600,
           "current_liab": 200, "equity": 400},
    2024: {"revenue": 1000, "op_income": 100, "net_income": 60,
           "assets": 1100, "current_assets": 400, "liabilities": 640,
           "current_liab": 250, "equity": 460},
}

# 분모가 0이거나 계정이 아예 없는 케이스
ZERO_OR_MISSING = {
    2023: {"revenue": 0, "op_income": 0, "net_income": 0, "assets": 0,
           "current_assets": 0, "liabilities": 0, "equity": 0},   # current_liab 없음
    2024: {"revenue": 500, "op_income": 25, "net_income": 10, "assets": 1000,
           "current_assets": 300, "current_liab": 200, "liabilities": 500, "equity": 500},
}

# 매출액만 있는 최소 입력
ONLY_REVENUE = {2023: {"revenue": 1000}, 2024: {"revenue": 1100}}


# ---------------------------------------------------------------------------
# 1. safe_ratio / growth_rate — 계산의 바닥 규칙
# ---------------------------------------------------------------------------

def test_safe_ratio_normal():
    # 150 ÷ 1200 × 100 = 12.5
    assert fs.safe_ratio(150, 1200) == pytest.approx(12.5)


def test_safe_ratio_zero_denominator_is_none():
    # 분모가 0이면 계산 불가 → None (ZeroDivisionError 를 내지 않는다)
    assert fs.safe_ratio(150, 0) is None


def test_safe_ratio_none_numerator_is_none():
    # 분자가 None(계정 미공시)이면 계산 불가 → None
    assert fs.safe_ratio(None, 1200) is None


def test_growth_rate_normal():
    # (1200 − 1000) ÷ 1000 × 100 = 20.0
    assert fs.growth_rate(1200, 1000) == pytest.approx(20.0)


def test_growth_rate_negative_base():
    # 전기가 적자(-200) → (100 − (-200)) ÷ |-200| × 100 = 300 ÷ 200 × 100 = 150.0
    # 절댓값으로 나누므로 부호가 뒤집히지 않는다.
    assert fs.growth_rate(100, -200) == pytest.approx(150.0)


def test_growth_rate_zero_or_none_base_is_none():
    assert fs.growth_rate(1000, 0) is None      # 전기 0 → 나눌 수 없음
    assert fs.growth_rate(None, 1000) is None   # 당기 없음


# ---------------------------------------------------------------------------
# 2. compute_ratios — 정상값 (7대 비율 + DuPont)
# ---------------------------------------------------------------------------

def test_compute_ratios_normal_2024():
    years_data, years = make_years(NORMAL)
    fs.compute_ratios(years_data, years)
    r = years_data[2024]["ratios"]

    assert r["op_margin"] == pytest.approx(12.5)        # 150 ÷ 1200 × 100
    assert r["net_margin"] == pytest.approx(10.0)       # 120 ÷ 1200 × 100
    assert r["roe"] == pytest.approx(8.0)               # 120 ÷ 1500 × 100
    assert r["debt_ratio"] == pytest.approx(46.666667, rel=1e-6)   # 700 ÷ 1500 × 100
    assert r["current_ratio"] == pytest.approx(200.0)   # 900 ÷ 450 × 100
    assert r["revenue_growth"] == pytest.approx(20.0)   # (1200 − 1000) ÷ 1000 × 100
    assert r["op_growth"] == pytest.approx(50.0)        # (150 − 100) ÷ 100 × 100
    assert r["roa"] == pytest.approx(5.454545, rel=1e-6)  # 120 ÷ 2200 × 100


def test_compute_ratios_first_year_growth_is_none():
    years_data, years = make_years(NORMAL)
    fs.compute_ratios(years_data, years)
    first = years_data[2023]["ratios"]

    # 2023년은 전년 데이터가 없으므로 성장률 두 개가 비어 있다.
    assert first["revenue_growth"] is None
    assert first["op_growth"] is None
    # 나머지는 계산된다: 100 ÷ 1000 × 100 = 10.0 / 80 ÷ 1400 × 100 = 5.714286
    assert first["op_margin"] == pytest.approx(10.0)
    assert first["roe"] == pytest.approx(5.714286, rel=1e-6)


def test_compute_dupont_normal():
    years_data, years = make_years(NORMAL)
    fs.compute_ratios(years_data, years)
    dupont = years_data[2024]["dupont"]

    assert dupont["net_margin"] == pytest.approx(10.0)                # 120 ÷ 1200 × 100
    assert dupont["asset_turnover"] == pytest.approx(0.545455, rel=1e-6)  # 1200 ÷ 2200 (기말 자산)
    assert dupont["equity_multiplier"] == pytest.approx(1.466667, rel=1e-6)  # 2200 ÷ 1500
    # 10.0 × 0.545455 × 1.466667 = 8.0  (단순 ROE 120 ÷ 1500 × 100 = 8.0 과 일치)
    assert dupont["roe"] == pytest.approx(8.0)
    assert dupont["roe_simple"] == pytest.approx(8.0)
    assert dupont["diff"] == pytest.approx(0.0, abs=1e-9)
    assert dupont["assets_basis"] == "기말"


def test_compute_dupont_first_year_also_matches():
    years_data, years = make_years(NORMAL)
    fs.compute_ratios(years_data, years)
    dupont = years_data[2023]["dupont"]

    # 0.5회 = 1000 ÷ 2000, 1.428571배 = 2000 ÷ 1400
    # 8.0 × 0.5 × 1.428571 = 5.714286 → 단순 ROE 80 ÷ 1400 × 100 = 5.714286
    assert dupont["asset_turnover"] == pytest.approx(0.5)
    assert dupont["equity_multiplier"] == pytest.approx(1.428571, rel=1e-6)
    assert dupont["roe"] == pytest.approx(5.714286, rel=1e-6)
    assert dupont["diff"] == pytest.approx(0.0, abs=1e-6)


# ---------------------------------------------------------------------------
# 3. compute_ratios — 전기가 적자인 경우
# ---------------------------------------------------------------------------

def test_compute_ratios_negative_base_growth():
    years_data, years = make_years(NEGATIVE_BASE)
    fs.compute_ratios(years_data, years)
    r = years_data[2024]["ratios"]

    # (100 − (-200)) ÷ |-200| × 100 = 150.0
    assert r["op_growth"] == pytest.approx(150.0)
    # (1000 − 800) ÷ 800 × 100 = 25.0
    assert r["revenue_growth"] == pytest.approx(25.0)
    # 60 ÷ 1000 × 100 = 6.0 / 60 ÷ 460 × 100 = 13.043478
    assert r["net_margin"] == pytest.approx(6.0)
    assert r["roe"] == pytest.approx(13.043478, rel=1e-6)


def test_compute_ratios_negative_base_flag():
    years_data, years = make_years(NEGATIVE_BASE)
    fs.compute_ratios(years_data, years)

    # 전년 영업이익이 적자였던 해의 증가율은 참고치 → 화면에 '*' 로 표시된다.
    assert years_data[2024]["op_growth_base_negative"] is True
    # 첫해는 비교 대상이 없으므로 False
    assert years_data[2023]["op_growth_base_negative"] is False

    # 반대로 전년이 흑자였던 정상 케이스는 False
    normal_data, normal_years = make_years(NORMAL)
    fs.compute_ratios(normal_data, normal_years)
    assert normal_data[2024]["op_growth_base_negative"] is False


def test_compute_dupont_negative_base():
    years_data, years = make_years(NEGATIVE_BASE)
    fs.compute_ratios(years_data, years)
    dupont = years_data[2024]["dupont"]

    # 순이익률 6.0 × (1000 ÷ 1100 = 0.909091) × (1100 ÷ 460 = 2.391304) = 13.043478
    assert dupont["roe"] == pytest.approx(13.043478, rel=1e-6)
    assert dupont["roe_simple"] == pytest.approx(13.043478, rel=1e-6)   # 60 ÷ 460 × 100
    assert dupont["diff"] == pytest.approx(0.0, abs=1e-6)


# ---------------------------------------------------------------------------
# 4. compute_ratios — 분모 0 / 값 None
# ---------------------------------------------------------------------------

def test_compute_ratios_zero_denominators_are_none():
    years_data, years = make_years(ZERO_OR_MISSING)
    fs.compute_ratios(years_data, years)
    first = years_data[2023]["ratios"]

    # 2023년은 매출·자본이 모두 0 → 모든 비율이 계산 불가(None)
    assert first["op_margin"] is None       # 0 ÷ 0
    assert first["net_margin"] is None
    assert first["roe"] is None             # 자본 0
    assert first["debt_ratio"] is None      # 자본 0
    assert first["current_ratio"] is None   # 유동부채 계정 없음
    assert first["roa"] is None             # 자산 0


def test_compute_ratios_zero_previous_revenue_skips_growth():
    years_data, years = make_years(ZERO_OR_MISSING)
    fs.compute_ratios(years_data, years)

    # 전기 매출 0 → 증가율을 만들 수 없다(None). 0으로 나누지 않는다.
    assert years_data[2024]["ratios"]["revenue_growth"] is None
    # 전기 영업이익 0 → 마찬가지로 None
    assert years_data[2024]["ratios"]["op_growth"] is None
    # 나머지는 정상 계산: 25 ÷ 500 × 100 = 5.0
    assert years_data[2024]["ratios"]["op_margin"] == pytest.approx(5.0)


def test_compute_dupont_with_zero_equity_is_none():
    years_data, years = make_years(ZERO_OR_MISSING)
    fs.compute_ratios(years_data, years)

    # 자본 0 → 재무레버리지 계산 불가 → DuPont ROE 도 None
    assert years_data[2023]["dupont"]["equity_multiplier"] is None
    assert years_data[2023]["dupont"]["roe"] is None
    assert years_data[2023]["dupont"]["diff"] is None


def test_compute_ratios_only_revenue():
    years_data, years = make_years(ONLY_REVENUE)
    fs.compute_ratios(years_data, years)
    second = years_data[2024]["ratios"]

    # 매출액만 있으면 나머지 비율은 전부 None 이지만, 증가율은 계산된다.
    assert second["op_margin"] is None
    assert second["roe"] is None
    assert second["debt_ratio"] is None
    assert second["revenue_growth"] == pytest.approx(10.0)   # (1100 − 1000) ÷ 1000 × 100
    assert second["op_growth"] is None                       # 영업이익 없음
    assert years_data[2024]["dupont"]["roe"] is None


# ---------------------------------------------------------------------------
# 5. compute_working_capital — DSO / DIO / DPO / CCC
# ---------------------------------------------------------------------------

def test_compute_working_capital_normal():
    years_data, years = make_years({
        2024: {"revenue": 1000, "cost_of_sales": 800,
               "receivables": 100, "inventory": 200, "payables": 150},
    })
    fs.compute_working_capital(years_data, years)
    wc = years_data[2024]["ccc"]

    # DSO = 100 ÷ 1000 × 365 = 36.5일
    # DIO = 200 ÷ 800 × 365 = 91.25일
    # DPO = 150 ÷ 800 × 365 = 68.4375일
    # CCC = 36.5 + 91.25 − 68.4375 = 59.3125일
    assert wc["dso"] == pytest.approx(36.5)
    assert wc["dio"] == pytest.approx(91.25)
    assert wc["dpo"] == pytest.approx(68.4375)
    assert wc["ccc"] == pytest.approx(59.3125)


def test_compute_working_capital_keeps_inputs():
    years_data, years = make_years({
        2024: {"revenue": 1000, "cost_of_sales": 800,
               "receivables": 100, "inventory": 200, "payables": 150},
    })
    fs.compute_working_capital(years_data, years)

    # 화면에서 "왜 N/A인지" 보여주기 위한 원재료도 남는다.
    inputs = years_data[2024]["ccc"]["inputs"]
    assert inputs["revenue"] == 1000
    assert inputs["cost_of_sales"] == 800
    assert inputs["receivables"] == 100
    assert inputs["inventory"] == 200
    assert inputs["payables"] == 150


def test_compute_working_capital_missing_cost_of_sales():
    years_data, years = make_years({
        2024: {"revenue": 1000, "receivables": 100, "inventory": 200, "payables": 150},
    })
    fs.compute_working_capital(years_data, years)
    wc = years_data[2024]["ccc"]

    # 매출원가 미공시 → DIO·DPO 를 만들 수 없고, CCC 도 계산하지 않는다.
    assert wc["dso"] == pytest.approx(36.5)
    assert wc["dio"] is None
    assert wc["dpo"] is None
    assert wc["ccc"] is None
    assert wc["inputs"]["cost_of_sales"] is None


def test_compute_working_capital_zero_revenue():
    years_data, years = make_years({
        2024: {"revenue": 0, "cost_of_sales": 800,
               "receivables": 100, "inventory": 200, "payables": 150},
    })
    fs.compute_working_capital(years_data, years)
    wc = years_data[2024]["ccc"]

    # 매출 0 → DSO 계산 불가(None) → CCC 도 None (0으로 나누지 않는다)
    assert wc["dso"] is None
    assert wc["dio"] == pytest.approx(91.25)     # 200 ÷ 800 × 365
    assert wc["dpo"] == pytest.approx(68.4375)   # 150 ÷ 800 × 365
    assert wc["ccc"] is None


def test_compute_working_capital_zero_cost_of_sales():
    years_data, years = make_years({
        2024: {"revenue": 1000, "cost_of_sales": 0,
               "receivables": 100, "inventory": 200, "payables": 150},
    })
    fs.compute_working_capital(years_data, years)
    wc = years_data[2024]["ccc"]

    # 매출원가 0 → DIO·DPO 를 0으로 나눌 수 없다.
    assert wc["dso"] == pytest.approx(36.5)
    assert wc["dio"] is None
    assert wc["dpo"] is None
    assert wc["ccc"] is None


def test_compute_working_capital_two_years():
    years_data, years = make_years({
        2023: {"revenue": 900, "cost_of_sales": 700,
               "receivables": 90, "inventory": 180, "payables": 140},
        2024: {"revenue": 1000, "cost_of_sales": 800,
               "receivables": 100, "inventory": 200, "payables": 150},
    })
    fs.compute_working_capital(years_data, years)

    # 2023: DSO 90 ÷ 900 × 365 = 36.5 / DIO 180 ÷ 700 × 365 = 93.857143
    #       DPO 140 ÷ 700 × 365 = 73.0 → CCC = 36.5 + 93.857143 − 73.0 = 57.357143
    previous = years_data[2023]["ccc"]
    assert previous["dso"] == pytest.approx(36.5)
    assert previous["dio"] == pytest.approx(93.857143, rel=1e-6)
    assert previous["dpo"] == pytest.approx(73.0)
    assert previous["ccc"] == pytest.approx(57.357143, rel=1e-6)
    # 2024는 위 정상 케이스와 같은 59.3125일
    assert years_data[2024]["ccc"]["ccc"] == pytest.approx(59.3125)


# ---------------------------------------------------------------------------
# DART 원문 직행 링크 (fetch_statement_links) — 네트워크 없이 파싱만 검증
# ---------------------------------------------------------------------------
# DART main.do 는 목차를 아래처럼 JavaScript 로 심어 보낸다(실제 응답에서 따온 형식).
SAMPLE_MAIN_HTML = """
<script type="text/javascript">
var node1 = {};
node1['text'] = "사업보고서 (2025.12)";
node1['id'] = "1";
node1['rcpNo'] = "20260310002820";
node1['dcmNo'] = "11104488";
node1['eleId'] = "1";
node1['offset'] = "957";
node1['length'] = "4524";
node1['dtd'] = "dart4.xsd";
node1['children'] = [];
var node2 = {};
node2['text'] = "2. 연결재무제표";
node2['id'] = "19";
node2['rcpNo'] = "20260310002820";
node2['dcmNo'] = "11104488";
node2['eleId'] = "19";
node2['offset'] = "376820";
node2['length'] = "258279";
node2['dtd'] = "dart4.xsd";
node2['children'] = [];
var node3 = {};
node3['text'] = "2-1. 연결 재무상태표";
node3['id'] = "20";
node3['rcpNo'] = "20260310002820";
node3['dcmNo'] = "11104488";
node3['eleId'] = "20";
node3['offset'] = "376968";
node3['length'] = "55064";
node3['dtd'] = "dart4.xsd";
var node3 = {};
node3['text'] = "2-3. 연결 포괄손익계산서";
node3['id'] = "22";
node3['rcpNo'] = "20260310002820";
node3['dcmNo'] = "11104488";
node3['eleId'] = "22";
node3['offset'] = "451713";
node3['length'] = "17431";
node3['dtd'] = "dart4.xsd";
var node3 = {};
node3['text'] = "2-5. 연결 현금흐름표";
node3['id'] = "24";
node3['rcpNo'] = "20260310002820";
node3['dcmNo'] = "11104488";
node3['eleId'] = "24";
node3['offset'] = "587836";
node3['length'] = "47267";
node3['dtd'] = "dart4.xsd";
var node2 = {};
node2['text'] = "4-1. 재무상태표";
node2['id'] = "41";
node2['rcpNo'] = "20260310002820";
node2['dcmNo'] = "11104488";
node2['eleId'] = "41";
node2['offset'] = "900000";
node2['length'] = "30000";
node2['dtd'] = "dart4.xsd";
var node3 = {};
node3['text'] = "27. 현금흐름표 (연결)";
node3['id'] = "77";
node3['rcpNo'] = "20260310002820";
node3['dcmNo'] = "11104488";
node3['eleId'] = "77";
node3['offset'] = "555555";
node3['length'] = "11111";
node3['dtd'] = "dart4.xsd";
</script>
"""

RCEPT = "20260310002820"


def _use_sample(monkeypatch, html=SAMPLE_MAIN_HTML):
    """네트워크 대신 위 샘플 HTML 을 돌려주게 하고, 캐시를 비운다."""
    fs._STATEMENT_LINKS_CACHE.clear()
    monkeypatch.setattr(fs, "_fetch_dart_html", lambda url, timeout: html)


def test_parse_toc_nodes_reads_text_and_viewdoc_params():
    nodes = fs.parse_toc_nodes(SAMPLE_MAIN_HTML)
    texts = [node["text"] for node in nodes]
    assert texts[0] == "사업보고서 (2025.12)"
    assert "2-1. 연결 재무상태표" in texts
    balance = next(node for node in nodes if node["text"] == "2-1. 연결 재무상태표")
    assert balance["rcpNo"] == RCEPT
    assert balance["dcmNo"] == "11104488"
    assert balance["eleId"] == "20"
    assert balance["offset"] == "376968"
    assert balance["length"] == "55064"
    assert balance["dtd"] == "dart4.xsd"


def test_parse_toc_nodes_empty_html():
    assert fs.parse_toc_nodes("") == []
    assert fs.parse_toc_nodes("<html>목차 없음</html>") == []


def test_toc_label_strips_numbering_and_spaces():
    assert fs.toc_label("2-1. 연결 재무상태표") == "연결재무상태표"
    assert fs.toc_label("27. 현금흐름표 (연결)") == "현금흐름표(연결)"
    assert fs.toc_label("Ⅱ. 연결재무제표") == "연결재무제표"
    assert fs.toc_label("연결 손익계산서") == "연결손익계산서"


def test_viewer_url_needs_every_parameter():
    node = {"rcpNo": RCEPT, "dcmNo": "11104488", "eleId": "20",
            "offset": "376968", "length": "55064", "dtd": "dart4.xsd"}
    url = fs.viewer_url(node)
    assert url.startswith("https://dart.fss.or.kr/report/viewer.do?")
    assert "rcpNo=" + RCEPT in url
    assert "dcmNo=11104488" in url
    assert "eleId=20" in url
    assert "offset=376968" in url
    assert "length=55064" in url
    assert "dtd=dart4.xsd" in url
    # 하나라도 빠지면 직행 링크를 만들지 않는다(→ main.do 로 물러남)
    assert fs.viewer_url({k: v for k, v in node.items() if k != "dtd"}) == ""
    assert fs.viewer_url(None) == ""


def test_fetch_statement_links_prefers_consolidated(monkeypatch):
    _use_sample(monkeypatch)
    links = fs.fetch_statement_links(RCEPT)

    # 연결 손익계산서가 없으므로 연결 포괄손익계산서(eleId 22)를 쓴다.
    assert links["labels"]["income"] == "연결포괄손익계산서"
    assert "eleId=22" in links["income"]
    assert links["labels"]["balance"] == "연결재무상태표"
    assert "eleId=20" in links["balance"]
    assert links["labels"]["cashflow"] == "연결현금흐름표"
    assert "eleId=24" in links["cashflow"]
    # 주석 속 "27. 현금흐름표 (연결)" 이 아니라 연결 현금흐름표를 골라야 한다.
    assert "eleId=77" not in links["cashflow"]
    assert links["fallback"] is False
    assert links["main"] == "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=" + RCEPT


def test_fetch_statement_links_falls_back_to_main_on_error(monkeypatch):
    fs._STATEMENT_LINKS_CACHE.clear()

    def broken(url, timeout):
        raise OSError("네트워크 없음")

    monkeypatch.setattr(fs, "_fetch_dart_html", broken)
    links = fs.fetch_statement_links(RCEPT)

    main_url = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=" + RCEPT
    assert links["income"] == main_url
    assert links["balance"] == main_url
    assert links["cashflow"] == main_url
    assert links["fallback"] is True
    # 실패해도 예외를 올리지 않는다(분석은 계속된다).
    assert fs.statement_link_label(links, "income") == "손익계산서"


def test_fetch_statement_links_uses_section_then_main(monkeypatch):
    # 연결재무제표 목차만 있고 개별 재무제표가 없는 보고서
    html = """
var node2 = {};
node2['text'] = "2. 연결재무제표";
node2['rcpNo'] = "20260310002820";
node2['dcmNo'] = "11104488";
node2['eleId'] = "19";
node2['offset'] = "376820";
node2['length'] = "258279";
node2['dtd'] = "dart4.xsd";
"""
    _use_sample(monkeypatch, html)
    links = fs.fetch_statement_links(RCEPT)
    assert links["labels"]["income"] == "연결재무제표"
    assert "eleId=19" in links["balance"]
    assert links["fallback"] is False   # main.do 까지 물러나지는 않았다

    # 목차를 아예 못 읽으면 main.do
    _use_sample(monkeypatch, "<html></html>")
    links = fs.fetch_statement_links(RCEPT)
    assert links["income"].startswith("https://dart.fss.or.kr/dsaf001/main.do?rcpNo=")
    assert links["fallback"] is True


def test_fetch_statement_links_caches_per_rcept(monkeypatch):
    fs._STATEMENT_LINKS_CACHE.clear()
    calls = {"count": 0}

    def counting(url, timeout):
        calls["count"] += 1
        return SAMPLE_MAIN_HTML

    monkeypatch.setattr(fs, "_fetch_dart_html", counting)
    first = fs.fetch_statement_links(RCEPT)
    second = fs.fetch_statement_links(RCEPT)
    assert calls["count"] == 1                       # 두 번째는 캐시
    assert first["balance"] == second["balance"]
    second["labels"]["income"] = "바뀜"
    assert fs.fetch_statement_links(RCEPT)["labels"]["income"] == "연결포괄손익계산서"  # 캐시는 안전


def test_fetch_statement_links_without_rcept_no():
    links = fs.fetch_statement_links("")
    assert links["main"] == ""
    assert links["balance"] == ""
    assert fs.statement_link_label(links, "balance") == "재무상태표"


def test_collect_company_attaches_statement_links(monkeypatch):
    def fake_statement(api_key, corp_code, year, fs_div_pref=("CFS", "OFS")):
        return "CFS", "연결재무제표", "2026%08d" % year, {"revenue": {"amount": 100.0}}

    def fake_links(rcept_no, **kwargs):
        return {"income": "viewer://income/" + rcept_no,
                "balance": "viewer://balance/" + rcept_no,
                "cashflow": "viewer://cashflow/" + rcept_no,
                "main": "main://" + rcept_no,
                "labels": {"income": "연결손익계산서", "balance": "연결재무상태표",
                           "cashflow": "연결현금흐름표"},
                "fallback": False}

    monkeypatch.setattr(fs, "fetch_statement", fake_statement)
    monkeypatch.setattr(fs, "fetch_extra_accounts", lambda *a, **k: {})
    monkeypatch.setattr(fs, "fetch_statement_links", fake_links)

    corp = {"corp_name": "테스트", "corp_code": "00000001", "stock_code": "000001"}
    company = fs.collect_company("KEY", corp, [2024, 2025], verbose=False)

    # 연도별·회사별 두 곳에서 같은 링크를 꺼낼 수 있다.
    assert set(company["statement_links"]) == {2024, 2025}
    assert company["statement_links"][2025]["income"] == "viewer://income/202600002025"
    assert company["years"][2024]["statement_links"]["balance"] == "viewer://balance/202600002024"


# ---------------------------------------------------------------------------
# 재무제표 직행 링크 시드 (statement_links_seed.json) — 해외 Streamlit Cloud 대비
# ---------------------------------------------------------------------------
# 시드에 있는 접수번호는 네트워크 없이(0ms) viewer.do 를 돌려주고,
# 없는 접수번호만 main.do 를 실시간 조회해야 한다.
SEED_RCEPT = "20240312000736"          # 시드에 들어 있는 예시 접수번호(삼성전자 2023)
SEED_LINKS = {
    "income": "https://dart.fss.or.kr/report/viewer.do?rcpNo=%s&dcmNo=9702846&eleId=21" % SEED_RCEPT,
    "balance": "https://dart.fss.or.kr/report/viewer.do?rcpNo=%s&dcmNo=9702846&eleId=20" % SEED_RCEPT,
    "cashflow": "https://dart.fss.or.kr/report/viewer.do?rcpNo=%s&dcmNo=9702846&eleId=22" % SEED_RCEPT,
    "main": "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=%s" % SEED_RCEPT,
    "labels": {"income": "연결손익계산서", "balance": "연결재무상태표", "cashflow": "연결현금흐름표"},
    "fallback": False,
}


def _seed(monkeypatch, links=None):
    monkeypatch.setattr(fs, "_STATEMENT_LINKS_SEED", links if links is not None else {SEED_RCEPT: SEED_LINKS})
    fs._STATEMENT_LINKS_CACHE.clear()


def test_statement_links_seed_hit_needs_no_network(monkeypatch):
    _seed(monkeypatch)
    monkeypatch.setattr(fs, "_fetch_dart_html",
                        lambda *a, **k: pytest.fail("시드에 있으면 main.do 를 조회하지 않아야 한다"))
    links = fs.fetch_statement_links(SEED_RCEPT)
    assert links["balance"] == SEED_LINKS["balance"]
    assert links["income"] == SEED_LINKS["income"]
    assert links["cashflow"] == SEED_LINKS["cashflow"]
    assert links["labels"]["balance"] == "연결재무상태표"
    assert links["fallback"] is False
    assert links["source"] == "seed"
    assert "/report/viewer.do?" in links["balance"]      # 표지(main.do)가 아니라 직행 링크


def test_statement_links_seed_hit_is_cached_and_copy_is_safe(monkeypatch):
    _seed(monkeypatch)
    monkeypatch.setattr(fs, "_fetch_dart_html", lambda *a, **k: SAMPLE_MAIN_HTML)
    first = fs.fetch_statement_links(SEED_RCEPT)
    first["labels"]["income"] = "바뀜"                   # 반환값을 고쳐도
    assert fs._STATEMENT_LINKS_SEED[SEED_RCEPT]["labels"]["income"] == "연결손익계산서"  # 시드는 그대로
    second = fs.fetch_statement_links(SEED_RCEPT)
    assert second["labels"]["income"] == "연결손익계산서"
    assert second["source"] == "seed"


def test_statement_links_seed_miss_falls_back_to_live_fetch(monkeypatch):
    _seed(monkeypatch)
    calls = {"count": 0}

    def counting(url, timeout):
        calls["count"] += 1
        return SAMPLE_MAIN_HTML

    monkeypatch.setattr(fs, "_fetch_dart_html", counting)
    links = fs.fetch_statement_links(RCEPT)              # 시드에 없는 접수번호
    assert calls["count"] == 1
    assert links["source"] == "live"
    assert "eleId=20" in links["balance"]


def test_statement_links_seed_entry_without_viewer_links_is_ignored(monkeypatch):
    # main.do 만 들어 있는 시드 항목은 시드로 치지 않고 실시간 조회를 시도해야 한다.
    main_only = dict(SEED_LINKS)
    main_only.update({"income": SEED_LINKS["main"], "balance": SEED_LINKS["main"],
                      "cashflow": SEED_LINKS["main"], "labels": {}})
    _seed(monkeypatch, {SEED_RCEPT: main_only})
    calls = {"count": 0}

    def counting(url, timeout):
        calls["count"] += 1
        return SAMPLE_MAIN_HTML

    monkeypatch.setattr(fs, "_fetch_dart_html", counting)
    links = fs.fetch_statement_links(SEED_RCEPT)
    assert calls["count"] == 1
    assert links["source"] == "live"
    assert "eleId=20" in links["balance"]


def test_statement_links_seed_file_is_read(monkeypatch, tmp_path):
    import json
    path = tmp_path / "statement_links_seed.json"
    path.write_text(json.dumps({"links": {SEED_RCEPT: SEED_LINKS}}, ensure_ascii=False),
                    encoding="utf-8")
    monkeypatch.setattr(fs, "STATEMENT_LINKS_SEED_FILE", str(path))
    monkeypatch.setattr(fs, "_STATEMENT_LINKS_SEED", None)     # 다음 조회 때 파일을 읽게
    assert fs.lookup_statement_links_seed(SEED_RCEPT)["balance"] == SEED_LINKS["balance"]
    assert fs.lookup_statement_links_seed("00000000000000") is None


def test_statement_links_seed_file_missing_or_broken_is_empty(monkeypatch, tmp_path):
    broken = tmp_path / "broken.json"
    broken.write_text("{ 망가진 JSON", encoding="utf-8")
    for path in (str(tmp_path / "none.json"), str(broken), ""):
        monkeypatch.setattr(fs, "STATEMENT_LINKS_SEED_FILE", path)
        monkeypatch.setattr(fs, "_STATEMENT_LINKS_SEED", None)
        assert fs.statement_links_seed() == {}
        assert fs.lookup_statement_links_seed(SEED_RCEPT) is None


def test_statement_links_seed_file_shipped(monkeypatch):
    """저장소에 동봉되는 시드: 주요 상장사 3년치가 viewer.do 직행 링크로 들어 있고 API 키가 없다."""
    import json
    import os
    monkeypatch.setattr(fs, "_STATEMENT_LINKS_SEED", None)
    path = fs.STATEMENT_LINKS_SEED_FILE
    assert os.path.exists(path), "statement_links_seed.json 이 저장소에 있어야 한다"
    data = json.load(open(path, encoding="utf-8"))
    assert len(data["companies"]) >= 30
    assert len(data["links"]) >= 90                        # 30개사 × 3년
    by_name = {c["corp_name"]: c for c in data["companies"]}
    for name in ("삼성전자", "SK하이닉스"):
        rcepts = by_name[name]["years"]
        assert set(rcepts) == {"2023", "2024", "2025"}
        assert all(rcepts[year] for year in rcepts)
        links = fs.lookup_statement_links_seed(rcepts["2023"])
        assert links is not None and links["source"] == "seed"
        for kind in ("income", "balance", "cashflow"):
            assert "/report/viewer.do?" in links[kind]
            assert "rcpNo=" + rcepts["2023"] in links[kind]
    text = open(path, encoding="utf-8").read()
    assert "crtfc" not in text                        # 인증키 파라미터 이름조차 없어야 한다
    assert not re.search(r"[0-9a-f]{40}", text)       # OpenDART 키(40자리) 같은 비밀값 없음


# ---------------------------------------------------------------------------
# DART 웹 요청 헤더·재시도 — 해외 리전에서 표지로 튕기는 문제 대비
# ---------------------------------------------------------------------------

def test_fetch_dart_html_sends_browser_headers_and_retries_on_timeout(monkeypatch):
    import socket
    requests = []

    class FakeResponse:
        def read(self):
            return SAMPLE_MAIN_HTML.encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(request, timeout=None):
        requests.append((request, timeout))
        if len(requests) == 1:
            raise socket.timeout("timed out")          # 첫 시도는 타임아웃
        return FakeResponse()

    monkeypatch.setattr(fs.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(fs.time, "sleep", lambda seconds: None)

    html = fs._fetch_dart_html("https://dart.fss.or.kr/dsaf001/main.do?rcpNo=" + RCEPT, 7)
    assert "연결 재무상태표" in html
    assert len(requests) == 2                          # 타임아웃 → 1회 재시도
    assert requests[0][1] == 7                         # timeout 은 호출한 쪽 값을 쓴다
    headers = {key.lower(): value for key, value in requests[-1][0].header_items()}
    assert headers["user-agent"].startswith("Mozilla/5.0")
    assert headers["user-agent"].endswith("Safari/537.36")
    assert headers["accept"].startswith("text/html")
    assert headers["accept-language"] == "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7"
    assert headers["referer"] == "https://dart.fss.or.kr/"

    # 헤더로도 두 번 다 실패하면 예외로 알리고, 그 실패는 main.do fallback 으로 처리된다.
    def always_timeout(request, timeout=None):
        raise socket.timeout("timed out")

    monkeypatch.setattr(fs.urllib.request, "urlopen", always_timeout)
    with pytest.raises(OSError):
        fs._fetch_dart_html("https://dart.fss.or.kr/dsaf001/main.do?rcpNo=" + RCEPT, 1)


# ---------------------------------------------------------------------------
# 기업 목록 — 저장소 시드 우선 + 다운로드 실패 시 물러나기 (네트워크 없이 검증)
# ---------------------------------------------------------------------------

SAMPLE_CORP_SEED = {
    "saved_at": "2026-11-26T09:00:00",
    "companies": [
        {"corp_name": "삼성전자", "corp_code": "00126380", "stock_code": "005930"},
        {"corp_name": "SK하이닉스", "corp_code": "00164779", "stock_code": "000660"},
    ],
}


def _write_json(path, data):
    import json
    with open(path, "w", encoding="utf-8") as fp:
        json.dump(data, fp, ensure_ascii=False)
    return str(path)


def test_bundled_seed_file_is_ready():
    """저장소에 실제로 커밋되는 시드 파일이 있고, 키 같은 비밀값이 없는지."""
    import json
    import os
    path = fs.CORP_SEED_FILE
    assert os.path.exists(path), "corp_codes_seed.json 이 저장소에 있어야 한다"
    data = json.load(open(path, encoding="utf-8"))
    assert len(data["companies"]) > 3000          # 상장사 4천여 개
    row = data["companies"][0]
    assert set(row) == {"corp_name", "corp_code", "stock_code"}
    assert len(row["corp_code"]) == 8 and row["corp_code"].isdigit()
    text = open(path, encoding="utf-8").read()
    assert "crtfc_key" not in text and "crtfc" not in text


def test_sync_corp_codes_uses_seed_without_download(monkeypatch, tmp_path):
    seed = _write_json(tmp_path / "seed.json", SAMPLE_CORP_SEED)
    monkeypatch.setattr(fs, "CORP_SEED_FILE", seed)
    monkeypatch.setattr(fs, "CORP_CACHE_FILE", str(tmp_path / "cache.json"))
    monkeypatch.setattr(fs, "_download_corp_codes",
                        lambda *a, **k: pytest.fail("시드가 있으면 다운로드하지 않아야 한다"))
    companies = fs.sync_corp_codes("x" * 40, verbose=False)
    assert [c["corp_name"] for c in companies] == ["삼성전자", "SK하이닉스"]


def test_sync_corp_codes_prefers_fresh_cache(monkeypatch, tmp_path):
    from datetime import datetime
    cache = dict(SAMPLE_CORP_SEED)
    cache["companies"] = [{"corp_name": "캐시회사", "corp_code": "00000001", "stock_code": "000001"}]
    cache["saved_at"] = datetime.now().isoformat(timespec="seconds")
    monkeypatch.setattr(fs, "CORP_CACHE_FILE", _write_json(tmp_path / "cache.json", cache))
    monkeypatch.setattr(fs, "CORP_SEED_FILE", _write_json(tmp_path / "seed.json", SAMPLE_CORP_SEED))
    monkeypatch.setattr(fs, "_download_corp_codes",
                        lambda *a, **k: pytest.fail("신선한 캐시가 있으면 다운로드하지 않는다"))
    assert fs.sync_corp_codes("x" * 40, verbose=False)[0]["corp_name"] == "캐시회사"


def test_sync_corp_codes_falls_back_to_seed_on_network_error(monkeypatch, tmp_path):
    import urllib.error
    seed = _write_json(tmp_path / "seed.json", SAMPLE_CORP_SEED)
    monkeypatch.setattr(fs, "CORP_SEED_FILE", seed)
    monkeypatch.setattr(fs, "CORP_CACHE_FILE", str(tmp_path / "cache.json"))

    def boom(*args, **kwargs):
        raise urllib.error.URLError("timed out")

    monkeypatch.setattr(fs, "_download_corp_codes", boom)
    companies = fs.sync_corp_codes("x" * 40, refresh=True, verbose=False)   # 강제 새로 받기도 실패 → 시드
    assert len(companies) == 2


def test_sync_corp_codes_raises_without_any_source(monkeypatch, tmp_path):
    import urllib.error
    monkeypatch.setattr(fs, "CORP_SEED_FILE", str(tmp_path / "none.json"))
    monkeypatch.setattr(fs, "CORP_CACHE_FILE", str(tmp_path / "none-cache.json"))

    def boom(*args, **kwargs):
        raise urllib.error.URLError("timed out")

    monkeypatch.setattr(fs, "_download_corp_codes", boom)
    with pytest.raises(fs.DartError):
        fs.sync_corp_codes("x" * 40, verbose=False)


def test_seed_corp_cache_copies_only_when_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(fs, "CORP_SEED_FILE", _write_json(tmp_path / "seed.json", SAMPLE_CORP_SEED))
    target = tmp_path / "cache.json"
    assert fs.seed_corp_cache(str(target)) is True
    assert target.exists()
    assert fs.seed_corp_cache(str(target)) is False          # 이미 있으면 그대로


# ---------------------------------------------------------------------------
# 네트워크 재시도 — 해외 리전에서 한 번 끊긴 호출이 분석 전체를 멈추지 않게
# ---------------------------------------------------------------------------

def test_call_json_retries_once_on_network_error(monkeypatch):
    import json
    import urllib.error
    calls = {"count": 0}

    def flaky(url, params, timeout=fs.HTTP_TIMEOUT):
        calls["count"] += 1
        if calls["count"] == 1:
            raise urllib.error.URLError("timed out")
        return json.dumps({"status": "000", "list": [{"account_nm": "매출액"}]}).encode("utf-8")

    monkeypatch.setattr(fs, "http_get", flaky)
    monkeypatch.setattr(fs.time, "sleep", lambda seconds: None)
    result = fs.call_json("fnlttSinglAcnt.json", {"crtfc_key": "x"})
    assert result["status"] == "000"
    assert calls["count"] == 2           # 첫 시도 실패 → 한 번 더


def test_call_json_raises_after_retries(monkeypatch):
    import urllib.error

    def boom(url, params, timeout=fs.HTTP_TIMEOUT):
        raise urllib.error.URLError("timed out")

    monkeypatch.setattr(fs, "http_get", boom)
    monkeypatch.setattr(fs.time, "sleep", lambda seconds: None)
    with pytest.raises(urllib.error.URLError):
        fs.call_json("fnlttSinglAcnt.json", {"crtfc_key": "x"})


# ---------------------------------------------------------------------------
# 12. 계정명 정규화·별칭 — 띄어쓰기('자본 총계')와 '연결당기순이익'
# ---------------------------------------------------------------------------
# 품질 감사에서 실제로 놓쳤던 두 가지:
#   · 금호석유화학 2023 전체재무제표는 '자본 총계'(공백)라 정확 일치가 실패했다.
#   · 현대자동차 2022 손익계산서 계정명은 '연결당기순이익'이라 후보에 없었다.

def test_normalize_name_ignores_every_kind_of_space():
    assert fs.normalize_name("자본 총계") == "자본총계"
    assert fs.normalize_name("자본\u00a0총계") == "자본총계"      # 줄바꿈 없는 공백(NBSP)
    assert fs.normalize_name(" 영업활동  현금흐름\n") == "영업활동현금흐름"
    assert fs.normalize_name(None) == ""


def _statement_rows():
    """주요계정 응답 흉내 — 공백 계정명·연결당기순이익·연결/별도 혼재."""
    return [
        {"fs_div": "CFS", "sj_div": "IS", "account_nm": "매출액",
         "thstrm_amount": "1,000", "frmtrm_amount": "900"},
        {"fs_div": "CFS", "sj_div": "IS", "account_nm": "연결당기순이익",
         "thstrm_amount": "120", "frmtrm_amount": "80"},
        {"fs_div": "CFS", "sj_div": "BS", "account_nm": "자산총계",
         "thstrm_amount": "2,000", "frmtrm_amount": "1,800"},
        {"fs_div": "CFS", "sj_div": "BS", "account_nm": "부채총계",
         "thstrm_amount": "700", "frmtrm_amount": "600"},
        {"fs_div": "CFS", "sj_div": "BS", "account_nm": "자본 총계",       # 공백 표기
         "thstrm_amount": "1,300", "frmtrm_amount": "1,200"},
        {"fs_div": "OFS", "sj_div": "BS", "account_nm": "자본총계",
         "thstrm_amount": "999,999", "frmtrm_amount": "999,999"},          # 별도는 쓰지 않는다
    ]


def test_fetch_statement_matches_names_ignoring_spaces(monkeypatch):
    monkeypatch.setattr(fs, "call_json",
                        lambda endpoint, params, attempts=2: {"status": "000", "list": _statement_rows()})
    fs_div, _fs_nm, _rcept, accounts = fs.fetch_statement("KEY", "00100000", 2023)

    assert fs_div == "CFS"
    # '자본 총계' → 자본총계로 인식 (별도재무제표 999,999 를 집어오면 실패)
    assert accounts["equity"]["amount"] == 1300
    assert accounts["equity"]["account_nm"] == "자본 총계"
    # '연결당기순이익' 별칭
    assert accounts["net_income"]["amount"] == 120
    assert accounts["net_income"]["account_nm"] == "연결당기순이익"


def test_pick_account_prefers_ifrs_tag_over_name():
    # 같은 계정이 표준태그 행과 이름 행 두 개로 올 때는 표준태그를 먼저 쓴다.
    rows = [
        {"sj_div": "IS", "account_id": "", "account_nm": "매출액", "thstrm_amount": "1"},
        {"sj_div": "IS", "account_id": "ifrs-full_Revenue", "account_nm": "수익(매출액)", "thstrm_amount": "2"},
    ]
    spec = {"key": "revenue", "sj_div": ("IS", "CIS"),
            "names": ("매출액", "수익(매출액)"), "ids": ("ifrs-full_Revenue",)}
    found = fs.pick_account(spec, fs.unique_rows_of(rows))
    assert found["thstrm_amount"] == "2"


# ---------------------------------------------------------------------------
# 13. fetch_extra_accounts — 실제 조회된 fs_div(CFS/OFS)를 따라간다
# ---------------------------------------------------------------------------
# 연결재무제표가 없는 회사(이스타코·성지건설 등)는 fs_div='CFS' 고정 탓에
# 현금흐름·이자비용·이익잉여금이 전 연도 N/A 였다.

def _extra_rows(with_fs_div=False):
    rows = [
        {"sj_div": "CF", "account_nm": "영업활동 현금흐름", "thstrm_amount": "300"},
        {"sj_div": "IS", "account_nm": "이자비용", "thstrm_amount": "50"},
        {"sj_div": "BS", "account_nm": "이익잉여금", "thstrm_amount": "400"},
    ]
    if with_fs_div:
        for row in rows:
            row["fs_div"] = "OFS"
    return rows


def test_fetch_extra_accounts_falls_back_to_ofs_when_cfs_is_empty(monkeypatch):
    requested = []

    def fake_call(endpoint, params, attempts=2):
        requested.append(params["fs_div"])
        if params["fs_div"] == "CFS":
            return {"status": "013", "message": "조회된 데이터가 없습니다."}
        return {"status": "000", "list": _extra_rows()}

    monkeypatch.setattr(fs, "call_json", fake_call)
    extras = fs.fetch_extra_accounts("KEY", "00100000", 2023)

    assert requested == ["CFS", "OFS"]              # 연결이 없으면 별도로 물러난다
    assert extras["op_cash_flow"]["amount"] == 300
    assert extras["interest_expense"]["amount"] == 50


def test_fetch_extra_accounts_follows_given_fs_div_first(monkeypatch):
    requested = []

    def fake_call(endpoint, params, attempts=2):
        requested.append(params["fs_div"])
        return {"status": "000", "list": _extra_rows()}

    monkeypatch.setattr(fs, "call_json", fake_call)
    fs.fetch_extra_accounts("KEY", "00100000", 2023, fs_div="OFS")

    assert requested == ["OFS"]                     # 별도로 분석했으면 별도를 먼저


def test_fetch_extra_accounts_keeps_only_the_requested_statement(monkeypatch):
    rows = _extra_rows(with_fs_div=True)
    rows.append({"fs_div": "CFS", "sj_div": "CF", "account_nm": "영업활동현금흐름",
                 "thstrm_amount": "999"})

    def fake_call(endpoint, params, attempts=2):
        return {"status": "000", "list": rows}

    monkeypatch.setattr(fs, "call_json", fake_call)
    extras = fs.fetch_extra_accounts("KEY", "00100000", 2023, fs_div="OFS")

    assert extras["op_cash_flow"]["amount"] == 300   # CFS 999 를 섞지 않는다


def test_fetch_extra_accounts_fills_missing_core_accounts(monkeypatch):
    # 한화·금호석유화학 2023: 주요계정 API에 자본총계가 아예 없다.
    rows = _extra_rows() + [
        {"sj_div": "BS", "account_nm": "자본 총계", "thstrm_amount": "1,300", "frmtrm_amount": "1,200"},
        {"sj_div": "IS", "account_nm": "매출액", "thstrm_amount": "9,999"},
    ]

    def fake_call(endpoint, params, attempts=2):
        return {"status": "000", "list": rows}

    monkeypatch.setattr(fs, "call_json", fake_call)
    known = {"revenue": {"amount": 1000.0}}          # 매출액은 이미 있다
    extras = fs.fetch_extra_accounts("KEY", "00100000", 2023, fs_div="CFS", known_accounts=known)

    assert extras["equity"]["amount"] == 1300        # 빠진 핵심 계정은 채운다
    assert extras["equity"]["prev_amount"] == 1200
    assert "revenue" not in extras                   # 있는 값은 덮어쓰지 않는다


def test_fetch_extra_accounts_without_known_accounts_does_not_return_core(monkeypatch):
    rows = _extra_rows() + [{"sj_div": "BS", "account_nm": "자본총계", "thstrm_amount": "1,300"}]
    monkeypatch.setattr(fs, "call_json",
                        lambda endpoint, params, attempts=2: {"status": "000", "list": rows})
    extras = fs.fetch_extra_accounts("KEY", "00100000", 2023)
    assert "equity" not in extras                    # 핵심 계정 보완은 요청했을 때만
    assert extras["interest_expense"]["amount"] == 50


def test_collect_company_passes_used_fs_div_and_known_accounts(monkeypatch):
    captured = {}

    def fake_statement(api_key, corp_code, year, fs_div_pref=("CFS", "OFS")):
        return "OFS", "재무제표", "2026%08d" % year, {"revenue": {"amount": 100.0}}

    def fake_extras(api_key, corp_code, year, fs_div=None, known_accounts=None):
        captured["fs_div"] = fs_div
        captured["known"] = dict(known_accounts or {})
        return {}

    monkeypatch.setattr(fs, "fetch_statement", fake_statement)
    monkeypatch.setattr(fs, "fetch_extra_accounts", fake_extras)
    monkeypatch.setattr(fs, "fetch_statement_links", lambda rcept_no, **kwargs: {})

    corp = {"corp_name": "테스트", "corp_code": "00000001", "stock_code": "000001"}
    fs.collect_company("KEY", corp, [2024, 2025], verbose=False)

    assert captured["fs_div"] == "OFS"                # 주요계정이 별도였으면 별도로 조회
    assert captured["known"] == {"revenue": {"amount": 100.0}}


# ---------------------------------------------------------------------------
# 14. 자본잠식(자본총계 ≤ 0) — 비율 왜곡 차단 + '자본잠식' 표시
# ---------------------------------------------------------------------------
# 태영건설 2023: 자본 -4,402억, 부채비율 -1,154%, ROE 도 부호가 뒤집혀 나왔다.

IMPAIRED = {
    2022: {"revenue": 1000, "op_income": 50, "net_income": 30,
           "assets": 1300, "current_assets": 600, "liabilities": 900,
           "current_liab": 500, "equity": 400},
    2023: {"revenue": 900, "op_income": -100, "net_income": -150,
           "assets": 1200, "current_assets": 500, "liabilities": 1400,
           "current_liab": 800, "equity": -200,            # 자본잠식
           "interest_expense": 60, "op_cash_flow": -120, "retained_earnings": -300},
}


def test_compute_ratios_skips_ratios_when_equity_is_negative():
    years_data, years = make_years(IMPAIRED)
    fs.compute_ratios(years_data, years)

    latest = years_data[2023]
    assert latest["capital_impaired"] is True
    assert latest["ratios"]["debt_ratio"] is None       # -700% 같은 왜곡값을 만들지 않는다
    assert latest["ratios"]["roe"] is None              # 적자인데 +75% 가 나오던 자리
    assert latest["dupont"]["equity_multiplier"] is None
    assert latest["dupont"]["roe"] is None
    # 자본과 무관한 비율은 그대로 계산한다.
    assert latest["ratios"]["op_margin"] == pytest.approx(-100 / 900 * 100)
    assert latest["ratios"]["current_ratio"] == pytest.approx(62.5)
    # 전년(자본 양수)은 정상 계산
    assert years_data[2022]["capital_impaired"] is False
    assert years_data[2022]["ratios"]["debt_ratio"] == pytest.approx(225.0)


def test_capital_impaired_info_text_and_amount():
    years_data, years = make_years(IMPAIRED)
    company = {"name": "테스트", "years": years_data}

    info = fs.capital_impaired_info(company, 2023)
    assert info["impaired"] is True
    assert info["amount"] == -200
    assert info["text"].startswith("자본잠식")

    ok = fs.capital_impaired_info(company, 2022)
    assert ok["impaired"] is False and ok["text"] == ""
    assert fs.capital_impaired_info({"years": {}}, 2023)["impaired"] is False


def test_evaluate_risk_marks_capital_impairment_as_danger_not_na():
    years_data, years = make_years(IMPAIRED)
    fs.compute_ratios(years_data, years)
    company = {"name": "테스트", "years": years_data}

    risk = fs.evaluate_risk(company, years)
    item = [i for i in risk["items"] if i["key"] == "debt_ratio"][0]

    assert item["value_text"] == "자본잠식"
    assert item["state"] == "danger"                    # N/A 로 빠져 점수를 올려 주지 않는다
    assert "자본잠식" in item["note"]
    assert risk["capital_impaired"] is True
    assert "자본잠식" in risk["capital_impaired_text"]
    assert "자본잠식" in risk["comment"]
    assert item["max"] == 20                            # 분모에 포함(자본잠식은 N/A 가 아니다)
    assert risk["available_count"] == 5                 # 5개 항목 모두 평가됨
    assert risk["score"] < 60 and risk["grade"] == "danger"


def test_evaluate_risk_healthy_company_is_not_marked_impaired():
    years_data, years = make_years(NORMAL)
    fs.compute_ratios(years_data, years)
    company = {"name": "정상", "years": years_data}

    risk = fs.evaluate_risk(company, years)
    assert risk["capital_impaired"] is False
    assert risk["capital_impaired_text"] == ""
    assert [i for i in risk["items"] if i["key"] == "debt_ratio"][0]["value_text"] != "자본잠식"


# ---------------------------------------------------------------------------
# 15. 공시 위험 키워드 — 채권은행·관리절차(워크아웃)·자본잠식
# ---------------------------------------------------------------------------
# 태영건설 2023-12-28 실제 공시명 '채권은행등의관리절차개시신청'을
# 예전 키워드 목록은 하나도 잡지 못했다(재현율 0).

def _disclosure(monkeypatch, titles):
    rows = [{"corp_code": "00100000", "corp_name": "테스트",
             "report_nm": title, "rcept_no": "20231228%08d" % index, "rcept_dt": "20231228"}
            for index, title in enumerate(titles)]

    def fake_call(endpoint, params, attempts=2):
        return {"status": "000", "list": rows, "total_count": len(rows), "total_page": 1}

    monkeypatch.setattr(fs, "call_json", fake_call)
    return fs.fetch_disclosure_risks("KEY", "00100000")


def test_disclosure_catches_creditor_bank_workout_disclosure(monkeypatch):
    result = _disclosure(monkeypatch, ["채권은행등의관리절차개시신청"])

    assert result["status"] == "ok"
    match = result["matches"][0]
    assert match["severity"] == "severe"
    assert match["labels"] == ["채권은행 관리절차(워크아웃) 신청"]   # 두 키워드가 걸려도 라벨은 하나


def test_disclosure_catches_event_of_default_and_capital_impairment(monkeypatch):
    result = _disclosure(monkeypatch, [
        "상장채권기한의이익상실",
        "반기검토의견부적정,의견거절또는완전자본잠식사실발생",
    ])
    labels = [label for match in result["matches"] for label in match["labels"]]

    assert "기한의이익 상실(채무불이행)" in labels
    assert "자본잠식" in labels
    assert all(match["severity"] == "severe" for match in result["matches"])


def test_disclosure_keyword_matching_ignores_spaces(monkeypatch):
    # 공시명에 띄어쓰기가 들어와도(기재정정 포함) 같은 사건으로 잡는다.
    result = _disclosure(monkeypatch, ["[기재정정]채권은행 등의 관리절차 개시 신청"])
    assert result["status"] == "ok"
    assert result["matches"][0]["labels"] == ["채권은행 관리절차(워크아웃) 신청"]


def test_disclosure_normal_titles_are_not_flagged(monkeypatch):
    result = _disclosure(monkeypatch, [
        "타인에대한채무보증결정",
        "단일판매ㆍ공급계약체결",
        "기업지배구조보고서공시",
    ])
    assert result["status"] == "clear"
    assert result["matches"] == []


# 자본잠식이지만 다른 지표가 멀쩡해 점수가 60점을 넘는 경우 — 등급은 위험으로 고정
# (자본잠식 자체가 관리종목·상장폐지 사유라 '주의'로 보이면 안 된다)
MILDLY_IMPAIRED = {
    2022: {"revenue": 1400, "op_income": 110, "net_income": 90,
           "assets": 1000, "current_assets": 600, "liabilities": 940,
           "current_liab": 300, "equity": 60,
           "interest_expense": 20, "op_cash_flow": 110, "retained_earnings": 200},
    2023: {"revenue": 1500, "op_income": 120, "net_income": 100,
           "assets": 1000, "current_assets": 600, "liabilities": 1020,
           "current_liab": 300, "equity": -20,          # 소폭 자본잠식
           "interest_expense": 20, "op_cash_flow": 120, "retained_earnings": 300},
}


def test_capital_impairment_forces_danger_grade_even_with_high_score():
    years_data, years = make_years(MILDLY_IMPAIRED)
    fs.compute_ratios(years_data, years)
    company = {"name": "테스트", "years": years_data}

    risk = fs.evaluate_risk(company, years)
    assert risk["score"] >= 60          # 나머지 항목은 안전/주의
    assert risk["grade"] == "danger"    # 그래도 자본잠식이면 위험
    assert "자본잠식" in risk["comment"]
    assert "점수와 무관하게 위험 등급" in risk["comment"]


# ---------------------------------------------------------------------------
# 4-4. 동종업계(피어) 자동 비교
#   · 업종 매핑 시드 → 피어 선정(동일 코드 → 3자리 → 2자리, 지주회사 분리)
#   · 지표 비교(평균·중앙값·순위) 및 API 실패 시 폴백
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolate_industry_seed(monkeypatch):
    """업종 시드·캐시를 테스트마다 비운다. (실제 시드 파일은 별도 테스트가 직접 읽는다)"""
    monkeypatch.setattr(fs, "_INDUSTRY_SEED", {})
    fs._INDUSTRY_PROFILE_CACHE.clear()
    fs._PEER_METRICS_CACHE.clear()
    yield
    fs._INDUSTRY_PROFILE_CACHE.clear()
    fs._PEER_METRICS_CACHE.clear()


def industry_record(corp_code, name, induty_code, order, stock_code="", sector=""):
    """시드 한 줄과 같은 모양의 dict."""
    return {"corp_code": corp_code, "corp_name": name, "stock_code": stock_code,
            "induty_code": induty_code, "industry_name": "", "sector": sector, "order": order}


def peer_company(name, corp_code, stock_code, accounts):
    """한 해짜리 기업 dict — 비율·CCC까지 계산해 둔다."""
    years_data, years = make_years({2025: accounts})
    fs.compute_ratios(years_data, years)
    fs.compute_working_capital(years_data, years)
    return {"name": name, "corp_code": corp_code, "stock_code": stock_code, "years": years_data}


def test_display_name_strips_corporate_suffix():
    assert fs.display_name("삼성전자(주)") == "삼성전자"
    assert fs.display_name("주식회사 크래프톤") == "크래프톤"
    assert fs.display_name("(주)KB금융지주") == "KB금융지주"
    assert fs.display_name("") == ""


def test_industry_seed_file_shipped():
    """저장소에 동봉한 업종 시드 — 80개사 이상, 필수 필드, API 키 없음."""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "industry_map_seed.json")
    seed = fs.read_industry_seed(path)
    assert len(seed) >= 80

    samsung = seed["00126380"]
    assert samsung["stock_code"] == "005930"
    assert samsung["induty_code"] == "264"

    # 업종 비교 대상(118개사)은 모두 DART 원문 직행 링크 시드에도 있어야 한다
    # (피어를 비교할 때마다 원문 링크가 표지로 물러나지 않게).
    with open(os.path.join(os.path.dirname(path), "statement_links_seed.json"), encoding="utf-8") as fp:
        links_seed = json.load(fp)
    linked_codes = {row["corp_code"] for row in links_seed["companies"]}
    missing = [record["corp_name"] for record in seed.values()
               if record["corp_code"] not in linked_codes]
    assert missing == []

    for record in seed.values():
        assert record["corp_code"] and record["stock_code"] and record["induty_code"]

    with open(path, encoding="utf-8") as fp:
        raw = fp.read()
    assert not re.search(r"[0-9a-f]{40}", raw)          # 40자리 인증키 형태 없음
    assert "crtfc_key" not in raw


def test_statement_links_seed_covers_major_companies():
    """직행 링크 시드 — 100개사 이상·모든 링크가 viewer.do·접수번호 일치·API 키 없음."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(root, "statement_links_seed.json")
    with open(path, encoding="utf-8") as fp:
        raw = fp.read()
    seed = json.loads(raw)

    assert len(seed["companies"]) >= 100
    assert len(seed["links"]) >= 300
    for rcept_no, entry in seed["links"].items():
        for kind in ("income", "balance", "cashflow"):
            assert "/report/viewer.do?" in entry[kind]
            assert "rcpNo=%s" % rcept_no in entry[kind]       # 같은 보고서의 표인지 확인
        assert entry["labels"]["balance"]                        # 목차 이름이 비지 않음
    # 3개년이 모두 연결된 회사가 대부분이어야 한다
    full = [row for row in seed["companies"]
            if all(row["years"].get(str(year)) in seed["links"] for year in (2023, 2024, 2025))]
    assert len(full) >= 100
    assert not re.search(r"[0-9a-f]{40}", raw)                    # 40자리 인증키 형태 없음
    assert "crtfc_key" not in raw


def test_industry_seed_missing_or_broken_is_empty(tmp_path):
    assert fs.read_industry_seed(str(tmp_path / "nope.json")) == {}
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert fs.read_industry_seed(str(broken)) == {}
    wrong = tmp_path / "wrong.json"
    wrong.write_text('{"companies": "nope"}', encoding="utf-8")
    assert fs.read_industry_seed(str(wrong)) == {}


def test_industry_record_finds_by_corp_code_then_stock_code():
    fs._INDUSTRY_SEED = {
        "00126380": industry_record("00126380", "삼성전자(주)", "264", 1, "005930", "반도체·전자"),
        "00164779": industry_record("00164779", "에스케이하이닉스(주)", "2612", 2, "000660"),
    }
    assert fs.industry_record("00126380")["induty_code"] == "264"
    assert fs.industry_record(None, "000660")["induty_code"] == "2612"
    assert fs.industry_record("00000000") is None


def test_peer_match_levels():
    assert fs.peer_match("264", "264") == "same"
    assert fs.peer_match("264", "2641") == "three"      # 앞 3자리가 같다
    assert fs.peer_match("2612", "261") == "three"
    assert fs.peer_match("2612", "262") == "two"   # 3자리가 달라 2자리까지 넓힌다
    assert fs.peer_match("2612", "26") == "two"
    assert fs.peer_match("264", "30121") is None
    assert fs.peer_match("", "264") is None


def test_select_industry_peers_fills_exact_code_first_then_widens():
    records = [
        industry_record("A", "대상", "264", 1),
        industry_record("B", "같은코드", "264", 2),
        industry_record("C", "삼자리", "2641", 3),
        industry_record("D", "이자리", "2612", 4),
        industry_record("E", "다른업종", "30121", 5),
    ]
    selection = fs.select_industry_peers("264", records, exclude_codes=["A"], max_peers=3)
    assert [p["corp_code"] for p in selection["peers"]] == ["B", "C", "D"]
    assert [p["match"] for p in selection["peers"]] == ["same", "three", "two"]
    assert selection["levels"] == {"same": 1, "three": 1, "two": 1}


def test_select_industry_peers_respects_max_three_and_curated_order():
    records = [industry_record("A", "대상", "108", 1)]
    records += [industry_record("P%d" % index, "피어%d" % index, "108", 10 + index)
                for index in range(1, 6)]
    selection = fs.select_industry_peers("108", records, exclude_codes=["A"])
    # 같은 코드 후보가 5개라도 최대 3개사, 시드 순서(대표성)대로
    assert [p["corp_code"] for p in selection["peers"]] == ["P1", "P2", "P3"]


def test_select_industry_peers_excludes_holding_companies_from_normal_peers():
    records = [
        industry_record("A", "대상", "264", 1),
        industry_record("H", "지주회사", "64992", 2),
        industry_record("B", "피어", "264", 3),
    ]
    selection = fs.select_industry_peers("264", records, exclude_codes=["A"])
    assert [p["corp_code"] for p in selection["peers"]] == ["B"]
    assert selection["excluded_holdings"] == 1


def test_select_industry_peers_of_holding_company_compares_holdings_only():
    records = [
        industry_record("KB", "KB금융지주", "64992", 5, sector="금융"),
        industry_record("SH", "신한금융지주", "64992", 6, sector="금융"),
        industry_record("LG", "LG", "64992", 1, sector="지주"),
        industry_record("SEC", "삼성전자", "264", 2),
    ]
    selection = fs.select_industry_peers("64992", records, exclude_codes=["KB"], target_sector="금융")
    # 64992 가 아닌 삼성전자는 빠지고, 같은 업종군(금융) 지주회사가 먼저 온다
    assert [p["corp_code"] for p in selection["peers"]] == ["SH", "LG"]
    assert all(p["induty_code"] == "64992" for p in selection["peers"])


def test_select_industry_peers_without_code_is_empty():
    selection = fs.select_industry_peers("", [industry_record("A", "대상", "264", 1)])
    assert selection["peers"] == []


def test_compare_peers_mean_median_rank_and_missing_values():
    target = {"op_margin": 12.3, "roe": 8.4, "debt_ratio": 25.1,
              "current_ratio": 250.3, "ccc": 92.0}
    peers = [
        {"metrics": {"op_margin": 6.0, "roe": 5.0, "debt_ratio": 40.0,
                     "current_ratio": 180.0, "ccc": 110.0}},
        {"metrics": {"op_margin": 8.1, "roe": 9.0, "debt_ratio": 55.0,
                     "current_ratio": 250.0, "ccc": 80.0}},
        {"metrics": {"op_margin": 10.5, "roe": 12.0, "debt_ratio": 30.0,
                     "current_ratio": 300.0, "ccc": None}},
    ]
    rows = {row["key"]: row for row in fs.compare_peers(target, peers)}

    assert rows["op_margin"]["mean"] == pytest.approx((6.0 + 8.1 + 10.5) / 3)
    assert rows["op_margin"]["median"] == 8.1
    assert rows["op_margin"]["rank"] == 1                 # 피어 중 가장 높다
    assert rows["op_margin"]["total"] == 4

    # 부채비율은 낮을수록 좋다 → 25.1%는 1위
    assert rows["debt_ratio"]["rank"] == 1
    # CCC는 3번째 피어에 값이 없어 평균은 2개사로만 계산하고, 순위 모집단도 3개사
    assert rows["ccc"]["mean"] == pytest.approx(95.0)
    assert rows["ccc"]["rank"] == 2
    assert rows["ccc"]["total"] == 3


def test_compare_peers_without_peer_values_has_no_rank():
    rows = {row["key"]: row for row in fs.compare_peers(
        {"op_margin": 5.0}, [{"metrics": {"op_margin": None}}])}
    assert rows["op_margin"]["mean"] is None
    assert rows["op_margin"]["rank"] is None


def test_make_industry_comment_uses_median_and_rank():
    target = {"op_margin": 13.0, "roe": 10.4, "debt_ratio": 29.9,
              "current_ratio": 232.8, "ccc": 127.0}
    peers = [
        {"metrics": {"op_margin": 37.0, "roe": 33.0, "debt_ratio": 45.0,
                     "current_ratio": 185.8, "ccc": 113.0}},
        {"metrics": {"op_margin": 6.0, "roe": 9.0, "debt_ratio": 140.0,
                     "current_ratio": 130.0, "ccc": None}},
        {"metrics": {"op_margin": 8.1, "roe": 7.5, "debt_ratio": 49.0,
                     "current_ratio": 185.8, "ccc": None}},
    ]
    rows = fs.compare_peers(target, peers)
    comment = fs.make_industry_comment(rows, "삼성전자", 3, 2025)
    assert "2025년 삼성전자" in comment
    assert "중앙값" in comment
    assert "위" in comment

    # 피어 값이 하나도 없으면 계산하지 않고 안내만 한다
    empty = fs.make_industry_comment(fs.compare_peers({}, peers), "삼성전자", 3, 2025)
    assert "계산하지 못했습니다" in empty


def test_make_industry_comment_almost_equal_uses_plain_wording():
    target = {"op_margin": 8.1}
    peers = [{"metrics": {"op_margin": 8.1}}, {"metrics": {"op_margin": 8.1}}]
    comment = fs.make_industry_comment(fs.compare_peers(target, peers), "테스트", 2, 2025)
    assert "거의 같습니다" in comment


PEER_SEED = {
    "00126380": industry_record("00126380", "삼성전자(주)", "264", 1, "005930", "반도체·전자"),
    "00066000": industry_record("00066000", "LG전자(주)", "264", 3, "066570", "반도체·전자"),
    "00164779": industry_record("00164779", "SK하이닉스(주)", "2612", 2, "000660", "반도체·전자"),
    "00000003": industry_record("00000003", "삼성전기(주)", "2622", 4, "009150", "반도체·전자"),
}

SAMSUNG_ACCOUNTS = {"revenue": 300000, "op_income": 39000, "net_income": 31000,
                    "assets": 500000, "current_assets": 200000, "liabilities": 90000,
                    "current_liab": 86000, "equity": 410000,
                    "cost_of_sales": 200000, "receivables": 40000,
                    "inventory": 30000, "payables": 25000}


def peer_statement_factory(accounts_by_code):
    def fake_fetch_statement(api_key, corp_code, year, fs_div_pref=("CFS", "OFS")):
        accounts = accounts_by_code[corp_code]
        return "CFS", "연결재무제표", "2025%08d" % int(corp_code[-3:] or 0), \
            {key: {"amount": value} for key, value in accounts.items()}
    return fake_fetch_statement


def test_build_industry_comparison_ok(monkeypatch):
    fs._INDUSTRY_SEED = PEER_SEED
    target = peer_company("삼성전자", "00126380", "005930", SAMSUNG_ACCOUNTS)
    analysis = {"companies": [target], "years": [2025]}

    monkeypatch.setattr(fs, "fetch_statement", peer_statement_factory({
        "00066000": {"revenue": 100000, "op_income": 8000, "net_income": 6000,
                     "assets": 120000, "current_assets": 40000, "liabilities": 60000,
                     "current_liab": 30000, "equity": 60000,
                     "cost_of_sales": 70000, "receivables": 15000,
                     "inventory": 20000, "payables": 12000},
        "00164779": {"revenue": 80000, "op_income": 20000, "net_income": 18000,
                     "assets": 140000, "current_assets": 70000, "liabilities": 50000,
                     "current_liab": 25000, "equity": 90000,
                     "cost_of_sales": 50000, "receivables": 18000,
                     "inventory": 22000, "payables": 14000},
        "00000003": {"revenue": 50000, "op_income": 4000, "net_income": 3000,
                     "assets": 60000, "current_assets": 25000, "liabilities": 20000,
                     "current_liab": 12000, "equity": 40000,
                     "cost_of_sales": 38000, "receivables": 9000,
                     "inventory": 8000, "payables": 6000},
    }))
    monkeypatch.setattr(fs, "fetch_extra_accounts", lambda *a, **k: {})

    result = fs.build_industry_comparison("key", analysis)
    assert result["status"] == "ok"
    assert result["induty_code"] == "264"
    assert result["code_source"] == "seed"
    assert [peer["name"] for peer in result["peers"]] == ["LG전자", "SK하이닉스", "삼성전기"]
    assert [peer["match"] for peer in result["peers"]] == ["same", "two", "two"]

    rows = {row["key"]: row for row in result["rows"]}
    # 영업이익률: 삼성전자 39000/300000 = 13.0%
    assert rows["op_margin"]["target"] == pytest.approx(13.0)
    assert rows["op_margin"]["mean"] == pytest.approx((8.0 + 25.0 + 8.0) / 3)
    assert "삼성전자" in result["comment"]


def test_build_industry_comparison_without_code(monkeypatch):
    fs._INDUSTRY_SEED = {}
    target = peer_company("새회사", "99999999", "", SAMSUNG_ACCOUNTS)
    monkeypatch.setattr(fs, "industry_profile", lambda *a, **k: None)
    result = fs.build_industry_comparison("key", {"companies": [target], "years": [2025]})
    assert result["status"] == "no_code"


def test_build_industry_comparison_without_peers():
    fs._INDUSTRY_SEED = {"00126380": PEER_SEED["00126380"]}
    target = peer_company("삼성전자", "00126380", "005930", SAMSUNG_ACCOUNTS)
    result = fs.build_industry_comparison("key", {"companies": [target], "years": [2025]})
    assert result["status"] == "no_peers"
    assert result["induty_code"] == "264"


def test_build_industry_comparison_skips_peer_without_financials(monkeypatch):
    fs._INDUSTRY_SEED = PEER_SEED
    target = peer_company("삼성전자", "00126380", "005930", SAMSUNG_ACCOUNTS)
    good = peer_statement_factory({
        "00164779": {"revenue": 80000, "op_income": 20000, "net_income": 18000,
                     "assets": 140000, "current_assets": 70000, "liabilities": 50000,
                     "current_liab": 25000, "equity": 90000},
    })

    def fake_fetch_statement(api_key, corp_code, year, fs_div_pref=("CFS", "OFS")):
        if corp_code in ("00066000", "00000003"):
            raise fs.DartError("조회된 데이터가 없습니다.", status="013")
        return good(api_key, corp_code, year)

    monkeypatch.setattr(fs, "fetch_statement", fake_fetch_statement)
    monkeypatch.setattr(fs, "fetch_extra_accounts", lambda *a, **k: {})

    result = fs.build_industry_comparison("key", {"companies": [target], "years": [2025]})
    assert result["status"] == "ok"
    assert len(result["peers"]) == 1
    assert sorted(result["skipped"]) == ["LG전자", "삼성전기"]


def test_build_industry_comparison_survives_all_peer_failures(monkeypatch):
    fs._INDUSTRY_SEED = PEER_SEED
    target = peer_company("삼성전자", "00126380", "005930", SAMSUNG_ACCOUNTS)

    def boom(*args, **kwargs):
        raise urllib.error.URLError("network down")

    monkeypatch.setattr(fs, "fetch_statement", boom)
    monkeypatch.setattr(fs, "fetch_extra_accounts", boom)
    result = fs.build_industry_comparison("key", {"companies": [target], "years": [2025]})
    assert result["status"] == "no_data"


def test_build_industry_comparison_never_raises(monkeypatch):
    fs._INDUSTRY_SEED = PEER_SEED
    target = peer_company("삼성전자", "00126380", "005930", SAMSUNG_ACCOUNTS)

    def boom(*args, **kwargs):
        raise ValueError("예상 못 한 오류")

    monkeypatch.setattr(fs, "select_industry_peers", boom)
    result = fs.build_industry_comparison("key", {"companies": [target], "years": [2025]})
    assert result["status"] == "error"
    assert "예상 못 한 오류" in result["reason"]


# ---------------------------------------------------------------------------
# 4-5. 피어 병렬 조회 — ThreadPoolExecutor(max_workers=3)
# ---------------------------------------------------------------------------

# 3개 피어의 실제 모양과 가까운 계정(2025년 한 해). 영업이익률은 8.0% / 25.0% / 8.0%.
PEER_ACCOUNTS = {
    "00066000": {"revenue": 100000, "op_income": 8000, "net_income": 6000,
                 "assets": 120000, "current_assets": 40000, "liabilities": 60000,
                 "current_liab": 30000, "equity": 60000,
                 "cost_of_sales": 70000, "receivables": 15000,
                 "inventory": 20000, "payables": 12000},
    "00164779": {"revenue": 80000, "op_income": 20000, "net_income": 18000,
                 "assets": 140000, "current_assets": 70000, "liabilities": 50000,
                 "current_liab": 25000, "equity": 90000,
                 "cost_of_sales": 50000, "receivables": 18000,
                 "inventory": 22000, "payables": 14000},
    "00000003": {"revenue": 50000, "op_income": 4000, "net_income": 3000,
                 "assets": 60000, "current_assets": 25000, "liabilities": 20000,
                 "current_liab": 12000, "equity": 40000,
                 "cost_of_sales": 38000, "receivables": 9000,
                 "inventory": 8000, "payables": 6000},
}

# 피어 선정 순서(LG전자 → SK하이닉스 → 삼성전기)와 같은 순서로 기대한다.
PEER_ORDER = ["LG전자", "SK하이닉스", "삼성전기"]


def industry_analysis_for_parallel():
    fs._INDUSTRY_SEED = PEER_SEED
    target = peer_company("삼성전자", "00126380", "005930", SAMSUNG_ACCOUNTS)
    return {"companies": [target], "years": [2025]}


def instrumented_fetcher(delays=None, boom_codes=(), state=None):
    """fetch_statement 대역 — 동시 실행 수를 재고, 회사별로 응답 시간을 다르게 만든다."""
    delays = delays or {}
    state = state if state is not None else {}
    state.setdefault("active", 0)
    state.setdefault("max", 0)
    state.setdefault("calls", [])
    lock = threading.Lock()
    good = peer_statement_factory(PEER_ACCOUNTS)

    def fake_fetch_statement(api_key, corp_code, year, fs_div_pref=("CFS", "OFS")):
        with lock:
            state["active"] += 1
            state["max"] = max(state["max"], state["active"])
            state["calls"].append(corp_code)
        try:
            time.sleep(delays.get(corp_code, 0))
            if corp_code in boom_codes:
                raise ValueError("예상 못 한 오류(%s)" % corp_code)
            return good(api_key, corp_code, year)
        finally:
            with lock:
                state["active"] -= 1

    return fake_fetch_statement


def test_build_industry_comparison_fetches_peers_in_parallel(monkeypatch):
    """피어 3곳을 동시에 받는다 — 한 순간에 3개 요청, 전체 시간은 피어 1곳 수준."""
    analysis = industry_analysis_for_parallel()
    state = {}
    fetch = instrumented_fetcher(delays={code: 0.2 for code in PEER_ACCOUNTS}, state=state)
    monkeypatch.setattr(fs, "fetch_statement", fetch)
    monkeypatch.setattr(fs, "fetch_extra_accounts", lambda *a, **k: {})

    started = time.monotonic()
    result = fs.build_industry_comparison("key", analysis)
    elapsed = time.monotonic() - started

    assert result["status"] == "ok"
    assert fs.PEER_FETCH_WORKERS == 3
    assert state["max"] == 3                     # 3곳이 겹쳐서 조회되었다(순차면 1)
    assert elapsed < 0.5                         # 순차 조회라면 0.6초 이상 걸린다
    assert len(result["peers"]) == 3
    assert sorted(state["calls"]) == sorted(PEER_ACCOUNTS)   # 각 피어는 한 번씩만


def test_build_industry_comparison_keeps_selection_order(monkeypatch):
    """가장 느린 피어가 첫 순서여도 표 순서는 '선정 순서' 그대로다(끝난 순서가 아니다)."""
    analysis = industry_analysis_for_parallel()
    fetch = instrumented_fetcher(delays={"00066000": 0.25, "00164779": 0.03, "00000003": 0.0})
    monkeypatch.setattr(fs, "fetch_statement", fetch)
    monkeypatch.setattr(fs, "fetch_extra_accounts", lambda *a, **k: {})

    result = fs.build_industry_comparison("key", analysis)

    assert result["status"] == "ok"
    assert [peer["name"] for peer in result["peers"]] == PEER_ORDER
    rows = {row["key"]: row for row in result["rows"]}
    assert rows["op_margin"]["mean"] == pytest.approx((8.0 + 25.0 + 8.0) / 3)


def test_build_industry_comparison_isolates_unexpected_peer_error(monkeypatch):
    """한 피어가 예상 못 한 예외(ValueError)를 내도 그 피어만 빠지고 나머지는 그대로 남는다."""
    analysis = industry_analysis_for_parallel()
    fetch = instrumented_fetcher(boom_codes=("00164779",), delays={"00164779": 0.05})
    monkeypatch.setattr(fs, "fetch_statement", fetch)
    monkeypatch.setattr(fs, "fetch_extra_accounts", lambda *a, **k: {})

    result = fs.build_industry_comparison("key", analysis)

    assert result["status"] == "ok"                  # 본 비교는 살아 있다
    assert [peer["name"] for peer in result["peers"]] == ["LG전자", "삼성전기"]
    assert result["skipped"] == ["SK하이닉스"]
    rows = {row["key"]: row for row in result["rows"]}
    assert rows["op_margin"]["mean"] == pytest.approx(8.0)   # 남은 2곳으로만 평균


def test_peer_metrics_many_keeps_order_and_marks_failures(monkeypatch):
    """병렬 수집 함수 자체 — 입력 순서 유지, 실패는 None, 빈 목록은 그대로 빈 목록."""
    def fake_peer_metrics(api_key, corp_code, year):
        if corp_code == "fail":
            raise ValueError("boom")
        time.sleep({"slow": 0.15, "mid": 0.05}.get(corp_code, 0))
        return {"op_margin": float(len(corp_code))}

    monkeypatch.setattr(fs, "peer_metrics", fake_peer_metrics)
    peers = [{"corp_code": "slow", "name": "가"},
             {"corp_code": "fail", "name": "나"},
             {"corp_code": "mid", "name": "다"}]

    out = fs.peer_metrics_many("key", peers, 2025)
    assert [peer["name"] for peer, _ in out] == ["가", "나", "다"]
    assert [metrics for _, metrics in out] == [{"op_margin": 4.0}, None, {"op_margin": 3.0}]
    assert fs.peer_metrics_many("key", [], 2025) == []

    # 피어가 1곳이면 스레드를 만들지 않고 그대로 받는다.
    single = fs.peer_metrics_many("key", [{"corp_code": "mid", "name": "다"}], 2025)
    assert [peer["name"] for peer, _ in single] == ["다"]
    assert single[0][1] == {"op_margin": 3.0}


def test_build_analysis_attaches_peers_only_for_single_company(monkeypatch):
    """단일 기업이면 동종업계 비교를 붙이고, 두 기업 비교면 붙이지 않는다."""
    monkeypatch.setattr(fs, "fetch_disclosure_risks", lambda *a, **k: {"status": "clear", "matches": []})

    def fake_collect(api_key, corp, years, verbose=True):
        company = peer_company(corp["corp_name"], corp["corp_code"], corp["stock_code"], SAMSUNG_ACCOUNTS)
        company["years"] = {year: dict(company["years"][2025]) for year in years}
        company["latest_rcept_no"] = ""
        company["fs_nm"] = "연결재무제표"
        company["statement_links"] = {year: {"main": ""} for year in years}
        for year in years:
            company["years"][year]["statement_links"] = company["statement_links"][year]
        return company

    monkeypatch.setattr(fs, "collect_company", fake_collect)
    monkeypatch.setattr(fs, "fetch_statement_links", lambda *a, **k: {"main": ""})
    monkeypatch.setattr(fs, "build_industry_comparison", lambda *a, **k: {"status": "ok", "comment": "비교"})

    single = fs.build_analysis("key", [{"corp_name": "삼성전자", "corp_code": "00126380",
                                        "stock_code": "005930"}], [2024, 2025], verbose=False)
    assert single["industry_peers"]["status"] == "ok"

    twice = fs.build_analysis("key", [
        {"corp_name": "삼성전자", "corp_code": "00126380", "stock_code": "005930"},
        {"corp_name": "LG전자", "corp_code": "00066000", "stock_code": "066570"},
    ], [2024, 2025], verbose=False)
    assert twice["industry_peers"] is None


# ---------------------------------------------------------------------------
# 9. 엑셀(.xlsx) 리포트 — excel_export.build_excel_report
# ---------------------------------------------------------------------------

# 실제 규모에 가까운 2개년 (2024년 매출 1.0조원 → 10,000억원)
EXCEL_ACCOUNTS = {
    2023: {"revenue": 1.2e12, "op_income": 8.0e10, "net_income": 4.0e10,
           "assets": 2.0e12, "current_assets": 9.0e11, "liabilities": 1.4e12,
           "current_liab": 6.0e11, "equity": 6.0e11,
           "interest_expense": 2.0e10, "op_cash_flow": 9.0e10, "retained_earnings": 2.0e11,
           "cost_of_sales": 9.0e11, "receivables": 1.5e11, "inventory": 1.2e11, "payables": 8.0e10},
    2024: {"revenue": 1.0e12, "op_income": -6.0e10, "net_income": -3.0e10,
           "assets": 1.9e12, "current_assets": 8.0e11, "liabilities": 1.95e12,
           "current_liab": 1.0e12, "equity": 5.0e10,
           "interest_expense": 3.0e10, "op_cash_flow": -2.0e10, "retained_earnings": -6.0e10,
           "cost_of_sales": 8.0e11, "receivables": 1.3e11, "inventory": 1.0e11, "payables": 9.0e10},
}

# 비교용 두 번째 기업 — 흑자 기업(ROE 격차가 실제로 생기게)
EXCEL_ACCOUNTS_B = {
    2023: {"revenue": 1.5e12, "op_income": 1.2e11, "net_income": 8.0e10,
           "assets": 2.4e12, "current_assets": 1.1e12, "liabilities": 1.6e12,
           "current_liab": 7.0e11, "equity": 8.0e11,
           "interest_expense": 1.5e10, "op_cash_flow": 1.3e11, "retained_earnings": 3.0e11,
           "cost_of_sales": 1.0e12, "receivables": 2.0e11, "inventory": 1.5e11, "payables": 1.1e11},
    2024: {"revenue": 1.6e12, "op_income": 1.4e11, "net_income": 9.0e10,
           "assets": 2.6e12, "current_assets": 1.2e12, "liabilities": 1.7e12,
           "current_liab": 8.0e11, "equity": 9.0e11,
           "interest_expense": 1.4e10, "op_cash_flow": 1.5e11, "retained_earnings": 3.9e11,
           "cost_of_sales": 1.05e12, "receivables": 2.1e11, "inventory": 1.6e11, "payables": 1.2e11},
}

CLEAR_DISCLOSURE = {
    "status": "clear", "matches": [], "scanned": 40, "total": 40, "pages": 1,
    "truncated": False, "oldest": "2024-12-01", "period": "2025-11-26 ~ 2026-11-26",
    "error": "", "corrections": 0,
}


def excel_company(name="삼성전자", corp_code="00126380", stock_code="005930",
                  impaired=False, disclosure=None, accounts=None):
    """엑셀 리포트 테스트용 기업 dict — 비율·CCC·리스크·코멘트까지 실제 엔진으로 채운다."""
    source = accounts or EXCEL_ACCOUNTS
    values_by_year = {year: dict(values) for year, values in source.items()}
    if impaired and 2024 in values_by_year:
        values_by_year[2024]["equity"] = -5.0e10

    years_data = {}
    for year, values in values_by_year.items():
        years_data[year] = {
            "accounts": {key: {"amount": value} for key, value in values.items()},
            "rcept_no": "20250330%06d" % (year - 2000),
            "fs_div": "CFS",
            "statement_links": {"income": "", "balance": "", "cashflow": "",
                                "main": "", "labels": {}, "fallback": False},
        }
    years = sorted(years_data)
    fs.compute_ratios(years_data, years)
    fs.compute_working_capital(years_data, years)

    company = {
        "name": name, "corp_code": corp_code, "stock_code": stock_code,
        "years": years_data, "fs_div": "CFS", "fs_nm": "연결재무제표",
        "latest_rcept_no": years_data[years[-1]]["rcept_no"],
        "statement_links": {year: years_data[year]["statement_links"] for year in years},
        "disclosure": disclosure or dict(CLEAR_DISCLOSURE),
    }
    company["risk"] = fs.evaluate_risk(company, years)
    company["dupont_comment"] = fs.make_dupont_comment(company, years[-1])
    company["ccc_comment"] = fs.make_ccc_comment(company, years)
    return company


def excel_analysis(companies, industry=None):
    years = [2023, 2024]
    return {
        "companies": companies,
        "years": years,
        "dupont_gap": fs.build_dupont_gap(companies, years[-1]) if len(companies) == 2 else None,
        "ccc_gap": fs.build_ccc_gap(companies, years[-1]) if len(companies) == 2 else None,
        "industry_peers": industry,
    }


def excel_industry_block():
    target_values = {"op_margin": 12.0, "roe": 8.0, "debt_ratio": 70.0,
                     "current_ratio": 150.0, "ccc": 70.0}
    peers = [
        {"corp_code": "00066000", "corp_name": "LG전자(주)", "name": "LG전자",
         "stock_code": "066570", "induty_code": "264", "match": "same", "match_label": "동일 코드",
         "metrics": {"op_margin": 6.0, "roe": 5.0, "debt_ratio": 90.0,
                     "current_ratio": 120.0, "ccc": 80.0, "fs_nm": "연결재무제표"}},
        {"corp_code": "00164779", "corp_name": "에스케이하이닉스(주)", "name": "SK하이닉스",
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


def excel_book(analysis):
    payload = xl.build_excel_report(analysis)
    assert isinstance(payload, bytes) and payload[:2] == b"PK"     # xlsx(zip) 시그니처
    return load_workbook(BytesIO(payload))


def sheet_values(ws):
    return [cell.value for row in ws.iter_rows() for cell in row if cell.value is not None]


def row_of(ws, label):
    for row in ws.iter_rows(min_col=1, max_col=1):
        if row[0].value == label:
            return row[0].row
    raise AssertionError("'%s' 행을 찾지 못했습니다" % label)


def test_excel_report_has_four_named_sheets_and_filename():
    analysis = excel_analysis([excel_company()])
    wb = excel_book(analysis)
    assert wb.sheetnames == ["요약 및 리스크", "재무제표 및 비율", "DuPont 및 CCC", "동종업계 및 공시"]
    assert xl.excel_filename(analysis) == "FinSight_삼성전자_2024.xlsx"


def test_excel_summary_sheet_has_score_grade_and_five_risks():
    company = excel_company(impaired=True)
    wb = excel_book(excel_analysis([company]))
    values = sheet_values(wb["요약 및 리스크"])

    assert "삼성전자" in values
    assert "%d점 / 100점" % company["risk"]["score"] in values
    assert "자본잠식 · 위험" in values                       # 자본잠식은 등급을 위험으로 고정
    assert "자본잠식 · 자본총계 -500억원" in values
    assert "판정 기준" in values and "메모" in values       # 5대 리스크 표 헤더
    for item in company["risk"]["items"]:
        assert item["label"] in values                        # 5대 항목별 수치·점수·판정
        assert item["value_text"] in values
    assert "최근 1년 공시 위험 요약" in values
    assert "0건 (최근 1년 위험 키워드 공시 없음)" in values     # 공시 위험 없음


def test_excel_amounts_are_eok_with_comma_and_ratio_format():
    wb = excel_book(excel_analysis([excel_company()]))
    ws = wb["재무제표 및 비율"]

    revenue_row = row_of(ws, "매출액")
    assert ws.cell(row=revenue_row, column=2).value == 12000          # 1.2조원 → 12,000억원
    assert ws.cell(row=revenue_row, column=3).value == 10000
    assert ws.cell(row=revenue_row, column=2).number_format == "#,##0;[Red]-#,##0"

    ratio_row = row_of(ws, "부채비율")
    assert ws.cell(row=ratio_row, column=3).value == pytest.approx(3900.0)   # 1.95조 ÷ 500억
    assert "%" in ws.cell(row=ratio_row, column=3).number_format


def test_excel_marks_capital_impairment_in_ratio_table():
    company = excel_company(impaired=True)
    wb = excel_book(excel_analysis([company]))
    ws = wb["재무제표 및 비율"]

    for label in ("ROE (자기자본이익률)", "부채비율"):
        row = row_of(ws, label)
        assert isinstance(ws.cell(row=row, column=2).value, float)     # 2023년은 정상 계산
        assert ws.cell(row=row, column=3).value == "자본잠식"           # 2024년 자본총계 음수
        assert ws.cell(row=row, column=3).font.bold                     # 빨간 굵은 글씨

    note_text = "\n".join(str(v) for v in sheet_values(ws))
    assert "자본잠식(자본총계 0 이하): 삼성전자 2024" in note_text


def test_excel_dupont_and_ccc_values():
    company = excel_company()
    wb = excel_book(excel_analysis([company]))
    ws = wb["DuPont 및 CCC"]

    assert "DuPont ROE 3요인 분해" in sheet_values(ws)
    margin_row = row_of(ws, "순이익률 (%)")
    assert ws.cell(row=margin_row, column=2).value == pytest.approx(3.3333, abs=0.001)
    roe_row = row_of(ws, "DuPont ROE (%)")
    assert ws.cell(row=roe_row, column=3).value == pytest.approx(
        company["years"][2024]["dupont"]["roe"], abs=0.001)

    ccc_row = row_of(ws, "CCC — 현금전환주기 (일)")
    assert ws.cell(row=ccc_row, column=3).value == pytest.approx(
        company["years"][2024]["ccc"]["ccc"], abs=0.001)
    assert ws.cell(row=ccc_row, column=3).number_format == '#,##0"일"'


def test_excel_two_companies_columns_and_dupont_gap():
    a = excel_company()
    b = excel_company("SK하이닉스", "00164779", "000660", accounts=EXCEL_ACCOUNTS_B)
    analysis = excel_analysis([a, b])
    assert analysis["dupont_gap"] and abs(analysis["dupont_gap"]["total"]) > 0.1

    wb = excel_book(analysis)
    ws = wb["재무제표 및 비율"]
    header = row_of(ws, "계정")
    assert [ws.cell(row=header, column=col).value for col in range(1, 6)] == [
        "계정", "삼성전자 2023", "삼성전자 2024", "SK하이닉스 2023", "SK하이닉스 2024"]

    dupont_ws = wb["DuPont 및 CCC"]
    titles = "\n".join(str(v) for v in sheet_values(dupont_ws))
    assert "ROE 격차 순차 대체법 분해" in titles
    total_row = row_of(dupont_ws, "합계 — ROE 격차")
    assert dupont_ws.cell(row=total_row, column=5).value == pytest.approx(
        analysis["dupont_gap"]["total"], abs=0.01)
    assert xl.excel_filename(analysis) == "FinSight_삼성전자_vs_SK하이닉스_2024.xlsx"


def test_excel_industry_peers_and_disclosure_links():
    disclosure = dict(CLEAR_DISCLOSURE)
    disclosure.update({
        "status": "ok", "scanned": 120, "total": 150, "corrections": 1,
        "matches": [{
            "labels": ["소송"], "severity": "severe", "report_nm": "소송등의제기",
            "rcept_dt": "2025-05-01", "rcept_no": "20250501000001",
            "url": fs.dart_link("20250501000001"), "correction": False, "subsidiary": False,
        }],
    })
    analysis = excel_analysis([excel_company(disclosure=disclosure)],
                              industry=excel_industry_block())
    wb = excel_book(analysis)
    ws = wb["동종업계 및 공시"]
    values = sheet_values(ws)
    text = "\n".join(str(v) for v in values)

    assert "업종코드 264" in text
    assert "피어 중앙값" in values and "피어 평균" in values
    assert "LG전자" in values and "SK하이닉스" in values
    assert "소송등의제기" in values and "심각" in values

    url = fs.dart_link("20250501000001")
    linked = [cell for row in ws.iter_rows() for cell in row
              if cell.value == url and cell.hyperlink is not None]
    assert linked and linked[0].hyperlink.target == url


def test_excel_survives_missing_accounts():
    company = excel_company(accounts={2023: {"revenue": 1.0e12}, 2024: {"revenue": 1.1e12}})
    wb = excel_book(excel_analysis([company]))

    ws = wb["재무제표 및 비율"]
    revenue_row = row_of(ws, "매출액")
    assert ws.cell(row=revenue_row, column=3).value == 11000
    equity_row = row_of(ws, "자본총계")
    assert ws.cell(row=equity_row, column=3).value == "-"            # 미공시 → 흐린 '-'

    ccc_ws = wb["DuPont 및 CCC"]
    ccc_row = row_of(ccc_ws, "CCC — 현금전환주기 (일)")
    assert ccc_ws.cell(row=ccc_row, column=3).value == "-"


def test_excel_industry_missing_is_quiet():
    wb = excel_book(excel_analysis([excel_company()], industry=None))
    text = "\n".join(str(v) for v in sheet_values(wb["동종업계 및 공시"]))
    assert "동종업계 비교 없음" in text


def test_excel_empty_analysis_raises():
    with pytest.raises(ValueError):
        xl.build_excel_report({})
    with pytest.raises(ValueError):
        xl.build_excel_report({"companies": [], "years": [2024]})


def test_excel_filename_replaces_forbidden_characters():
    company = excel_company(name="A/B:C*?")
    filename = xl.excel_filename(excel_analysis([company]))
    for char in '\\/:*?"<>|':
        assert char not in filename
    assert filename.endswith("_2024.xlsx")


# ---------------------------------------------------------------------------
# 4-6. 3표 연결형 FP&A 시뮬레이터 — simulate_fpa_scenarios()
# ---------------------------------------------------------------------------
# 손으로 검산하기 좋은 숫자로 맞췄다.
#   매출 1,000e9 · 매출원가 600e9(60%) · 판관비 300e9(30%) · 영업이익 100e9(10%)
#   감가상각비 60e9 · 유형자산의 취득 -120e9(현금 유출 → CAPEX 120e9) · 이자비용 20e9
#   현금성자산 200e9 · 단기차입금 100e9 + 사채 50e9 = 차입금 150e9
#   매출채권 100e9 → DSO 36.5일 / 재고자산 50e9 → DIO 30.42일 / 매입채무 60e9 → DPO 36.5일
#   CCC = 36.5 + 30.4167 − 36.5 = 30.4167일 / 운전자본(근사) = 1,000e9 ÷ 365 × 30.4167 = 83.3333e9

FPA_SPEC = {
    2023: {"revenue": 900e9, "op_income": 90e9, "net_income": 70e9, "assets": 1400e9,
           "current_assets": 450e9, "liabilities": 850e9, "current_liab": 280e9,
           "equity": 550e9, "cost_of_sales": 540e9, "sgna": 270e9, "receivables": 90e9,
           "inventory": 45e9, "payables": 54e9},
    2024: {"revenue": 1000e9, "op_income": 100e9, "net_income": 75e9, "assets": 1500e9,
           "current_assets": 500e9, "liabilities": 900e9, "current_liab": 300e9,
           "equity": 600e9, "cost_of_sales": 600e9, "sgna": 300e9, "receivables": 100e9,
           "inventory": 50e9, "payables": 60e9, "cash": 200e9,
           "short_term_borrowings": 100e9, "bonds": 50e9, "depreciation": 60e9,
           "capex": -120e9, "interest_expense": 20e9},
}


def fpa_company(spec=None, name="테스트"):
    years_data, years = make_years(spec or FPA_SPEC)
    fs.compute_ratios(years_data, years)
    fs.compute_working_capital(years_data, years)
    return {"name": name, "corp_code": "00000000", "stock_code": "000000", "years": years_data}


def test_fpa_baseline_reads_three_statements() -> None:
    company = fpa_company()
    baseline = fs.fpa_baseline(company)

    assert baseline["year"] == 2024
    assert baseline["revenue"] == 1000e9 and baseline["op_income"] == 100e9
    assert baseline["op_margin"] == pytest.approx(10.0)
    # 손익계산서 원가구조
    assert baseline["cost_structure_available"] is True
    assert baseline["cost_of_sales"] == 600e9 and baseline["sgna"] == 300e9
    assert baseline["cogs_ratio"] == pytest.approx(0.6)
    assert baseline["sgna_ratio"] == pytest.approx(0.3)
    assert baseline["op_residual"] == pytest.approx(0.0)   # 공시 영업이익 = 매출 − 원가 − 판관비
    # 재무상태표 · 현금흐름표
    assert baseline["cash"] == 200e9 and baseline["cash_available"] is True
    assert baseline["borrowings"] == 150e9 and baseline["borrowings_available"] is True
    assert baseline["depreciation"] == 60e9 and baseline["depreciation_available"] is True
    assert baseline["capex"] == 120e9 and baseline["capex_available"] is True  # 음수를 양수로
    assert baseline["capex_ratio"] == pytest.approx(12.0)
    assert baseline["interest_expense"] == 20e9
    # 운전자본
    assert baseline["ccc"] == pytest.approx(30.4167, abs=1e-3)
    assert baseline["working_capital"] == pytest.approx(83.3333e9, rel=1e-4)

    assert baseline["capital_impaired"] is False and baseline["op_loss"] is False
    assert fs.fpa_baseline(company, 2023)["revenue"] == 900e9
    # 2023년은 현금·CAPEX·이자비용이 없어 '미공시'로 구분된다(0과 다르다)
    old = fs.fpa_baseline(company, 2023)
    assert old["cash"] is None and old["cash_available"] is False
    assert old["capex"] is None and old["capex_available"] is False
    assert fs.fpa_baseline({"years": {}}) is None
    assert fs.fpa_baseline({}) is None


def test_fpa_default_assumptions_use_disclosed_capex_ratio() -> None:
    defaults = fs.fpa_default_assumptions(fs.fpa_baseline(fpa_company()))
    assert defaults["fixed_cost_ratio_pct"] == pytest.approx(fs.FPNA_DEFAULT_FIXED_COST_RATIO)
    assert defaults["capex_pct"] == pytest.approx(12.0)
    # 슬라이더 간격(0.5%p)에 맞춰 반올림 — 화면 기본값과 계산값이 어긋나지 않게
    assert fs.fpa_default_assumptions({"capex_ratio": 14.2})["capex_pct"] == pytest.approx(14.0)
    assert fs.fpa_default_assumptions({"capex_ratio": 99.0})["capex_pct"] == pytest.approx(30.0)
    # CAPEX 미공시면 0%에서 시작한다
    assert fs.fpa_default_assumptions({"capex_ratio": None})["capex_pct"] == 0.0


def test_fpa_depreciation_prefers_combined_and_never_double_counts() -> None:
    combined = fs.fpa_depreciation({"da_combined": {"amount": 90e9},
                                    "depreciation": {"amount": 60e9}})
    assert combined["amount"] == 90e9 and combined["basis"] == "combined"

    parts = fs.fpa_depreciation({"depreciation": {"amount": 60e9},
                                 "amortization": {"amount": 20e9},
                                 "lease_depreciation": {"amount": 10e9}})
    assert parts["amount"] == 90e9 and parts["basis"] == "sum"

    # 음수로 들어와도 절댓값으로 더한다(현금흐름표 표기 흔들림 방어)
    assert fs.fpa_depreciation({"depreciation": {"amount": -60e9}})["amount"] == 60e9
    missing = fs.fpa_depreciation({})
    assert missing["amount"] is None and missing["available"] is False


def test_fpa_total_borrowings_sums_parts_then_falls_back_to_single_line() -> None:
    parts = fs.fpa_total_borrowings({"short_term_borrowings": {"amount": 100e9},
                                     "long_term_borrowings": {"amount": 30e9},
                                     "bonds": {"amount": 20e9}})
    assert parts["amount"] == 150e9 and parts["available"] is True
    assert fs.fpa_total_borrowings({"borrowings": {"amount": 70e9}})["amount"] == 70e9
    assert fs.fpa_total_borrowings({})["available"] is False


def test_fetch_extra_accounts_normalizes_new_three_statement_accounts(monkeypatch) -> None:
    """전체 재무제표 응답에서 3표 연결용 계정을 뽑는다(네트워크 없이 행 목록으로 대체)."""
    rows = [
        {"sj_div": "IS", "account_nm": "판매비와 관리비", "account_id": "-표준계정코드 미사용-",
         "thstrm_amount": "300,000,000,000", "frmtrm_amount": "270,000,000,000"},
        {"sj_div": "BS", "account_nm": "현금및현금성자산", "account_id": "ifrs-full_CashAndCashEquivalents",
         "thstrm_amount": "200,000,000,000", "frmtrm_amount": "180,000,000,000"},
        {"sj_div": "BS", "account_nm": "단기차입금", "account_id": "-표준계정코드 미사용-",
         "thstrm_amount": "100,000,000,000", "frmtrm_amount": "90,000,000,000"},
        {"sj_div": "BS", "account_nm": "사채", "account_id": "ifrs-full_NoncurrentPortionOfNoncurrentBondsIssued",
         "thstrm_amount": "50,000,000,000", "frmtrm_amount": "50,000,000,000"},
        {"sj_div": "CF", "account_nm": "감가상각비", "account_id": "-표준계정코드 미사용-",
         "thstrm_amount": "60,000,000,000", "frmtrm_amount": "55,000,000,000"},
        {"sj_div": "CF", "account_nm": "유형자산의 취득", "account_id": "-표준계정코드 미사용-",
         "thstrm_amount": "-120,000,000,000", "frmtrm_amount": "-100,000,000,000"},
    ]
    monkeypatch.setattr(fs, "fetch_full_statement_rows", lambda *a, **k: ("CFS", rows))

    found = fs.fetch_extra_accounts("fake-key", "00126380", 2024, fs_div="CFS")
    assert found["sgna"]["amount"] == 300e9
    assert found["cash"]["amount"] == 200e9
    assert found["short_term_borrowings"]["amount"] == 100e9
    assert found["bonds"]["amount"] == 50e9
    assert found["depreciation"]["amount"] == 60e9
    assert found["capex"]["amount"] == -120e9        # 원본 부호는 그대로, 해석은 fpa_capex가 한다

    baseline = fs.fpa_baseline({"years": {2024: {"accounts": found, "ratios": {}}}})
    assert baseline["depreciation"] == 60e9
    assert baseline["capex"] == 120e9
    assert baseline["borrowings"] == 150e9


def test_simulate_fpa_scenarios_three_statement_math() -> None:
    """Y+1~Y+3 손익 → EBITDA → FCF → 기말 현금을 손으로 검산한다."""
    company = fpa_company()
    result = fs.simulate_fpa_scenarios(company, 2024, revenue_growth_pct=10.0,
                                      fixed_cost_ratio_pct=70.0, ccc_delta_days=0.0, capex_pct=12.0)

    assert result["status"] == "ok"
    assert result["base_year"] == 2024 and result["horizon"] == 3
    assert [s["label"] for s in result["scenarios"]] == ["보수적", "기준", "낙관적", "내 시나리오"]
    assert result["scenarios"][3]["is_custom"] is True
    assert result["assumptions"] == {"revenue_growth_pct": 10.0, "fixed_cost_ratio_pct": 70.0,
                                     "ccc_delta_days": 0.0, "capex_pct": 12.0}

    custom = result["custom"]
    rows = custom["years"]
    # Y+1: 매출 1,100 / 매출원가 660 / 판관비 210+99=309 / 영업이익 131
    assert rows[0]["revenue"] == pytest.approx(1100e9)
    assert rows[0]["cost_of_sales"] == pytest.approx(660e9)
    assert rows[0]["sgna"] == pytest.approx(309e9)
    assert rows[0]["op_income"] == pytest.approx(131e9)
    assert rows[0]["op_margin"] == pytest.approx(131e9 / 1100e9 * 100.0)
    # EBITDA 131+60=191, 운전자본 1100/365×30.4167=91.667 → 증감 8.333, CAPEX 132, 이자 20
    assert rows[0]["ebitda"] == pytest.approx(191e9)
    assert rows[0]["delta_working_capital"] == pytest.approx(8.3333e9, rel=1e-4)
    assert rows[0]["capex"] == pytest.approx(132e9)
    assert rows[0]["fcf"] == pytest.approx(30.6667e9, rel=1e-4)
    assert rows[0]["ending_cash"] == pytest.approx(230.6667e9, rel=1e-4)
    assert rows[0]["interest_coverage"] == pytest.approx(6.55)
    # Y+3: 매출 1,331 / 영업이익 202.61
    assert rows[2]["revenue"] == pytest.approx(1000e9 * 1.1 ** 3)
    assert rows[2]["sgna"] == pytest.approx(210e9 + 1331e9 * 0.09)
    assert rows[2]["op_income"] == pytest.approx(1331e9 - 1331e9 * 0.6 - (210e9 + 1331e9 * 0.09))
    assert custom["end_revenue"] == pytest.approx(rows[2]["revenue"])
    assert custom["cum_fcf"] == pytest.approx(sum(row["fcf"] for row in rows))
    assert custom["end_cash"] == pytest.approx(200e9 + custom["cum_fcf"])
    # 기초 현금 대비 늘어난다 — FCF가 계속 플러스이기 때문
    assert custom["cum_fcf"] > 0

    # 손익계산서 항등식: 매출 − 매출원가 − 판관비 = 영업이익 (기타손익 0)
    for row in rows:
        assert row["revenue"] - row["cost_of_sales"] - row["sgna"] == pytest.approx(row["op_income"])

    bear, base, bull = result["scenarios"][0], result["scenarios"][1], result["scenarios"][2]
    assert bear["end_revenue"] < base["end_revenue"] < bull["end_revenue"]
    assert bear["end_op_income"] < base["end_op_income"] < bull["end_op_income"]
    assert bear["cum_fcf"] < base["cum_fcf"] < bull["cum_fcf"]
    # 기준 시나리오(성장 0%)는 출발 연도 손익을 그대로 재현한다
    for row in base["years"]:
        assert row["revenue"] == pytest.approx(1000e9)
        assert row["op_income"] == pytest.approx(100e9)
        assert row["delta_working_capital"] == pytest.approx(0.0, abs=1e-6)


def test_simulate_fpa_scenarios_operating_leverage_from_fixed_cost_ratio() -> None:
    """판관비 고정비 비중이 높으면 매출이 늘 때 영업이익이 더 빠르게 는다."""
    company = fpa_company()
    high = fs.simulate_fpa_scenarios(company, 2024, revenue_growth_pct=10.0, fixed_cost_ratio_pct=100.0)
    low = fs.simulate_fpa_scenarios(company, 2024, revenue_growth_pct=10.0, fixed_cost_ratio_pct=0.0)

    # 고정비 100%: 판관비 300 고정 → 영업이익 1100−660−300 = 140
    assert high["custom"]["years"][0]["op_income"] == pytest.approx(140e9)
    # 고정비 0%: 판관비가 매출을 따라 330 → 영업이익 1100−660−330 = 110
    assert low["custom"]["years"][0]["op_income"] == pytest.approx(110e9)
    assert high["custom"]["years"][0]["op_margin"] > low["custom"]["years"][0]["op_margin"]

    # 반대로 매출이 줄면 고정비가 큰 쪽이 더 크게 나빠진다
    high_down = fs.simulate_fpa_scenarios(company, 2024, revenue_growth_pct=-10.0, fixed_cost_ratio_pct=100.0)
    low_down = fs.simulate_fpa_scenarios(company, 2024, revenue_growth_pct=-10.0, fixed_cost_ratio_pct=0.0)
    assert high_down["custom"]["years"][0]["op_income"] < low_down["custom"]["years"][0]["op_income"]


def test_simulate_fpa_scenarios_flags_interest_and_cash_alerts() -> None:
    """이자보상배율 1배 미만과 현금 고갈을 각각 첫 해로 잡는다."""
    spec = {
        2024: {"revenue": 1000e9, "op_income": 50e9, "net_income": 10e9,
               "assets": 900e9, "liabilities": 800e9, "equity": 100e9,
               "cost_of_sales": 700e9, "sgna": 250e9,
               "cash": 20e9, "depreciation": 30e9, "capex": -300e9, "interest_expense": 60e9},
    }
    company = fpa_company(spec)
    result = fs.simulate_fpa_scenarios(company, 2024, revenue_growth_pct=0.0, capex_pct=25.0)

    custom = result["custom"]
    # 영업이익 50 < 이자 60 → 첫 해부터 이자보상배율 0.83배
    assert custom["years"][0]["interest_coverage"] == pytest.approx(50e9 / 60e9)
    assert custom["first_icr_alert_step"] == 1
    assert custom["years"][0]["icr_alert"] is True
    # FCF = (50+30) − 0 − 250 − 60 = −230 → 기초 현금 20에서 첫 해에 이미 고갈
    assert custom["years"][0]["fcf"] == pytest.approx(-230e9)
    assert custom["first_cash_alert_step"] == 1
    assert custom["years"][0]["ending_cash"] < 0
    assert "이자보상배율" in custom["alert"]["text"]
    assert "경보선" in custom["alert_text"]

    # 경보가 없는 기업은 '-' 대신 안내 문구가 나온다
    safe = fs.simulate_fpa_scenarios(fpa_company(), revenue_growth_pct=0.0)
    assert safe["custom"]["alert"] is None
    assert "닿지 않습니다" in safe["custom"]["alert_text"]


def test_simulate_fpa_scenarios_falls_back_to_margin_without_cost_structure() -> None:
    """금융업처럼 매출원가·판관비가 없으면 영업이익률 고정 방식으로 물러난다(예외 없음)."""
    spec = {
        2024: {"revenue": 1000e9, "op_income": 80e9, "net_income": 60e9,
               "assets": 5000e9, "liabilities": 4500e9, "equity": 500e9,
               "interest_expense": 30e9, "cash": 100e9},
    }
    company = fpa_company(spec)
    result = fs.simulate_fpa_scenarios(company, 2024, revenue_growth_pct=4.0, fixed_cost_ratio_pct=100.0)

    assert result["status"] == "ok"
    assert result["cost_structure"] is False
    custom = result["custom"]
    assert custom["end_revenue"] == pytest.approx(1000e9 * 1.04 ** 3)
    assert custom["end_op_income"] == pytest.approx(1000e9 * 1.04 ** 3 * 0.08)
    assert all(row["cost_of_sales"] is None and row["sgna"] is None for row in custom["years"])
    # CCC를 못 구해도 FCF는 운전자본 없이 계산된다
    assert result["wc_available"] is False
    assert all(row["delta_working_capital"] is None for row in custom["years"])
    assert custom["cum_fcf"] is not None
    assert "영업이익률을 출발 연도 수준" in result["insights"][0] or "고정해 계산" in result["insights"][0]


def test_simulate_fpa_scenarios_handles_missing_cash_depreciation_and_interest() -> None:
    """현금·감가상각비·이자비용이 미공시여도 예외 없이 None으로 물러난다."""
    spec = {
        2024: {"revenue": 500e9, "op_income": 40e9, "net_income": 30e9,
               "assets": 700e9, "liabilities": 300e9, "equity": 400e9,
               "cost_of_sales": 300e9, "sgna": 160e9},
    }
    result = fs.simulate_fpa_scenarios(fpa_company(spec), 2024, revenue_growth_pct=5.0)

    assert result["status"] == "ok"
    assert result["cash_available"] is False
    assert result["depreciation_available"] is False
    assert result["interest_available"] is False
    custom = result["custom"]
    assert all(row["ebitda"] == row["op_income"] for row in custom["years"])   # 감가상각비 0 취급
    assert all(row["ending_cash"] is None for row in custom["years"])
    assert all(row["interest_coverage"] is None for row in custom["years"])
    assert custom["cum_fcf"] is not None
    assert result["depreciation_available"] is False      # 화면이 이 값으로 'EBITDA=영업이익' 고지를 띄운다
    assert "FCF" in result["insights"][1]


def test_simulate_fpa_scenarios_revenue_missing_or_zero() -> None:
    empty = fs.simulate_fpa_scenarios({})
    assert empty["status"] == "no_data" and empty["scenarios"] == []
    assert empty["limits"] == fs.FPNA_LIMITS

    no_revenue = fs.simulate_fpa_scenarios({"years": {2024: {"accounts": {}}}})
    assert no_revenue["status"] == "no_revenue"
    assert "매출액" in no_revenue["message"]
    assert no_revenue["scenarios"] == []

    zero = fs.simulate_fpa_scenarios({"years": {2024: {"accounts": {"revenue": {"amount": 0}}}}})
    assert zero["status"] == "no_revenue"                # 0으로 나누지 않는다


def test_simulate_fpa_scenarios_capital_impaired_and_operating_loss() -> None:
    spec = {
        2024: {"revenue": 100e9, "op_income": -20e9, "net_income": -30e9,
               "assets": 300e9, "current_assets": 100e9, "liabilities": 350e9,
               "current_liab": 250e9, "equity": -50e9,
               "cost_of_sales": 90e9, "sgna": 30e9, "receivables": 20e9,
               "inventory": 10e9, "payables": 6e9, "interest_expense": 10e9, "cash": 5e9},
    }
    company = fpa_company(spec)
    result = fs.simulate_fpa_scenarios(company, 2024, revenue_growth_pct=-30.0,
                                       fixed_cost_ratio_pct=70.0, ccc_delta_days=45.0)

    assert result["status"] == "ok"
    assert result["capital_impaired"] is True and result["op_loss"] is True
    custom = result["custom"]
    assert custom["years"][0]["op_income"] < 0
    assert custom["years"][0]["working_capital"] is not None
    assert custom["cum_fcf"] is not None
    assert custom["first_icr_alert_step"] == 1       # 영업적자 → 이자보상배율 음수


def test_simulate_fpa_scenarios_clamps_assumptions_to_slider_range() -> None:
    company = fpa_company()
    result = fs.simulate_fpa_scenarios(company, 2024, revenue_growth_pct=999.0,
                                       fixed_cost_ratio_pct=250.0, ccc_delta_days="abc",
                                       capex_pct=-5.0)

    assert result["assumptions"] == {"revenue_growth_pct": 40.0, "fixed_cost_ratio_pct": 100.0,
                                     "ccc_delta_days": 0.0, "capex_pct": 0.0}

    nan_result = fs.simulate_fpa_scenarios(company, revenue_growth_pct=float("nan"))
    assert nan_result["assumptions"]["revenue_growth_pct"] == 0.0
    assert nan_result["status"] == "ok"
    # 기본값(None)은 기업의 공시 비율에서 온다
    assert nan_result["assumptions"]["fixed_cost_ratio_pct"] == pytest.approx(70.0)
    assert nan_result["assumptions"]["capex_pct"] == pytest.approx(12.0)


def test_simulate_fpa_scenarios_does_not_mutate_company() -> None:
    company = fpa_company()
    before = json.dumps({str(y): company["years"][y]["accounts"] for y in company["years"]}, sort_keys=True)
    fs.simulate_fpa_scenarios(company, revenue_growth_pct=12.0, ccc_delta_days=-10)
    after = json.dumps({str(y): company["years"][y]["accounts"] for y in company["years"]}, sort_keys=True)
    assert before == after                               # 순수 함수 — 입력을 바꾸지 않는다


def test_simulate_fpa_scenarios_note_limits_and_insights() -> None:
    result = fs.simulate_fpa_scenarios(fpa_company(), revenue_growth_pct=5.0, ccc_delta_days=5)

    assert "투자 권유가 아니" in result["note"]
    assert "민감도" in result["note"]
    assert len(result["limits"]) >= 2                    # 공시 데이터 한계 고지
    assert any("감가상각비" in line for line in result["limits"])
    assert 2 <= len(result["insights"]) <= 3
    assert "영업레버리지" in result["insights"][0]        # 매출 대비 영업이익 민감도
    assert "FCF" in result["insights"][1]                 # 누적 FCF · 기말 현금
