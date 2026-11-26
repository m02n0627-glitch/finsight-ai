#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
statement_links_seed.json 생성기 — DART 원문 재무제표 '직행 링크' 시드
=====================================================================

사업보고서 원문(dart.fss.or.kr)의 재무제표 한 장짜리 링크(viewer.do)를 주요 상장사
3개년(2023~2025) 분량으로 미리 계산해 저장소에 동봉한다.

왜 필요한가
-----------
Streamlit Cloud(미국 리전)에서는 dart.fss.or.kr 의 main.do 목차 조회가 느리거나 막혀
모든 링크가 표지(main.do)로 물러나는 일이 있다. 시드에 접수번호가 있으면 네트워크 없이
0ms로 viewer.do 직행 링크를 돌려준다.

인증키가 필요 없다
------------------
DART 공시통합검색(`/dsab007/detailSearch.ax`, 사업보고서만 필터)에서 접수번호를 받고,
`analyzer.fetch_statement_links()` 가 main.do 목차를 파싱한다. 둘 다 공개 화면이라
OpenDART API 키를 쓰지 않는다(키는 파일·인자 어디에도 저장하지 않는다).

사용법
------
    python3 build_statement_links_seed.py            # 이어서 수집(진행 상황 유지)
    python3 build_statement_links_seed.py --limit 20 # 20개사만 추가로
    python3 build_statement_links_seed.py --sleep 0.5

· 결과는 매 회사마다 statement_links_seed.json 에 바로 반영된다(중간에 끊겨도 안전).
· 이미 있는 접수번호의 링크는 절대 지우거나 덮어쓰지 않는다(기존 데이터 보존).
· 서버에 부담을 주지 않도록 요청 사이에 쉽(sleep)을 둔다.
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

import analyzer as fs  # noqa: E402

SEED_PATH = os.path.join(ROOT, "statement_links_seed.json")
INDUSTRY_PATH = os.path.join(ROOT, "industry_map_seed.json")
CORP_PATH = os.path.join(ROOT, "corp_codes_seed.json")
YEARS = (2023, 2024, 2025)
SEARCH_URL = "https://dart.fss.or.kr/dsab007/detailSearch.ax"
HEADERS = dict(fs.DART_BROWSER_HEADERS)
HEADERS["Referer"] = "https://dart.fss.or.kr/dsab007/main.do"
HEADERS["X-Requested-With"] = "XMLHttpRequest"

# 업종 지도·기존 시드에 없는 대표 대형주 — 이름만 두고 corp_code 는 corp_codes_seed.json 에서 찾는다
# (잘못된 고유번호로 다른 회사 보고서를 받아 오는 사고를 막기 위해서다).
TOPUP = [
    "삼성물산", "SK", "현대건설", "SK이노베이션", "한화", "두산에너빌리티", "POSCO홀딩스",
    "크래프톤", "엔씨소프트", "넷마블", "하이브", "카카오뱅크", "이마트", "롯데쇼핑",
    "BGF리테일", "GS리테일", "오리온", "농심", "CJ대한통운", "대한항공", "아시아나항공",
    "HMM", "팬오션", "한국전력", "한국가스공사", "효성첨단소재", "코오롱인더",
    "금호석유화학", "한솔케미칼", "솔브레인", "동진쎄미켐", "삼성SDI", "에코프로비엠",
    "에코프로", "포스코퓨처엠", "엘앤에프", "SK아이이테크놀로지", "현대제철", "고려아연",
    "풍산", "삼성중공업", "한화오션", "현대로템", "한화시스템", "LIG넥스원",
    "한국항공우주", "영풍", "OCI홀딩스", "한미약품", "유한양행", "종근당", "대웅제약",
    "녹십자", "SK바이오팜", "SK바이오사이언스", "알테오젠", "클래시스", "파마리서치",
    "삼성생명", "삼성화재해상보험", "DB손해보험", "현대해상", "메리츠화재해상보험",
    "KB금융", "신한지주", "하나금융지주", "우리금융지주", "기업은행", "한국금융지주",
    "미래에셋증권", "키움증권", "NAVER", "카카오", "SK텔레콤", "케이티", "LG유플러스",
    "LG", "LS", "GS", "효성", "한국타이어앤테크놀로지", "금호타이어", "KG모빌리티",
    "코웨이", "아모레퍼시픽", "LG생활건강", "한국화장품", "코스맥스", "하이트진로",
    "롯데칠성음료", "오뚜기", "삼양식품", "CJ제일제당", "대상", "하림지주",
    "스튜디오드래곤", "CJ ENM", "넥슨게임즈", "펄어비스", "위메이드", "컴투스",
    "호텔신라", "강원랜드", "롯데관광개발", "대한해운", "동원산업", "사조대림",
    "한국콜마", "콜마비앤에이치", "동국제강", "태광산업", "효성화학", "한국앤컴퍼니",
    "두산", "두산로보틱스", "한화에어로스페이스", "현대모비스", "기아", "현대자동차",
]


