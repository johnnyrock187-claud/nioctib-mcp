# Connecteur Claude — Bitcoin Live (nioctib.live)

Serveur MCP distant qui interroge l'API JSON publique de https://nioctib.live/
(celle que le dashboard appelle lui-même) : order flow BTC/ETH/SOL, CVD, flux ETF,
funding, liquidations, ratios long/short. Pas de navigateur, pas de clé API.

**Statut** : les 13 outils ont été testés de bout en bout contre le vrai site
(serveur lancé dans un sandbox distant, appels via un client MCP).
L'API du site n'est pas documentée officiellement : elle peut changer sans préavis.

## Outils

| Outil | Rôle |
|---|---|
| `flow_snapshot(window_seconds, market)` | Qui contrôle le marché : buy %, delta, signal, prix pour BTC/ETH/SOL |
| `flow_overview(market)` | Totaux et variation sur 24 h |
| `flow_by_exchange(asset, window_seconds, market, top)` | Order flow par exchange |
| `flow_pressure(asset, interval_seconds, market, limit)` | Série de pression acheteuse/vendeuse |
| `flow_candles(asset, interval_seconds, market, limit)` | Bougies prix + CVD |
| `etf_summary()` / `etf_series(ticker, days)` | Flux des ETF Bitcoin spot US (M USD) |
| `derivatives_funding(asset, limit)` | Funding rate courant, prédit, historique |
| `derivatives_liquidations(asset, limit)` | Liquidations long/short |
| `long_short()` | Ratios long/short par exchange + taker 4 h |
| `dealer_delta()` | Modèle dealer delta BTC (version allégée) |
| `feeds_status()` | Santé des connexions du site aux exchanges |
| `api_get(path, query)` | GET sur une route publique (liste blanche) pour explorer |

`market` = `combined` (défaut), `spot` ou `perp`. `asset` = `BTC`, `ETH` ou `SOL`.
Fenêtres et intervalles en secondes (60 = 1 min, 3600 = 1 h, 86400 = 24 h).

## Lancer

Local :

    pip install -r requirements.txt
    python server.py          # http://localhost:8000/mcp

Hébergement : le `Dockerfile` fonctionne tel quel sur Render, Fly.io, Railway,
Cloud Run ou un VPS. Variables d'environnement :

- `PORT` (défaut 8000)
- `MCP_PATH` (défaut `/mcp`) — **mets un chemin secret**, ex. `/mcp-k3x9f2a7`, car il n'y a pas d'authentification
- `CACHE_TTL` (défaut 5 s) — cache court pour ne pas marteler le site
- `SITE_URL`, `MAX_CHARS` (optionnels)

Santé : `GET /health`.

## Ajouter dans Claude

Réglages → Connecteurs → Ajouter un connecteur personnalisé, puis colle
`https://ton-domaine/mcp-k3x9f2a7`. (Vérifie que ton offre autorise les
connecteurs personnalisés.)

## Remarques

- Au moment des tests, `/api/oi/snapshot` renvoyait une erreur 500 côté site ; il reste accessible via `api_get`.
- Vérifie les conditions d'utilisation du site avant un usage intensif.
