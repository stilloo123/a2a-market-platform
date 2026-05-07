import asyncio
import os
import sys
from contextlib import asynccontextmanager

import httpx
import uvicorn
import yaml
from dotenv import load_dotenv
from fastapi import FastAPI, Query

from observer.rankings import network_summary, rank_markets, rank_traders
from shared.a2a import get_a2a_agent_card, get_markets, get_traders, get_schedule, get_stats
from shared.models import A2AAgentCard, AgentCapabilities, StatsResponse


def load_config(path: str) -> dict:
    load_dotenv()
    cfg = yaml.safe_load(open(path))
    if "registry_url" in cfg and "registry_urls" not in cfg:
        cfg["registry_urls"] = [cfg["registry_url"]]
    if (v := os.getenv("REGISTRY_URLS")) is not None:
        cfg["registry_urls"] = [u.strip() for u in v.split(",") if u.strip()]
    if (v := os.getenv("POLL_INTERVAL_SECONDS")) is not None:
        cfg["poll_interval_seconds"] = float(v)
    return cfg


def build_app(cfg: dict) -> FastAPI:
    _cache: dict = {"stats": [], "schedule": [], "agent_urls": set()}
    registry_urls: list[str] = cfg["registry_urls"]

    async def _collect() -> list[StatsResponse]:
        all_stats: list[StatsResponse] = []
        try:
            markets = await get_markets(registry_urls)
            traders = await get_traders(registry_urls)
            urls = [e["url"] for e in markets + traders]
        except Exception as exc:
            print(f"[observer] registry error: {exc}")
            return all_stats

        _cache["agent_urls"] = set(urls)

        async def fetch(url: str):
            try:
                s = await get_stats(url)
                all_stats.append(s)
            except Exception:
                pass

        await asyncio.gather(*[fetch(u) for u in urls])
        return all_stats

    async def _collect_schedule() -> list[dict]:
        """Fetch upcoming runs from all market AgentCards."""
        runs: list[dict] = []
        try:
            markets = await get_markets(registry_urls)
        except Exception:
            return runs
        async def fetch(entry: dict):
            try:
                card = await get_a2a_agent_card(entry["url"])
                state = await get_schedule(entry["url"])
                for run in state.get("scheduled_runs", []):
                    runs.append({
                        "market_name": card.name,
                        "market_url": entry["url"],
                        "run_id": run["run_id"],
                        "game_name": run["game_name"],
                        "scheduled_at": run["scheduled_at"],
                        "bet_open_at": run["bet_open_at"],
                        "status": run["status"],
                        "total_bets": run["total_bets"],
                        "total_wagered": run["total_wagered"],
                    })
            except Exception:
                pass
        await asyncio.gather(*[fetch(e) for e in markets])
        runs.sort(key=lambda r: r["scheduled_at"])
        return runs

    async def _poll_loop():
        while True:
            stats = await _collect()
            schedule = await _collect_schedule()
            _cache["stats"] = stats
            _cache["schedule"] = schedule
            await asyncio.sleep(cfg.get("poll_interval_seconds", 30))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        asyncio.create_task(_poll_loop())
        yield

    app = FastAPI(title="A2A Market Platform Observer", lifespan=lifespan)

    @app.get("/.well-known/agent-card.json")
    async def a2a_agent_card():
        card = A2AAgentCard(
            name="Observer",
            description="A2A Gambling network observer — rankings, schedule, and stats aggregator.",
            version="1.0",
            supported_interfaces=[],
            capabilities=AgentCapabilities(),
            skills=[],
        )
        return card.model_dump(by_alias=True, mode="json")

    @app.get("/rankings/markets")
    async def rankings_markets():
        return [s.model_dump() for s in rank_markets(_cache["stats"])]

    @app.get("/rankings/traders")
    async def rankings_traders():
        return [s.model_dump() for s in rank_traders(_cache["stats"])]

    @app.get("/network")
    async def network():
        return network_summary(_cache["stats"])

    @app.get("/schedule")
    async def schedule():
        return _cache["schedule"]

    @app.get("/agents")
    async def agents():
        """Live agent list for client-side thought discovery."""
        try:
            markets = await get_markets(registry_urls)
            traders = await get_traders(registry_urls)
        except Exception:
            markets, traders = [], []
        return {
            "markets": [{"url": e["url"], "name": e.get("name", e["url"])} for e in markets],
            "traders": [{"url": e["url"], "name": e.get("name", e["url"])} for e in traders],
        }

    @app.get("/proxy/thoughts")
    async def proxy_thoughts(url: str = Query(...), n: int = 20):
        """Proxy agent /thoughts to avoid browser CORS blocks."""
        from fastapi import HTTPException
        if url not in _cache["agent_urls"]:
            raise HTTPException(status_code=403, detail="URL not in known agent list")
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{url}/thoughts", params={"n": n})
            return resp.json()

    @app.get("/", response_class=None)
    async def dashboard_html():
        from fastapi.responses import HTMLResponse
        markets_list, traders_list = [], []
        market_thought_urls = []
        trader_thought_urls = []
        refresh = cfg.get("poll_interval_seconds", 30) * 1000
        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>A2A Market Platform</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    :root {{
      --bg: #0a0a0f;
      --sidebar: #13131a;
      --card: #1f1f2e;
      --border: #2a2a3a;
      --text: #e4e4e7;
      --muted: #a1a1aa;
      --accent: #6366f1;
      --glow: rgba(99,102,241,0.2);
      --green: #10b981;
      --red: #ef4444;
      --yellow: #f59e0b;
      --blue: #3b82f6;
    }}
    body {{
      font-family: 'Inter', sans-serif;
      background: var(--bg);
      color: var(--text);
      display: flex;
      min-height: 100vh;
    }}

    /* ── Sidebar ── */
    .sidebar {{
      width: 68px;
      background: var(--sidebar);
      border-right: 1px solid var(--border);
      display: flex;
      flex-direction: column;
      align-items: flex-start;
      padding: 20px 0;
      position: fixed;
      top: 0; left: 0; bottom: 0;
      z-index: 100;
      overflow: hidden;
      transition: width 0.22s cubic-bezier(0.4,0,0.2,1);
    }}
    .sidebar:hover {{ width: 220px; }}
    .logo {{
      display: flex;
      align-items: center;
      gap: 10px;
      padding: 4px 20px 28px;
      white-space: nowrap;
    }}
    .logo-icon {{ font-size: 1.4em; }}
    .logo-text {{ font-size: 0.88em; font-weight: 600; color: var(--text); opacity: 0; transition: opacity 0.15s; }}
    .sidebar:hover .logo-text {{ opacity: 1; }}
    .nav-item {{
      display: flex;
      align-items: center;
      gap: 12px;
      width: 100%;
      padding: 11px 22px;
      cursor: pointer;
      color: var(--muted);
      font-size: 0.84em;
      font-weight: 500;
      white-space: nowrap;
      transition: color 0.15s, background 0.15s;
      border-left: 3px solid transparent;
    }}
    .nav-item:hover {{ color: var(--text); background: rgba(99,102,241,0.08); }}
    .nav-item.active {{ color: var(--accent); border-left-color: var(--accent); background: rgba(99,102,241,0.12); }}
    .nav-icon {{ font-size: 1.05em; flex-shrink: 0; width: 22px; text-align: center; }}
    .nav-label {{ opacity: 0; transition: opacity 0.12s; }}
    .sidebar:hover .nav-label {{ opacity: 1; }}

    /* ── Main ── */
    .main {{
      margin-left: 68px;
      flex: 1;
      padding: 28px 32px;
    }}
    .page-header {{
      display: flex;
      align-items: baseline;
      gap: 14px;
      margin-bottom: 6px;
    }}
    h1 {{ font-size: 1.4em; font-weight: 600; }}
    .subtitle {{ color: var(--muted); font-size: 0.8em; }}
    .updated {{
      font-family: 'JetBrains Mono', monospace;
      font-size: 0.72em;
      color: var(--muted);
      margin-bottom: 26px;
    }}

    /* ── Sections ── */
    .section {{ display: none; }}
    .section.active {{ display: block; }}

    /* ── KPI cards ── */
    .kpi-grid {{ display: flex; gap: 14px; flex-wrap: wrap; margin-bottom: 26px; }}
    .kpi-card {{
      background: var(--card);
      background-image: linear-gradient(135deg, rgba(99,102,241,0.06), transparent);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 18px 22px;
      min-width: 140px;
      flex: 1;
      transition: border-color 0.2s, box-shadow 0.2s;
    }}
    .kpi-card:hover {{
      border-color: var(--accent);
      box-shadow: 0 0 0 1px var(--accent), 0 0 14px var(--glow);
    }}
    .kpi-label {{
      font-size: 0.68em;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: var(--muted);
      margin-bottom: 10px;
    }}
    .kpi-value {{
      font-family: 'JetBrains Mono', monospace;
      font-size: 1.85em;
      font-weight: 500;
      color: var(--accent);
      line-height: 1;
    }}

    /* ── Panels ── */
    .grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 18px; margin-bottom: 26px; }}
    @media (max-width: 900px) {{ .grid {{ grid-template-columns: 1fr; }} }}
    .panel {{
      background: var(--card);
      background-image: linear-gradient(135deg, rgba(99,102,241,0.04), transparent);
      border: 1px solid var(--border);
      border-radius: 10px;
      overflow: hidden;
      transition: border-color 0.2s, box-shadow 0.2s;
    }}
    .panel:hover {{
      border-color: rgba(99,102,241,0.4);
      box-shadow: 0 0 18px rgba(99,102,241,0.1);
    }}
    .panel-header {{
      display: flex;
      align-items: center;
      gap: 8px;
      padding: 13px 18px;
      border-bottom: 1px solid var(--border);
    }}
    .panel-icon {{ font-size: 0.9em; }}
    .panel-header h2 {{
      font-size: 0.75em;
      font-weight: 600;
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: 0.07em;
    }}

    /* ── Tables ── */
    table {{ width: 100%; border-collapse: collapse; }}
    th {{
      padding: 9px 16px;
      text-align: left;
      font-size: 0.66em;
      font-weight: 600;
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: 0.06em;
      border-bottom: 1px solid var(--border);
    }}
    td {{
      padding: 10px 16px;
      font-size: 0.81em;
      font-family: 'JetBrains Mono', monospace;
      border-bottom: 1px solid rgba(42,42,58,0.5);
    }}
    tr:last-child td {{ border-bottom: none; }}
    tr:hover td {{ background: rgba(99,102,241,0.04); }}
    .rank-badge {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 22px; height: 22px;
      background: rgba(99,102,241,0.15);
      color: var(--accent);
      border-radius: 6px;
      font-size: 0.75em;
      font-weight: 600;
    }}
    .name-cell {{ font-family: 'Inter', sans-serif; font-weight: 500; color: var(--text); }}
    .pos {{ color: var(--green); }}
    .neg {{ color: var(--red); }}

    /* ── Thought feed ── */
    .thoughts-panel {{
      background: var(--card);
      background-image: linear-gradient(135deg, rgba(99,102,241,0.04), transparent);
      border: 1px solid var(--border);
      border-radius: 10px;
      margin-bottom: 18px;
      overflow: hidden;
    }}
    .thought {{
      padding: 13px 18px;
      border-bottom: 1px solid rgba(42,42,58,0.5);
      border-left: 3px solid transparent;
      transition: background 0.15s;
    }}
    .thought:last-child {{ border-bottom: none; }}
    .thought:hover {{ background: rgba(99,102,241,0.04); }}
    .thought.ev-GAME_DESIGN  {{ border-left-color: var(--accent); }}
    .thought.ev-ADAPTATION   {{ border-left-color: var(--yellow); }}
    .thought.ev-BID_DECISION {{ border-left-color: var(--blue); }}
    .thought.ev-BID_RESULT   {{ border-left-color: var(--green); }}
    .thought.ev-RUN_RESOLVED {{ border-left-color: #a855f7; }}
    .thought-meta {{ display: flex; align-items: center; gap: 10px; margin-bottom: 7px; }}
    .event-badge {{
      display: inline-block;
      padding: 2px 8px;
      border-radius: 5px;
      font-size: 0.64em;
      font-weight: 600;
      letter-spacing: 0.05em;
      text-transform: uppercase;
      font-family: 'JetBrains Mono', monospace;
    }}
    .badge-GAME_DESIGN  {{ background: rgba(99,102,241,0.18); color: var(--accent); }}
    .badge-ADAPTATION   {{ background: rgba(245,158,11,0.18);  color: var(--yellow); }}
    .badge-BID_DECISION {{ background: rgba(59,130,246,0.18);  color: var(--blue); }}
    .badge-BID_RESULT   {{ background: rgba(16,185,129,0.18);  color: var(--green); }}
    .badge-RUN_RESOLVED {{ background: rgba(168,85,247,0.18);  color: #a855f7; }}
    .thought-time {{ font-family: 'JetBrains Mono', monospace; font-size: 0.7em; color: var(--muted); }}
    .thought-text {{ color: var(--text); font-size: 0.82em; line-height: 1.6; }}
    .empty {{ padding: 22px 18px; color: var(--muted); font-size: 0.84em; text-align: center; }}

    /* ── Schedule ── */
    .run-row {{
      display: flex;
      align-items: center;
      gap: 14px;
      padding: 12px 18px;
      border-bottom: 1px solid rgba(42,42,58,0.5);
      font-size: 0.82em;
    }}
    .run-row:last-child {{ border-bottom: none; }}
    .run-row:hover {{ background: rgba(99,102,241,0.04); }}
    .run-time {{ font-family: 'JetBrains Mono', monospace; color: var(--muted); min-width: 60px; }}
    .run-game {{ flex: 1; font-weight: 500; }}
    .run-market {{ color: var(--muted); font-size: 0.9em; }}
    .run-edge {{ font-family: 'JetBrains Mono', monospace; color: var(--accent); min-width: 50px; text-align: right; }}
    .status-pill {{
      display: inline-block;
      padding: 2px 9px;
      border-radius: 5px;
      font-size: 0.68em;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      font-family: 'JetBrains Mono', monospace;
    }}
    .status-open      {{ background: rgba(16,185,129,0.18); color: var(--green); }}
    .status-scheduled {{ background: rgba(99,102,241,0.15); color: var(--accent); }}
    .status-closed    {{ background: rgba(161,161,170,0.15); color: var(--muted); }}
    .status-resolved  {{ background: rgba(168,85,247,0.15); color: #a855f7; }}
  </style>
</head>
<body>

<nav class="sidebar">
  <div class="logo">
    <span class="logo-icon">🎰</span>
    <span class="logo-text">A2A Market</span>
  </div>
  <div class="nav-item active" onclick="showSection('overview')">
    <span class="nav-icon">📊</span><span class="nav-label">Overview</span>
  </div>
  <div class="nav-item" onclick="showSection('markets')">
    <span class="nav-icon">🏛️</span><span class="nav-label">Markets</span>
  </div>
  <div class="nav-item" onclick="showSection('traders')">
    <span class="nav-icon">🎲</span><span class="nav-label">Traders</span>
  </div>
  <div class="nav-item" onclick="showSection('schedule')">
    <span class="nav-icon">🗓️</span><span class="nav-label">Schedule</span>
  </div>
  <div class="nav-item" onclick="showSection('thoughts')">
    <span class="nav-icon">💭</span><span class="nav-label">Thoughts</span>
  </div>
</nav>

<main class="main">
  <div class="page-header">
    <h1>A2A Market Platform</h1>
    <span class="subtitle">Observer · refreshes every {cfg.get("poll_interval_seconds", 30)}s</span>
  </div>
  <p class="updated" id="updated">loading…</p>

  <!-- Overview -->
  <div class="section active" id="section-overview">
    <div class="kpi-grid" id="kpi-grid"></div>
    <div class="grid">
      <div class="panel">
        <div class="panel-header"><span class="panel-icon">🏛️</span><h2>Top Markets</h2></div>
        <table>
          <thead><tr><th>#</th><th>Name</th><th>Balance</th><th>Profit</th><th>Games</th><th>Bids</th></tr></thead>
          <tbody id="market-rows"></tbody>
        </table>
      </div>
      <div class="panel">
        <div class="panel-header"><span class="panel-icon">🎲</span><h2>Top Traders</h2></div>
        <table>
          <thead><tr><th>#</th><th>Name</th><th>Balance</th><th>Profit</th><th>ROI</th><th>Bids</th></tr></thead>
          <tbody id="trader-rows"></tbody>
        </table>
      </div>
    </div>
    <div id="thoughts-preview"></div>
  </div>

  <!-- Markets -->
  <div class="section" id="section-markets">
    <div class="panel" style="margin-bottom:20px">
      <div class="panel-header"><span class="panel-icon">🏛️</span><h2>All Markets — Ranked by Profit</h2></div>
      <table>
        <thead><tr><th>#</th><th>Name</th><th>Balance</th><th>Seed</th><th>Profit</th><th>Games</th><th>Bids</th><th>Wagered</th></tr></thead>
        <tbody id="market-full-rows"></tbody>
      </table>
    </div>
  </div>

  <!-- Traders -->
  <div class="section" id="section-traders">
    <div class="panel" style="margin-bottom:20px">
      <div class="panel-header"><span class="panel-icon">🎲</span><h2>All Traders — Ranked by ROI</h2></div>
      <table>
        <thead><tr><th>#</th><th>Name</th><th>Balance</th><th>Seed</th><th>Profit</th><th>ROI</th><th>Bids</th><th>Wagered</th></tr></thead>
        <tbody id="trader-full-rows"></tbody>
      </table>
    </div>
  </div>

  <!-- Schedule -->
  <div class="section" id="section-schedule">
    <div class="panel" style="margin-bottom:20px">
      <div class="panel-header"><span class="panel-icon">🗓️</span><h2>Upcoming Runs — All Markets</h2></div>
      <div id="schedule-rows"><div class="empty">Loading schedule…</div></div>
    </div>
  </div>

  <!-- Thoughts -->
  <div class="section" id="section-thoughts">
    <div id="thoughts-full"></div>
  </div>
</main>

<script>
const marketThoughtUrls = [{",".join(market_thought_urls)}];
const traderThoughtUrls = [{",".join(trader_thought_urls)}];

const NAV_KEYS = ['overview','markets','traders','schedule','thoughts'];

function showSection(name) {{
  document.querySelectorAll('.section').forEach(s => s.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  document.getElementById('section-' + name).classList.add('active');
  document.querySelectorAll('.nav-item')[NAV_KEYS.indexOf(name)].classList.add('active');
}}

function money(v) {{ return '$' + Number(v).toFixed(2); }}
function sign(v, fmt) {{
  return `<span class="${{v >= 0 ? 'pos' : 'neg'}}">${{v >= 0 ? '+' : ''}}${{fmt(v)}}</span>`;
}}
function rankBadge(i) {{ return `<span class="rank-badge">${{i+1}}</span>`; }}
function badge(ev) {{
  return `<span class="event-badge badge-${{ev}}">${{ev.replace(/_/g,' ')}}</span>`;
}}

async function loadNetwork() {{
  try {{
    const d = await fetch('/network').then(r => r.json());
    document.getElementById('kpi-grid').innerHTML = [
      ['Markets', d.total_markets],
      ['Traders', d.total_traders],
      ['Volume', money(d.total_volume)],
      ['Payouts', money(d.total_payouts)],
      ['House Profit', money(d.total_house_profit)],
      ['Trader Profit', money(d.total_trader_profit)],
    ].map(([l,v]) => `<div class="kpi-card"><div class="kpi-label">${{l}}</div><div class="kpi-value">${{v}}</div></div>`).join('');
  }} catch {{}}
}}

async function loadMarkets() {{
  try {{
    const d = await fetch('/rankings/markets').then(r => r.json());
    document.getElementById('market-rows').innerHTML = d.map((c,i) => `<tr>
      <td>${{rankBadge(i)}}</td><td class="name-cell">${{c.name}}</td>
      <td>${{money(c.balance)}}</td><td>${{sign(c.profit, money)}}</td>
      <td>${{c.active_games ?? '—'}}</td><td>${{c.total_bets}}</td></tr>`).join('');
    document.getElementById('market-full-rows').innerHTML = d.map((c,i) => `<tr>
      <td>${{rankBadge(i)}}</td><td class="name-cell">${{c.name}}</td>
      <td>${{money(c.balance)}}</td><td>${{money(c.seed_balance)}}</td>
      <td>${{sign(c.profit, money)}}</td><td>${{c.active_games ?? '—'}}</td>
      <td>${{c.total_bets}}</td><td>${{money(c.total_wagered)}}</td></tr>`).join('');
  }} catch {{}}
}}

async function loadTraders() {{
  try {{
    const d = await fetch('/rankings/traders').then(r => r.json());
    const roi = p => p.seed_balance ? (p.profit / p.seed_balance * 100) : 0;
    document.getElementById('trader-rows').innerHTML = d.map((p,i) => `<tr>
      <td>${{rankBadge(i)}}</td><td class="name-cell">${{p.name}}</td>
      <td>${{money(p.balance)}}</td><td>${{sign(p.profit, money)}}</td>
      <td>${{sign(roi(p), v => v.toFixed(1)+'%')}}</td><td>${{p.total_bets}}</td></tr>`).join('');
    document.getElementById('trader-full-rows').innerHTML = d.map((p,i) => `<tr>
      <td>${{rankBadge(i)}}</td><td class="name-cell">${{p.name}}</td>
      <td>${{money(p.balance)}}</td><td>${{money(p.seed_balance)}}</td>
      <td>${{sign(p.profit, money)}}</td><td>${{sign(roi(p), v => v.toFixed(1)+'%')}}</td>
      <td>${{p.total_bets}}</td><td>${{money(p.total_wagered)}}</td></tr>`).join('');
  }} catch {{}}
}}

function thoughtCard(t) {{
  return `<div class="thought ev-${{t.event}}">
    <div class="thought-meta">${{badge(t.event)}}<span class="thought-time">${{new Date(t.ts).toLocaleTimeString()}}</span></div>
    <div class="thought-text">${{t.reasoning}}</div>
  </div>`;
}}

async function loadThoughts() {{
  let agentList;
  try {{
    agentList = await fetch('/agents').then(r => r.json());
  }} catch {{ agentList = {{markets: [], traders: []}}; }}
  const allAgents = [
    ...agentList.markets.map(a => [a.url + '/thoughts', a.name, 'market']),
    ...agentList.traders.map(a => [a.url + '/thoughts', a.name, 'trader']),
  ];
  const panels = await Promise.all(allAgents.map(async ([url, name, type]) => {{
    try {{
      const proxyUrl = '/proxy/thoughts?url=' + encodeURIComponent(url.replace('/thoughts','')) + '&n=20';
      const thoughts = await fetch(proxyUrl).then(r => r.json());
      const icon = type === 'market' ? '🏛️' : '🎲';
      const rows = thoughts.slice().reverse().map(thoughtCard).join('');
      return `<div class="thoughts-panel">
        <div class="panel-header"><span class="panel-icon">${{icon}}</span><h2>${{name}} — Thoughts</h2></div>
        ${{rows || '<div class="empty">No thoughts yet…</div>'}}
      </div>`;
    }} catch {{ return ''; }}
  }}));
  const html = panels.filter(Boolean).join('') || '<div class="empty">No agents connected yet.</div>';
  const preview = document.getElementById('thoughts-preview');
  const full = document.getElementById('thoughts-full');
  if (preview) preview.innerHTML = html;
  if (full) full.innerHTML = html;
}}

async function loadSchedule() {{
  try {{
    const runs = await fetch('/schedule').then(r => r.json());
    if (!runs.length) {{
      document.getElementById('schedule-rows').innerHTML = '<div class="empty">No upcoming runs yet.</div>';
      return;
    }}
    document.getElementById('schedule-rows').innerHTML = runs.map(r => {{
      const at = new Date(r.scheduled_at);
      const open = new Date(r.bet_open_at);
      const now = new Date();
      const secsToOpen = Math.max(0, (open - now) / 1000);
      const secsToRun = Math.max(0, (at - now) / 1000);
      const countdown = r.status === 'open'
        ? `closes in ${{Math.floor(secsToRun/60)}}m ${{Math.floor(secsToRun%60)}}s`
        : r.status === 'scheduled'
          ? `opens in ${{Math.floor(secsToOpen/60)}}m ${{Math.floor(secsToOpen%60)}}s`
          : r.status;
      const betsInfo = r.total_bets > 0
        ? `${{r.total_bets}} bet${{r.total_bets !== 1 ? 's' : ''}} · ${{money(r.total_wagered)}} wagered`
        : 'no bets yet';
      return `<div class="run-row">
        <span class="run-time">${{at.toLocaleTimeString([], {{hour:'2-digit',minute:'2-digit'}})}}</span>
        <span class="run-game">${{r.game_name}}<span class="run-market"> @ ${{r.market_name}}</span></span>
        <span style="color:var(--muted);font-size:0.8em">${{betsInfo}}</span>
        <span class="status-pill status-${{r.status}}">${{countdown}}</span>
      </div>`;
    }}).join('');
  }} catch {{}}
}}

refresh();
loadThoughts();
loadSchedule();
setInterval(refresh, {refresh});
setInterval(loadThoughts, 10000);
setInterval(loadSchedule, 10000);

async function refresh() {{
  await Promise.all([loadNetwork(), loadMarkets(), loadTraders()]);
  document.getElementById('updated').textContent = 'Last updated: ' + new Date().toLocaleTimeString();
}}
</script>
</body></html>"""
        return HTMLResponse(html)

    return app


if __name__ == "__main__":
    cfg_path = "observer/config.yaml"
    if len(sys.argv) > 1:
        cfg_path = sys.argv[1]
    cfg = load_config(cfg_path)
    app = build_app(cfg)
    uvicorn.run(app, host="0.0.0.0", port=cfg["port"])
