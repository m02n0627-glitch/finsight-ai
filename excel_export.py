# -*- coding: utf-8 -*-
"""
FinSight 엑셀(.xlsx) 리포트 생성기
=================================

analyzer.build_analysis() 의 결과(analysis dict) 하나만 받아서, 실무에서 바로 열어볼 수 있는
엑셀 통합 문서(bytes)를 만든다. 네트워크·API 호출은 하지 않는다(이미 계산된 값만 서식화한다).

시트 구성
    1) 요약 및 리스크      기업명·기준 연도·리스크 총점/등급(자본잠식 표시) · 5대 리스크 항목 · 공시 위험 요약
    2) 재무제표 및 비율    연도별 핵심 재무제표(단위: 억원, 천 단위 쉼표) + 7대 재무비율(자본잠식 표기) + 원문 링크
    3) DuPont 및 CCC      DuPont ROE 3요인 분해 · 순차 대체법 격차(2개사) · DSO/DIO/DPO/CCC
    4) 동종업계 및 공시    동종업계 피어 비교표 + 최근 1년 공시 위험 목록과 DART 원문 URL

서식
    헤더는 진한 배경 + 흰 굵은 글씨, 항목 헤더는 연한 회색 + 굵은 글씨.
    금액 '#,##0', 비율 '#,##0.0"%"', 배수 '0.00"배"', 일수 '#,##0"일"'.
    열 너비는 내용(한글 2칸 폭)에 맞춰 자동으로 잡는다.

사용
    from excel_export import build_excel_report, excel_filename
    payload = build_excel_report(analysis)      # bytes
    name = excel_filename(analysis)             # FinSight_삼성전자_2024.xlsx
"""

import unicodedata
from io import BytesIO

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.properties import PageSetupProperties

import analyzer as fs

# ---------------------------------------------------------------------------
# 0. 시트 이름 · 숫자 서식 · 스타일
# ---------------------------------------------------------------------------

SHEET_SUMMARY = "요약 및 리스크"
SHEET_FINANCIALS = "재무제표 및 비율"
SHEET_DUPONT = "DuPont 및 CCC"
SHEET_MARKET = "동종업계 및 공시"

AMOUNT_FMT = '#,##0;[Red]-#,##0'          # 억원
PCT_FMT = '#,##0.0"%"'                    # 비율 (%) — 값은 이미 % 단위
PCT2_FMT = '#,##0.00"%"'
GAP_FMT = '+#,##0.00"%p";-#,##0.00"%p";0.00"%p"'
TURN_FMT = '0.00"회"'
MULT_FMT = '0.00"배"'
DAY_FMT = '#,##0"일"'
SCORE_FMT = '0'

TITLE_FILL = PatternFill("solid", fgColor="111827")      # 딥블랙
SECTION_FILL = PatternFill("solid", fgColor="EEF2FF")    # 인디고 50
HEADER_FILL = PatternFill("solid", fgColor="F3F4F6")
HEADER_BORDER = Border(bottom=Side(style="thin", color="D1D5DB"))

TITLE_FONT = Font(bold=True, size=13, color="FFFFFF")
SECTION_FONT = Font(bold=True, size=11, color="111827")
HEADER_FONT = Font(bold=True, size=10, color="374151")
BODY_FONT = Font(size=10, color="111827")
BOLD_FONT = Font(size=10, bold=True, color="111827")
NOTE_FONT = Font(size=9, italic=True, color="6B7280")
DASH_FONT = Font(size=10, color="9CA3AF")
IMPAIRED_FONT = Font(size=10, bold=True, color="B91C1C")
LINK_FONT = Font(size=10, color="4F46E5", underline="single")

GRADE_LABEL = {"safe": "안전", "caution": "주의", "danger": "위험", "excluded": "판정 제외"}
SEVERITY_LABEL = {"severe": "심각", "caution": "주의"}

# 핵심 재무제표(2번 시트) — 화면의 CORE_ROWS 와 같은 순서
CORE_ROWS = [
    ("revenue", "매출액"),
    ("op_income", "영업이익"),
    ("net_income", "당기순이익"),
    ("assets", "자산총계"),
    ("liabilities", "부채총계"),
    ("equity", "자본총계"),
]

