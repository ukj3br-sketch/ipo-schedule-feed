#!/usr/bin/env python3
"""
fetch_ipo.py — 피너츠(finuts.co.kr) 공모주 청약 예정 데이터 자동 수집
사용법: python3 fetch_ipo.py
결과: ipo_data.json → 공모주 관리시트 대시보드에서 [JSON 가져오기]로 임포트

표준 라이브러리만 사용 (별도 설치 불필요)
"""
import json
import re
import urllib.request
import urllib.parse
from datetime import date, timedelta
from collections import defaultdict


URL = "https://www.finuts.co.kr/html/task/ipo/ipoListQuery.php"
PAYLOAD = urllib.parse.urlencode({"cat": "ipo-011"}).encode()
HEADERS = {
    "Content-Type": "application/x-www-form-urlencoded",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Referer": "https://www.finuts.co.kr/html/ipo/ipoList.php",
    "X-Requested-With": "XMLHttpRequest",
}

ALLOWED_SE_CODES = {"IPO", "SPAC"}

BROKER_ALIASES = (
    ("미래에셋", "미래에셋"), ("미래", "미래에셋"),
    ("NH", "NH"), ("한국", "한국"), ("하나", "하나"),
    ("삼성", "삼성"), ("키움", "키움"), ("대신", "대신"),
    ("신한", "신한"), ("KB", "KB"), ("한화", "한화"),
    ("현대차", "현대차"), ("유진", "유진"), ("IBK", "IBK"),
    ("DB", "DB금융"), ("유안타", "유안타"), ("상상인", "상상인"),
    ("LS", "LS"), ("SK", "SK"), ("신영", "신영"),
    ("BNK", "BNK"), ("메리츠", "메리츠"), ("교보", "교보"),
    ("IM", "IM"),
)


def _is_valid_date(value):
    if not value or value in ("9999-99-99", "0"):
        return False
    try:
        date.fromisoformat(value)
        return True
    except ValueError:
        return False


def _previous_business_day(value, count=1):
    current = date.fromisoformat(value)
    remaining = count
    while remaining:
        current -= timedelta(days=1)
        if current.weekday() < 5:
            remaining -= 1
    return current.isoformat()


def _normalize_subscription_dates(start, end, refunds):
    """피너츠의 비정상 장기 S 일정은 환불일을 기준으로 2일 청약으로 복원한다."""
    if not _is_valid_date(start):
        start = ""
    if not _is_valid_date(end):
        end = start

    malformed = not start or not end
    if start and end:
        malformed = (date.fromisoformat(end) - date.fromisoformat(start)).days not in range(0, 8)

    if malformed:
        valid_refunds = sorted(r for r in refunds if _is_valid_date(r))
        if valid_refunds:
            end = _previous_business_day(valid_refunds[0], 2)
        if end:
            start = _previous_business_day(end)
    return start, end


def _normalize_leads(raw_value):
    text = re.sub(r"투자증권|금융투자|증권|금융|\s+", "", raw_value or "")
    hits = []
    for alias, canonical in BROKER_ALIASES:
        position = text.find(alias)
        if position >= 0:
            hits.append((position, canonical))
    result = []
    for _, canonical in sorted(hits):
        if canonical not in result:
            result.append(canonical)
    return result


