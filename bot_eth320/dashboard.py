"""
dashboard.py - Flask live web dashboard.
Embedded Tailwind CSS, dark mode, auto-refresh every 2s.
"""

import csv
import io
import os
import time
from flask import Flask, jsonify, Response


def create_app(bot):
    app = Flask(__name__)
    CSV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trade_history.csv")

    def _bids():
        bids = {}
        for br in bot.feed.get_brackets():
            bk = bot.feed.get_book(br.yes_tid)
            if bk and bk.bb() and bk.ba():
                bids[(br.strike, br.end_ts, "YES")] = bk.bb()
                bids[(br.strike, br.end_ts, "NO")] = 1.0 - bk.ba()
        return bids

    @app.route("/")
    def index(): return DASHBOARD_HTML

    @app.route("/api/stats")
    def api_stats():
        t = bot.trader; f = bot.feed
        bids = _bids()
        closed = [p for p in t.positions if p.status == "CLOSED"]
        return jsonify({
            "initial_capital": t.initial_capital,
            "current_equity": round(t.total_equity(bids), 2),
            "cash": round(t.get_cash(), 2),
            "unrealized_pnl": round(t.unrealized_pnl(bids), 4),
            "realized_pnl": round(t.realized_pnl(), 4),
            "win_rate": round(t.win_rate(), 1),
            "open_positions_count": len(t.open_positions()),
            "closed_trades": len(closed),
            "pending_makers": len(t.pending_makers),
            "eth_price": f.get_eth_price(),
            "poly_ok": f.poly_ok,
            "bnc_ok": f.bnc_ok,
            "brackets_count": len(f.get_brackets()),
            "uptime_sec": int(time.time() - bot.start_time),
            "status": bot.status,
            "live_mode": getattr(bot, "executor", None) is not None,
        })

    @app.route("/api/positions")
    def api_positions():
        t = bot.trader; bids = _bids()
        oracle_data = bot.oracle.latest if bot.oracle else {}
        now = time.time()
        result = []
        for pos in t.open_positions():
            bid = bids.get((pos.strike, pos.end_ts, pos.side), pos.entry_price)
            pnl_usd = (bid - pos.entry_price) * pos.tokens
            pnl_pct = (pnl_usd / pos.cost_usd * 100) if pos.cost_usd > 0 else 0
            pot_pnl = (pos.tokens * 1.0) - pos.cost_usd  # potential if resolves YES at $1
            p_real = 0
            for ed in oracle_data.values():
                pd = ed.get("probabilities", {}).get(pos.strike)
                if pd: p_real = pd.get("p_real_yes", 0); break
            runtime_min = (now - pos.opened_at) / 60
            result.append({
                "id": pos.id,
                "strike": pos.strike,
                "event": pos.event_title[:35] if pos.event_title else f"ETH>{pos.strike:,}",
                "order_type": pos.order_type,
                "entry": round(pos.entry_price, 4),
                "current": round(bid, 4),
                "tokens": round(pos.tokens, 1),
                "invested": round(pos.cost_usd, 2),
                "pnl_usd": round(pnl_usd, 4),
                "pnl_pct": round(pnl_pct, 1),
                "potential_pnl": round(pot_pnl, 2),
                "p_real": round(p_real, 4),
                "edge_entry": round(pos.edge_at_entry * 100, 2),
                "runtime_min": round(runtime_min, 1),
                "entry_reason": pos.entry_reason,
                "side": pos.side,
            })
        return jsonify(result)

    @app.route("/api/log")
    def api_log():
        return jsonify(bot.trader.log_lines[-80:])

    @app.route("/api/history")
    def api_history():
        rows = []
        try:
            if os.path.exists(CSV_PATH):
                with open(CSV_PATH, "r", encoding="utf-8") as f:
                    for row in csv.DictReader(f):
                        rows.append(row)
        except: pass
        return jsonify(rows[-100:])

    @app.route("/api/brackets")
    def api_brackets():
        from quant_engine import calculate_nd2
        from datetime import datetime, timezone
        feed   = bot.feed
        oracle = bot.oracle
        now_ts = time.time()
        spot   = oracle.spot_price if oracle else 0
        result = []
        for br in feed.get_brackets():
            ybk = feed.get_book(br.yes_tid)
            y_ask = ybk.ba() if ybk else None
            y_bid = ybk.bb() if ybk else None

            # Per-bracket IV and p_real (strict 1-to-1 expiry mapping)
            p_real = None
            iv_pct = None
            exp_code = None
            if oracle and spot > 0 and br.end_ts > now_ts:
                t_years = (br.end_ts - now_ts) / 31_536_000.0
                target_dt = datetime.fromtimestamp(br.end_ts, tz=timezone.utc)
                iv, code = oracle.get_iv_for_date(target_dt, br.strike)
                if iv and iv > 0 and t_years > 0:
                    p_real   = round(calculate_nd2(spot, br.strike, t_years, iv), 4)
                    iv_pct   = round(iv * 100, 1)
                    exp_code = code

            yes_edge = round((p_real - y_ask) * 100, 2) if (p_real and y_ask) else None
            n_bid = 1.0 - y_ask if y_ask else None
            n_ask = 1.0 - y_bid if y_bid else None
            no_p_real = 1.0 - p_real if p_real else None
            no_edge = round((no_p_real - n_ask) * 100, 2) if (no_p_real and n_ask) else None

            result.append({
                "strike":    br.strike,
                "event":     br.event_title[:40],
                "end_ts":    br.end_ts,
                "y_bid":     round(y_bid, 4) if y_bid else None,
                "y_ask":     round(y_ask, 4) if y_ask else None,
                "n_bid":     round(n_bid, 4) if n_bid else None,
                "n_ask":     round(n_ask, 4) if n_ask else None,
                "spread_pct": round(ybk.spread() * 100, 1) if ybk and ybk.spread() else None,
                "p_real":    p_real,
                "p_no":      no_p_real,
                "yes_edge":  yes_edge,
                "no_edge":   no_edge,
                "iv_pct":    iv_pct,
                "exp_code":  exp_code,
                "book_age":  round(time.time() - ybk.ts, 0) if ybk and ybk.ts > 0 else None,
            })
        return jsonify(result)

    @app.route("/api/history/csv")
    def download_csv():
        try:
            with open(CSV_PATH, "r", encoding="utf-8") as f:
                return Response(f.read(), mimetype="text/csv",
                    headers={"Content-disposition": "attachment; filename=trade_history.csv"})
        except: return Response("No data", mimetype="text/plain")

    return app


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Quant Arb — Paper Trading</title>
<script src="https://cdn.tailwindcss.com"></script>
<style>
body{background:#080d16;color:#c9d1d9;font-family:'Inter',system-ui,sans-serif;margin:0}
.card{background:rgba(16,23,42,.9);border:1px solid rgba(51,65,100,.4);border-radius:12px}
.glow-cyan{box-shadow:0 0 24px rgba(34,211,238,.06)}
.glow-green{box-shadow:0 0 24px rgba(74,222,128,.06)}
.pup{color:#4ade80}.pdn{color:#f87171}
.tag-taker{background:rgba(251,191,36,.12);color:#fbbf24;padding:1px 6px;border-radius:4px;font-size:10px}
.tag-maker{background:rgba(139,92,246,.12);color:#a78bfa;padding:1px 6px;border-radius:4px;font-size:10px}
@keyframes fadeIn{from{opacity:0;transform:translateY(-4px)}to{opacity:1;transform:translateY(0)}}
.fade{animation:fadeIn .3s ease}
#liveLog{font-family:'JetBrains Mono',monospace;font-size:11px;line-height:1.7}
th{text-transform:uppercase;font-size:10px;letter-spacing:.6px;color:#475569;padding:8px 10px;text-align:left}
td{padding:7px 10px;border-bottom:1px solid rgba(51,65,100,.2);font-size:12px}
tr:last-child td{border:none}
::-webkit-scrollbar{width:4px}::-webkit-scrollbar-thumb{background:#1e293b;border-radius:4px}
.badge{padding:3px 10px;border-radius:20px;font-size:11px;font-weight:600}
</style>
</head>
<body class="p-3">
<div class="max-w-screen-2xl mx-auto space-y-3">

  <!-- HEADER -->
  <div class="flex items-center justify-between py-1">
    <div>
      <h1 class="text-xl font-bold bg-gradient-to-r from-cyan-400 via-blue-400 to-violet-400 bg-clip-text text-transparent tracking-tight">
        ⚡ Quant Arb Engine
      </h1>
      <p class="text-xs text-slate-600">Multi-Strike Probability Arbitrage <span id="modeBadge" class="ml-2 px-2 py-0.5 rounded text-[10px] font-bold tracking-wider"></span></p>
    </div>
    <div class="flex items-center gap-3">
      <span id="statusBadge" class="badge"></span>
      <div class="text-right">
        <div id="ethPrice" class="text-lg font-bold text-amber-400 font-mono"></div>
        <div class="text-xs text-slate-500">ETH/USDT</div>
      </div>
    </div>
  </div>

  <!-- KPIs -->
  <div class="grid grid-cols-5 gap-2">
    <div class="card glow-cyan p-3 text-center">
      <div class="text-xs text-slate-500 mb-1">Initial Capital</div>
      <div class="text-lg font-bold text-slate-300 font-mono" id="kInitCap">—</div>
    </div>
    <div class="card glow-green p-3 text-center">
      <div class="text-xs text-slate-500 mb-1">Current Equity</div>
      <div class="text-lg font-bold font-mono" id="kEquity">—</div>
      <div class="text-xs text-slate-500" id="kEquityChg">—</div>
    </div>
    <div class="card p-3 text-center">
      <div class="text-xs text-slate-500 mb-1">Unrealized PnL</div>
      <div class="text-lg font-bold font-mono" id="kUnreal">—</div>
    </div>
    <div class="card p-3 text-center">
      <div class="text-xs text-slate-500 mb-1">Realized PnL</div>
      <div class="text-lg font-bold font-mono" id="kReal">—</div>
    </div>
    <div class="card p-3 text-center">
      <div class="text-xs text-slate-500 mb-1">Win Rate</div>
      <div class="text-lg font-bold font-mono" id="kWinRate">—</div>
      <div class="text-xs text-slate-500" id="kClosed">—</div>
    </div>
  </div>

  <!-- STATUS BAR -->
  <div class="card px-4 py-2 flex items-center gap-5 text-xs text-slate-400 flex-wrap">
    <span>⏱ <b id="sUptime">—</b></span>
    <span>📡 Poly: <b id="sPoly">—</b></span>
    <span>₿ Bnc: <b id="sBnc">—</b></span>
    <span>🔎 Brackets: <b id="sBkt" class="text-cyan-400">—</b></span>
    <span>📂 Open: <b id="sOpen" class="text-green-400">—</b></span>
    <span>⏳ Pending: <b id="sPend" class="text-amber-400">—</b></span>
    <span>✅ Closed: <b id="sClosed2">—</b></span>
    <span class="ml-auto text-slate-600 text-xs" id="lastUpdate">—</span>
  </div>

  <!-- OPEN POSITIONS + LOG -->
  <div class="grid grid-cols-1 xl:grid-cols-2 gap-3">
    <div class="card p-4">
      <h2 class="text-xs font-bold text-slate-400 tracking-widest mb-3">OPEN POSITIONS</h2>
      <div class="overflow-x-auto">
        <table class="w-full">
          <thead><tr>
            <th>Event / Strike</th>
            <th>Type</th>
            <th>Side</th>
            <th>Entry</th>
            <th>Current</th>
            <th>Tokens</th>
            <th>Invested</th>
            <th>Unreal PnL</th>
            <th>Potential PnL</th>
            <th>Edge@Entry</th>
            <th>P(real)</th>
            <th>Runtime</th>
          </tr></thead>
          <tbody id="posTable"></tbody>
        </table>
      </div>
      <p id="posEmpty" class="text-center text-slate-700 py-6 text-sm">No open positions</p>
    </div>

    <div class="card p-4">
      <h2 class="text-xs font-bold text-slate-400 tracking-widest mb-2">LIVE LOG</h2>
      <div id="liveLog" class="h-72 overflow-y-auto bg-slate-950/80 rounded-lg p-3"></div>
    </div>
  </div>

  <!-- MARKET BRACKETS -->
  <div class="card p-4">
    <h2 class="text-xs font-bold text-slate-400 tracking-widest mb-3">MARKET BRACKETS</h2>
    <div class="overflow-x-auto">
      <table class="w-full">
        <thead><tr>
          <th>Strike</th>
          <th>Event</th>
          <th>Y Bid</th>
          <th>Y Ask</th>
          <th>N Bid</th>
          <th>N Ask</th>
          <th>Spread</th>
          <th>P(Y)</th>
          <th>P(N)</th>
          <th>Y Edge%</th>
          <th>N Edge%</th>
          <th>IV %</th>
          <th>Book Age</th>
        </tr></thead>
        <tbody id="bktTable"></tbody>
      </table>
    </div>
    <p id="bktEmpty" class="text-center text-slate-700 py-4 text-sm">Loading brackets...</p>
  </div>

  <!-- TRADE HISTORY -->
  <div class="card p-4">
    <div class="flex items-center justify-between mb-3">
      <h2 class="text-xs font-bold text-slate-400 tracking-widest">TRADE LOG <span class="text-slate-600 font-normal normal-case">(OPEN + CLOSE events)</span></h2>
      <a href="/api/history/csv" class="text-xs text-cyan-500 hover:text-cyan-300 hover:underline">⬇ Download CSV</a>
    </div>
    <div class="overflow-x-auto">
      <table class="w-full">
        <thead><tr>
          <th>Time</th>
          <th>Event</th>
          <th>Type</th>
          <th>Side</th>
          <th>Strike</th>
          <th>Tokens</th>
          <th>Entry</th>
          <th>Exit</th>
          <th>PnL</th>
          <th>ROI</th>
          <th>Reason</th>
          <th>Runtime</th>
        </tr></thead>
        <tbody id="histTable"></tbody>
      </table>
    </div>
    <p id="histEmpty" class="text-center text-slate-700 py-4 text-sm">No activity yet</p>
  </div>

</div><!-- /container -->

<script>
const $ = id => document.getElementById(id);
const fmt = (v, d=2) => v != null && !isNaN(v) ? Number(v).toFixed(d) : '—';
const fmtK = v => v != null ? '$'+Number(v).toLocaleString('en',{maximumFractionDigits:0}) : '—';
const fmtS = v => v != null ? '$'+Number(v).toFixed(2) : '—';
const pnlClass = v => Number(v) >= 0 ? 'pup' : 'pdn';
const pnlFmt = (v, pre='$') => {
  const n = Number(v);
  if (isNaN(n)) return '—';
  return `<span class="${pnlClass(n)}">${n>=0?'+':''}${pre=='$'?'$'+Math.abs(n).toFixed(2):n.toFixed(1)+'%'}</span>`;
};

async function poll() {
  try {
    const [stats, pos, logData, hist, bkts] = await Promise.all([
      fetch('/api/stats').then(r=>r.json()),
      fetch('/api/positions').then(r=>r.json()),
      fetch('/api/log').then(r=>r.json()),
      fetch('/api/history').then(r=>r.json()),
      fetch('/api/brackets').then(r=>r.json()),
    ]);

    // KPIs
    $('kInitCap').textContent = fmtS(stats.initial_capital);
    $('kEquity').textContent = fmtS(stats.current_equity);
    $('kEquity').className = 'text-lg font-bold font-mono ' + (stats.current_equity >= stats.initial_capital ? 'pup' : 'pdn');
    const eqChg = stats.current_equity - stats.initial_capital;
    $('kEquityChg').innerHTML = pnlFmt(eqChg);
    $('kUnreal').innerHTML = pnlFmt(stats.unrealized_pnl);
    $('kReal').innerHTML = pnlFmt(stats.realized_pnl);
    $('kWinRate').textContent = fmt(stats.win_rate, 1) + '%';
    $('kClosed').textContent = stats.closed_trades + ' trades';

    // Status bar
    $('ethPrice').textContent = stats.eth_price ? fmtK(stats.eth_price) : '—';
    const m = Math.floor(stats.uptime_sec/60), s = stats.uptime_sec%60;
    $('sUptime').textContent = `${m}m${s}s`;
    $('sPoly').innerHTML = stats.poly_ok ? '<span class="pup">●</span> OK' : '<span class="pdn">●</span> OFF';
    $('sBnc').innerHTML  = stats.bnc_ok  ? '<span class="pup">●</span> OK' : '<span class="pdn">●</span> OFF';
    $('sBkt').textContent = stats.brackets_count;
    $('sOpen').textContent = stats.open_positions_count;
    $('sPend').textContent = stats.pending_makers;
    $('sClosed2').textContent = stats.closed_trades;
    $('lastUpdate').textContent = 'Updated ' + new Date().toLocaleTimeString();
    $('statusBadge').textContent = stats.status;
    $('statusBadge').className = 'badge ' + (stats.status === 'RUNNING' ?
      'bg-green-500/20 text-green-400 border border-green-500/30' :
      'bg-slate-500/20 text-slate-400 border border-slate-500/30');

    const mb = $('modeBadge');
    if (stats.live_mode) {
      mb.textContent = '🔴 LIVE EXECUTION';
      mb.className = 'ml-2 px-2 py-0.5 rounded text-[10px] font-bold tracking-wider bg-red-900/40 text-red-400 border border-red-800/50';
    } else {
      mb.textContent = '📝 PAPER TRADING';
      mb.className = 'ml-2 px-2 py-0.5 rounded text-[10px] font-bold tracking-wider bg-blue-900/30 text-blue-300 border border-blue-800/50';
    }

    // Open Positions
    const pt = $('posTable'); pt.innerHTML='';
    if (pos.length) {
      $('posEmpty').style.display = 'none';
      pt.innerHTML = pos.map(p => {
        const typeTag = p.order_type === 'TAKER_FAK'
          ? '<span class="tag-taker">TAKER</span>'
          : '<span class="tag-maker">MAKER</span>';
        const pdir = p.current >= p.entry ? '▲' : '▼';
        const pdirColor = p.current >= p.entry ? 'pup' : 'pdn';
        return `<tr class="fade">
          <td>
            <div class="font-bold text-slate-200">$${p.strike.toLocaleString()}</div>
            <div class="text-xs text-slate-500 truncate max-w-[160px]">${p.event}</div>
          </td>
          <td>${typeTag}</td>
          <td>${p.side === 'YES'
            ? '<span style="color:#22d3ee;font-size:10px;background:rgba(34,211,238,.12);padding:1px 7px;border-radius:4px;font-weight:700">YES</span>'
            : '<span style="color:#fb923c;font-size:10px;background:rgba(251,146,60,.12);padding:1px 7px;border-radius:4px;font-weight:700">NO</span>'
          }</td>
          <td class="font-mono text-slate-300">${fmt(p.entry, 4)}</td>
          <td class="font-mono"><span class="${pdirColor}">${pdir} ${fmt(p.current, 4)}</span></td>
          <td class="font-mono">${fmt(p.tokens, 1)}</td>
          <td class="font-mono text-slate-400">$${fmt(p.invested)}</td>
          <td>${pnlFmt(p.pnl_usd)} <span class="text-slate-600 text-xs">(${fmt(p.pnl_pct,1)}%)</span></td>
          <td class="text-cyan-400 font-mono">$${fmt(p.potential_pnl)}</td>
          <td class="${p.edge_entry > 0 ? 'pup' : 'pdn'} font-mono">${fmt(p.edge_entry, 1)}%</td>
          <td class="text-violet-400 font-mono">${fmt(p.p_real, 4)}</td>
          <td class="text-slate-500 font-mono">${fmt(p.runtime_min, 1)}m</td>
        </tr>`;
      }).join('');
    } else {
      $('posEmpty').style.display = '';
    }

    // Live Log
    const ll = $('liveLog');
    ll.innerHTML = logData.map(l => {
      let color = 'text-slate-400';
      if (l.includes('BUY')) color = 'text-green-400';
      else if (l.includes('CLOSE') || l.includes('SELL')) color = 'text-amber-400';
      else if (l.includes('SIGNAL')) color = 'text-cyan-400';
      else if (l.includes('SKIP') || l.includes('MISS')) color = 'text-slate-600';
      else if (l.includes('RESOLVED')) color = 'text-violet-400';
      else if (l.includes('REGIME SHIELD')) color = 'text-rose-400';
      // Inline YES/NO highlighting within the log line text
      const hl = l
        .replace(/\bYES\b/g, '<span style="color:#22d3ee;font-weight:700">YES</span>')
        .replace(/\bNO\b/g,  '<span style="color:#fb923c;font-weight:700">NO</span>');
      return `<div class="${color}">${hl}</div>`;
    }).join('');
    ll.scrollTop = ll.scrollHeight;

    // Brackets
    const bt = $('bktTable'); bt.innerHTML='';
    if (bkts.length) {
      $('bktEmpty').style.display='none';
      bt.innerHTML = bkts.map(b => {
        const yEdgeCls = b.yes_edge == null ? '' : b.yes_edge > 0 ? 'pup font-semibold' : 'pdn';
        const nEdgeCls = b.no_edge == null ? '' : b.no_edge > 0 ? 'pup font-semibold' : 'pdn';
        const ageCls = b.book_age > 20 ? 'text-amber-500' : 'text-slate-600';
        return `<tr>
          <td class="font-mono font-bold text-slate-200">$${b.strike.toLocaleString()}</td>
          <td class="text-xs text-slate-500 max-w-[140px] truncate">${b.event||''}</td>
          <td class="font-mono text-cyan-200">${fmt(b.y_bid, 4)}</td>
          <td class="font-mono text-cyan-200">${fmt(b.y_ask, 4)}</td>
          <td class="font-mono text-amber-200">${fmt(b.n_bid, 4)}</td>
          <td class="font-mono text-amber-200">${fmt(b.n_ask, 4)}</td>
          <td class="text-slate-400">${b.spread_pct != null ? fmt(b.spread_pct,1)+'%' : '—'}</td>
          <td class="text-violet-400 font-mono">${b.p_real != null ? fmt(b.p_real,4) : '—'}</td>
          <td class="text-violet-400 font-mono">${b.p_no != null ? fmt(b.p_no,4) : '—'}</td>
          <td class="font-mono ${yEdgeCls}">${b.yes_edge != null ? (b.yes_edge>0?'+':'')+fmt(b.yes_edge,1)+'%' : '—'}</td>
          <td class="font-mono ${nEdgeCls}">${b.no_edge != null ? (b.no_edge>0?'+':'')+fmt(b.no_edge,1)+'%' : '—'}</td>
          <td class="text-slate-400">${b.iv_pct != null ? fmt(b.iv_pct,1)+'%' : '—'}</td>
          <td class="${ageCls} text-xs">${b.book_age != null ? b.book_age+'s' : '—'}</td>
        </tr>`;
      }).join('');
    } else $('bktEmpty').style.display='';

    // History
    const ht = $('histTable'); ht.innerHTML='';
    const rows = [...hist].reverse();
    if (rows.length) {
      $('histEmpty').style.display='none';
      ht.innerHTML = rows.map(h => {
        const isOpen = h.event_type === 'OPEN';
        const pnl = parseFloat(h.realized_pnl_usd || 0);
        return `<tr>
          <td class="text-slate-500 text-xs font-mono whitespace-nowrap">${h.timestamp ? (() => { const d = new Date(h.timestamp); return d.toLocaleDateString('en-GB', {timeZone:'Asia/Bangkok', day:'2-digit', month:'short'}) + ' ' + d.toLocaleTimeString('en-GB', {timeZone:'Asia/Bangkok'}); })() : '—'}</td>
          <td class="text-xs max-w-[120px] truncate text-slate-400">${h.event_name||''}</td>
          <td>${isOpen
            ? '<span style="color:#60a5fa;font-size:10px;background:rgba(96,165,250,.1);padding:1px 6px;border-radius:4px">OPEN</span>'
            : '<span style="color:#fb923c;font-size:10px;background:rgba(251,146,60,.1);padding:1px 6px;border-radius:4px">CLOSE</span>'}</td>
          <td>${(h.side||'') === 'YES'
            ? '<span style="color:#22d3ee;font-size:10px;background:rgba(34,211,238,.12);padding:1px 7px;border-radius:4px;font-weight:700">YES</span>'
            : (h.side||'') === 'NO'
              ? '<span style="color:#fb923c;font-size:10px;background:rgba(251,146,60,.12);padding:1px 7px;border-radius:4px;font-weight:700">NO</span>'
              : '<span class="text-slate-600">—</span>'
          }</td>
          <td class="font-mono text-slate-300">$${Number(h.strike||0).toLocaleString()}</td>
          <td class="font-mono text-slate-400">${h.tokens||'—'}</td>
          <td class="font-mono">${h.entry_price||'—'}</td>
          <td class="font-mono">${h.exit_price||'—'}</td>
          <td>${isOpen ? '<span class="text-slate-600">—</span>' : pnlFmt(pnl)}</td>
          <td>${isOpen ? '<span class="text-slate-600">—</span>' : pnlFmt(h.roi_pct||0,'%')}</td>
          <td class="text-xs text-slate-500 max-w-[140px] truncate">${h.close_reason||h.entry_reason||''}</td>
          <td class="font-mono text-slate-600 text-xs">${h.runtime_min ? h.runtime_min+'m' : '—'}</td>
        </tr>`;
      }).join('');
    } else $('histEmpty').style.display='';

  } catch(e) { console.error('Poll error:', e); }
}
setInterval(poll, 2000);
poll();
</script>
</body></html>"""
