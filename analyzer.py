#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OpenDART 재무제표 분석 프로그램 (analyzer.py)
=============================================

OpenDART(금융감독원 전자공시시스템) 오픈 API로 상장사의 사업보고서 재무제표를 받아
주요 계정과 재무비율을 계산하고, 두 회사를 나란히 비교하는 HTML 대시보드를 만든다.

  * 외부 패키지 설치 없이 파이썬 표준 라이브러리만 사용한다. (Python 3.8 이상)
  * 회사 이름만 넣으면 corpCode.xml(전체 기업 고유번호)을 내려받아 자동으로 찾아준다.
  * 연결재무제표(CFS)를 우선 사용하고, 없으면 별도재무제표(OFS)로 대체한다.

사용 예시
---------
    # 1) API 키를 환경변수로 두고 실행 (권장)
    export DART_API_KEY="발급받은_40자리_키"
    python3 analyzer.py 삼성전자 SK하이닉스

    # 2) 키를 인자로 직접 넘기기
    python3 analyzer.py 삼성전자 SK하이닉스 --api-key 발급받은키

    # 3) 연도를 직접 지정하기
    python3 analyzer.py 삼성전자 SK하이닉스 --years 2022 2023 2024

결과물
------
    * 콘솔에 회사별·연도별 요약표와 핵심 비율 출력
    * report_삼성전자_vs_SK하이닉스.html  (Chart.js 차트 + 비교표, 파일 하나로 동작)

API 키 발급: https://opendart.fss.or.kr  →  인증키 신청/관리
"""

import argparse
import io
import json
import os
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timedelta
from xml.etree import ElementTree

# ---------------------------------------------------------------------------
# 0. 기본 설정값
# ---------------------------------------------------------------------------

API_BASE = "https://opendart.fss.or.kr/api"

# 사업보고서 = 11011 (1분기 11013 / 반기 11012 / 3분기 11014)
REPORT_ANNUAL = "11011"

# corpCode.xml 캐시 파일 (받아둔 기업 목록을 재사용해 매번 3.6MB를 받지 않게 한다)
CORP_CACHE_FILE = "corp_codes.json"
CORP_CACHE_DAYS = 7  # 캐시 유효기간(일). 지나면 자동으로 다시 받는다.

# API 응답을 기다리는 최대 시간(초)
HTTP_TIMEOUT = 30


# ---------------------------------------------------------------------------
# 1. OpenDART API 호출 도우미
# ---------------------------------------------------------------------------

def http_get(url, params):
    """URL에 파라미터를 붙여 GET 요청을 보내고 바이트(bytes)를 돌려준다."""
    query = urllib.parse.urlencode(params)
    full_url = url + "?" + query
    request = urllib.request.Request(
        full_url,
        headers={"User-Agent": "Mozilla/5.0 (dart-analyzer; educational project)"},
    )
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
        return response.read()


def call_json(endpoint, params):
    """
    JSON을 돌려주는 OpenDART API를 호출한다.

    OpenDART는 오류일 때도 HTTP 200으로 응답하고 JSON 안의 "status" 값으로 알려주므로
    status가 "000"(정상)인지 반드시 확인해야 한다.
    """
    raw = http_get(API_BASE + "/" + endpoint, params)
    try:
        return json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError:
        # 가끔 키 오류 시 XML 오류 페이지가 오기도 한다.
        raise RuntimeError("OpenDART 응답을 JSON으로 읽지 못했습니다: " + raw[:200].decode("utf-8", "ignore"))


# API가 돌려주는 상태 코드 중 자주 만나는 것들에 대한 안내 문구
STATUS_MESSAGE = {
    "010": "등록되지 않은 인증키입니다. 키를 다시 확인해 주세요.",
    "011": "사용할 수 없는 인증키입니다. OpenDART에서 키 상태를 확인해 주세요.",
    "012": "접근할 수 없는 IP입니다.",
    "013": "조회된 데이터가 없습니다. (해당 연도 사업보고서가 아직 없거나 회사 코드가 다름)",
    "014": "파일이 존재하지 않습니다.",
    "020": "요청 제한을 초과했습니다. OpenDART는 하루 20,000건까지 호출할 수 있습니다.",
    "021": "조회 가능한 회사 개수가 초과했습니다.",
    "100": "필드의 부적절한 값입니다. 요청 인자를 확인해 주세요.",
    "101": "부적절한 접근입니다.",
    "800": "시스템 점검 중입니다. 잠시 후 다시 시도해 주세요.",
    "900": "정의되지 않은 오류가 발생했습니다.",
}


def check_status(result, context=""):
    """API 응답의 status를 검사하고, 정상이 아니면 사람이 읽을 수 있는 예외를 만든다."""
    status = result.get("status")
    if status == "000":
        return result
    message = result.get("message", "")
    hint = STATUS_MESSAGE.get(status, "")
    detail = " / ".join(x for x in [message, hint] if x)
    raise DartError("OpenDART 오류 [%s] %s%s" % (status, context, (" - " + detail) if detail else ""))


class DartError(Exception):
    """OpenDART 호출 중 생긴, 사용자가 이해할 수 있는 오류."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------------------
# 2. 기업 고유번호(corp_code) 찾기 — corpCode.xml 다운로드 + 로컬 캐시
# ---------------------------------------------------------------------------

def sync_corp_codes(api_key, refresh=False, verbose=True):
    """
    전체 기업 고유번호 목록을 확보해서 리스트로 돌려준다.

    1) 로컬 캐시(corp_codes.json)가 있고 7일 이내라면 그대로 사용 (빠름)
    2) 아니면 corpCode.xml ZIP을 내려받아 압축을 풀고 XML을 파싱해 캐시로 저장

    반환값: [{"corp_name": "삼성전자", "corp_code": "00126380", "stock_code": "005930"}, ...]
    """
    # --- (1) 캐시 확인 -----------------------------------------------------
    if not refresh and os.path.exists(CORP_CACHE_FILE):
        try:
            with open(CORP_CACHE_FILE, "r", encoding="utf-8") as fp:
                cache = json.load(fp)
            saved_at = datetime.fromisoformat(cache["saved_at"])
            age_days = (datetime.now() - saved_at).total_seconds() / 86400
            if age_days < CORP_CACHE_DAYS and cache.get("companies"):
                if verbose:
                    print("  · 기업 목록 캐시 사용 (%s, %d개사)" % (cache["saved_at"][:10], len(cache["companies"])))
                return cache["companies"]
        except (ValueError, KeyError, json.JSONDecodeError):
            pass  # 캐시가 깨졌으면 무시하고 새로 받는다.

    # --- (2) 새로 내려받기 -------------------------------------------------
    if verbose:
        print("  · corpCode.xml 내려받는 중... (약 3.6MB, 최초 1회만)")
    blob = http_get(API_BASE + "/corpCode.xml", {"crtfc_key": api_key})

    if not blob.startswith(b"PK"):
        # ZIP이 아니면 오류 XML이 온 것 → 키 문제일 가능성이 높다.
        raise DartError(
            "corpCode.xml을 받지 못했습니다. 인증키를 확인해 주세요. (응답 앞부분: %s)"
            % blob[:200].decode("utf-8", "ignore")
        )

    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        xml_name = archive.namelist()[0]
        xml_bytes = archive.read(xml_name)

    companies = []
    root = ElementTree.fromstring(xml_bytes)
    for node in root.findall("list"):
        stock_code = (node.findtext("stock_code") or "").strip()
        # 종목코드가 있는 회사 = 상장사만 저장한다.
        # (전체 12만 개사 중 상장사는 약 4천 개사 → 캐시 파일이 10MB에서 0.4MB로 줄어든다)
        if not stock_code:
            continue
        companies.append({
            "corp_name": (node.findtext("corp_name") or "").strip(),
            "corp_code": (node.findtext("corp_code") or "").strip(),
            "stock_code": stock_code,
        })

    # --- (3) 캐시로 저장 ---------------------------------------------------
    with open(CORP_CACHE_FILE, "w", encoding="utf-8") as fp:
        json.dump(
            {"saved_at": datetime.now().isoformat(timespec="seconds"), "companies": companies},
            fp, ensure_ascii=False,
        )
    if verbose:
        print("  · 상장사 %d개사 목록 저장 완료" % len(companies))
    return companies


def clean_name(name):
    """회사 이름 비교용 정규화: 공백/㈜/주식회사 제거, 소문자화."""
    for token in ("(주)", "㈜", "주식회사", " ", "\t"):
        name = name.replace(token, "")
    return name.strip().lower()


def find_company(companies, query):
    """
    회사 이름으로 corp_code를 찾는다.

    검색 순서: 정확히 일치(상장사) → 정확히 일치(전체) → 이름에 검색어가 포함된 상장사
    우선주(예: 삼성전자우)가 먼저 잡히지 않도록 '보통주(종목코드 6자리, 끝자리 0)'를 우선한다.
    """
    key = clean_name(query)

    exact = [c for c in companies if clean_name(c["corp_name"]) == key and c["stock_code"]]
    if not exact:
        exact = [c for c in companies if clean_name(c["corp_name"]) == key]
    if exact:
        # 우선주(끝자리가 0이 아닌 종목코드)보다 보통주를 먼저 쓴다.
        exact.sort(key=lambda c: (c["stock_code"][-1:] not in ("0", ""), c["corp_name"]))
        return exact[0]

    # 부분 일치 검색 (상장사만)
    partial = [c for c in companies if key in clean_name(c["corp_name"]) and c["stock_code"]]
    if len(partial) == 1:
        return partial[0]
    if len(partial) > 1:
        preview = ", ".join("%s(%s)" % (c["corp_name"], c["stock_code"]) for c in partial[:10])
        raise DartError("'%s'와 일치하는 회사가 여러 개입니다: %s … 회사 이름을 더 정확히 적어주세요." % (query, preview))

    raise DartError("'%s' 회사를 찾지 못했습니다. 상장사 이름을 정확히 적어주세요. (예: 삼성전자, SK하이닉스)" % query)


# ---------------------------------------------------------------------------
# 3. 재무제표 조회 및 주요 계정 추출
# ---------------------------------------------------------------------------

# 우리가 뽑고 싶은 계정 정의
#   key      : 프로그램 내부에서 쓸 이름
#   label    : 화면에 보여줄 이름
#   sj_div   : 재무제표 구분 (BS=재무상태표, IS=손익계산서, CIS=포괄손익계산서)
#   names    : 회사마다 다를 수 있는 실제 계정명 후보 (앞에 있을수록 우선)
ACCOUNT_SPECS = [
    # --- 손익계산서 ---
    {"key": "revenue",  "label": "매출액",     "sj_div": ("IS", "CIS"),
     "names": ("매출액", "수익(매출액)", "영업수익", "매출")},
    {"key": "op_income", "label": "영업이익",  "sj_div": ("IS", "CIS"),
     "names": ("영업이익", "영업이익(손실)", "영업손익")},
    {"key": "net_income", "label": "당기순이익", "sj_div": ("IS", "CIS"),
     "names": ("당기순이익", "당기순이익(손실)", "분기순이익", "반기순이익", "분기순이익(손실)")},
    # --- 재무상태표 ---
    {"key": "assets",        "label": "자산총계", "sj_div": ("BS",), "names": ("자산총계",)},
    {"key": "current_assets", "label": "유동자산", "sj_div": ("BS",), "names": ("유동자산",)},
    {"key": "liabilities",   "label": "부채총계", "sj_div": ("BS",), "names": ("부채총계",)},
    {"key": "current_liab",  "label": "유동부채", "sj_div": ("BS",), "names": ("유동부채",)},
    {"key": "equity",        "label": "자본총계", "sj_div": ("BS",), "names": ("자본총계",)},
]


# --- 리스크 진단용 확장 계정 (fnlttSinglAcntAll.json 에서만 받을 수 있는 항목) ---
# 이자보상배율·현금흐름·Z-Score 계산에는 주요계정 API에 없는 계정이 필요하다.
# account_id(IFRS 표준 태그)를 먼저 보고, 없으면 계정명으로 찾는다.
EXTRA_SPECS = [
    {"key": "interest_expense", "label": "이자비용", "sj_div": ("IS", "CIS"),
     "names": ("이자비용", "금융비용", "이자비용(금융비용)"),
     "ids": ("ifrs-full_InterestExpense", "ifrs-full_FinanceCosts")},
    {"key": "op_cash_flow", "label": "영업활동현금흐름", "sj_div": ("CF",),
     "names": ("영업활동현금흐름", "영업활동 현금흐름", "영업활동으로 인한 현금흐름"),
     "ids": ("ifrs-full_CashFlowsFromUsedInOperatingActivities",)},
    {"key": "retained_earnings", "label": "이익잉여금", "sj_div": ("BS",),
     "names": ("이익잉여금", "결손금"),
     "ids": ("ifrs-full_RetainedEarnings",)},
    # --- 운전자본(현금전환주기, CCC) 계산용 계정 ---
    # 매출원가는 손익계산서, 매출채권·재고자산·매입채무는 재무상태표(기말잔액)에서 가져온다.
    # 모두 fnlttSinglAcntAll.json(전체 재무제표)에만 있어서 이 함수에서 함께 뽑는다.
    {"key": "cost_of_sales", "label": "매출원가", "sj_div": ("IS", "CIS"),
     "names": ("매출원가", "매출원가(손실)", "상품매출원가", "제조원가"),
     "ids": ("ifrs-full_CostOfSales",)},
    {"key": "receivables", "label": "매출채권", "sj_div": ("BS",),
     "names": ("매출채권", "매출채권(순액)", "외상매출금"),
     "ids": ("ifrs-full_CurrentTradeReceivables", "ifrs-full_TradeReceivables")},
    {"key": "inventory", "label": "재고자산", "sj_div": ("BS",),
     "names": ("재고자산", "재고자산(순액)"),
     "ids": ("ifrs-full_Inventories",)},
    {"key": "payables", "label": "매입채무", "sj_div": ("BS",),
     "names": ("매입채무", "매입채무(순액)", "외상매입금"),
     "ids": ("ifrs-full_TradeAndOtherCurrentPayablesToTradeSuppliers", "ifrs-full_TradePayables")},
]


def normalize_name(name):
    """계정명 비교용: 띄어쓰기를 없애 '영업활동 현금흐름'과 '영업활동현금흐름'을 같게 본다."""
    return (name or "").replace(" ", "")


def fetch_extra_accounts(api_key, corp_code, year):
    """
    fnlttSinglAcntAll.json(전체 재무제표)에서 리스크 진단용 계정을 추가로 뽑는다.

    현금흐름표(CF)·이익잉여금·금융비용은 주요계정(fnlttSinglAcnt)에는 없다.
    이 호출이 실패해도 기본 분석은 그대로 진행되도록 빈 dict를 돌려준다(→ 리스크 항목은 N/A).
    """
    params = {
        "crtfc_key": api_key,
        "corp_code": corp_code,
        "bsns_year": str(year),
        "reprt_code": REPORT_ANNUAL,
        "fs_div": "CFS",
    }
    try:
        result = call_json("fnlttSinglAcntAll.json", params)
    except (DartError, RuntimeError):
        return {}
    if result.get("status") != "000":
        return {}

    rows = result.get("list") or []
    # 이 엔드포인트는 fs_div 필드를 주지 않는 경우가 많다. 있으면 연결만 사용.
    if any("fs_div" in row for row in rows):
        cfs_rows = [row for row in rows if row.get("fs_div") == "CFS"]
        rows = cfs_rows or rows

    unique = {}
    for row in rows:
        unique.setdefault((row.get("sj_div"), row.get("account_nm")), row)

    extras = {}
    for spec in EXTRA_SPECS:
        found = None
        for account_id in spec["ids"]:            # 1순위: IFRS 표준 태그
            for (sj_div, _name), row in unique.items():
                if sj_div in spec["sj_div"] and row.get("account_id") == account_id:
                    found = row
                    break
            if found:
                break
        if found is None:                          # 2순위: 계정명(띄어쓰기 무시)
            for name in spec["names"]:
                for (sj_div, account_nm), row in unique.items():
                    if sj_div in spec["sj_div"] and normalize_name(account_nm) == normalize_name(name):
                        found = row
                        break
                if found:
                    break
        if found:
            extras[spec["key"]] = {
                "amount": parse_amount(found.get("thstrm_amount")),
                "account_nm": found.get("account_nm"),
            }
    return extras


def parse_amount(text):
    """
    "333,605,938,000,000" 같은 문자열을 숫자로 바꾼다.
    OpenDART는 값이 없으면 "-" 또는 빈 문자열을 넣어 보낸다 → 0으로 처리.
    """
    if text is None:
        return 0.0
    cleaned = str(text).replace(",", "").replace(" ", "").strip()
    if cleaned in ("", "-", "－"):
        return 0.0
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def fetch_statement(api_key, corp_code, year, fs_div_pref=("CFS", "OFS")):
    """
    한 회사·한 사업연도의 사업보고서 주요계정(fnlttSinglAcnt.json)을 가져온다.

    ※ 주의: fs_div를 지정해도 API가 연결(CFS)과 별도(OFS)를 함께 돌려주는 경우가 있다.
      그래서 응답을 받은 뒤 fs_div 값으로 직접 걸러서 쓴다. (실제로 이 부분이 가장 흔한 함정)

    반환값: (fs_div, fs_nm, rcept_no, {key: {"amount":값, "account_nm":계정명}}, 연도별 원본행)
    """
    params = {
        "crtfc_key": api_key,
        "corp_code": corp_code,
        "bsns_year": str(year),
        "reprt_code": REPORT_ANNUAL,
        "fs_div": fs_div_pref[0],  # 일단 연결로 요청 (필터는 아래에서 직접)
    }
    result = call_json("fnlttSinglAcnt.json", params)
    rows = result.get("list") or []
    if not rows:
        raise DartError("", status="013")

    # --- 이름 검색에 실패한 경우: 오류 메시지를 그대로 전달 ---
    if result.get("status") != "000":
        check_status(result, "(%s %d년)" % (corp_code, year))

    # --- (1) 원하는 fs_div(연결 우선)의 행만 남긴다 ---
    used_div, used_rows = None, []
    for div in fs_div_pref:
        candidate = [r for r in rows if r.get("fs_div") == div]
        if candidate:
            used_div, used_rows = div, candidate
            break
    if not used_rows:  # fs_div 값이 예상과 다른 경우 → 전체 사용
        used_div, used_rows = rows[0].get("fs_div", "CFS"), rows

    # --- (2) 같은 계정이 두 번 오는 경우가 있어 (sj_div, account_nm) 기준으로 첫 행만 사용 ---
    unique = {}
    for row in used_rows:
        unique.setdefault((row.get("sj_div"), row.get("account_nm")), row)

    # --- (3) 우리가 원하는 계정을 이름 후보 순서대로 찾아 담는다 ---
    accounts = {}
    for spec in ACCOUNT_SPECS:
        for name in spec["names"]:
            found = None
            for (sj_div, account_nm), row in unique.items():
                if sj_div in spec["sj_div"] and account_nm == name:
                    found = row
                    break
            if found:
                accounts[spec["key"]] = {
                    "amount": parse_amount(found.get("thstrm_amount")),
                    "prev_amount": parse_amount(found.get("frmtrm_amount")),
                    "account_nm": name,
                }
                break

    fs_nm = used_rows[0].get("fs_nm", "연결재무제표" if used_div == "CFS" else "재무제표")
    return used_div, fs_nm, used_rows[0].get("rcept_no", ""), accounts


