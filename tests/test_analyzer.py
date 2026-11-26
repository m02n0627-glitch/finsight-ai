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

import os
import sys

import pytest

# 저장소 루트의 analyzer.py 를 불러온다. (tests/ 의 부모 폴더)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import analyzer as fs  # noqa: E402


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