RATIO_ROWS = [(key, label) for key, label, _unit, _group in fs.RATIO_LABELS]

DUPONT_ROWS = [
    ("net_margin", "순이익률 (%)", PCT_FMT),
    ("asset_turnover", "총자산회전율 (회)", TURN_FMT),
    ("equity_multiplier", "재무레버리지 (배)", MULT_FMT),
    ("roe", "DuPont ROE (%)", PCT_FMT),
    ("roe_simple", "단순 ROE (%) — 검산", PCT_FMT),
    ("diff", "차이 (%p)", GAP_FMT),
]

CCC_ROWS = [
    ("dso", "DSO — 매출채권 회수 (일)"),
    ("dio", "DIO — 재고 보유 (일)"),
    ("dpo", "DPO — 매입채무 지급 (일)"),
    ("ccc", "CCC — 현금전환주기 (일)"),
]


# ---------------------------------------------------------------------------
# 1. 값 도우미
# ---------------------------------------------------------------------------

def won_to_eok(value):
    """원 → 억원 (표 단위). 값이 없으면 None."""
    return None if value is None else round(value / 1e8)


def display_name(company):
    """'삼성전자(주)' → '삼성전자' (엔진과 같은 규칙)."""
    return fs.display_name((company or {}).get("name"))


def years_of(analysis):
    years = list((analysis or {}).get("years") or [])
    if not years:
        for company in (analysis or {}).get("companies") or []:
            years.extend((company.get("years") or {}).keys())
    return sorted(set(years))


def column_titles(analysis, years):
    """표의 열 이름 — 한 기업이면 '2024', 두 기업이면 '삼성전자 2024'."""
    companies = (analysis or {}).get("companies") or []
    titles = []
    for company in companies:
        for year in years:
            titles.append("%s %d" % (display_name(company), year) if len(companies) > 1 else "%d" % year)
    return titles


def fs_label(company, year):
    """연결재무제표 / 별도재무제표 표기."""
    entry = ((company or {}).get("years") or {}).get(year) or {}
    fs_div = entry.get("fs_div") or (company or {}).get("fs_div")
    if fs_div == "CFS":
        return "연결재무제표"
    if fs_div == "OFS":
        return "별도재무제표"
    return "-"


def capital_impaired(company, year):
    return bool(fs.capital_impaired_info(company, year).get("impaired"))


def impaired_text(company, year):
    """'자본잠식 · 자본총계 -4,402억원' (아니면 '')."""
    return fs.capital_impaired_info(company, year).get("text") or ""


def disclosure_summary(company):
    """공시 위험 한 줄 요약 — (굵게 보여 줄 값, 설명)."""
    data = (company or {}).get("disclosure") or {}
    status = data.get("status")
    matches = data.get("matches") or []
    if status == "ok":
        severe = sum(1 for item in matches if item.get("severity") == "severe")
        caution = len(matches) - severe
        return "%d건" % len(matches), "심각 %d건 · 주의 %d건" % (severe, caution)
    if status == "clear":
        return "0건", "최근 1년 위험 키워드 공시 없음"
    if status == "error":
        return "조회 실패", (data.get("error") or "공시검색 API 호출 실패")[:80]
    return "-", ""


# ---------------------------------------------------------------------------
# 2. 시트 작성 도우미 (커서)
# ---------------------------------------------------------------------------

def text_width(text):
    """한글·한자 등 넓은 문자는 2칸으로 세는 표시 폭."""
    width = 0
    for char in str(text):
        width += 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
    return width


