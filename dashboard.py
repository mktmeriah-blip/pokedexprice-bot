#!/usr/bin/env python3
"""
PokeDex Price — Web Dashboard
==============================
Jalankan di Termux (sesi terpisah dari bot):
  pip install flask
  python3 dashboard.py

Buka di browser HP: http://localhost:5000
Akses dari HP lain : http://<IP-lokal>:5000
"""

import os
import glob
import sqlite3
from datetime import datetime, timedelta
from flask import Flask, jsonify, render_template_string, request

# ─── Auto-detect DB ───────────────────────────────────────────────────────────
def _find_db() -> str:
    # 1. Env var override
    if os.getenv("DB_PATH"):
        return os.getenv("DB_PATH")
    # 2. Folder yang sama dengan dashboard.py — cek semua nama DB yang mungkin
    script_dir = os.path.dirname(os.path.abspath(__file__))
    for name in ("pokemon_inventory.db", "pokedex.db", "inventory.db"):
        p = os.path.join(script_dir, name)
        if os.path.exists(p):
            return p
    # 3. Lokasi umum Termux / home
    home = os.path.expanduser("~")
    bot_dirs = ["", "bot", "pokedexprice-bot", "pokedex-bot", "PokeDexPrice",
                "pokedexeprice-bot", "pokemon-bot"]
    db_names = ["pokemon_inventory.db", "pokedex.db", "inventory.db"]
    for d in bot_dirs:
        for n in db_names:
            p = os.path.join(home, d, n) if d else os.path.join(home, n)
            if os.path.exists(p):
                return p
    # 4. Cari di seluruh home (semua nama DB)
    for name in db_names:
        found = glob.glob(os.path.join(home, "**", name), recursive=True)
        if found:
            found.sort(key=os.path.getmtime, reverse=True)
            return found[0]
    # Fallback
    return os.path.join(script_dir, "pokemon_inventory.db")

DB_PATH = _find_db()
RATE    = int(os.getenv("EXCHANGE_RATE", "16000"))
PORT    = int(os.getenv("DASHBOARD_PORT", "5000"))

app = Flask(__name__)

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

# ─── API: Users ───────────────────────────────────────────────────────────────
@app.route("/api/users")
def api_users():
    with db() as conn:
        rows = conn.execute("""
            SELECT DISTINCT user_id, COUNT(*) as cards
            FROM inventory GROUP BY user_id ORDER BY cards DESC
        """).fetchall()
    return jsonify([dict(r) for r in rows])

# ─── API: Stats ───────────────────────────────────────────────────────────────
@app.route("/api/stats")
def api_stats():
    uid = request.args.get("uid", type=int)
    w   = "WHERE user_id=?" if uid else ""
    p   = (uid,) if uid else ()
    with db() as conn:
        inv = conn.execute(f"""
            SELECT COUNT(*) as cnt,
                   COALESCE(SUM(price_usd),0)     as total_usd,
                   COALESCE(SUM(buy_price_usd),0) as total_buy,
                   COALESCE(SUM(CASE WHEN for_sale=1 THEN 1 ELSE 0 END),0) as fs_cnt,
                   COALESCE(SUM(CASE WHEN for_sale=1 THEN ask_price_usd ELSE 0 END),0) as fs_ask
            FROM inventory {w}
        """, p).fetchone()
        wish = conn.execute(f"SELECT COUNT(*) as cnt FROM wishlist {w}", p).fetchone()
        sets = conn.execute(f"""
            SELECT COUNT(DISTINCT COALESCE(card_set,'(Tanpa Set)')) as cnt
            FROM inventory {w}
        """, p).fetchone()
        psa = conn.execute(f"""
            SELECT COUNT(*) as cnt FROM inventory {w}
            {"AND" if uid else "WHERE"} psa_grade IS NOT NULL AND psa_grade != ''
        """, p).fetchone()

    total_usd = inv["total_usd"] or 0
    total_buy = inv["total_buy"] or 0
    roi = ((total_usd - total_buy) / total_buy * 100) if total_buy > 0 else 0

    return jsonify({
        "cards":      inv["cnt"],
        "total_usd":  total_usd,
        "total_idr":  total_usd * RATE,
        "total_buy":  total_buy,
        "roi":        round(roi, 2),
        "profit_usd": total_usd - total_buy,
        "fs_cnt":     inv["fs_cnt"],
        "fs_ask":     inv["fs_ask"],
        "wishlist":   wish["cnt"],
        "sets":       sets["cnt"],
        "psa_graded": psa["cnt"],
    })

# ─── API: Portfolio History ───────────────────────────────────────────────────
@app.route("/api/portfolio_history")
def api_portfolio_history():
    uid  = request.args.get("uid", type=int)
    days = request.args.get("days", 30, type=int)
    w    = "WHERE user_id=?" if uid else ""
    p    = (uid,) if uid else ()
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    with db() as conn:
        rows = conn.execute(f"""
            SELECT DATE(snapshot_at) as date,
                   COALESCE(SUM(total_usd), 0) as total_usd
            FROM portfolio_snapshots {w}
            {"AND" if uid else "WHERE"} snapshot_at >= ?
            GROUP BY DATE(snapshot_at) ORDER BY date
        """, p + (cutoff,)).fetchall()
    return jsonify([{"date": r["date"], "value": round(r["total_usd"], 2)} for r in rows])