# ---------------------------------------------------------------------------
# 4. 재무비율 계산
# ---------------------------------------------------------------------------

def safe_ratio(numerator, denominator, scale=100.0):
    """분모가 0이거나 값이 없으면 None(=계산 불가)을 돌려준다."""
    if numerator is None or not denominator:
        return None
    return numerator / denominator * scale


def growth_rate(current, previous):
    """
    증감률(%) = (당기 - 전기) / |전기| × 100
    전기가 음수(적자)면 'abs'로 나눠 부호가 뒤집히지 않게 하고, 화면에는 참고 표시를 붙인다.
    """
    if current is None or previous is None or previous == 0:
        return None
    return (current - previous) / abs(previous) * 100.0


def compute_ratios(years_data, years_sorted):
    """
    연도별 계정 금액으로 재무비율을 계산해 각 연도 dict 안에 'ratios'로 넣는다.

    수익성: 영업이익률, 순이익률, ROE
    안정성: 부채비율, 유동비율
    성장성: 매출액 증가율, 영업이익 증가율 (전년 대비, YoY)
    """
    for index, year in enumerate(years_sorted):
        entry = years_data[year]
        acc = entry["accounts"]

        def value(key):
            item = acc.get(key)
            return item["amount"] if item else None

        revenue = value("revenue")
        op_income = value("op_income")
        net_income = value("net_income")
        assets = value("assets")
        current_assets = value("current_assets")
        liabilities = value("liabilities")
        current_liab = value("current_liab")
        equity = value("equity")

        # 전년도 데이터 (정렬된 연도 목록에서 바로 앞 연도)
        prev_entry = years_data.get(years_sorted[index - 1]) if index > 0 else None
        prev_revenue = prev_entry["accounts"].get("revenue", {}).get("amount") if prev_entry else None
        prev_op = prev_entry["accounts"].get("op_income", {}).get("amount") if prev_entry else None

        entry["ratios"] = {
            "op_margin": safe_ratio(op_income, revenue),          # 영업이익률 (%)
            "net_margin": safe_ratio(net_income, revenue),        # 순이익률 (%)
            "roe": safe_ratio(net_income, equity),                # ROE (%) — 기말 자본 기준
            "debt_ratio": safe_ratio(liabilities, equity),        # 부채비율 (%)
            "current_ratio": safe_ratio(current_assets, current_liab),  # 유동비율 (%)
            "revenue_growth": growth_rate(revenue, prev_revenue),  # 매출액 증가율 (%)
            "op_growth": growth_rate(op_income, prev_op),          # 영업이익 증가율 (%)
            "roa": safe_ratio(net_income, assets),                 # ROA (%) — 참고용
        }
        # 영업이익 증가율의 기저가 적자였는지 표시 (해석할 때 주의)
        entry["op_growth_base_negative"] = bool(prev_op is not None and prev_op < 0)

        # --- DuPont 3단계 분해 -------------------------------------------------
        #   ROE = 순이익율 × 총자산회전율 × 재무레버리지
        #   총자산회전율의 분모는 '기말 자산총계'다(기초·기말 평균잔액이 아님).
        #   매출액 ÷ 기말자산으로 계산해야 두 해 자산이 크게 늘어난 기업도
        #   공시된 한 시점 자산과 바로 대응되며, HTML·콘솔·README에 이 기준을 명시한다.
        net_margin = entry["ratios"]["net_margin"]                    # %
        asset_turnover = safe_ratio(revenue, assets, scale=1.0)      # 회 (매출액 ÷ 기말 자산총계)
        equity_multiplier = safe_ratio(assets, equity, scale=1.0)    # 배 (자산총계 ÷ 자본총계)
        dupont_roe = None
        if None not in (net_margin, asset_turnover, equity_multiplier):
            # (%/100) × 회 × 배 × 100 = %
            dupont_roe = net_margin * asset_turnover * equity_multiplier
        simple_roe = entry["ratios"]["roe"]
        entry["dupont"] = {
            "net_margin": net_margin,
            "asset_turnover": asset_turnover,
            "equity_multiplier": equity_multiplier,
            "roe": dupont_roe,
            "roe_simple": simple_roe,
            "diff": (dupont_roe - simple_roe) if (dupont_roe is not None and simple_roe is not None) else None,
            "assets_basis": "기말",
        }


RATIO_LABELS = [
    # (키, 표시 이름, 단위, 그룹)
    ("op_margin", "영업이익률", "%", "수익성"),
    ("net_margin", "순이익률", "%", "수익성"),
    ("roe", "ROE (자기자본이익률)", "%", "수익성"),
    ("debt_ratio", "부채비율", "%", "안정성"),
    ("current_ratio", "유동비율", "%", "안정성"),
    ("revenue_growth", "매출액 증가율(YoY)", "%", "성장성"),
    ("op_growth", "영업이익 증가율(YoY)", "%", "성장성"),
]


# ---------------------------------------------------------------------------
# 4-1. DuPont 분해 · 운전자본(CCC) · DART 원문 링크
# ---------------------------------------------------------------------------

# DART 원문 뷰어 주소: rcpNo(접수번호)만 있으면 사업보고서 원문이 바로 열린다.
DART_VIEWER = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=%s"

# CCC 계산에 쓰는 네 가지 계정 (계산 불가 안내 문구에 그대로 쓴다)
CCC_INPUTS = [
    ("cost_of_sales", "매출원가"),
    ("receivables", "매출채권"),
    ("inventory", "재고자산"),
    ("payables", "매입채무"),
]

# DuPont 세 요인 → (내부 키, 표시 이름)
DUPONT_FACTORS = [
    ("profitability", "수익성(순이익률)"),
    ("efficiency", "자산효율성(총자산회전율)"),
    ("leverage", "재무레버리지"),
]


def dart_link(rcept_no):
    """접수번호(rcept_no)로 DART 원문 공시 뷰어 링크를 만든다."""
    rcept_no = (rcept_no or "").strip()
    return DART_VIEWER % rcept_no if rcept_no else ""


def rcept_date(rcept_no):
    """접수번호 앞 8자리(YYYYMMDD)에서 접수일을 만든다. (DART 접수번호 규칙)"""
    digits = (rcept_no or "").strip()[:8]
    if len(digits) != 8 or not digits.isdigit():
        return ""
    return "%s-%s-%s" % (digits[:4], digits[4:6], digits[6:])


def fmt(value, digits=1, suffix="%"):
    """숫자에 단위를 붙인 문자열. 값이 없으면 '-'. (단위가 %가 아니어도 쓸 수 있게 suffix로 받는다)"""
    if value is None:
        return "-"
    return (("%." + str(digits) + "f") % value) + suffix


def fmt_turn(value):
    return fmt(value, 2, "회")


def fmt_mult(value):
    return fmt(value, 2, "배")


def fmt_days(value):
    return fmt(value, 0, "일")


def fmt_diff(value, digits=2, suffix="%p"):
    """차이값 표시용. 0에 아주 가까우면 '-0.00' 대신 '0.00'으로 보여 준다."""
    if value is None:
        return "-"
    if abs(value) < 10 ** (-digits):
        return ("%." + str(digits) + "f") % 0.0 + suffix
    return ("%+." + str(digits) + "f") % value + suffix


def with_josa(word, batchim, no_batchim):
    """
    마지막 한글 글자의 받침 유무로 조사를 붙인다. (은/는, 이/가 …)
    끝에 괄호가 붙은 라벨("자산효율성(총자산회전율)")은 괄호를 뺀 글자로 판단한다.
    """
    core = word.rstrip(")")
    last = core[-1] if core else ""
    code = ord(last) - 0xAC00 if last and 0xAC00 <= ord(last) <= 0xD7A3 else None
    has_batchim = (code % 28 != 0) if code is not None else False
    return word + (batchim if has_batchim else no_batchim)


def compute_working_capital(years_data, years_sorted):
    """
    운전자본 관리 지표(DSO·DIO·DPO·CCC)를 연도별로 계산해 'ccc'에 넣는다.

        DSO (매출채권회전일수) = 매출채권 ÷ 매출액 × 365
        DIO (재고자산회전일수) = 재고자산 ÷ 매출원가 × 365
        DPO (매입채무회전일수) = 매입채무 ÷ 매출원가 × 365
        CCC (현금전환주기)     = DSO + DIO − DPO

    · 분자(매출채권·재고자산·매입채무)는 재무상태표 **기말잔액**이다.
      기초·기말 평균잔액을 쓰는 교과서식과는 값이 조금 달라질 수 있다.
    · 매출원가·매출채권·재고자산·매입채무 중 하나라도 공시되지 않으면
      (금융회사처럼 재고자산이 없는 업종, 매출원가를 따로 표시하지 않는 회사 등)
      해당 지표는 None(=N/A)으로 두고 CCC도 계산하지 않는다.
    """
    for year in years_sorted:
        entry = years_data[year]
        acc = entry["accounts"]

        def amount(key):
            item = acc.get(key)
            return item["amount"] if item else None

        revenue = amount("revenue")
        cogs = amount("cost_of_sales")
        receivables = amount("receivables")
        inventory = amount("inventory")
        payables = amount("payables")

        dso = safe_ratio(receivables, revenue, 365.0) if receivables is not None else None
        dio = safe_ratio(inventory, cogs, 365.0) if inventory is not None else None
        dpo = safe_ratio(payables, cogs, 365.0) if payables is not None else None
        ccc = (dso + dio - dpo) if None not in (dso, dio, dpo) else None

        entry["ccc"] = {
            "dso": dso,
            "dio": dio,
            "dpo": dpo,
            "ccc": ccc,
            # 화면에서 "왜 N/A인지"를 보여주기 위해 원재료(금액)도 남겨 둔다.
            "inputs": {
                "revenue": revenue, "cost_of_sales": cogs, "receivables": receivables,
                "inventory": inventory, "payables": payables,
            },
        }


def make_dupont_comment(company, latest):
    """한 기업의 DuPont 분해 결과를 문장으로 풀어 준다(수치가 없으면 안내 문구)."""
    dupont = (company["years"][latest].get("dupont") or {})
    if dupont.get("roe") is None:
        return ("DuPont 분해에 필요한 계정(매출액·당기순이익·자산총계·자본총계)이 일부 없어 "
                "분해할 수 없습니다.")
    text = ("ROE %s = 순이익률 %s × 총자산회전율 %s × 재무레버리지 %s. "
            "매출 100원당 최종이익이 %.1f원, 기말 자산 1원이 만든 매출이 %.2f원, "
            "자기자본 1원에 대응하는 자산이 %.2f원이라는 뜻입니다." % (
                fmt(dupont["roe"]), fmt(dupont["net_margin"]),
                fmt_turn(dupont["asset_turnover"]), fmt_mult(dupont["equity_multiplier"]),
                dupont["net_margin"], dupont["asset_turnover"], dupont["equity_multiplier"]))
    if dupont.get("diff") is not None and abs(dupont["diff"]) < 0.05:
        text += " 기존 단순 ROE(당기순이익 ÷ 기말 자본총계, %s)와 검산이 일치합니다." % fmt(dupont["roe_simple"])
    return text


def build_dupont_gap(companies, latest):
    """
    두 기업의 ROE 격차를 세 요인으로 분해한다(순차 대체법).

    ROE = 순이익률 × 총자산회전율 × 재무레버리지 이므로,
    A의 요인을 하나씩 B의 값으로 바꿔 가며 변화량을 누적하면
        ΔROE = 수익성 기여 + 자산효율성 기여 + 레버리지 기여
    가 정확히 성립한다(잔차 0, 반올림 오차만 남음).
    어떤 요인을 먼저 바꾸느냐에 따라 개별 기여도는 조금 달라질 수 있어
    '수익성 → 자산효율성 → 레버리지' 순서로 고정했다.

    반환값: {rows, total, text, ...} 또는 계산 불가 시 None
    """
    if len(companies) != 2:
        return None
    a, b = companies
    da = a["years"][latest].get("dupont") or {}
    db = b["years"][latest].get("dupont") or {}
    needed = ("net_margin", "asset_turnover", "equity_multiplier", "roe")
    if any(da.get(key) is None or db.get(key) is None for key in needed):
        return None

    pm_a, at_a, em_a = da["net_margin"] / 100.0, da["asset_turnover"], da["equity_multiplier"]
    pm_b, at_b, em_b = db["net_margin"] / 100.0, db["asset_turnover"], db["equity_multiplier"]
    r0 = pm_a * at_a * em_a * 100.0                      # A의 ROE
    r1 = pm_b * at_a * em_a * 100.0                      # 수익성만 B로
    r2 = pm_b * at_b * em_a * 100.0                      # 수익성 + 자산효율성
    r3 = pm_b * at_b * em_b * 100.0                      # 전부 B (= B의 ROE)

    contributions = {
        "profitability": r1 - r0,
        "efficiency": r2 - r1,
        "leverage": r3 - r2,
    }
    rows = []
    for key, label in DUPONT_FACTORS:
        if key == "profitability":
            va, vb = da["net_margin"], db["net_margin"]
        elif key == "efficiency":
            va, vb = da["asset_turnover"], db["asset_turnover"]
        else:
            va, vb = da["equity_multiplier"], db["equity_multiplier"]
        rows.append({
            "key": key, "label": label, "a": va, "b": vb,
            "delta": vb - va, "contribution": contributions[key],
        })

    total = r3 - r0
    roe_a, roe_b = da["roe"], db["roe"]
    higher = b if total > 0 else (a if total < 0 else None)

    if higher is None:
        text = ("%d년 ROE가 %s %.1f%%, %s %.1f%%로 같습니다. 세 요인의 조합이 서로 달라도 "
                "최종 ROE는 같은 수준입니다." % (latest, a["name"], roe_a, b["name"], roe_b))
    else:
        lower = a if higher is b else b
        gap = abs(total)
        text = ("%d년 ROE는 %s %.1f%%, %s %.1f%%로 %s가 %.1f%%p 높습니다. 격차를 요인별로 쪼개면 "
                "수익성 %+.1f%%p, 자산효율성 %+.1f%%p, 레버리지 %+.1f%%p입니다." % (
                    latest, a["name"], roe_a, b["name"], roe_b, higher["name"], gap,
                    contributions["profitability"], contributions["efficiency"], contributions["leverage"]))
        ordered = sorted(rows, key=lambda row: -abs(row["contribution"]))
        top = ordered[0]
        text += " 가장 큰 요인은 %s %+.1f%%p입니다." % (top["label"], top["contribution"])
        if total:
            share = top["contribution"] / total * 100.0
            if share >= 60:
                text += " 격차의 약 %d%%가 여기서 나왔습니다." % round(share)
        opposing = [row for row in rows if row["contribution"] * total < 0 and abs(row["contribution"]) >= 0.05]
        if opposing:
            text += " 반면 %s %s로 격차를 줄이는 방향으로 작용했습니다." % (
                with_josa(", ".join(row["label"] for row in opposing), "은", "는"),
                ", ".join("%+.1f%%p" % row["contribution"] for row in opposing))

    return {
        "rows": rows,
        "total": total,
        "text": text,
        "a": a["name"],
        "b": b["name"],
        "roe_a": roe_a,
        "roe_b": roe_b,
        "higher": higher["name"] if higher else "",
    }


def make_ccc_comment(company, years):
    """한 기업의 CCC 계산 결과와 전년 대비 변화를 문장으로 풀어 준다."""
    latest = years[-1]
    current = company["years"][latest].get("ccc") or {}
    if current.get("ccc") is None:
        missing = [label for key, label in CCC_INPUTS
                   if (current.get("inputs") or {}).get(key) is None]
        detail = ("·".join(missing) + " 미공시") if missing else "관련 계정 부족"
        return ("CCC를 계산할 수 없습니다 — %s. 금융업처럼 재고자산·매출원가가 없는 업종은 "
                "운전자본 주기 지표를 쓰지 않습니다." % detail)

    text = ("%d년 CCC는 %.0f일입니다 — DSO %.0f일(매출채권 회수) + DIO %.0f일(재고 보유) "
            "− DPO %.0f일(매입채무 지급)." % (
                latest, current["ccc"], current["dso"], current["dio"], current["dpo"]))

    if len(years) >= 2:
        previous = company["years"][years[-2]].get("ccc") or {}
        if previous.get("ccc") is not None:
            delta = current["ccc"] - previous["ccc"]
            if abs(delta) < 0.5:
                text += " 전년(%d년)과 거의 같은 수준입니다." % years[-2]
            else:
                moves = [
                    ("DSO", current["dso"] - previous["dso"]),
                    ("DIO", current["dio"] - previous["dio"]),
                    ("DPO", -(current["dpo"] - previous["dpo"])),   # DPO가 늘면 CCC는 줄어든다
                ]
                driver = max(moves, key=lambda item: abs(item[1]))
                text += (" 전년(%.0f일)보다 %.0f일 %s고, 가장 큰 변화는 %s가 %.0f일 %s 것입니다." % (
                    previous["ccc"], abs(delta), "짧아졌" if delta < 0 else "길어졌",
                    driver[0], abs(driver[1]), "줄어든" if driver[1] < 0 else "늘어난"))
    return text


