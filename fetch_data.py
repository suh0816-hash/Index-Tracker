#!/usr/bin/env python3
"""지표 수집 -> data.json (표준 라이브러리만 사용).

출처
- 코스피/코스닥/나스닥/S&P500/WTI/원달러: Yahoo Finance chart API (비공식, 키 불필요)
- 미국채 2Y/10Y: FRED (DGS2, DGS10) fredgraph.csv (키 불필요)
- 한국 국고채 3Y/10Y: 한국은행 ECOS (817Y002, 키 필요: 환경변수 ECOS_API_KEY)

시리즈별로 실패해도 나머지는 계속 진행하고, 실패한 시리즈는 이전 data.json 값을
유지하되 stale=true 로 표시한다.
"""
import csv
import io
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.json")
KEEP = 90  # 보관할 최근 관측치 수
UA = {"User-Agent": "Mozilla/5.0 (stock-dashboard)"}
TIMEOUT = 30

# kind: pct = 등락률(%), bp = 금리 변화(bp)
SERIES = [
    # id, 표시명, 그룹, 단위, 소수자리, kind, 출처(type, 코드)
    ("kospi", "코스피", "주가지수", "", 2, "pct", ("yahoo", "^KS11")),
    ("kosdaq", "코스닥", "주가지수", "", 2, "pct", ("yahoo", "^KQ11")),
    ("nasdaq", "나스닥 종합", "주가지수", "", 2, "pct", ("yahoo", "^IXIC")),
    ("spx", "S&P 500", "주가지수", "", 2, "pct", ("yahoo", "^GSPC")),
    ("us2y", "미국채 2년", "금리", "%", 3, "bp", ("fred", "DGS2")),
    ("us10y", "미국채 10년", "금리", "%", 3, "bp", ("fred", "DGS10")),
    ("kr3y", "국고채 3년", "금리", "%", 3, "bp", ("ecos", "국고채(3년)")),
    ("kr10y", "국고채 10년", "금리", "%", 3, "bp", ("ecos", "국고채(10년)")),
    ("wti", "WTI 원유", "원자재·환율", "$", 2, "pct", ("yahoo", "CL=F")),
    ("usdkrw", "원/달러", "원자재·환율", "원", 2, "pct", ("yahoo", "KRW=X")),
]


def http_get(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read().decode("utf-8")


def fetch_yahoo(symbol):
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/"
           + urllib.parse.quote(symbol, safe="")
           + "?range=6mo&interval=1d")
    res = json.loads(http_get(url))["chart"]["result"][0]
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
    start = (datetime.now(timezone.utc) - timedelta(days=200)).date().isoformat()
    url = ("https://fred.stlouisfed.org/graph/fredgraph.csv?id=%s&cosd=%s"
           % (series_id, start))
    rows = list(csv.reader(io.StringIO(http_get(url))))
    pts = []
    for row in rows[1:]:  # 헤더 제외 (observation_date/DATE 모두 대응)
        if len(row) < 2 or row[1] in ("", "."):
            continue
        pts.append([row[0], float(row[1])])
    return pts


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
    s = (today - timedelta(days=200)).strftime("%Y%m%d")
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


FETCHERS = {"yahoo": fetch_yahoo, "fred": fetch_fred, "ecos": fetch_ecos}
SOURCE_LABEL = {"yahoo": "Yahoo Finance", "fred": "FRED", "ecos": "한국은행 ECOS"}


def summarize(pts, kind):
    pts = pts[-KEEP:]
    last, prev = pts[-1], pts[-2] if len(pts) > 1 else None
    out = {"points": pts, "last": last[1], "as_of": last[0]}
    if prev:
        diff = last[1] - prev[1]
        out["prev"] = prev[1]
        out["change"] = round(diff * 100, 2) if kind == "bp" else round(diff, 4)
        out["change_pct"] = (round(diff / prev[1] * 100, 3)
                             if kind == "pct" and prev[1] else None)
    return out


def main():
    try:
        with open(OUT, encoding="utf-8") as f:
            old = {s["id"]: s for s in json.load(f).get("series", [])}
    except (OSError, ValueError):
        old = {}

    result, failures = [], 0
    for sid, label, group, unit, dec, kind, (stype, code) in SERIES:
        item = {"id": sid, "label": label, "group": group, "unit": unit,
                "decimals": dec, "kind": kind, "source": SOURCE_LABEL[stype]}
        try:
            pts = FETCHERS[stype](code)
            if not pts:
                raise RuntimeError("데이터 없음")
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
        result.append(item)

    if failures == len(SERIES):
        print("[ERROR] 모든 시리즈 실패 - data.json 갱신 안 함", file=sys.stderr)
        return 1

    payload = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "series": result}
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    print("OK: %d/%d 시리즈 갱신" % (len(SERIES) - failures, len(SERIES)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
