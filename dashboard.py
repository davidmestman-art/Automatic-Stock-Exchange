#!/usr/bin/env python3
"""Web dashboard for the NYSE Algorithmic Trading Engine.

Run:  python dashboard.py
Then open http://localhost:8080
"""

import logging
import threading
import time
import traceback
from datetime import datetime, timedelta
from typing import Optional

from flask import Flask, jsonify, render_template_string

from config import config
from src.trading.engine import TradingEngine

CYCLE_INTERVAL = 60  # seconds between automatic trading cycles

app = Flask(__name__)
_lock = threading.Lock()
_engine = TradingEngine(config)
_last_state: dict = {}
_last_cycle_at: Optional[datetime] = None
_next_cycle_at: Optional[datetime] = None

log = logging.getLogger(__name__)


def _background_loop() -> None:
    """Daemon thread: run a full trading cycle every CYCLE_INTERVAL seconds."""
    global _last_cycle_at, _next_cycle_at
    # Short warm-up so the server is ready before the first cycle fires
    _next_cycle_at = datetime.now() + timedelta(seconds=10)
    time.sleep(10)
    while True:
        with _lock:
            try:
                _engine.run_cycle()
                _last_cycle_at = datetime.now()
            except Exception as e:
                log.error(f"Background cycle error: {e}")
        _next_cycle_at = datetime.now() + timedelta(seconds=CYCLE_INTERVAL)
        time.sleep(CYCLE_INTERVAL)

# ── State builder ─────────────────────────────────────────────────────────────

def _build_state(signals=None, prices=None, ind_map=None, error=None) -> dict:
    portfolio = _engine.portfolio

    price_lookup = prices or {}
    pos_list = []
    for sym, pos in portfolio.positions.items():
        cp = price_lookup.get(sym, pos.entry_price)
        pos_list.append({
            "symbol": sym,
            "shares": round(pos.shares, 4),
            "entry_price": round(pos.entry_price, 2),
            "current_price": round(cp, 2),
            "stop_loss": round(pos.stop_loss, 2),
            "take_profit": round(pos.take_profit, 2),
            "pnl": round(pos.unrealized_pnl(cp), 2),
            "pnl_pct": round(pos.unrealized_pnl_pct(cp) * 100, 2),
        })

    summary = portfolio.get_summary(price_lookup) if price_lookup else {
        "total_value": portfolio.cash,
        "cash": portfolio.cash,
        "position_value": 0,
        "total_pnl": 0,
        "total_pnl_pct": 0,
        "open_positions": len(portfolio.positions),
        "total_trades": len(portfolio.trades),
    }

    sig_list = []
    if signals:
        for sym, sig in signals.items():
            ind = ind_map.get(sym) if ind_map else None
            sig_list.append({
                "symbol": sym,
                "price": round(price_lookup.get(sym, 0), 2),
                "action": sig.action,
                "score": round(sig.score, 3),
                "confidence": round(sig.confidence, 3),
                "rsi": round(ind.rsi, 1) if ind and ind.rsi else None,
                "reasons": sig.reasons[:3],
            })
        sig_list.sort(key=lambda x: -abs(x["score"]))

    trades_list = [
        {
            "timestamp": t.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
            "action": t.action,
            "symbol": t.symbol,
            "shares": round(t.shares, 4),
            "price": round(t.price, 2),
            "pnl": round(t.pnl, 2) if t.pnl is not None else None,
            "pnl_pct": round(t.pnl_pct * 100, 2) if t.pnl_pct is not None else None,
            "reason": t.reason,
        }
        for t in reversed(portfolio.trades[-30:])
    ]

    mode = (
        "Alpaca Paper" if config.use_alpaca and config.paper_trading
        else "Alpaca LIVE" if config.use_alpaca
        else "Local Simulation"
    )

    market_open = None
    if config.use_alpaca:
        try:
            market_open = _engine.executor.is_market_open()
        except Exception:
            market_open = None

    return {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "mode": mode,
        "market_open": market_open,
        "portfolio": {
            "total_value": round(summary["total_value"], 2),
            "cash": round(summary["cash"], 2),
            "position_value": round(summary["position_value"], 2),
            "total_pnl": round(summary["total_pnl"], 2),
            "total_pnl_pct": round(summary["total_pnl_pct"], 2),
            "open_positions": summary["open_positions"],
            "total_trades": summary["total_trades"],
            "initial_capital": config.initial_capital,
        },
        "positions": pos_list,
        "signals": sig_list,
        "trades": trades_list,
        "error": error,
        "last_cycle_at": _last_cycle_at.isoformat() if _last_cycle_at else None,
        "next_cycle_at": _next_cycle_at.isoformat() if _next_cycle_at else None,
        "cycle_interval": CYCLE_INTERVAL,
        "watchlist": _engine.watchlist,
        "scan": _engine.scanner.last_result.to_dict() if _engine.scanner.last_result else None,
        "voo": _engine.voo_monitor.last_status.to_dict() if _engine.voo_monitor.last_status else None,
    }