def build_ccc_gap(companies, latest):
    """두 기업의 CCC와 구성요소 차이를 한 문장으로 정리한다."""
    if len(companies) != 2:
        return None
    a, b = companies
    ca = a["years"][latest].get("ccc") or {}
    cb = b["years"][latest].get("ccc") or {}
    if ca.get("ccc") is None or cb.get("ccc") is None:
        return None
    shorter, longer = (a, b) if ca["ccc"] <= cb["ccc"] else (b, a)
    gap = abs(ca["ccc"] - cb["ccc"])
    components = [
        ("DSO(매출채권회전일수)", ca["dso"], cb["dso"]),
        ("DIO(재고자산회전일수)", ca["dio"], cb["dio"]),
        ("DPO(매입채무회전일수)", ca["dpo"], cb["dpo"]),
    ]
    biggest = max(components, key=lambda item: abs(item[1] - item[2]))
    text = ("%d년 CCC는 %s %.0f일, %s %.0f일로 %s가 %.0f일 짧아 운전자본 부담이 적습니다. "
            "가장 큰 차이는 %s로, %s %.0f일 vs %s %.0f일입니다." % (
                latest, a["name"], ca["ccc"], b["name"], cb["ccc"], shorter["name"], gap,
                biggest[0], a["name"], biggest[1], b["name"], biggest[2]))
    return {"text": text, "a": ca, "b": cb, "gap": gap, "shorter": shorter["name"]}



# ---------------------------------------------------------------------------
# 4-2. 5대 재무 리스크 진단 (신호등 + 100점 종합 점수)
# ---------------------------------------------------------------------------
#
# 평가 항목은 5개, 항목당 20점 만점이다.
#   🟢 안전 20점 / 🟡 주의 10점 / 🔴 위험 0점
# 데이터를 못 구한 항목(N/A)은 분모에서 빼고 남은 항목으로 100점으로 환산한다.
#   종합점수 = (획득점수 합 ÷ 평가가능 항목 만점 합) × 100
#
# 등급: 85점 이상 🟢 안전 / 60점 이상 🟡 주의 / 60점 미만 🔴 위험

RISK_SCORES = {"safe": 20, "caution": 10, "danger": 0}
RISK_META = {
    "safe": {"label": "안전", "light": "🟢", "class": "safe"},
    "caution": {"label": "주의", "light": "🟡", "class": "caution"},
    "danger": {"label": "위험", "light": "🔴", "class": "danger"},
    "na": {"label": "N/A", "light": "⚪", "class": "na"},
}

# 항목별 판정 기준 (README·HTML에 그대로 표기되는 문구)
RISK_CRITERIA = {
    "current_ratio": "150% 이상 안전 · 100~150% 주의 · 100% 미만 위험",
    "debt_ratio": "100% 이하 안전 · 100~200% 주의 · 200% 초과 위험",
    "interest_coverage": "3배 이상 안전 · 1~3배 주의 · 1배 미만 위험",
    "cash_quality": "영업현금흐름과 순이익 동반 흑자 안전 · 순이익 흑자이나 현금흐름 적자 위험 · 동반 적자 위험",
    "solvency": "연속 영업적자 없음 + Z'' 2.6 이상 안전 · 1.1~2.6 주의 · 1.1 미만 위험",
}


def count_op_loss_streak(company, years):
    """최근 연도부터 거슬러 올라가며 '연속 영업적자' 연수를 센다."""
    streak = 0
    for year in reversed(years):
        op_income = company["years"][year]["accounts"].get("op_income", {}).get("amount")
        if op_income is not None and op_income < 0:
            streak += 1
        else:
            break
    return streak


def altman_z_double_prime(accounts):
    """
    Altman Z''-Score (비상장·신흥국 기업용 장부가 기준 변형)
        Z'' = 6.56·X1 + 3.26·X2 + 6.72·X3 + 1.05·X4
        X1 = 운전자본 / 자산총계        (운전자본 = 유동자산 - 유동부채)
        X2 = 이익잉여금 / 자산총계
        X3 = 영업이익(EBIT) / 자산총계
        X4 = 자기자본(자본총계) / 부채총계

    원래 Altman 모형의 X4는 '시가총액/부채총계'지만, 시가총액을 쓰지 않고
    장부가(자본총계)를 쓰는 Z'' 변형을 택했다. 상장·비상장 모두 같은 기준으로 비교할 수 있다.
    필요한 계정이 없으면 None.
    """
    def amount(key):
        return accounts.get(key, {}).get("amount")

    assets = amount("assets")
    current_assets = amount("current_assets")
    current_liab = amount("current_liab")
    liabilities = amount("liabilities")
    equity = amount("equity")
    op_income = amount("op_income")
    retained = amount("retained_earnings")

    if not assets or not liabilities or equity is None or op_income is None:
        return None

    x1 = ((current_assets or 0) - (current_liab or 0)) / assets
    x2 = (retained or 0) / assets          # 이익잉여금 미공시면 0으로 보수적 처리
    x3 = op_income / assets
    x4 = equity / liabilities if liabilities else 0
    return 6.56 * x1 + 3.26 * x2 + 6.72 * x3 + 1.05 * x4


def evaluate_risk(company, years):
    """
    한 회사의 5대 리스크 항목을 판정하고 100점 만점 종합 점수를 계산한다.

    반환값: {
      "items": [{key,label,value_text,state,score,max,note}],
      "score": 88, "grade": "safe", "comment": "...", "z_score": 5.4, "op_loss_streak": 0
    }
    """
    latest = years[-1]
    entry = company["years"][latest]
    accounts = entry["accounts"]
    ratios = entry["ratios"]
    items = []

    def add(key, label, value_text, state, note=""):
        items.append({
            "key": key,
            "label": label,
            "value_text": value_text,
            "state": state,
            "score": RISK_SCORES.get(state, 0),
            "max": 20 if state != "na" else 0,
            "note": note,
            "criteria": RISK_CRITERIA[key],
        })

    # --- (1) 유동비율 ---
    current_ratio = ratios.get("current_ratio")
    if current_ratio is None:
        add("current_ratio", "유동비율", "계산 불가", "na", "유동자산·유동부채 미공시(금융회사 등)")
    else:
        state = "safe" if current_ratio >= 150 else ("caution" if current_ratio >= 100 else "danger")
        add("current_ratio", "유동비율", pct(current_ratio), state)

    # --- (2) 부채비율 ---
    debt_ratio = ratios.get("debt_ratio")
    if debt_ratio is None:
        add("debt_ratio", "부채비율", "계산 불가", "na", "자본총계·부채총계 미공시")
    else:
        state = "safe" if debt_ratio <= 100 else ("caution" if debt_ratio <= 200 else "danger")
        add("debt_ratio", "부채비율", pct(debt_ratio), state)

    # --- (3) 이자보상배율 = 영업이익 ÷ 이자비용 ---
    op_income = accounts.get("op_income", {}).get("amount")
    interest = accounts.get("interest_expense")
    if interest is None:
        add("interest_coverage", "이자보상배율", "N/A", "na", "이자비용(금융비용) 계정 미공시")
    elif interest["amount"] == 0:
        add("interest_coverage", "이자보상배율", "이자비용 없음", "safe")
    elif op_income is None:
        add("interest_coverage", "이자보상배율", "N/A", "na", "영업이익 미공시")
    else:
        coverage = op_income / interest["amount"]
        state = "safe" if coverage >= 3 else ("caution" if coverage >= 1 else "danger")
        note = "%s 기준" % interest.get("account_nm", "이자비용")
        add("interest_coverage", "이자보상배율", "%.1f배" % coverage, state, note)

    # --- (4) 현금흐름 건전성 (영업활동현금흐름 vs 당기순이익) ---
    op_cf = accounts.get("op_cash_flow", {}).get("amount")
    net_income = accounts.get("net_income", {}).get("amount")
    if op_cf is None or net_income is None:
        add("cash_quality", "현금흐름 건전성", "N/A", "na", "현금흐름표 또는 당기순이익 미공시")
    elif net_income > 0 and op_cf > 0:
        add("cash_quality", "현금흐름 건전성", "이익·현금 동반 흑자", "safe",
            "영업현금흐름 %s / 순이익 %s" % (to_jo(op_cf), to_jo(net_income)))
    elif net_income > 0 and op_cf <= 0:
        add("cash_quality", "현금흐름 건전성", "이익 흑자·현금 적자", "danger",
            "장부상 이익이 현금으로 들어오지 않음(이익의 질 의심)")
    elif net_income <= 0 and op_cf > 0:
        add("cash_quality", "현금흐름 건전성", "적자이나 현금 창출", "caution",
            "손익은 적자지만 영업현금흐름은 플러스")
    else:
        add("cash_quality", "현금흐름 건전성", "이익·현금 동반 적자", "danger",
            "영업현금흐름 %s / 순이익 %s" % (to_jo(op_cf), to_jo(net_income)))

    # --- (5) 연속 적자 · 부실 예측 (Altman Z''-Score + 연속 영업적자) ---
    streak = count_op_loss_streak(company, years)
    z_score = altman_z_double_prime(accounts)
    if z_score is None:
        if streak >= 2:
            add("solvency", "연속 적자·부실 예측", "%d년 연속 영업적자" % streak, "danger")
        elif streak == 1:
            add("solvency", "연속 적자·부실 예측", "1년 영업적자", "caution")
        else:
            add("solvency", "연속 적자·부실 예측", "N/A", "na", "Z'' 계산에 필요한 계정 부족")
    else:
        if streak >= 3:
            state = "danger"                     # 3년 연속 영업적자 → 모형과 무관하게 위험
        elif streak == 2:
            state = "danger" if z_score < 1.1 else "caution"
        else:
            state = "safe" if z_score >= 2.6 else ("caution" if z_score >= 1.1 else "danger")
        value_text = "Z'' %.1f점" % z_score
        if streak:
            value_text += " · %d년 연속 적자" % streak
        band = "안전" if z_score >= 2.6 else ("회색" if z_score >= 1.1 else "부실")
        streak_text = "%d년 연속 영업적자" % streak if streak else "최근 3개년 영업흑자"
        add("solvency", "연속 적자·부실 예측", value_text, state,
            "%s · Z'' %s 구간" % (streak_text, band))

    # --- 종합 점수: N/A 항목은 분모에서 제외하고 100점으로 환산 ---
    earned = sum(item["score"] for item in items)
    available_max = sum(item["max"] for item in items)
    score = round(earned / available_max * 100) if available_max else 0
    grade = "safe" if score >= 85 else ("caution" if score >= 60 else "danger")

    # --- 자동 한 줄 코멘트 ---
    dangers = [i for i in items if i["state"] == "danger"]
    cautions = [i for i in items if i["state"] == "caution"]
    safes = [i for i in items if i["state"] == "safe"]
    nas = [i for i in items if i["state"] == "na"]
    phrases = []
    if dangers:
        phrases.append("위험 신호 — " + ", ".join("%s %s" % (i["label"], i["value_text"]) for i in dangers))
    if cautions:
        phrases.append("주의 신호 — " + ", ".join("%s %s" % (i["label"], i["value_text"]) for i in cautions))
    if safes:
        phrases.append("안전 %d개 항목(%s)" % (len(safes), ", ".join(i["label"] for i in safes)))
    if nas:
        phrases.append("판정 불가 %d개(%s)" % (len(nas), ", ".join(i["label"] for i in nas)))

    headline = {
        "safe": "재무 리스크가 낮습니다",
        "caution": "일부 지표를 지켜봐야 합니다",
        "danger": "재무 리스크가 높습니다",
    }[grade]
    comment = "%s %s · %d점 — %s" % (RISK_META[grade]["light"], headline, score, " / ".join(phrases))

    return {
        "items": items,
        "score": score,
        "grade": grade,
        "comment": comment,
        "z_score": z_score,
        "op_loss_streak": streak,
        "available_count": sum(1 for i in items if i["state"] != "na"),
    }


# ---------------------------------------------------------------------------
# 4-3. 비재무 공시 위험 키워드 탐지(공시검색 list.json)
# ---------------------------------------------------------------------------
#
# 재무제표는 "숫자로 드러난 과거"만 보여준다. 소송·횡령·의견거절처럼
# 숫자보다 먼저 또는 나중에 나타나는 위험은 공시 목록에서 찾아야 한다.
# 여기서는 최근 1년 공시 제목(report_nm)에 위험 키워드가 있는지만 본다.
#   · 점수에는 반영하지 않는다(정성 정보이므로 ‘참고’로만 표시)
#   · 호출이 실패해도 재무 분석은 그대로 진행된다

# (공시명에 포함된 문자열, 화면 표시 이름, 심각도)
#   severe  = 즉시 확인이 필요한 사건성 공시
#   caution = 자본·구조 변경처럼 맥락을 봐야 하는 공시
DISCLOSURE_RISK_KEYWORDS = [
    ("횡령", "횡령", "severe"),
    ("배임", "배임", "severe"),
    ("의견거절", "감사의견 거절", "severe"),
    ("감사의견", "감사의견 비적정", "severe"),
    ("한정", "감사의견 한정", "severe"),
    ("상장폐지", "상장폐지", "severe"),
    ("관리종목", "관리종목", "severe"),
    ("불성실공시", "불성실공시", "severe"),
    ("부도", "부도", "severe"),
    ("회생", "회생절차", "severe"),
    ("소송", "소송", "severe"),
    ("유상증자", "유상증자", "caution"),
    ("감자", "감자", "caution"),
]

DISCLOSURE_SCAN_DAYS = 365      # 최근 1년
DISCLOSURE_PAGE_COUNT = 100     # 한 번(페이지)에 가져올 수 있는 최대 건수 (API 상한)
DISCLOSURE_MAX_PAGES = 30       # 최대 30페이지 = 3,000건까지 훑는다 (1년치 전체 조회용 안전장치)


def fetch_disclosure_risks(api_key, corp_code, days=DISCLOSURE_SCAN_DAYS, max_pages=DISCLOSURE_MAX_PAGES):
    """
    최근 N일(기본 1년) 공시 목록에서 위험 키워드가 포함된 공시를 찾는다.

    ★ 한 번에 100건까지만 오는 API라, 페이지를 넘겨 가며 '1년치 전체'를 훑는다.
      대기업은 1년에 공시가 2,000~3,000건이라 첫 100건만 보면 3~4개월치만 보게 되고,
      그러면 "위험 공시 없음"이 사실과 달라질 수 있다. (실제로 삼성전자는 최근 1년 2,916건)
      안전장치로 최대 DISCLOSURE_MAX_PAGES(30)페이지 = 3,000건까지만 조회하고,
      거기서 잘리면 truncated=True로 알려 화면에 '일부만 조회'를 표시한다.

    반환값 dict
      status    : "clear"(위험 공시 없음) / "ok"(탐지됨) / "error"(조회 실패)
      matches   : [{labels, severity, report_nm, rcept_dt, rcept_no, url, correction}, ...]  (심각 → 주의 순)
      scanned   : 실제로 훑은 공시 건수
      total     : 공시검색이 알려 준 전체 건수(모를 때 None)
      pages     : 조회한 페이지 수
      truncated : 페이지 상한에 걸려 1년치를 다 보지 못했는지
      oldest    : 실제로 훑은 가장 오래된 공시일
      period    : "2025-11-26 ~ 2026-11-26"
      error     : 오류 메시지(있을 때만)

    비재무 정보는 보조 자료이므로, 어떤 이유로 실패하더라도 예외를 던지지 않고
    상태값으로만 알려서 재무 분석이 멈추지 않게 한다.
    """
    end = datetime.now().date()
    begin = end - timedelta(days=days)
    result = {
        "status": "error",
        "matches": [],
        "scanned": 0,
        "total": None,
        "pages": 0,
        "truncated": False,
        "oldest": "",
        "period": "%s ~ %s" % (begin.isoformat(), end.isoformat()),
        "begin": begin.isoformat(),
        "end": end.isoformat(),
        "error": "",
    }

    params = {
        "crtfc_key": api_key,
        "corp_code": corp_code,
        "bgn_de": begin.strftime("%Y%m%d"),
        "end_de": end.strftime("%Y%m%d"),
        "sort": "date",
        "sort_mth": "desc",
        "page_no": "1",
        "page_count": str(DISCLOSURE_PAGE_COUNT),
    }

    rows, page_no = [], 1
    while page_no <= max_pages:
        params["page_no"] = str(page_no)
        try:
            data = call_json("list.json", params)
        except (DartError, RuntimeError, urllib.error.URLError, OSError, ValueError) as error:
            if page_no == 1:                 # 첫 페이지부터 실패하면 '조회 실패'
                result["error"] = str(error)
                return result
            result["error"] = "중간 페이지 조회 실패: %s (조회된 %d건까지만 반영)" % (error, len(rows))
            break
        status = data.get("status")
        if status == "013":                  # "조회된 데이터가 없습니다" = 공시 자체가 없음
            break
        if status != "000":
            if page_no == 1:
                result["error"] = "OpenDART 오류 [%s] %s" % (status, data.get("message", ""))
                return result
            result["error"] = "중간 페이지 오류 [%s] %s" % (status, data.get("message", ""))
            break

        page_rows = data.get("list") or []
        result["pages"] = page_no
        if data.get("total_count") is not None:
            try:
                result["total"] = int(data["total_count"])
            except (TypeError, ValueError):
                pass
        rows.extend(page_rows)
        if page_rows:
            result["oldest"] = rcept_date(page_rows[-1].get("rcept_dt") or "")
        if len(page_rows) < DISCLOSURE_PAGE_COUNT:     # 마지막 페이지
            break
        if data.get("total_page") is not None:
            try:
                if page_no >= int(data["total_page"]):
                    break
            except (TypeError, ValueError):
                pass
        page_no += 1
        time.sleep(0.12)                     # 서버 예의상 짧은 간격
    else:
        result["truncated"] = True           # 페이지 상한에 걸려 1년치를 다 보지 못함

    result["scanned"] = len(rows)

    matches, seen = [], set()
    for row in rows:
        report_nm = row.get("report_nm") or ""
        labels, severity = [], "caution"
        for keyword, label, level in DISCLOSURE_RISK_KEYWORDS:
            if keyword in report_nm:
                labels.append(label)
                if level == "severe":
                    severity = "severe"
        if not labels:
            continue
        rcept_no = (row.get("rcept_no") or "").strip()
        fingerprint = (rcept_no, report_nm)
        if fingerprint in seen:              # 같은 공시가 중복으로 오는 경우 방어
            continue
        seen.add(fingerprint)
        is_correction = ("[기재정정]" in report_nm) or ("[첨부정정]" in report_nm) or ("[첨부추가]" in report_nm)
        matches.append({
            "labels": labels,
            "severity": severity,
            "report_nm": report_nm,
            "rcept_dt": rcept_date(row.get("rcept_dt") or rcept_no),
            "rcept_no": rcept_no,
            "url": dart_link(rcept_no),
            "correction": is_correction,
            # 공시명에 '종속회사'가 있으면 모회사가 아니라 연결 종속회사의 사건일 수 있다.
            "subsidiary": "종속회사" in report_nm,
        })

    # 심각한 건을 먼저, 같은 급에서는 최신순(API가 날짜 내림차순으로 주므로 순서 유지)
    matches.sort(key=lambda item: 0 if item["severity"] == "severe" else 1)
    result["matches"] = matches
    result["corrections"] = sum(1 for item in matches if item["correction"])
    result["status"] = "ok" if matches else "clear"
    return result


