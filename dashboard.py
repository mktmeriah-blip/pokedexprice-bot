"""
Pokédex Price Dashboard — Flask web app
Reads from pokemon_inventory.db (same DB as bot.py)
Run:  python dashboard.py
Then: cloudflared tunnel --url http://localhost:5000
"""

import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, render_template_string, request

# ── Config ────────────────────────────────────────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=Path(__file__).parent / ".env", override=True)
except ImportError:
    pass

DB_PATH      = os.getenv("DB_PATH", "pokemon_inventory.db")
EXCHANGE_RATE = int(os.getenv("EXCHANGE_RATE", 16000))
PORT          = int(os.getenv("DASHBOARD_PORT", 5000))

app = Flask(__name__)

# ── DB helpers ────────────────────────────────────────────────────────────────
def get_db():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    return db


def q(sql, params=()):
    with get_db() as db:
        cur = db.execute(sql, params)
        return cur.fetchall()


def q1(sql, params=()):
    with get_db() as db:
        cur = db.execute(sql, params)
        row = cur.fetchone()
        return row


# ── Template ──────────────────────────────────────────────────────────────────
HTML = r"""<!doctype html>
<html lang="id">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PokéDex Price</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Fira+Mono:wght@400;500&display=swap" rel="stylesheet">
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
/* ── Tokens ──────────────────────────────────────────────────────── */
:root {
  --bg:        #f5f6fa;
  --surface:   #ffffff;
  --border:    #e2e6ee;
  --fg:        #1a1d2e;
  --fg2:       #5a6180;
  --fg3:       #8a92b0;
  --accent:    #4361ee;
  --accent2:   #3a0ca3;
  --green:     #2dc653;
  --green-bg:  #eaf9ee;
  --red:       #e63946;
  --red-bg:    #fff0f0;
  --yellow:    #f4a261;
  --yellow-bg: #fff8ee;
  --poke-red:  #e63946;
  --poke-blue: #4361ee;
  --row-alt:   #f9fafd;
  --shadow:    0 1px 3px rgba(0,0,0,.08), 0 1px 2px rgba(0,0,0,.04);
  --shadow-lg: 0 4px 16px rgba(0,0,0,.10);
  color-scheme: light;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg:        #0f1117;
    --surface:   #1a1d2e;
    --border:    #2a2e45;
    --fg:        #e8eaf6;
    --fg2:       #9ea8d0;
    --fg3:       #6270a0;
    --accent:    #738bff;
    --accent2:   #b388ff;
    --green:     #4ade80;
    --green-bg:  #0f2a1a;
    --red:       #fc5c65;
    --red-bg:    #2a0f12;
    --yellow:    #fbbf24;
    --yellow-bg: #2a1f0a;
    --row-alt:   #1e2235;
    --shadow:    0 1px 3px rgba(0,0,0,.3);
    --shadow-lg: 0 4px 16px rgba(0,0,0,.4);
    color-scheme: dark;
  }
}
:root[data-theme="dark"] {
  --bg:        #0f1117;
  --surface:   #1a1d2e;
  --border:    #2a2e45;
  --fg:        #e8eaf6;
  --fg2:       #9ea8d0;
  --fg3:       #6270a0;
  --accent:    #738bff;
  --accent2:   #b388ff;
  --green:     #4ade80;
  --green-bg:  #0f2a1a;
  --red:       #fc5c65;
  --red-bg:    #2a0f12;
  --yellow:    #fbbf24;
  --yellow-bg: #2a1f0a;
  --row-alt:   #1e2235;
  --shadow:    0 1px 3px rgba(0,0,0,.3);
  --shadow-lg: 0 4px 16px rgba(0,0,0,.4);
  color-scheme: dark;
}

/* ── Reset / Base ────────────────────────────────────────────────── */
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
html, body { height: 100%; }
body {
  font-family: 'Inter', system-ui, sans-serif;
  font-size: 14px;
  background: var(--bg);
  color: var(--fg);
  line-height: 1.5;
}
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
img { max-width: 100%; }

/* ── Layout ──────────────────────────────────────────────────────── */
.app-shell {
  display: flex;
  min-height: 100%;
}
.sidebar {
  width: 220px;
  flex-shrink: 0;
  background: var(--fg);
  color: var(--bg);
  display: flex;
  flex-direction: column;
  position: sticky;
  top: 0;
  height: 100vh;
  overflow-y: auto;
}
.main {
  flex: 1;
  min-width: 0;
  padding: 24px;
  overflow-x: hidden;
}
@media (max-width: 768px) {
  .app-shell { flex-direction: column; }
  .sidebar {
    width: 100%;
    height: auto;
    position: static;
    flex-direction: row;
    flex-wrap: wrap;
    padding: 8px 16px;
    gap: 8px;
  }
  .sidebar .logo { margin-bottom: 0; padding: 0; border-bottom: none; }
  .sidebar .nav-section { display: flex; gap: 4px; flex-wrap: wrap; }
  .sidebar .nav-section h4 { display: none; }
  .main { padding: 16px; }
}

/* ── Sidebar ─────────────────────────────────────────────────────── */
.logo {
  padding: 20px 16px 16px;
  border-bottom: 1px solid rgba(255,255,255,.08);
  margin-bottom: 8px;
}
.logo-title {
  font-size: 16px;
  font-weight: 700;
  letter-spacing: -.3px;
  color: #fff;
  display: flex;
  align-items: center;
  gap: 6px;
}
.logo-ball {
  width: 22px; height: 22px;
  background: conic-gradient(#e63946 0deg 180deg, #fff 180deg 184deg, #1a1d2e 184deg 360deg);
  border-radius: 50%;
  border: 2px solid rgba(255,255,255,.3);
  flex-shrink: 0;
}
.logo-sub { font-size: 11px; color: rgba(255,255,255,.4); margin-top: 2px; letter-spacing: .3px; }
.nav-section { padding: 0 8px 12px; }
.nav-section h4 {
  font-size: 10px;
  font-weight: 600;
  letter-spacing: .8px;
  text-transform: uppercase;
  color: rgba(255,255,255,.3);
  padding: 10px 8px 4px;
}
.nav-link {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 7px 8px;
  border-radius: 6px;
  color: rgba(255,255,255,.7);
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
  transition: background .15s, color .15s;
}
.nav-link:hover, .nav-link.active {
  background: rgba(255,255,255,.1);
  color: #fff;
  text-decoration: none;
}
.nav-link .icon { font-size: 15px; width: 18px; text-align: center; }
.nav-count {
  margin-left: auto;
  background: rgba(255,255,255,.15);
  border-radius: 20px;
  font-size: 11px;
  padding: 1px 6px;
}
.theme-toggle {
  margin-top: auto;
  padding: 12px 16px;
  border-top: 1px solid rgba(255,255,255,.08);
}
.theme-btn {
  width: 100%;
  padding: 7px 12px;
  border-radius: 6px;
  border: 1px solid rgba(255,255,255,.15);
  background: transparent;
  color: rgba(255,255,255,.6);
  font-size: 12px;
  cursor: pointer;
  font-family: inherit;
}
.theme-btn:hover { background: rgba(255,255,255,.08); color: #fff; }

/* ── Section/Page ────────────────────────────────────────────────── */
.page { display: none; }
.page.active { display: block; }

/* ── Page header ─────────────────────────────────────────────────── */
.page-header {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 20px;
  flex-wrap: wrap;
}
.page-title { font-size: 20px; font-weight: 700; letter-spacing: -.3px; }
.page-sub { font-size: 13px; color: var(--fg2); margin-left: 4px; }

/* ── Stat grid ───────────────────────────────────────────────────── */
.stat-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(170px, 1fr));
  gap: 12px;
  margin-bottom: 24px;
}
.stat-card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 14px 16px;
  box-shadow: var(--shadow);
}
.stat-label {
  font-size: 11px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: .6px;
  color: var(--fg3);
  margin-bottom: 4px;
}
.stat-value {
  font-size: 22px;
  font-weight: 700;
  letter-spacing: -.5px;
  font-variant-numeric: tabular-nums;
  color: var(--fg);
}
.stat-sub {
  font-size: 11px;
  color: var(--fg3);
  margin-top: 2px;
  font-variant-numeric: tabular-nums;
}
.stat-value.green { color: var(--green); }
.stat-value.red   { color: var(--red); }
.stat-value.blue  { color: var(--accent); }

/* ── Card / Surface ──────────────────────────────────────────────── */
.card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 10px;
  box-shadow: var(--shadow);
  margin-bottom: 20px;
  overflow: hidden;
}
.card-header {
  padding: 14px 16px 12px;
  border-bottom: 1px solid var(--border);
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.card-title {
  font-size: 14px;
  font-weight: 600;
}
.card-body { padding: 16px; }
.card-body.no-pad { padding: 0; }

/* ── Search / Filter bar ─────────────────────────────────────────── */
.filter-bar {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  margin-left: auto;
}
.search-input {
  padding: 6px 12px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--bg);
  color: var(--fg);
  font-size: 13px;
  font-family: inherit;
  outline: none;
  width: 200px;
}
.search-input:focus { border-color: var(--accent); }
select.filter-select {
  padding: 6px 10px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--bg);
  color: var(--fg);
  font-size: 13px;
  font-family: inherit;
  outline: none;
  cursor: pointer;
}
select.filter-select:focus { border-color: var(--accent); }

/* ── Tables ──────────────────────────────────────────────────────── */
.table-wrap { overflow-x: auto; }
table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
  font-variant-numeric: tabular-nums;
}
thead th {
  padding: 10px 12px;
  text-align: left;
  font-size: 11px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: .5px;
  color: var(--fg3);
  background: var(--bg);
  border-bottom: 1px solid var(--border);
  white-space: nowrap;
  cursor: pointer;
  user-select: none;
}
thead th:hover { color: var(--fg2); }
thead th.sort-asc::after  { content: ' ↑'; color: var(--accent); }
thead th.sort-desc::after { content: ' ↓'; color: var(--accent); }
tbody tr {
  border-bottom: 1px solid var(--border);
  transition: background .1s;
}
tbody tr:nth-child(even) { background: var(--row-alt); }
tbody tr:hover { background: rgba(67,97,238,.06); }
tbody td { padding: 9px 12px; color: var(--fg); vertical-align: middle; }
.td-name {
  font-weight: 500;
  max-width: 220px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.td-set { color: var(--fg2); font-size: 12px; }
.td-num { text-align: right; }

/* ── Badges ──────────────────────────────────────────────────────── */
.badge {
  display: inline-block;
  padding: 2px 7px;
  border-radius: 20px;
  font-size: 11px;
  font-weight: 600;
  white-space: nowrap;
}
.badge-green  { background: var(--green-bg);  color: var(--green); }
.badge-red    { background: var(--red-bg);    color: var(--red); }
.badge-yellow { background: var(--yellow-bg); color: var(--yellow); }
.badge-blue   { background: rgba(67,97,238,.12); color: var(--accent); }
.badge-gray   { background: var(--border); color: var(--fg2); }

/* ── P&L coloring ────────────────────────────────────────────────── */
.pnl-pos { color: var(--green); font-weight: 600; }
.pnl-neg { color: var(--red);   font-weight: 600; }
.pnl-zero{ color: var(--fg3); }

/* ── Chart area ──────────────────────────────────────────────────── */
.chart-wrap {
  position: relative;
  width: 100%;
}
.chart-wrap canvas { max-height: 260px; }
.chart-wrap-tall canvas { max-height: 320px; }

/* ── Top-N list ──────────────────────────────────────────────────── */
.top-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 14px;
  border-bottom: 1px solid var(--border);
}
.top-row:last-child { border-bottom: none; }
.top-num {
  width: 22px;
  height: 22px;
  border-radius: 50%;
  background: var(--bg);
  border: 1px solid var(--border);
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 11px;
  font-weight: 700;
  color: var(--fg3);
  flex-shrink: 0;
}
.top-num.gold   { background: #ffd700; border-color: #e6c200; color: #7a5700; }
.top-num.silver { background: #c0c0c0; border-color: #aaa; color: #555; }
.top-num.bronze { background: #cd7f32; border-color: #b06420; color: #fff; }
.top-info { flex: 1; min-width: 0; }
.top-name {
  font-weight: 600;
  font-size: 13px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.top-set { font-size: 11px; color: var(--fg3); }
.top-price { font-weight: 700; color: var(--fg); white-space: nowrap; }

/* ── Portfolio chart + top row ───────────────────────────────────── */
.two-col {
  display: grid;
  grid-template-columns: 1fr 320px;
  gap: 20px;
  margin-bottom: 20px;
}
@media (max-width: 960px) { .two-col { grid-template-columns: 1fr; } }

/* ── Empty state ─────────────────────────────────────────────────── */
.empty {
  padding: 48px 24px;
  text-align: center;
  color: var(--fg3);
}
.empty .icon { font-size: 36px; margin-bottom: 8px; }
.empty p { font-size: 13px; }

/* ── Pagination ──────────────────────────────────────────────────── */
.pagination {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 14px;
  border-top: 1px solid var(--border);
  font-size: 12px;
  color: var(--fg3);
  flex-wrap: wrap;
  gap: 6px;
}
.pag-btns { display: flex; gap: 4px; }
.pag-btn {
  padding: 4px 10px;
  border: 1px solid var(--border);
  border-radius: 5px;
  background: var(--surface);
  color: var(--fg2);
  font-size: 12px;
  cursor: pointer;
  font-family: inherit;
}
.pag-btn:hover:not(:disabled) { border-color: var(--accent); color: var(--accent); }
.pag-btn:disabled { opacity: .4; cursor: default; }
.pag-btn.active { background: var(--accent); color: #fff; border-color: var(--accent); }

/* ── Modal ───────────────────────────────────────────────────────── */
.modal-overlay {
  position: fixed; inset: 0;
  background: rgba(0,0,0,.55);
  display: flex; align-items: center; justify-content: center;
  z-index: 100;
  opacity: 0; pointer-events: none;
  transition: opacity .2s;
}
.modal-overlay.open { opacity: 1; pointer-events: auto; }
.modal {
  background: var(--surface);
  border-radius: 12px;
  width: 700px;
  max-width: 96vw;
  max-height: 90vh;
  overflow-y: auto;
  box-shadow: var(--shadow-lg);
  transform: translateY(12px);
  transition: transform .2s;
}
.modal-overlay.open .modal { transform: translateY(0); }
.modal-header {
  padding: 16px 20px 14px;
  border-bottom: 1px solid var(--border);
  display: flex; align-items: center; gap: 10px;
}
.modal-title { font-size: 16px; font-weight: 700; flex: 1; }
.modal-close {
  background: none; border: none; cursor: pointer;
  color: var(--fg3); font-size: 20px; line-height: 1; padding: 2px 4px;
}
.modal-close:hover { color: var(--fg); }
.modal-body { padding: 20px; }

/* ── Misc ────────────────────────────────────────────────────────── */
.flex { display: flex; align-items: center; gap: 8px; }
.spacer { flex: 1; }
.text-muted { color: var(--fg3); }
.text-sm { font-size: 12px; }
.mono { font-family: 'Fira Mono', monospace; }
.grade-pill {
  display: inline-flex; align-items: center; justify-content: center;
  width: 32px; height: 32px;
  border-radius: 50%;
  background: linear-gradient(135deg, #4361ee, #3a0ca3);
  color: #fff;
  font-size: 12px; font-weight: 700;
}
.for-sale-dot {
  display: inline-block; width: 7px; height: 7px;
  border-radius: 50%; background: var(--green);
  margin-right: 3px;
}
</style>
</head>
<body>
<div class="app-shell">

<!-- ── Sidebar ─────────────────────────────────────────────────────── -->
<aside class="sidebar">
  <div class="logo">
    <div class="logo-title">
      <div class="logo-ball"></div>
      PokéDex Price
    </div>
    <div class="logo-sub">TCG Portfolio Tracker</div>
  </div>

  <nav class="nav-section">
    <h4>Menu</h4>
    <a class="nav-link active" onclick="showPage('dashboard')">
      <span class="icon">📊</span> Dashboard
    </a>
    <a class="nav-link" onclick="showPage('inventory')">
      <span class="icon">🗃️</span> Inventory
      <span class="nav-count" id="nc-inv">—</span>
    </a>
    <a class="nav-link" onclick="showPage('wishlist')">
      <span class="icon">⭐</span> Wishlist
      <span class="nav-count" id="nc-wish">—</span>
    </a>
    <a class="nav-link" onclick="showPage('graded')">
      <span class="icon">🏆</span> Graded
      <span class="nav-count" id="nc-graded">—</span>
    </a>
    <a class="nav-link" onclick="showPage('trade')">
      <span class="icon">🔄</span> Trade
      <span class="nav-count" id="nc-trade">—</span>
    </a>
    <a class="nav-link" onclick="showPage('sold')">
      <span class="icon">💸</span> Sold
      <span class="nav-count" id="nc-sold">—</span>
    </a>
  </nav>

  <div class="theme-toggle">
    <button class="theme-btn" onclick="toggleTheme()">🌙 Toggle Theme</button>
  </div>
</aside>

<!-- ── Main ────────────────────────────────────────────────────────── -->
<main class="main">

<!-- ═══════════ DASHBOARD ═══════════ -->
<section id="page-dashboard" class="page active">
  <div class="page-header">
    <span class="page-title">Portfolio Overview</span>
    <span class="page-sub" id="last-updated"></span>
  </div>

  <div class="stat-grid" id="stat-grid">
    <div class="stat-card"><div class="stat-label">Total Value (USD)</div><div class="stat-value blue" id="s-usd">—</div><div class="stat-sub" id="s-idr">—</div></div>
    <div class="stat-card"><div class="stat-label">Kartu</div><div class="stat-value" id="s-count">—</div><div class="stat-sub" id="s-sets">—</div></div>
    <div class="stat-card"><div class="stat-label">P&amp;L (USD)</div><div class="stat-value" id="s-pnl">—</div><div class="stat-sub" id="s-pnl-pct">—</div></div>
    <div class="stat-card"><div class="stat-label">Graded</div><div class="stat-value" id="s-graded">—</div><div class="stat-sub">cards</div></div>
    <div class="stat-card"><div class="stat-label">For Sale</div><div class="stat-value green" id="s-forsale">—</div><div class="stat-sub">cards listed</div></div>
    <div class="stat-card"><div class="stat-label">Wishlist</div><div class="stat-value" id="s-wish">—</div><div class="stat-sub" id="s-wish-val">items</div></div>
  </div>

  <div class="two-col">
    <div class="card">
      <div class="card-header">
        <span class="card-title">📈 Portfolio Value History</span>
        <span class="spacer"></span>
        <select class="filter-select" onchange="loadPortfolioChart(this.value)">
          <option value="30">30 hari</option>
          <option value="90">90 hari</option>
          <option value="365">1 tahun</option>
          <option value="0">Semua</option>
        </select>
      </div>
      <div class="card-body">
        <div class="chart-wrap-tall chart-wrap"><canvas id="portfolioChart"></canvas></div>
      </div>
    </div>

    <div class="card">
      <div class="card-header"><span class="card-title">🏅 Top 10 Most Valuable</span></div>
      <div id="top10-list">
        <div class="empty"><div class="icon">⏳</div><p>Loading…</p></div>
      </div>
    </div>
  </div>

  <div class="two-col">
    <div class="card">
      <div class="card-header"><span class="card-title">🍕 Portfolio by Set</span></div>
      <div class="card-body">
        <div class="chart-wrap"><canvas id="setChart"></canvas></div>
      </div>
    </div>
    <div class="card">
      <div class="card-header"><span class="card-title">📦 Condition Breakdown</span></div>
      <div class="card-body">
        <div class="chart-wrap"><canvas id="condChart"></canvas></div>
      </div>
    </div>
  </div>
</section>

<!-- ═══════════ INVENTORY ═══════════ -->
<section id="page-inventory" class="page">
  <div class="page-header">
    <span class="page-title">Inventory</span>
    <div class="filter-bar">
      <input class="search-input" id="inv-search" placeholder="🔍 Cari kartu…" oninput="filterInventory()">
      <select class="filter-select" id="inv-filter-set" onchange="filterInventory()">
        <option value="">Semua Set</option>
      </select>
      <select class="filter-select" id="inv-filter-cond" onchange="filterInventory()">
        <option value="">Semua Kondisi</option>
        <option>Mint</option>
        <option>Near Mint</option>
        <option>Lightly Played</option>
        <option>Moderately Played</option>
        <option>Heavily Played</option>
        <option>Damaged</option>
      </select>
      <select class="filter-select" id="inv-filter-folder" onchange="filterInventory()">
        <option value="">Semua Folder</option>
      </select>
    </div>
  </div>
  <div class="card">
    <div class="card-body no-pad">
      <div class="table-wrap">
        <table id="inv-table">
          <thead>
            <tr>
              <th onclick="sortTable('inv','card_name')">#  Kartu</th>
              <th onclick="sortTable('inv','card_set')">Set</th>
              <th onclick="sortTable('inv','condition')">Kondisi</th>
              <th onclick="sortTable('inv','psa_grade')">Grade</th>
              <th onclick="sortTable('inv','price_usd')" class="td-num">Harga Pasar</th>
              <th onclick="sortTable('inv','buy_price_usd')" class="td-num">Beli</th>
              <th onclick="sortTable('inv','pnl')" class="td-num">P&amp;L</th>
              <th>Folder</th>
              <th></th>
            </tr>
          </thead>
          <tbody id="inv-tbody"></tbody>
        </table>
      </div>
      <div class="pagination" id="inv-pag"></div>
    </div>
  </div>
</section>

<!-- ═══════════ WISHLIST ═══════════ -->
<section id="page-wishlist" class="page">
  <div class="page-header"><span class="page-title">⭐ Wishlist</span></div>
  <div class="card">
    <div class="card-body no-pad">
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>#</th>
              <th>Kartu</th>
              <th>Set</th>
              <th class="td-num">Harga Pasar</th>
              <th class="td-num">Target</th>
              <th class="td-num">Gap</th>
              <th>Ditambah</th>
            </tr>
          </thead>
          <tbody id="wish-tbody"></tbody>
        </table>
      </div>
    </div>
  </div>
</section>

<!-- ═══════════ GRADED ═══════════ -->
<section id="page-graded" class="page">
  <div class="page-header"><span class="page-title">🏆 Graded Cards</span></div>
  <div class="card">
    <div class="card-body no-pad">
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Kartu</th>
              <th>Set</th>
              <th class="td-num">Grade</th>
              <th class="td-num">Harga Pasar</th>
              <th class="td-num">Ref Price</th>
              <th class="td-num">Perubahan</th>
            </tr>
          </thead>
          <tbody id="graded-tbody"></tbody>
        </table>
      </div>
    </div>
  </div>
</section>

<!-- ═══════════ TRADE ═══════════ -->
<section id="page-trade" class="page">
  <div class="page-header"><span class="page-title">🔄 Trade Offers</span></div>
  <div class="card">
    <div class="card-body no-pad">
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>User</th>
              <th>Punya</th>
              <th>Mau</th>
              <th>Status</th>
              <th>Tanggal</th>
            </tr>
          </thead>
          <tbody id="trade-tbody"></tbody>
        </table>
      </div>
    </div>
  </div>
</section>

<!-- ═══════════ SOLD ═══════════ -->
<section id="page-sold" class="page">
  <div class="page-header"><span class="page-title">💸 Sold History</span></div>
  <div class="stat-grid">
    <div class="stat-card"><div class="stat-label">Total Terjual</div><div class="stat-value" id="sold-count">—</div></div>
    <div class="stat-card"><div class="stat-label">Total Revenue</div><div class="stat-value green" id="sold-rev">—</div></div>
    <div class="stat-card"><div class="stat-label">Total Profit</div><div class="stat-value" id="sold-profit">—</div></div>
  </div>
  <div class="card">
    <div class="card-body no-pad">
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Kartu</th>
              <th>Set</th>
              <th class="td-num">Jual</th>
              <th class="td-num">Beli</th>
              <th class="td-num">Profit</th>
              <th>Tanggal</th>
            </tr>
          </thead>
          <tbody id="sold-tbody"></tbody>
        </table>
      </div>
    </div>
  </div>
</section>

</main><!-- end .main -->
</div><!-- end .app-shell -->

<!-- ── Price History Modal ─────────────────────────────────────────── -->
<div class="modal-overlay" id="modal-overlay" onclick="closeModal(event)">
  <div class="modal">
    <div class="modal-header">
      <span class="modal-title" id="modal-card-name">—</span>
      <button class="modal-close" onclick="document.getElementById('modal-overlay').classList.remove('open')">✕</button>
    </div>
    <div class="modal-body">
      <div class="stat-grid" id="modal-stats"></div>
      <div class="chart-wrap-tall chart-wrap" style="margin-bottom:16px"><canvas id="priceHistChart"></canvas></div>
      <div class="table-wrap">
        <table>
          <thead><tr><th>Tanggal</th><th class="td-num">USD</th><th class="td-num">IDR</th></tr></thead>
          <tbody id="modal-hist-tbody"></tbody>
        </table>
      </div>
    </div>
  </div>
</div>

<!-- ══════════════════════════════════════════════════════════════════ -->
<script>
// ── State ────────────────────────────────────────────────────────────
let inv = [], invFiltered = [], invPage = 1, invPerPage = 25;
let invSort = { col: 'price_usd', dir: 'desc' };
let portfolioChart, priceHistChart, setChart, condChart;
const EXRATE = {{ exchange_rate }};

// ── Theme ─────────────────────────────────────────────────────────────
function toggleTheme() {
  const root = document.documentElement;
  if (root.dataset.theme === 'dark') {
    root.dataset.theme = 'light';
    try { localStorage.setItem('theme','light'); } catch(e){}
  } else {
    root.dataset.theme = 'dark';
    try { localStorage.setItem('theme','dark'); } catch(e){}
  }
  redrawCharts();
}
try {
  const t = localStorage.getItem('theme');
  if (t) document.documentElement.dataset.theme = t;
} catch(e){}

function chartColors() {
  const dark = document.documentElement.dataset.theme === 'dark' ||
    (!document.documentElement.dataset.theme &&
      window.matchMedia('(prefers-color-scheme: dark)').matches);
  return {
    grid: dark ? 'rgba(255,255,255,.07)' : 'rgba(0,0,0,.06)',
    text: dark ? '#9ea8d0' : '#8a92b0',
    line: dark ? '#738bff' : '#4361ee',
    lineGrad: dark ? ['rgba(115,139,255,.35)','rgba(115,139,255,.01)']
                   : ['rgba(67,97,238,.3)','rgba(67,97,238,.01)'],
    green: '#2dc653',
    red:   '#e63946',
  };
}

// ── Page navigation ───────────────────────────────────────────────────
function showPage(id) {
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-link').forEach(a => a.classList.remove('active'));
  document.getElementById('page-' + id).classList.add('active');
  event.currentTarget.classList.add('active');

  if (id === 'inventory' && inv.length === 0) loadInventory();
  if (id === 'wishlist')  loadWishlist();
  if (id === 'graded')    loadGraded();
  if (id === 'trade')     loadTrade();
  if (id === 'sold')      loadSold();
}

// ── API helpers ───────────────────────────────────────────────────────
async function api(path) {
  const r = await fetch(path);
  return r.json();
}

// ── Dashboard init ────────────────────────────────────────────────────
async function initDashboard() {
  const [stats, top10, bySet, byCond] = await Promise.all([
    api('/api/stats'),
    api('/api/top10'),
    api('/api/by-set'),
    api('/api/by-condition'),
  ]);

  // Stats
  const pnl = stats.total_market_usd - stats.total_buy_usd;
  const pnlPct = stats.total_buy_usd > 0 ? (pnl / stats.total_buy_usd * 100).toFixed(1) : 0;
  document.getElementById('s-usd').textContent = '$' + stats.total_market_usd.toFixed(2);
  document.getElementById('s-idr').textContent = 'Rp ' + Math.round(stats.total_market_usd * EXRATE).toLocaleString('id-ID');
  document.getElementById('s-count').textContent = stats.card_count;
  document.getElementById('s-sets').textContent = stats.set_count + ' set berbeda';
  const pelm = document.getElementById('s-pnl');
  pelm.textContent = (pnl >= 0 ? '+$' : '-$') + Math.abs(pnl).toFixed(2);
  pelm.className = 'stat-value ' + (pnl >= 0 ? 'green' : 'red');
  document.getElementById('s-pnl-pct').textContent = (pnl >= 0 ? '+' : '') + pnlPct + '%';
  document.getElementById('s-graded').textContent = stats.graded_count;
  document.getElementById('s-forsale').textContent = stats.for_sale_count;
  document.getElementById('s-wish').textContent = stats.wishlist_count;
  document.getElementById('s-wish-val').textContent = 'items';
  document.getElementById('last-updated').textContent =
    'Updated: ' + new Date().toLocaleTimeString('id-ID');

  // Nav counts
  document.getElementById('nc-inv').textContent    = stats.card_count;
  document.getElementById('nc-wish').textContent   = stats.wishlist_count;
  document.getElementById('nc-graded').textContent = stats.graded_count;
  document.getElementById('nc-trade').textContent  = stats.trade_count;
  document.getElementById('nc-sold').textContent   = stats.sold_count;

  // Top 10
  const medals = ['gold','silver','bronze'];
  document.getElementById('top10-list').innerHTML = top10.map((c,i) => `
    <div class="top-row">
      <div class="top-num ${medals[i]||''}">${i+1}</div>
      <div class="top-info">
        <div class="top-name">${esc(c.card_name)}</div>
        <div class="top-set">${esc(c.card_set||'—')}</div>
      </div>
      <div class="top-price">$${c.price_usd.toFixed(2)}</div>
    </div>
  `).join('');

  // Portfolio chart
  await loadPortfolioChart(30);

  // By-set doughnut
  drawSetChart(bySet);

  // Condition pie
  drawCondChart(byCond);
}

// ── Portfolio chart ───────────────────────────────────────────────────
async function loadPortfolioChart(days) {
  const data = await api('/api/portfolio-history?days=' + days);
  const c = chartColors();
  const ctx = document.getElementById('portfolioChart');

  if (portfolioChart) portfolioChart.destroy();
  portfolioChart = new Chart(ctx, {
    type: 'line',
    data: {
      labels: data.map(d => d.date),
      datasets: [{
        label: 'Portfolio USD',
        data: data.map(d => d.total_usd),
        borderColor: c.line,
        borderWidth: 2,
        tension: 0.4,
        fill: true,
        pointRadius: data.length > 60 ? 0 : 3,
        pointHoverRadius: 5,
        backgroundColor: ctx2 => {
          const g = ctx2.chart.ctx.createLinearGradient(0, 0, 0, 260);
          g.addColorStop(0, c.lineGrad[0]);
          g.addColorStop(1, c.lineGrad[1]);
          return g;
        },
      }]
    },
    options: {
      responsive: true, maintainAspectRatio: true,
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            label: ctx => ' $' + ctx.parsed.y.toFixed(2)
          }
        }
      },
      scales: {
        x: {
          grid: { color: c.grid },
          ticks: { color: c.text, maxTicksLimit: 8, font: { size: 11 } }
        },
        y: {
          grid: { color: c.grid },
          ticks: { color: c.text, font: { size: 11 },
            callback: v => '$' + v.toFixed(0) }
        }
      }
    }
  });
}

// ── Set doughnut ──────────────────────────────────────────────────────
function drawSetChart(data) {
  if (!data.length) return;
  const top = data.slice(0, 8);
  const colors = ['#4361ee','#3a0ca3','#7209b7','#e63946','#f4a261','#2dc653','#38b2ac','#ed8936'];
  const ctx = document.getElementById('setChart');
  if (setChart) setChart.destroy();
  setChart = new Chart(ctx, {
    type: 'doughnut',
    data: {
      labels: top.map(d => d.card_set || 'Unknown'),
      datasets: [{ data: top.map(d => d.total_usd.toFixed(2)), backgroundColor: colors, borderWidth: 2 }]
    },
    options: {
      responsive: true, maintainAspectRatio: true,
      plugins: {
        legend: {
          position: 'right',
          labels: { color: chartColors().text, font: { size: 11 }, padding: 12 }
        },
        tooltip: { callbacks: { label: ctx => ' $' + ctx.raw } }
      }
    }
  });
}

// ── Condition pie ─────────────────────────────────────────────────────
function drawCondChart(data) {
  if (!data.length) return;
  const condColor = {
    'Mint': '#2dc653', 'Near Mint': '#4361ee',
    'Lightly Played': '#38b2ac', 'Moderately Played': '#f4a261',
    'Heavily Played': '#e63946', 'Damaged': '#7209b7',
  };
  const ctx = document.getElementById('condChart');
  if (condChart) condChart.destroy();
  condChart = new Chart(ctx, {
    type: 'doughnut',
    data: {
      labels: data.map(d => d.condition),
      datasets: [{
        data: data.map(d => d.cnt),
        backgroundColor: data.map(d => condColor[d.condition] || '#8a92b0'),
        borderWidth: 2,
      }]
    },
    options: {
      responsive: true, maintainAspectRatio: true,
      plugins: {
        legend: {
          position: 'right',
          labels: { color: chartColors().text, font: { size: 11 }, padding: 12 }
        }
      }
    }
  });
}

function redrawCharts() {
  if (portfolioChart) {
    const days = document.querySelector('#page-dashboard select')?.value || 30;
    loadPortfolioChart(days);
  }
  // Redraw set/cond
  if (setChart) {
    api('/api/by-set').then(drawSetChart);
    api('/api/by-condition').then(drawCondChart);
  }
}

// ── Inventory ─────────────────────────────────────────────────────────
async function loadInventory() {
  const data = await api('/api/inventory');
  inv = data;
  // Populate set filter
  const sets = [...new Set(data.map(d => d.card_set).filter(Boolean))].sort();
  const sf = document.getElementById('inv-filter-set');
  sf.innerHTML = '<option value="">Semua Set</option>' +
    sets.map(s => `<option>${esc(s)}</option>`).join('');
  const folders = [...new Set(data.map(d => d.folder).filter(Boolean))].sort();
  const ff = document.getElementById('inv-filter-folder');
  ff.innerHTML = '<option value="">Semua Folder</option>' +
    folders.map(f => `<option>${esc(f)}</option>`).join('');
  filterInventory();
}

function filterInventory() {
  const q    = document.getElementById('inv-search').value.toLowerCase();
  const set  = document.getElementById('inv-filter-set').value;
  const cond = document.getElementById('inv-filter-cond').value;
  const fold = document.getElementById('inv-filter-folder').value;
  invFiltered = inv.filter(c => {
    if (q    && !c.card_name.toLowerCase().includes(q) &&
               !(c.card_set||'').toLowerCase().includes(q)) return false;
    if (set  && c.card_set !== set) return false;
    if (cond && c.condition !== cond) return false;
    if (fold && c.folder !== fold) return false;
    return true;
  });
  sortApply();
  invPage = 1;
  renderInvTable();
}

function sortTable(tbl, col) {
  if (tbl === 'inv') {
    if (invSort.col === col) invSort.dir = invSort.dir === 'asc' ? 'desc' : 'asc';
    else { invSort.col = col; invSort.dir = 'desc'; }
    sortApply();
    renderInvTable();
  }
}

function sortApply() {
  const { col, dir } = invSort;
  invFiltered.sort((a,b) => {
    let va = a[col], vb = b[col];
    if (va == null) va = col === 'price_usd' ? 0 : '';
    if (vb == null) vb = col === 'price_usd' ? 0 : '';
    if (typeof va === 'string') va = va.toLowerCase();
    if (typeof vb === 'string') vb = vb.toLowerCase();
    if (va < vb) return dir === 'asc' ? -1 : 1;
    if (va > vb) return dir === 'asc' ? 1 : -1;
    return 0;
  });
  document.querySelectorAll('#inv-table thead th').forEach(th => {
    th.classList.remove('sort-asc','sort-desc');
  });
  const headers = document.querySelectorAll('#inv-table thead th');
  const cols = ['card_name','card_set','condition','psa_grade','price_usd','buy_price_usd','pnl','folder'];
  const idx = cols.indexOf(col);
  if (idx >= 0) headers[idx].classList.add('sort-' + invSort.dir);
}

function renderInvTable() {
  const total = invFiltered.length;
  const pages = Math.ceil(total / invPerPage) || 1;
  if (invPage > pages) invPage = pages;
  const start = (invPage - 1) * invPerPage;
  const slice = invFiltered.slice(start, start + invPerPage);

  document.getElementById('inv-tbody').innerHTML = slice.map((c, i) => {
    const pnl = (c.price_usd || 0) - (c.buy_price_usd || 0);
    const pnlCls = pnl > 0 ? 'pnl-pos' : pnl < 0 ? 'pnl-neg' : 'pnl-zero';
    const pnlStr = pnl === 0 ? '—' : (pnl > 0 ? '+' : '') + '$' + pnl.toFixed(2);
    const cond = c.condition || 'Near Mint';
    const condBadge = {
      'Mint':'badge-green','Near Mint':'badge-blue','Lightly Played':'badge-yellow',
      'Moderately Played':'badge-yellow','Heavily Played':'badge-red','Damaged':'badge-red'
    }[cond] || 'badge-gray';
    return `<tr>
      <td><a class="td-name" style="max-width:200px;display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis"
           onclick="openCardModal(${c.id},'${esc(c.card_name)}')"
           href="javascript:void(0)">${esc(c.card_name)}</a>
           ${c.for_sale ? '<span class="for-sale-dot" title="For Sale"></span>' : ''}</td>
      <td class="td-set">${esc(c.card_set||'—')}</td>
      <td><span class="badge ${condBadge}">${cond}</span></td>
      <td class="td-num">${c.psa_grade ? `<span class="grade-pill">${esc(c.psa_grade)}</span>` : '<span class="text-muted">—</span>'}</td>
      <td class="td-num">$${(c.price_usd||0).toFixed(2)}</td>
      <td class="td-num">${c.buy_price_usd ? '$'+c.buy_price_usd.toFixed(2) : '<span class="text-muted">—</span>'}</td>
      <td class="td-num ${pnlCls}">${pnlStr}</td>
      <td><span class="badge badge-gray">${esc(c.folder||'Pribadi')}</span></td>
      <td><a href="javascript:void(0)" onclick="openCardModal(${c.id},'${esc(c.card_name)}')" style="font-size:11px;color:var(--accent)">📊</a></td>
    </tr>`;
  }).join('');

  // Pagination
  const pag = document.getElementById('inv-pag');
  pag.innerHTML = `
    <span>${total} kartu${total !== inv.length ? ' (filter)' : ''} &nbsp;·&nbsp; Halaman ${invPage} dari ${pages}</span>
    <div class="pag-btns">
      <button class="pag-btn" onclick="changePage(-1)" ${invPage<=1?'disabled':''}>‹ Prev</button>
      ${Array.from({length: Math.min(pages,7)}, (_,k) => {
        const p = invPage <= 4 ? k+1 : invPage - 3 + k;
        if (p < 1 || p > pages) return '';
        return `<button class="pag-btn${p===invPage?' active':''}" onclick="gotoPage(${p})">${p}</button>`;
      }).join('')}
      <button class="pag-btn" onclick="changePage(1)" ${invPage>=pages?'disabled':''}>Next ›</button>
    </div>
  `;
}
function changePage(d) { invPage = Math.max(1, Math.min(Math.ceil(invFiltered.length/invPerPage), invPage+d)); renderInvTable(); }
function gotoPage(p) { invPage = p; renderInvTable(); }

// ── Card modal ─────────────────────────────────────────────────────────
async function openCardModal(id, name) {
  document.getElementById('modal-card-name').textContent = name;
  document.getElementById('modal-stats').innerHTML = '<div class="text-muted">Loading…</div>';
  document.getElementById('modal-hist-tbody').innerHTML = '';
  document.getElementById('modal-overlay').classList.add('open');

  const [details, hist] = await Promise.all([
    api('/api/card/' + id),
    api('/api/price-history?name=' + encodeURIComponent(name)),
  ]);

  // Stats
  const pnl = (details.price_usd||0) - (details.buy_price_usd||0);
  const pnlPct = details.buy_price_usd > 0 ? (pnl/details.buy_price_usd*100).toFixed(1) : 0;
  document.getElementById('modal-stats').innerHTML = `
    <div class="stat-card"><div class="stat-label">Harga Pasar</div><div class="stat-value blue">$${(details.price_usd||0).toFixed(2)}</div></div>
    <div class="stat-card"><div class="stat-label">Harga Beli</div><div class="stat-value">${details.buy_price_usd?'$'+details.buy_price_usd.toFixed(2):'—'}</div></div>
    <div class="stat-card"><div class="stat-label">P&L</div><div class="stat-value ${pnl>=0?'green':'red'}">${pnl>=0?'+':''}$${pnl.toFixed(2)} <span style="font-size:14px">(${pnlPct}%)</span></div></div>
    <div class="stat-card"><div class="stat-label">Kondisi</div><div class="stat-value" style="font-size:16px">${details.condition||'—'}</div></div>
  `;

  // History table
  document.getElementById('modal-hist-tbody').innerHTML = hist.map(h => `
    <tr>
      <td>${h.recorded_at.slice(0,16).replace('T',' ')}</td>
      <td class="td-num">$${(h.price_usd||0).toFixed(2)}</td>
      <td class="td-num">Rp ${Math.round((h.price_usd||0)*EXRATE).toLocaleString('id-ID')}</td>
    </tr>
  `).join('') || '<tr><td colspan="3" class="text-muted" style="padding:16px;text-align:center">Belum ada riwayat harga</td></tr>';

  // Chart
  const ctx = document.getElementById('priceHistChart');
  if (priceHistChart) priceHistChart.destroy();
  if (hist.length > 0) {
    const c = chartColors();
    priceHistChart = new Chart(ctx, {
      type: 'line',
      data: {
        labels: hist.map(h => h.recorded_at.slice(0,10)),
        datasets: [{
          label: 'USD',
          data: hist.map(h => h.price_usd),
          borderColor: c.line,
          borderWidth: 2,
          tension: 0.3,
          fill: false,
          pointRadius: 4,
          pointHoverRadius: 6,
        }]
      },
      options: {
        responsive: true, maintainAspectRatio: true,
        plugins: {
          legend: { display: false },
          tooltip: { callbacks: { label: ctx => ' $' + ctx.parsed.y.toFixed(2) } }
        },
        scales: {
          x: { grid: { color: c.grid }, ticks: { color: c.text, font: { size: 11 } } },
          y: { grid: { color: c.grid }, ticks: { color: c.text, font: { size: 11 },
               callback: v => '$' + v.toFixed(2) } }
        }
      }
    });
  }
}

function closeModal(e) {
  if (e.target.id === 'modal-overlay')
    document.getElementById('modal-overlay').classList.remove('open');
}

// ── Wishlist ──────────────────────────────────────────────────────────
async function loadWishlist() {
  const data = await api('/api/wishlist');
  document.getElementById('wish-tbody').innerHTML = data.map((w,i) => {
    const gap = w.target_price_usd > 0 ? (w.price_usd - w.target_price_usd) : null;
    const gapStr = gap === null ? '—' : (gap <= 0
      ? `<span class="badge badge-green">On Target!</span>`
      : `<span class="pnl-neg">-$${gap.toFixed(2)}</span>`);
    return `<tr>
      <td class="text-muted">${i+1}</td>
      <td class="td-name">${esc(w.card_name)}</td>
      <td class="td-set">${esc(w.card_set||'—')}</td>
      <td class="td-num">$${(w.price_usd||0).toFixed(2)}</td>
      <td class="td-num">${w.target_price_usd ? '$'+w.target_price_usd.toFixed(2) : '—'}</td>
      <td class="td-num">${gapStr}</td>
      <td class="text-muted text-sm">${(w.added_at||'').slice(0,10)}</td>
    </tr>`;
  }).join('') || '<tr><td colspan="7"><div class="empty"><div class="icon">🌟</div><p>Wishlist kosong</p></div></td></tr>';
}

// ── Graded ────────────────────────────────────────────────────────────
async function loadGraded() {
  const data = await api('/api/graded');
  document.getElementById('graded-tbody').innerHTML = data.map(c => {
    const ref = c.grade_ref_price;
    const cur = c.price_usd || 0;
    let chg = '';
    if (ref && ref > 0) {
      const pct = ((cur - ref) / ref * 100);
      chg = `<span class="${pct >= 0 ? 'pnl-pos' : 'pnl-neg'}">${pct >= 0 ? '+' : ''}${pct.toFixed(1)}%</span>`;
    }
    return `<tr>
      <td class="td-name">${esc(c.card_name)}</td>
      <td class="td-set">${esc(c.card_set||'—')}</td>
      <td class="td-num"><span class="grade-pill">${esc(c.psa_grade)}</span></td>
      <td class="td-num">$${cur.toFixed(2)}</td>
      <td class="td-num">${ref ? '$'+ref.toFixed(2) : '<span class="text-muted">—</span>'}</td>
      <td class="td-num">${chg || '<span class="text-muted">—</span>'}</td>
    </tr>`;
  }).join('') || '<tr><td colspan="6"><div class="empty"><div class="icon">🏆</div><p>Belum ada kartu graded</p></div></td></tr>';
}

// ── Trade offers ──────────────────────────────────────────────────────
async function loadTrade() {
  const data = await api('/api/trade-offers');
  document.getElementById('trade-tbody').innerHTML = data.map(t => `
    <tr>
      <td>${esc(t.username || 'User '+t.user_id)}</td>
      <td><span class="badge badge-blue">${esc(t.card_name_have)}</span></td>
      <td><span class="badge badge-yellow">${esc(t.card_name_want)}</span></td>
      <td><span class="badge ${t.status==='open'?'badge-green':'badge-gray'}">${t.status}</span></td>
      <td class="text-sm text-muted">${(t.created_at||'').slice(0,10)}</td>
    </tr>
  `).join('') || '<tr><td colspan="5"><div class="empty"><div class="icon">🔄</div><p>Belum ada trade offer</p></div></td></tr>';
}

// ── Sold history ──────────────────────────────────────────────────────
async function loadSold() {
  const data = await api('/api/sold');
  let totalRev = 0, totalProfit = 0;
  const rows = data.map(s => {
    totalRev    += s.sell_price_usd || 0;
    totalProfit += s.profit_usd     || 0;
    const pCls = (s.profit_usd||0) >= 0 ? 'pnl-pos' : 'pnl-neg';
    return `<tr>
      <td class="td-name">${esc(s.card_name)}</td>
      <td class="td-set">${esc(s.card_set||'—')}</td>
      <td class="td-num">$${(s.sell_price_usd||0).toFixed(2)}</td>
      <td class="td-num">$${(s.buy_price_usd||0).toFixed(2)}</td>
      <td class="td-num ${pCls}">${(s.profit_usd||0)>=0?'+':''}$${(s.profit_usd||0).toFixed(2)}</td>
      <td class="text-sm text-muted">${(s.sold_at||'').slice(0,10)}</td>
    </tr>`;
  });
  document.getElementById('sold-count').textContent  = data.length;
  document.getElementById('sold-rev').textContent    = '$' + totalRev.toFixed(2);
  const profEl = document.getElementById('sold-profit');
  profEl.textContent = (totalProfit >= 0 ? '+$' : '-$') + Math.abs(totalProfit).toFixed(2);
  profEl.className = 'stat-value ' + (totalProfit >= 0 ? 'green' : 'red');
  document.getElementById('sold-tbody').innerHTML = rows.join('') ||
    '<tr><td colspan="6"><div class="empty"><div class="icon">💸</div><p>Belum ada kartu terjual</p></div></td></tr>';
}

// ── Escape HTML ──────────────────────────────────────────────────────
function esc(s) {
  if (!s && s !== 0) return '';
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

// ── Boot ─────────────────────────────────────────────────────────────
initDashboard();
</script>
</body>
</html>
"""

# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template_string(HTML, exchange_rate=EXCHANGE_RATE)


@app.route("/api/stats")
def api_stats():
    inv = q("""
        SELECT price_usd, buy_price_usd, psa_grade, for_sale, card_set
        FROM inventory
    """)
    total_market = sum(r["price_usd"] or 0 for r in inv)
    total_buy    = sum(r["buy_price_usd"] or 0 for r in inv)
    graded       = sum(1 for r in inv if r["psa_grade"])
    for_sale     = sum(1 for r in inv if r["for_sale"])
    sets         = len({r["card_set"] for r in inv if r["card_set"]})

    wish = q("SELECT COUNT(*) as c FROM wishlist")
    wish_count = wish[0]["c"] if wish else 0

    trade = q("SELECT COUNT(*) as c FROM trade_offers WHERE status='open'")
    trade_count = trade[0]["c"] if trade else 0

    sold = q("SELECT COUNT(*) as c FROM trade_log")
    sold_count = sold[0]["c"] if sold else 0

    return jsonify(
        total_market_usd=round(total_market, 2),
        total_buy_usd   =round(total_buy, 2),
        card_count      =len(inv),
        set_count       =sets,
        graded_count    =graded,
        for_sale_count  =for_sale,
        wishlist_count  =wish_count,
        trade_count     =trade_count,
        sold_count      =sold_count,
    )


