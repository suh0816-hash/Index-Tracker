#!/usr/bin/env python3
"""지표 수집 -> data.json (표준 라이브러리만 사용).

출처
- 주가지수·원자재·환율·VIX·비트코인·EWY: Yahoo Finance chart API (비공식, 키 불필요)
- 미국채 2/10/30년: 미 재무부 일별 금리 CSV (실패 시 FRED로 대체, 키 불필요)
- 국고채 3년: 한국은행 ECOS (817Y002, 키 필요: 환경변수 ECOS_API_KEY)
- EWY(원화환산): EWY(달러) x 원/달러 를 계산 (환율 영향 제거)

시리즈별로 실패해도 나머지는 계속 진행하고, 실패한 시리즈는 이전 data.json 값을
유지하되 stale=true 로 표시한다.
"""
import csv
import io
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.json")
KEEP_DAYS = 450  # 보관 기간(일): 8개월 표시 + 120일 이동평균 계산용
UA = {"User-Agent": "Mozilla/5.0 (stock-dashboard)"}
TIMEOUT = 30
DEFAULT_MA = [20, 60, 120]
DEFAULT_MONTHS = 8

# (id, 표시명, 그룹, 단위, 소수자리, kind, (출처, 코드), 추가설정)
#  kind: pct = 등락률(%), bp = 금리 변화(bp)
#  추가설정: ma(이동평균 기간 목록), months(상세 차트 표시 개월), note(카드 보조 문구)
SERIES = [
    ("kospi", "코스피", "주가지수", "", 2, "pct", ("yahoo", "^KS11"), {}),
    ("kosdaq", "코스닥", "주가지수", "", 2, "pct", ("yahoo", "^KQ11"), {}),
    ("ewy", "EWY (달러)", "주가지수", "$", 2, "pct", ("yahoo", "EWY"),
     {"note": "미국 상장 한국 ETF"}),
    ("ewy_krw", "EWY (원화환산)", "주가지수", "원", 0, "pct", ("calc", ("ewy", "usdkrw")),
     {"note": "환율 영향 제거"}),
    ("nikkei", "니케이 225", "주가지수", "", 2, "pct", ("yahoo", "^N225"), {}),
    ("nasdaq", "나스닥 종합", "주가지수", "", 2, "pct", ("yahoo", "^IXIC"), {}),
    ("spx", "S&P 500", "주가지수", "", 2, "pct", ("yahoo", "^GSPC"), {}),
    ("us2y", "미국채 2년", "금리", "%", 3, "bp", ("ust", ("2 Yr", "DGS2")), {}),
    ("us10y", "미국채 10년", "금리", "%", 3, "bp", ("ust", ("10 Yr", "DGS10")), {}),
    ("us30y", "미국채 30년", "금리", "%", 3, "bp", ("ust", ("30 Yr", "DGS30")), {}),
    ("kr3y", "국고채 3년", "금리", "%", 3, "bp", ("ecos", "국고채(3년)"), {}),
    ("wti", "WTI 원유", "원자재·환율", "$", 2, "pct", ("yahoo", "CL=F"), {}),
    ("gold", "금", "원자재·환율", "$", 1, "pct", ("yahoo", "GC=F"), {}),
    ("usdkrw", "원/달러", "원자재·환율", "원", 2, "pct", ("yahoo", "KRW=X"),
     {"note": "↑ 원화 약세"}),
    ("usdjpy", "달러/엔", "원자재·환율", "엔", 2, "pct", ("yahoo", "JPY=X"),
     {"note": "↑ 엔화 약세"}),
    ("usdidr", "달러/루피아", "원자재·환율", "루피아", 0, "pct", ("yahoo", "IDR=X"),
     {"note": "↑ 루피아 약세"}),
    ("dxy", "달러 인덱스", "원자재·환율", "", 2, "pct", ("yahoo", "DX-Y.NYB"), {}),
    ("vix", "VIX", "위험·심리", "", 2, "pct", ("yahoo", "^VIX"),
     {"ma": [10, 20], "months": 3}),
    ("btc", "비트코인", "위험·심리", "$", 0, "pct", ("yahoo", "BTC-USD"), {}),
]