def clean_name(name):
    """'삼성전자(주)' → '삼성전자' 처럼 표시용 이름을 다듬는다."""
    text = str(name or "")
    text = text.replace("주식회사", "").replace("(주)", "").replace("㈜", "")
    return text.strip()


def search_annual_reports(corp_code, sleep=0.4):
    """
    공시통합검색(공개 화면 · 인증키 불필요)에서 사업보고서 접수번호를 받는다.

    반환: {사업연도(int): [{"rcept_no":.., "correction":bool, "date":"2026.03.10"}, ...]}
    """
    payload = {
        "currentPage": "1",
        "maxResults": "100",
        "maxLinks": "10",
        "sort": "date",
        "series": "",
        "textCrpCik": corp_code,
        "option": "corp",
        "textCrpNm": "",
        "publicType": "A001",          # 사업보고서만
        "startDate": "%d0101" % YEARS[0],
        "endDate": "%d1231" % (YEARS[-1] + 1),
        "autoSearch": "N",
        "autoSearchCorp": "Y",
        "businessCode": "all",
    }
    request = urllib.request.Request(SEARCH_URL, data=urllib.parse.urlencode(payload).encode(),
                                     headers=HEADERS, method="POST")
    with urllib.request.urlopen(request, timeout=30) as response:
        html = response.read().decode("utf-8", "replace")
    time.sleep(sleep)

    found = {}
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S | re.I):
        match = re.search(r"사업보고서\s*\((\d{4})\.12\)", row)
        rcept = re.search(r"openReportViewer\('(\d{8,20})'", row)
        if not match or not rcept:
            continue
        year = int(match.group(1))
        if year not in YEARS:
            continue
        date = re.search(r"(\d{4}\.\d{2}\.\d{2})", row)
        found.setdefault(year, []).append({
            "rcept_no": rcept.group(1),
            "correction": "[기재정정]" in row,
            "date": date.group(1) if date else "",
        })
    return found


def load_seed():
    if not os.path.exists(SEED_PATH):
        return {"saved_at": "", "source": "", "years": list(YEARS), "count": 0,
                "companies": [], "links": {}}
    with open(SEED_PATH, "r", encoding="utf-8") as fp:
        return json.load(fp)