@app.route("/api/top10")
def api_top10():
    rows = q("""
        SELECT card_name, card_set, price_usd
        FROM inventory
        ORDER BY price_usd DESC
        LIMIT 10
    """)
    return jsonify([dict(r) for r in rows])


@app.route("/api/inventory")
def api_inventory():
    rows = q("""
        SELECT id, card_name, card_set, price_usd, price_idr,
               condition, psa_grade, buy_price_usd, for_sale,
               ask_price_usd, folder, tags, notes, grade_ref_price
        FROM inventory
        ORDER BY price_usd DESC
    """)
    result = []
    for r in rows:
        d = dict(r)
        d["pnl"] = (d.get("price_usd") or 0) - (d.get("buy_price_usd") or 0)
        result.append(d)
    return jsonify(result)


@app.route("/api/card/<int:card_id>")
def api_card(card_id):
    row = q1("""
        SELECT id, card_name, card_set, price_usd, price_idr,
               condition, psa_grade, buy_price_usd, for_sale,
               ask_price_usd, folder, tags, notes, grade_ref_price
        FROM inventory WHERE id = ?
    """, (card_id,))
    if row is None:
        return jsonify({}), 404
    return jsonify(dict(row))


@app.route("/api/price-history")
def api_price_history():
    name = request.args.get("name", "")
    rows = q("""
        SELECT price_usd, price_idr, recorded_at
        FROM price_history
        WHERE LOWER(card_name) = LOWER(?)
        ORDER BY recorded_at ASC
        LIMIT 200
    """, (name,))
    return jsonify([dict(r) for r in rows])