def disclosure_coverage(disclosure):
    """공시 위험 탐지가 실제로 얼마나 훑었는지 한 줄로 설명한다(화면·콘솔 공용)."""
    if not disclosure:
        return ""
    scanned, total = disclosure.get("scanned", 0), disclosure.get("total")
    if total and scanned < total:
        text = "전체 %s건 중 최신 %s건 조회" % (format(total, ","), format(scanned, ","))
    elif total:
        text = "공시 %s건 전체 조회" % format(total, ",")
    else:
        text = "공시 %s건 조회" % format(scanned, ",")
    if disclosure.get("oldest"):
        text += " · %s까지" % disclosure["oldest"]
    return text


# ---------------------------------------------------------------------------
# 5. 콘솔 출력 (한글 폭을 고려한 정렬)
# ---------------------------------------------------------------------------

def display_width(text):
    """한글·한자 등 동아시아 넓은 문자는 폭을 2로 계산한다 (콘솔 표 정렬용)."""
    width = 0
    for ch in str(text):
        width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return width


def pad(text, width, align="left"):
    """표시 폭 기준으로 공백을 채운다."""
    text = str(text)
    gap = max(0, width - display_width(text))
    if align == "right":
        return " " * gap + text
    if align == "center":
        left = gap // 2
        return " " * left + text + " " * (gap - left)
    return text + " " * gap


def to_jo(value):
    """원 단위 금액을 '조원' 단위 문자열로 바꾼다."""
    if value is None:
        return "-"
    return "%.1f조" % (value / 1e12)


def pct(value, digits=1):
    """비율을 '%' 붙은 문자열로 바꾼다. 계산 불가면 '-'."""
    if value is None:
        return "-"
    return ("%." + str(digits) + "f%%") % value


def print_console_report(analysis):
    """분석 결과를 콘솔에 보기 좋게 출력한다."""
    companies = analysis["companies"]
    years = analysis["years"]

    print()
    print("=" * 84)
    print(pad("  OpenDART 재무제표 분석 결과", 84, "center"))
    print(pad("  기준: %d~%d 사업보고서 (reprt_code=11011)" % (years[0], years[-1]), 84, "center"))
    print("=" * 84)

    for company in companies:
        print()
        print("■ %s (%s)  ·  %s 기준  ·  접수번호 %s" % (
            company["name"], company["stock_code"] or "-", company["fs_nm"],
            company["latest_rcept_no"] or "-"))
        if dart_link(company["latest_rcept_no"]):
            print("  DART 원문: %s" % dart_link(company["latest_rcept_no"]))
        print("-" * 84)
        header = pad("구분", 10) + pad("계정", 16)
        for year in years:
            header += pad("%d년" % year, 16, "right")
        print(header)
        for key, label in [("revenue", "매출액"), ("op_income", "영업이익"),
                           ("net_income", "당기순이익"), ("assets", "자산총계"),
                           ("liabilities", "부채총계"), ("equity", "자본총계")]:
            line = pad("손익" if key in ("revenue", "op_income", "net_income") else "재무상태", 10)
            line += pad(label, 16)
            for year in years:
                item = company["years"][year]["accounts"].get(key)
                line += pad(to_jo(item["amount"]) if item else "-", 16, "right")
            print(line)

        print()
        print(pad("재무비율", 10) + pad("지표", 26) + "".join(pad("%d년" % y, 16, "right") for y in years))
        for key, label, unit, _group in RATIO_LABELS:
            line = pad("", 10) + pad(label + " (%s)" % unit, 26)
            for year in years:
                value = company["years"][year]["ratios"].get(key)
                text = pct(value)
                if key == "op_growth" and company["years"][year].get("op_growth_base_negative") and value is not None:
                    text += " *"
                line += pad(text, 16, "right")
            print(line)
        if any(c["years"][y].get("op_growth_base_negative") for y in years for c in [company]):
            print("  * 전년 영업이익이 적자였던 해의 증가율은 절대값 기준으로 계산한 참고치입니다.")

        # ---- DuPont 3단계 분해 ----
        print()
        print(pad("[DuPont] ROE = 순이익률 × 총자산회전율 × 재무레버리지", 84))
        if any(company["years"][y]["dupont"].get("roe") is not None for y in years):
            print(pad("  연도", 10) + pad("순이익률", 16, "right") + pad("총자산회전율*", 18, "right")
                  + pad("재무레버리지", 16, "right") + pad("DuPont ROE", 16, "right"))
            for year in years:
                dupont = company["years"][year]["dupont"]
                print(pad("  %d년" % year, 10) + pad(fmt(dupont["net_margin"]), 16, "right")
                      + pad(fmt_turn(dupont["asset_turnover"]), 18, "right")
                      + pad(fmt_mult(dupont["equity_multiplier"]), 16, "right")
                      + pad(fmt(dupont["roe"]), 16, "right"))
            print("  * 총자산회전율 = 매출액 ÷ 기말 자산총계 (기말잔액 기준, 평균잔액 아님)")
        print("  → " + company["dupont_comment"])

        # ---- 운전자본 관리 (현금전환주기) ----
        print()
        print(pad("[운전자본] 현금전환주기 CCC = DSO + DIO − DPO", 84))
        if any((company["years"][y].get("ccc") or {}).get("ccc") is not None for y in years):
            print(pad("  연도", 10) + pad("DSO", 14, "right") + pad("DIO", 14, "right")
                  + pad("DPO", 14, "right") + pad("CCC", 14, "right"))
            for year in years:
                wc = company["years"][year]["ccc"]
                print(pad("  %d년" % year, 10) + pad(fmt_days(wc["dso"]), 14, "right")
                      + pad(fmt_days(wc["dio"]), 14, "right") + pad(fmt_days(wc["dpo"]), 14, "right")
                      + pad(fmt_days(wc["ccc"]), 14, "right"))
            print("  * DSO = 매출채권÷매출액×365, DIO = 재고자산÷매출원가×365, DPO = 매입채무÷매출원가×365 (기말잔액)")
        print("  → " + company["ccc_comment"])

        # ---- 5대 리스크 진단 (신호등) ----
        risk = company.get("risk")
        if risk:
            meta = RISK_META[risk["grade"]]
            print()
            print(pad("[리스크 진단] 종합 %d점 %s %s  (평가 %d/5항목)"
                     % (risk["score"], meta["light"], meta["label"], risk["available_count"]), 84))
            for item in risk["items"]:
                tone = RISK_META[item["state"]]
                line = "  %s %s %s" % (tone["light"], pad(item["label"], 20), pad(item["value_text"], 26, "right"))
                detail = item["note"]
                if detail:
                    line += "  (" + detail + ")"
                print(line)
            print("  → " + risk["comment"])

            # ---- 비재무 공시 위험 키워드 (점수 미반영 · 참고 정보) ----
            disclosure = company.get("disclosure")
            if disclosure:
                coverage = disclosure_coverage(disclosure)
                if disclosure.get("truncated"):
                    coverage += " (페이지 상한으로 일부만 조회)"
                if disclosure["status"] == "clear":
                    print("  [공시 위험] 최근 1년(%s) 주요 위험 공시 없음 🟢  (%s)"
                          % (disclosure["period"], coverage))
                elif disclosure["status"] == "ok":
                    extra = (" (정정공시 %d건 포함)" % disclosure.get("corrections", 0)
                             if disclosure.get("corrections") else "")
                    print("  [공시 위험] 최근 1년(%s) 위험 키워드 공시 %d건%s / %s"
                          % (disclosure["period"], len(disclosure["matches"]), extra, coverage))
                    for match in disclosure["matches"][:10]:
                        light = "🔴" if match["severity"] == "severe" else "🟡"
                        print("    %s %s | %s | %s%s" % (
                            light, "/".join(match["labels"]), match["report_nm"], match["rcept_dt"],
                            "  (종속회사 공시)" if match.get("subsidiary") else ""))
                        if match["url"]:
                            print("       %s" % match["url"])
                    if len(disclosure["matches"]) > 10:
                        print("    … 외 %d건" % (len(disclosure["matches"]) - 10))
                else:
                    print("  [공시 위험] 최근 1년 공시 목록을 조회하지 못했습니다 — %s" % disclosure["error"])
                    print("             (비재무 정보라 재무 분석은 그대로 진행했습니다)")

    # ---- 두 회사 비교 요약 (최근 연도) ----
    if len(companies) == 2:
        a, b = companies
        latest = years[-1]
        print()
        print("=" * 84)
        print(pad("  %d년 요약 비교: %s vs %s" % (latest, a["name"], b["name"]), 84, "center"))
        print("=" * 84)
        print(pad("지표", 28) + pad(a["name"], 24, "right") + pad(b["name"], 24, "right") + pad("우위", 18, "center"))
        rows = [
            ("매출액", lambda c: to_jo(c["years"][latest]["accounts"].get("revenue", {}).get("amount")), None),
            ("영업이익", lambda c: to_jo(c["years"][latest]["accounts"].get("op_income", {}).get("amount")), None),
            ("영업이익률", lambda c: pct(c["years"][latest]["ratios"]["op_margin"]), "op_margin"),
            ("순이익률", lambda c: pct(c["years"][latest]["ratios"]["net_margin"]), "net_margin"),
            ("ROE", lambda c: pct(c["years"][latest]["ratios"]["roe"]), "roe"),
            ("부채비율", lambda c: pct(c["years"][latest]["ratios"]["debt_ratio"]), "debt_ratio"),
            ("유동비율", lambda c: pct(c["years"][latest]["ratios"]["current_ratio"]), "current_ratio"),
            ("매출액 증가율", lambda c: pct(c["years"][latest]["ratios"]["revenue_growth"]), "revenue_growth"),
        ]
        for label, formatter, ratio_key in rows:
            va = a["years"][latest]["ratios"].get(ratio_key) if ratio_key else None
            vb = b["years"][latest]["ratios"].get(ratio_key) if ratio_key else None
            winner = ""
            if va is not None and vb is not None and ratio_key:
                if label == "부채비율":  # 부채비율은 낮을수록 좋다
                    winner = a["name"] if va < vb else (b["name"] if vb < va else "무승부")
                else:
                    winner = a["name"] if va > vb else (b["name"] if vb > va else "무승부")
            print(pad(label, 28) + pad(formatter(a), 24, "right") + pad(formatter(b), 24, "right") + pad(winner, 18, "center"))

        # 리스크 점수는 숫자가 높을수록 안전하므로 별도로 한 줄 더 넣는다.
        risk_a, risk_b = a.get("risk"), b.get("risk")
        if risk_a and risk_b:
            winner = (a["name"] if risk_a["score"] > risk_b["score"]
                      else (b["name"] if risk_b["score"] > risk_a["score"] else "무승부"))
            print(pad("종합 리스크 점수", 28)
                  + pad("%d점 %s" % (risk_a["score"], RISK_META[risk_a["grade"]]["light"]), 24, "right")
                  + pad("%d점 %s" % (risk_b["score"], RISK_META[risk_b["grade"]]["light"]), 24, "right")
                  + pad(winner, 18, "center"))
            print("  · 리스크 점수는 100점 만점이며 높을수록 안전합니다 (85점↑ 🟢 / 60점↑ 🟡 / 60점↓ 🔴).")

        # DuPont·CCC 비교 해석
        if analysis.get("dupont_gap"):
            print()
            print("[DuPont 격차 분해] " + analysis["dupont_gap"]["text"])
        if analysis.get("ccc_gap"):
            print("[운전자본 CCC] " + analysis["ccc_gap"]["text"])

        print("=" * 84)
        print("  · 금액 단위: 조원(1조 = 1,000,000,000,000원). 연결재무제표 기준.")
        print("  · ROE = 당기순이익 ÷ 기말 자본총계, 부채비율 = 부채총계 ÷ 자본총계,")
        print("    유동비율 = 유동자산 ÷ 유동부채. 모두 표준 재무비율 정의를 따릅니다.")
        print("  · DuPont ROE = 순이익률 × 총자산회전율(매출액 ÷ 기말 자산총계) × 재무레버리지(자산총계 ÷ 자본총계).")
        print("  · CCC = DSO(매출채권÷매출액×365) + DIO(재고자산÷매출원가×365) − DPO(매입채무÷매출원가×365), 기말잔액 기준.")
        print("  · 리스크 진단과 공시 위험 탐지는 과거 결산 재무제표·공시 기반 1차 스크리닝입니다.")
        print("    업종 구조 차이(금융업 등)와 향후 시장 변수는 반영되지 않았습니다.")
        print()


# ---------------------------------------------------------------------------
# 6. HTML 대시보드 생성
# ---------------------------------------------------------------------------