def save_seed(seed):
    payload = dict(seed)
    payload["saved_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    payload["years"] = list(YEARS)
    payload["count"] = len(payload["links"])
    payload["source"] = (
        "DART 공시통합검색(detailSearch.ax · 사업보고서)과 main.do 목차 파싱 결과 — 주요 상장사 "
        "%d~%d 사업보고서 재무제표 직행 링크 (접수번호·링크·목차 이름만 수록, API 키 없음)"
        % (YEARS[0], YEARS[-1]))
    tmp = SEED_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fp:
        json.dump(payload, fp, ensure_ascii=False, indent=1)
    os.replace(tmp, SEED_PATH)


def target_companies(seed):
    """업종 지도(118개사) 우선 + 기존 시드 + 대형주 보충 — 중복 corp_code 제거."""
    targets, seen = [], set()

    def add(name, corp_code, stock_code=""):
        code = str(corp_code or "").strip()
        if not code or code in seen:
            return
        seen.add(code)
        targets.append({"corp_name": clean_name(name), "corp_code": code,
                        "stock_code": str(stock_code or "")})

    try:
        with open(INDUSTRY_PATH, "r", encoding="utf-8") as fp:
            for item in json.load(fp).get("companies", []):
                add(item.get("corp_name"), item.get("corp_code"), item.get("stock_code"))
    except (OSError, ValueError):
        pass
    for item in seed.get("companies", []):
        add(item.get("corp_name"), item.get("corp_code"), item.get("stock_code"))
    known = {}
    try:
        with open(CORP_PATH, "r", encoding="utf-8") as fp:
            known = {clean_name(c.get("corp_name")): c for c in json.load(fp).get("companies", [])}
            known.update({str(c.get("corp_name")): c for c in known.values()})
    except (OSError, ValueError):
        pass
    for name in TOPUP:
        item = known.get(clean_name(name))
        if item:
            add(name, item.get("corp_code"), item.get("stock_code"))
    return targets


def main(argv=None):
    parser = argparse.ArgumentParser(description="statement_links_seed.json 확장(이어서 수집)")
    parser.add_argument("--limit", type=int, default=0, help="이번 실행에서 처리할 기업 수(0=전부)")
    parser.add_argument("--sleep", type=float, default=0.4, help="요청 사이 대기(초)")
    args = parser.parse_args(argv)

    seed = load_seed()
    links = seed.setdefault("links", {})
    done = {item.get("corp_code"): item for item in seed.get("companies", [])}
    targets = target_companies(seed)
    processed = 0

    for company in targets:
        if args.limit and processed >= args.limit:
            break
        corp_code = company["corp_code"]
        existing = done.get(corp_code)
        if existing and all(existing.get("years", {}).get(str(y)) in links for y in YEARS):
            continue                       # 3개년 직행 링크가 모두 있으면 건너뛴다
        try:
            reports = search_annual_reports(corp_code, sleep=args.sleep)
        except Exception as error:         # noqa: BLE001 — 한 회사 실패가 전체를 막지 않게
            print("   검색 실패 %s(%s): %s" % (company["corp_name"], corp_code, error), flush=True)
            continue

        entry = existing or {"corp_name": company["corp_name"], "corp_code": corp_code,
                             "stock_code": company.get("stock_code", ""), "years": {}}
        entry["years"] = dict(entry.get("years") or {})
        got = 0
        for year in YEARS:
            versions = reports.get(year) or []
            if not versions:
                entry["years"].setdefault(str(year), "")
                continue
            primary = next((v for v in versions if not v["correction"]), versions[-1])
            for version in versions:
                rcept_no = version["rcept_no"]
                if rcept_no in links:      # 기존 링크는 건드리지 않는다
                    continue
                try:
                    result = fs.fetch_statement_links(rcept_no, use_cache=False)
                except Exception as error:  # noqa: BLE001
                    print("   링크 실패 %s: %s" % (rcept_no, error), flush=True)
                    continue
                if result.get("fallback"):
                    print("   건너뜀(목차 없음) %s %s %d" % (rcept_no, company["corp_name"], year),
                          flush=True)
                else:
                    links[rcept_no] = {
                        "income": result["income"], "balance": result["balance"],
                        "cashflow": result["cashflow"], "main": result["main"],
                        "labels": result["labels"], "fallback": False,
                    }
                    got += 1
                time.sleep(args.sleep)
            # 그 해의 대표 접수번호는 '실제로 재무제표 링크가 만들어진' 보고서를 우선한다.
            # (정정신고처럼 이름만 사업보고서인 문서가 섞여 있을 수 있다)
            linked = next((v["rcept_no"] for v in versions if v["rcept_no"] in links), "")
            entry["years"][str(year)] = linked or primary["rcept_no"]
        done[corp_code] = entry
        seed["companies"] = list(done.values())
        save_seed(seed)
        processed += 1
        print("OK %-16s %s (+%d links, total %d)" % (
            company["corp_name"], entry["years"], got, len(links)), flush=True)

    save_seed(seed)
    print("DONE companies=%d links=%d" % (len(seed["companies"]), len(seed["links"])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