class SheetWriter:
    """행을 하나씩 내려가며 제목·구역·표를 그리는 작은 도우미."""

    def __init__(self, ws, span):
        self.ws = ws
        self.span = max(1, int(span))
        self.row = 1
        self.merges = []          # (min_row, min_col, max_row, max_col) — 열 너비 계산에서 제외

    # --- 낱개 셀 ---------------------------------------------------------
    def _cell(self, row, col, value, font=None, fill=None, fmt=None, align=None, border=None):
        cell = self.ws.cell(row=row, column=col)
        cell.value = value
        if font is not None:
            cell.font = font
        if fill is not None:
            cell.fill = fill
        if fmt:
            cell.number_format = fmt
        cell.alignment = align or Alignment(vertical="center")
        if border is not None:
            cell.border = border
        return cell

    def _fill_row(self, row, fill):
        for col in range(1, self.span + 1):
            self.ws.cell(row=row, column=col).fill = fill

    def _merge(self, row, first=1, last=None):
        last = last or self.span
        if last > first:
            self.ws.merge_cells(start_row=row, start_column=first, end_row=row, end_column=last)
            self.merges.append((row, first, row, last))

    # --- 블록 ------------------------------------------------------------
    def title(self, text):
        row = self.row
        self._fill_row(row, TITLE_FILL)
        self._cell(row, 1, text, font=TITLE_FONT,
                   align=Alignment(horizontal="left", vertical="center"))
        self._merge(row)
        self.ws.row_dimensions[row].height = 26
        self.row += 1
        return row

    def section(self, text):
        row = self.row
        self._fill_row(row, SECTION_FILL)
        self._cell(row, 1, text, font=SECTION_FONT,
                   align=Alignment(horizontal="left", vertical="center"))
        self._merge(row)
        self.ws.row_dimensions[row].height = 21
        self.row += 1
        return row

    def header(self, values):
        row = self.row
        for index, value in enumerate(values, start=1):
            self._cell(row, index, value, font=HEADER_FONT, fill=HEADER_FILL,
                       align=Alignment(horizontal="left" if index == 1 else "center",
                                       vertical="center", wrap_text=True),
                       border=HEADER_BORDER)
        self.ws.row_dimensions[row].height = 19
        self.row += 1
        return row

    def row_values(self, values, formats=None, bold_first=False):
        """값 한 줄. None 은 흐린 '-' , 숫자는 오른쪽 정렬 + 서식."""
        row = self.row
        for index, value in enumerate(values, start=1):
            fmt = (formats[index - 1] if formats else None)
            font = BOLD_FONT if (index == 1 and bold_first) else BODY_FONT
            if value is None:
                self._cell(row, index, "-", font=DASH_FONT,
                           align=Alignment(horizontal="center", vertical="center"))
            elif isinstance(value, str):
                self._cell(row, index, value, font=font, fmt=fmt,
                           align=Alignment(horizontal="left" if index == 1 else "center",
                                           vertical="center", wrap_text=True))
            else:
                self._cell(row, index, value, font=font, fmt=fmt,
                           align=Alignment(horizontal="right", vertical="center"))
        self.row += 1
        return row

    def link(self, row, col, text, url):
        cell = self._cell(row, col, text, font=LINK_FONT,
                          align=Alignment(horizontal="left", vertical="center"))
        if url:
            cell.hyperlink = url
        return cell

    def note(self, text):
        if not text:
            return None
        row = self.row
        self._cell(row, 1, text, font=NOTE_FONT,
                   align=Alignment(horizontal="left", vertical="top", wrap_text=True))
        self._merge(row)
        lines = max(1, int((text_width(text) - 1) // max(20, self.span * 12)) + 1)
        self.ws.row_dimensions[row].height = 13.5 * lines
        self.row += 1
        return row

    def blank(self, count=1):
        self.row += count


# ---------------------------------------------------------------------------
# 3. 시트 1 — 요약 및 리스크
# ---------------------------------------------------------------------------

def _sheet_summary(wb, analysis):
    ws = wb.active
    ws.title = SHEET_SUMMARY
    years = years_of(analysis)
    latest = years[-1] if years else None
    companies = analysis.get("companies") or []
    writer = SheetWriter(ws, 7)

    writer.title("FinSight 재무·리스크 리포트 — 요약 및 리스크")
    writer.note("데이터: 금융감독원 OpenDART 사업보고서 · 계산: 결정론적 Python 규칙 · 투자 권유가 아닙니다.")
    if latest:
        basis = "기준 연도: %d년 (최신 사업연도)" % latest
        if len(years) > 1:
            basis += " · 분석 기간 %d–%d년" % (years[0], years[-1])
        writer.note(basis)
    writer.blank()

    for company in companies:
        name = display_name(company)
        risk = company.get("risk") or {}
        impaired = bool(risk.get("capital_impaired")) or (
            latest is not None and capital_impaired(company, latest))
        grade = risk.get("grade") or ""
        grade_label = GRADE_LABEL.get(grade, "-")
        grade_text = ("자본잠식 · " + grade_label) if impaired else grade_label
        d_value, d_note = disclosure_summary(company)

        writer.section("%s (%s) · %s년" % (
            name, (company.get("stock_code") or "-"), latest if latest else "-"))

        writer.header(["항목", "값"])
        summary_rows = [
            ("기업명", name),
            ("종목코드 · 고유번호", "%s · %s" % (company.get("stock_code") or "-",
                                                company.get("corp_code") or "-")),
            ("기준 연도", latest),
            ("재무제표", fs_label(company, latest) if latest else "-"),
            ("리스크 총점", ("%s점 / 100점" % risk["score"]) if risk.get("score") is not None else None),
            ("리스크 등급", grade_text),
            ("자본잠식 여부", impaired_text(company, latest) if (impaired and latest) else "해당 없음"),
            ("최근 1년 공시 위험", ("%s (%s)" % (d_value, d_note)) if d_note else d_value),
        ]
        for label, value in summary_rows:
            row = writer.row_values([label, value], bold_first=True)
            if label == "자본잠식 여부" and impaired:
                ws.cell(row=row, column=2).font = IMPAIRED_FONT
            if label == "리스크 등급" and impaired:
                ws.cell(row=row, column=2).font = IMPAIRED_FONT

        if risk.get("comment"):
            writer.note("한 줄 진단: " + str(risk["comment"]).replace("🟢 ", "").replace("🟡 ", "").replace("🔴 ", ""))

        # 감가상각비 출처 1줄 — API 현금흐름표 / 주석 원문 파싱 / 미공시(보수 계산)
        try:
            baseline = fs.fpa_baseline(company, latest) if latest else None
            if baseline:
                writer.note(fs.depreciation_source_text(baseline))
        except Exception:
            pass

        writer.blank()
        writer.section("5대 재무 리스크 항목")
        writer.header(["항목", "측정값", "점수", "만점", "판정", "판정 기준", "메모"])
        items = risk.get("items") or []
        if items:
            for item in items:
                writer.row_values([
                    item.get("label") or "-",
                    item.get("value_text") or "-",
                    item.get("score"),
                    item.get("max"),
                    GRADE_LABEL.get(item.get("state"), "N/A"),
                    item.get("criteria") or "-",
                    item.get("note") or "-",
                ], formats=[None, None, SCORE_FMT, SCORE_FMT, None, None, None], bold_first=True)
        else:
            writer.row_values(["리스크 진단 결과가 없습니다."] + [None] * 6)

        writer.blank()
        writer.section("최근 1년 공시 위험 요약")
        disclosure = company.get("disclosure") or {}
        writer.header(["항목", "내용"])
        writer.row_values(["요약", ("%s · %s" % (d_value, d_note)) if d_note else d_value], bold_first=True)
        writer.row_values(["조회 범위", fs.disclosure_coverage(disclosure) or "-"], bold_first=True)
        if disclosure.get("period"):
            writer.row_values(["조회 기간", disclosure["period"]], bold_first=True)
        if disclosure.get("truncated"):
            writer.row_values(["주의", "페이지 상한(3,000건)에 걸려 1년치 전체를 보지 못했습니다."], bold_first=True)
        if disclosure.get("status") == "error" and disclosure.get("error"):
            writer.row_values(["오류", disclosure["error"]], bold_first=True)
        writer.blank(2)


# ---------------------------------------------------------------------------
# 4. 시트 2 — 재무제표 및 비율
# ---------------------------------------------------------------------------

def _sheet_financials(wb, analysis):
    ws = wb.create_sheet(SHEET_FINANCIALS)
    companies = analysis.get("companies") or []
    years = years_of(analysis)
    span = max(6, 1 + len(companies) * len(years))
    writer = SheetWriter(ws, span)
    titles = column_titles(analysis, years)

    writer.title("재무제표 및 재무비율")
    writer.note("단위: 억원 · 천 단위 쉼표 · 연결재무제표 우선(없으면 별도) · 사업보고서 기준")
    writer.blank()

    # --- 핵심 재무제표 ---------------------------------------------------
    writer.section("핵심 재무제표 (단위: 억원)")
    writer.header(["계정"] + titles)
    for key, label in CORE_ROWS:
        values = []
        for company in companies:
            for year in years:
                item = ((company.get("years") or {}).get(year) or {}).get("accounts", {}).get(key)
                values.append(won_to_eok(item.get("amount")) if item else None)
        writer.row_values([label] + values, formats=[None] + [AMOUNT_FMT] * len(values), bold_first=True)
    writer.blank()

    # --- 7대 재무비율 ----------------------------------------------------
    writer.section("7대 재무비율")
    writer.header(["비율"] + titles)
    impaired_pairs = []
    for key, label in RATIO_ROWS:
        row = writer.row
        writer._cell(row, 1, label, font=BOLD_FONT, align=Alignment(vertical="center"))
        col = 2
        for company in companies:
            for year in years:
                impaired = capital_impaired(company, year)
                value = (((company.get("years") or {}).get(year) or {}).get("ratios") or {}).get(key)
                if impaired and key in ("roe", "debt_ratio"):
                    writer._cell(row, col, "자본잠식", font=IMPAIRED_FONT,
                                 align=Alignment(horizontal="center", vertical="center"))
                    impaired_pairs.append("%s %d" % (display_name(company), year))
                elif value is None:
                    writer._cell(row, col, "-", font=DASH_FONT,
                                 align=Alignment(horizontal="center", vertical="center"))
                else:
                    writer._cell(row, col, value, font=BODY_FONT, fmt=PCT_FMT,
                                 align=Alignment(horizontal="right", vertical="center"))
                col += 1
        writer.row += 1
    writer.note("단위 % · 성장률은 전년 대비 · ROE는 기말 자본 기준")
    if impaired_pairs:
        writer.note("자본잠식(자본총계 0 이하): %s — 부채비율·ROE는 음수 자본으로 왜곡되어 '자본잠식'으로 표기했습니다."
                    % ", ".join(sorted(set(impaired_pairs))))
    writer.blank()

    # --- 사업보고서 원문 -------------------------------------------------
    writer.section("사업보고서 원문")
    writer.header(["기업", "사업연도", "재무제표", "접수번호", "공시일", "DART 원문"])
    for company in companies:
        for year in years:
            entry = ((company.get("years") or {}).get(year) or {})
            rcept_no = entry.get("rcept_no") or ""
            row = writer.row
            writer.row_values([
                display_name(company), year, fs_label(company, year),
                rcept_no or "-", fs.rcept_date(rcept_no) or "-", None,
            ], formats=[None, SCORE_FMT, None, None, None, None], bold_first=True)
            if rcept_no:
                writer.link(row, 6, fs.dart_link(rcept_no), fs.dart_link(rcept_no))
    writer.blank()
    writer.note("숫자를 클릭하면 원문이 열리도록 만든 화면(웹)과 달리, 엑셀에는 원문 URL을 표로 함께 둡니다.")


# ---------------------------------------------------------------------------
# 5. 시트 3 — DuPont 및 CCC
# ---------------------------------------------------------------------------

def _sheet_dupont(wb, analysis):
    ws = wb.create_sheet(SHEET_DUPONT)
    companies = analysis.get("companies") or []
    years = years_of(analysis)
    latest = years[-1] if years else None
    span = max(5, 1 + len(companies) * len(years))
    writer = SheetWriter(ws, span)
    titles = column_titles(analysis, years)

    writer.title("DuPont ROE 분해 및 현금전환주기(CCC)")
    writer.note("ROE = 순이익률 × 총자산회전율 × 재무레버리지 (총자산회전율은 기말 자산 기준)")
    writer.blank()

    # --- DuPont --------------------------------------------------------
    writer.section("DuPont ROE 3요인 분해")
    writer.header(["항목"] + titles)
    for key, label, fmt in DUPONT_ROWS:
        row = writer.row
        writer._cell(row, 1, label, font=BOLD_FONT, align=Alignment(vertical="center"))
        col = 2
        for company in companies:
            for year in years:
                impaired = capital_impaired(company, year)
                value = (((company.get("years") or {}).get(year) or {}).get("dupont") or {}).get(key)
                if impaired and key in ("equity_multiplier", "roe", "roe_simple", "diff"):
                    writer._cell(row, col, "자본잠식", font=IMPAIRED_FONT,
                                 align=Alignment(horizontal="center", vertical="center"))
                elif value is None:
                    writer._cell(row, col, "-", font=DASH_FONT,
                                 align=Alignment(horizontal="center", vertical="center"))
                else:
                    writer._cell(row, col, value, font=BODY_FONT, fmt=fmt,
                                 align=Alignment(horizontal="right", vertical="center"))
                col += 1
        writer.row += 1
    for company in companies:
        if company.get("dupont_comment"):
            writer.note("%s · %s" % (display_name(company), company["dupont_comment"]))
    writer.blank()

    # --- 순차 대체법 격차 (2개사) ----------------------------------------
    gap = analysis.get("dupont_gap")
    if gap:
        writer.section("ROE 격차 순차 대체법 분해 — %s년 · %s → %s" % (latest, gap.get("a"), gap.get("b")))
        writer.header(["요인", gap.get("a"), gap.get("b"), "변화 (B−A)", "ROE 기여도 (%p)"])
        factor_fmt = {"profitability": PCT_FMT, "efficiency": TURN_FMT, "leverage": MULT_FMT}
        for item in gap.get("rows") or []:
            fmt = factor_fmt.get(item.get("key"))
            writer.row_values([
                item.get("label"), item.get("a"), item.get("b"), item.get("delta"), item.get("contribution"),
            ], formats=[None, fmt, fmt, fmt, GAP_FMT], bold_first=True)
        writer.row_values([
            "합계 — ROE 격차",
            gap.get("roe_a", gap.get("a_roe")),
            gap.get("roe_b", gap.get("b_roe")),
            gap.get("total"),
            gap.get("total"),
        ], formats=[None, PCT_FMT, PCT_FMT, GAP_FMT, GAP_FMT], bold_first=True)
        writer.note("각 요인을 A의 값에서 B의 값으로 하나씩 바꾸며 계산했습니다(수익성 → 자산효율성 → 레버리지 순). 세 기여도의 합이 ROE 격차와 같습니다.")
        if gap.get("text"):
            writer.note(gap["text"])
        writer.blank()

    # --- CCC -----------------------------------------------------------
    writer.section("운전자본 회전일수 · 현금전환주기(CCC)")
    writer.header(["항목"] + titles)
    for key, label in CCC_ROWS:
        values = []
        for company in companies:
            for year in years:
                values.append((((company.get("years") or {}).get(year) or {}).get("ccc") or {}).get(key))
        writer.row_values([label] + values, formats=[None] + [DAY_FMT] * len(values), bold_first=True)
    writer.note("DSO + DIO − DPO = CCC · 기말잔액 · 365일 기준 · 짧을수록 운전자본 부담이 작습니다")
    for company in companies:
        current = ((company.get("years") or {}).get(latest) or {}).get("ccc") or {}
        if current.get("ccc") is None:
            missing = [label for key, label in fs.CCC_INPUTS
                       if (current.get("inputs") or {}).get(key) is None]
            writer.note("%s · 계산 불가 — 미공시 계정: %s" % (
                display_name(company), ", ".join(missing) if missing else "관련 계정 부족"))
        elif company.get("ccc_comment"):
            writer.note("%s · %s" % (display_name(company), company["ccc_comment"]))
    gap_ccc = analysis.get("ccc_gap")
    if gap_ccc and gap_ccc.get("text"):
        writer.note("CCC 비교: " + gap_ccc["text"])
    writer.blank()
    writer.note("자본잠식(자본총계 0 이하)인 해는 재무레버리지·ROE가 음수 자본으로 왜곡되어 계산하지 않습니다.")


# ---------------------------------------------------------------------------
# 6. 시트 4 — 동종업계 및 공시
# ---------------------------------------------------------------------------

def _sheet_market(wb, analysis):
    ws = wb.create_sheet(SHEET_MARKET)
    companies = analysis.get("companies") or []
    writer = SheetWriter(ws, 7)

    writer.title("동종업계 비교 및 최근 1년 공시 위험")
    writer.blank()

    # --- 동종업계 -------------------------------------------------------
    peer = analysis.get("industry_peers") or {}
    writer.section("동종업계 피어 비교")
    if peer.get("status") == "ok":
        basis = ["업종코드 %s" % (peer.get("induty_code") or "-")]
        if peer.get("sector") or peer.get("industry_name"):
            basis.append(peer.get("sector") or peer.get("industry_name"))
        if peer.get("fs_nm"):
            basis.append(peer["fs_nm"])
        writer.note("%s · %d년 · 피어 %d개사 (평균·중앙값은 값이 있는 피어만으로 계산)"
                    % (" · ".join(basis), peer.get("year") or "-", len(peer.get("peers") or [])))

        header = ["기업 (%s년)" % (peer.get("year") or "-")]
        formats = [None]
        for _key, label, unit, _better, digits in fs.PEER_METRICS:
            header.append(label + (" (%)" if unit == "%" else " (일)"))
            formats.append(PCT_FMT if unit == "%" else DAY_FMT)
        header.append("비고")
        formats.append(None)
        writer.header(header)

        def metric_values(values):
            out = []
            for key, _label, _unit, _better, _digits in fs.PEER_METRICS:
                out.append((values or {}).get(key))
            return out

        ranks = []
        for item in peer.get("rows") or []:
            if item.get("rank") is not None and item.get("total"):
                ranks.append("%s %d위/%d개사" % (item["label"], item["rank"], item["total"]))
        writer.row_values(["%s (대상)" % (peer.get("name") or "-")] + metric_values(peer.get("target_values"))
                          + [" · ".join(ranks) if ranks else "-"],
                          formats=formats, bold_first=True)
        average = {item["key"]: item.get("mean") for item in peer.get("rows") or []}
        median = {item["key"]: item.get("median") for item in peer.get("rows") or []}
        count = len(peer.get("peers") or [])
        writer.row_values(["피어 평균"] + metric_values(average) + ["%d개사" % count], formats=formats)
        writer.row_values(["피어 중앙값"] + metric_values(median) + ["%d개사" % count], formats=formats)
        for item in peer.get("peers") or []:
            writer.row_values([item.get("name") or "-"] + metric_values(item.get("metrics"))
                              + [item.get("match_label") or "-"], formats=formats)
        if peer.get("comment"):
            writer.note(peer["comment"])
        if peer.get("skipped"):
            writer.note("재무제표를 받지 못해 표에서 제외한 피어: %s" % ", ".join(peer["skipped"]))
    else:
        reason = peer.get("reason") or "동종업계 비교는 한 기업만 분석했을 때 제공됩니다."
        writer.note("동종업계 비교 없음 — %s" % reason)
    writer.blank(2)

    # --- 공시 위험 ------------------------------------------------------
    writer.section("최근 1년 공시 위험 목록")
    for company in companies:
        data = company.get("disclosure") or {}
        writer.note("%s · %s · %s" % (
            display_name(company),
            fs.disclosure_coverage(data) or "조회 범위 정보 없음",
            disclosure_summary(company)[1] or "-"))
        matches = data.get("matches") or []
        if matches:
            writer.header(["기업", "공시일", "심각도", "위험 유형", "공시명", "정정", "DART 원문"])
            for item in matches:
                row = writer.row
                labels = ", ".join(item.get("labels") or []) or "-"
                if item.get("subsidiary"):
                    labels += " (종속회사)"
                writer.row_values([
                    display_name(company),
                    item.get("rcept_dt") or "-",
                    SEVERITY_LABEL.get(item.get("severity"), "-"),
                    labels,
                    item.get("report_nm") or "-",
                    "정정" if item.get("correction") else "-",
                    None,
                ], bold_first=True)
                url = item.get("url") or fs.dart_link(item.get("rcept_no") or "")
                if url:
                    writer.link(row, 7, url, url)
        else:
            status = data.get("status")
            if status == "error":
                writer.note("공시 위험 조회 실패 — %s (재무 분석은 그대로 진행되었습니다.)"
                            % ((data.get("error") or "")[:120] or "공시검색 API 호출 실패"))
            else:
                writer.note("최근 1년 위험 키워드 공시가 없습니다.")
        writer.blank()
    writer.note("공시 위험은 제목의 위험 키워드만 본 참고 정보이며 리스크 점수에는 반영하지 않습니다.")


# ---------------------------------------------------------------------------
# 7. 열 너비 · 인쇄 설정
# ---------------------------------------------------------------------------

def _autofit(ws, min_width=9, max_width=44, caps=None):
    """내용(한글 2칸 폭)에 맞춰 열 너비를 잡는다. 병합 셀은 너비 계산에서 뺀다.

    caps: {열 번호: 최대 너비} — 판정 기준·메모처럼 긴 문장 열은 좁게 두고 줄바꿈으로 보여 준다.
    """
    merged = [(rng.min_row, rng.min_col, rng.max_row, rng.max_col) for rng in ws.merged_cells.ranges]
    widths = {}
    for row in ws.iter_rows():
        for cell in row:
            if cell.value is None:
                continue
            if any(min_row <= cell.row <= max_row and min_col <= cell.column <= max_col
                   for min_row, min_col, max_row, max_col in merged):
                continue
            width = text_width(cell.value)
            widths[cell.column] = max(widths.get(cell.column, 0), width)
    caps = caps or {}
    for column, width in widths.items():
        limit = caps.get(column, max_width)
        ws.column_dimensions[get_column_letter(column)].width = min(limit, max(min_width, width + 3))


def _page_setup(ws):
    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr = PageSetupProperties(fitToPage=True)
    ws.print_options.horizontalCentered = False


# ---------------------------------------------------------------------------
# 8. 공개 함수
# ---------------------------------------------------------------------------

def build_excel_report(analysis):
    """
    분석 결과(dict) → 엑셀 통합 문서 bytes.

    analysis 는 analyzer.build_analysis() 의 반환값과 같은 모양이면 되고,
    단일 기업·2개사 비교 모두 지원한다. 계산 불가 값은 '-' , 자본잠식은 '자본잠식'으로 표기한다.
    """
    if not isinstance(analysis, dict) or not (analysis.get("companies") or []):
        raise ValueError("분석 결과(analysis)가 비어 있어 엑셀 리포트를 만들 수 없습니다.")

    wb = Workbook()
    wb.properties.creator = "FinSight"
    wb.properties.title = "FinSight 재무·리스크 리포트"

    _sheet_summary(wb, analysis)
    _sheet_financials(wb, analysis)
    _sheet_dupont(wb, analysis)
    _sheet_market(wb, analysis)

    for ws in wb.worksheets:
        caps = {6: 34, 7: 34} if ws.title == SHEET_SUMMARY else None
        _autofit(ws, caps=caps)
        _page_setup(ws)

    stream = BytesIO()
    wb.save(stream)
    return stream.getvalue()


def excel_filename(analysis):
    """'FinSight_삼성전자_2024.xlsx' (두 기업이면 'FinSight_삼성전자_vs_SK하이닉스_2024.xlsx')."""
    companies = (analysis or {}).get("companies") or []
    years = years_of(analysis)
    names = [display_name(company) for company in companies] or ["report"]
    stem = "_vs_".join(names)
    year = str(years[-1]) if years else ""
    filename = "FinSight_%s_%s.xlsx" % (stem, year) if year else "FinSight_%s.xlsx" % stem
    return _safe_filename(filename)


def _safe_filename(name):
    """파일 이름에 쓸 수 없는 문자(\\ / : * ? " < > |)만 밑줄로 바꾼다."""
    for char in '\\/:*?"<>|':
        name = name.replace(char, "_")
    return " ".join(name.split()).strip() or "FinSight_report.xlsx"