PAGE_CSS = """
:root {
  --samsung: #1428a0;
  --hynix: #e8392b;
  --ink: #16202b;
  --muted: #6b7785;
  --line: #e3e8ee;
  --bg: #f5f7fa;
  --card: #ffffff;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  padding: 0 0 64px;
  font-family: "Pretendard", "Apple SD Gothic Neo", "Malgun Gothic", "Noto Sans KR", sans-serif;
  background: var(--bg);
  color: var(--ink);
  line-height: 1.6;
  -webkit-font-smoothing: antialiased;
}
header {
  background: linear-gradient(135deg, #10204a 0%, #1b3a7a 55%, #25519f 100%);
  color: #fff;
  padding: 44px 24px 38px;
}
.wrap { max-width: 1120px; margin: 0 auto; padding: 0 24px; }
header .wrap { padding: 0; }
.eyebrow {
  display: inline-block;
  font-size: 12px;
  letter-spacing: 2px;
  text-transform: uppercase;
  background: rgba(255, 255, 255, 0.16);
  border-radius: 999px;
  padding: 4px 12px;
  margin-bottom: 14px;
}
h1 { font-size: 30px; margin: 0 0 8px; letter-spacing: -0.5px; }
header p { margin: 0; color: #cfdcf7; font-size: 14px; }
header .meta { margin-top: 18px; display: flex; flex-wrap: wrap; gap: 8px; }
header .meta span {
  font-size: 12.5px;
  background: rgba(255, 255, 255, 0.12);
  border: 1px solid rgba(255, 255, 255, 0.2);
  border-radius: 6px;
  padding: 4px 10px;
}
section { margin-top: 36px; }
h2 {
  font-size: 19px;
  margin: 0 0 4px;
  letter-spacing: -0.3px;
}
h2 .num {
  display: inline-block;
  width: 24px; height: 24px; line-height: 24px;
  text-align: center;
  background: var(--samsung);
  color: #fff;
  border-radius: 6px;
  font-size: 13px;
  margin-right: 8px;
  vertical-align: 2px;
}
.sub { color: var(--muted); font-size: 13.5px; margin: 0 0 16px; }
.kpi-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(230px, 1fr));
  gap: 14px;
}
.kpi {
  background: var(--card);
  border: 1px solid var(--line);
  border-radius: 12px;
  padding: 16px 18px;
  box-shadow: 0 1px 2px rgba(16, 32, 74, 0.04);
}
.kpi .name { font-size: 13px; color: var(--muted); display: flex; align-items: center; gap: 6px; }
.kpi .dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
.kpi .value { font-size: 26px; font-weight: 700; letter-spacing: -1px; margin-top: 2px; }
.kpi .note { font-size: 12px; color: var(--muted); }
.kpi .delta { font-size: 12.5px; margin-top: 2px; }
.up { color: #d33a2c; }
.down { color: #1f6fd0; }
.chart-card {
  background: var(--card);
  border: 1px solid var(--line);
  border-radius: 12px;
  padding: 18px 20px 10px;
  box-shadow: 0 1px 2px rgba(16, 32, 74, 0.04);
}
.chart-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(430px, 1fr));
  gap: 16px;
}
.chart-title { font-size: 14.5px; font-weight: 600; margin-bottom: 2px; }
.chart-desc { font-size: 12.5px; color: var(--muted); margin-bottom: 10px; }
canvas { width: 100% !important; }
table {
  width: 100%;
  border-collapse: collapse;
  background: var(--card);
  border: 1px solid var(--line);
  border-radius: 12px;
  overflow: hidden;
  font-size: 13.5px;
}
th, td { padding: 10px 14px; text-align: right; border-bottom: 1px solid var(--line); }
th:first-child, td:first-child { text-align: left; }
thead th { background: #eef2f8; font-weight: 600; color: #33445c; font-size: 12.5px; }
tbody tr:last-child td { border-bottom: none; }
tbody tr:hover { background: #fafbfd; }
td.group { color: var(--muted); font-size: 12.5px; }
td.better { font-weight: 700; }
.tbl-scroll { overflow-x: auto; }
.legend { display: flex; gap: 18px; font-size: 12.5px; color: var(--muted); margin: 10px 0 0; }
.legend i { width: 10px; height: 10px; border-radius: 3px; display: inline-block; margin-right: 6px; }

/* ---- 리스크 진단 ---- */
.risk-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 16px; }
.risk-card {
  background: var(--card);
  border: 1px solid var(--line);
  border-radius: 14px;
  padding: 20px 22px 16px;
  position: relative;
  overflow: hidden;
  box-shadow: 0 1px 2px rgba(16, 32, 74, 0.04);
}
.risk-card::before {
  content: ""; position: absolute; left: 0; top: 0; bottom: 0; width: 6px; background: #cfd7e2;
}
.risk-card.safe::before { background: #2e9e5b; }
.risk-card.caution::before { background: #e8a33d; }
.risk-card.danger::before { background: #d9483b; }
.risk-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; }
.risk-name { font-size: 13.5px; color: var(--muted); margin-bottom: 2px; }
.risk-score { font-size: 40px; font-weight: 800; letter-spacing: -2px; line-height: 1.05; }
.risk-score small { font-size: 13px; font-weight: 600; color: var(--muted); letter-spacing: 0; margin-left: 4px; }
.badge { display: inline-block; border-radius: 999px; padding: 4px 11px; font-size: 12px; font-weight: 700; white-space: nowrap; }
.badge.safe { background: #e6f4ec; color: #1e7a45; }
.badge.caution { background: #fdf1dc; color: #94640f; }
.badge.danger { background: #fbe6e3; color: #a83226; }
.badge.na { background: #eef1f5; color: #6b7785; }
.score-bar { height: 7px; border-radius: 999px; background: #eef1f5; margin: 12px 0 4px; overflow: hidden; }
.score-bar div { height: 100%; border-radius: 999px; }
.risk-comment { font-size: 12.8px; color: #44546a; line-height: 1.65; margin-top: 8px; }
.risk-rows { margin-top: 14px; border-top: 1px solid var(--line); }
.risk-row { display: flex; align-items: center; gap: 8px; padding: 8px 0; border-bottom: 1px dashed var(--line); font-size: 13px; }
.risk-row:last-child { border-bottom: none; }
.risk-row .light { font-size: 13px; }
.risk-row .lbl { flex: 1; color: #33445c; }
.risk-row .val { color: var(--ink); font-weight: 600; font-variant-numeric: tabular-nums; text-align: right; }
.risk-row .val i { display: block; font-style: normal; font-size: 11px; color: #8a95a3; font-weight: 400; }
.risk-note { background: #fff; border: 1px solid var(--line); border-radius: 12px; padding: 16px 20px; font-size: 12.8px; color: #4b5a6d; margin-top: 16px; }
.risk-note strong { color: #33445c; }
.risk-note ul { margin: 8px 0 0; padding-left: 18px; }
.risk-note li { margin: 4px 0; }
.risk-note code { background: #f1f4f8; padding: 1px 5px; border-radius: 4px; font-size: 12px; }

/* ---- DuPont · 운전자본(CCC) ---- */
.link-chip {
  display: inline-flex; align-items: center; gap: 4px;
  font-size: 12px; font-weight: 600; color: var(--samsung); text-decoration: none;
  border: 1px solid #c9d4f5; background: #eef2ff; border-radius: 999px;
  padding: 2px 9px; white-space: nowrap;
}
.link-chip:hover { background: #e2e9ff; }
td a.link-inline { color: var(--samsung); text-decoration: none; font-weight: 600; font-size: 11.5px; }
td a.link-inline:hover { text-decoration: underline; }
.dupont-tree { display: flex; align-items: stretch; gap: 8px; flex-wrap: wrap; margin: 8px 0 10px; }
.dupont-node { flex: 1 1 128px; background: #f7f9fc; border: 1px solid var(--line); border-radius: 10px; padding: 10px 12px; }
.dupont-node .k { font-size: 11.5px; color: var(--muted); }
.dupont-node .v { font-size: 19px; font-weight: 700; letter-spacing: -0.6px; }
.dupont-node .f { font-size: 11px; color: #8a95a3; }
.dupont-node.result { background: #eef2ff; border-color: #c9d4f5; }
.dupont-node.result .v { color: var(--samsung); font-size: 23px; }
.dupont-op { align-self: center; font-size: 16px; font-weight: 700; color: #9aa6b4; }
.verify-line {
  font-size: 12.5px; color: #1e7a45; background: #e6f4ec;
  border-radius: 8px; padding: 6px 10px; display: inline-block; margin-bottom: 8px;
}
.verify-line.bad { color: #a83226; background: #fbe6e3; }
.wc-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 10px; margin: 10px 0 6px; }
.wc-box { background: #f7f9fc; border: 1px solid var(--line); border-radius: 10px; padding: 10px 12px; }
.wc-box .k { font-size: 11.5px; color: var(--muted); }
.wc-box .v { font-size: 20px; font-weight: 700; letter-spacing: -0.6px; }
.wc-box.total { background: #fff8ec; border-color: #f0dcc0; }
.wc-box.total .v { color: #94640f; }
.mini-note { font-size: 12px; color: #7d8794; margin-top: 6px; }
footer {
  margin-top: 40px;
  border-top: 1px solid var(--line);
  padding-top: 18px;
  font-size: 12.5px;
  color: var(--muted);
}
footer ul { margin: 8px 0 0; padding-left: 18px; }
@media (max-width: 640px) {
  h1 { font-size: 22px; }
  .chart-grid { grid-template-columns: 1fr; }
  th, td { padding: 8px 9px; font-size: 12.5px; }
}
"""

# Chart.js 데이터는 파이썬에서 JSON으로 만들어 __DATA__ 자리에 끼워 넣는다.
PAGE_JS = """
const D = __DATA__;

Chart.defaults.font.family = "'Pretendard','Apple SD Gothic Neo','Malgun Gothic',sans-serif";
Chart.defaults.font.size = 12;
Chart.defaults.color = "#5b6877";

const money = v => (v === null || v === undefined) ? "-" : (v / 1e12).toFixed(1) + "조";
const pctText = v => (v === null || v === undefined) ? "-" : v.toFixed(1) + "%";

function barOptions(title, isPercent) {
  return {
    responsive: true,
    maintainAspectRatio: false,
    interaction: { mode: "index", intersect: false },
    plugins: {
      legend: { position: "bottom", labels: { boxWidth: 10, boxHeight: 10, usePointStyle: true } },
      tooltip: {
        callbacks: {
          label: ctx => " " + ctx.dataset.label + ": " +
            (isPercent ? pctText(ctx.parsed.y) : money(ctx.parsed.y) + "원")
        }
      }
    },
    scales: {
      x: { grid: { display: false } },
      y: {
        beginAtZero: true,
        grid: { color: "#eef1f5" },
        ticks: { callback: v => isPercent ? v + "%" : (v / 1e12).toFixed(0) + "조" }
      }
    }
  };
}

function lineOptions(isPercent) {
  const opt = barOptions("", true);
  opt.plugins.tooltip.callbacks.label = ctx => " " + ctx.dataset.label + ": " + pctText(ctx.parsed.y);
  opt.scales.y.ticks.callback = v => v + "%";
  opt.elements = { line: { tension: 0.28, borderWidth: 2.4 }, point: { radius: 3, hoverRadius: 5 } };
  return opt;
}

function renderBar(canvasId, labels, series, isPercent) {
  new Chart(document.getElementById(canvasId), {
    type: "bar",
    data: { labels, datasets: series.map(s => ({
      label: s.label, data: s.data, backgroundColor: s.color, borderRadius: 4, barPercentage: 0.68
    })) },
    options: barOptions("", isPercent)
  });
}

function renderLine(canvasId, labels, series) {
  new Chart(document.getElementById(canvasId), {
    type: "line",
    data: { labels, datasets: series.map(s => ({
      label: s.label, data: s.data, borderColor: s.color,
      backgroundColor: s.color + "22", fill: false, borderDash: s.dash ? [6, 4] : []
    })) },
    options: lineOptions(true)
  });
}

// 일(day) 단위 라인 차트 — CCC 추이용
function renderDays(canvasId, labels, series) {
  const opt = lineOptions(true);
  opt.plugins.tooltip.callbacks.label = ctx => " " + ctx.dataset.label + ": " +
    (ctx.parsed.y === null || ctx.parsed.y === undefined ? "-" : ctx.parsed.y.toFixed(0) + "일");
  opt.scales.y.ticks.callback = v => v + "일";
  opt.scales.y.beginAtZero = false;
  new Chart(document.getElementById(canvasId), {
    type: "line",
    data: { labels, datasets: series.map(s => ({
      label: s.label, data: s.data, borderColor: s.color,
      backgroundColor: s.color + "22", fill: false, spanGaps: true
    })) },
    options: opt
  });
}

// ROE 격차 요인 기여도(%p) 가로 막대 — 0 기준선을 가운데 둔다
function renderGap(canvasId, gap) {
  new Chart(document.getElementById(canvasId), {
    type: "bar",
    data: {
      labels: gap.labels,
      datasets: [{
        label: "ROE 기여도",
        data: gap.values,
        backgroundColor: gap.colors,
        borderRadius: 4,
        barPercentage: 0.55
      }]
    },
    options: {
      indexAxis: "y",
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: { callbacks: { label: ctx => " " + ctx.parsed.x.toFixed(1) + "%p" } }
      },
      scales: {
        x: { grid: { color: "#eef1f5" }, ticks: { callback: v => v + "%p" } },
        y: { grid: { display: false } }
      }
    }
  });
}

// 적재 막대 — DSO + DIO − DPO = CCC (막대 전체 높이가 CCC)
function renderStacked(canvasId, labels, series) {
  new Chart(document.getElementById(canvasId), {
    type: "bar",
    data: {
      labels,
      datasets: series.map(s => ({
        label: s.label, data: s.data, backgroundColor: s.color,
        borderRadius: 3, stack: "ccc", barPercentage: 0.68
      }))
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: { position: "bottom", labels: { boxWidth: 10, boxHeight: 10, usePointStyle: true } },
        tooltip: {
          callbacks: {
            label: ctx => " " + ctx.dataset.label + ": " +
              (ctx.parsed.y === null || ctx.parsed.y === undefined ? "-" :
               (ctx.parsed.y < 0 ? "−" : "") + Math.abs(ctx.parsed.y).toFixed(0) + "일")
          }
        }
      },
      scales: {
        x: { stacked: true, grid: { display: false }, ticks: { maxRotation: 45, minRotation: 0 } },
        y: { stacked: true, grid: { color: "#eef1f5" }, ticks: { callback: v => v + "일" } }
      }
    }
  });
}

// 캔버스가 없는 경우(1개사 분석 · 데이터없음)는 그리지 않는다
function mount(id, draw) {
  if (document.getElementById(id)) draw();
}

mount("chartRevenue", () => renderBar("chartRevenue", D.years, D.series.revenue, false));
mount("chartProfit", () => renderBar("chartProfit", D.years, D.series.op_income, false));
mount("chartMargin", () => renderLine("chartMargin", D.years, D.series.margins));
mount("chartStability", () => renderLine("chartStability", D.years, D.series.stability));
mount("chartDupont", () => renderLine("chartDupont", D.years, D.series.dupont_roe));
mount("chartCCC", () => renderDays("chartCCC", D.years, D.series.ccc));
mount("chartCCCStack", () => renderStacked("chartCCCStack", D.ccc_stack.labels, D.ccc_stack.series));
if (D.dupont_gap) mount("chartDupontGap", () => renderGap("chartDupontGap", D.dupont_gap));
"""


def build_chart_data(analysis):
    """Chart.js에 넘길 데이터 묶음을 만든다."""
    years = [str(y) for y in analysis["years"]]
    colors = ["#1428a0", "#e8392b", "#0f9d58"]

    def series_for(getter):
        return [
            {"label": c["name"], "color": colors[i % len(colors)],
             "data": [getter(c, y) for y in analysis["years"]]}
            for i, c in enumerate(analysis["companies"])
        ]

    def acc(key):
        return lambda c, y: c["years"][y]["accounts"].get(key, {}).get("amount")

    def ratio(key):
        return lambda c, y: c["years"][y]["ratios"].get(key)

    return {
        "years": years,
        "series": {
            "revenue": series_for(acc("revenue")),
            "op_income": series_for(acc("op_income")),
            # 영업이익률(실선)과 순이익률(점선 느낌으로 같은 색 옅게)을 함께 보여준다
            "margins": [
                {"label": c["name"] + " 영업이익률", "color": colors[i % len(colors)],
                 "data": [c["years"][y]["ratios"].get("op_margin") for y in analysis["years"]]}
                for i, c in enumerate(analysis["companies"])
            ] + [
                {"label": c["name"] + " 순이익률", "color": colors[i % len(colors)], "dash": True,
                 "data": [c["years"][y]["ratios"].get("net_margin") for y in analysis["years"]]}
                for i, c in enumerate(analysis["companies"])
            ],
            "stability": [
                {"label": c["name"] + " 부채비율", "color": colors[i % len(colors)],
                 "data": [c["years"][y]["ratios"].get("debt_ratio") for y in analysis["years"]]}
                for i, c in enumerate(analysis["companies"])
            ] + [
                {"label": c["name"] + " 유동비율", "color": colors[i % len(colors)], "dash": True,
                 "data": [c["years"][y]["ratios"].get("current_ratio") for y in analysis["years"]]}
                for i, c in enumerate(analysis["companies"])
            ],
            # DuPont ROE 추이 (순이익율 × 총자산회전율 × 재무레버리지 로 재계산한 값)
            "dupont_roe": [
                {"label": c["name"] + " ROE", "color": colors[i % len(colors)],
                 "data": [c["years"][y]["dupont"].get("roe") for y in analysis["years"]]}
                for i, c in enumerate(analysis["companies"])
            ],
            # CCC 추이 (일)
            "ccc": [
                {"label": c["name"] + " CCC", "color": colors[i % len(colors)],
                 "data": [(c["years"][y].get("ccc") or {}).get("ccc") for y in analysis["years"]]}
                for i, c in enumerate(analysis["companies"])
            ],
        },
        "ccc_stack": build_ccc_stack(analysis),
        "dupont_gap": build_dupont_gap_chart(analysis),
    }


def build_ccc_stack(analysis):
    """CCC 구성요소를 적재 막대(DSO + DIO − DPO)로 보여줄 데이터."""
    labels, dso, dio, dpo = [], [], [], []
    for year in analysis["years"]:
        for company in analysis["companies"]:
            wc = company["years"][year].get("ccc") or {}
            labels.append("%s %d년" % (company["name"], year))
            dso.append(wc.get("dso"))
            dio.append(wc.get("dio"))
            # DPO는 CCC에서 빼는 항목이므로 음수로 그려 적재 높이가 CCC가 되게 한다
            dpo.append(None if wc.get("dpo") is None else -wc["dpo"])
    return {
        "labels": labels,
        "series": [
            {"label": "DSO (매출채권 회수)", "color": "#4a78c8", "data": dso},
            {"label": "DIO (재고 보유)", "color": "#9bb2e0", "data": dio},
            {"label": "−DPO (매입채무 지급)", "color": "#e8a33d", "data": dpo},
        ],
    }


def build_dupont_gap_chart(analysis):
    """두 기업의 ROE 격차를 요인별 기여도(%p) 막대로 보여줄 데이터."""
    gap = analysis.get("dupont_gap")
    if not gap:
        return None
    labels = [row["label"] for row in gap["rows"]]
    values = [round(row["contribution"], 1) for row in gap["rows"]]
    colors = ["#d9483b" if value >= 0 else "#1f6fd0" for value in values]
    return {
        "labels": labels,
        "values": values,
        "colors": colors,
        "title": "%s → %s ROE 격차(%+.1f%%p)의 요인별 기여도" % (
            gap["a"], gap["b"], gap["total"]),
        "unit": "%p",
        "positive": "#d9483b",
        "negative": "#1f6fd0",
    }


