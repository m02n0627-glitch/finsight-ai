#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FP&A 계획·차이 분석 (fpa_variance.py)
=====================================

관리회계(FP&A)의 두 가지 기본 작업을 결정론적 순수 함수로 구현한다.

  1) 민감도 매트릭스 (Sensitivity Matrix)
     매출 성장률 × 영업이익률(원가율) 5×5 조합마다 영업이익과 잉여현금흐름(FCF)이
     얼마가 되는지 한 표로 보여 준다. "매출이 5% 늘고 원가율이 2%p 나빠지면?"을
     한눈에 비교하는 표다.

  2) 예산 대비 실적 차이 분석 (Budget vs Actual Variance)
     계획(예산)과 실적의 매출·매출원가·판관비·영업이익 차이를 금액과 증감률로 보여 주고,
     영업이익 차이를 ① 매출 증감 효과 ② 원가율(비용률) 변동 효과로 분해한다.

계산 규칙 (LLM·확률·난수 없음 — 같은 입력이면 언제나 같은 숫자)
  · 금액 단위는 '원'. 화면(억원) 변환은 앱이 한다.
  · 민감도 1년치
        매출액(n)   = 매출액(0) × (1 + 성장률)
        영업이익(n) = 매출액(n) × 영업이익률
        EBITDA(n)   = 영업이익(n) + 감가상각비(0)
        운전자본(n) = 매출액(n) ÷ 365 × CCC          (CCC가 있을 때만)
        CAPEX(n)    = 매출액(n) × 매출 대비 CAPEX 비율
        FCF(n)      = EBITDA(n) − (운전자본(n) − 운전자본(0)) − CAPEX(n) − 이자비용(0)
  · 차이 분해 (영업이익)
        계획 원가율 cr0 = (매출원가0 + 판관비0) ÷ 매출액0
        실적 원가율 cr1 = (매출원가1 + 판관비1) ÷ 매출액1
        매출 효과     = (매출액1 − 매출액0) × (1 − cr0)      ← 계획 원가율을 유지했다면
        원가율 효과   = 매출액1 × (cr0 − cr1)                ← 같은 매출에서 원가율이 달라진 몫
        두 효과의 합 = 영업이익1 − 영업이익0 (항등식)
      원가율 효과는 다시 매출원가율 효과와 판관비율 효과로 나눈다.

한계
  · 민감도는 1년치 단순 계산이다(3개년 누적은 analyzer.simulate_fpa_scenarios).
  · 차이 분해는 '매출 효과를 계획 원가율로 평가'하는 표준적 단순 분해다.
    제품 구성(mix)·환율·일회성 항목은 나누지 않는다.
"""

VARIANCE_LINES = (
    ("revenue", "매출액"),
    ("cost_of_sales", "매출원가"),
    ("sgna", "판관비"),
    ("op_income", "영업이익"),
)

# 민감도 기본 축 — 성장률은 −10~+10% 5단계, 이익률은 중심에서 ±4%p 5단계
SENSITIVITY_GROWTH_STEPS = (-10.0, -5.0, 0.0, 5.0, 10.0)
SENSITIVITY_MARGIN_OFFSETS = (-4.0, -2.0, 0.0, 2.0, 4.0)
MARGIN_FLOOR, MARGIN_CEIL = -50.0, 90.0

# 예산 vs 실적 체험용 샘플 (억원) — 계획 10% 이익, 실적은 매출 +10%·원가율 개선
SAMPLE_PLAN = {"revenue": 1000.0, "cost_of_sales": 700.0, "sgna": 200.0}
SAMPLE_ACTUAL = {"revenue": 1100.0, "cost_of_sales": 760.0, "sgna": 215.0}
SAMPLE_UNIT_LABEL = "억원"


def _number(value, default=0.0):
    """None·문자열·NaN을 안전하게 숫자로."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:                 # NaN
        return default
    return number