# ── API endpoints ─────────────────────────────────────────────────────────────

@app.route("/api/state")
def api_state():
    global _last_state
    with _lock:
        try:
            signals, prices, ind_map = _engine.get_signals()
            _last_state = _build_state(signals, prices, ind_map)
        except Exception as e:
            _last_state = _build_state(error=str(e))
    return jsonify(_last_state)


@app.route("/api/cycle", methods=["POST"])
def api_cycle():
    global _last_state
    with _lock:
        try:
            _engine.run_cycle()
            _last_state = _build_state(error=None)
            return jsonify({"ok": True, "state": _last_state})
        except Exception as e:
            err = traceback.format_exc()
            return jsonify({"ok": False, "error": str(e), "detail": err}), 500


@app.route("/api/voo", methods=["POST"])
def api_voo():
    try:
        status = _engine.voo_monitor.check(force=True)
        return jsonify({"ok": True, "voo": status.to_dict() if status else None})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/rescan", methods=["POST"])
def api_rescan():
    global _last_state
    with _lock:
        try:
            _engine.refresh_watchlist()
            signals, prices, ind_map = _engine.get_signals()
            _last_state = _build_state(signals, prices, ind_map)
            return jsonify({"ok": True, "state": _last_state})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500


# ── Dashboard HTML ────────────────────────────────────────────────────────────

HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>NYSE Trading Engine</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0f172a;color:#e2e8f0;font-family:'Segoe UI',system-ui,sans-serif;font-size:14px;min-height:100vh}
a{color:inherit;text-decoration:none}