@app.route("/api/portfolio-history")
def api_portfolio_history():
    days = int(request.args.get("days", 30))
    if days > 0:
        rows = q("""
            SELECT date(snapped_at) as date, SUM(total_usd) as total_usd
            FROM portfolio_snapshots
            WHERE snapped_at >= date('now', ? || ' days')
            GROUP BY date(snapped_at)
            ORDER BY date ASC
        """, (f"-{days}",))
    else:
        rows = q("""
            SELECT date(snapped_at) as date, SUM(total_usd) as total_usd
            FROM portfolio_snapshots
            GROUP BY date(snapped_at)
            ORDER BY date ASC
        """)
    # If empty, return today's value as single point
    if not rows:
        total = q1("SELECT COALESCE(SUM(price_usd),0) as t FROM inventory")
        today = datetime.now().strftime("%Y-%m-%d")
        return jsonify([{"date": today, "total_usd": round(total["t"], 2)}])
    return jsonify([dict(r) for r in rows])


@app.route("/api/by-set")
def api_by_set():
    rows = q("""
        SELECT COALESCE(card_set,'Unknown') as card_set,
               SUM(price_usd) as total_usd,
               COUNT(*) as cnt
        FROM inventory
        GROUP BY card_set
        ORDER BY total_usd DESC
        LIMIT 10
    """)
    return jsonify([dict(r) for r in rows])