# ─── API: By Set ──────────────────────────────────────────────────────────────
@app.route("/api/by_set")
def api_by_set():
    uid = request.args.get("uid", type=int)
    w   = "WHERE user_id=?" if uid else ""
    p   = (uid,) if uid else ()
    with db() as conn:
        rows = conn.execute(f"""
            SELECT COALESCE(card_set,'(Tanpa Set)') as set_name,
                   COUNT(*) as cnt,
                   COALESCE(SUM(price_usd), 0) as total_usd
            FROM inventory {w}
            GROUP BY set_name ORDER BY total_usd DESC LIMIT 10
        """, p).fetchall()
    return jsonify([dict(r) for r in rows])

# ─── API: By Condition ────────────────────────────────────────────────────────
@app.route("/api/by_condition")
def api_by_condition():
    uid = request.args.get("uid", type=int)
    w   = "WHERE user_id=?" if uid else ""
    p   = (uid,) if uid else ()
    with db() as conn:
        rows = conn.execute(f"""
            SELECT COALESCE(condition,'Unknown') as cond,
                   COUNT(*) as cnt,
                   COALESCE(SUM(price_usd), 0) as total_usd
            FROM inventory {w}
            GROUP BY cond ORDER BY total_usd DESC
        """, p).fetchall()
    return jsonify([dict(r) for r in rows])

# ─── API: Top Cards ───────────────────────────────────────────────────────────
@app.route("/api/top_cards")
def api_top_cards():
    uid   = request.args.get("uid", type=int)
    limit = request.args.get("limit", 10, type=int)
    w     = "WHERE user_id=?" if uid else ""
    p     = (uid,) if uid else ()
    with db() as conn:
        rows = conn.execute(f"""
            SELECT id, card_name, card_set, price_usd, buy_price_usd,
                   condition, psa_grade, for_sale, ask_price_usd, tags
            FROM inventory {w}
            ORDER BY price_usd DESC LIMIT ?
        """, p + (limit,)).fetchall()
    return jsonify([dict(r) for r in rows])