/* layout */
header{background:#1e293b;border-bottom:1px solid #334155;padding:14px 24px;display:flex;align-items:center;gap:16px;position:sticky;top:0;z-index:10}
.logo{font-size:18px;font-weight:700;color:#f1f5f9;letter-spacing:.5px}
.badge{padding:3px 10px;border-radius:99px;font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.5px}
.badge-paper{background:#1d4ed8;color:#bfdbfe}
.badge-live{background:#7f1d1d;color:#fecaca}
.badge-sim{background:#374151;color:#9ca3af}
.market-dot{width:8px;height:8px;border-radius:50%;display:inline-block;margin-right:4px}
.market-open{background:#22c55e;box-shadow:0 0 6px #22c55e}
.market-closed{background:#ef4444}
.market-unknown{background:#6b7280}
.hdr-right{margin-left:auto;display:flex;align-items:center;gap:10px}
.ts{font-size:11px;color:#64748b}
button{cursor:pointer;padding:6px 16px;border-radius:6px;border:none;font-size:13px;font-weight:600;transition:opacity .15s}
.btn-refresh{background:#334155;color:#e2e8f0}
.btn-refresh:hover{opacity:.8}
.btn-rescan{background:#7c3aed;color:#fff}
.btn-rescan:hover{opacity:.85}
.btn-rescan:disabled{opacity:.5;cursor:not-allowed}
.btn-cycle{background:#0ea5e9;color:#fff}
.btn-cycle:hover{opacity:.85}
.btn-cycle:disabled{opacity:.5;cursor:not-allowed}

main{padding:20px 24px;max-width:1400px;margin:0 auto}

/* stat cards */
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:12px;margin-bottom:20px}
.card{background:#1e293b;border-radius:10px;padding:16px;border:1px solid #334155}
.card-label{font-size:11px;color:#64748b;text-transform:uppercase;letter-spacing:.5px;margin-bottom:6px}
.card-value{font-size:22px;font-weight:700;font-variant-numeric:tabular-nums}
.card-sub{font-size:11px;color:#64748b;margin-top:3px}
.pos{color:#22c55e}.neg{color:#ef4444}.neu{color:#e2e8f0}

/* grid */
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:16px}
.grid1{margin-bottom:16px}
@media(max-width:900px){.grid2{grid-template-columns:1fr}}

/* panels */
.panel{background:#1e293b;border-radius:10px;border:1px solid #334155;overflow:hidden}
.panel-title{padding:12px 16px;font-weight:600;font-size:13px;color:#94a3b8;border-bottom:1px solid #334155;text-transform:uppercase;letter-spacing:.5px;display:flex;align-items:center;gap:6px}
.panel-title .count{background:#334155;color:#94a3b8;border-radius:99px;padding:1px 8px;font-size:11px}

/* tables */
table{width:100%;border-collapse:collapse}
th{padding:8px 12px;text-align:left;font-size:11px;color:#64748b;text-transform:uppercase;letter-spacing:.5px;border-bottom:1px solid #334155}
td{padding:9px 12px;border-bottom:1px solid #1e293b;font-variant-numeric:tabular-nums}
tr:last-child td{border-bottom:none}
tr:hover td{background:#263044}

/* signal pill */
.pill{display:inline-block;padding:2px 8px;border-radius:4px;font-weight:700;font-size:11px}
.pill-BUY{background:#14532d;color:#4ade80}
.pill-SELL{background:#7f1d1d;color:#f87171}
.pill-HOLD{background:#374151;color:#9ca3af}

/* score bar */
.score-wrap{display:flex;align-items:center;gap:6px}
.score-bar-bg{width:60px;height:5px;background:#334155;border-radius:3px;overflow:hidden}
.score-bar{height:5px;border-radius:3px;transition:width .3s}

/* VOO monitor */
.voo-panel{background:#1e293b;border-radius:10px;border:1px solid #334155;overflow:hidden;margin-bottom:16px}
.voo-header{padding:12px 16px;border-bottom:1px solid #334155;display:flex;align-items:center;gap:10px}
.voo-title{font-weight:600;font-size:13px;color:#94a3b8;text-transform:uppercase;letter-spacing:.5px}
.voo-checked{font-size:11px;color:#475569;margin-left:auto}
.voo-stats{display:grid;grid-template-columns:repeat(3,1fr);gap:0}
.voo-stat{padding:18px 20px;border-right:1px solid #334155}
.voo-stat:last-child{border-right:none}
.voo-stat-label{font-size:11px;color:#64748b;text-transform:uppercase;letter-spacing:.5px;margin-bottom:6px}
.voo-stat-value{font-size:26px;font-weight:700;font-variant-numeric:tabular-nums}
.voo-alert-bar{padding:14px 20px;display:flex;align-items:center;gap:10px;font-size:13px;font-weight:600}
.voo-above{background:#0f2318;color:#4ade80;border-top:1px solid #166534}
.voo-below{background:#14532d;color:#dcfce7;border-top:1px solid #22c55e;animation:voo-pulse 2s ease-in-out infinite}
.voo-loading{padding:24px;text-align:center;color:#475569;font-size:13px}
@keyframes voo-pulse{0%,100%{opacity:1}50%{opacity:.8}}
.btn-voo{background:#1d4ed8;color:#fff;font-size:12px;padding:4px 12px;margin-left:auto}
.btn-voo:hover{opacity:.85}

/* empty state */
.empty{padding:32px;text-align:center;color:#475569}

/* error banner */
.error-banner{background:#7f1d1d;color:#fecaca;border-radius:8px;padding:10px 16px;margin-bottom:16px;font-size:13px;display:none}

/* spinner */
.spinner{display:inline-block;width:14px;height:14px;border:2px solid #334155;border-top-color:#0ea5e9;border-radius:50%;animation:spin .7s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}

.loading-overlay{display:none;position:fixed;inset:0;background:rgba(15,23,42,.7);z-index:50;align-items:center;justify-content:center;flex-direction:column;gap:12px}
.loading-overlay.active{display:flex}
</style>
</head>
<body>

<div class="loading-overlay" id="overlay">
  <div class="spinner" style="width:36px;height:36px;border-width:4px"></div>
  <div style="color:#94a3b8;font-size:13px" id="overlay-msg">Running cycle…</div>
</div>

<header>
  <div class="logo">NYSE Trading Engine</div>
  <span class="badge" id="mode-badge">—</span>
  <span id="market-status" style="font-size:12px;color:#94a3b8">
    <span class="market-dot market-unknown" id="market-dot"></span>
    <span id="market-label">Market —</span>
  </span>
  <div class="hdr-right">
    <span class="ts" id="cycle-info" style="color:#475569">—</span>
    <span class="ts" id="last-ts">—</span>
    <button class="btn-refresh" onclick="refresh()">Refresh</button>
    <button class="btn-rescan" id="btn-rescan" onclick="rescan()">Re-scan</button>
    <button class="btn-cycle" id="btn-cycle" onclick="runCycle()">Run Cycle</button>
  </div>
</header>

<main>
  <div class="error-banner" id="err-banner"></div>

  <!-- stat cards -->
  <div class="cards">
    <div class="card">
      <div class="card-label">Total Value</div>
      <div class="card-value neu" id="c-total">—</div>
      <div class="card-sub" id="c-initial">—</div>
    </div>
    <div class="card">
      <div class="card-label">Cash</div>
      <div class="card-value neu" id="c-cash">—</div>
    </div>
    <div class="card">
      <div class="card-label">In Positions</div>
      <div class="card-value neu" id="c-pos-val">—</div>
    </div>
    <div class="card">
      <div class="card-label">Total P&amp;L</div>
      <div class="card-value" id="c-pnl">—</div>
      <div class="card-sub" id="c-pnl-pct">—</div>
    </div>
    <div class="card">
      <div class="card-label">Open Positions</div>
      <div class="card-value neu" id="c-open">—</div>
    </div>
    <div class="card">
      <div class="card-label">Total Trades</div>
      <div class="card-value neu" id="c-trades">—</div>
    </div>
  </div>

  <!-- VOO 200-week MA monitor -->
  <div class="voo-panel" id="voo-panel">
    <div class="voo-header">
      <span class="voo-title">VOO — 200-Week Moving Average Monitor</span>
      <span class="voo-checked" id="voo-checked">—</span>
      <button class="btn-voo" onclick="refreshVOO()">Refresh VOO</button>
    </div>
    <div id="voo-body"><div class="voo-loading">Waiting for first cycle… click Refresh VOO to load now.</div></div>
  </div>

  <!-- watchlist scan -->
  <div class="panel grid1" id="scan-panel">
    <div class="panel-title">
      Watchlist
      <span class="count" id="wl-count">0</span>
      <span id="scan-meta" style="font-size:11px;color:#475569;margin-left:8px">—</span>
      <span id="fund-badge" style="display:none;margin-left:auto;font-size:11px;padding:2px 10px;border-radius:99px;background:#14532d;color:#4ade80;font-weight:600"></span>
    </div>
    <div id="fund-bar" style="display:none;padding:8px 16px;border-bottom:1px solid #334155;font-size:12px;color:#94a3b8;display:none;gap:16px;flex-wrap:wrap"></div>
    <div style="display:flex;flex-wrap:wrap;gap:8px;padding:12px 16px" id="wl-chips"></div>
  </div>

  <!-- signal analysis -->
  <div class="panel grid1">
    <div class="panel-title">Signal Analysis <span class="count" id="sig-count">0</span></div>
    <table>
      <thead><tr>
        <th>Ticker</th><th>Price</th><th>Signal</th><th>Score</th><th>RSI</th>
      </tr></thead>
      <tbody id="sig-body"><tr><td colspan="5" class="empty">No data yet — click Refresh</td></tr></tbody>
    </table>
  </div>

  <!-- positions + trades -->
  <div class="grid2">
    <div class="panel">
      <div class="panel-title">Positions <span class="count" id="pos-count">0</span></div>
      <table>
        <thead><tr>
          <th>Ticker</th><th>Entry Price</th><th>Current Price</th><th>Qty</th><th>Unrealized P&amp;L</th>
        </tr></thead>
        <tbody id="pos-body"><tr><td colspan="5" class="empty">No open positions</td></tr></tbody>
      </table>
    </div>
    <div class="panel">
      <div class="panel-title">Trades <span class="count" id="trade-count">0</span></div>
      <table>
        <thead><tr>
          <th>Time</th><th>Ticker</th><th>Side</th><th>Qty</th><th>Price</th><th>Realized P&amp;L</th>
        </tr></thead>
        <tbody id="trade-body"><tr><td colspan="6" class="empty">No trades yet</td></tr></tbody>
      </table>
    </div>
  </div>
</main>

<script>
const fmt = (n, dec=2) => n == null ? '—' : n.toLocaleString('en-US',{minimumFractionDigits:dec,maximumFractionDigits:dec});
const fmtD = n => n == null ? '—' : (n>=0?'+':'')+fmt(n);
const cls = n => n > 0 ? 'pos' : n < 0 ? 'neg' : 'neu';

function applyState(s) {
  const p = s.portfolio;

  // mode badge
  const badge = document.getElementById('mode-badge');
  badge.textContent = s.mode;
  badge.className = 'badge ' + (s.mode.includes('Paper') ? 'badge-paper' : s.mode.includes('LIVE') ? 'badge-live' : 'badge-sim');

  // market
  const dot = document.getElementById('market-dot');
  const lbl = document.getElementById('market-label');
  if (s.market_open === true) {
    dot.className = 'market-dot market-open'; lbl.textContent = 'Market OPEN';
  } else if (s.market_open === false) {
    dot.className = 'market-dot market-closed'; lbl.textContent = 'Market CLOSED';
  } else {
    dot.className = 'market-dot market-unknown'; lbl.textContent = 'Market —';
  }

  document.getElementById('last-ts').textContent = s.timestamp;
  _nextCycleAt = s.next_cycle_at ? new Date(s.next_cycle_at) : null;
  _lastCycleAt = s.last_cycle_at ? new Date(s.last_cycle_at) : null;
  updateCycleInfo();

  // cards
  document.getElementById('c-total').textContent = '$' + fmt(p.total_value);
  document.getElementById('c-initial').textContent = 'Initial $' + fmt(p.initial_capital);
  document.getElementById('c-cash').textContent = '$' + fmt(p.cash);
  document.getElementById('c-pos-val').textContent = '$' + fmt(p.position_value);

  const pnlEl = document.getElementById('c-pnl');
  pnlEl.textContent = fmtD(p.total_pnl) && ('$' + (p.total_pnl >= 0 ? '+' : '') + fmt(Math.abs(p.total_pnl)));
  pnlEl.className = 'card-value ' + cls(p.total_pnl);
  document.getElementById('c-pnl-pct').textContent = (p.total_pnl_pct >= 0 ? '+' : '') + fmt(p.total_pnl_pct) + '%';

  document.getElementById('c-open').textContent = p.open_positions;
  document.getElementById('c-trades').textContent = p.total_trades;

  // watchlist chips
  const wl = s.watchlist || [];
  document.getElementById('wl-count').textContent = wl.length;
  const scan = s.scan;
  if (scan) {
    document.getElementById('scan-meta').textContent =
      `scanned ${scan.scanned_at}  ·  volume top ${scan.volume_candidates_count}  ·  signal ranked to ${wl.length}`;

    // fundamental filter badge + bar
    const fundBadge = document.getElementById('fund-badge');
    const fundBar   = document.getElementById('fund-bar');
    if (scan.fund_enabled) {
      fundBadge.style.display = 'inline-block';
      fundBadge.textContent   = `Fundamentals ON  ✓${scan.fund_passed} ✗${scan.fund_failed}`;
      fundBar.style.display   = 'flex';
      fundBar.innerHTML =
        `<span style="color:#4ade80">✓ ${scan.fund_passed} passed</span>` +
        `<span style="color:#f87171">✗ ${scan.fund_failed} filtered out</span>` +
        `<span style="color:#475569">P/E &lt; 30 · D/E &lt; 2 · Positive FCF · Positive EPS growth</span>`;
    } else {
      fundBadge.style.display = 'none';
      fundBar.style.display   = 'none';
    }
  }
  const chips = document.getElementById('wl-chips');
  if (!wl.length) {
    chips.innerHTML = '<span style="color:#475569;font-size:12px">No watchlist yet — waiting for session scan</span>';
  } else {
    chips.innerHTML = wl.map(sym => {
      const action = scan && scan.actions ? scan.actions[sym] : null;
      const score  = scan && scan.scores  ? scan.scores[sym]  : null;
      const col = action === 'BUY' ? '#22c55e' : action === 'SELL' ? '#ef4444' : '#94a3b8';
      const scoreStr = score != null ? ` ${score >= 0 ? '+' : ''}${score}` : '';
      return `<span style="background:#1e293b;border:1px solid #334155;border-radius:6px;padding:5px 10px;font-size:12px;font-weight:600">
        <span style="color:${col}">${sym}</span><span style="color:#475569;font-size:11px">${scoreStr}</span>
      </span>`;
    }).join('');
  }

  // signals
  document.getElementById('sig-count').textContent = s.signals.length;
  const sb = document.getElementById('sig-body');
  if (!s.signals.length) {
    sb.innerHTML = '<tr><td colspan="5" class="empty">No signals — click Refresh</td></tr>';
  } else {
    sb.innerHTML = s.signals.map(r => {
      const barPct = Math.round(Math.abs(r.score) * 100);
      const barCol = r.action === 'BUY' ? '#22c55e' : r.action === 'SELL' ? '#ef4444' : '#6b7280';
      return `<tr>
        <td style="font-weight:600">${r.symbol}</td>
        <td>$${fmt(r.price)}</td>
        <td><span class="pill pill-${r.action}">${r.action}</span></td>
        <td>
          <div class="score-wrap">
            <span style="color:${barCol};font-weight:600">${r.score >= 0 ? '+' : ''}${fmt(r.score, 3)}</span>
            <div class="score-bar-bg"><div class="score-bar" style="width:${barPct}%;background:${barCol}"></div></div>
          </div>
        </td>
        <td>${r.rsi != null ? fmt(r.rsi, 1) : '—'}</td>
      </tr>`;
    }).join('');
  }

  // positions — columns: Ticker, Entry Price, Current Price, Qty, Unrealized P&L
  document.getElementById('pos-count').textContent = s.positions.length;
  const pb = document.getElementById('pos-body');
  if (!s.positions.length) {
    pb.innerHTML = '<tr><td colspan="5" class="empty">No open positions</td></tr>';
  } else {
    pb.innerHTML = s.positions.map(p => `<tr>
      <td style="font-weight:600">${p.symbol}</td>
      <td>$${fmt(p.entry_price)}</td>
      <td>$${fmt(p.current_price)}</td>
      <td>${p.shares}</td>
      <td class="${cls(p.pnl)}">${p.pnl >= 0 ? '+' : ''}$${fmt(Math.abs(p.pnl))} (${p.pnl_pct >= 0 ? '+' : ''}${fmt(p.pnl_pct)}%)</td>
    </tr>`).join('');
  }

  // trades — columns: Time, Ticker, Side, Qty, Price, Realized P&L
  document.getElementById('trade-count').textContent = s.trades.length;
  const tb = document.getElementById('trade-body');
  if (!s.trades.length) {
    tb.innerHTML = '<tr><td colspan="6" class="empty">No trades yet</td></tr>';
  } else {
    tb.innerHTML = s.trades.map(t => `<tr>
      <td style="color:#64748b;font-size:12px">${t.timestamp}</td>
      <td style="font-weight:600">${t.symbol}</td>
      <td><span class="pill pill-${t.action}">${t.action}</span></td>
      <td>${t.shares}</td>
      <td>$${fmt(t.price)}</td>
      <td class="${t.pnl != null ? cls(t.pnl) : 'neu'}">${t.pnl != null ? (t.pnl >= 0 ? '+' : '') + '$' + fmt(Math.abs(t.pnl)) + ' (' + (t.pnl_pct >= 0 ? '+' : '') + fmt(t.pnl_pct) + '%)' : '—'}</td>
    </tr>`).join('');
  }

  // VOO panel
  renderVOO(s.voo);

  // error
  const eb = document.getElementById('err-banner');
  if (s.error) { eb.textContent = '⚠ ' + s.error; eb.style.display = 'block'; }
  else { eb.style.display = 'none'; }
}

function renderVOO(voo) {
  const body = document.getElementById('voo-body');
  const checked = document.getElementById('voo-checked');
  if (!voo) {
    body.innerHTML = '<div class="voo-loading">Waiting for first cycle… click Refresh VOO to load now.</div>';
    checked.textContent = '—';
    return;
  }
  checked.textContent = 'Updated ' + voo.checked_at;

  const gapSign = voo.gap_pct >= 0 ? '+' : '';
  const gapCol  = voo.above_ma ? '#4ade80' : '#f87171';
  const alertBar = voo.above_ma
    ? `<div class="voo-alert-bar voo-above">
         <span style="font-size:16px">✓</span>
         VOO is <strong>ABOVE</strong> the 200-Week MA — broad market in long-term uptrend
       </div>`
    : `<div class="voo-alert-bar voo-below">
         <span style="font-size:18px">🟢</span>
         BUY ALERT — VOO is <strong>BELOW</strong> the 200-Week MA — rare long-term buying opportunity
       </div>`;

  body.innerHTML = `
    <div class="voo-stats">
      <div class="voo-stat">
        <div class="voo-stat-label">VOO Price</div>
        <div class="voo-stat-value neu">$${fmt(voo.price)}</div>
      </div>
      <div class="voo-stat">
        <div class="voo-stat-label">200-Week MA</div>
        <div class="voo-stat-value neu">$${fmt(voo.ma200w)}</div>
      </div>
      <div class="voo-stat">
        <div class="voo-stat-label">Gap vs MA</div>
        <div class="voo-stat-value" style="color:${gapCol}">${gapSign}${fmt(voo.gap_pct, 1)}%</div>
      </div>
    </div>
    ${alertBar}`;
}

async function refreshVOO() {
  const btn = document.querySelector('.btn-voo');
  btn.disabled = true;
  btn.textContent = 'Loading…';
  try {
    const res  = await fetch('/api/voo', {method: 'POST'});
    const data = await res.json();
    if (data.ok) renderVOO(data.voo);
    else {
      document.getElementById('err-banner').textContent = 'VOO fetch error: ' + data.error;
      document.getElementById('err-banner').style.display = 'block';
    }
  } catch(e) {
    document.getElementById('err-banner').textContent = 'VOO fetch failed: ' + e;
    document.getElementById('err-banner').style.display = 'block';
  } finally {
    btn.disabled = false;
    btn.textContent = 'Refresh VOO';
  }
}

async function refresh() {
  try {
    const res = await fetch('/api/state');
    const data = await res.json();
    applyState(data);
  } catch(e) {
    document.getElementById('err-banner').textContent = 'Failed to fetch state: ' + e;
    document.getElementById('err-banner').style.display = 'block';
  }
}

async function rescan() {
  const btn = document.getElementById('btn-rescan');
  const overlay = document.getElementById('overlay');
  document.getElementById('overlay-msg').textContent = 'Scanning S&P 500 universe…';
  btn.disabled = true;
  overlay.classList.add('active');
  try {
    const res = await fetch('/api/rescan', {method:'POST'});
    const data = await res.json();
    if (data.ok) applyState(data.state);
    else {
      document.getElementById('err-banner').textContent = 'Scan error: ' + data.error;
      document.getElementById('err-banner').style.display = 'block';
    }
  } catch(e) {
    document.getElementById('err-banner').textContent = 'Scan failed: ' + e;
    document.getElementById('err-banner').style.display = 'block';
  } finally {
    btn.disabled = false;
    overlay.classList.remove('active');
    document.getElementById('overlay-msg').textContent = 'Running cycle…';
  }
}

async function runCycle() {
  const btn = document.getElementById('btn-cycle');
  const overlay = document.getElementById('overlay');
  btn.disabled = true;
  overlay.classList.add('active');
  try {
    const res = await fetch('/api/cycle', {method:'POST'});
    const data = await res.json();
    if (data.ok) applyState(data.state);
    else {
      document.getElementById('err-banner').textContent = 'Cycle error: ' + data.error;
      document.getElementById('err-banner').style.display = 'block';
    }
  } catch(e) {
    document.getElementById('err-banner').textContent = 'Cycle failed: ' + e;
    document.getElementById('err-banner').style.display = 'block';
  } finally {
    btn.disabled = false;
    overlay.classList.remove('active');
  }
}

// Countdown ticker — updates every second without a server round-trip
let _nextCycleAt = null;
let _lastCycleAt = null;
function updateCycleInfo() {
  const el = document.getElementById('cycle-info');
  if (!_nextCycleAt) { el.textContent = ''; return; }
  const secsLeft = Math.max(0, Math.round((_nextCycleAt - Date.now()) / 1000));
  let parts = [];
  if (_lastCycleAt) {
    const ago = Math.round((Date.now() - _lastCycleAt) / 1000);
    parts.push('Last cycle ' + (ago < 60 ? ago + 's ago' : Math.round(ago/60) + 'm ago'));
  }
  parts.push('Next in ' + secsLeft + 's');
  el.textContent = parts.join('  ·  ');
}
setInterval(updateCycleInfo, 1000);

// Initial load + auto-refresh every 5s (picks up background cycle results quickly)
refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>"""


@app.route("/")
def index():
    return render_template_string(HTML)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    t = threading.Thread(target=_background_loop, daemon=True, name="cycle-scheduler")
    t.start()
    log.info(f"Cycle scheduler started — running every {CYCLE_INTERVAL}s")
    print(f"Dashboard running at http://localhost:8080  (auto-cycle every {CYCLE_INTERVAL}s)")
    app.run(host="0.0.0.0", port=8080, debug=False, use_reloader=False)
