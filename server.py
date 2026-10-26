"""Connecteur MCP « Bitcoin Live » (https://nioctib.live).

Le dashboard s'appuie sur une API JSON publique (/api/...), sans authentification.
Ce serveur l'interroge directement (pas de navigateur). Cette API n'est pas
documentée officiellement : elle peut changer sans préavis.

Valeurs observées :
  - actifs : BTC, ETH, SOL
  - market : "combined" (spot + perp), "spot", "perp"
  - window / interval : en secondes
"""

import asyncio
import json
import os
import time
from datetime import datetime, timezone
from typing import Any, Literal

import httpx
from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import PlainTextResponse

BASE_URL = os.getenv("SITE_URL", "https://nioctib.live").rstrip("/")
MAX_CHARS = int(os.getenv("MAX_CHARS", "20000"))
CACHE_TTL = float(os.getenv("CACHE_TTL", "5"))  # secondes ; évite de marteler le site

Asset = Literal["BTC", "ETH", "SOL"]
Market = Literal["combined", "spot", "perp"]

mcp = FastMCP(
    "bitcoin-live",
    instructions=(
        "Données de marché du dashboard Bitcoin Live (nioctib.live) : order flow "
        "agrégé de plusieurs exchanges pour BTC, ETH et SOL (taker buy/sell, delta, "
        "pression acheteuse en %, CVD), spot vs futures perpétuels, flux des ETF "
        "Bitcoin spot US, funding, liquidations, ratios long/short. "
        "Pour savoir qui contrôle le marché : flow_snapshot (buyPercent > 50 = "
        "acheteurs dominants). Les montants sont en USD sauf mention '_usd_m' "
        "(millions d'USD)."
    ),
    host=os.getenv("HOST", "0.0.0.0"),
    port=int(os.getenv("PORT", "8000")),
    # Mets un chemin difficile à deviner (ex. /mcp-x8f3k2...) : il n'y a pas d'auth.
    streamable_http_path=os.getenv("MCP_PATH", "/mcp"),
    stateless_http=True,
)

_client: httpx.AsyncClient | None = None
_cache: dict[str, tuple[float, Any]] = {}
_slots = asyncio.Semaphore(4)


@mcp.custom_route("/health", methods=["GET"])
async def health(_: Request) -> PlainTextResponse:
    return PlainTextResponse("ok")


# --------------------------------------------------------------------------- utils


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            base_url=BASE_URL,
            timeout=30,
            follow_redirects=True,
            headers={"User-Agent": "bitcoin-live-mcp/1.0", "Accept": "application/json"},
        )
    return _client


async def _get(path: str, **params: Any) -> Any:
    params = {k: v for k, v in params.items() if v is not None}
    key = path + "?" + "&".join(f"{k}={params[k]}" for k in sorted(params))
    now = time.monotonic()
    hit = _cache.get(key)
    if hit and now - hit[0] < CACHE_TTL:
        return hit[1]

    async with _slots:
        resp = await _http().get(path, params=params)
    try:
        data = resp.json()
    except ValueError:
        data = None
    if resp.status_code >= 400:
        msg = data.get("error") if isinstance(data, dict) else resp.text[:200]
        raise ValueError(f"{BASE_URL}{path} -> HTTP {resp.status_code}: {msg}")
    if data is None:
        raise ValueError(f"{BASE_URL}{path} : réponse non JSON")

    if len(_cache) > 200:
        _cache.clear()
    _cache[key] = (now, data)
    return data


def _round(o: Any, nd: int = 4) -> Any:
    if isinstance(o, float):
        return round(o, nd)
    if isinstance(o, dict):
        return {k: _round(v, nd) for k, v in o.items()}
    if isinstance(o, list):
        return [_round(v, nd) for v in o]
    return o


def _iso(ms: Any) -> str | None:
    if not isinstance(ms, (int, float)):
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _drop(d: dict, *keys: str) -> dict:
    return {k: v for k, v in d.items() if k not in keys}


def _lim(n: int, low: int = 1, high: int = 300) -> int:
    return max(low, min(high, int(n)))


def _points(points: list[dict], limit: int) -> list[dict]:
    """Derniers points, avec l'horodatage `time` (ms) converti en ISO UTC (`at`)."""
    out = []
    for p in points[-_lim(limit):]:
        q = dict(p)
        ts = q.pop("time", None)
        out.append({"at": _iso(ts), **q} if ts is not None else q)
    return _round(out)


def _slim(d: dict, max_len: int = 1500) -> dict:
    """Remplace les valeurs volumineuses par un marqueur."""
    out = {}
    for k, v in d.items():
        size = len(json.dumps(v, default=str)) if isinstance(v, (dict, list)) else 0
        out[k] = f"<omis: {size} octets>" if size > max_len else v
    return out