def margin_axis(center_pct, offsets=SENSITIVITY_MARGIN_OFFSETS):
    """중심 영업이익률에서 ±(2·4)%p 5단계 — −50~90% 범위로 자르고 소수 1자리."""
    center = _number(center_pct)
    values = []
    for offset in offsets:
        value = round(max(MARGIN_FLOOR, min(MARGIN_CEIL, center + _number(offset))), 1)
        values.append(value)
    return values


def sensitivity_cell(base, growth_pct, margin_pct):
    """한 조합(성장률, 영업이익률)의 1년치 매출·영업이익·EBITDA·FCF."""
    base = base or {}
    revenue0 = _number(base.get("revenue"))
    if revenue0 <= 0:
        return None
    growth = _number(growth_pct) / 100.0
    margin = _number(margin_pct) / 100.0
    revenue = revenue0 * (1.0 + growth)
    op_income = revenue * margin
    depreciation = _number(base.get("depreciation"))
    ebitda = op_income + depreciation

    ccc = base.get("ccc")
    working_capital0 = base.get("working_capital")
    if working_capital0 is None and ccc is not None and revenue0:
        working_capital0 = revenue0 / 365.0 * _number(ccc)
    delta_wc = 0.0
    if ccc is not None and working_capital0 is not None:
        delta_wc = revenue / 365.0 * _number(ccc) - _number(working_capital0)

    capex_pct = _number(base.get("capex_pct"))
    capex = revenue * capex_pct / 100.0
    interest = _number(base.get("interest_expense"))
    fcf = ebitda - delta_wc - capex - interest
    return {
        "growth_pct": _number(growth_pct),
        "margin_pct": _number(margin_pct),
        "revenue": revenue,
        "op_income": op_income,
        "ebitda": ebitda,
        "delta_wc": delta_wc,
        "capex": capex,
        "interest": interest,
        "fcf": fcf,
        "op_margin": margin_pct,
    }