@app.route("/api/by-condition")
def api_by_condition():
    rows = q("""
        SELECT COALESCE(condition,'Near Mint') as condition,
               COUNT(*) as cnt
        FROM inventory
        GROUP BY condition
        ORDER BY cnt DESC
    """)
    return jsonify([dict(r) for r in rows])


@app.route("/api/wishlist")
def api_wishlist():
    rows = q("""
        SELECT id, card_name, card_set, price_usd, price_idr,
               target_price_usd, added_at
        FROM wishlist
        ORDER BY price_usd DESC
    """)
    return jsonify([dict(r) for r in rows])


@app.route("/api/graded")
def api_graded():
    rows = q("""
        SELECT card_name, card_set, psa_grade, price_usd, grade_ref_price
        FROM inventory
        WHERE psa_grade IS NOT NULL AND psa_grade != ''
        ORDER BY price_usd DESC
    """)
    return jsonify([dict(r) for r in rows])


@app.route("/api/trade-offers")
def api_trade_offers():
    rows = q("""
        SELECT id, user_id, username, card_name_have, card_name_want, status, created_at
        FROM trade_offers
        ORDER BY created_at DESC
        LIMIT 100
    """)
    return jsonify([dict(r) for r in rows])


@app.route("/api/sold")
def api_sold():
    rows = q("""
        SELECT card_name, card_set, sell_price_usd, buy_price_usd, profit_usd, sold_at
        FROM trade_log
        ORDER BY sold_at DESC
    """)
    return jsonify([dict(r) for r in rows])


# ── Run ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print(f"""
╔══════════════════════════════════════════════╗
║       PokéDex Price Dashboard  🎴            ║
╠══════════════════════════════════════════════╣
║  URL   : http://localhost:{PORT}              ║
║  DB    : {DB_PATH:<38}║
║  Rate  : 1 USD = Rp {EXCHANGE_RATE:,}               ║
╠══════════════════════════════════════════════╣
║  Cloudflare Tunnel:                          ║
║  cloudflared tunnel --url http://localhost:{PORT} ║
╚══════════════════════════════════════════════╝
""")
    app.run(host="0.0.0.0", port=PORT, debug=False)