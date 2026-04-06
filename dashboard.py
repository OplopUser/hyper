"""
dashboard.py — Live performance monitoring dashboard for the trading bot.

Run separately from the bot:
    python3 dashboard.py

Opens a web dashboard at http://localhost:5050 that shows:
  - Account equity, margin, withdrawable balance
  - All open positions with live unrealised PnL
  - Recent fills / trade history
  - Bot log stream (signals, orders, risk events)
  - Equity curve and PnL chart (built from fills history)

Auto-refreshes every 5 seconds via JavaScript polling.
"""

from __future__ import annotations

import os
import re
import json
from datetime import datetime, timezone
from collections import deque
from pathlib import Path
from functools import lru_cache

from flask import Flask, jsonify, render_template_string
from hyperliquid.info import Info
from dotenv import load_dotenv

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────
USE_TESTNET = os.getenv("USE_TESTNET", "true").lower() in ("true", "1", "yes")
BASE_URL    = ("https://api.hyperliquid-testnet.xyz" if USE_TESTNET
               else "https://api.hyperliquid.xyz")
ADDRESS     = os.getenv("ACCOUNT_ADDRESS", "")
LOG_FILE    = Path(__file__).parent / "logs" / "bot.log"
DASHBOARD_PORT = int(os.getenv("DASHBOARD_PORT", 5050))

app = Flask(__name__)
info = Info(base_url=BASE_URL, skip_ws=True)

# ── Data helpers ──────────────────────────────────────────────────────────────

def get_account() -> dict:
    """Fetch account equity, margin, positions."""
    try:
        state = info.user_state(ADDRESS)
        summary = state.get("marginSummary", {})
        positions = []
        for ap in state.get("assetPositions", []):
            pos = ap.get("position", {})
            if float(pos.get("szi", 0)) != 0:
                szi        = float(pos["szi"])
                entry_px   = float(pos.get("entryPx") or 0)
                upnl       = float(pos.get("unrealizedPnl") or 0)
                margin     = float(pos.get("marginUsed") or 0)
                liq_px     = pos.get("liquidationPx")
                positions.append({
                    "coin":       pos.get("coin", "?"),
                    "side":       "Long" if szi > 0 else "Short",
                    "size":       abs(szi),
                    "entry_px":   entry_px,
                    "upnl":       upnl,
                    "margin":     margin,
                    "liq_px":     float(liq_px) if liq_px else None,
                    "roe":        float(pos.get("returnOnEquity") or 0) * 100,
                })
        return {
            "equity":      float(summary.get("accountValue", 0)),
            "margin_used": float(summary.get("totalMarginUsed", 0)),
            "unrealised":  float(summary.get("totalUnrealizedPnl", 0)),
            "withdrawable":float(state.get("withdrawable", 0)),
            "positions":   positions,
            "error":       None,
        }
    except Exception as exc:
        return {"error": str(exc), "positions": [], "equity": 0,
                "margin_used": 0, "unrealised": 0, "withdrawable": 0}


def get_fills(n: int = 50) -> list:
    """Fetch recent fills (closed trades) for the account."""
    try:
        raw = info.user_fills(ADDRESS)
        fills = []
        for f in raw[-n:]:
            fills.append({
                "time":  datetime.fromtimestamp(f["time"] / 1000, tz=timezone.utc)
                                  .strftime("%m-%d %H:%M"),
                "coin":  f.get("coin", "?"),
                "side":  f.get("side", "?"),
                "px":    float(f.get("px", 0)),
                "sz":    float(f.get("sz", 0)),
                "pnl":   float(f.get("closedPnl", 0)),
                "fee":   float(f.get("fee", 0)),
            })
        return list(reversed(fills))   # newest first
    except Exception:
        return []


