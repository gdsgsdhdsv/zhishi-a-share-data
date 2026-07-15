#!/usr/bin/env python3
"""Build a fail-closed A-share market snapshot from AKShare public interfaces."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import akshare as ak
import pandas as pd
import requests


_ORIGINAL_REQUEST = requests.sessions.Session.request
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
}


def _curl_get(
    candidates: list[str],
    headers: dict[str, str],
    params: dict[str, Any] | None,
    timeout: float,
) -> requests.Response | None:
    for candidate in candidates:
        prepared = requests.Request(
            method="GET",
            url=candidate,
            params=params,
            headers=headers,
        ).prepare()
        command = [
            "curl",
            "--fail",
            "--silent",
            "--show-error",
            "--location",
            "--max-time",
            str(max(5, int(timeout))),
            prepared.url,
        ]
        for key, item in headers.items():
            command[1:1] = ["--header", f"{key}: {item}"]
        try:
            result = subprocess.run(
                command,
                check=True,
                capture_output=True,
                timeout=timeout + 5,
            )
            response = requests.Response()
            response.status_code = 200
            response._content = result.stdout
            response.url = prepared.url
            response.encoding = "utf-8"
            return response
        except (subprocess.SubprocessError, OSError):
            continue
    return None


def _resilient_public_request(
    session: requests.Session,
    method: str,
    url: str,
    **kwargs: Any,
) -> requests.Response:
    """Use browser-like headers and alternate Eastmoney hosts when one edge drops."""
    headers = dict(_BROWSER_HEADERS)
    headers.update(kwargs.pop("headers", {}) or {})
    kwargs["headers"] = headers
    candidates = [url]
    if "82.push2.eastmoney.com" in url:
        candidates.extend(
            [
                url.replace("82.push2.eastmoney.com", "push2.eastmoney.com"),
                url.replace("82.push2.eastmoney.com", "6.push2.eastmoney.com"),
            ]
        )
        page_number = int((kwargs.get("params") or {}).get("pn", 1))
        offset = (page_number - 1) % len(candidates)
        candidates = candidates[offset:] + candidates[:offset]
    timeout = float(kwargs.get("timeout", 15))
    if method.upper() == "GET" and "push2.eastmoney.com" in url:
        curl_response = _curl_get(candidates, headers, kwargs.get("params"), timeout)
        if curl_response is not None:
            return curl_response
    last_error: requests.RequestException | None = None
    for candidate in candidates:
        try:
            response = _ORIGINAL_REQUEST(session, method, candidate, **kwargs)
            response.raise_for_status()
            return response
        except requests.RequestException as error:
            last_error = error
    if method.upper() == "GET":
        curl_response = _curl_get(candidates, headers, kwargs.get("params"), timeout)
        if curl_response is not None:
            return curl_response
    assert last_error is not None
    raise last_error


requests.sessions.Session.request = _resilient_public_request

SHANGHAI = ZoneInfo("Asia/Shanghai")
FACTORS = (
    ("trend", "趋势结构", 18),
    ("volume", "量价配合", 12),
    ("funds", "资金行为", 16),
    ("industry", "行业强度", 12),
    ("valuation", "估值位置", 12),
    ("momentum", "相对动量", 10),
    ("liquidity", "流动性", 10),
    ("risk", "风险质量", 10),
)


def clean_columns(frame: pd.DataFrame) -> pd.DataFrame:
    frame = frame.copy()
    frame.columns = [re.sub(r"\s+", "", str(column)) for column in frame.columns]
    return frame


def finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def value(row: pd.Series | dict[str, Any] | None, *keys: str) -> float | None:
    if row is None:
        return None
    for key in keys:
        if key in row:
            result = finite(row[key])
            if result is not None:
                return result
    return None


def text_value(row: pd.Series | dict[str, Any] | None, *keys: str, fallback: str = "") -> str:
    if row is None:
        return fallback
    for key in keys:
        if key in row and pd.notna(row[key]):
            result = str(row[key]).strip()
            if result:
                return result
    return fallback


def clamp(number: float, minimum: float = 0, maximum: float = 100) -> float:
    return min(maximum, max(minimum, number))


def rounded(number: float | None, digits: int = 1) -> float | None:
    return None if number is None else round(number, digits)


def records_by_code(frame: pd.DataFrame) -> dict[str, dict[str, Any]]:
    if frame.empty or "代码" not in frame.columns:
        return {}
    records: dict[str, dict[str, Any]] = {}
    for record in frame.to_dict("records"):
        code = str(record.get("代码", "")).zfill(6)
        if re.fullmatch(r"\d{6}", code):
            records[code] = record
    return records


def is_trading_day(today: datetime) -> bool:
    calendar = ak.tool_trade_date_hist_sina()
    if calendar.empty:
        raise RuntimeError("交易日历为空")
    column = "trade_date" if "trade_date" in calendar.columns else calendar.columns[0]
    dates = set(pd.to_datetime(calendar[column]).dt.strftime("%Y%m%d"))
    return today.strftime("%Y%m%d") in dates


ALL_STOCKS = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"


def eastmoney_frame(
    *,
    fields: dict[str, str],
    sort_field: str,
    stock_filter: str,
    extra: dict[str, str] | None = None,
    limit: int | None = None,
) -> pd.DataFrame:
    """Fetch only the fields used by the dashboard to avoid oversized edge requests."""
    url = "https://82.push2.eastmoney.com/api/qt/clist/get"
    base = {
        "po": "1",
        "np": "1",
        "fltt": "2",
        "invt": "2",
        "fid": sort_field,
        "fs": stock_filter,
        "fields": ",".join(fields),
        "pz": str(min(limit or 500, 500)),
    }
    if extra:
        base.update(extra)
    records: list[dict[str, Any]] = []
    page = 1
    expected_total: int | None = None
    while True:
        params = {**base, "pn": str(page)}
        response: requests.Response | None = None
        for attempt in range(4):
            try:
                response = requests.get(url, params=params, timeout=20)
                break
            except requests.RequestException:
                if attempt == 3:
                    raise
                time.sleep((attempt + 1) * 4)
        assert response is not None
        payload = response.json()
        data = payload.get("data") or {}
        batch = data.get("diff") or []
        if expected_total is None:
            expected_total = int(data.get("total") or 0)
        records.extend(batch)
        if not batch or len(records) >= expected_total or (limit is not None and len(records) >= limit):
            break
        time.sleep(0.8)
        page += 1
        if page > 80:
            raise RuntimeError("公开行情分页异常")
    if limit is None and expected_total and len(records) < min(expected_total, 4500):
        raise RuntimeError(f"公开行情分页不完整: {len(records)}/{expected_total}")
    frame = pd.DataFrame(records[:limit] if limit is not None else records)
    frame.rename(columns=fields, inplace=True)
    return frame[[name for name in fields.values() if name in frame.columns]]


def load_frames() -> dict[str, pd.DataFrame]:
    quotes = clean_columns(ak.stock_zh_a_spot())
    quotes["代码"] = quotes["代码"].astype(str).str.extract(r"(\d{6})", expand=False)
    return {
        "quotes": quotes,
        "main": eastmoney_frame(
            fields={
                "f12": "代码", "f14": "名称", "f184": "今日排行榜-主力净占比",
                "f165": "5日排行榜-主力净占比", "f109": "5日排行榜-5日涨跌",
                "f100": "所属板块",
            },
            sort_field="f184",
            stock_filter=ALL_STOCKS,
            limit=500,
        ),
        "flow_today": eastmoney_frame(
            fields={
                "f12": "代码", "f14": "名称", "f62": "今日主力净流入-净额",
                "f184": "今日主力净流入-净占比",
            },
            sort_field="f62",
            stock_filter=ALL_STOCKS,
            limit=500,
        ),
        "flow_5": eastmoney_frame(
            fields={
                "f12": "代码", "f14": "名称", "f164": "5日主力净流入-净额",
                "f165": "5日主力净流入-净占比",
            },
            sort_field="f164",
            stock_filter=ALL_STOCKS,
            limit=500,
        ),
        "sector_today": eastmoney_frame(
            fields={"f14": "名称", "f3": "今日涨跌幅", "f62": "今日主力净流入-净额"},
            sort_field="f62",
            stock_filter="m:90+t:2",
            limit=100,
        ),
        "sector_5": eastmoney_frame(
            fields={"f14": "名称", "f109": "5日涨跌幅", "f164": "5日主力净流入-净额"},
            sort_field="f164",
            stock_filter="m:90+t:2",
            limit=100,
        ),
    }


def validate_quotes(frame: pd.DataFrame, snapshot_type: str) -> None:
    required = {"代码", "名称", "最新价", "涨跌幅", "成交额"}
    missing = required.difference(frame.columns)
    if missing:
        raise RuntimeError(f"行情字段缺失: {', '.join(sorted(missing))}")
    if len(frame) < 4500:
        raise RuntimeError(f"行情覆盖不足: {len(frame)}")
    valid_price_ratio = pd.to_numeric(frame["最新价"], errors="coerce").gt(0).mean()
    valid_amount_ratio = pd.to_numeric(frame["成交额"], errors="coerce").ge(0).mean()
    if valid_price_ratio < 0.82 or valid_amount_ratio < 0.9:
        raise RuntimeError("行情有效值比例不足")
    if snapshot_type == "close":
        changed = pd.to_numeric(frame["涨跌幅"], errors="coerce").fillna(0).ne(0).mean()
        if changed < 0.35:
            raise RuntimeError("收盘行情疑似尚未刷新完整")


def build_sectors(today_frame: pd.DataFrame, five_frame: pd.DataFrame) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    today_map = {text_value(row, "名称"): row for row in today_frame.to_dict("records")}
    five_map = {text_value(row, "名称"): row for row in five_frame.to_dict("records")}
    sectors: list[dict[str, Any]] = []
    lookup: dict[str, dict[str, Any]] = {}
    for name, row in today_map.items():
        if not name:
            continue
        five = five_map.get(name)
        net_today = value(row, "今日主力净流入-净额", "主力净流入-净额")
        net_five = value(five, "5日主力净流入-净额", "主力净流入-净额")
        net_today_yi = (net_today or 0) / 100_000_000
        net_five_yi = (net_five or 0) / 100_000_000
        signal = (
            "连续流入" if net_today_yi > 0 and net_five_yi > net_today_yi
            else "当日回流" if net_today_yi > 0
            else "短线流出" if net_five_yi > 0
            else "持续流出"
        )
        item = {
            "name": name,
            "pctChange": rounded(value(row, "今日涨跌幅"), 2) or 0,
            "netTodayYi": rounded(net_today_yi, 2) or 0,
            "net5Yi": rounded(net_five_yi, 2) or 0,
            "companyCount": 0,
            "leadStock": text_value(row, "今日主力净流入最大股", "主力净流入最大股", fallback="—"),
            "signal": signal,
        }
        sectors.append(item)
        lookup[name] = item
    inflow = sorted(sectors, key=lambda item: item["netTodayYi"], reverse=True)[:6]
    outflow = sorted(sectors, key=lambda item: item["netTodayYi"])[:2]
    selected = inflow + [item for item in outflow if item["name"] not in {x["name"] for x in inflow}]
    return selected, lookup


def factor(key: str, score: float, available: bool = True) -> dict[str, Any]:
    definition = next(item for item in FACTORS if item[0] == key)
    return {"key": key, "label": definition[1], "weight": definition[2], "score": round(clamp(score)), "available": available}


def score_stocks(frames: dict[str, pd.DataFrame], sector_lookup: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    quote_records = frames["quotes"].to_dict("records")
    main = records_by_code(frames["main"])
    flow_today = records_by_code(frames["flow_today"])
    flow_five = records_by_code(frames["flow_5"])
    changes = sorted(item for item in (value(row, "涨跌幅") for row in quote_records) if item is not None)
    median_change = changes[len(changes) // 2] if changes else 0
    stocks: list[dict[str, Any]] = []

    for quote in quote_records:
        code = text_value(quote, "代码").zfill(6)
        name = text_value(quote, "名称")
        close = value(quote, "最新价")
        amount = value(quote, "成交额")
        if not re.fullmatch(r"\d{6}", code) or not name or not close or close <= 0 or not amount or amount < 50_000_000 or "退" in name:
            continue
        main_row = main.get(code)
        today_row = flow_today.get(code)
        five_row = flow_five.get(code)
        industry = text_value(main_row, "所属板块", fallback="未分类")
        sector = sector_lookup.get(industry)
        pct_change = value(quote, "涨跌幅") or 0
        amount_yi = amount / 100_000_000
        turnover = value(quote, "换手率")
        volume_ratio = value(quote, "量比")
        pe = value(quote, "市盈率-动态")
        pb = value(quote, "市净率")
        market_cap = value(quote, "总市值")
        market_cap_yi = market_cap / 100_000_000 if market_cap is not None else None
        return5 = value(main_row, "5日排行榜-5日涨跌")
        return60 = value(quote, "60日涨跌幅")
        today_pct = value(main_row, "今日排行榜-主力净占比")
        if today_pct is None:
            today_pct = value(today_row, "今日主力净流入-净占比")
        five_pct = value(main_row, "5日排行榜-主力净占比")
        if five_pct is None:
            five_pct = value(five_row, "5日主力净流入-净占比")
        net_today = value(today_row, "今日主力净流入-净额")
        net_five = value(five_row, "5日主力净流入-净额")

        trend_available = return5 is not None and return60 is not None
        trend_score = 50 + clamp(pct_change * 1.5, -12, 12) + clamp((return5 or 0) * 1.1, -18, 18) + clamp((return60 or 0) * 0.35, -20, 20)
        direction = 1 if pct_change >= 0 else -1
        volume_score = 50 + (clamp(((volume_ratio or 1) - 1) * 22 * direction, -18, 18) if volume_ratio is not None else 0) + clamp(pct_change * 2, -12, 12) + (7 if turnover is not None and 0.7 < turnover < 12 else 0)
        funds_available = today_pct is not None or net_today is not None
        funds_score = 50 + clamp((today_pct or 0) * 1.6, -28, 28) + clamp((five_pct or 0) * 0.9, -18, 18)
        industry_available = sector is not None
        industry_score = 50 if sector is None else 50 + clamp(sector["pctChange"] * 5, -18, 18) + clamp(sector["netTodayYi"] / 3, -18, 18) + clamp(sector["net5Yi"] / 12, -12, 12)
        valuation_available = pe is not None or pb is not None
        valuation_score = 42
        if pe is not None and pe > 0:
            valuation_score = 76 - clamp(abs(pe - 24) * 1.15, 0, 42)
        if pb is not None:
            valuation_score += 6 if 0 < pb < 5 else -10 if pb > 10 else 0
        momentum_score = 50 + clamp((pct_change - median_change) * 5, -22, 22) + clamp((return5 or 0) * 1.1, -18, 18)
        liquidity_available = turnover is not None and market_cap_yi is not None
        liquidity_score = 50 + clamp(math.log10(max(amount_yi, 0.1)) * 12, -8, 22)
        if turnover is not None:
            liquidity_score += 8 if 0.7 < turnover < 12 else -8 if turnover > 25 else 0
        if market_cap_yi is not None and market_cap_yi < 40:
            liquidity_score -= 12
        risk_score = 82
        if "ST" in name.upper():
            risk_score -= 42
        if abs(pct_change) >= 9.5:
            risk_score -= 14
        if turnover is not None and turnover > 25:
            risk_score -= 14
        if pe is not None and pe <= 0:
            risk_score -= 10
        if market_cap_yi is not None and market_cap_yi < 40:
            risk_score -= 10
        if pb is not None and pb > 12:
            risk_score -= 10

        factors = [
            factor("trend", trend_score, trend_available),
            factor("volume", volume_score, volume_ratio is not None and turnover is not None),
            factor("funds", funds_score, funds_available),
            factor("industry", industry_score, industry_available),
            factor("valuation", valuation_score, valuation_available),
            factor("momentum", momentum_score, return5 is not None),
            factor("liquidity", liquidity_score, liquidity_available),
            factor("risk", risk_score),
        ]
        total_score = round(sum(item["score"] * item["weight"] / 100 for item in factors))
        risk_gate = factors[0]["score"] < 55 or factors[7]["score"] < 50
        verdict = "风险降级" if risk_gate else "优先研究" if total_score >= 80 else "持续跟踪" if total_score >= 70 else "等待确认"
        trend_state = (
            "中期上行" if (return60 or 0) > 8 and (return5 or 0) > 0
            else "上行回踩" if (return60 or 0) > 0 and (return5 or 0) < 0
            else "弱势区间" if (return60 or 0) < -8
            else "震荡确认"
        ) if trend_available else ("当日偏强" if pct_change >= 0 else "当日偏弱")
        stocks.append({
            "code": code,
            "tsCode": code,
            "name": name,
            "industry": industry,
            "close": rounded(close, 2),
            "pctChange": rounded(pct_change, 2),
            "amountYi": rounded(amount_yi, 2),
            "turnoverRate": rounded(turnover, 2),
            "volumeRatio": rounded(volume_ratio, 2),
            "peTtm": rounded(pe, 2),
            "pb": rounded(pb, 2),
            "marketCapYi": rounded(market_cap_yi, 1),
            "netFlowWan": rounded(net_today / 10_000 if net_today is not None else None, 1),
            "netFlow5Wan": rounded(net_five / 10_000 if net_five is not None else None, 1),
            "return5": rounded(return5, 2),
            "return60": rounded(return60, 2),
            "totalScore": total_score,
            "verdict": verdict,
            "trendState": trend_state,
            "riskGate": risk_gate,
            "factors": factors,
        })
    return sorted(stocks, key=lambda item: item["totalScore"], reverse=True)[:40]


def build_snapshot(snapshot_type: str, now: datetime, frames: dict[str, pd.DataFrame]) -> dict[str, Any]:
    validate_quotes(frames["quotes"], snapshot_type)
    warnings: list[str] = []
    if len(frames["main"]) < 300:
        warnings.append("资金活跃池或行业字段覆盖不足")
    else:
        warnings.append("个股资金与行业字段覆盖资金活跃度前 500 只")
    if len(frames["flow_today"]) < 300 or len(frames["flow_5"]) < 300:
        warnings.append("个股资金净额覆盖不足")
    if len(frames["sector_today"]) < 60 or len(frames["sector_5"]) < 60:
        warnings.append("行业资金覆盖不完整")
    sectors, sector_lookup = build_sectors(frames["sector_today"], frames["sector_5"])
    stocks = score_stocks(frames, sector_lookup)
    if len(stocks) < 20:
        raise RuntimeError("评分池有效股票不足")

    changes = pd.to_numeric(frames["quotes"]["涨跌幅"], errors="coerce").dropna()
    amounts = pd.to_numeric(frames["quotes"]["成交额"], errors="coerce").dropna()
    rising = int((changes > 0).sum())
    falling = int((changes < 0).sum())
    flat = int((changes == 0).sum())
    breadth = (rising - falling) / max(len(changes), 1)
    environment_score = round(clamp(55 + breadth * 35, 20, 85))
    regime = "风险偏好偏强" if environment_score >= 68 else "结构性轮动" if environment_score >= 52 else "防守观察" if environment_score >= 38 else "风险收缩"
    summary = (
        "上涨覆盖较好，优先研究趋势、资金和行业同时确认的标的。" if environment_score >= 68
        else "市场仍以结构性轮动为主，强行业可以跟踪，但不宜用单日涨幅替代趋势确认。" if environment_score >= 52
        else "风险偏好偏弱，系统更重视流动性、趋势完整性和负面信息核验。"
    )
    labels = ("≤−7%", "−7~−3%", "−3~0%", "0~3%", "3~7%", "≥7%")
    buckets = [0] * 6
    for change in changes:
        index = 0 if change <= -7 else 1 if change <= -3 else 2 if change < 0 else 3 if change < 3 else 4 if change < 7 else 5
        buckets[index] += 1
    coverage = {
        "quotes": True,
        "valuation": "市盈率-动态" in frames["quotes"].columns,
        "stockFlow": len(frames["flow_today"]) >= 300,
        "sectorFlow": len(frames["sector_today"]) >= 60,
        "industry": len(frames["main"]) >= 300,
    }
    status = "partial" if warnings else "live"
    return {
        "status": status,
        "source": "AKShare",
        "snapshotType": snapshot_type,
        "tradeDate": now.strftime("%Y%m%d"),
        "updatedAt": now.isoformat(),
        "message": "正式免费数据快照已载入。" if status == "live" else "正式行情已载入，部分免费指标暂缺。",
        "warnings": warnings,
        "coverage": coverage,
        "market": {
            "rising": rising,
            "falling": falling,
            "flat": flat,
            "amountYi": rounded(float(amounts.sum()) / 100_000_000, 1),
            "amountChangePct": None,
            "environmentScore": environment_score,
            "regime": regime,
            "summary": summary,
            "distribution": [{"label": label, "count": count} for label, count in zip(labels, buckets)],
        },
        "sectors": sectors,
        "stocks": stocks,
    }


def atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="market-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot-type", choices=("midday", "close"), required=True)
    parser.add_argument("--output", default="data/market.json")
    parser.add_argument("--force", action="store_true", help="Allow manual runs outside normal market times")
    args = parser.parse_args()
    now = datetime.now(SHANGHAI)
    if not args.force and not is_trading_day(now):
        print(f"{now:%Y-%m-%d} 不是交易日，不覆盖现有快照")
        return 0
    if not args.force:
        minutes = now.hour * 60 + now.minute
        if args.snapshot_type == "midday" and minutes < 11 * 60 + 35:
            raise RuntimeError("午盘快照运行过早")
        if args.snapshot_type == "close" and minutes < 15 * 60 + 32:
            raise RuntimeError("收盘快照运行过早")
    frames = load_frames()
    snapshot = build_snapshot(args.snapshot_type, now, frames)
    atomic_write(Path(args.output), snapshot)
    print(f"已生成 {args.snapshot_type} 快照：{snapshot['tradeDate']}，评分池 {len(snapshot['stocks'])} 只")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