def build_insights(analysis):
    """
    최근 연도 수치를 근거로 '한눈에 보는 결론' 문장을 자동 생성한다.
    2개사 비교일 때만 만들고, 값이 없으면 해당 문장은 건너뛴다.
    반환값: [(제목, 문장), ...]  — 모든 문장은 아래 계산값에서만 나오므로 숫자가 틀어질 일이 없다.
    """
    companies = analysis["companies"]
    if len(companies) != 2:
        return []
    a, b = companies
    latest = analysis["years"][-1]
    ra, rb = a["years"][latest]["ratios"], b["years"][latest]["ratios"]
    aa, ab = a["years"][latest]["accounts"], b["years"][latest]["accounts"]
    insights = []

    def amount(acc, key):
        return acc.get(key, {}).get("amount")

    # --- 1) 규모: 매출액 ---
    rev_a, rev_b = amount(aa, "revenue"), amount(ab, "revenue")
    if rev_a and rev_b:
        bigger, smaller = (a, b) if rev_a >= rev_b else (b, a)
        multiple = max(rev_a, rev_b) / min(rev_a, rev_b)
        insights.append(("외형", "%d년 매출액은 %s가 %s의 %.1f배 규모입니다. (%s %s vs %s %s)" % (
            latest, bigger["name"], smaller["name"], multiple,
            a["name"], to_jo(rev_a), b["name"], to_jo(rev_b))))

    # --- 2) 수익성: 영업이익률 ---
    if ra.get("op_margin") is not None and rb.get("op_margin") is not None:
        better, worse = (a, b) if ra["op_margin"] >= rb["op_margin"] else (b, a)
        gap = abs(ra["op_margin"] - rb["op_margin"])
        insights.append(("수익성", "영업이익률은 %s가 %s보다 %.1f%%p 높습니다. 매출 100원당 남는 본업 이익이 그만큼 차이 납니다. (%s %s vs %s %s)" % (
            better["name"], worse["name"], gap,
            a["name"], pct(ra["op_margin"]), b["name"], pct(rb["op_margin"]))))

    # --- 3) 안정성: 부채비율·유동비율 ---
    if ra.get("debt_ratio") is not None and rb.get("debt_ratio") is not None:
        safer = a if ra["debt_ratio"] <= rb["debt_ratio"] else b
        other = b if safer is a else a
        sentence = "부채비율은 %s가 %s로 %s(%s)보다 낮아 재무구조가 더 안정적입니다." % (
            safer["name"], pct(min(ra["debt_ratio"], rb["debt_ratio"])),
            other["name"], pct(max(ra["debt_ratio"], rb["debt_ratio"])))
        if ra.get("current_ratio") is not None and rb.get("current_ratio") is not None:
            liquid = a if ra["current_ratio"] >= rb["current_ratio"] else b
            sentence += " 유동비율도 %s가 %s로 단기 지급능력이 더 낫습니다." % (
                liquid["name"], pct(max(ra["current_ratio"], rb["current_ratio"])))
        insights.append(("안정성", sentence))

    # --- 4) 성장성: 3개년 영업이익률 변화폭 ---
    first_year = analysis["years"][0]
    if len(analysis["years"]) >= 2:
        first_a = a["years"][first_year]["ratios"].get("op_margin")
        first_b = b["years"][first_year]["ratios"].get("op_margin")
        if None not in (first_a, first_b, ra.get("op_margin"), rb.get("op_margin")):
            delta_a = ra["op_margin"] - first_a
            delta_b = rb["op_margin"] - first_b
            faster = a if delta_a >= delta_b else b
            insights.append(("성장성", "%d년 → %d년 영업이익률 개선폭은 %s가 %.1f%%p로 %s(%.1f%%p)보다 컸습니다. 업황 회복을 누가 더 크게 흡수했는지 보여줍니다." % (
                first_year, latest, faster["name"], max(delta_a, delta_b),
                (b if faster is a else a)["name"], min(delta_a, delta_b))))
    # --- 5) DuPont 분해: 두 회사의 ROE 격차가 어디서 나오는지 ---
    gap = analysis.get("dupont_gap")
    if gap:
        insights.append(("DuPont ROE", gap["text"]))

    # --- 6) 운전자본 관리: CCC 비교 ---
    ccc_gap = analysis.get("ccc_gap")
    if ccc_gap:
        insights.append(("운전자본 CCC", ccc_gap["text"]))

    # --- 7) 리스크 진단 결과 요약 ---
    risk_a, risk_b = a.get("risk"), b.get("risk")
    if risk_a and risk_b:
        if risk_a["score"] == risk_b["score"]:
            verdict = "두 회사 모두 %d점으로 같은 등급입니다" % risk_a["score"]
        else:
            safer = a if risk_a["score"] > risk_b["score"] else b
            other = b if safer is a else a
            safer_risk = risk_a if safer is a else risk_b
            other_risk = risk_b if safer is a else risk_a
            verdict = "%s가 %d점으로 %s(%d점)보다 리스크가 낮습니다" % (
                safer["name"], safer_risk["score"], other["name"], other_risk["score"])
        insights.append(("리스크", "5대 리스크 진단 결과 %s %d점 %s, %s %d점 %s입니다. %s. "
                         "(점수가 높을수록 안전 — 85점↑ 🟢 / 60점↑ 🟡 / 60점↓ 🔴)" % (
                             a["name"], risk_a["score"], RISK_META[risk_a["grade"]]["label"],
                             b["name"], risk_b["score"], RISK_META[risk_b["grade"]]["label"], verdict)))

    # --- 8) 비재무 공시 위험 ---
    flagged = []
    for company in companies:
        disclosure = company.get("disclosure") or {}
        if disclosure.get("status") == "ok":
            severe = sum(1 for match in disclosure["matches"] if match["severity"] == "severe")
            flagged.append("%s %d건(심각 %d건)" % (company["name"], len(disclosure["matches"]), severe))
    if flagged:
        insights.append(("공시 위험", "최근 1년 공시에서 위험 키워드가 잡혔습니다 — " + ", ".join(flagged) +
                         ". 원문을 확인해 재무제표·주석에 어떻게 반영됐는지 보세요(공시 위험은 점수에 반영하지 않습니다)."))
    return insights