def get_log_lines(n: int = 120) -> list[str]:
    """Return the last n lines from the bot log file."""
    if not LOG_FILE.exists():
        return ["[log file not found — start the bot first]"]
    try:
        with open(LOG_FILE, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
        return [l.rstrip() for l in lines[-n:]][::-1]  # newest first
    except Exception as exc:
        return [f"[error reading log: {exc}]"]


def compute_stats(fills: list) -> dict:
    """Compute basic performance stats from fill history."""
    if not fills:
        return {"total_pnl": 0, "win_rate": 0, "wins": 0, "losses": 0,
                "total_trades": 0, "avg_pnl": 0, "total_fees": 0}
    closed = [f for f in fills if f["pnl"] != 0]
    wins   = [f for f in closed if f["pnl"] > 0]
    losses = [f for f in closed if f["pnl"] < 0]
    total_pnl   = sum(f["pnl"]  for f in closed)
    total_fees  = sum(f["fee"]  for f in fills)
    return {
        "total_pnl":    round(total_pnl, 4),
        "total_fees":   round(total_fees, 4),
        "net_pnl":      round(total_pnl - abs(total_fees), 4),
        "wins":         len(wins),
        "losses":       len(losses),
        "total_trades": len(closed),
        "win_rate":     round(len(wins) / len(closed) * 100, 1) if closed else 0,
        "avg_pnl":      round(total_pnl / len(closed), 4) if closed else 0,
    }


# ── API routes ────────────────────────────────────────────────────────────────

@app.route("/api/account")
def api_account():
    return jsonify(get_account())

@app.route("/api/fills")
def api_fills():
    fills = get_fills(100)
    return jsonify({"fills": fills, "stats": compute_stats(fills)})

@app.route("/api/logs")
def api_logs():
    return jsonify({"lines": get_log_lines(150)})

@app.route("/api/all")
def api_all():
    """Single endpoint that returns everything — used by the dashboard poll."""
    account = get_account()
    fills   = get_fills(100)
    stats   = compute_stats(fills)
    logs    = get_log_lines(80)
    mids    = {}
    try:
        coins = list({p["coin"] for p in account["positions"]})
        if coins:
            all_mids = info.all_mids()
            mids = {c: float(all_mids[c]) for c in coins if c in all_mids}
    except Exception:
        pass
    # Load backtest results if available
    backtest = []
    bt_path = Path(__file__).parent / "backtest_results.json"
    if bt_path.exists():
        try:
            with open(bt_path) as f:
                backtest = json.load(f)
        except Exception:
            pass

    return jsonify({
        "account": account,
        "fills":   fills[:30],
        "stats":   stats,
        "logs":    logs,
        "mids":    mids,
        "backtest": backtest,
        "symbols": os.getenv("SYMBOLS", os.getenv("SYMBOL", "ETH,SOL")).split(","),
        "ts":      datetime.now(timezone.utc).strftime("%H:%M:%S UTC"),
        "network": "TESTNET" if USE_TESTNET else "MAINNET",
        "address": ADDRESS[:6] + "..." + ADDRESS[-4:] if ADDRESS else "n/a",
    })


# ── HTML dashboard ────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Hyperliquid Bot Dashboard</title>
<style>
  :root {
    --bg:      #0d1117;
    --surface: #161b22;
    --border:  #30363d;
    --text:    #e6edf3;
    --muted:   #8b949e;
    --blue:    #58a6ff;
    --green:   #3fb950;
    --red:     #f85149;
    --yellow:  #e3b341;
    --purple:  #bc8cff;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text);
         font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', monospace;
         font-size: 13px; min-height: 100vh; }

  /* ── Header ── */
  header {
    background: var(--surface); border-bottom: 1px solid var(--border);
    padding: 14px 24px; display: flex; align-items: center;
    justify-content: space-between;
  }
  header h1 { font-size: 16px; font-weight: 600; color: var(--blue); }
  .badge {
    padding: 3px 10px; border-radius: 20px; font-size: 11px;
    font-weight: 700; letter-spacing: 0.5px;
  }
  .badge-mainnet { background: #3d1212; color: var(--red); border: 1px solid var(--red); }
  .badge-testnet { background: #1a2d1a; color: var(--green); border: 1px solid var(--green); }
  #ts { color: var(--muted); font-size: 12px; }

  /* ── Layout ── */
  .page { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 16px;
          padding: 20px 24px; }

  /* ── Cards ── */
  .card {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 10px; padding: 18px;
  }
  .card-title {
    font-size: 10px; font-weight: 700; text-transform: uppercase;
    letter-spacing: 1px; color: var(--muted); margin-bottom: 14px;
    display: flex; align-items: center; gap: 8px;
  }
  .card-full  { grid-column: 1 / -1; }
  .card-two   { grid-column: span 2; }

  /* ── Stat grid ── */
  .stat-grid { display: grid; grid-template-columns: repeat(4,1fr); gap: 14px; }
  .stat { }
  .stat-label { font-size: 11px; color: var(--muted); margin-bottom: 4px; }
  .stat-value { font-size: 22px; font-weight: 700; }
  .stat-sub   { font-size: 11px; color: var(--muted); margin-top: 2px; }

  .up   { color: var(--green); }
  .down { color: var(--red);   }
  .flat { color: var(--muted); }

  /* ── Positions table ── */
  table { width: 100%; border-collapse: collapse; font-size: 12px; }
  th {
    text-align: left; padding: 6px 10px; font-size: 10px; font-weight: 600;
    text-transform: uppercase; letter-spacing: 0.8px; color: var(--muted);
    border-bottom: 1px solid var(--border);
  }
  td { padding: 9px 10px; border-bottom: 1px solid #1e242c; }
  tr:last-child td { border-bottom: none; }
  tr:hover td { background: #1c2333; }
  .empty { color: var(--muted); text-align: center; padding: 20px; }

  .pill {
    padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 600;
  }
  .pill-long  { background: #1a3a1a; color: var(--green); }
  .pill-short { background: #3a1a1a; color: var(--red);   }

  /* ── Mini equity chart ── */
  #equity-chart { width: 100%; height: 120px; display: block; }

  /* ── Log ── */
  #log-box {
    background: #0d1117; border: 1px solid var(--border); border-radius: 6px;
    padding: 12px; height: 240px; overflow-y: auto; font-size: 11px;
    font-family: 'SF Mono', 'Fira Code', monospace; line-height: 1.7;
  }
  #log-box .log-signal { color: #79c0ff; }
  #log-box .log-order  { color: var(--green); }
  #log-box .log-risk   { color: var(--yellow); }
  #log-box .log-error  { color: var(--red); }
  #log-box .log-info   { color: var(--muted); }

  /* ── Pulse dot ── */
  .pulse { display: inline-block; width: 8px; height: 8px; border-radius: 50%;
           background: var(--green); animation: pulse 2s infinite; }
  @keyframes pulse {
    0%,100% { opacity: 1; } 50% { opacity: 0.3; }
  }

  /* ── Fills table scrollable ── */
  .fills-wrap { max-height: 260px; overflow-y: auto; }

  /* ── Responsive ── */
  @media (max-width: 900px) {
    .page { grid-template-columns: 1fr; }
    .card-two, .card-full { grid-column: 1; }
    .stat-grid { grid-template-columns: repeat(2,1fr); }
  }
</style>
</head>
<body>

<header>
  <h1>⚡ Hyperliquid Bot</h1>
  <div style="display:flex;align-items:center;gap:14px;">
    <span id="network-badge" class="badge">...</span>
    <span id="address-badge" style="color:var(--muted);font-size:12px;font-family:monospace;"></span>
    <span class="pulse"></span>
    <span id="ts" style="min-width:90px;text-align:right;">--:--:-- UTC</span>
  </div>
</header>

<div class="page">

  <!-- ── Account Summary ── -->
  <div class="card card-full">
    <div class="card-title">📊 Account Summary</div>
    <div class="stat-grid">
      <div class="stat">
        <div class="stat-label">Equity</div>
        <div class="stat-value" id="s-equity">—</div>
        <div class="stat-sub">total account value</div>
      </div>
      <div class="stat">
        <div class="stat-label">Unrealised PnL</div>
        <div class="stat-value" id="s-upnl">—</div>
        <div class="stat-sub">open positions</div>
      </div>
      <div class="stat">
        <div class="stat-label">Margin Used</div>
        <div class="stat-value" id="s-margin">—</div>
        <div class="stat-sub" id="s-margin-pct"></div>
      </div>
      <div class="stat">
        <div class="stat-label">Withdrawable</div>
        <div class="stat-value" id="s-withdraw">—</div>
        <div class="stat-sub">available</div>
      </div>
    </div>
  </div>

  <!-- ── Performance Stats ── -->
  <div class="card">
    <div class="card-title">🏆 Performance</div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;">
      <div class="stat">
        <div class="stat-label">Net PnL</div>
        <div class="stat-value" id="p-net">—</div>
        <div class="stat-sub">after fees</div>
      </div>
      <div class="stat">
        <div class="stat-label">Win Rate</div>
        <div class="stat-value" id="p-wr">—</div>
        <div class="stat-sub" id="p-wl">— wins / — losses</div>
      </div>
      <div class="stat">
        <div class="stat-label">Total Trades</div>
        <div class="stat-value" id="p-trades">—</div>
        <div class="stat-sub">closed positions</div>
      </div>
      <div class="stat">
        <div class="stat-label">Avg PnL</div>
        <div class="stat-value" id="p-avg">—</div>
        <div class="stat-sub">per trade</div>
      </div>
    </div>
  </div>

  <!-- ── Open Positions ── -->
  <div class="card card-two">
    <div class="card-title">📈 Open Positions</div>
    <table>
      <thead>
        <tr>
          <th>Coin</th>
          <th>Side</th>
          <th>Size</th>
          <th>Entry</th>
          <th>Mark</th>
          <th>Unr. PnL</th>
          <th>ROE</th>
          <th>Liq. Price</th>
        </tr>
      </thead>
      <tbody id="positions-body">
        <tr><td colspan="8" class="empty">No open positions</td></tr>
      </tbody>
    </table>
  </div>

  <!-- ── Recent Fills ── -->
  <div class="card card-two">
    <div class="card-title">📋 Recent Fills</div>
    <div class="fills-wrap">
      <table>
        <thead>
          <tr>
            <th>Time</th>
            <th>Coin</th>
            <th>Side</th>
            <th>Price</th>
            <th>Size</th>
            <th>Closed PnL</th>
            <th>Fee</th>
          </tr>
        </thead>
        <tbody id="fills-body">
          <tr><td colspan="7" class="empty">No fills yet</td></tr>
        </tbody>
      </table>
    </div>
  </div>

  <!-- ── Equity Sparkline ── -->
  <div class="card">
    <div class="card-title">📉 PnL per Trade</div>
    <svg id="equity-chart" viewBox="0 0 300 100"></svg>
  </div>

  <!-- ── Backtest Results ── -->
  <div class="card card-full" id="backtest-section" style="display:none;">
    <div class="card-title">🧪 Backtest Results <span style="font-weight:400;color:var(--muted)">(historical simulation)</span></div>
    <div id="backtest-cards" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:14px;"></div>
  </div>

  <!-- ── Bot Log ── -->
  <div class="card card-full">
    <div class="card-title">🖥️ Bot Log <span style="font-weight:400;color:var(--muted)">(live)</span></div>
    <div id="log-box">Waiting for log data...</div>
  </div>

</div>

<script>
const $ = id => document.getElementById(id);

function fmt(n, decimals=2) {
  if (n === null || n === undefined) return '—';
  return Number(n).toFixed(decimals);
}
function fmtUSD(n) {
  if (n === null || n === undefined) return '—';
  const v = Number(n);
  return (v >= 0 ? '+' : '') + '$' + Math.abs(v).toFixed(2);
}
function colorClass(v) {
  if (v > 0) return 'up';
  if (v < 0) return 'down';
  return 'flat';
}

function renderAccount(a) {
  $('s-equity').textContent   = '$' + fmt(a.equity);
  $('s-equity').className     = 'stat-value';

  const upnl = a.unrealised;
  $('s-upnl').textContent     = fmtUSD(upnl);
  $('s-upnl').className       = 'stat-value ' + colorClass(upnl);

  $('s-margin').textContent   = '$' + fmt(a.margin_used);
  const pct = a.equity > 0 ? (a.margin_used / a.equity * 100).toFixed(1) : '0.0';
  $('s-margin-pct').textContent = pct + '% of equity';

  $('s-withdraw').textContent = '$' + fmt(a.withdrawable);
}

function renderPositions(positions, mids) {
  const tbody = $('positions-body');
  if (!positions || positions.length === 0) {
    tbody.innerHTML = '<tr><td colspan="8" class="empty">No open positions</td></tr>';
    return;
  }
  tbody.innerHTML = positions.map(p => {
    const mark  = mids[p.coin] ? '$' + fmt(mids[p.coin], 4) : '—';
    const pnlCls = colorClass(p.upnl);
    const liq   = p.liq_px ? '$' + fmt(p.liq_px, 2) : '—';
    const pill  = p.side === 'Long'
      ? '<span class="pill pill-long">Long</span>'
      : '<span class="pill pill-short">Short</span>';
    return `<tr>
      <td><strong>${p.coin}</strong></td>
      <td>${pill}</td>
      <td>${fmt(p.size, 4)}</td>
      <td>$${fmt(p.entry_px, 2)}</td>
      <td>${mark}</td>
      <td class="${pnlCls}">${fmtUSD(p.upnl)}</td>
      <td class="${colorClass(p.roe)}">${fmt(p.roe, 2)}%</td>
      <td style="color:var(--red)">${liq}</td>
    </tr>`;
  }).join('');
}

function renderFills(fills) {
  const tbody = $('fills-body');
  if (!fills || fills.length === 0) {
    tbody.innerHTML = '<tr><td colspan="7" class="empty">No fills yet</td></tr>';
    return;
  }
  tbody.innerHTML = fills.map(f => {
    const pnlCls = colorClass(f.pnl);
    const sideCls = f.side && f.side.toLowerCase().includes('b') ? 'up' : 'down';
    return `<tr>
      <td style="color:var(--muted)">${f.time}</td>
      <td><strong>${f.coin}</strong></td>
      <td class="${sideCls}">${f.side}</td>
      <td>$${fmt(f.px, 2)}</td>
      <td>${fmt(f.sz, 5)}</td>
      <td class="${pnlCls}">${f.pnl !== 0 ? fmtUSD(f.pnl) : '—'}</td>
      <td style="color:var(--muted)">-$${fmt(Math.abs(f.fee), 4)}</td>
    </tr>`;
  }).join('');
}

function renderStats(s) {
  const netCls = colorClass(s.net_pnl);
  $('p-net').textContent    = fmtUSD(s.net_pnl);
  $('p-net').className      = 'stat-value ' + netCls;
  $('p-wr').textContent     = s.total_trades > 0 ? s.win_rate + '%' : '—';
  $('p-wl').textContent     = s.wins + ' wins / ' + s.losses + ' losses';
  $('p-trades').textContent = s.total_trades || '—';
  const avgCls = colorClass(s.avg_pnl);
  $('p-avg').textContent    = s.total_trades > 0 ? fmtUSD(s.avg_pnl) : '—';
  $('p-avg').className      = 'stat-value ' + avgCls;
}

function renderPnLChart(fills) {
  const svg    = $('equity-chart');
  const closed = fills.filter(f => f.pnl !== 0).slice().reverse(); // oldest first
  if (closed.length < 2) {
    svg.innerHTML = '<text x="150" y="55" text-anchor="middle" fill="#484f58" font-size="12">Not enough trades yet</text>';
    return;
  }
  const pnls = closed.map(f => f.pnl);
  const cumulative = pnls.reduce((acc, v) => {
    acc.push((acc[acc.length - 1] || 0) + v);
    return acc;
  }, []);
  const W = 300, H = 100, PAD = 10;
  const minV = Math.min(...cumulative, 0);
  const maxV = Math.max(...cumulative, 0);
  const range = maxV - minV || 1;
  const scaleX = (i) => PAD + (i / (cumulative.length - 1)) * (W - PAD * 2);
  const scaleY = (v) => PAD + (1 - (v - minV) / range) * (H - PAD * 2);

  const pts = cumulative.map((v, i) => `${scaleX(i)},${scaleY(v)}`).join(' ');
  const lastVal = cumulative[cumulative.length - 1];
  const colour  = lastVal >= 0 ? '#3fb950' : '#f85149';

  // Fill area
  const firstPt = `${scaleX(0)},${scaleY(0)}`;
  const lastPt  = `${scaleX(cumulative.length-1)},${scaleY(0)}`;
  const fillPts = `${firstPt} ${pts} ${lastPt}`;

  svg.innerHTML = `
    <defs>
      <linearGradient id="g" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0%" stop-color="${colour}" stop-opacity="0.3"/>
        <stop offset="100%" stop-color="${colour}" stop-opacity="0"/>
      </linearGradient>
    </defs>
    <polygon points="${fillPts}" fill="url(#g)"/>
    <polyline points="${pts}" fill="none" stroke="${colour}" stroke-width="1.5"/>
    <line x1="${PAD}" y1="${scaleY(0)}" x2="${W-PAD}" y2="${scaleY(0)}"
          stroke="#30363d" stroke-width="0.5" stroke-dasharray="3,3"/>
    <text x="${W-PAD}" y="${scaleY(lastVal) - 4}" fill="${colour}" font-size="10"
          text-anchor="end">${lastVal >= 0 ? '+' : ''}$${lastVal.toFixed(2)}</text>
  `;
}

function renderLogs(lines) {
  const box = $('log-box');
  const html = lines.map(line => {
    let cls = 'log-info';
    if (line.includes('[SIGNAL]')) cls = 'log-signal';
    else if (line.includes('[ORDER]')) cls = 'log-order';
    else if (line.includes('[RISK]'))  cls = 'log-risk';
    else if (line.includes('ERROR') || line.includes('[POSITION]')) cls = 'log-error';
    return `<div class="${cls}">${escapeHtml(line)}</div>`;
  }).join('');
  box.innerHTML = html || '<div class="log-info">No log lines yet.</div>';
}

function escapeHtml(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

async function poll() {
  try {
    const resp = await fetch('/api/all');
    const d    = await resp.json();

    $('ts').textContent = d.ts || '';
    $('address-badge').textContent = d.address || '';

    const badge = $('network-badge');
    if (d.network === 'MAINNET') {
      badge.textContent = '🔴 MAINNET';
      badge.className   = 'badge badge-mainnet';
    } else {
      badge.textContent = '🟢 TESTNET';
      badge.className   = 'badge badge-testnet';
    }

    if (!d.account.error) {
      renderAccount(d.account);
      renderPositions(d.account.positions, d.mids || {});
    }

    renderFills(d.fills || []);
    renderStats(d.stats || {});
    renderPnLChart(d.fills || []);
    renderLogs(d.logs || []);
    renderBacktest(d.backtest || []);
  } catch(e) {
    console.error('Poll error:', e);
  }
}

function renderBacktest(results) {
  const section = $('backtest-section');
  const container = $('backtest-cards');
  if (!results || results.length === 0) {
    section.style.display = 'none';
    return;
  }
  section.style.display = '';
  container.innerHTML = results.map(r => {
    const pnlCls = colorClass(r.total_pnl);
    const pf = r.profit_factor === Infinity ? '∞' : r.profit_factor;
    return `<div class="card" style="background:var(--bg);border-color:var(--border);">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;">
        <strong style="font-size:15px;">${r.symbol}</strong>
        <span style="font-size:11px;color:var(--muted)">${r.timeframe} · ${r.period}</span>
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;font-size:12px;">
        <div><span style="color:var(--muted)">PnL</span><br><strong class="${pnlCls}">${fmtUSD(r.total_pnl)}</strong></div>
        <div><span style="color:var(--muted)">Win Rate</span><br><strong>${r.win_rate}%</strong></div>
        <div><span style="color:var(--muted)">Trades</span><br><strong>${r.total_trades}</strong></div>
        <div><span style="color:var(--muted)">Max DD</span><br><strong class="down">${r.max_drawdown}%</strong></div>
        <div><span style="color:var(--muted)">Sharpe</span><br><strong>${r.sharpe_ratio}</strong></div>
        <div><span style="color:var(--muted)">Profit Factor</span><br><strong>${pf}</strong></div>
      </div>
      ${r.filters_applied && r.filters_applied.length ? `<div style="margin-top:8px;font-size:10px;color:var(--muted)">Filters: ${r.filters_applied.join(', ')}</div>` : ''}
      ${r.equity_curve && r.equity_curve.length > 2 ? renderMiniChart(r.equity_curve, r.symbol) : ''}
    </div>`;
  }).join('');
}

function renderMiniChart(curve, label) {
  const W = 260, H = 60, PAD = 4;
  const min = Math.min(...curve); const max = Math.max(...curve);
  const range = max - min || 1;
  const sx = (i) => PAD + (i/(curve.length-1)) * (W-PAD*2);
  const sy = (v) => PAD + (1-(v-min)/range) * (H-PAD*2);
  const pts = curve.map((v,i) => `${sx(i)},${sy(v)}`).join(' ');
  const last = curve[curve.length-1]; const first = curve[0];
  const col = last >= first ? '#3fb950' : '#f85149';
  return `<svg viewBox="0 0 ${W} ${H}" style="width:100%;height:${H}px;margin-top:8px;display:block;">
    <polyline points="${pts}" fill="none" stroke="${col}" stroke-width="1.5"/>
    <line x1="${PAD}" y1="${sy(first)}" x2="${W-PAD}" y2="${sy(first)}" stroke="#30363d" stroke-width="0.5" stroke-dasharray="3,3"/>
  </svg>`;
}

// Initial load + repeat every 5s
poll();
setInterval(poll, 5000);
</script>
</body>
</html>
"""

@app.route("/")
def index():
    return render_template_string(HTML)


if __name__ == "__main__":
    print(f"\n  📊 Dashboard → http://localhost:{DASHBOARD_PORT}")
    print(f"  Network : {'TESTNET' if USE_TESTNET else 'MAINNET'}")
    print(f"  Address : {ADDRESS[:10]}...{ADDRESS[-4:] if len(ADDRESS) > 14 else ''}\n")
    app.run(host="0.0.0.0", port=DASHBOARD_PORT, debug=False)