def sensitivity_matrix(base, growth_steps=None, margins=None):
    """
    5×5 민감도 표.

    반환: {"growth": [...], "margins": [...], "cells": [[cell, ...], ...],
           "best": cell, "worst": cell, "base_cell": cell, "available": bool}
      · cells[i][j] = growth[i] × margins[j]
      · best/worst 는 FCF 기준(FCF가 없으면 영업이익 기준)
    """
    growth_steps = list(growth_steps if growth_steps is not None else SENSITIVITY_GROWTH_STEPS)
    margins = list(margins if margins is not None else
                   margin_axis(_number((base or {}).get("op_margin"))))
    cells = []
    for growth in growth_steps:
        row = [sensitivity_cell(base, growth, margin) for margin in margins]
        cells.append(row)
    flat = [cell for row in cells for cell in row if cell]
    if not flat:
        return {"growth": growth_steps, "margins": margins, "cells": cells,
                "best": None, "worst": None, "base_cell": None, "available": False}
    key = lambda cell: cell["fcf"] if cell.get("fcf") is not None else cell["op_income"]
    base_cell = next((cell for cell in flat if abs(cell["growth_pct"]) < 1e-9
                      and abs(cell["margin_pct"] - _number((base or {}).get("op_margin"))) < 0.05), None)
    if base_cell is None:
        base_cell = cells[len(cells) // 2][len(margins) // 2]
    return {
        "growth": growth_steps,
        "margins": margins,
        "cells": cells,
        "best": max(flat, key=key),
        "worst": min(flat, key=key),
        "base_cell": base_cell,
        "available": True,
    }


def sensitivity_comment(matrix):
    """민감도 표 한 줄 해석 — 가장 좋은/나쁜 칸과 현재 위치."""
    if not matrix or not matrix.get("available"):
        return "매출액이 없어 민감도 표를 계산할 수 없습니다."
    best, worst, base = matrix["best"], matrix["worst"], matrix["base_cell"]
    unit = "FCF" if best.get("fcf") is not None else "영업이익"
    return ("매출 성장 %+.0f%% · 영업이익률 %.1f%%일 때 %s가 %s로 가장 크고, "
            "성장 %+.0f%% · 이익률 %.1f%%일 때 %s가 %s로 가장 작습니다. 현재 가정(성장 %+.0f%%, "
            "이익률 %.1f%%)에서는 %s %s입니다." % (
                best["growth_pct"], best["margin_pct"], unit, _short(best),
                worst["growth_pct"], worst["margin_pct"], unit, _short(worst),
                base["growth_pct"], base["margin_pct"], unit, _short(base)))


def _short(cell):
    value = cell.get("fcf") if cell.get("fcf") is not None else cell.get("op_income")
    return "%s억원" % format(round(_number(value) / 1e8), ",")


def _line_amounts(plan, actual, key):
    """(계획, 실적) 금액 — 영업이익은 없으면 매출−원가−판관비로 계산한다."""
    def value(source, name):
        amount = (source or {}).get(name)
        if amount is None and name == "op_income":
            revenue = _number((source or {}).get("revenue"))
            cost = _number((source or {}).get("cost_of_sales")) + _number((source or {}).get("sgna"))
            return revenue - cost if revenue else None
        return None if amount is None else _number(amount)

    return value(plan, key), value(actual, key)


def variance_rows(plan, actual):
    """계획 vs 실적 4줄 — 금액·차이·증감률."""
    rows = []
    for key, label in VARIANCE_LINES:
        plan_value, actual_value = _line_amounts(plan, actual, key)
        diff = None
        pct = None
        if plan_value is not None and actual_value is not None:
            diff = actual_value - plan_value
            pct = (diff / abs(plan_value) * 100.0) if plan_value else None
        rows.append({
            "key": key, "label": label,
            "plan": plan_value, "actual": actual_value,
            "diff": diff, "diff_pct": pct,
            "favorable": None if diff is None else (
                diff >= 0 if key in ("revenue", "op_income") else diff <= 0),
        })
    return rows


def operating_variance_effects(plan, actual):
    """
    영업이익 차이를 ① 매출 효과 ② 원가율 효과로 분해한다(항등식으로 검산).

    반환: {plan_op, actual_op, total, revenue_effect, cost_rate_effect,
           cogs_rate_effect, sgna_rate_effect, plan_cost_ratio, actual_cost_ratio,
           plan_op_margin, actual_op_margin, check}
    """
    plan, actual = plan or {}, actual or {}
    revenue0 = _number(plan.get("revenue"))
    revenue1 = _number(actual.get("revenue"))
    cogs0, cogs1 = _number(plan.get("cost_of_sales")), _number(actual.get("cost_of_sales"))
    sgna0, sgna1 = _number(plan.get("sgna")), _number(actual.get("sgna"))
    cost0, cost1 = cogs0 + sgna0, cogs1 + sgna1
    op0 = revenue0 - cost0
    op1 = revenue1 - cost1
    cost_ratio0 = (cost0 / revenue0) if revenue0 else None
    cost_ratio1 = (cost1 / revenue1) if revenue1 else None

    revenue_effect = None
    cost_rate_effect = None
    cogs_effect = sgna_effect = None
    if cost_ratio0 is not None and cost_ratio1 is not None:
        revenue_effect = (revenue1 - revenue0) * (1.0 - cost_ratio0)
        cogs_effect = revenue1 * ((cogs0 / revenue0) - (cogs1 / revenue1))
        sgna_effect = revenue1 * ((sgna0 / revenue0) - (sgna1 / revenue1))
        cost_rate_effect = cogs_effect + sgna_effect
    total = op1 - op0
    check = None
    if revenue_effect is not None:
        check = (revenue_effect + cost_rate_effect) - total
    return {
        "plan_op": op0, "actual_op": op1, "total": total,
        "revenue_effect": revenue_effect, "cost_rate_effect": cost_rate_effect,
        "cogs_rate_effect": cogs_effect, "sgna_rate_effect": sgna_effect,
        "plan_cost_ratio": cost_ratio0, "actual_cost_ratio": cost_ratio1,
        "plan_op_margin": (op0 / revenue0 * 100.0) if revenue0 else None,
        "actual_op_margin": (op1 / revenue1 * 100.0) if revenue1 else None,
        "check": check,
    }


def variance_comment(effects):
    """차이 분해 한 줄 해석."""
    if not effects or effects.get("revenue_effect") is None:
        return "매출액이 0이거나 비어 있어 차이를 분해할 수 없습니다."
    total = effects["total"]
    direction = "늘었습니다" if total >= 0 else "줄었습니다"
    parts = []
    if abs(effects["revenue_effect"]) >= 1:
        parts.append("매출 증감 효과 %s%s" % (_won(effects["revenue_effect"]),
                                            " (플러스)" if effects["revenue_effect"] >= 0 else " (마이너스)"))
    if abs(effects["cost_rate_effect"]) >= 1:
        parts.append("원가율 변동 효과 %s" % _won(effects["cost_rate_effect"]))
    detail = " — " + ", ".join(parts) if parts else ""
    margin0, margin1 = effects.get("plan_op_margin"), effects.get("actual_op_margin")
    margin_text = ""
    if margin0 is not None and margin1 is not None:
        margin_text = " 영업이익률은 %.1f%% → %.1f%%(%+.1f%%p)입니다." % (
            margin0, margin1, margin1 - margin0)
    return "영업이익은 계획 대비 %s %s%s.%s" % (_won(total), direction, detail, margin_text)


def _won(value):
    value = _number(value)
    eok = value / 1e8
    if abs(eok) >= 1:
        return "%s억원" % format(round(eok, 1), ",")
    return "%s원" % format(round(value), ",")


def _merge_thousands(cells):
    """
    '1,000,1,100' 처럼 따옴표 없이 천단위 쉼표를 쓴 경우를 두 숫자로 되돌린다.

    왼쪽부터 '3자리 묶음'을 만족하는 조각을 합쳐 숫자 두 개로 줄인다.
    ('1','000','1','100' → '1,000' / '1,100')
    """
    parts = [cell for cell in cells if cell != ""]
    while len(parts) > 2:
        merged = "%s,%s" % (parts[0], parts[1])
        if len(parts[1]) == 3 and "," not in parts[0] and "." not in parts[1]:
            parts = [merged] + parts[2:]
        else:
            break
    return parts


def parse_variance_csv(text, unit_factor=1e8):
    """
    CSV 텍스트 → {"plan": {...}, "actual": {...}}.

    형식(헤더 필수): 항목,계획,실적
        매출액,1000,1100
        매출원가,700,760
        판관비,200,215
    값 단위는 unit_factor(기본 1e8 = 억원)로 곱해 원으로 바꾼다.
    읽지 못한 행은 건너뛰고, 세 항목(매출액·매출원가·판관비)이 모두 있어야 성공한다.
    """
    if not text:
        return None
    labels = {"매출액": "revenue", "매출": "revenue", "매출원가": "cost_of_sales",
              "판관비": "sgna", "판매비와관리비": "sgna", "판매관리비": "sgna"}
    plan, actual = {}, {}
    for index, raw in enumerate(text.splitlines()):
        line = raw.strip().strip('"')
        if not line:
            continue
        cells = [cell.strip().strip('"') for cell in line.split(",")]
        if len(cells) < 3:
            continue
        key = labels.get(cells[0].replace(" ", ""))
        if key is None:
            continue                                 # 헤더·모르는 항목은 건너뛴다
        amounts = _merge_thousands(cells[1:])
        if len(amounts) < 2:
            continue
        for target, cell in ((plan, amounts[0]), (actual, amounts[-1])):
            cleaned = cell.replace(",", "").replace("원", "").strip()
            try:
                target[key] = float(cleaned) * unit_factor
            except ValueError:
                continue
    if not all(key in plan and key in actual for key in ("revenue", "cost_of_sales", "sgna")):
        return None
    return {"plan": plan, "actual": actual}


def sample_budget(unit_factor=1e8):
    """체험용 샘플(계획·실적) — 원 단위로 변환해 돌려준다."""
    return {
        "plan": {key: value * unit_factor for key, value in SAMPLE_PLAN.items()},
        "actual": {key: value * unit_factor for key, value in SAMPLE_ACTUAL.items()},
    }