def http_get(url):
    last = None
    for attempt in range(2):  # 일시 오류 대비 1회 재시도
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return r.read().decode("utf-8")
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(1)
    raise last


def fetch_yahoo(symbol):
    path = ("/v8/finance/chart/" + urllib.parse.quote(symbol, safe="")
            + "?range=2y&interval=1d")
    last = None
    for host in ("query1.finance.yahoo.com", "query2.finance.yahoo.com"):
        try:
            res = json.loads(http_get("https://" + host + path))["chart"]["result"][0]
            break
        except Exception as exc:  # noqa: BLE001
            last = exc
    else:
        raise last
    off = res["meta"].get("gmtoffset", 0)
    closes = res["indicators"]["quote"][0]["close"]
    pts = []
    for ts, c in zip(res["timestamp"], closes):
        if c is None:
            continue
        d = datetime.fromtimestamp(ts + off, tz=timezone.utc).date().isoformat()
        pts.append([d, float(c)])
    return pts


def fetch_fred(series_id):
    start = (datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS + 20)).date().isoformat()
    url = ("https://fred.stlouisfed.org/graph/fredgraph.csv?id=%s&cosd=%s"
           % (series_id, start))
    rows = list(csv.reader(io.StringIO(http_get(url))))
    pts = []
    for row in rows[1:]:  # 헤더 제외 (observation_date/DATE 모두 대응)
        if len(row) < 2 or row[1] in ("", "."):
            continue
        pts.append([row[0], float(row[1])])
    return pts


def fetch_treasury(col, _cache={}):
    """미 재무부 Daily Par Yield Curve CSV (연도별). col 예: '10 Yr'."""
    year = datetime.now(timezone.utc).year
    pts = {}
    for y in (year - 1, year):
        if y not in _cache:
            url = ("https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
                   "daily-treasury-rates.csv/%d/all?type=daily_treasury_yield_curve"
                   "&field_tdr_date_value=%d&page&_format=csv" % (y, y))
            _cache[y] = list(csv.reader(io.StringIO(http_get(url))))
        rows = _cache[y]
        header = [h.strip() for h in rows[0]]
        if col not in header:
            raise RuntimeError("열 없음: " + col)
        ci = header.index(col)
        for r in rows[1:]:
            if len(r) <= ci or not r[ci].strip():
                continue
            mm, dd, yy = r[0].strip().split("/")
            pts["%s-%s-%s" % (yy, mm, dd)] = float(r[ci])
    if not pts:
        raise RuntimeError("데이터 없음")
    return [[k, pts[k]] for k in sorted(pts)]


def fetch_ust(code):
    """미국채: 재무부 CSV 우선, 실패 시 FRED로 대체."""
    col, fred_id = code
    try:
        return fetch_treasury(col)
    except Exception as e1:  # noqa: BLE001
        try:
            return fetch_fred(fred_id)
        except Exception as e2:  # noqa: BLE001
            raise RuntimeError("재무부 %s / FRED %s" % (str(e1)[:50], str(e2)[:50]))


def _ecos(path):
    data = json.loads(http_get("https://ecos.bok.or.kr/api/" + path))
    if "RESULT" in data:
        raise RuntimeError("ECOS: %s" % data["RESULT"].get("MESSAGE"))
    return data


def fetch_ecos(item_name, _cache={}):
    key = os.environ.get("ECOS_API_KEY", "").strip()
    if not key:
        raise RuntimeError("ECOS_API_KEY 없음")
    stat = "817Y002"  # 시장금리(일별)
    if "items" not in _cache:
        lst = _ecos("StatisticItemList/%s/json/kr/1/200/%s" % (key, stat))
        _cache["items"] = lst["StatisticItemList"]["row"]
    want = item_name.replace(" ", "")
    code = None
    for r in _cache["items"]:
        if r["ITEM_NAME"].replace(" ", "") == want:
            code = r["ITEM_CODE"]
            break
    if code is None:
        raise RuntimeError("ECOS 항목 코드를 찾지 못함: " + item_name)
    today = datetime.now(timezone.utc).date()
    s = (today - timedelta(days=KEEP_DAYS + 20)).strftime("%Y%m%d")
    e = today.strftime("%Y%m%d")
    res = _ecos("StatisticSearch/%s/json/kr/1/500/%s/D/%s/%s/%s"
                % (key, stat, s, e, code))
    pts = []
    for r in res["StatisticSearch"]["row"]:
        t, v = r["TIME"], r["DATA_VALUE"]
        if v in ("", None):
            continue
        pts.append(["%s-%s-%s" % (t[:4], t[4:6], t[6:8]), float(v)])
    pts.sort()
    return pts


