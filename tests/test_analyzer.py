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
