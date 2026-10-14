#!/usr/bin/env python3
"""시장 상태 실시간 갱신 (PC·토스 없이 클라우드에서 도는 단독 스크립트).

공개 시세만 받는다: Yahoo Finance(yfinance)와 네이버 증권 모바일 API. 계좌·키·비밀번호는 쓰지 않는다.
결과 `market.json`은 대시보드 페이지가 열릴 때 읽어 '시장 상태'와 '밀물과 파도'의 숫자만 덮는다.

    python market_live.py --out market.json [--prev-url https://raw.githubusercontent.com/<repo>/market-data/market.json]

의존: yfinance, requests (표준 라이브러리 외). ryu 패키지에 기대지 않는다 (ryu-board 저장소에서 단독 실행).
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
# key → yfinance 심볼
YAHOO = {"KOSPI": "^KS11", "KOSDAQ": "^KQ11", "USDKRW": "KRW=X", "NASDAQ": "^IXIC", "SOX": "^SOX",
         "BTC": "BTC-KRW", "VIX": "^VIX", "US10Y": "^TNX", "DXY": "DX-Y.NYB", "WTI": "CL=F"}
NAVER_INDEX = ("KOSPI", "KOSDAQ")
NAVER = "https://m.stock.naver.com/api/index/{key}/{what}"
UA = {"User-Agent": "Mozilla/5.0"}
SPARK_LEN = 21
HIST_KEEP = 12


def _num(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v) if v == v and abs(v) != float("inf") else None


def _diff(closes: list[float], n: int):
    if len(closes) <= n:
        return None, None
    base = closes[-1 - n]
    return closes[-1] - base, (closes[-1] / base - 1) if base else None


def entry(by_date: dict[str, float]) -> dict | None:
    """{날짜: 종가} → 대시보드 지표 항목 (가장 최근 종가 기준)."""
    days = sorted(by_date)
    closes = [by_date[d] for d in days]
    if not closes:
        return None
    d1, p1 = _diff(closes, 1)
    d20, p20 = _diff(closes, 20)
    hi, lo = max(closes), min(closes)
    pos = (closes[-1] - lo) / (hi - lo) if hi != lo else None
    r = lambda v: None if v is None else round(v, 6)  # noqa: E731
    return {"date": days[-1], "last": r(closes[-1]), "d1": r(d1), "p1": r(p1), "d20": r(d20), "p20": r(p20),
            "pos52": r(pos), "spark": [round(c, 4) for c in closes[-SPARK_LEN:]]}


def yahoo_series(download) -> dict[str, dict[str, float]]:
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        df = download(list(YAHOO.values()), period="1y", interval="1d", progress=False, auto_adjust=True)
    out: dict[str, dict[str, float]] = {}
    close = df["Close"]
    for key, sym in YAHOO.items():
        if sym not in getattr(close, "columns", []):
            continue
        s = close[sym].dropna()
        out[key] = {(i.strftime("%Y-%m-%d") if hasattr(i, "strftime") else str(i)[:10]): float(v)
                    for i, v in s.items()}
    return out


def _to_float(text) -> float | None:
    try:
        return float(str(text).replace(",", "").replace("+", ""))
    except ValueError:
        return None


def naver_index(session, key: str) -> dict[str, float]:
    """네이버 지수: 최근 25거래일 종가 + 장중이면 현재가(오늘 날짜로)."""
    out: dict[str, float] = {}
    r = session.get(NAVER.format(key=key, what="price"), params={"pageSize": 25, "page": 1}, headers=UA, timeout=15)
    r.raise_for_status()
    for row in r.json():
        v, d = _to_float(row.get("closePrice")), str(row.get("localTradedAt") or "")[:10]
        if v is not None and len(d) == 10:
            out[d] = v
    b = session.get(NAVER.format(key=key, what="basic"), headers=UA, timeout=15)
    b.raise_for_status()
    j = b.json()
    v, d = _to_float(j.get("closePrice")), str(j.get("localTradedAt") or "")[:10]
    if v is not None and len(d) == 10:
        out[d] = v
    return out


def naver_foreign(session) -> tuple[str, float] | None:
    """코스피 외국인 순매수 (억원, 순매도는 음수)와 기준일."""
    r = session.get(NAVER.format(key="KOSPI", what="trend"), headers=UA, timeout=15)
    r.raise_for_status()
    j = r.json()
    biz, v = str(j.get("bizdate") or ""), _to_float(j.get("foreignValue"))
    if len(biz) != 8 or v is None:
        return None
    return f"{biz[:4]}-{biz[4:6]}-{biz[6:]}", v


def streak(hist: dict[str, float]) -> int:
    """최신부터 같은 부호가 이어진 날 수 (순매수 +, 순매도 -)."""
    vals = [hist[d] for d in sorted(hist, reverse=True)]
    if not vals or vals[0] == 0:
        return 0
    sign = 1 if vals[0] > 0 else -1
    n = 0
    for v in vals:
        if v == 0 or (v > 0) != (sign > 0):
            break
        n += 1
    return sign * n


def build(prev: dict | None, download, session, now: datetime | None = None) -> dict | None:
    now = (now or datetime.now(KST)).astimezone(KST)
    sources: dict[str, str] = {}
    series: dict[str, dict[str, float]] = {}
    try:
        series = yahoo_series(download)
        sources["yahoo"] = "ok" if len(series) == len(YAHOO) else f"partial: {len(YAHOO) - len(series)}"
    except Exception as exc:  # noqa: BLE001
        sources["yahoo"] = f"error: {type(exc).__name__}"
    for key in NAVER_INDEX:   # 한국 지수는 네이버가 더 빠르다: 같은 날짜는 네이버 값으로 덮는다
        try:
            series.setdefault(key, {}).update(naver_index(session, key))
            sources.setdefault("naver", "ok")
        except Exception as exc:  # noqa: BLE001
            sources["naver"] = f"error: {type(exc).__name__}"
    indicators = {k: e for k, v in series.items() if (e := entry(v))}

    hist = {d: v for d, v in ((prev or {}).get("foreign_hist") or {}).items() if _num(v) is not None}
    flows: dict = {}
    try:
        got = naver_foreign(session)
        if got:
            hist[got[0]] = got[1]
            hist = dict(sorted(hist.items())[-HIST_KEEP:])
            flows["KOSPI"] = {"date": got[0], "foreign_net_krw": got[1] * 1e8, "foreign_streak": streak(hist)}
        sources["naver_flow"] = "ok" if got else "error: no-data"
    except Exception as exc:  # noqa: BLE001
        sources["naver_flow"] = f"error: {type(exc).__name__}"
    if not indicators and not flows:
        return None
    return {"updated_at": now.isoformat(timespec="seconds"), "sources": sources,
            "indicators": indicators, "market_flows": flows, "foreign_hist": hist}


def _core(doc: dict | None) -> dict | None:
    """갱신 시각·소스 상태를 뺀 실제 데이터."""
    if not isinstance(doc, dict):
        return None
    return {k: doc.get(k) for k in ("indicators", "market_flows", "foreign_hist")}


def same_data(prev: dict | None, new: dict | None) -> bool:
    """실제 시세가 지난번과 같은가. 지난 파일을 못 읽었으면 다르다고 본다."""
    return prev is not None and new is not None and _core(prev) == _core(new)


def _load_prev(url: str | None, session) -> dict | None:
    if not url:
        return None
    try:
        r = session.get(url, headers=UA, timeout=15)
        return r.json() if r.status_code == 200 else None
    except Exception:  # noqa: BLE001
        return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="market.json")
    ap.add_argument("--prev-url", default=None)
    args = ap.parse_args(argv)
    import requests
    import yfinance
    session = requests.Session()
    prev = _load_prev(args.prev_url, session)
    out = build(prev, yfinance.download, session)
    if out is None:
        print("시세를 하나도 받지 못했다", file=sys.stderr)
        return 1
    if same_data(prev, out):
        print("변동 없음: 파일을 만들지 않는다")
        return 0
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    print(f"{args.out}: 지표 {len(out['indicators'])}개, {out['sources']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
