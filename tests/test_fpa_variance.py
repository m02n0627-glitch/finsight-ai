# -*- coding: utf-8 -*-
"""
fpa_variance.py 단위 테스트 (pytest)
====================================

FP&A 계획·차이 분석의 두 계산을 손으로 검산한다(네트워크 없음).
  · sensitivity_matrix / sensitivity_cell — 성장률 × 영업이익률 5×5, 영업이익·FCF
  · variance_rows / operating_variance_effects — 계획 vs 실적 차이와 원인 분해
  · parse_variance_csv — CSV 업로드 파싱

실행
    python3 -m pytest tests/test_fpa_variance.py -v
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fpa_variance as fv  # noqa: E402

E = 1e8          # 1억원


def base(**overrides):
    """매출 1,000억 · 영업이익률 10% · 감가상각비 50억 · CAPEX 5% · 이자 10억 기준값."""
    values = {
        "revenue": 1000 * E,
        "op_margin": 10.0,
        "depreciation": 50 * E,
        "capex_pct": 5.0,
        "ccc": 0.0,
        "working_capital": 0.0,
        "interest_expense": 10 * E,
    }
    values.update(overrides)
    return values


# ---------------------------------------------------------------------------
# 1. 민감도 매트릭스
# ---------------------------------------------------------------------------

def test_sensitivity_cell_math():
    cell = fv.sensitivity_cell(base(), growth_pct=0.0, margin_pct=10.0)

    assert cell["revenue"] == pytest.approx(1000 * E)
    assert cell["op_income"] == pytest.approx(100 * E)          # 1,000억 × 10%
    assert cell["ebitda"] == pytest.approx(150 * E)             # 영업이익 + 감가상각비 50억
    assert cell["capex"] == pytest.approx(50 * E)               # 매출의 5%
    assert cell["fcf"] == pytest.approx(150 * E - 50 * E - 10 * E)   # EBITDA − CAPEX − 이자
    assert cell["delta_wc"] == pytest.approx(0.0)


def test_sensitivity_cell_growth_and_margin_move_together():
    cell = fv.sensitivity_cell(base(), growth_pct=10.0, margin_pct=12.0)

    assert cell["revenue"] == pytest.approx(1100 * E)
    assert cell["op_income"] == pytest.approx(132 * E)          # 1,100억 × 12%
    assert cell["fcf"] == pytest.approx(132 * E + 50 * E - 55 * E - 10 * E)


def test_sensitivity_cell_working_capital_when_ccc_present():
    """CCC 30일이면 매출 1일분 × 30일이 운전자본 — 매출이 늘면 그만큼 현금이 묶인다."""
    values = base(ccc=30.0, working_capital=1000 * E / 365 * 30)
    cell = fv.sensitivity_cell(values, growth_pct=10.0, margin_pct=10.0)

    assert cell["delta_wc"] == pytest.approx((1100 - 1000) * E / 365 * 30)
    assert cell["fcf"] == pytest.approx(110 * E + 50 * E - cell["delta_wc"] - 55 * E - 10 * E)


def test_sensitivity_cell_returns_none_without_revenue():
    assert fv.sensitivity_cell({"revenue": 0}, 0.0, 10.0) is None
    assert fv.sensitivity_cell({}, 0.0, 10.0) is None


def test_margin_axis_has_five_steps_and_clips():
    assert fv.margin_axis(10.0) == [6.0, 8.0, 10.0, 12.0, 14.0]
    assert fv.margin_axis(88.0) == [84.0, 86.0, 88.0, 90.0, 90.0]     # 위쪽 90%로 자름
    assert fv.margin_axis(-48.0)[0] == -50.0                          # 아래쪽 −50%로 자름


def test_sensitivity_matrix_is_five_by_five_with_best_worst():
    matrix = fv.sensitivity_matrix(base())

    assert matrix["available"] is True
    assert len(matrix["growth"]) == 5 and len(matrix["margins"]) == 5
    assert all(len(row) == 5 for row in matrix["cells"])
    # 성장 +10% · 이익률 14%가 가장 좋고, 성장 −10% · 이익률 6%가 가장 나쁘다
    assert matrix["best"]["growth_pct"] == 10.0 and matrix["best"]["margin_pct"] == 14.0
    assert matrix["worst"]["growth_pct"] == -10.0 and matrix["worst"]["margin_pct"] == 6.0
    # 현재 가정(성장 0%, 이익률 10%) 칸이 표시된다
    assert matrix["base_cell"]["growth_pct"] == 0.0
    assert matrix["base_cell"]["margin_pct"] == pytest.approx(10.0)


def test_sensitivity_matrix_without_revenue_is_not_available():
    matrix = fv.sensitivity_matrix({"revenue": 0})
    assert matrix["available"] is False
    assert "계산할 수 없습니다" in fv.sensitivity_comment(matrix)


def test_sensitivity_comment_mentions_best_and_current():
    text = fv.sensitivity_comment(fv.sensitivity_matrix(base()))
    assert "가장 크고" in text and "현재 가정" in text


# ---------------------------------------------------------------------------
# 2. 예산 vs 실적 차이
# ---------------------------------------------------------------------------

def test_variance_rows_amounts_and_percent():
    plan = {"revenue": 1000 * E, "cost_of_sales": 700 * E, "sgna": 200 * E}
    actual = {"revenue": 1100 * E, "cost_of_sales": 760 * E, "sgna": 215 * E}
    rows = {row["key"]: row for row in fv.variance_rows(plan, actual)}

    assert rows["revenue"]["diff"] == pytest.approx(100 * E)
    assert rows["revenue"]["diff_pct"] == pytest.approx(10.0)
    assert rows["op_income"]["plan"] == pytest.approx(100 * E)     # 계산해서 채운다
    assert rows["op_income"]["actual"] == pytest.approx(125 * E)
    assert rows["op_income"]["diff"] == pytest.approx(25 * E)
    assert rows["op_income"]["favorable"] is True
    assert rows["cost_of_sales"]["favorable"] is False             # 원가는 늘면 불리
    assert rows["sgna"]["diff_pct"] == pytest.approx(7.5)


def test_operating_variance_decomposition_adds_up():
    plan = {"revenue": 1000 * E, "cost_of_sales": 700 * E, "sgna": 200 * E}
    actual = {"revenue": 1100 * E, "cost_of_sales": 760 * E, "sgna": 215 * E}
    effects = fv.operating_variance_effects(plan, actual)

    # 매출 효과 = 100억 × (1 − 계획 원가율 0.9) = 10억
    assert effects["revenue_effect"] == pytest.approx(10 * E)
    # 원가율 효과 = 매출원가율 1.0%p 개선 + 판관비율 0.45%p 개선 = 15억
    assert effects["cogs_rate_effect"] == pytest.approx(1100 * E * (0.7 - 760 / 1100))
    assert effects["sgna_rate_effect"] == pytest.approx(1100 * E * (0.2 - 215 / 1100))
    assert effects["cost_rate_effect"] == pytest.approx(
        effects["cogs_rate_effect"] + effects["sgna_rate_effect"])
    # 두 효과의 합 = 영업이익 차이 (항등식)
    assert effects["revenue_effect"] + effects["cost_rate_effect"] == pytest.approx(25 * E)
    assert effects["total"] == pytest.approx(25 * E)
    assert effects["check"] == pytest.approx(0.0, abs=1e-3)
    assert effects["plan_op_margin"] == pytest.approx(10.0)
    assert effects["actual_op_margin"] == pytest.approx(125 / 1100 * 100)


def test_variance_comment_reads_as_sentence():
    plan = {"revenue": 1000 * E, "cost_of_sales": 700 * E, "sgna": 200 * E}
    actual = {"revenue": 1100 * E, "cost_of_sales": 760 * E, "sgna": 215 * E}
    text = fv.variance_comment(fv.operating_variance_effects(plan, actual))
    assert "영업이익은 계획 대비" in text and "매출 증감 효과" in text
    assert "영업이익률은 10.0% → 11.4%" in text


def test_variance_handles_zero_plan_revenue():
    effects = fv.operating_variance_effects({"revenue": 0}, {"revenue": 100})
    assert effects["revenue_effect"] is None
    assert "분해할 수 없습니다" in fv.variance_comment(effects)


def test_sample_budget_is_won_units():
    sample = fv.sample_budget()
    assert sample["plan"]["revenue"] == pytest.approx(1000 * E)
    assert sample["actual"]["revenue"] == pytest.approx(1100 * E)


# ---------------------------------------------------------------------------
# 3. CSV 파싱
# ---------------------------------------------------------------------------

def test_parse_variance_csv_basic():
    text = "항목,계획,실적\n매출액,1000,1100\n매출원가,700,760\n판관비,200,215\n"
    parsed = fv.parse_variance_csv(text)

    assert parsed["plan"]["revenue"] == pytest.approx(1000 * E)
    assert parsed["actual"]["sgna"] == pytest.approx(215 * E)


def test_parse_variance_csv_tolerates_commas_and_spaces():
    text = " 항목 , 계획 , 실적 \n 매출액 , 1,000 , 1,100 \n매출원가,700,760\n판매비와관리비,200,215\n"
    parsed = fv.parse_variance_csv(text)
    assert parsed["plan"]["revenue"] == pytest.approx(1000 * E)
    assert parsed["actual"]["sgna"] == pytest.approx(215 * E)


def test_parse_variance_csv_requires_three_lines():
    assert fv.parse_variance_csv("항목,계획,실적\n매출액,1000,1100\n") is None
    assert fv.parse_variance_csv("") is None
    assert fv.parse_variance_csv(None) is None