# --------------------------------------------------------------------------- tools


@mcp.tool()
async def flow_snapshot(window_seconds: int = 3600, market: Market = "combined") -> dict:
    """Qui contrôle le marché maintenant ? Pour BTC, ETH et SOL : volume taker achat/vente,
    delta net, pression acheteuse (buyPercent > 50 = acheteurs dominants), signal, prix.

    window_seconds: fenêtre de lookback en secondes (60 = 1 min, 3600 = 1 h, 86400 = 24 h,
        jusqu'à ~1 mois).
    market: "combined" (spot + perp), "spot" ou "perp".
    """
    d = await _get(
        "/api/flow/snapshot", window=max(60, min(2_592_000, int(window_seconds))), market=market
    )
    return _round(
        {
            "market": d.get("market"),
            "window_seconds": d.get("windowSeconds"),
            "threshold_percent": d.get("thresholdPercent"),
            "generated_at": _iso(d.get("generatedAt")),
            "assets": [_drop(a, "historyCoverage") for a in d.get("assets", [])],
        }
    )


@mcp.tool()
async def flow_overview(market: Market = "combined") -> dict:
    """Vue d'ensemble sur 24 h : totaux taker buy/sell, delta, nombre de trades, et par actif
    (BTC/ETH/SOL) le prix d'ouverture, le dernier prix et la variation en %."""
    d = await _get("/api/flow/overview", market=market)
    return _round(
        {
            "market": d.get("market"),
            "window_hours": d.get("windowHours"),
            "generated_at": _iso(d.get("generatedAt")),
            "totals": d.get("totals"),
            "assets": [_drop(a, "historyCoverage") for a in d.get("assets", [])],
        }
    )


@mcp.tool()
async def flow_by_exchange(
    asset: Asset = "BTC", window_seconds: int = 3600, market: Market = "combined", top: int = 15
) -> dict:
    """Order flow par exchange (Binance, Coinbase, Bybit, ...) trié par volume décroissant :
    taker buy/sell, delta, buyPercent, nombre de trades."""
    d = await _get(
        "/api/flow/exchanges",
        asset=asset,
        window=max(60, min(2_592_000, int(window_seconds))),
        market=market,
    )
    ex = [e for e in d.get("exchanges", []) if not e.get("unavailable")]
    ex.sort(key=lambda e: e.get("totalUsd") or 0, reverse=True)
    return _round(
        {
            "asset": d.get("asset"),
            "market": d.get("market"),
            "window_seconds": d.get("windowSeconds"),
            "exchange_count": len(ex),
            "exchanges": ex[: _lim(top, 1, 60)],
        }
    )


@mcp.tool()
async def flow_pressure(
    asset: Asset = "BTC", interval_seconds: int = 60, market: Market = "combined", limit: int = 60
) -> dict:
    """Série de la pression acheteuse/vendeuse (buyUsd, sellUsd, total, buyPercent) par
    intervalle, du plus ancien au plus récent. interval_seconds : 60, 300, 900, 3600..."""
    d = await _get(
        "/api/flow/pressure", asset=asset, interval=max(60, int(interval_seconds)), market=market
    )
    return {
        "asset": d.get("asset"),
        "market": d.get("market"),
        "interval_seconds": d.get("intervalSeconds"),
        "points": _points(d.get("points", []), limit),
    }


@mcp.tool()
async def flow_candles(
    asset: Asset = "BTC", interval_seconds: int = 900, market: Market = "combined", limit: int = 48
) -> dict:
    """Bougies de prix avec CVD (cumulative volume delta) : cvdOpen/High/Low/Close, buyUsd,
    sellUsd, delta, tradeCount, priceOpen/High/Low/Close. interval_seconds : 900, 3600..."""
    d = await _get(
        "/api/flow/candles", asset=asset, interval=max(60, int(interval_seconds)), market=market
    )
    return {
        "asset": d.get("asset"),
        "market": d.get("market"),
        "interval_seconds": d.get("intervalSeconds"),
        "candles": _points(d.get("candles", []), limit),
    }


@mcp.tool()
async def etf_summary() -> dict:
    """Flux des ETF Bitcoin spot US (IBIT, FBTC, ARKB, BITB, ...) : dernier jour, flux net total,
    cumul, et par ETF le dernier flux et les 5 derniers jours. Montants en millions d'USD."""
    d = await _get("/api/etf/summary")
    tickers = [
        {**_drop(t, "recentFlows"), "last_5_flows_usd_m": (t.get("recentFlows") or [])[-5:]}
        for t in d.get("tickers", [])
    ]
    return _round(
        {
            "latest_date": d.get("latestDate"),
            "latest_day_total_usd_m": d.get("latestDayTotalUsdM"),
            "total_cumulative_usd_m": d.get("totalCumulativeUsdM"),
            "data_source": d.get("dataSource"),
            "snapshot_sessions_behind": d.get("snapshotSessionsBehind"),
            "tickers": tickers,
        }
    )


