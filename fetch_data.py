#!/usr/bin/env python3
"""지표 수집 -> data.json (표준 라이브러리만 사용).

출처
- 주가지수·원자재·환율·VIX·비트코인·섹터 ETF: Yahoo Finance chart API (비공식, 키 불필요)
- 미국채 10년: 미 재무부 일별 금리 CSV (실패 시 FRED로 대체, 키 불필요)
- 국고채 3년: 한국은행 ECOS (817Y002, 키 필요: 환경변수 ECOS_API_KEY)

수집 방식
- 15분마다 실행되지만, 지표별로 "지금 수집할 때인지" 판단한다.
  · 장이 열린 구간(cal)에는 매번 수집, 닫힌 구간에는 IDLE_MINUTES(6시간)에 한 번만 확인
  · 일 단위 지표(every)는 지정한 분 간격으로만 수집
  · 이전 수집이 실패한 지표는 장이 닫혀 있어도 다시 시도
- 건너뛴 지표는 이전 data.json 값을 그대로 유지한다.
- 시리즈별로 실패해도 나머지는 계속 진행하고, 실패한 시리즈는 이전 값을 유지하되 stale=true 로 표시한다.
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
from zoneinfo import ZoneInfo

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.json")
KEEP_DAYS = 450  # 보관 기간(일): 8개월 표시 + 120일 이동평균 계산용
UA = {"User-Agent": "Mozilla/5.0 (stock-dashboard)"}
TIMEOUT = 30
DEFAULT_MA = [20, 60, 120]
DEFAULT_MONTHS = 8
IDLE_MINUTES = 360  # 장이 닫혀 있을 때 확인 주기(분)

# (id, 표시명, 그룹, 단위, 소수자리, kind, (출처, 코드), 추가설정)
#  kind: pct = 등락률(%), bp = 금리 변화(bp)
#  추가설정
#    cal    : 수집 구간 (kr, jp, us, vix, fx, always)
#    every  : 일 단위 지표의 수집 간격(분). cal 대신 사용
#    candle : True면 시가·고가·저가도 저장(캔들 차트)
#    ma / months / note / view : 화면 설정
SECTORS = [("xlk", "기술", "XLK"), ("xlf", "금융", "XLF"), ("xle", "에너지", "XLE"),
           ("xlv", "헬스케어", "XLV"), ("xly", "경기소비재", "XLY"), ("xlp", "필수소비재", "XLP"),
           ("xli", "산업재", "XLI"), ("xlb", "소재", "XLB"), ("xlu", "유틸리티", "XLU"),
           ("xlre", "부동산", "XLRE"), ("xlc", "커뮤니케이션", "XLC")]

SERIES = [
    ("kospi", "코스피", "주가지수", "", 2, "pct", ("yahoo", "^KS11"), {"cal": "kr", "candle": True}),
    ("kosdaq", "코스닥", "주가지수", "", 2, "pct", ("yahoo", "^KQ11"), {"cal": "kr", "candle": True}),
    ("nikkei", "니케이 225", "주가지수", "", 2, "pct", ("yahoo", "^N225"), {"cal": "jp", "candle": True}),
    ("nasdaq", "나스닥 종합", "주가지수", "", 2, "pct", ("yahoo", "^IXIC"), {"cal": "us", "candle": True}),
    ("spx", "S&P 500", "주가지수", "", 2, "pct", ("yahoo", "^GSPC"), {"cal": "us", "candle": True}),
    ("djt", "다우 운송", "주가지수", "", 2, "pct", ("yahoo", "^DJT"), {"cal": "us", "candle": True}),
    ("ewy", "EWY (한국 ETF, 달러)", "주가지수", "$", 2, "pct", ("yahoo", "EWY"), {"cal": "us", "candle": True}),
    ("us10y", "미국채 10년", "금리", "%", 3, "bp", ("ust", ("10 Yr", "DGS10")), {"every": 180}),
    ("kr3y", "국고채 3년", "금리", "%", 3, "bp", ("ecos", "국고채(3년)"), {"every": 180}),
    ("wti", "WTI 원유", "원자재·환율", "$", 2, "pct", ("yahoo", "CL=F"), {"cal": "fx", "candle": True}),
    ("gold", "금", "원자재·환율", "$", 1, "pct", ("yahoo", "GC=F"), {"cal": "fx", "candle": True}),
    ("usdkrw", "원/달러", "원자재·환율", "원", 2, "pct", ("yahoo", "KRW=X"),
     {"cal": "fx", "note": "↑ 원화 약세"}),
    ("usdjpy", "달러/엔", "원자재·환율", "엔", 2, "pct", ("yahoo", "JPY=X"),
     {"cal": "fx", "note": "↑ 엔화 약세"}),
    ("usdidr", "달러/루피아", "원자재·환율", "루피아", 0, "pct", ("yahoo", "IDR=X"),
     {"cal": "fx", "note": "↑ 루피아 약세"}),
    ("dxy", "달러 인덱스", "원자재·환율", "", 2, "pct", ("yahoo", "DX-Y.NYB"), {"cal": "fx"}),
    ("vix", "VIX", "위험·심리", "", 2, "pct", ("yahoo", "^VIX"),
     {"cal": "vix", "candle": True, "ma": [10, 20], "months": 3}),
    ("btc", "비트코인", "위험·심리", "$", 0, "pct", ("yahoo", "BTC-USD"),
     {"cal": "always", "candle": True}),
    ("samsung", "삼성전자", "반도체 주식", "원", 0, "pct", ("yahoo", "005930.KS"), {"cal": "kr", "candle": True}),
    ("hynix", "SK하이닉스", "반도체 주식", "원", 0, "pct", ("yahoo", "000660.KS"), {"cal": "kr", "candle": True}),
    ("mu", "마이크론", "반도체 주식", "$", 2, "pct", ("yahoo", "MU"), {"cal": "us", "candle": True}),
    ("sndk", "샌디스크", "반도체 주식", "$", 2, "pct", ("yahoo", "SNDK"), {"cal": "us", "candle": True}),
    ("tsm", "TSMC (ADR)", "반도체 주식", "$", 2, "pct", ("yahoo", "TSM"), {"cal": "us", "candle": True}),
] + [
    (sid, "%s (%s)" % (name, sym), "미국 섹터", "$", 2, "pct", ("yahoo", sym),
     {"cal": "us", "candle": True, "view": "table"})
    for sid, name, sym in SECTORS
]

DATA_KEYS = ("points", "ohlc", "last", "as_of", "prev", "change", "change_pct", "fetched_at")


def utcnow():
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------- 수집 구간 판단
def _minutes(t):
    return t.hour * 60 + t.minute


def window_open(cal, now):
    """지금(now, UTC aware)이 해당 시장의 수집 구간인지. 공휴일은 반영하지 않는다."""
    if cal == "always":
        return True
    if cal in ("kr", "jp"):  # 평일 08:30~16:30 (장 시작 30분 전 ~ 마감 후 1시간)
        t = now.astimezone(ZoneInfo("Asia/Seoul" if cal == "kr" else "Asia/Tokyo"))
        return t.weekday() < 5 and 8 * 60 + 30 <= _minutes(t) <= 16 * 60 + 30
    et = now.astimezone(ZoneInfo("America/New_York"))
    m, wd = _minutes(et), et.weekday()
    if cal == "us":  # 미국 현지 평일 09:00~17:00
        return wd < 5 and 9 * 60 <= m <= 17 * 60
    if cal == "vix":  # 미국 현지 평일 03:00~17:00
        return wd < 5 and 3 * 60 <= m <= 17 * 60
    if cal == "fx":  # 24시간 거래: 일요일 17:00(ET) 개장 ~ 금요일 17:00 마감
        if wd == 5:
            return False
        if wd == 6:
            return m >= 17 * 60
        if wd == 4:
            return m <= 17 * 60 + 5
        return True
    raise ValueError("알 수 없는 cal: %s" % cal)


def is_due(extra, old, now):
    """이번 실행에서 이 지표를 수집해야 하는지."""
    if not old or not old.get("points") or old.get("error") or not old.get("fetched_at"):
        return True
    if extra.get("candle") and not old.get("ohlc"):
        return True  # 캔들 데이터가 아직 없는 이전 형식
    try:
        last = datetime.fromisoformat(old["fetched_at"])
    except ValueError:
        return True
    age = (now - last).total_seconds() / 60
    if "every" in extra:
        return age >= extra["every"]
    if window_open(extra["cal"], now):
        return True
    return age >= IDLE_MINUTES


# ---------------------------------------------------------------- 수집기
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
    """[날짜, 종가, 시가, 고가, 저가] 목록. 같은 날짜가 중복되면 마지막 값을 쓴다."""
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
    q = res["indicators"]["quote"][0]
    n = len(res["timestamp"])
    closes = q["close"]
    opens = q.get("open") or [None] * n
    highs = q.get("high") or [None] * n
    lows = q.get("low") or [None] * n
    by_date = {}
    for i, ts in enumerate(res["timestamp"]):
        if closes[i] is None:
            continue
        d = datetime.fromtimestamp(ts + off, tz=timezone.utc).date().isoformat()
        by_date[d] = [d, float(closes[i]), opens[i], highs[i], lows[i]]
    return [by_date[k] for k in sorted(by_date)]


def fetch_fred(series_id):
    start = (utcnow() - timedelta(days=KEEP_DAYS + 20)).date().isoformat()
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
    year = utcnow().year
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
    today = utcnow().date()
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


FETCHERS = {"yahoo": fetch_yahoo, "fred": fetch_fred, "ecos": fetch_ecos, "ust": fetch_ust}
SOURCE_LABEL = {"yahoo": "Yahoo Finance", "fred": "FRED", "ecos": "한국은행 ECOS",
                "ust": "미 재무부(FRED 보조)"}


# ---------------------------------------------------------------- 가공
def summarize(pts, kind, candle=False, dec=2):
    """pts: [날짜, 종가(, 시가, 고가, 저가)]. 최근 KEEP_DAYS만 남기고 변화량을 계산한다."""
    last_d = datetime.strptime(pts[-1][0], "%Y-%m-%d")
    cutoff = (last_d - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
    pts = [p for p in pts if p[0] >= cutoff]
    nd = dec + 2  # 저장 자릿수(용량 절감)
    closes = [[p[0], round(p[1], nd)] for p in pts]
    last, prev = closes[-1], closes[-2] if len(closes) > 1 else None
    out = {"points": closes, "last": last[1], "as_of": last[0]}
    if candle:
        ohlc = []
        for p in pts:
            if len(p) >= 5 and None not in p[2:5]:
                ohlc.append([round(p[2], nd), round(p[3], nd), round(p[4], nd)])
            else:  # 값이 비면 종가로 대신 (납작한 캔들)
                ohlc.append([round(p[1], nd)] * 3)
        out["ohlc"] = ohlc
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
    for k in ("note", "view"):
        if k in extra:
            item[k] = extra[k]
    if extra.get("candle"):
        item["candle"] = True
    return item


def main():
    now = utcnow()
    try:
        with open(OUT, encoding="utf-8") as f:
            old = {s["id"]: s for s in json.load(f).get("series", [])}
    except (OSError, ValueError):
        old = {}

    items, attempted, failures, skipped = {}, 0, 0, 0
    for spec in SERIES:
        sid, dec, kind = spec[0], spec[4], spec[5]
        stype, code = spec[6]
        extra = spec[7]
        item = make_item(spec)
        prev = old.get(sid)

        if not is_due(extra, prev, now):  # 장이 닫혀 있거나 아직 수집 간격 전: 이전 값 유지
            item.update({k: prev[k] for k in DATA_KEYS if k in prev})
            items[sid] = item
            skipped += 1
            continue

        attempted += 1
        try:
            pts = FETCHERS[stype](code)
            if not pts:
                raise RuntimeError("데이터 없음")
            item.update(summarize(pts, kind, extra.get("candle", False), dec))
            item["fetched_at"] = now.isoformat(timespec="seconds")
        except Exception as exc:  # noqa: BLE001 - 시리즈 단위로 격리
            failures += 1
            print("[WARN] %s 실패: %s" % (sid, exc), file=sys.stderr)
            if prev and prev.get("points"):
                item.update({k: prev[k] for k in DATA_KEYS if k in prev})
                item["stale"] = True
            item["error"] = str(exc)
        items[sid] = item

    if attempted == 0:
        print("수집 대상 없음 (%d개 모두 장 마감/간격 대기)" % skipped)
        return 0
    if failures == attempted:
        print("[ERROR] 시도한 %d개 모두 실패 - data.json 갱신 안 함" % attempted, file=sys.stderr)
        return 1

    payload = {"generated_at": now.isoformat(timespec="seconds"),
               "series": [items[s[0]] for s in SERIES]}
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    print("OK: 수집 %d개 중 성공 %d개, 건너뜀 %d개 (전체 %d개)"
          % (attempted, attempted - failures, skipped, len(SERIES)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