def html_escape(text):
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def build_html(analysis):
    """분석 결과를 단독 실행 가능한 HTML 문서 문자열로 만든다."""
    companies = analysis["companies"]
    years = analysis["years"]
    latest = years[-1]
    colors = ["#1428a0", "#e8392b", "#0f9d58"]
    generated = datetime.now().strftime("%Y-%m-%d %H:%M")
    title = " vs ".join(c["name"] for c in companies)

    parts = []
    parts.append("<!DOCTYPE html>")
    parts.append('<html lang="ko"><head><meta charset="utf-8">')
    parts.append('<meta name="viewport" content="width=device-width, initial-scale=1">')
    parts.append("<title>%s 재무제표 비교 분석</title>" % html_escape(title))
    parts.append("<style>%s</style>" % PAGE_CSS)
    parts.append('<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>')
    parts.append("</head><body>")

    # ---------- 헤더 ----------
    parts.append("<header><div class=\"wrap\">")
    parts.append('<span class="eyebrow">OpenDART Financial Analysis</span>')
    parts.append("<h1>%s 재무제표 비교 분석</h1>" % html_escape(title))
    parts.append("<p>금융감독원 전자공시(DART) 사업보고서의 연결재무제표를 자동 수집해 핵심 재무비율을 계산한 결과입니다.</p>")
    parts.append('<div class="meta">')
    parts.append("<span>분석 기간 %d ~ %d 사업연도</span>" % (years[0], years[-1]))
    parts.append("<span>지표: 재무비율 · 5대 리스크 진단 · DuPont ROE · CCC</span>")
    parts.append("<span>출처: 사업보고서 연결재무제표 · OpenDART</span>")
    parts.append("<span>생성 %s</span>" % generated)
    parts.append("</div></div></header>")

    parts.append('<div class="wrap">')

    # ---------- 0. 한눈에 보는 결론 (자동 생성) ----------
    insights = build_insights(analysis)
    if insights:
        parts.append("<section>")
        parts.append('<h2><span class="num">0</span>한눈에 보는 결론</h2>')
        parts.append('<p class="sub">%d년 수치를 기준으로 프로그램이 자동 도출한 요약입니다.</p>' % latest)
        parts.append('<div class="chart-card"><table style="border:none"><tbody>')
        for label, text in insights:
            parts.append('<tr><td style="width:86px;font-weight:700;color:#1428a0">%s</td><td style="text-align:left;color:#33445c">%s</td></tr>'
                         % (label, html_escape(text)))
        parts.append("</tbody></table></div></section>")

    # ---------- 1. 5대 재무 리스크 진단 ----------
    parts.append("<section>")
    parts.append('<h2><span class="num">1</span>5대 재무 리스크 진단</h2>')
    parts.append('<p class="sub">%d년 연결재무제표 기준 · 항목당 20점씩 총 100점 만점(높을수록 안전)입니다. '
                 '판정이 불가한 항목(N/A)은 분모에서 빼고 남은 항목으로 환산합니다.</p>' % latest)
    # ★ 반드시 먼저 읽여야 하는 한계 안내
    parts.append('<div class="risk-note" style="border-color:#f0dcc0;background:#fffaf0;margin-top:0;margin-bottom:16px">')
    parts.append("<strong>⚠ 이 평가를 읽는 법 — 한계와 전제</strong>")
    parts.append("<ul>")
    parts.append("<li>이 진단은 <b>과거 결산 재무제표 기반의 1차 건전성 스크리닝</b>입니다. "
                 "미래 실적을 예측하는 모형이 아니며, 점수는 ‘의심 지점을 빨리 찾는 출발점’으로 쓰는 것이 맞습니다.</li>")
    parts.append("<li><b>업종별 구조 차이를 반영하지 않습니다.</b> 금융업(은행·보험)은 유동·비유동 구분이 없어 유동비율이 N/A가 되고, "
                 "건설·조선처럼 선수금·계약부채가 큰 업종은 부채비율이 실제보다 높게 보입니다. "
                 "장치산업(반도체·철강)은 감가상각 부담 때문에 같은 수익성이라도 ROA가 낮게 나오는 경향이 있습니다.</li>")
    parts.append("<li>금리·환율·업황 같은 <b>향후 시장 변수</b>, 회계정책 변경·일회성 손익, 우발부채의 성격은 반영되지 않았습니다. "
                 "같은 등급 안의 미세한 점수 차이도 의미를 두기 어렵습니다.</li>")
    parts.append("<li>따라서 <b>투자·여신 판단의 단독 근거가 아닙니다.</b> 업종 비교군, 공시 원문(아래 링크), 최근 분기 실적과 함께 검토하세요.</li>")
    parts.append("</ul></div>")
    parts.append('<div class="risk-grid">')
    for index, company in enumerate(companies):
        risk = company["risk"]
        meta = RISK_META[risk["grade"]]
        bar_color = {"safe": "#2e9e5b", "caution": "#e8a33d", "danger": "#d9483b"}[risk["grade"]]
        parts.append('<div class="risk-card %s">' % meta["class"])
        parts.append('<div class="risk-head"><div>')
        parts.append('<div class="risk-name">%s · %d년</div>' % (html_escape(company["name"]), latest))
        parts.append('<div class="risk-score">%d<small>점 / 100</small></div>' % risk["score"])
        parts.append('</div><span class="badge %s">%s %s</span></div>' % (meta["class"], meta["light"], meta["label"]))
        parts.append('<div class="score-bar"><div style="width:%d%%;background:%s"></div></div>'
                     % (risk["score"], bar_color))
        parts.append('<div class="risk-comment">%s</div>' % html_escape(risk["comment"]))
        if any(item["key"] == "current_ratio" and item["state"] == "na" for item in risk["items"]):
            parts.append('<div class="risk-comment" style="color:#a83226">⚠ 유동·비유동 구분이 없는 업종(은행·보험 등)은 '
                         '부채비율 판정이 실제보다 나쁘게 나올 수 있습니다. 점수를 그대로 해석하지 마세요.</div>')
        parts.append('<div class="risk-rows">')
        for item in risk["items"]:
            tone = RISK_META[item["state"]]
            parts.append('<div class="risk-row"><span class="light">%s</span><span class="lbl">%s</span>'
                         '<span class="val">%s%s</span></div>'
                         % (tone["light"], html_escape(item["label"]), html_escape(item["value_text"]),
                            ("<i>%s</i>" % html_escape(item["note"])) if item["note"] else ""))
        parts.append("</div></div>")
    parts.append("</div>")

    # 항목별 나란히 비교표
    parts.append('<div class="tbl-scroll" style="margin-top:18px"><table><thead><tr>'
                 '<th>진단 항목</th><th>판정 기준</th>')
    for company in companies:
        parts.append("<th>%s</th>" % html_escape(company["name"]))
    parts.append("</tr></thead><tbody>")
    for position in range(5):
        first = companies[0]["risk"]["items"][position]
        parts.append("<tr><td>%s</td><td class=\"group\" style=\"text-align:left\">%s</td>"
                     % (html_escape(first["label"]), html_escape(first["criteria"])))
        for company in companies:
            item = company["risk"]["items"][position]
            tone = RISK_META[item["state"]]
            parts.append("<td><span class=\"light\">%s</span> %s <span class=\"badge %s\">%s</span></td>"
                         % (tone["light"], html_escape(item["value_text"]), tone["class"], tone["label"]))
        parts.append("</tr>")
    parts.append("</tbody></table></div>")

    # ---- 비재무 공시 위험 (최근 1년 공시검색 결과 · 점수 미반영) ----
    parts.append('<div style="margin-top:20px">')
    parts.append('<div class="chart-title" style="font-size:15px">비재무 공시 위험 키워드 <span style="font-weight:400;color:#8a95a3;font-size:12.5px">·'
                 '최근 1년 공시검색(list.json) · 점수에는 반영하지 않는 참고 정보</span></div>')
    parts.append('<div class="chart-desc">소송·횡령·배임·감사의견·상장폐지·관리종목·불성실공시·유상증자·감자 같은 '
                 '위험 키워드가 공시명(report_nm)에 들어 있는지 보고, 건수와 최근 공시 제목을 원문 링크와 함께 보여줍니다. '
                 '재무제표에 아직 드러나지 않은 사건을 빨리 찾기 위한 장치입니다.</div>')
    parts.append('<div class="risk-grid">')
    for company in companies:
        disclosure = company.get("disclosure") or {}
        status = disclosure.get("status")
        matches = disclosure.get("matches") or []
        parts.append('<div class="risk-card %s">' % (
            "safe" if status == "clear" else ("danger" if any(m["severity"] == "severe" for m in matches)
                                               else ("caution" if matches else ""))))
        parts.append('<div class="risk-head"><div>')
        parts.append('<div class="risk-name">%s</div>' % html_escape(company["name"]))
        if status == "clear":
            parts.append('<div class="risk-score" style="font-size:22px;letter-spacing:-0.5px">🟢 최근 1년 주요 위험 공시 없음</div>')
            parts.append('<div class="risk-comment">%s · %s%s</div>'
                         % (html_escape(disclosure.get("period", "")), html_escape(disclosure_coverage(disclosure)),
                            " (페이지 상한으로 일부만 조회)" if disclosure.get("truncated") else ""))
        elif status == "ok":
            severe = sum(1 for match in matches if match["severity"] == "severe")
            caution = len(matches) - severe
            parts.append('<div class="risk-score" style="font-size:24px;letter-spacing:-0.6px">위험 키워드 %d건</div>' % len(matches))
            parts.append('<div class="risk-comment">%s · 심각 %d건 / 주의 %d건%s<br>%s</div>'
                         % (html_escape(disclosure.get("period", "")), severe, caution,
                            (" · 정정공시 %d건 포함" % disclosure["corrections"]) if disclosure.get("corrections") else "",
                            html_escape(disclosure_coverage(disclosure))))
        else:
            parts.append('<div class="risk-score" style="font-size:20px;letter-spacing:-0.4px;color:#8a95a3">조회 실패 — N/A</div>')
            parts.append('<div class="risk-comment">공시 목록을 가져오지 못했습니다. 재무 분석에는 영향이 없습니다. '
                         '<br>사유: %s</div>' % html_escape(disclosure.get("error", "알 수 없음")))
        parts.append('</div>')
        if status == "ok":
            badge_class = "danger" if any(m["severity"] == "severe" for m in matches) else "caution"
            parts.append('<span class="badge %s">확인 필요</span>' % badge_class)
        parts.append("</div>")  # risk-head
        if status == "ok":
            parts.append('<div class="risk-rows">')
            for match in matches[:8]:
                light = "🔴" if match["severity"] == "severe" else "🟡"
                title = html_escape(match["report_nm"])
                if match["url"]:
                    title = ('<a href="%s" target="_blank" rel="noopener" style="color:#1f6fd0;text-decoration:none">%s</a>'
                             % (html_escape(match["url"]), title))
                if match.get("subsidiary"):
                    title += ' <span style="color:#8a95a3;font-size:11px">(종속회사)</span>'
                parts.append('<div class="risk-row"><span class="light">%s</span>'
                             '<span class="lbl" style="flex:2">%s</span>'
                             '<span class="val">%s<i>%s</i></span></div>' % (
                                 light, title, html_escape("/".join(match["labels"])), match["rcept_dt"]))
            if len(matches) > 8:
                parts.append('<div class="risk-row"><span class="lbl" style="color:#8a95a3">… 외 %d건은 DART 공시 목록에서 확인</span></div>'
                             % (len(matches) - 8))
            parts.append("</div>")
        parts.append("</div>")  # risk-card
    parts.append("</div>")
    parts.append('<div class="mini-note">탐지 기준: 최근 365일 공시 제목에 ' + ", ".join(
        "‘%s’" % label for _keyword, label, _level in DISCLOSURE_RISK_KEYWORDS) +
        " 포함 여부. 키워드만 보는 단순 필터라 업종·규모별 맥락은 반영하지 않으니, 잡힌 공시는 원문으로 확인하세요. "
        "같은 사건이 [기재정정] 등으로 여러 번 공시되면 건수가 늘어나므로, 건수보다 내용을 보세요.</div>")
    parts.append("</div>")

    # 계산 기준 안내 (지시받은 대로 산식을 명시)
    parts.append('<div class="risk-note">')
    parts.append("<strong>계산 기준</strong>")
    parts.append("<ul>")
    parts.append("<li><b>이자보상배율</b> = 영업이익 ÷ 이자비용. 이자비용 계정이 따로 없으면 "
                 "공시된 <code>금융비용</code>을 사용했으며(카드에 계정명 표시), 둘 다 없으면 N/A입니다.</li>")
    parts.append("<li><b>현금흐름 건전성</b> = 현금흐름표(CF)의 영업활동현금흐름과 당기순이익 비교. "
                 "순이익 흑자인데 영업현금흐름이 적자면 이익의 질을 의심해 위험으로 봅니다.</li>")
    parts.append("<li><b>연속 적자·부실 예측</b>은 두 가지를 함께 봅니다. "
                 "① 최근 연도부터 거슬러 올라간 연속 영업적자 연수(3년 연속이면 즉시 위험) "
                 "② Altman Z''-Score(장부가 기준): "
                 "<code>6.56·X1 + 3.26·X2 + 6.72·X3 + 1.05·X4</code>. "
                 "X1=운전자본/자산총계, X2=이익잉여금/자산총계, X3=영업이익/자산총계, X4=자본총계/부채총계(장부가). "
                 "2.6 이상 안전, 1.1~2.6 회색, 1.1 미만 부실 구간으로 읽습니다.</li>")
    parts.append("<li>종합점수 = (획득점수 합 ÷ 평가가능 항목 만점 합) × 100. "
                 "85점 이상 🟢 안전, 60점 이상 🟡 주의, 60점 미만 🔴 위험입니다.</li>")
    parts.append("<li><b>비재무 공시 위험</b>은 공시검색 API(<code>list.json</code>)로 최근 365일 공시 제목에서 "
                 "<code>소송·횡령·배임·감사의견·한정·의견거절·상장폐지·관리종목·불성실공시·유상증자·감자·부도·회생</code> "
                 "키워드를 찾는 단순 필터입니다. 조회 실패 시 N/A로 표시되고 점수에는 반영하지 않습니다. "
                 "키워드가 잡히지 않았다고 위험이 없다는 뜻은 아닙니다(공시명이 항상 사건을 드러내지 않음).</li>")
    parts.append("<li>시가총액을 쓰지 않는 Z'' 변형이라 상장·비상장, 업종 구분 없이 같은 기준으로 비교할 수 있습니다. "
                 "다만 금융회사처럼 유동/비유동 구분이 없는 재무제표는 일부 항목이 N/A로 빠집니다.</li>")
    parts.append("</ul></div></section>")

    # ---------- 2. DuPont ROE 3단계 분해 ----------
    def dupont_node(label, value, formula, extra_class=""):
        return ('<div class="dupont-node %s"><div class="k">%s</div><div class="v">%s</div>'
                '<div class="f">%s</div></div>' % (extra_class, label, value, formula))

    dupont_unit = {
        "profitability": lambda value: fmt(value),
        "efficiency": lambda value: fmt_turn(value),
        "leverage": lambda value: fmt_mult(value),
    }
    dupont_delta_unit = {
        "profitability": lambda value: "%+.1f%%p" % value if value is not None else "-",
        "efficiency": lambda value: "%+.2f회" % value if value is not None else "-",
        "leverage": lambda value: "%+.2f배" % value if value is not None else "-",
    }

    parts.append("<section>")
    parts.append('<h2><span class="num">2</span>DuPont ROE 3단계 분해</h2>')
    parts.append('<p class="sub">ROE를 <b>수익성(순이익률) × 자산효율성(총자산회전율) × 재무레버리지</b>로 쪼개 '
                 '"이익을 어디서 만들어냈는지"를 봅니다. 총자산회전율의 분모는 <b>기말 자산총계</b>입니다'
                 '(기초·기말 평균잔액이 아님).</p>')
    parts.append('<div class="chart-grid">')
    for company in companies:
        dupont = company["years"][latest]["dupont"]
        parts.append('<div class="chart-card">')
        parts.append('<div class="chart-title">%s · %d년</div>' % (html_escape(company["name"]), latest))
        parts.append('<div class="dupont-tree">')
        parts.append(dupont_node("순이익률", fmt(dupont["net_margin"]), "당기순이익 ÷ 매출액"))
        parts.append('<div class="dupont-op">×</div>')
        parts.append(dupont_node("총자산회전율", fmt_turn(dupont["asset_turnover"]), "매출액 ÷ 기말 자산총계"))
        parts.append('<div class="dupont-op">×</div>')
        parts.append(dupont_node("재무레버리지", fmt_mult(dupont["equity_multiplier"]), "자산총계 ÷ 자본총계"))
        parts.append('<div class="dupont-op">=</div>')
        parts.append(dupont_node("DuPont ROE", fmt(dupont["roe"]), "기존 ROE와 검산", "result"))
        parts.append("</div>")
        if dupont.get("roe") is None:
            parts.append('<div class="verify-line" style="color:#6b7785;background:#eef1f5">계산 불가 — 매출액·당기순이익·자산총계·자본총계 중 일부가 공시되지 않았습니다</div>')
        elif dupont.get("diff") is not None and abs(dupont["diff"]) < 0.05:
            parts.append('<div class="verify-line">✓ 검산 일치 — DuPont ROE %s = 기존 단순 ROE(당기순이익 ÷ 기말 자본총계) %s '
                         '· 차이 %s</div>'
                         % (fmt(dupont["roe"]), fmt(dupont["roe_simple"]), fmt_diff(dupont["diff"])))
        else:
            parts.append('<div class="verify-line bad">△ 검산 차이 — DuPont ROE %s vs 기존 ROE %s. 계정 누락 여부를 확인하세요.</div>'
                         % (fmt(dupont["roe"]), fmt(dupont["roe_simple"])))
        parts.append('<div class="risk-comment">%s</div>' % html_escape(company.get("dupont_comment", "")))
        parts.append("</div>")
    parts.append("</div>")

    # ROE 격차 요인 분해 (2개사 비교일 때만)
    gap = analysis.get("dupont_gap")
    if gap:
        parts.append('<div class="chart-card" style="margin-top:16px">')
        parts.append('<div class="chart-title">어디서 갈렸나 — %d년 ROE 격차 %+.1f%%p의 요인 분해</div>' % (latest, gap["total"]))
        parts.append('<div class="chart-desc">순차 대체법: %s의 요인을 하나씩 %s의 값으로 바꾸며 기여도를 누적했습니다 '
                     '(세 기여도의 합 = ROE 격차). 어떤 요인을 먼저 바꾸느냐에 따라 개별 기여도는 조금 달라질 수 있습니다.</div>'
                     % (html_escape(gap["a"]), html_escape(gap["b"])))
        parts.append('<div class="risk-comment" style="font-size:13.5px;color:#33445c">%s</div>' % html_escape(gap["text"]))
        parts.append('<div class="tbl-scroll" style="margin-top:12px"><table><thead><tr>'
                     '<th>요인</th><th>%s</th><th>%s</th><th>차이</th><th>ROE 기여도</th>'
                     '</tr></thead><tbody>' % (html_escape(gap["a"]), html_escape(gap["b"])))
        for row in gap["rows"]:
            parts.append("<tr><td>%s</td><td>%s</td><td>%s</td><td class=\"group\">%s</td>"
                         "<td class=\"better\">%+.1f%%p</td></tr>" % (
                             html_escape(row["label"]), dupont_unit[row["key"]](row["a"]),
                             dupont_unit[row["key"]](row["b"]), dupont_delta_unit[row["key"]](row["delta"]),
                             row["contribution"]))
        parts.append('<tr><td><b>합계</b></td><td>ROE %s</td><td>ROE %s</td><td class="group">%+.1f%%p</td>'
                     '<td class="better">%+.1f%%p</td></tr>' % (
                         fmt(gap["roe_a"]), fmt(gap["roe_b"]), gap["total"], gap["total"]))
        parts.append("</tbody></table></div></div>")

    # DuPont 차트
    parts.append('<div class="chart-grid" style="margin-top:16px">')
    parts.append('<div class="chart-card"><div class="chart-title">ROE 추이 (DuPont 재계산)</div>'
                 '<div class="chart-desc">순이익률 × 총자산회전율 × 재무레버리지로 다시 계산한 ROE(%). 단순 ROE와 같습니다.</div>'
                 '<div style="height:250px"><canvas id="chartDupont"></canvas></div></div>')
    if gap:
        parts.append('<div class="chart-card"><div class="chart-title">ROE 격차 요인 기여도</div>'
                     '<div class="chart-desc">%s → %s 방향으로 요인을 바꿔 가며 본 기여도(%%p). 빨강은 ROE를 높인 요인, 파랑은 낮춘 요인입니다.</div>'
                     '<div style="height:250px"><canvas id="chartDupontGap"></canvas></div></div>'
                     % (html_escape(gap["a"]), html_escape(gap["b"])))
    parts.append("</div>")

    # 연도별 DuPont 표
    parts.append('<div class="tbl-scroll" style="margin-top:16px"><table><thead><tr>'
                 '<th>기업</th><th>사업연도</th><th>순이익률</th><th>총자산회전율</th><th>재무레버리지</th>'
                 '<th>DuPont ROE</th><th>기존 ROE</th><th>검산 차이</th></tr></thead><tbody>')
    for year in years:
        for company in companies:
            dupont = company["years"][year]["dupont"]
            parts.append("<tr><td>%s</td><td class=\"group\">%d년</td><td>%s</td><td>%s</td><td>%s</td>"
                         "<td class=\"better\">%s</td><td>%s</td><td class=\"group\">%s</td></tr>" % (
                             html_escape(company["name"]), year, fmt(dupont["net_margin"]),
                             fmt_turn(dupont["asset_turnover"]), fmt_mult(dupont["equity_multiplier"]),
                             fmt(dupont["roe"]), fmt(dupont["roe_simple"]),
                             fmt_diff(dupont["diff"])))
    parts.append("</tbody></table></div>")
    parts.append('<div class="mini-note">계산식 · 순이익률 = 당기순이익 ÷ 매출액 × 100 · '
                 '총자산회전율 = 매출액 ÷ <b>기말</b> 자산총계 · 재무레버리지 = 자산총계 ÷ 자본총계 · '
                 'DuPont ROE = 세 요인의 곱(단순 ROE와 같은 값이 나와야 정상입니다).</div>')
    parts.append("</section>")

    # ---------- 3. 운전자본 관리 — 현금전환주기(CCC) ----------
    def wc_box(label, value, formula, extra_class=""):
        return ('<div class="wc-box %s"><div class="k">%s</div><div class="v">%s</div>'
                '<div class="f" style="font-size:11px;color:#8a95a3">%s</div></div>'
                % (extra_class, label, value, formula))

    parts.append("<section>")
    parts.append('<h2><span class="num">3</span>운전자본 관리 — 현금전환주기(CCC)</h2>')
    parts.append('<p class="sub">매입해서 팔고, 돈을 회수해 다시 현금이 되는 데 걸리는 시간입니다. '
                 '<b>CCC = DSO + DIO − DPO</b>. 짧을수록 운전자본 부담이 작습니다. '
                 '분자(매출채권·재고자산·매입채무)는 <b>기말잔액</b> 기준입니다.</p>')
    for company in companies:
        wc = company["years"][latest].get("ccc") or {}
        parts.append('<div class="chart-card" style="margin-bottom:14px">')
        parts.append('<div class="chart-title">%s · %d년</div>' % (html_escape(company["name"]), latest))
        parts.append('<div class="wc-grid">')
        parts.append(wc_box("DSO 매출채권회전일수", fmt_days(wc.get("dso")), "매출채권 ÷ 매출액 × 365"))
        parts.append(wc_box("DIO 재고자산회전일수", fmt_days(wc.get("dio")), "재고자산 ÷ 매출원가 × 365"))
        parts.append(wc_box("DPO 매입채무회전일수", fmt_days(wc.get("dpo")), "매입채무 ÷ 매출원가 × 365"))
        parts.append(wc_box("CCC 현금전환주기", fmt_days(wc.get("ccc")), "DSO + DIO − DPO", "total"))
        parts.append("</div>")
        parts.append('<div class="risk-comment">%s</div>' % html_escape(company.get("ccc_comment", "")))
        inputs = wc.get("inputs") or {}
        if inputs:
            parts.append('<div class="mini-note">입력값 · 매출액 %s · 매출원가 %s · 매출채권 %s · 재고자산 %s · 매입채무 %s</div>'
                         % (to_jo(inputs.get("revenue")), to_jo(inputs.get("cost_of_sales")),
                            to_jo(inputs.get("receivables")), to_jo(inputs.get("inventory")),
                            to_jo(inputs.get("payables"))))
        parts.append("</div>")

    parts.append('<div class="chart-grid">')
    parts.append('<div class="chart-card"><div class="chart-title">CCC 구성요소 (기업·연도별)</div>'
                 '<div class="chart-desc">DSO + DIO − DPO를 적재했습니다. 막대 전체 높이가 CCC(일)입니다.</div>'
                 '<div style="height:280px"><canvas id="chartCCCStack"></canvas></div></div>')
    parts.append('<div class="chart-card"><div class="chart-title">CCC 추이</div>'
                 '<div class="chart-desc">낮을수록 현금이 빨리 돌아옵니다(일).</div>'
                 '<div style="height:280px"><canvas id="chartCCC"></canvas></div></div>')
    parts.append("</div>")

    # CCC 연도별 비교표
    parts.append('<div class="tbl-scroll" style="margin-top:16px"><table><thead><tr>'
                 '<th>기업</th><th>사업연도</th><th>DSO</th><th>DIO</th><th>DPO</th>'
                 '<th>CCC</th><th>전년 대비</th></tr></thead><tbody>')
    for company in companies:
        for index, year in enumerate(years):
            wc = company["years"][year].get("ccc") or {}
            delta_text = "-"
            if index > 0:
                previous = (company["years"][years[index - 1]].get("ccc") or {}).get("ccc")
                if wc.get("ccc") is not None and previous is not None:
                    move = wc["ccc"] - previous
                    delta_text = ("%+.0f일 (%s)" % (move, "개선" if move < 0 else ("악화" if move > 0 else "동일")))
            parts.append("<tr><td>%s</td><td class=\"group\">%d년</td><td>%s</td><td>%s</td><td>%s</td>"
                         "<td class=\"better\">%s</td><td class=\"group\">%s</td></tr>" % (
                             html_escape(company["name"]), year, fmt_days(wc.get("dso")),
                             fmt_days(wc.get("dio")), fmt_days(wc.get("dpo")), fmt_days(wc.get("ccc")),
                             delta_text))
    parts.append("</tbody></table></div>")
    if analysis.get("ccc_gap"):
        parts.append('<div class="chart-card" style="margin-top:14px">'
                     '<div class="risk-comment" style="font-size:13.5px;color:#33445c">%s</div></div>'
                     % html_escape(analysis["ccc_gap"]["text"]))
    parts.append('<div class="risk-note"><strong>계산 기준과 오해 주의</strong><ul>'
                 '<li><b>DSO</b> = (기말 매출채권 ÷ 매출액) × 365 · <b>DIO</b> = (기말 재고자산 ÷ 매출원가) × 365 · '
                 '<b>DPO</b> = (기말 매입채무 ÷ 매출원가) × 365</li>'
                 '<li>교과서식은 기초·기말 <b>평균잔액</b>을 쓰지만, 여기서는 DART에 공시된 <b>기말잔액</b>으로 계산했습니다. '
                 '매출·재고가 급격히 늘어난 해에는 평균 기준보다 값이 다소 크게 나옵니다.</li>'
                 '<li>매출원가·매출채권·재고자산·매입채무 중 하나라도 공시되지 않으면 N/A입니다. '
                 '금융업(재고자산·매출원가 없음)이나 매출원가를 따로 표시하지 않는 회사가 여기에 해당합니다.</li>'
                 '<li>DPO는 ‘매입채무’만 보며 미지급금·미지급비용 등 기타 채무는 제외했습니다. '
                 '그래서 실제 지급 조건보다 짧게(보수적으로) 나올 수 있습니다.</li>'
                 '<li>반도체처럼 매출이 급변하는 업종은 재고·매출채권 잔액의 시점 효과가 커서, 한 해 수치만으로 추세를 단정하지 않습니다.</li>'
                 '</ul></div>')
    parts.append("</section>")

    # ---------- 4. 최근 연도 핵심 지표 카드 ----------
    parts.append("<section>")
    parts.append('<h2><span class="num">4</span>%d년 핵심 지표</h2>' % latest)
    parts.append('<p class="sub">사업보고서 기준 확정 실적입니다. 화살표는 전년 대비 증감률이며, *는 전년 영업이익이 적자였던 해의 참고치입니다.</p>')
    parts.append('<div class="kpi-grid">')
    for index, company in enumerate(companies):
        entry = company["years"][latest]
        acc = entry["accounts"]
        ratios = entry["ratios"]
        color = colors[index % len(colors)]
        revenue = acc.get("revenue", {}).get("amount")
        op = acc.get("op_income", {}).get("amount")

        def delta_html(value, mark=""):
            if value is None:
                return '<div class="delta" style="color:#8a95a3">전년 비교 불가</div>'
            cls = "up" if value >= 0 else "down"
            arrow = "▲" if value >= 0 else "▼"
            return '<div class="delta %s">%s %.1f%% (YoY)%s</div>' % (cls, arrow, abs(value), mark)

        parts.append('<div class="kpi">')
        parts.append('<div class="name"><span class="dot" style="background:%s"></span>%s · 매출액</div>'
                     % (color, html_escape(company["name"])))
        parts.append('<div class="value">%s</div>' % to_jo(revenue))
        parts.append(delta_html(ratios.get("revenue_growth")))
        parts.append("</div>")

        parts.append('<div class="kpi">')
        parts.append('<div class="name"><span class="dot" style="background:%s"></span>%s · 영업이익</div>'
                     % (color, html_escape(company["name"])))
        parts.append('<div class="value">%s</div>' % to_jo(op))
        if op is not None and op < 0:
            parts.append('<div class="delta down">영업손실</div>')
        else:
            parts.append(delta_html(ratios.get("op_growth"),
                                    " *" if entry.get("op_growth_base_negative") else ""))
        parts.append("</div>")

        parts.append('<div class="kpi">')
        parts.append('<div class="name"><span class="dot" style="background:%s"></span>%s · 영업이익률</div>'
                     % (color, html_escape(company["name"])))
        parts.append('<div class="value">%s</div>' % pct(ratios.get("op_margin")))
        parts.append('<div class="note">순이익률 %s</div>' % pct(ratios.get("net_margin")))
        parts.append("</div>")

        parts.append('<div class="kpi">')
        parts.append('<div class="name"><span class="dot" style="background:%s"></span>%s · ROE</div>'
                     % (color, html_escape(company["name"])))
        parts.append('<div class="value">%s</div>' % pct(ratios.get("roe")))
        parts.append('<div class="note">부채비율 %s · 유동비율 %s</div>'
                     % (pct(ratios.get("debt_ratio")), pct(ratios.get("current_ratio"))))
        parts.append("</div>")
    parts.append("</div>")
    parts.append("</section>")

    # ---------- 5. 추이 차트 ----------
    parts.append("<section>")
    parts.append('<h2><span class="num">5</span>연도별 추이</h2>')
    parts.append('<p class="sub">막대는 규모(조원), 선은 비율(%)입니다. 마우스를 올리면 정확한 값이 보입니다.</p>')
    parts.append('<div class="chart-grid">')

    def chart_box(canvas_id, chart_title, desc):
        return ('<div class="chart-card"><div class="chart-title">%s</div>'
                '<div class="chart-desc">%s</div>'
                '<div style="height:260px"><canvas id="%s"></canvas></div></div>'
                % (chart_title, desc, canvas_id))

    parts.append(chart_box("chartRevenue", "매출액 추이", "단위: 조원 (연결 기준)"))
    parts.append(chart_box("chartProfit", "영업이익 추이", "단위: 조원 · 본업에서 번 이익"))
    parts.append(chart_box("chartMargin", "수익성 비율", "영업이익률과 순이익률(%)"))
    parts.append(chart_box("chartStability", "안정성 비율", "부채비율과 유동비율(%)"))
    parts.append("</div></section>")

    # ---------- 6. 재무비율 비교표 ----------
    parts.append("<section>")
    parts.append('<h2><span class="num">6</span>재무비율 비교표</h2>')
    parts.append('<p class="sub">굵은 글씨는 두 회사 중 유리한 쪽입니다. (부채비율은 낮을수록 유리)</p>')
    parts.append('<div class="tbl-scroll"><table><thead><tr><th>구분</th><th>지표</th>')
    for year in years:
        for company in companies:
            parts.append("<th>%s<br><span style=\"font-weight:400;color:#7d8794\">%d년</span></th>"
                         % (html_escape(company["name"]), year))
    parts.append("</tr></thead><tbody>")

    for key, label, unit, group in RATIO_LABELS:
        parts.append("<tr><td class=\"group\">%s</td><td>%s</td>" % (group, label))
        for year in years:
            values = [c["years"][year]["ratios"].get(key) for c in companies]
            best = None
            if len(companies) == 2 and all(v is not None for v in values):
                best = min(values) if key == "debt_ratio" else max(values)
            for value in values:
                cls = "better" if (best is not None and value == best) else ""
                parts.append('<td class="%s">%s</td>' % (cls, pct(value)))
        parts.append("</tr>")
    parts.append("</tbody></table></div></section>")

    # ---------- 7. 재무제표 주요 계정 ----------
    parts.append("<section>")
    parts.append('<h2><span class="num">7</span>재무제표 주요 계정</h2>')
    parts.append('<p class="sub">DART에 공시된 금액 그대로입니다. 단위: 조원. 연도 옆 <b>원문</b>을 누르면 해당 사업보고서가 열립니다.</p>')
    parts.append('<div class="tbl-scroll"><table><thead><tr><th>계정</th>')
    for year in years:
        for company in companies:
            rcept_no = company["years"][year].get("rcept_no") or ""
            link = ('<br><a class="link-inline" href="%s" target="_blank" rel="noopener">원문 ↗</a>'
                    % html_escape(dart_link(rcept_no))) if rcept_no else ""
            parts.append("<th>%s<br><span style=\"font-weight:400;color:#7d8794\">%d년</span>%s</th>"
                         % (html_escape(company["name"]), year, link))
    parts.append("</tr></thead><tbody>")
    for key, label in [("revenue", "매출액"), ("op_income", "영업이익"), ("net_income", "당기순이익"),
                       ("assets", "자산총계"), ("current_assets", "유동자산"), ("liabilities", "부채총계"),
                       ("current_liab", "유동부채"), ("equity", "자본총계"),
                       ("cost_of_sales", "매출원가"), ("receivables", "매출채권"),
                       ("inventory", "재고자산"), ("payables", "매입채무")]:
        parts.append("<tr><td>%s</td>" % label)
        for year in years:
            for company in companies:
                item = company["years"][year]["accounts"].get(key)
                parts.append("<td>%s</td>" % (to_jo(item["amount"]) if item else "-"))
        parts.append("</tr>")
    parts.append("</tbody></table></div></section>")

    # ---------- 8. DART 원문 공시 바로가기 ----------
    parts.append("<section>")
    parts.append('<h2><span class="num">8</span>DART 원문 공시 바로가기</h2>')
    parts.append('<p class="sub">표나 카드의 숫자를 보고 바로 근거를 확인할 수 있게, 연도별 사업보고서 접수번호(rcept_no)를 '
                 'DART 원문 링크로 연결했습니다. 링크를 누르면 새 창에서 해당 사업보고서가 열립니다.</p>')
    parts.append('<div class="tbl-scroll"><table><thead><tr>'
                 '<th>기업</th><th>사업연도</th><th>재무제표</th><th>접수번호</th><th>접수일</th><th>원문</th>'
                 '</tr></thead><tbody>')
    for year in years:
        for company in companies:
            entry = company["years"][year]
            rcept_no = entry.get("rcept_no") or ""
            fs_name = entry.get("fs_div") or company.get("fs_div") or ""
            fs_text = {"CFS": "연결재무제표", "OFS": "별도재무제표"}.get(fs_name, fs_name or "-")
            link = ("<a class=\"link-chip\" href=\"%s\" target=\"_blank\" rel=\"noopener\">원문 열기 ↗</a>"
                    % html_escape(dart_link(rcept_no))) if rcept_no else "<span class=\"group\">없음</span>"
            parts.append("<tr><td>%s</td><td class=\"group\">%d년</td><td class=\"group\">%s</td>"
                         "<td>%s</td><td class=\"group\">%s</td><td>%s</td></tr>" % (
                             html_escape(company["name"]), year, fs_text,
                             rcept_no or "-", rcept_date(rcept_no) or "-", link))
    parts.append("</tbody></table></div>")
    parts.append('<div class="mini-note">접수번호는 금감원 DART가 공시서류를 접수할 때 부여하는 번호이며, 앞 8자리가 접수일자(YYYYMMDD)입니다. '
                 '링크 형식: <code>https://dart.fss.or.kr/dsaf001/main.do?rcpNo=접수번호</code></div>')
    parts.append("</section>")

    # ---------- 9. 해석 가이드 ----------
    parts.append("<section>")
    parts.append('<h2><span class="num">9</span>지표 해석 가이드</h2>')
    parts.append('<p class="sub">숫자를 읽는 기준을 함께 적어 두면, 포트폴리오에서 분석 역량을 보여주기 좋습니다.</p>')
    parts.append('<div class="chart-card"><table style="border:none"><tbody>')
    guide = [
        ("영업이익률", "매출 100원 중 본업에서 남긴 이익. 반도체처럼 업황 변동이 큰 산업은 10%p 이상 출렁이기도 합니다."),
        ("순이익률", "영업이익에서 이자·세금 등을 반영한 최종 이익률. 영업이익률과 격차가 크면 금융비용을 점검합니다."),
        ("ROE", "주주가 맡긴 자본으로 얼마나 벌었는지. 15% 이상이면 우수하다고 보는 경우가 많습니다."),
        ("부채비율", "자본 대비 부채. 100% 이하면 재무구조가 안정적, 200%를 넘으면 주의해서 봅니다."),
        ("유동비율", "1년 안에 갚을 빚 대비 1년 안에 현금화할 자산. 100% 미만이면 단기 자금 부담이 있습니다."),
        ("매출액 증가율", "시장에서 외형이 커지는 속도. 업황 사이클을 함께 봐야 합니다."),
        ("영업이익 증가율", "전년 영업이익이 적자였다면 절대값 기준 참고치이므로 부호 해석에 주의합니다."),
        ("DuPont ROE", "ROE = 순이익률 × 총자산회전율 × 재무레버리지. 같은 ROE라도 이익을 남기는 힘(수익성)인지, "
                        "자산을 빨리 돌리는 힘(효율성)인지, 아니면 빚을 써서 자본을 키웠는지(레버리지)가 다릅니다."),
        ("총자산회전율", "매출액 ÷ 기말 자산총계. 1회를 넘으면 자산 총액보다 많은 매출을 냈다는 뜻입니다. "
                        "장치산업(반도체·철강)은 1회를 밑돌고 유통·서비스업은 높게 나오는 경향이 있습니다."),
        ("현금전환주기(CCC)", "DSO + DIO − DPO. 매입해서 돈을 회수할 때까지 걸리는 시간(일)입니다. "
                            "짧으면 운전자본 부담이 작고, 길면 재고·매출채권에 현금이 늘 묶입니다."),
    ]
    for name, desc in guide:
        parts.append('<tr><td style="width:140px;font-weight:600">%s</td><td style="text-align:left;color:#5b6877">%s</td></tr>'
                     % (name, desc))
    parts.append("</tbody></table></div></section>")

    # ---------- 푸터 ----------
    parts.append("<footer>")
    parts.append("<strong>데이터 출처 및 유의사항</strong>")
    parts.append("<ul>")
    parts.append("<li>금융감독원 전자공시시스템(DART) Open API <code>fnlttSinglAcnt.json</code>, "
                 "사업보고서(reprt_code=11011) 기준.</li>")
    parts.append("<li>%s</li>" % " · ".join(
        '%s %d년 <a href="%s" target="_blank" rel="noopener">%s</a>' % (
            html_escape(c["name"]), year, html_escape(dart_link(c["years"][year].get("rcept_no"))),
            c["years"][year].get("rcept_no") or "-")
        for c in companies for year in years))
    parts.append("<li>연결재무제표(CFS)를 우선 사용했고 없을 경우 별도재무제표(OFS)로 대체했습니다.</li>")
    parts.append("<li>ROE는 기말 자본총계 기준 단순 계산이며, DuPont ROE도 같은 기준(기말 자산·기말 자본)으로 재계산해 검산했습니다. "
                 "총자산회전율의 분모는 기말 자산총계입니다.</li>")
    parts.append("<li>DuPont·CCC 계산에 쓰는 매출원가·매출채권·재고자산·매입채무는 <code>fnlttSinglAcntAll.json</code>(전체 재무제표)에서 "
                 "IFRS 표준 계정 ID를 먼저 보고, 없으면 계정명 후보로 찾습니다. 미공시·금융업은 N/A로 표시했습니다.</li>")
    parts.append("<li>DSO·DIO·DPO는 기말잔액 기준(365일)입니다. 교과서식(기초·기말 평균)과는 값이 다를 수 있습니다.</li>")
    parts.append("<li>원문 공시 링크는 각 연도 사업보고서의 접수번호(rcept_no)로 만든 DART 뷰어 주소입니다.</li>")
    parts.append("<li>본 문서는 학습·포트폴리오 목적의 분석이며 투자 권유가 아닙니다.</li>")
    parts.append("</ul>")
    parts.append("<p>Generated by analyzer.py · %s</p>" % generated)
    parts.append("</footer>")

    parts.append("</div>")  # wrap

    # ---------- 스크립트 ----------
    data_json = json.dumps(build_chart_data(analysis), ensure_ascii=False)
    parts.append("<script>%s</script>" % PAGE_JS.replace("__DATA__", data_json))
    parts.append("</body></html>")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# 7. 실행 진입점