def fetch_raw():
    req = urllib.request.Request(URL, data=PAYLOAD, headers=HEADERS, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def parse(data, today_str=None, include_current_month=False):
    if today_str is None:
        today_str = date.today().isoformat()
    today = date.fromisoformat(today_str)
    window = (today + timedelta(days=30)).isoformat()
    history_start = today.replace(day=1).isoformat() if include_current_month else today_str

    items = data.get("data", [])

    # IPO_SN별로 이벤트 묶기
    by_sn = defaultdict(lambda: {
        "name": "", "se_cd": "IPO",
        "subs": [],     # S: 일반 공모주 청약
        "lists": [],    # bgng for L
        "refunds": [],  # R: 환불일
        "surveys": [],  # D: 수요예측
    })

    for item in items:
        sn = item.get("IPO_SN", "")
        if not sn:
            continue
        name = (item.get("ENT_NM") or "").strip()
        if not name:
            continue

        se_cd = item.get("SE_CD", "IPO")
        if se_cd not in ALLOWED_SE_CODES:
            continue

        d = by_sn[sn]
        d["name"] = name
        d["se_cd"] = se_cd

        schdl = item.get("SCHDL_SE_CD", "")
        bgng = item.get("BGNG_YMD") or ""
        end = item.get("END_YMD") or bgng

        if schdl == "S" and _is_valid_date(bgng):
            raw_issue = item.get("PSS_PRC") or item.get("BAND_END_AMT") or "0"
            issue = int(raw_issue) if str(raw_issue).isdigit() else 0
            d["subs"].append({
                "start": bgng,
                "end": end,
                "issuePrice": issue,
                "lead": _normalize_leads(item.get("INDCT_JUGANSA_NM")),
            })

        if schdl == "L" and _is_valid_date(bgng):
            d["lists"].append(bgng)

        if schdl == "R" and _is_valid_date(bgng):
            d["refunds"].append(bgng)

        if schdl == "D" and _is_valid_date(bgng):
            d["surveys"].append((bgng, end if _is_valid_date(end) else bgng))

    # SN별 유효 청약 결정
    sn_result = {}
    for sn, d in by_sn.items():
        if not d["name"] or not d["subs"]:
            continue

        list_date = min(d["lists"]) if d["lists"] else ""
        valid_subs = []
        for subscription in d["subs"]:
            sub_start, sub_end = _normalize_subscription_dates(
                subscription["start"], subscription["end"], d["refunds"]
            )
            if sub_start and (not list_date or sub_start < list_date):
                valid_subs.append({**subscription, "start": sub_start, "end": sub_end})
        if not valid_subs:
            continue

        best = min(valid_subs, key=lambda s: s["start"])

        sn_result[sn] = {
            "name": d["name"], "se_cd": d["se_cd"],
            "subStart": best["start"], "subEnd": best["end"],
            "listDate": list_date, "issuePrice": best["issuePrice"],
            "lead": best["lead"],
        }

    # 회사별 가장 이른 유효 청약 SN 선택.
    # 캘린더 동기화에서는 이번 달에 이미 청약했거나 이번 달 이후 상장하는 종목도
    # 함께 내려줘야 현재 월의 청약·상장 이력을 정확히 다시 그릴 수 있다.
    by_name_future = defaultdict(list)
    for sn, d in sn_result.items():
        activity_end = max(d["subEnd"] or "", d["listDate"] or "")
        if activity_end >= history_start:
            by_name_future[d["name"]].append(d)

    result = []
    for name, candidates in by_name_future.items():
        best = min(candidates, key=lambda x: x["subStart"])
        is_spac = best["se_cd"] == "SPAC"
        result.append({
            "name": name,
            "issuePrice": best["issuePrice"],
            "expectedPrice": 0,
            "subStart": best["subStart"],
            "subEnd": best["subEnd"],
            "listDate": best["listDate"],
            "lead": best["lead"],
            "minQty": 10,
            "depositRate": 50,
            "strategy": "both",
            "note": f"피너츠 자동 수집 {today_str}{' [SPAC]' if is_spac else ''}",
        })
    result.sort(key=lambda x: x["subStart"])

    # 수요예측 알람: D 이벤트가 있으나 아직 S 청약 일정이 없는 종목 (30일 이내)
    alerts = []
    seen_names = set()
    for sn, d in by_sn.items():
        if not d["surveys"] or d["subs"]:
            continue
        future_s = [(b, e) for b, e in d["surveys"] if e >= today_str and b <= window]
        if not future_s:
            continue
        if d["name"] in seen_names:
            continue
        seen_names.add(d["name"])
        best_s = min(future_s, key=lambda x: x[0])
        alerts.append({"name": d["name"], "sStart": best_s[0], "sEnd": best_s[1], "seCd": d["se_cd"]})
    alerts.sort(key=lambda x: x["sStart"])

    return {"ipos": result, "alerts": alerts}


def main():
    print("피너츠 IPO 데이터 가져오는 중...\n")
    try:
        data = fetch_raw()
    except Exception as e:
        print(f"[오류] 데이터 수집 실패: {e}")
        return

    today_str = date.today().isoformat()
    result = parse(data, today_str)
    ipos = result["ipos"]
    alerts = result["alerts"]

    output_file = "ipo_data.json"
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"청약 예정 종목 수: {len(ipos)}개")
    print(f"수요예측 알람 종목 수: {len(alerts)}개")
    print(f"저장 완료: {output_file}")
    print()
    print("== 청약 예정 종목 (상위 10개) ==")
    for ipo in ipos[:10]:
        price = f"{ipo['issuePrice']:,}원" if ipo["issuePrice"] else "미확정"
        tag = " [SPAC]" if "SPAC" in ipo.get("note", "") else ""
        list_d = f" → 상장 {ipo['listDate']}" if ipo["listDate"] else ""
        print(f"  청약 {ipo['subStart']}~{ipo['subEnd']}  {ipo['name']}{tag}  공모가:{price}{list_d}")
    if len(ipos) > 10:
        print(f"  ... 외 {len(ipos)-10}개")
    if alerts:
        print()
        print("== 수요예측 예정 (알람) ==")
        for a in alerts:
            tag = " [SPAC]" if a["seCd"] == "SPAC" else ""
            print(f"  수요예측 {a['sStart']}~{a['sEnd']}  {a['name']}{tag}")
    print()
    print(f"→ 공모주 관리시트 대시보드에서 [JSON 가져오기] 버튼으로 {output_file}을 선택하세요.")
    print("  피너츠에 주관사가 공개된 종목은 관리시트 증권사명으로 자동 변환됩니다.")


if __name__ == "__main__":
    main()