@mcp.tool()
async def etf_series(ticker: str = "TOTAL", days: int = 30) -> dict:
    """Série quotidienne de flux d'ETF : flowUsdM (flux du jour) et cumulativeUsdM, en millions
    d'USD. ticker : "TOTAL" (tous les ETF) ou un ticker comme "IBIT", "FBTC"."""
    d = await _get("/api/etf/series", ticker=ticker.strip().upper())
    return _round(
        {
            "ticker": d.get("ticker"),
            "name": d.get("name"),
            "points": (d.get("points") or [])[-_lim(days, 1, 400):],
        }
    )


@mcp.tool()
async def derivatives_funding(asset: Asset = "BTC", limit: int = 24) -> dict:
    """Funding rate agrégé (en %) : taux courant, taux prédit et historique horaire récent."""
    d = await _get("/api/derivatives/funding", asset=asset)
    return {
        **_round(_drop(d, "points")),
        "points": _points(d.get("points", []), limit),
    }


@mcp.tool()
async def derivatives_liquidations(asset: Asset = "BTC", limit: int = 24) -> dict:
    """Liquidations agrégées en USD (longUsd / shortUsd) : totaux et historique horaire récent."""
    d = await _get("/api/derivatives/liquidations", asset=asset)
    return {
        **_round(_drop(d, "points")),
        "points": _points(d.get("points", []), limit),
    }


@mcp.tool()
async def long_short() -> dict:
    """Ratios long/short BTC par exchange (comptes globaux, top comptes, top positions,
    sentiment smart money) et ratio taker long/short sur 4 h."""
    snap, taker = await asyncio.gather(
        _get("/api/long-short/snapshot"),
        _get("/api/long-short/taker"),
        return_exceptions=True,
    )
    if isinstance(snap, Exception):
        raise snap
    return _round(
        {
            "snapshot": snap,
            "taker_4h": {"error": str(taker)} if isinstance(taker, Exception) else taker,
        }
    )


@mcp.tool()
async def dealer_delta() -> dict:
    """Modèle « dealer delta » BTC : dernier intervalle (flux signé, open interest, variation d'OI,
    classe d'intervalle comme shorts_closing) et variation sur la fenêtre. Version allégée."""
    d = await _get("/api/dealer-delta/snapshot")
    out = _slim(d)
    if isinstance(d.get("latest"), dict):
        out["latest"] = _slim(d["latest"], 400)
    return _round(out)


@mcp.tool()
async def feeds_status() -> dict:
    """État des connexions du site aux exchanges : nombre de flux connectés et liste de ceux
    qui sont déconnectés (utile pour juger de la fiabilité des données)."""
    d = await _get("/api/feeds/status")
    feeds = d.get("feeds", [])
    return {
        "generated_at": _iso(d.get("generatedAt")),
        "total": len(feeds),
        "connected": sum(1 for f in feeds if f.get("connected")),
        "disconnected": [
            {
                "exchange": f.get("exchange"),
                "market": f.get("market"),
                "last_failure_reason": f.get("lastFailureReason"),
            }
            for f in feeds
            if not f.get("connected")
        ],
    }


_API_ALLOWED = (
    "/api/flow/",
    "/api/etf/",
    "/api/derivatives/",
    "/api/long-short/",
    "/api/dealer-delta/",
    "/api/oi/",
    "/api/feeds/",
)


@mcp.tool()
async def api_get(path: str, query: dict[str, str | int | float | bool] | None = None) -> dict:
    """Appel GET direct sur une route de données publique du site (pour explorer ce que les
    autres outils n'exposent pas). Préfixes autorisés : /api/flow/, /api/etf/,
    /api/derivatives/, /api/long-short/, /api/dealer-delta/, /api/oi/, /api/feeds/.
    Exemple : path="/api/etf/holdings". La réponse est tronquée si elle est trop longue."""
    if ".." in path or "://" in path or not path.startswith(_API_ALLOWED):
        raise ValueError(f"Chemin non autorisé. Préfixes permis : {', '.join(_API_ALLOWED)}")
    data = await _get(path, **(query or {}))
    text = json.dumps(_round(data), separators=(",", ":"), default=str)
    if len(text) <= MAX_CHARS:
        return {"path": path, "data": json.loads(text)}
    return {"path": path, "truncated": True, "total_chars": len(text), "preview": text[:MAX_CHARS]}


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