def calc_product(a, b):
    """a의 각 날짜 값 x (그 날짜 이전 가장 최근 b 값). 예: EWY(달러) x 원/달러."""
    out, j, last = [], 0, None
    b = sorted(b)
    for d, v in sorted(a):
        while j < len(b) and b[j][0] <= d:
            last = b[j][1]
            j += 1
        if last is not None:
            out.append([d, v * last])
    if not out:
        raise RuntimeError("겹치는 날짜 없음")
    return out


FETCHERS = {"yahoo": fetch_yahoo, "fred": fetch_fred, "ecos": fetch_ecos, "ust": fetch_ust}
SOURCE_LABEL = {"yahoo": "Yahoo Finance", "fred": "FRED", "ecos": "한국은행 ECOS",
                "ust": "미 재무부(FRED 보조)", "calc": "계산: EWY × 원/달러"}


def summarize(pts, kind):
    last_d = datetime.strptime(pts[-1][0], "%Y-%m-%d")
    cutoff = (last_d - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
    pts = [p for p in pts if p[0] >= cutoff]
    last, prev = pts[-1], pts[-2] if len(pts) > 1 else None
    out = {"points": pts, "last": last[1], "as_of": last[0]}
    if prev:
        diff = last[1] - prev[1]
        out["prev"] = prev[1]
        out["change"] = round(diff * 100, 2) if kind == "bp" else round(diff, 4)
        out["change_pct"] = (round(diff / prev[1] * 100, 3)
                             if kind == "pct" and prev[1] else None)
    return out


def make_item(spec):
    sid, label, group, unit, dec, kind, (stype, _code), extra = spec
    item = {"id": sid, "label": label, "group": group, "unit": unit, "decimals": dec,
            "kind": kind, "source": SOURCE_LABEL[stype],
            "ma": extra.get("ma", DEFAULT_MA), "months": extra.get("months", DEFAULT_MONTHS)}
    if "note" in extra:
        item["note"] = extra["note"]
    return item


def main():
    try:
        with open(OUT, encoding="utf-8") as f:
            old = {s["id"]: s for s in json.load(f).get("series", [])}
    except (OSError, ValueError):
        old = {}

    raw, items, failures = {}, {}, 0

    def apply(spec, getter):
        nonlocal failures
        sid, kind = spec[0], spec[5]
        item = make_item(spec)
        try:
            pts = getter()
            if not pts:
                raise RuntimeError("데이터 없음")
            raw[sid] = pts
            item.update(summarize(pts, kind))
        except Exception as exc:  # noqa: BLE001 - 시리즈 단위로 격리
            failures += 1
            print("[WARN] %s 실패: %s" % (sid, exc), file=sys.stderr)
            prev = old.get(sid)
            if prev and prev.get("points"):
                item.update({k: prev[k] for k in
                             ("points", "last", "as_of", "prev", "change",
                              "change_pct") if k in prev})
                item["stale"] = True
            item["error"] = str(exc)
        items[sid] = item

    for spec in SERIES:  # 1차: 외부 수집
        stype, code = spec[6]
        if stype != "calc":
            apply(spec, lambda f=FETCHERS[stype], c=code: f(c))

    for spec in SERIES:  # 2차: 계산 지표 (수집 결과에 의존)
        stype, code = spec[6]
        if stype == "calc":
            a, b = code

            def getter(a=a, b=b):
                if a not in raw or b not in raw:
                    raise RuntimeError("입력 지표 수집 실패: %s, %s" % (a, b))
                return calc_product(raw[a], raw[b])
            apply(spec, getter)

    if failures == len(SERIES):
        print("[ERROR] 모든 시리즈 실패 - data.json 갱신 안 함", file=sys.stderr)
        return 1

    payload = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "series": [items[s[0]] for s in SERIES]}
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    print("OK: %d/%d 시리즈 갱신" % (len(SERIES) - failures, len(SERIES)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