# ─── API: Cards (paginated, searchable) ───────────────────────────────────────
@app.route("/api/cards")
def api_cards():
    uid       = request.args.get("uid", type=int)
    q         = request.args.get("q", "")
    set_f     = request.args.get("set", "")
    page      = request.args.get("page", 1, type=int)
    per_page  = 20

    conditions, params = [], []
    if uid:   conditions.append("user_id=?");          params.append(uid)
    if q:     conditions.append("card_name LIKE ?");   params.append(f"%{q}%")
    if set_f: conditions.append("card_set=?");         params.append(set_f)
    where  = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    offset = (page - 1) * per_page

    with db() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM inventory {where}", params).fetchone()[0]
        rows  = conn.execute(f"""
            SELECT id, card_name, card_set, price_usd, buy_price_usd,
                   condition, psa_grade, for_sale, ask_price_usd, notes, tags
            FROM inventory {where}
            ORDER BY price_usd DESC LIMIT ? OFFSET ?
        """, params + [per_page, offset]).fetchall()

    return jsonify({
        "total": total,
        "page":  page,
        "pages": max(1, (total + per_page - 1) // per_page),
        "cards": [dict(r) for r in rows],
    })

# ─── API: Sets List ───────────────────────────────────────────────────────────
@app.route("/api/sets_list")
def api_sets_list():
    uid = request.args.get("uid", type=int)
    w   = "WHERE user_id=?" if uid else ""
    p   = (uid,) if uid else ()
    with db() as conn:
        rows = conn.execute(f"""
            SELECT DISTINCT COALESCE(card_set,'(Tanpa Set)') as name
            FROM inventory {w} ORDER BY name
        """, p).fetchall()
    return jsonify([r["name"] for r in rows])

# ─── API: For Sale ────────────────────────────────────────────────────────────
@app.route("/api/for_sale")
def api_for_sale():
    uid = request.args.get("uid", type=int)
    w   = "WHERE for_sale=1" + (" AND user_id=?" if uid else "")
    p   = (uid,) if uid else ()
    with db() as conn:
        rows = conn.execute(f"""
            SELECT id, card_name, card_set, ask_price_usd, buy_price_usd,
                   price_usd, condition, psa_grade, notes
            FROM inventory {w}
            ORDER BY ask_price_usd DESC
        """, p).fetchall()
    return jsonify([dict(r) for r in rows])

# ─── API: Recent Trades ───────────────────────────────────────────────────────
@app.route("/api/recent_trades")
def api_recent_trades():
    uid = request.args.get("uid", type=int)
    w   = "WHERE user_id=?" if uid else ""
    p   = (uid,) if uid else ()
    with db() as conn:
        rows = conn.execute(f"""
            SELECT card_name, sell_price_usd, buy_price_usd,
                   (sell_price_usd - buy_price_usd) as profit,
                   sold_at
            FROM trade_log {w}
            ORDER BY sold_at DESC LIMIT 20
        """, p).fetchall()
    return jsonify([dict(r) for r in rows])

# ─── API: Wishlist ────────────────────────────────────────────────────────────
@app.route("/api/wishlist")
def api_wishlist():
    uid = request.args.get("uid", type=int)
    w   = "WHERE user_id=?" if uid else ""
    p   = (uid,) if uid else ()
    with db() as conn:
        rows = conn.execute(f"""
            SELECT id, card_name, card_set, target_price_usd, priority, notes, added_at
            FROM wishlist {w}
            ORDER BY priority DESC, target_price_usd DESC LIMIT 50
        """, p).fetchall()
    return jsonify([dict(r) for r in rows])

# ─── Frontend HTML ────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template_string(DASHBOARD_HTML)

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="id">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PokeDex Price — Dashboard</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.min.css">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
:root {
  --pk-red:    #e53935;
  --pk-gold:   #fdd835;
  --pk-dark:   #0d0d1a;
  --pk-card:   #16162a;
  --pk-card2:  #1e1e36;
  --pk-border: #2e2e50;
  --pk-muted:  #7878a0;
  --pk-text:   #ddddf0;
}
*  { box-sizing: border-box; }
body { background: var(--pk-dark); color: var(--pk-text); font-family: 'Segoe UI', system-ui, sans-serif; margin: 0; }

/* Navbar */
.navbar  { background: var(--pk-card) !important; border-bottom: 2px solid var(--pk-red); padding: .6rem 1rem; position: sticky; top:0; z-index:100; }
.nb-logo { color: var(--pk-gold); font-weight: 800; font-size: 1.1rem; letter-spacing: -.3px; }
.nb-logo small { color: var(--pk-muted); font-weight: 400; font-size: .7rem; display: block; line-height: 1; }
.btn-refresh { background: none; border: 1px solid var(--pk-border); color: var(--pk-muted); border-radius: 8px; padding: 4px 10px; cursor: pointer; font-size: .85rem; transition: .2s; }
.btn-refresh:hover { border-color: var(--pk-red); color: var(--pk-red); }

/* UID Bar */
#uid-bar { background: var(--pk-card2); border-bottom: 1px solid var(--pk-border); padding: .4rem 1rem; display: flex; align-items: center; gap: .6rem; }
#uid-bar label { color: var(--pk-muted); font-size: .8rem; white-space: nowrap; }
#uid-select { background: var(--pk-card); border: 1px solid var(--pk-border); color: var(--pk-text); border-radius: 6px; padding: 3px 8px; font-size: .82rem; max-width: 220px; }

/* Stat Cards */
.stats-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: .6rem; padding: .8rem; }
@media(min-width:768px){ .stats-grid { grid-template-columns: repeat(4,1fr); } }
.scard { background: var(--pk-card); border: 1px solid var(--pk-border); border-radius: 12px; padding: 1rem 1.1rem; transition: transform .15s; }
.scard:hover { transform: translateY(-2px); }
.scard .lbl  { color: var(--pk-muted); font-size: .72rem; text-transform: uppercase; letter-spacing: 1px; margin-bottom: .3rem; }
.scard .val  { font-size: 1.5rem; font-weight: 700; line-height: 1.1; }
.scard .sub  { color: var(--pk-muted); font-size: .78rem; margin-top: .15rem; }
.c-gold  { color: var(--pk-gold); }
.c-green { color: #66bb6a; }
.c-red   { color: #ef5350; }
.c-blue  { color: #42a5f5; }
.c-muted { color: var(--pk-muted); }

/* Tabs */
.tabs-row { display: flex; gap: 0; border-bottom: 1px solid var(--pk-border); padding: 0 .8rem; background: var(--pk-card); overflow-x: auto; }
.tab-btn  { background: none; border: none; color: var(--pk-muted); padding: .65rem .9rem; font-size: .82rem; cursor: pointer; border-bottom: 2px solid transparent; white-space: nowrap; transition: .15s; }
.tab-btn:hover  { color: var(--pk-text); }
.tab-btn.active { color: var(--pk-gold); border-bottom-color: var(--pk-gold); font-weight: 600; }

/* Content */
.content { padding: .8rem; }
.box { background: var(--pk-card); border: 1px solid var(--pk-border); border-radius: 12px; padding: 1rem; margin-bottom: .8rem; }
.box-title { color: var(--pk-gold); font-size: .78rem; font-weight: 600; text-transform: uppercase; letter-spacing: 1px; margin-bottom: .8rem; }

/* Charts Grid */
.charts-grid { display: grid; grid-template-columns: 1fr; gap: .8rem; }
@media(min-width:768px){ .charts-grid { grid-template-columns: 2fr 1fr; } }
.charts-grid-2 { display: grid; grid-template-columns: 1fr; gap: .8rem; }
@media(min-width:768px){ .charts-grid-2 { grid-template-columns: 1fr 1fr; } }

/* Table */
.pk-table { width: 100%; border-collapse: collapse; font-size: .82rem; }
.pk-table th { color: var(--pk-muted); font-weight: 500; padding: .4rem .6rem; border-bottom: 1px solid var(--pk-border); text-align: left; white-space: nowrap; }
.pk-table td { padding: .45rem .6rem; border-bottom: 1px solid var(--pk-border); vertical-align: middle; }
.pk-table tr:last-child td { border-bottom: none; }
.pk-table tr:hover td { background: var(--pk-card2); }
.pk-table .text-right { text-align: right; }
.pk-table .text-center { text-align: center; }

/* Badges */
.badge-cond { font-size: .68rem; padding: 2px 5px; border-radius: 4px; font-weight: 600; }
.b-mint  { background: #00695c; color: #fff; }
.b-nm    { background: #2e7d32; color: #fff; }
.b-lp    { background: #1565c0; color: #fff; }
.b-mp    { background: #bf360c; color: #fff; }
.b-hp    { background: #b71c1c; color: #fff; }
.b-dmg   { background: #4a148c; color: #fff; }
.b-unk   { background: #37474f; color: #ccc; }
.badge-sale { background: var(--pk-red); color: #fff; font-size: .65rem; padding: 1px 5px; border-radius: 3px; font-weight: 700; }
.badge-psa  { background: var(--pk-gold); color: #000; font-size: .65rem; padding: 1px 5px; border-radius: 3px; font-weight: 700; }
.badge-priority { font-size: .65rem; padding: 1px 5px; border-radius: 3px; }

/* Search */
.search-box { background: var(--pk-dark); border: 1px solid var(--pk-border); color: var(--pk-text); border-radius: 8px; padding: .4rem .8rem; font-size: .85rem; width: 100%; outline: none; }
.search-box:focus { border-color: var(--pk-red); }
select.search-box { cursor: pointer; }
.search-row { display: flex; gap: .5rem; flex-wrap: wrap; margin-bottom: .8rem; }
.search-row .search-box { flex: 1; min-width: 140px; }

/* Pagination */
.pag { display: flex; gap: .3rem; justify-content: center; margin-top: .8rem; flex-wrap: wrap; }
.pag-btn { background: var(--pk-card2); border: 1px solid var(--pk-border); color: var(--pk-text); border-radius: 6px; padding: 3px 10px; font-size: .8rem; cursor: pointer; }
.pag-btn:hover  { border-color: var(--pk-red); }
.pag-btn.active { background: var(--pk-red); border-color: var(--pk-red); color: #fff; }

/* Spinner */
.spin-wrap { text-align: center; padding: 2rem; color: var(--pk-muted); font-size: .9rem; }
.spin { display: inline-block; animation: spin 1s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }

/* ROI */
.pos { color: #66bb6a; }
.neg { color: #ef5350; }
.neu { color: var(--pk-muted); }

/* Rank badge */
.rank { color: var(--pk-muted); font-size: .8rem; min-width: 22px; }
.rank-1 { color: var(--pk-gold); }
.rank-2 { color: #b0bec5; }
.rank-3 { color: #cd7f32; }

/* Hide helper */
.d-none { display: none !important; }

::-webkit-scrollbar { width: 5px; height: 5px; }
::-webkit-scrollbar-track { background: var(--pk-dark); }
::-webkit-scrollbar-thumb { background: var(--pk-border); border-radius: 3px; }
</style>
</head>
<body>

<!-- Navbar -->
<nav class="navbar d-flex justify-content-between align-items-center">
  <div class="nb-logo">⚡ PokeDex Price <small>Web Dashboard</small></div>
  <div class="d-flex align-items-center gap-2">
    <span id="last-upd" style="color:var(--pk-muted);font-size:.72rem"></span>
    <button class="btn-refresh" onclick="loadAll()"><i class="bi bi-arrow-clockwise"></i> Refresh</button>
  </div>
</nav>

<!-- UID Bar -->
<div id="uid-bar">
  <label>👤 User:</label>
  <select id="uid-select" onchange="loadAll()">
    <option value="">Semua User</option>
  </select>
  <span id="uid-info" style="color:var(--pk-muted);font-size:.75rem"></span>
</div>

<!-- Stat Cards -->
<div class="stats-grid" id="stats-row">
  <div class="scard">
    <div class="lbl">💰 Total Value</div>
    <div class="val c-gold" id="s-usd">—</div>
    <div class="sub"   id="s-idr">—</div>
  </div>
  <div class="scard">
    <div class="lbl">📈 ROI / Profit</div>
    <div class="val"   id="s-roi">—</div>
    <div class="sub"   id="s-profit">—</div>
  </div>
  <div class="scard">
    <div class="lbl">🃏 Koleksi</div>
    <div class="val c-blue" id="s-cards">—</div>
    <div class="sub"        id="s-sets">—</div>
  </div>
  <div class="scard">
    <div class="lbl">🏷️ Dijual</div>
    <div class="val c-red" id="s-fs">—</div>
    <div class="sub"       id="s-fsidr">—</div>
  </div>
</div>

<!-- Tabs -->
<div class="tabs-row">
  <button class="tab-btn active" data-tab="overview">📊 Overview</button>
  <button class="tab-btn"        data-tab="collection">🃏 Koleksi</button>
  <button class="tab-btn"        data-tab="forsale">🏷️ Dijual</button>
  <button class="tab-btn"        data-tab="trades">💸 Sales</button>
  <button class="tab-btn"        data-tab="wishlist">⭐ Wishlist</button>
</div>

<!-- ═══════════ TAB: OVERVIEW ═══════════ -->
<div class="content" id="tab-overview">

  <div class="charts-grid">
    <div class="box">
      <div class="box-title">📈 Portfolio Value — 30 Hari</div>
      <div style="position:relative;height:200px"><canvas id="chart-portfolio"></canvas></div>
      <div id="chart-portfolio-empty" class="d-none spin-wrap" style="height:200px;padding-top:70px">
        Belum ada data snapshot.<br><small>Bot otomatis snapshot setiap hari.</small>
      </div>
    </div>
    <div class="box">
      <div class="box-title">🥧 Per Kondisi</div>
      <div style="position:relative;height:200px"><canvas id="chart-condition"></canvas></div>
    </div>
  </div>

  <div class="charts-grid-2">
    <div class="box">
      <div class="box-title">📂 Value Per Set (Top 8)</div>
      <div style="position:relative;height:180px"><canvas id="chart-sets"></canvas></div>
    </div>
    <div class="box">
      <div class="box-title">🏆 Top 10 Kartu</div>
      <div id="top-cards"></div>
    </div>
  </div>

</div>

<!-- ═══════════ TAB: KOLEKSI ═══════════ -->
<div class="content d-none" id="tab-collection">
  <div class="box">
    <div class="search-row">
      <input type="text"   id="sq"    class="search-box" placeholder="🔍 Cari nama kartu..." oninput="debSearch()">
      <select              id="sset"  class="search-box" onchange="doSearch(1)" style="max-width:180px">
        <option value="">Semua Set</option>
      </select>
    </div>
    <div id="cards-wrap"><div class="spin-wrap"><span class="spin">⚽</span> Loading...</div></div>
    <div id="cards-pag" class="pag"></div>
  </div>
</div>

<!-- ═══════════ TAB: FOR SALE ═══════════ -->
<div class="content d-none" id="tab-forsale">
  <div class="box">
    <div class="box-title">🏷️ Kartu Dijual</div>
    <div id="fs-wrap"><div class="spin-wrap"><span class="spin">⚽</span> Loading...</div></div>
  </div>
</div>

<!-- ═══════════ TAB: SALES ═══════════ -->
<div class="content d-none" id="tab-trades">
  <div class="box">
    <div class="box-title">💸 Riwayat Penjualan (20 Terakhir)</div>
    <div id="trades-wrap"><div class="spin-wrap"><span class="spin">⚽</span> Loading...</div></div>
  </div>
</div>

<!-- ═══════════ TAB: WISHLIST ═══════════ -->
<div class="content d-none" id="tab-wishlist">
  <div class="box">
    <div class="box-title">⭐ Wishlist</div>
    <div id="wish-wrap"><div class="spin-wrap"><span class="spin">⚽</span> Loading...</div></div>
  </div>
</div>

<script>
const RATE = """ + str(RATE) + r""";
let charts = {};
let curPage = 1;
let searchTimer;
let activeTab = 'overview';

// ── Utils ─────────────────────────────────────────────────────────────────────
const $ = id => document.getElementById(id);
const uid = () => $('uid-select').value;

function fmt(n, d=2) {
  if (n == null) return '—';
  const v = parseFloat(n);
  if (isNaN(v)) return '—';
  return '$' + v.toFixed(d).replace(/\B(?=(\d{3})+(?!\d))/g,',');
}
function fmtIdr(usd) {
  const v = parseFloat(usd||0) * RATE;
  if (v >= 1000000) return 'Rp ' + (v/1000000).toFixed(1) + 'jt';
  if (v >= 1000)    return 'Rp ' + (v/1000).toFixed(0) + 'rb';
  return 'Rp ' + v.toFixed(0);
}
function condBadge(c) {
  const m = {'Mint':'b-mint','Near Mint':'b-nm','Lightly Played':'b-lp',
             'Moderately Played':'b-mp','Heavily Played':'b-hp','Damaged':'b-dmg'};
  const s = {'Near Mint':'NM','Lightly Played':'LP','Moderately Played':'MP',
             'Heavily Played':'HP','Damaged':'DMG','Mint':'M'};
  return `<span class="badge-cond ${m[c]||'b-unk'}">${s[c]||c||'?'}</span>`;
}
function roiClass(r) { return r > 0 ? 'pos' : r < 0 ? 'neg' : 'neu'; }
function roiStr(r)   { return r == null ? '—' : (r>0?'+':'')+r.toFixed(1)+'%'; }
function calcRoi(price, buy) {
  if (!buy || buy <= 0) return null;
  return ((price - buy) / buy * 100);
}
const q = () => { const p = new URLSearchParams(); if(uid()) p.set('uid',uid()); return p; };

// ── Users ─────────────────────────────────────────────────────────────────────
async function loadUsers() {
  const data = await fetch('/api/users').then(r=>r.json()).catch(()=>[]);
  const sel = $('uid-select');
  data.forEach(u => {
    const o = document.createElement('option');
    o.value = u.user_id; o.textContent = `ID ${u.user_id} (${u.cards} kartu)`;
    sel.appendChild(o);
  });
  if (data.length === 1) sel.value = data[0].user_id;
}

// ── Stats ─────────────────────────────────────────────────────────────────────
async function loadStats() {
  const p = q(); const s = await fetch('/api/stats?'+p).then(r=>r.json()).catch(()=>({}));
  $('s-usd').textContent    = fmt(s.total_usd);
  $('s-idr').textContent    = fmtIdr(s.total_usd||0);
  const roi = s.roi||0;
  $('s-roi').textContent    = (roi>0?'+':'')+roi.toFixed(1)+'%';
  $('s-roi').className      = 'val ' + roiClass(roi);
  const ps = s.profit_usd||0;
  $('s-profit').textContent = (ps>=0?'+':'')+fmt(ps) + ' profit';
  $('s-cards').textContent  = (s.cards||0) + ' kartu';
  $('s-sets').textContent   = (s.sets||0) + ' set · PSA ' + (s.psa_graded||0);
  $('s-fs').textContent     = (s.fs_cnt||0) + ' kartu';
  $('s-fsidr').textContent  = fmt(s.fs_ask||0) + ' ask total';
  $('last-upd').textContent = new Date().toLocaleTimeString('id-ID');
}

// ── Charts ────────────────────────────────────────────────────────────────────
const C = { // chart defaults
  responsive: true, maintainAspectRatio: false,
  plugins: { legend: { labels: { color:'#7878a0', font:{size:10} } } }
};

async function loadPortfolioChart() {
  const p = q(); p.set('days','30');
  const data = await fetch('/api/portfolio_history?'+p).then(r=>r.json()).catch(()=>[]);
  const canvas = $('chart-portfolio');
  const empty  = $('chart-portfolio-empty');

  if (!data.length) {
    canvas.classList.add('d-none');
    empty.classList.remove('d-none');
    return;
  }
  canvas.classList.remove('d-none');
  empty.classList.add('d-none');

  if (charts.portfolio) charts.portfolio.destroy();
  charts.portfolio = new Chart(canvas.getContext('2d'), {
    type: 'line',
    data: {
      labels: data.map(d=>d.date),
      datasets: [{
        label: 'Portfolio (USD)',
        data: data.map(d=>d.value),
        borderColor: '#e53935',
        backgroundColor: 'rgba(229,57,53,.12)',
        fill: true, tension: .4,
        pointRadius: data.length < 15 ? 4 : 2,
        pointBackgroundColor: '#e53935'
      }]
    },
    options: {
      ...C,
      scales: {
        x: { ticks:{color:'#555',maxTicksLimit:7,font:{size:9}}, grid:{color:'#1e1e36'} },
        y: { ticks:{color:'#555',callback:v=>'$'+v.toFixed(0)}, grid:{color:'#1e1e36'} }
      },
      plugins: { ...C.plugins, tooltip:{callbacks:{label:c=>fmt(c.parsed.y)}} }
    }
  });
}

async function loadConditionChart() {
  const p = q();
  const data = await fetch('/api/by_condition?'+p).then(r=>r.json()).catch(()=>[]);
  if (charts.cond) charts.cond.destroy();
  charts.cond = new Chart($('chart-condition').getContext('2d'), {
    type: 'doughnut',
    data: {
      labels: data.map(d=>d.cond + ' ('+d.cnt+')'),
      datasets: [{ data: data.map(d=>d.total_usd),
        backgroundColor: ['#00897b','#1e88e5','#43a047','#fb8c00','#e53935','#8e24aa','#fdd83566'],
        borderWidth: 0
      }]
    },
    options: { ...C, plugins: { ...C.plugins,
      tooltip: { callbacks:{ label: c => c.label+': '+fmt(c.parsed) } }
    }}
  });
}

async function loadSetsChart() {
  const p = q();
  const data = (await fetch('/api/by_set?'+p).then(r=>r.json()).catch(()=>[])).slice(0,8);
  if (charts.sets) charts.sets.destroy();
  charts.sets = new Chart($('chart-sets').getContext('2d'), {
    type: 'bar',
    data: {
      labels: data.map(d => d.set_name.length>14 ? d.set_name.slice(0,14)+'…' : d.set_name),
      datasets: [{ label:'USD', data: data.map(d=>d.total_usd),
        backgroundColor:'rgba(229,57,53,.7)', borderColor:'#e53935',
        borderWidth:1, borderRadius:4
      }]
    },
    options: { ...C,
      scales: {
        x: { ticks:{color:'#555',font:{size:9}}, grid:{display:false} },
        y: { ticks:{color:'#555',callback:v=>'$'+v}, grid:{color:'#1e1e36'} }
      }
    }
  });
}

async function loadTopCards() {
  const p = q(); p.set('limit','10');
  const data = await fetch('/api/top_cards?'+p).then(r=>r.json()).catch(()=>[]);
  const rankCls = ['','rank-1','rank-2','rank-3'];
  const html = data.map((c,i) => {
    const roi = calcRoi(c.price_usd, c.buy_price_usd);
    const fsTag  = c.for_sale  ? '<span class="badge-sale ms-1">SALE</span>' : '';
    const psaTag = c.psa_grade ? `<span class="badge-psa ms-1">PSA ${c.psa_grade}</span>` : '';
    return `<div style="display:flex;align-items:center;gap:.5rem;padding:.4rem 0;border-bottom:1px solid var(--pk-border)">
      <span class="rank ${rankCls[i+1]||''}">#${i+1}</span>
      <div style="flex:1;overflow:hidden">
        <div style="font-size:.82rem;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">
          ${c.card_name}${fsTag}${psaTag}
        </div>
        <div style="font-size:.72rem;color:var(--pk-muted)">${c.card_set||'—'} · ${condBadge(c.condition)}</div>
      </div>
      <div style="text-align:right">
        <div style="font-size:.85rem;font-weight:700;color:var(--pk-gold)">${fmt(c.price_usd)}</div>
        ${roi!=null ? `<div style="font-size:.72rem" class="${roiClass(roi)}">${roiStr(roi)}</div>` : ''}
      </div>
    </div>`;
  }).join('');
  $('top-cards').innerHTML = html || '<div class="spin-wrap">Koleksi kosong</div>';
}

// ── Collection Tab ────────────────────────────────────────────────────────────
async function loadSetsList() {
  const p = q();
  const sets = await fetch('/api/sets_list?'+p).then(r=>r.json()).catch(()=>[]);
  const sel = $('sset');
  sel.innerHTML = '<option value="">Semua Set</option>';
  sets.forEach(s => { const o = document.createElement('option'); o.value=o.textContent=s; sel.appendChild(o); });
}

async function doSearch(page=1) {
  curPage = page;
  const p = q();
  const qv = $('sq').value;
  const sv = $('sset').value;
  if (qv) p.set('q', qv);
  if (sv) p.set('set', sv);
  p.set('page', page);
  $('cards-wrap').innerHTML = '<div class="spin-wrap"><span class="spin">⚽</span></div>';
  const data = await fetch('/api/cards?'+p).then(r=>r.json()).catch(()=>({cards:[],total:0,pages:1}));
  const rows = data.cards.map(c => {
    const roi = calcRoi(c.price_usd, c.buy_price_usd);
    const fsTag  = c.for_sale  ? '<span class="badge-sale ms-1">SALE</span>' : '';
    const psaTag = c.psa_grade ? `<span class="badge-psa ms-1">PSA ${c.psa_grade}</span>` : '';
    const noteRow = c.notes ? `<tr><td colspan="6" style="padding-top:0;padding-bottom:.4rem">
      <small style="color:var(--pk-gold);opacity:.8">📝 ${c.notes}</small></td></tr>` : '';
    return `<tr>
      <td class="c-muted" style="font-size:.75rem">#${c.id}</td>
      <td><span style="font-weight:600">${c.card_name}</span>${fsTag}${psaTag}</td>
      <td class="c-muted" style="font-size:.75rem">${c.card_set||'—'}</td>
      <td>${condBadge(c.condition)}</td>
      <td class="text-right" style="font-weight:700;color:var(--pk-gold)">${fmt(c.price_usd)}</td>
      <td class="text-right ${roi!=null?roiClass(roi):'c-muted'}">${roiStr(roi)}</td>
    </tr>${noteRow}`;
  }).join('');
  const tbl = `<div style="overflow-x:auto"><table class="pk-table">
    <thead><tr><th>#</th><th>Kartu</th><th>Set</th><th>Kondisi</th><th class="text-right">Harga</th><th class="text-right">ROI</th></tr></thead>
    <tbody>${rows||'<tr><td colspan="6" class="spin-wrap">Tidak ada kartu</td></tr>'}</tbody>
  </table></div>`;
  $('cards-wrap').innerHTML = tbl;
  $('cards-pag').innerHTML = Array.from({length:data.pages},(_, i)=>
    `<button class="pag-btn ${i+1===page?'active':''}" onclick="doSearch(${i+1})">${i+1}</button>`
  ).join('');
}
function debSearch() { clearTimeout(searchTimer); searchTimer = setTimeout(()=>doSearch(1), 350); }

// ── For Sale Tab ──────────────────────────────────────────────────────────────
async function loadForSale() {
  const p = q();
  const data = await fetch('/api/for_sale?'+p).then(r=>r.json()).catch(()=>[]);
  if (!data.length) { $('fs-wrap').innerHTML='<div class="spin-wrap">Tidak ada kartu yang dijual 🏷️</div>'; return; }
  let total = 0;
  const rows = data.map(c => {
    const base = c.buy_price_usd || c.price_usd || 0;
    const profit = (c.ask_price_usd||0) - base;
    const ps = profit >= 0 ? '+' : '';
    const psaTag = c.psa_grade ? `<span class="badge-psa ms-1">PSA ${c.psa_grade}</span>` : '';
    total += (c.ask_price_usd||0);
    return `<tr>
      <td><div style="font-weight:600">${c.card_name}${psaTag}</div>
          <div class="c-muted" style="font-size:.72rem">${c.card_set||'—'}
            ${c.notes?`<br><span style="color:var(--pk-gold)">📝 ${c.notes}</span>`:''}
          </div></td>
      <td>${condBadge(c.condition)}</td>
      <td class="text-right" style="font-weight:700;color:var(--pk-gold)">${fmt(c.ask_price_usd)}</td>
      <td class="text-right c-muted">${fmt(c.price_usd)}</td>
      <td class="text-right ${profit>=0?'pos':'neg'}">${ps}${fmt(profit)}</td>
    </tr>`;
  }).join('');
  $('fs-wrap').innerHTML = `<div style="overflow-x:auto"><table class="pk-table">
    <thead><tr><th>Kartu</th><th>Kondisi</th><th class="text-right">Ask</th><th class="text-right">Market</th><th class="text-right">Profit</th></tr></thead>
    <tbody>${rows}</tbody>
    <tfoot><tr style="border-top:2px solid var(--pk-border)">
      <td colspan="2" class="c-muted">Total ${data.length} kartu</td>
      <td class="text-right" style="font-weight:700;color:var(--pk-gold)">${fmt(total)}</td>
      <td class="text-right c-muted">${fmtIdr(total)}</td><td></td>
    </tr></tfoot>
  </table></div>`;
}

// ── Trades Tab ────────────────────────────────────────────────────────────────
async function loadTrades() {
  const p = q();
  const data = await fetch('/api/recent_trades?'+p).then(r=>r.json()).catch(()=>[]);
  if (!data.length) { $('trades-wrap').innerHTML='<div class="spin-wrap">Belum ada riwayat penjualan 💸</div>'; return; }
  let totalSell=0, totalProfit=0;
  const rows = data.map(c => {
    const profit = (c.sell_price_usd||0) - (c.buy_price_usd||0);
    totalSell += (c.sell_price_usd||0); totalProfit += profit;
    return `<tr>
      <td style="font-weight:600">${c.card_name}</td>
      <td class="text-right" style="color:var(--pk-gold)">${fmt(c.sell_price_usd)}</td>
      <td class="text-right c-muted">${fmt(c.buy_price_usd)}</td>
      <td class="text-right ${profit>=0?'pos':'neg'}">${profit>=0?'+':''}${fmt(profit)}</td>
      <td class="c-muted" style="font-size:.75rem">${(c.sold_at||'').slice(0,10)||'—'}</td>
    </tr>`;
  }).join('');
  $('trades-wrap').innerHTML = `<div style="overflow-x:auto"><table class="pk-table">
    <thead><tr><th>Kartu</th><th class="text-right">Jual</th><th class="text-right">Beli</th><th class="text-right">Profit</th><th>Tgl</th></tr></thead>
    <tbody>${rows}</tbody>
    <tfoot><tr style="border-top:2px solid var(--pk-border)">
      <td class="c-muted">Total ${data.length} transaksi</td>
      <td class="text-right" style="font-weight:700;color:var(--pk-gold)">${fmt(totalSell)}</td>
      <td></td>
      <td class="text-right ${totalProfit>=0?'pos':'neg'}" style="font-weight:700">${totalProfit>=0?'+':''}${fmt(totalProfit)}</td>
      <td></td>
    </tr></tfoot>
  </table></div>`;
}

// ── Wishlist Tab ──────────────────────────────────────────────────────────────
async function loadWishlist() {
  const p = q();
  const data = await fetch('/api/wishlist?'+p).then(r=>r.json()).catch(()=>[]);
  if (!data.length) { $('wish-wrap').innerHTML='<div class="spin-wrap">Wishlist kosong ⭐</div>'; return; }
  const prioMap = {3:'🔴 Tinggi',2:'🟡 Sedang',1:'🟢 Rendah'};
  const rows = data.map(c => `<tr>
    <td style="font-weight:600">${c.card_name}
      ${c.notes ? `<div class="c-muted" style="font-size:.72rem">📝 ${c.notes}</div>` : ''}
    </td>
    <td class="c-muted" style="font-size:.75rem">${c.card_set||'—'}</td>
    <td class="text-right" style="color:var(--pk-gold)">${c.target_price_usd ? fmt(c.target_price_usd) : '—'}</td>
    <td><span class="badge-priority">${prioMap[c.priority]||'—'}</span></td>
    <td class="c-muted" style="font-size:.72rem">${(c.added_at||'').slice(0,10)||'—'}</td>
  </tr>`).join('');
  $('wish-wrap').innerHTML = `<div style="overflow-x:auto"><table class="pk-table">
    <thead><tr><th>Kartu</th><th>Set</th><th class="text-right">Target</th><th>Prioritas</th><th>Tgl</th></tr></thead>
    <tbody>${rows}</tbody>
  </table></div>`;
}

// ── Tab Switch ────────────────────────────────────────────────────────────────
document.querySelectorAll('.tab-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    activeTab = btn.dataset.tab;
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    ['overview','collection','forsale','trades','wishlist'].forEach(t => {
      $('tab-'+t).classList.toggle('d-none', t !== activeTab);
    });
    if (activeTab === 'collection') { loadSetsList(); doSearch(1); }
    if (activeTab === 'forsale')    loadForSale();
    if (activeTab === 'trades')     loadTrades();
    if (activeTab === 'wishlist')   loadWishlist();
  });
});

// ── Load All ──────────────────────────────────────────────────────────────────
async function loadAll() {
  loadStats();
  if (activeTab === 'overview') {
    loadPortfolioChart(); loadConditionChart(); loadSetsChart(); loadTopCards();
  } else if (activeTab === 'collection') {
    loadSetsList(); doSearch(curPage);
  } else if (activeTab === 'forsale')  loadForSale();
  else if (activeTab === 'trades')    loadTrades();
  else if (activeTab === 'wishlist')  loadWishlist();
}

// ── Init ──────────────────────────────────────────────────────────────────────
(async () => {
  await loadUsers();
  await loadAll();
  setInterval(loadAll, 60000); // auto-refresh 60s
})();
</script>
</body>
</html>"""

# ─── Entry Point ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    db_status = "✅ ditemukan" if os.path.exists(DB_PATH) else "❌ TIDAK ditemukan"
    print(f"""
╔════════════════════════════════════════╗
║  🎴  PokeDex Price — Web Dashboard    ║
╚════════════════════════════════════════╝
  Database : {DB_PATH}
             {db_status}
  Rate     : Rp {RATE:,} / USD
  Port     : {PORT}

  Buka di browser HP  : http://localhost:{PORT}
  Dari HP lain (WiFi) : http://<IP-lokal>:{PORT}
  Cari IP lokal       : ip addr | grep 192

  Tekan Ctrl+C untuk stop.
""")
    if not os.path.exists(DB_PATH):
        print("  ⚠️  Database tidak ditemukan! Coba:")
        print("  DB_PATH=/path/ke/pokedex.db python3 dashboard.py")
        print("  Atau cari dengan: find ~ -name 'pokedex.db' 2>/dev/null\n")

    app.run(host="0.0.0.0", port=PORT, debug=False)