# ---------------------------------------------------------------------------

def resolve_api_key(cli_key):
    """API 키를 (1) 인자 → (2) 환경변수 → (3) 안내 순서로 찾는다."""
    key = cli_key or os.environ.get("DART_API_KEY") or os.environ.get("OPENDART_API_KEY")
    if not key:
        print("오류: OpenDART 인증키가 없습니다.", file=sys.stderr)
        print("  방법 1) 환경변수:  export DART_API_KEY=\"발급받은키\"  (Windows: set DART_API_KEY=발급받은키)", file=sys.stderr)
        print("  방법 2) 실행 인자:  python3 analyzer.py 삼성전자 SK하이닉스 --api-key 발급받은키", file=sys.stderr)
        print("  키 발급: https://opendart.fss.or.kr → 인증키 신청/관리", file=sys.stderr)
        sys.exit(1)
    if len(key) != 40:
        print("경고: OpenDART 인증키는 보통 40자리입니다. 지금 입력한 키는 %d자리입니다." % len(key), file=sys.stderr)
    return key.strip()


def collect_company(api_key, corp, years, verbose=True):
    """한 회사의 여러 연도 재무제표를 모아 하나의 dict로 만든다."""
    company = {
        "name": corp["corp_name"],
        "corp_code": corp["corp_code"],
        "stock_code": corp["stock_code"],
        "years": {},
        "fs_div": None,
        "fs_nm": None,
        "latest_rcept_no": "",
    }
    for year in years:
        try:
            fs_div, fs_nm, rcept_no, accounts = fetch_statement(api_key, corp["corp_code"], year)
        except DartError as error:
            if error.status == "013":
                if verbose:
                    print("  · %s %d년: 공시 데이터 없음 → 건너뜀" % (corp["corp_name"], year))
                continue
            raise
        company["years"][year] = {"accounts": accounts, "rcept_no": rcept_no, "fs_div": fs_div}
        # 리스크 진단용 계정(이자비용·영업활동현금흐름·이익잉여금)을 보강한다.
        # 전체 재무제표 API가 막혀도 본 분석은 계속되도록 결과가 비어도 넘어간다.
        extras = fetch_extra_accounts(api_key, corp["corp_code"], year)
        accounts.update(extras)
        # 최근 연도 기준으로 어떤 재무제표를 썼는지 기록
        company["fs_div"] = fs_div
        company["fs_nm"] = fs_nm
        company["latest_rcept_no"] = rcept_no
        if verbose:
            missing = [s["label"] for s in ACCOUNT_SPECS if s["key"] not in accounts]
            note = (" (일부 계정 없음: %s)" % ", ".join(missing)) if missing else ""
            print("  · %s %d년: %s, 접수번호 %s%s" % (corp["corp_name"], year, fs_nm, rcept_no, note))
        time.sleep(0.25)  # 서버 예의상 짧은 간격
    return company


def build_analysis(api_key, corps, years, verbose=True):
    """
    회사(corp) 목록과 연도로 분석 결과(analysis dict)를 만든다.

    CLI(main)와 Streamlit 앱(app.py)이 같은 파이프라인을 쓰도록 한 곳에 모았다.
      1) 연도별 사업보고서 수집 (collect_company)
      2) 실제 데이터가 있는 연도만 남김 (회사 간 비교를 위해 교집합)
      3) 재무비율 · 운전자본(CCC) · 5대 리스크 · 최근 1년 공시 위험 · 자동 해석 문장
      4) 두 회사면 DuPont ROE 격차 분해(build_dupont_gap)와 CCC 비교(build_ccc_gap)

    반환값: {"companies": [...], "years": [...], "dupont_gap": ..., "ccc_gap": ...}
    연도가 2개 미만이면 status="years"인 DartError를 낸다(호출한 쪽이 안내 문구를 정한다).
    """
    companies = [collect_company(api_key, corp, years, verbose=verbose) for corp in corps]

    # 실제 데이터가 있는 연도만 남긴다 (회사 간 연도를 맞춰 비교하기 위해 교집합 사용)
    available = set(years)
    for company in companies:
        available &= set(company["years"].keys())
    years = sorted(available)
    if len(years) < 2:
        raise DartError("비교할 수 있는 연도가 2개 미만입니다. --years 옵션으로 연도를 지정해 보세요.", status="years")

    for company in companies:
        company["years"] = {y: company["years"][y] for y in years}
        compute_ratios(company["years"], years)
        compute_working_capital(company["years"], years)
        # 5대 리스크 진단 (신호등 + 100점 점수)
        company["risk"] = evaluate_risk(company, years)
        # 비재무 공시 위험 키워드 (최근 1년, list.json) — 실패해도 분석은 계속된다
        company["disclosure"] = fetch_disclosure_risks(api_key, company["corp_code"])
        # DuPont·CCC 자동 해석 코멘트 (두 회사 비교 문장은 아래에서 따로 만든다)
        company["dupont_comment"] = make_dupont_comment(company, years[-1])
        company["ccc_comment"] = make_ccc_comment(company, years)

    analysis = {"companies": companies, "years": years}
    # 두 기업의 ROE 격차 요인 분해 · CCC 비교
    analysis["dupont_gap"] = build_dupont_gap(companies, years[-1])
    analysis["ccc_gap"] = build_ccc_gap(companies, years[-1])
    return analysis


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="OpenDART 사업보고서로 상장사 재무비율을 분석하고 HTML 대시보드를 만듭니다.",
        epilog="예시) python3 analyzer.py 삼성전자 SK하이닉스",
    )
    parser.add_argument("companies", nargs="+", help="비교할 회사 이름 (1~3개). 예: 삼성전자 SK하이닉스")
    parser.add_argument("--api-key", help="OpenDART 인증키 (없으면 DART_API_KEY 환경변수 사용)")
    parser.add_argument("--years", nargs="+", type=int, help="분석할 사업연도. 생략하면 최근 3개년")
    parser.add_argument("--out", help="HTML 파일 경로. 생략하면 report_<회사1>_vs_<회사2>.html")
    parser.add_argument("--refresh", action="store_true", help="기업 목록 캐시를 무시하고 새로 받기")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    api_key = resolve_api_key(args.api_key)

    if len(args.companies) > 3:
        print("오류: 회사는 최대 3개까지 비교할 수 있습니다.", file=sys.stderr)
        return 1

    print()
    print("[1/4] 기업 목록 준비")
    corp_list = sync_corp_codes(api_key, refresh=args.refresh)

    print("[2/4] 회사 코드 확인")
    corps = []
    for name in args.companies:
        corp = find_company(corp_list, name)
        corps.append(corp)
        print("  · %s → corp_code %s (종목코드 %s)" % (corp["corp_name"], corp["corp_code"], corp["stock_code"] or "-"))

    # --- 연도 결정: 지정이 없으면 '작년, 재작년, 그 전년'을 시도 ---
    if args.years:
        years = sorted(args.years)
    else:
        this_year = datetime.now().year
        years = [this_year - 3, this_year - 2, this_year - 1]  # 2026년 기준 2023~2025

    print("[3/4] 재무제표 조회 (%d개년)" % len(years))
    try:
        analysis = build_analysis(api_key, corps, years)
    except DartError as error:
        if error.status == "years":
            print("오류: %s" % error, file=sys.stderr)
            return 1
        raise

    # --- 콘솔 보고서 ---
    print_console_report(analysis)

    # --- HTML 저장 ---
    safe_names = [c["name"].replace(" ", "_") for c in analysis["companies"]]
    default_name = "report_%s.html" % "_vs_".join(safe_names)
    out_path = args.out or default_name
    with open(out_path, "w", encoding="utf-8") as fp:
        fp.write(build_html(analysis))

    print("[4/4] HTML 대시보드 저장 완료 → %s" % os.path.abspath(out_path))
    print("      브라우저에서 파일을 열면 차트가 바로 보입니다. (Chart.js는 CDN에서 불러옵니다)")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except DartError as error:
        print("\n[오류] %s" % error, file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as error:
        print("\n[네트워크 오류] %s" % error, file=sys.stderr)
        print("  인터넷 연결을 확인해 주세요.", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n중단했습니다.")
        sys.exit(130)
