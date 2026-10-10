import asyncio
import csv
import difflib
import io
import json
import logging
import os
import re
from datetime import datetime, timedelta

import aiosqlite
import httpx
from dotenv import load_dotenv
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

try:
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    HAS_MATPLOTLIB = True
except ImportError:
    HAS_MATPLOTLIB = False

try:
    import pytesseract
    from PIL import ImageFilter, ImageEnhance
    HAS_TESSERACT = True
except ImportError:
    HAS_TESSERACT = False

load_dotenv(dotenv_path=Path(__file__).parent / ".env", override=True)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.DEBUG,
)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)

TELEGRAM_TOKEN      = os.getenv("TELEGRAM_TOKEN")
EXCHANGE_RATE       = int(os.getenv("EXCHANGE_RATE", 16000))
DB_PATH             = os.getenv("DB_PATH", "pokemon_inventory.db")
POKEMON_TCG_API_KEY = os.getenv("POKEMON_TCG_API_KEY", "")

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN wajib diisi di .env!")

CARD_CONDITIONS = [
    "Mint", "Near Mint", "Lightly Played",
    "Moderately Played", "Heavily Played", "Damaged"
]

CONDITION_MULTIPLIERS = {
    "Mint": 1.0,
    "Near Mint": 1.0,
    "Lightly Played": 0.80,
    "Moderately Played": 0.65,
    "Heavily Played": 0.50,
    "Damaged": 0.25,
}

# ── MarkdownV2 escape ─────────────────────────────────────────────────────────
def esc(text: str) -> str:
    return re.sub(r'([_*\[\]()~`>#\+\-=|{}.!\\])', r'\\\1', str(text))

def esc_usd(v: float) -> str:
    """MarkdownV2-safe USD price string, e.g. esc_usd(5.5) → '\\$5\\.50'"""
    return "\\$" + esc(f"{v:.2f}")


# ── Database ──────────────────────────────────────────────────────────────────
async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS inventory (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id   INTEGER NOT NULL,
                card_name TEXT    NOT NULL,
                card_set  TEXT,
                price_usd REAL    DEFAULT 0.0,
                price_idr REAL    DEFAULT 0.0,
                condition TEXT    DEFAULT 'Near Mint',
                psa_grade TEXT    DEFAULT NULL
            )
        """)
        for col, definition in [
            ("condition",          "TEXT DEFAULT 'Near Mint'"),
            ("psa_grade",          "TEXT DEFAULT NULL"),
            ("buy_price_usd",      "REAL DEFAULT 0.0"),
            ("photo_file_id",      "TEXT DEFAULT NULL"),
            ("photo_file_id_back", "TEXT DEFAULT NULL"),
            ("tags",               "TEXT DEFAULT NULL"),
            ("notes",              "TEXT DEFAULT NULL"),
            ("for_sale",           "INTEGER DEFAULT 0"),
            ("ask_price_usd",      "REAL DEFAULT 0.0"),
        ]:
            try:
                await db.execute(f"ALTER TABLE inventory ADD COLUMN {col} {definition}")
            except Exception:
                pass

        await db.execute("""
            CREATE TABLE IF NOT EXISTS wishlist (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id          INTEGER NOT NULL,
                card_name        TEXT    NOT NULL,
                card_set         TEXT,
                price_usd        REAL    DEFAULT 0.0,
                price_idr        REAL    DEFAULT 0.0,
                target_price_usd REAL    DEFAULT 0.0,
                added_at         TEXT    DEFAULT CURRENT_TIMESTAMP
            )
        """)
        try:
            await db.execute("ALTER TABLE wishlist ADD COLUMN target_price_usd REAL DEFAULT 0.0")
        except Exception:
            pass

        await db.execute("""
            CREATE TABLE IF NOT EXISTS price_history (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                card_name   TEXT    NOT NULL,
                card_set    TEXT,
                price_usd   REAL    DEFAULT 0.0,
                price_idr   REAL    DEFAULT 0.0,
                recorded_at TEXT    DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS price_alerts (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id       INTEGER NOT NULL,
                card_name     TEXT    NOT NULL,
                threshold_usd REAL    NOT NULL,
                alert_type    TEXT    DEFAULT 'turun',
                created_at    TEXT    DEFAULT CURRENT_TIMESTAMP
            )
        """)
        try:
            await db.execute("ALTER TABLE price_alerts ADD COLUMN alert_type TEXT DEFAULT 'turun'")
        except Exception:
            pass

        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id INTEGER PRIMARY KEY,
                lang    TEXT DEFAULT 'id'
            )
        """)

        # ── Trade log (fitur /jual) ────────────────────────────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS trade_log (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id       INTEGER NOT NULL,
                card_name     TEXT    NOT NULL,
                card_set      TEXT,
                sell_price_usd REAL   DEFAULT 0.0,
                sell_price_idr REAL   DEFAULT 0.0,
                buy_price_usd  REAL   DEFAULT 0.0,
                profit_usd     REAL   DEFAULT 0.0,
                sold_at        TEXT   DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # ── Card name cache (fitur autocomplete & /gen) ────────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS card_cache (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT    NOT NULL,
                card_set   TEXT,
                set_series TEXT,
                set_id     TEXT,
                price_usd  REAL    DEFAULT 0.0,
                release_date TEXT,
                updated_at TEXT    DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_card_cache_name ON card_cache(LOWER(name))")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_card_cache_series ON card_cache(set_series)")

        # ── Portfolio value snapshot (fitur /portohistory) ─────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS portfolio_snapshots (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL,
                total_usd  REAL    DEFAULT 0.0,
                total_idr  REAL    DEFAULT 0.0,
                card_count INTEGER DEFAULT 0,
                snapped_at TEXT    DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_snap_user ON portfolio_snapshots(user_id, snapped_at)"
        )

        # ── Persistent user state (survive bot restart) ──────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_states (
                user_id    INTEGER NOT NULL,
                state_key  TEXT    NOT NULL,
                state_val  TEXT    NOT NULL,
                updated_at TEXT    DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, state_key)
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_ustate_user ON user_states(user_id)")

        # ── Daily report opt-in columns ──────────────────────────────────────
        for col, definition in [
            ("daily_enabled", "INTEGER DEFAULT 0"),
            ("daily_chat_id", "INTEGER DEFAULT NULL"),
        ]:
            try:
                await db.execute(f"ALTER TABLE user_settings ADD COLUMN {col} {definition}")
            except Exception:
                pass

        # ── Reminders (fitur /remind) ─────────────────────────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS reminders (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                card_id     INTEGER NOT NULL,
                card_name   TEXT    NOT NULL,
                remind_at   TEXT    NOT NULL,
                sent        INTEGER DEFAULT 0,
                created_at  TEXT    DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_reminders_user ON reminders(user_id, remind_at, sent)")

        # ── Tong sampah / soft-delete (fitur /trash + /restore) ──────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS deleted_inventory (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                orig_id            INTEGER,
                user_id            INTEGER NOT NULL,
                card_name          TEXT    NOT NULL,
                card_set           TEXT,
                price_usd          REAL    DEFAULT 0.0,
                price_idr          REAL    DEFAULT 0.0,
                condition          TEXT    DEFAULT 'Near Mint',
                psa_grade          TEXT,
                buy_price_usd      REAL    DEFAULT 0.0,
                photo_file_id      TEXT,
                photo_file_id_back TEXT,
                tags               TEXT,
                deleted_at         TEXT    DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_trash_user ON deleted_inventory(user_id, deleted_at)"
        )

        # ── v11 — folder koleksi & auto-refresh setting ───────────────────────
        for col, definition in [
            ("folder", "TEXT DEFAULT 'Pribadi'"),
        ]:
            try:
                await db.execute(f"ALTER TABLE inventory ADD COLUMN {col} {definition}")
            except Exception:
                pass
        for col, definition in [
            ("autorefresh_enabled", "INTEGER DEFAULT 0"),
            ("autorefresh_chat_id", "INTEGER DEFAULT NULL"),
        ]:
            try:
                await db.execute(f"ALTER TABLE user_settings ADD COLUMN {col} {definition}")
            except Exception:
                pass

        await db.commit()

# ── Helper: get user language ─────────────────────────────────────────────────
async def get_user_lang(user_id: int) -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT lang FROM user_settings WHERE user_id=?", (user_id,)) as cur:
            row = await cur.fetchone()
    return row[0] if row else "id"

# ── Persistent user state helpers ────────────────────────────────────────────
async def get_ustate(user_id: int, key: str):
    """Ambil pending state dari SQLite. Return None kalau tidak ada."""
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT state_val FROM user_states WHERE user_id=? AND state_key=?",
                (user_id, key)
            ) as cur:
                row = await cur.fetchone()
        if row is None:
            logger.debug(f"[ustate] GET {user_id}/{key} → None")
            return None
        try:
            val = json.loads(row[0])
        except Exception:
            val = row[0]
        logger.debug(f"[ustate] GET {user_id}/{key} → {repr(val)}")
        return val
    except Exception as e:
        logger.error(f"[ustate] GET ERROR {user_id}/{key}: {e}")
        return None

async def set_ustate(user_id: int, key: str, value) -> None:
    """Simpan pending state ke SQLite."""
    logger.debug(f"[ustate] SET {user_id}/{key} = {repr(value)}")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT OR REPLACE INTO user_states (user_id, state_key, state_val, updated_at)
               VALUES (?, ?, ?, datetime('now'))""",
            (user_id, key, json.dumps(value))
        )
        await db.commit()

async def del_ustate(user_id: int, key: str) -> None:
    """Hapus satu pending state."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM user_states WHERE user_id=? AND state_key=?",
            (user_id, key)
        )
        await db.commit()

async def pop_ustate(user_id: int, key: str, default=None):
    """Ambil dan hapus pending state (atomic get-then-delete)."""
    value = await get_ustate(user_id, key)
    if value is not None:
        await del_ustate(user_id, key)
        return value
    return default

async def clear_ustate(user_id: int, keys: list) -> None:
    """Hapus beberapa pending state sekaligus."""
    async with aiosqlite.connect(DB_PATH) as db:
        for key in keys:
            await db.execute(
                "DELETE FROM user_states WHERE user_id=? AND state_key=?",
                (user_id, key)
            )
        await db.commit()

# ── Parse harga Rupiah (support: 900000 / 900ribu / 900rb / 1jt / 1.5jt / 900k) ──
def parse_rupiah(text: str) -> float:
    """
    Parse angka Rupiah dari berbagai format:
      900000       → 900000.0
      900.000      → 900000.0 (dot sebagai pemisah ribuan IDR)
      900,000      → 900000.0
      900ribu      → 900000.0
      900rb / 900k → 900000.0
      1jt / 1juta  → 1000000.0
      1.5jt        → 1500000.0
      Rp 900.000   → 900000.0
    """
    t = text.strip().lower()
    t = re.sub(r'rp\.?\s*', '', t)   # hapus prefix Rp / Rp.

    multiplier = 1.0
    if re.search(r'juta|jt', t):
        multiplier = 1_000_000.0
        t = re.sub(r'juta|jt', '', t)
    elif re.search(r'ribu|rb|k(?!\w)', t):
        multiplier = 1_000.0
        t = re.sub(r'ribu|rb|k(?!\w)', '', t)

    # Normalise separators: jika ada titik DAN koma → titik = ribuan, koma = desimal
    if '.' in t and ',' in t:
        t = t.replace('.', '').replace(',', '.')
    elif ',' in t:
        # Cek apakah koma adalah pemisah ribuan (1,500) atau desimal (1,5)
        parts = t.split(',')
        if len(parts) == 2 and len(parts[1]) == 3:
            t = t.replace(',', '')   # 1,500 → 1500
        else:
            t = t.replace(',', '.')  # 1,5 → 1.5
    elif '.' in t:
        parts = t.split('.')
        # Jika ada lebih dari satu titik ATAU bagian setelah titik ada 3 digit → pemisah ribuan
        if len(parts) > 2 or (len(parts) == 2 and len(parts[1]) == 3 and multiplier == 1.0):
            t = t.replace('.', '')   # 900.000 → 900000 / 1.500.000 → 1500000
        # else: 1.5 → biarkan sebagai desimal

    t = re.sub(r'[^\d.]', '', t).strip()
    if not t:
        raise ValueError("Tidak ada angka yang bisa dibaca")
    return float(t) * multiplier

# ── Pokemon TCG API — helper: build headers ───────────────────────────────────
def _tcg_headers() -> dict:
    headers = {}
    if POKEMON_TCG_API_KEY:
        headers["X-Api-Key"] = POKEMON_TCG_API_KEY
    return headers

# ── TCGdex — image fallback ────────────────────────────────────────────────────
async def fetch_tcgdex_image(card_name: str, set_name: str = "") -> str | None:
    """
    Cari gambar dari tcgdex.dev sebagai fallback kalau pokemontcg.io tidak punya.
    Return URL gambar high-quality WebP, atau None kalau tidak ditemukan.
    """
    try:
        # Coba exact match dulu (case-sensitive), lalu contains match
        for query in [f"eq:{card_name}", card_name.split()[0]]:
            url = f"https://api.tcgdex.net/v2/en/cards?name={query}&limit=20"
            async with httpx.AsyncClient(timeout=10) as http:
                r = await http.get(url)
            if r.status_code != 200:
                continue
            cards = r.json()
            if not cards or not isinstance(cards, list):
                continue

            # Cari kartu yang punya gambar
            candidates = [c for c in cards if c.get("image")]
            if not candidates:
                continue

            # Kalau ada set_name, coba cocokkan dari card id (format: setId-localId)
            if set_name:
                set_lower = set_name.lower().replace(" ", "")
                for c in candidates:
                    card_id = c.get("id", "")
                    set_part = card_id.split("-")[0].lower() if "-" in card_id else ""
                    if set_part and set_part in set_lower or set_lower in set_part:
                        return f"{c['image']}/high.webp"

            # Fallback: pakai kandidat pertama yang ada gambarnya
            return f"{candidates[0]['image']}/high.webp"

    except Exception as e:
        logger.debug(f"TCGdex image fallback failed for '{card_name}': {e}")
    return None

async def _fill_missing_image(card: dict) -> dict:
    """Isi image yang kosong dari pokemontcg menggunakan tcgdex sebagai fallback."""
    if card.get("image"):
        return card
    img = await fetch_tcgdex_image(card.get("name", ""), card.get("set", ""))
    if img:
        card = dict(card)  # copy biar tidak mutate original
        card["image"] = img
    return card

# ── Pokemon TCG API — single best match ───────────────────────────────────────
async def search_pokemon_card(card_name: str) -> dict | None:
    clean  = card_name.strip()
    term   = clean.split()[0] if clean.split() else clean
    url    = f"https://api.pokemontcg.io/v2/cards?q=name:*{term}*&pageSize=20"

    try:
        async with httpx.AsyncClient(timeout=15) as http:
            r = await http.get(url, headers=_tcg_headers())
            if r.status_code == 429:
                return {"error": "rate_limit"}
            if r.status_code >= 500:
                return {"error": "api_down"}
            r.raise_for_status()
            cards_list = r.json().get("data")

        if not cards_list:
            return None

        selected  = cards_list[0]
        clean_low = clean.lower()
        for card in cards_list:
            title = card.get("name", "").lower()
            if "ex" in clean_low and "ex" in title:
                selected = card; break
            if " v" in clean_low and any(x in title for x in ["vmax", "vstar", " v"]):
                selected = card; break

        result = _extract_card(selected)
        return await _fill_missing_image(result)

    except httpx.TimeoutException:
        return {"error": "timeout"}
    except httpx.HTTPStatusError as e:
        if e.response.status_code >= 500:
            return {"error": "api_down"}
        return {"error": f"http_{e.response.status_code}"}
    except Exception as e:
        return {"error": str(e)}

# ── Pokemon TCG API — multi results ───────────────────────────────────────────
async def search_pokemon_cards_multi(card_name: str, limit: int = 5) -> list | dict | None:
    clean = card_name.strip()
    term  = clean.split()[0] if clean.split() else clean
    url   = f"https://api.pokemontcg.io/v2/cards?q=name:*{term}*&pageSize=20"

    try:
        async with httpx.AsyncClient(timeout=15) as http:
            r = await http.get(url, headers=_tcg_headers())
            if r.status_code == 429:
                return {"error": "rate_limit"}
            if r.status_code >= 500:
                return {"error": "api_down"}
            r.raise_for_status()
            cards_list = r.json().get("data")

        if not cards_list:
            return None

        extracted = [_extract_card(c) for c in cards_list[:limit]]
        return list(await asyncio.gather(*[_fill_missing_image(c) for c in extracted]))

    except httpx.TimeoutException:
        return {"error": "timeout"}
    except httpx.HTTPStatusError as e:
        if e.response.status_code >= 500:
            return {"error": "api_down"}
        return {"error": f"http_{e.response.status_code}"}
    except Exception as e:
        return {"error": str(e)}

# ── Pokemon TCG API — by set name ─────────────────────────────────────────────
async def search_pokemon_set(set_name: str, limit: int = 100) -> dict | None:
    url = f"https://api.pokemontcg.io/v2/cards?q=set.name:*{set_name}*&pageSize={limit}"
    try:
        async with httpx.AsyncClient(timeout=30) as http:
            r = await http.get(url, headers=_tcg_headers())
            if r.status_code == 429:
                return {"error": "rate_limit"}
            if r.status_code >= 500:
                return {"error": "api_down"}
            r.raise_for_status()
            data       = r.json()
            cards_list = data.get("data", [])
            total      = data.get("totalCount", len(cards_list))

        if not cards_list:
            return None

        filled = list(await asyncio.gather(*[_fill_missing_image(_extract_card(c)) for c in cards_list]))
        return {"cards": filled, "total": total}

    except httpx.TimeoutException:
        return {"error": "timeout"}
    except httpx.HTTPStatusError as e:
        if e.response.status_code >= 500:
            return {"error": "api_down"}
        return {"error": f"http_{e.response.status_code}"}
    except Exception as e:
        return {"error": str(e)}

# ── Pokemon TCG API — all versions of a card ──────────────────────────────────
async def search_all_versions(card_name: str) -> list | dict | None:
    clean = card_name.strip().split()[0] if card_name.strip() else card_name
    url   = f"https://api.pokemontcg.io/v2/cards?q=name:{clean}&pageSize=50"
    try:
        async with httpx.AsyncClient(timeout=20) as http:
            r = await http.get(url, headers=_tcg_headers())
            if r.status_code == 429:
                return {"error": "rate_limit"}
            if r.status_code >= 500:
                return {"error": "api_down"}
            r.raise_for_status()
            cards_list = r.json().get("data", [])

        if not cards_list:
            return None

        extracted     = list(await asyncio.gather(*[_fill_missing_image(_extract_card(c)) for c in cards_list]))
        with_price    = sorted([c for c in extracted if c["price_usd"] > 0], key=lambda x: x["price_usd"])
        without_price = [c for c in extracted if c["price_usd"] == 0]
        return with_price + without_price

    except httpx.TimeoutException:
        return {"error": "timeout"}
    except httpx.HTTPStatusError as e:
        if e.response.status_code >= 500:
            return {"error": "api_down"}
        return {"error": f"http_{e.response.status_code}"}
    except Exception as e:
        return {"error": str(e)}

def _extract_card(card: dict) -> dict:
    market_usd = 0.0
    prices     = card.get("tcgplayer", {}).get("prices", {})
    for ptype in ["holofoil", "normal", "reverseHolofoil", "1stEditionHolofoil", "unlimitedHolofoil"]:
        if ptype in prices:
            val = prices[ptype].get("market") or 0.0
            if val > 0:
                market_usd = val; break
    if market_usd == 0.0:
        for ptype in prices.values():
            if isinstance(ptype, dict) and ptype.get("market"):
                market_usd = ptype["market"]; break

    return {
        "name":      card.get("name", "Unknown"),
        "set":       card.get("set", {}).get("name", "Unknown"),
        "rarity":    card.get("rarity", "Common/Unspecified"),
        "price_usd": market_usd,
        "price_idr": market_usd * EXCHANGE_RATE,
        "image":     card.get("images", {}).get("large"),
    }

# ── Format pesan kartu ────────────────────────────────────────────────────────
def card_message(card: dict, label: str = "", show_save_hint: bool = False) -> str:
    suffix    = f" \\({label}\\)" if label else ""
    price_usd = esc_usd(card['price_usd']) if card["price_usd"] > 0 else "Tidak tersedia"
    price_idr = f"Rp {card['price_idr']:,.0f}" if card["price_idr"] > 0 else "Tidak tersedia"
    hint = "\n💡 _Tekan tombol di bawah untuk simpan ke inventory_" if show_save_hint else \
           f"\n💡 _Mau simpan? Ketik: /add {esc(card['name'])}_"
    return (
        f"✨ *{esc(card['name'])}*{suffix} ✨\n"
        f"📦 Set: {esc(card['set'])}\n"
        f"⭐ Rarity: {esc(card['rarity'])}\n\n"
        f"💰 *Estimasi Harga:*\n"
        f"• Internasional: {price_usd}\n"
        f"• Pasaran Lokal \\(IDR\\): {price_idr}"
        f"{hint}"
    )

async def send_card(
    update: Update,
    card: dict,
    label: str = "",
    context: ContextTypes.DEFAULT_TYPE | None = None,
    show_save_buttons: bool = False,
) -> None:
    """Kirim info kartu. Jika show_save_buttons=True, tampilkan tombol simpan ke inventory."""
    import uuid as _uuid

    markup = None
    if show_save_buttons and context is not None:
        key = _uuid.uuid4().hex[:12]
        context.bot_data[f"card_snap_{key}"] = card
        markup = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("💾 Simpan ke Inventory",   callback_data=f"snap_save:{key}"),
                InlineKeyboardButton("💰 Simpan + Set Modal",    callback_data=f"snap_buy:{key}"),
            ],
            [
                InlineKeyboardButton("⭐ Simpan ke Wishlist",    callback_data=f"snap_wish:{key}"),
            ],
        ])

    msg = card_message(card, label, show_save_hint=show_save_buttons)
    try:
        if card.get("image"):
            await update.message.reply_photo(
                photo=card["image"], caption=msg,
                parse_mode="MarkdownV2", reply_markup=markup,
            )
        else:
            await update.message.reply_text(msg, parse_mode="MarkdownV2", reply_markup=markup)
    except Exception as e:
        logger.error(f"MarkdownV2 error, fallback plain: {e}")
        plain = (
            f"✨ {card['name']} ✨\nSet: {card['set']}\nRarity: {card['rarity']}\n\n"
            f"Harga: ${card['price_usd']:.2f} | Rp {card['price_idr']:,.0f}"
        )
        if card.get("image"):
            await update.message.reply_photo(photo=card["image"], caption=plain, reply_markup=markup)
        else:
            await update.message.reply_text(plain, reply_markup=markup)


# ── Callback: Simpan dari foto/scan langsung ──────────────────────────────────
async def handle_snap_save(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query   = update.callback_query
    await query.answer()
    data    = query.data   # "snap_save:{key}" | "snap_buy:{key}" | "snap_wish:{key}"
    parts   = data.split(":", 1)
    action  = parts[0]
    key     = parts[1] if len(parts) > 1 else ""
    user_id = update.effective_user.id

    card = context.bot_data.get(f"card_snap_{key}")
    if not card:
        await query.answer("⚠️ Data kartu sudah expired, cari ulang.", show_alert=True)
        return

    if action == "snap_wish":
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO wishlist (user_id, card_name, card_set, price_usd, price_idr) VALUES (?,?,?,?,?)",
                (user_id, card["name"], card["set"], card["price_usd"], card["price_idr"]),
            )
            await db.commit()
        await query.message.reply_text(
            f"⭐ *{esc(card['name'])}* ditambahkan ke wishlist\\!",
            parse_mode="MarkdownV2",
        )
        return

    # snap_save atau snap_buy → simpan ke inventory
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO inventory (user_id, card_name, card_set, price_usd, price_idr, condition) VALUES (?,?,?,?,?,?)",
            (user_id, card["name"], card["set"], card["price_usd"], card["price_idr"], "Near Mint"),
        )
        new_id = cur.lastrowid
        await db.execute(
            "INSERT INTO price_history (user_id, card_name, card_set, price_usd, price_idr) VALUES (?,?,?,?,?)",
            (user_id, card["name"], card["set"], card["price_usd"], card["price_idr"]),
        )
        await db.commit()

    price_str = esc_usd(card['price_usd']) if card["price_usd"] > 0 else "N/A"
    idr_str   = f"Rp {card['price_idr']:,.0f}" if card["price_idr"] > 0 else "N/A"

    if action == "snap_save":
        await query.message.reply_text(
            f"✅ *{esc(card['name'])}* disimpan\\! \\(ID: {new_id}\\)\n\n"
            f"💵 {price_str} \\| {idr_str}\n\n"
            f"📌 Set harga beli: `/buyprice {new_id} \\<harga\\>`\n"
            f"💰 Jual nanti: `/jual {new_id} \\<harga\\_jual\\>`",
            parse_mode="MarkdownV2",
        )

    elif action == "snap_buy":
        # Simpan pending state → tunggu user kirim harga beli
        await set_ustate(user_id, "pending_buy", new_id)
        await query.message.reply_text(
            f"✅ *{esc(card['name'])}* disimpan\\! \\(ID: {new_id}\\)\n\n"
            f"💸 *Berapa harga beli kartu ini \\(USD\\)?*\n"
            f"Ketik nominalnya sekarang, contoh: `12\\.5`",
            parse_mode="MarkdownV2",
        )

# ── /start ────────────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    lang    = await get_user_lang(user_id)
    name    = esc(update.effective_user.first_name)

    if lang == "en":
        await update.message.reply_text(
            f"Hello, {name}\\! ⚡\n"
            "Pokémon TCG Portfolio & Vision Bot is active\\!\n\n"
            "📸 *Scan:* Send a card photo → AI reads name & checks price\\!\n"
            "🃏 *Multi\\-Card Scan:* Photo of many cards → all prices shown\\!\n\n"
            "📖 *Commands:*\n"
            "• Type card name → Check price\n"
            "• `/add [name]` → Save to inventory\n"
            "• `/inventory` → View collection\n"
            "• `/refresh` → Update inventory prices\n"
            "• `/compare A \\| B` → Compare 2\\-3 cards\n"
            "• `/top10` → Top 10 most expensive\n"
            "• `/history [name]` → Price history\n"
            "• `/alert [name] [price]` → Price drop alert\n"
            "• `/alert [name] [price] naik` → Price rise alert\n"
            "• `/alerts` → View active alerts\n"
            "• `/removealert [no]` → Delete alert\n"
            "• `/scanset [set name]` → Scan all cards in a set 🆕\n"
            "• `/portfoliochart` → Portfolio value chart 🆕\n"
            "• `/findcheap [name]` → Find cheapest version 🆕\n"
            "• `/duplikat` → Check duplicate cards 🆕\n"
            "• `/nilai` → Value adjusted by condition 🆕\n"
            "• `/lang [id/en]` → Change language 🆕\n"
            "• `/backup` → Full data backup 🆕\n"
            "• `/wish [name]` → Add to wishlist\n"
            "• `/wishlist` → View wishlist\n"
            "• `/stats` → Portfolio statistics\n"
            "• `/help` → Full guide",
            parse_mode="MarkdownV2",
        )
    else:
        await update.message.reply_text(
            f"Halo, {name}\\! ⚡\n"
            "Bot Pokémon TCG Portfolio & Vision Scanner aktif\\!\n\n"
            "🃏 *Multi\\-Card Scan:* Foto banyak kartu → semua harga keluar\\!\n\n"
            "📖 *Perintah:*\n"
            "• Ketik nama kartu → Cek harga\n"
            "• `/add \\[nama\\]` → Simpan ke inventory\n"
            "• `/inventory` → Lihat koleksi\n"
            "• `/refresh` → Update harga inventory\n"
            "• `/compare A \\| B` → Bandingkan 2\\-3 kartu\n"
            "• `/top10` → 10 kartu termahal\n"
            "• `/history \\[nama\\]` → Riwayat harga\n"
            "• `/alert \\[nama\\] \\[harga\\]` → Alert harga turun\n"
            "• `/alert \\[nama\\] \\[harga\\] naik` → Alert harga naik\n"
            "• `/alerts` → Lihat alert aktif\n"
            "• `/removealert \\[no\\]` → Hapus alert\n"
            "• `/scanset \\[nama set\\]` → Scan semua kartu di set 🆕\n"
            "• `/portfoliochart` → Grafik nilai portfolio 🆕\n"
            "• `/findcheap \\[nama\\]` → Cari versi termurah 🆕\n"
            "• `/duplikat` → Cek kartu dobel 🆕\n"
            "• `/nilai` → Nilai disesuaikan kondisi 🆕\n"
            "• `/lang \\[id/en\\]` → Ganti bahasa 🆕\n"
            "• `/backup` → Backup semua data 🆕\n"
            "• `/wish \\[nama\\]` → Tambah ke wishlist\n"
            "• `/wishlist` → Lihat wishlist\n"
            "• `/stats` → Statistik portfolio\n"
            "• `/help` → Bantuan lengkap",
            parse_mode="MarkdownV2",
        )

# ── /help ─────────────────────────────────────────────────────────────────────
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "📖 *PANDUAN BOT POKÉMON TCG v6*\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "🔍 *CEK HARGA*\n"
        "• Ketik nama kartu → cari & lihat harga\n"
        "• Setelah cek harga muncul tombol simpan\\!\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "📦 *INVENTORY*\n"
        "• `/add Charizard` → Tambah ke koleksi\n"
        "• `/inventory` → Lihat semua koleksi\n"
        "• `/refresh` → Update harga semua kartu\n"
        "• `/editprice 1 25\\.5` → Edit harga manual kartu\n"
        "• `/delete 1` → Hapus kartu nomor 1\n"
        "• `/setcondition 1 Mint` → Set kondisi kartu\n"
        "• `/setgrade 1 PSA 10` → Set grade PSA/BGS\n"
        "• `/export` → Download CSV\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "💰 *JUAL BELI & ROI*\n"
        "• `/buyprice 1 12\\.5` → Set harga beli \\(modal\\)\n"
        "• `/roi` → Lihat profit/ROI semua kartu\n"
        "• `/jual 1 25` → Catat penjualan kartu\n"
        "• `/riwayatjual` → Riwayat kartu terjual\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "📈 *ANALISIS & CHART*\n"
        "• `/trend Charizard` → Grafik tren harga\n"
        "• `/setkomplit Base Set` → Cek kelengkapan set\n"
        "• `/portfoliochart` → Grafik distribusi nilai\n"
        "• `/stats` → Statistik & ringkasan portfolio\n"
        "• `/top10` → 10 kartu termahal di inventory\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "🔎 *BROWSE & CARI KARTU*\n"
        "• `/gen 1` s\\.d\\. `/gen 9` → Jelajah per generasi\n"
        "• `/newsets` → Set TCG terbaru \\(1 tahun\\)\n"
        "• `/newcards` → Kartu terbaru\n"
        "• `/cari Pikachu` → Cari dari database lokal\n"
        "• `/synccards` → Sync database nama kartu\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "⭐ *WISHLIST*\n"
        "• `/wish Mewtwo ex 50` → Tambah \\+ target harga\n"
        "• `/wishlist` → Lihat wishlist\n"
        "• `/removewish 1` → Hapus dari wishlist\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "🔔 *PRICE ALERT*\n"
        "• `/alert Charizard 50` → Notif harga ≤ \\$50 📉\n"
        "• `/alert Charizard 100 naik` → Notif harga ≥ \\$100 📈\n"
        "• `/alerts` → Lihat semua alert aktif\n"
        "• `/removealert 1` → Hapus alert nomor 1\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "🛒 *HARGA LOKAL & SARAN JUAL*\n"
        "• `/hargalokal Charizard` → Harga di Tokopedia\n"
        "• `/saraanjual` → Rekomendasi kartu untuk dijual\n"
        "• `/portohistory` → Grafik nilai portfolio harian\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "📂 *IMPORT MASSAL*\n"
        "• Kirim file \\. csv → Import semua kartu sekaligus\n"
        "• Kolom: `card\\_name, card\\_set, buy\\_price\\_usd, condition`\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "🛠️ *LAINNYA*\n"
        "• `/scanset Base Set` → Semua kartu di set\n"
        "• `/findcheap Pikachu` → Versi termurah Pikachu\n"
        "• `/compare Pikachu \\| Charizard` → Bandingkan\n"
        "• `/history Charizard` → Riwayat harga\n"
        "• `/duplikat` → Kartu dobel di inventory\n"
        "• `/nilai` → Nilai real berdasar kondisi\n"
        "• `/backup` → Backup data JSON\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "🏷️ *KONDISI KARTU*\n"
        "Mint/NM: 100% \\| LP: 80% \\| MP: 65%\n"
        "HP: 50% \\| Damaged: 25%\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "🆕 *FITUR v11*\n"
        "• `/autorefresh on` → Auto\\-refresh harga tiap hari jam 10\\.00 WIB\n"
        "• `/autorefresh off` → Matikan auto\\-refresh\n"
        "• `/autorefresh status` → Cek status auto\\-refresh\n\n"
        "📂 *FOLDER KOLEKSI*\n"
        "• `/folder` → Lihat semua folder & nilai per folder\n"
        "• `/folder <id>` → Pindah kartu via tombol keyboard\n"
        "• `/folder <id> <nama>` → Langsung pindah folder\n"
        "• `/infolder Graded` → Lihat kartu di folder tertentu\n"
        "Folder: 🏠 Pribadi · 🏷️ Dijual · 🏆 Graded · 🔄 Trade · 🖼️ Display\n\n"
        "📊 *LAPORAN P&L*\n"
        "• `/pl` → Laporan profit/loss bulan ini\n"
        "• `/pl Oktober` → P&L bulan spesifik\n"
        "• `/pl 10 2025` → P&L bulan & tahun spesifik\n\n"
        "🎓 *WORTH GRADING*\n"
        "• `/hitunggrade Charizard Base Set` → Kalkulasi worth grading\n"
        "• `/hitunggrade id:5` → Dari kartu di inventory\n"
        "Tampil: estimasi nilai PSA 7/8/9/10, biaya grading, break\\-even & rekomendasi",
        parse_mode="MarkdownV2",
    )

# ── Helper: compress image ────────────────────────────────────────────────────
# ── Helper: parse card names + kondisi dari AI ───────────────────────────────
def parse_card_names(raw: str) -> list[str]:
    """Backward-compat: return list of names only."""
    return [name for name, _ in parse_card_names_with_condition(raw)]

def parse_card_names_with_condition(raw: str) -> list[tuple[str, str]]:
    """
    Parse format baru: 'NAMA: Charizard VMAX | KONDISI: Near Mint'
    Fallback ke format lama (satu nama per baris).
    Return list of (name, condition).
    """
    VALID_CONDITIONS = {
        "mint": "Mint", "near mint": "Near Mint", "nm": "Near Mint",
        "lightly played": "Lightly Played", "lp": "Lightly Played",
        "moderately played": "Moderately Played", "mp": "Moderately Played",
        "heavily played": "Heavily Played", "hp": "Heavily Played",
        "damaged": "Damaged",
    }
    results: list[tuple[str, str]] = []
    seen: set[str] = set()
    for line in raw.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        # Format baru
        if "NAMA:" in line and "KONDISI:" in line:
            try:
                name_part, cond_part = line.split("|", 1)
                name = name_part.replace("NAMA:", "").strip()
                cond_raw = cond_part.replace("KONDISI:", "").strip().lower()
                cond = VALID_CONDITIONS.get(cond_raw, "Near Mint")
            except Exception:
                continue
        else:
            # Format lama: plain name per line
            name = re.sub(r'^[\s\d\.\-\*•]+', '', line).strip()
            name = re.sub(r'\s*\(.*?\)\s*$', '', name).strip()
            cond = "Near Mint"
        if name and name.lower() not in seen:
            seen.add(name.lower())
            results.append((name, cond))
    return results

# ── Helper: tampilkan konfirmasi simpan manual ────────────────────────────────
async def _show_manual_confirm(message, user_id: int, data: dict) -> None:
    name      = data["name"]
    price_idr = data["price_idr"]
    price_usd = data["price_usd"]
    keyboard  = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Simpan ke Inventory", callback_data=f"manual_confirm:{user_id}")],
        [
            InlineKeyboardButton("✏️ Edit Nama",  callback_data=f"manual_editnama:{user_id}"),
            InlineKeyboardButton("💰 Edit Harga", callback_data=f"manual_editharga:{user_id}"),
        ],
        [InlineKeyboardButton("❌ Batal", callback_data=f"manual_cancel:{user_id}")],
    ])
    await message.reply_text(
        f"📋 *Konfirmasi Simpan*\n\n"
        f"🃏 Nama: *{esc(name)}*\n"
        f"💵 Harga: Rp {esc(f'{price_idr:,.0f}')} \\(\\${esc(f'{price_usd:.2f}')}\\)\n\n"
        "_Sudah benar?_",
        reply_markup=keyboard,
        parse_mode="MarkdownV2",
    )

# ── Handler manual save (tombol setelah kartu tidak ditemukan) ───────────────
async def handle_manual_save(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query     = update.callback_query
    await query.answer()
    user_id   = update.effective_user.id
    card_name = await pop_ustate(user_id, "pending_manual_name") or "Unknown Card"
    await del_ustate(user_id, "pending_photo_name")
    await set_ustate(user_id, "pending_manual_price", card_name)
    await query.message.reply_text(
        f"💰 *Berapa harga kartu ini \\(Rupiah\\)?*\n"
        f"Kartu: *{esc(card_name)}*\n\n"
        f"Ketik nominalnya bre, contoh: `900000`",
        parse_mode="MarkdownV2",
    )

async def handle_manual_retry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query   = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    await del_ustate(user_id, "pending_manual_name")
    await set_ustate(user_id, "pending_photo_name", True)
    await query.message.reply_text(
        "📝 Ketik nama kartunya lagi bre:",
        parse_mode="MarkdownV2",
    )

async def handle_manual_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query   = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    data    = await pop_ustate(user_id, "pending_manual_confirm")
    if not data:
        await query.message.reply_text("⚠️ Data expired, coba ulangi bre\\.", parse_mode="MarkdownV2")
        return
    name          = data["name"]
    price_idr     = data["price_idr"]
    price_usd     = data["price_usd"]
    photo_file_id = await pop_ustate(user_id, "pending_photo_file_id")
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO inventory (user_id, card_name, price_usd, price_idr, condition, photo_file_id) VALUES (?,?,?,?,?,?)",
            (user_id, name, price_usd, price_idr, "Near Mint", photo_file_id),
        )
        new_id = cur.lastrowid
        await db.execute(
            "INSERT INTO price_history (user_id, card_name, price_usd, price_idr) VALUES (?,?,?,?)",
            (user_id, name, price_usd, price_idr),
        )
        await db.commit()
    caption = (
        f"✅ *{esc(name)}* disimpan\\! \\(ID: \\#{new_id}\\)\n"
        f"💵 \\${esc(f'{price_usd:.2f}')} \\| Rp {esc(f'{price_idr:,.0f}')}\n\n"
        f"_Set kondisi: /setcondition {new_id}_"
    )
    if photo_file_id:
        await query.message.reply_photo(
            photo=photo_file_id,
            caption=caption,
            parse_mode="MarkdownV2",
        )
    else:
        await query.message.reply_text(caption, parse_mode="MarkdownV2")

async def handle_manual_editnama(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query   = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    data    = await pop_ustate(user_id, "pending_manual_confirm")
    if not data:
        await query.message.reply_text("⚠️ Data expired, coba ulangi bre\\.", parse_mode="MarkdownV2")
        return
    await set_ustate(user_id, "pending_manual_edit_nama", data)
    await query.message.reply_text(
        f"✏️ Ketik nama kartu yang baru bre:\n_Nama sekarang: {esc(data['name'])}_",
        parse_mode="MarkdownV2",
    )

async def handle_manual_editharga(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query   = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    data    = await pop_ustate(user_id, "pending_manual_confirm")
    if not data:
        await query.message.reply_text("⚠️ Data expired, coba ulangi bre\\.", parse_mode="MarkdownV2")
        return
    await set_ustate(user_id, "pending_manual_edit_harga", data["name"])
    await query.message.reply_text(
        f"💰 Ketik harga baru \\(Rupiah\\) bre:\n"
        f"_Harga sekarang: Rp {data['price_idr']:,.0f}_",
        parse_mode="MarkdownV2",
    )

async def handle_manual_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query   = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    await clear_ustate(user_id, [
        "pending_manual_confirm", "pending_manual_price",
        "pending_manual_name",    "pending_manual_edit_nama",
        "pending_manual_edit_harga", "pending_photo_name",
        "pending_ocr_name",       "pending_photo_file_id",
    ])
    await query.message.reply_text("❌ Dibatalkan\\.", parse_mode="MarkdownV2")

# ── OCR: baca nama kartu dari foto label PSA ─────────────────────────────────
def _clean_ocr_line(text: str) -> str:
    """Buang karakter noise OCR: |, 1 di awal, karakter non-huruf selain spasi."""
    # Buang karakter pipa, angka junk, tanda baca di sekitar huruf
    text = re.sub(r"[|\\/*_@#$%^&]", " ", text)
    # Buang angka yang berdiri sendiri di awal/akhir (bukan bagian nama)
    text = re.sub(r"^\d+\s+", "", text)
    text = re.sub(r"\s+\d+$", "", text)
    # Collapse spasi ganda
    text = re.sub(r"\s+", " ", text).strip()
    return text

def ocr_card_label(photo_bytes: bytes) -> str | None:
    """Baca nama kartu dari foto PSA/label pakai Tesseract OCR."""
    if not HAS_TESSERACT:
        return None
    try:
        image = Image.open(io.BytesIO(photo_bytes))
        w, h  = image.size

        # Crop area label: ambil 38% atas, 70% kiri
        label = image.crop((int(w * 0.01), int(h * 0.03), int(w * 0.70), int(h * 0.40)))

        # Upscale 3x supaya OCR lebih akurat pada label kecil
        label = label.resize((label.width * 3, label.height * 3), Image.LANCZOS)

        # Grayscale + sharpen + contrast tinggi
        label = label.convert("L")
        label = ImageEnhance.Contrast(label).enhance(3.0)
        label = label.filter(ImageFilter.SHARPEN)
        label = label.filter(ImageFilter.SHARPEN)

        raw = pytesseract.image_to_string(label, config="--psm 4 --oem 3")
        lines = [_clean_ocr_line(l) for l in raw.strip().splitlines()]
        lines = [l for l in lines if len(l) >= 3 and re.search(r"[a-zA-Z]", l)]

        if not lines:
            return None

        SKIP_WORDS = {"GEM", "MT", "NM", "PSA", "AUTHENTIC", "MINT", "NEAR",
                      "POOR", "FAIR", "GOOD", "VG", "EX", "NM", "PR", "FR"}

        # Cari baris "POKEMON" → baris berikutnya adalah nama kartu
        for i, line in enumerate(lines):
            if "POKEMON" in line.upper() or "POKÉMON" in line.upper():
                name_parts = []
                for j in range(i + 1, min(i + 4, len(lines))):
                    nl = lines[j].strip()
                    if len(nl) < 3:
                        continue
                    words = nl.upper().split()
                    # Skip baris yang semua katanya adalah kata grade/meta
                    if all(w in SKIP_WORDS for w in words):
                        continue
                    # Baris pertama setelah POKEMON = nama kartu utama
                    name_parts.append(nl)
                    # Baris kedua boleh diambil kalau ada dan tidak seperti sertifikat
                    if len(name_parts) == 1 and j + 1 < len(lines):
                        next_line = lines[j + 1].strip()
                        if (len(next_line) >= 3
                                and re.search(r"[a-zA-Z]", next_line)
                                and not re.match(r"^\d+$", next_line.replace(" ", ""))):
                            name_parts.append(next_line)
                    break
                if name_parts:
                    result = " ".join(name_parts).title()
                    return result

        # Fallback: baris non-angka pertama yang punya cukup huruf
        for line in lines:
            letters = re.sub(r"[^a-zA-Z]", "", line)
            if len(letters) >= 4:
                return line.title()

        return None
    except Exception as e:
        logger.warning(f"OCR error: {e}")
        return None

# ── Callback OCR: pakai nama terdeteksi ──────────────────────────────────────
async def handle_ocr_use(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query   = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    name    = await pop_ustate(user_id, "pending_ocr_name")
    if not name:
        await query.message.reply_text("⚠️ Data expired, kirim foto lagi bre\\.", parse_mode="MarkdownV2")
        return
    # Langsung minta harga, tidak perlu cari API
    await set_ustate(user_id, "pending_manual_price", name)
    await query.message.reply_text(
        f"✅ Nama kartu: *{esc(name)}*\n\n"
        f"💰 Masukkan harga beli kamu \\(Rupiah\\)\\:\n_Contoh: `900000`_",
        parse_mode="MarkdownV2",
    )

async def handle_ocr_manual(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query   = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    await del_ustate(user_id, "pending_ocr_name")
    await set_ustate(user_id, "pending_photo_name", True)
    await query.message.reply_text(
        "📝 Ketik nama kartunya bre:",
        parse_mode="MarkdownV2",
    )

# ── Handler foto ─────────────────────────────────────────────────────────────
async def handle_photo_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id

    # ── Intercept /setphoto flow ──────────────────────────────────────────────
    setphoto_id = await pop_ustate(user_id, "pending_setphoto_id")
    if setphoto_id is not None:
        if not update.message.photo:
            await update.message.reply_text("⚠️ Kirim foto bre, bukan file\\.", parse_mode="MarkdownV2")
            await set_ustate(user_id, "pending_setphoto_id", setphoto_id)  # kembalikan state
            return
        file_id = update.message.photo[-1].file_id
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT card_name FROM inventory WHERE id=? AND user_id=?",
                (setphoto_id, user_id),
            ) as cur:
                row = await cur.fetchone()
            if row:
                await db.execute(
                    "UPDATE inventory SET photo_file_id=? WHERE id=? AND user_id=?",
                    (file_id, setphoto_id, user_id),
                )
                await db.commit()
                await update.message.reply_photo(
                    photo=file_id,
                    caption=f"✅ Foto *{esc(row[0])}* \\(ID: \\#{setphoto_id}\\) berhasil diupdate\\!\n_Lihat dengan /photo {setphoto_id}_",
                    parse_mode="MarkdownV2",
                )
            else:
                await update.message.reply_text("⚠️ Kartu tidak ditemukan bre\\.", parse_mode="MarkdownV2")
        return  # jangan lanjut ke OCR flow

    # ── Intercept /setphoto2 flow (foto belakang) ─────────────────────────────
    setphoto_back_id = await pop_ustate(user_id, "pending_setphoto_back_id")
    if setphoto_back_id is not None:
        if not update.message.photo:
            await update.message.reply_text("⚠️ Kirim foto bre, bukan file\\.", parse_mode="MarkdownV2")
            await set_ustate(user_id, "pending_setphoto_back_id", setphoto_back_id)
            return
        file_id = update.message.photo[-1].file_id
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT card_name FROM inventory WHERE id=? AND user_id=?",
                (setphoto_back_id, user_id),
            ) as cur:
                row = await cur.fetchone()
            if row:
                await db.execute(
                    "UPDATE inventory SET photo_file_id_back=? WHERE id=? AND user_id=?",
                    (file_id, setphoto_back_id, user_id),
                )
                await db.commit()
                await update.message.reply_photo(
                    photo=file_id,
                    caption=f"✅ Foto belakang *{esc(row[0])}* \\(ID: \\#{setphoto_back_id}\\) berhasil disimpan\\!\n_Lihat dengan /photo {setphoto_back_id}_",
                    parse_mode="MarkdownV2",
                )
            else:
                await update.message.reply_text("⚠️ Kartu tidak ditemukan bre\\.", parse_mode="MarkdownV2")
        return

    # Simpan file_id foto terbesar (kualitas terbaik) untuk disimpan ke inventory nanti
    if update.message.photo:
        await set_ustate(user_id, "pending_photo_file_id", update.message.photo[-1].file_id)

    if HAS_TESSERACT:
        status = await update.message.reply_text(
            "📸 Foto diterima\\! Sedang baca nama kartu\\.\\.\\.",
            parse_mode="MarkdownV2",
        )
        try:
            photo_file  = await update.message.photo[-1].get_file()
            photo_bytes = bytes(await photo_file.download_as_bytearray())
            detected    = ocr_card_label(photo_bytes)
        except Exception as e:
            logger.error(f"Photo download error: {e}")
            detected = None

        if detected:
            await set_ustate(user_id, "pending_ocr_name", detected)
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton(f"✅ Pakai: {detected}", callback_data=f"ocr_use:{user_id}")],
                [InlineKeyboardButton("✏️ Ketik nama sendiri",   callback_data=f"ocr_manual:{user_id}")],
            ])
            await status.edit_text(
                f"🔍 Terdeteksi: *{esc(detected)}*\n\nPakai nama ini?",
                reply_markup=keyboard,
                parse_mode="MarkdownV2",
            )
        else:
            await set_ustate(user_id, "pending_photo_name", True)
            await status.edit_text(
                "📸 Foto diterima\\!\n\n"
                "📝 *Ketik nama kartunya bre:*\n"
                "_Contoh: `Pikachu Gym Event Campaign`_",
                parse_mode="MarkdownV2",
            )
    else:
        # Tesseract tidak terinstall → fallback manual
        await set_ustate(user_id, "pending_photo_name", True)
        await update.message.reply_text(
            "📸 Foto diterima\\!\n\n"
            "📝 *Ketik nama kartunya bre:*\n"
            "_Contoh: `Pikachu Gym Event Campaign`_",
            parse_mode="MarkdownV2",
        )

# ── Handler teks ──────────────────────────────────────────────────────────────
async def handle_card_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.message.text.strip()
    await update.message.reply_text(f"🔍 Mencari kartu *{esc(query)}*\\.\\.\\.", parse_mode="MarkdownV2")

    results = await search_pokemon_cards_multi(query, limit=5)

    if results is None:
        await update.message.reply_text(f"❌ Kartu '{esc(query)}' tidak ditemukan, Bre\\!", parse_mode="MarkdownV2")
        return
    if isinstance(results, dict) and results.get("error"):
        err = results["error"]
        if err == "rate_limit":
            await update.message.reply_text("⚠️ API rate limit, tunggu sebentar\\!", parse_mode="MarkdownV2")
        elif err == "timeout":
            await update.message.reply_text("⏱️ Timeout\\! Coba lagi ya\\.", parse_mode="MarkdownV2")
        elif err == "api_down":
            await update.message.reply_text(
                "🔧 Pokemontcg\\.io lagi gangguan bre\\! Server mereka down sementara\\.\n"
                "Coba lagi beberapa menit lagi ya\\! 🙏",
                parse_mode="MarkdownV2"
            )
        else:
            await update.message.reply_text("❌ Gagal fetch data kartu bre, coba lagi\\!", parse_mode="MarkdownV2")
        return

    if len(results) == 1:
        await send_card(update, results[0], context=context, show_save_buttons=True)
        return

    keyboard = []
    for i, card in enumerate(results):
        price_str = f"${card['price_usd']:.2f}" if card["price_usd"] > 0 else "N/A"
        btn_label = f"{card['name']} ({card['set']}) — {price_str}"
        keyboard.append([InlineKeyboardButton(btn_label, callback_data=f"card_select:{i}:{update.effective_user.id}")])

    context.bot_data[f"search_{update.effective_user.id}"] = results

    await update.message.reply_text(
        f"🃏 Ditemukan *{len(results)} kartu* untuk *{esc(query)}*\\:\n_Pilih yang sesuai:_",
        parse_mode="MarkdownV2",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )

# ── Callback: pilih kartu ─────────────────────────────────────────────────────
async def handle_card_select(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    parts   = query.data.split(":")
    idx     = int(parts[1])
    user_id = int(parts[2])

    cards = context.bot_data.get(f"search_{user_id}")
    if not cards or idx >= len(cards):
        await query.edit_message_text("❌ Data expired, coba cari lagi\\.", parse_mode="MarkdownV2")
        return

    card = cards[idx]
    await query.edit_message_text(f"✅ Kamu pilih: *{esc(card['name'])}*", parse_mode="MarkdownV2")

    # Tampilkan kartu dengan tombol simpan
    await send_card(query, card, context=context, show_save_buttons=True)

# ── /add ──────────────────────────────────────────────────────────────────────
async def add_inventory(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    card_query = " ".join(context.args).strip()
    if not card_query:
        await update.message.reply_text("⚠️ Format: `/add Charizard`", parse_mode="MarkdownV2")
        return

    await update.message.reply_text(f"⏳ Memproses *{esc(card_query)}*\\.\\.\\.", parse_mode="MarkdownV2")

    card = await search_pokemon_card(card_query)
    if not card or (isinstance(card, dict) and card.get("error")):
        await update.message.reply_text(f"❌ Kartu '{esc(card_query)}' tidak ditemukan\\.", parse_mode="MarkdownV2")
        return

    user_id = update.effective_user.id
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO inventory (user_id, card_name, card_set, price_usd, price_idr, condition) VALUES (?,?,?,?,?,?)",
            (user_id, card["name"], card["set"], card["price_usd"], card["price_idr"], "Near Mint"),
        )
        await db.execute(
            "INSERT INTO price_history (user_id, card_name, card_set, price_usd, price_idr) VALUES (?,?,?,?,?)",
            (user_id, card["name"], card["set"], card["price_usd"], card["price_idr"]),
        )
        await db.commit()

    price_str = esc_usd(card['price_usd']) if card["price_usd"] > 0 else "Tidak tersedia"
    idr_str   = f"Rp {card['price_idr']:,.0f}" if card["price_idr"] > 0 else "Tidak tersedia"

    await update.message.reply_text(
        f"✅ *{esc(card['name'])}* ditambahkan\\!\n\n"
        f"📦 Set: {esc(card['set'])}\n"
        f"🏷️ Kondisi: Near Mint \\(default\\)\n"
        f"💵 {price_str} \\| {idr_str}\n\n"
        f"_Atur kondisi: /setcondition \\[no\\] \\[kondisi\\]_\n"
        f"_Atur grade: /setgrade \\[no\\] \\[grade\\]_",
        parse_mode="MarkdownV2",
    )

# ── /inventory ────────────────────────────────────────────────────────────────
INV_PAGE_SIZE = 10

def _inventory_nav_keyboard(page: int, total_pages: int):
    """Return InlineKeyboardMarkup dengan tombol Prev/Next, atau None kalau 1 halaman."""
    if total_pages <= 1:
        return None
    buttons = []
    if page > 0:
        buttons.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"inv_page:{page - 1}"))
    buttons.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="inv_page:noop"))
    if page < total_pages - 1:
        buttons.append(InlineKeyboardButton("Next ➡️", callback_data=f"inv_page:{page + 1}"))
    return InlineKeyboardMarkup([buttons])


async def _build_inventory_page(user_id: int, page: int) -> tuple:
    """Fetch inventory dan build teks halaman ke-page (0-indexed).
    Returns (text, actual_page, total_pages, total_cards).
    """
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, card_name, card_set, price_usd, price_idr, condition, "
            "psa_grade, photo_file_id, tags, notes, for_sale, ask_price_usd "
            "FROM inventory WHERE user_id=? ORDER BY id",
            (user_id,),
        ) as cur:
            items = await cur.fetchall()

    if not items:
        return ("📂 Inventory kosong\\! Tambah dengan `/add \\[nama\\]`\\.", 0, 0, 0)

    total_usd   = sum(r[3] or 0 for r in items)
    total_idr   = sum(r[4] or 0 for r in items)
    total       = len(items)
    total_pages = max(1, (total + INV_PAGE_SIZE - 1) // INV_PAGE_SIZE)
    page        = max(0, min(page, total_pages - 1))
    start       = page * INV_PAGE_SIZE
    page_items  = items[start:start + INV_PAGE_SIZE]

    header = f"📦 *Portfolio Koleksi Pokémon* \\(hal {page + 1}/{total_pages}\\):\n"
    lines  = [header]

    for global_idx, (inv_id, name, card_set, p_usd, p_idr,
                     condition, psa_grade, photo_file_id, tags,
                     notes, for_sale, ask_price) in enumerate(page_items, start + 1):
        usd_str   = esc_usd(p_usd)   if (p_usd  or 0) > 0 else "N/A"
        idr_str   = f"Rp {p_idr:,.0f}" if (p_idr or 0) > 0 else "N/A"
        cond_str  = esc(condition or "Near Mint")
        grade_str = f" \\| 🏆 {esc(str(psa_grade))}" if psa_grade else ""
        photo_str = f" \\| 📷 /photo {inv_id}" if photo_file_id else ""
        set_str   = f" \\({esc(card_set)}\\)" if card_set else ""
        sale_str  = f" \\| 🏷️ _{esc_usd(ask_price or 0)}_" if for_sale else ""

        mid_rows  = f"   ├ {cond_str}{grade_str}{photo_str}\n"
        if tags:
            mid_rows += f"   ├ 🔖 _{esc(tags)}_\n"
        if notes:
            mid_rows += f"   ├ 📝 _{esc(notes)}_\n"

        lines.append(
            f"{global_idx}\\. *{esc(name)}*{set_str} — `\\#{inv_id}`\n"
            f"{mid_rows}"
            f"   └ 💵 {usd_str} \\| {idr_str}{sale_str}\n"
        )

    # Footer hanya di halaman terakhir
    if page == total_pages - 1:
        lines.append(
            f"\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\n"
            f"💰 *Total: {esc_usd(total_usd)} \\| Rp {total_idr:,.0f}* \\({total} kartu\\)\n\n"
            f"_/note \\[id\\] \\[teks\\] • /forsale \\[id\\] \\[harga\\]_\n"
            f"_/listing /tag /remind /share /setphoto /editkartu_\n"
            f"_/setgrade \\[id\\] \\[grade\\] • /setcondition \\[id\\] \\[kondisi\\]_"
        )

    return "\n".join(lines), page, total_pages, total


async def show_inventory(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    text, page, total_pages, total = await _build_inventory_page(user_id, 0)

    if total == 0:
        await update.message.reply_text(
            "📂 Inventory kosong\\! Tambah dengan `/add \\[nama\\]`\\.",
            parse_mode="MarkdownV2",
        )
        return

    kb = _inventory_nav_keyboard(page, total_pages)
    await update.message.reply_text(text, parse_mode="MarkdownV2", reply_markup=kb)


async def inventory_page_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Callback: navigasi halaman inventory."""
    query = update.callback_query
    await query.answer()

    data = query.data  # "inv_page:<n>" atau "inv_page:noop"
    if data == "inv_page:noop":
        return

    try:
        page = int(data.split(":")[1])
    except (IndexError, ValueError):
        return

    user_id = query.from_user.id
    text, page, total_pages, _ = await _build_inventory_page(user_id, page)
    kb = _inventory_nav_keyboard(page, total_pages)

    try:
        await query.edit_message_text(text, parse_mode="MarkdownV2", reply_markup=kb)
    except Exception:
        pass  # pesan tidak berubah → abaikan

# ── /photo <id> — Kirim foto kartu dari inventory ─────────────────────────────
async def show_card_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not context.args:
        await update.message.reply_text(
            "Format: `/photo \\<id\\>`\n_Contoh: `/photo 3`_\n\nID bisa dilihat di /inventory",
            parse_mode="MarkdownV2",
        )
        return
    try:
        card_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("⚠️ ID harus angka bre\\.", parse_mode="MarkdownV2")
        return
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT card_name, card_set, price_usd, photo_file_id, photo_file_id_back FROM inventory WHERE id=? AND user_id=?",
            (card_id, user_id),
        ) as cur:
            row = await cur.fetchone()
    if not row:
        await update.message.reply_text("⚠️ Kartu tidak ditemukan bre\\.", parse_mode="MarkdownV2")
        return
    card_name, card_set, price_usd, photo_file_id, photo_file_id_back = row
    if not photo_file_id and not photo_file_id_back:
        await update.message.reply_text(
            f"📷 *{esc(card_name)}* belum punya foto tersimpan bre\\.\n"
            f"_Tambah foto depan: /setphoto {card_id}_\n"
            f"_Tambah foto belakang: /setphoto2 {card_id}_",
            parse_mode="MarkdownV2",
        )
        return
    caption = (
        f"📷 *{esc(card_name)}*"
        + (f" \\| {esc(card_set)}" if card_set else "")
        + (f"\n💵 {esc_usd(price_usd)}" if price_usd and price_usd > 0 else "")
    )
    if photo_file_id:
        has_back = " \\| _/photo2 {card_id} untuk belakang_" if photo_file_id_back else ""
        await update.message.reply_photo(
            photo=photo_file_id,
            caption=caption + (f"\n_Foto belakang: /photo2 {card_id}_" if photo_file_id_back else ""),
            parse_mode="MarkdownV2",
        )
    elif photo_file_id_back:
        await update.message.reply_photo(
            photo=photo_file_id_back,
            caption=caption + "\n_\\(hanya foto belakang tersedia\\)_",
            parse_mode="MarkdownV2",
        )

# ── /refresh ──────────────────────────────────────────────────────────────────
async def refresh_inventory(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, card_name, card_set, price_usd FROM inventory WHERE user_id=? ORDER BY id",
            (user_id,),
        ) as cur:
            items = await cur.fetchall()

    if not items:
        await update.message.reply_text("📂 Inventory kosong, tidak ada yang di\\-refresh\\.", parse_mode="MarkdownV2")
        return

    status = await update.message.reply_text(
        f"🔄 Mengupdate harga *{len(items)} kartu*\\.\\.\\.",
        parse_mode="MarkdownV2"
    )

    updated = 0
    changes = []

    for (item_id, card_name, card_set, old_usd) in items:
        card = await search_pokemon_card(card_name)
        if card and not (isinstance(card, dict) and card.get("error")):
            new_usd = card["price_usd"]
            new_idr = card["price_idr"]
            diff    = new_usd - old_usd

            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "UPDATE inventory SET price_usd=?, price_idr=? WHERE id=?",
                    (new_usd, new_idr, item_id),
                )
                await db.execute(
                    "INSERT INTO price_history (user_id, card_name, card_set, price_usd, price_idr) VALUES (?,?,?,?,?)",
                    (user_id, card_name, card_set, new_usd, new_idr),
                )
                await db.commit()

            updated += 1
            if abs(diff) > 0.01:
                arrow = "📈" if diff > 0 else "📉"
                changes.append(f"{arrow} *{esc(card_name)}*: {esc_usd(old_usd)} → {esc_usd(new_usd)}")

        await asyncio.sleep(0.3)

    change_text = "\n".join(changes) if changes else "_Tidak ada perubahan harga signifikan_"
    await status.edit_text(
        f"✅ *Refresh selesai\\!* {updated}/{len(items)} kartu diperbarui\n\n"
        f"📊 *Perubahan Harga:*\n{change_text}",
        parse_mode="MarkdownV2",
    )

# ── /compare ──────────────────────────────────────────────────────────────────
async def compare_cards(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = " ".join(context.args).strip()
    if not text or "|" not in text:
        await update.message.reply_text(
            "⚠️ Format: `/compare Pikachu \\| Charizard`\n"
            "Bisa sampai 3 kartu: `/compare A \\| B \\| C`",
            parse_mode="MarkdownV2"
        )
        return

    names = [n.strip() for n in text.split("|")][:3]
    if len(names) < 2:
        await update.message.reply_text("⚠️ Minimal 2 kartu untuk dibandingkan\\!", parse_mode="MarkdownV2")
        return

    await update.message.reply_text(f"⚖️ Membandingkan *{len(names)} kartu*\\.\\.\\.", parse_mode="MarkdownV2")

    tasks   = [search_pokemon_card(name) for name in names]
    results = await asyncio.gather(*tasks)

    lines      = ["⚖️ *Perbandingan Kartu Pokémon:*\n", "━━━━━━━━━━━━━━━━━━━━━━\n"]
    prices_usd = []

    for name, card in zip(names, results):
        if not card or (isinstance(card, dict) and card.get("error")):
            lines.append(f"❌ *{esc(name)}*: tidak ditemukan\n")
            prices_usd.append(0)
        else:
            usd = card["price_usd"]
            prices_usd.append(usd)
            price_usd = f"{esc_usd(usd)}" if usd > 0 else "N/A"
            price_idr = f"Rp {card['price_idr']:,.0f}" if usd > 0 else "N/A"
            lines.append(
                f"🃏 *{esc(card['name'])}*\n"
                f"   📦 {esc(card['set'])}\n"
                f"   ⭐ {esc(card['rarity'])}\n"
                f"   💵 {price_usd} \\| {price_idr}\n"
            )

    valid_prices = [(i, p) for i, p in enumerate(prices_usd) if p > 0]
    if len(valid_prices) >= 2:
        max_idx     = max(valid_prices, key=lambda x: x[1])[0]
        winner_name = names[max_idx]
        lines.append("━━━━━━━━━━━━━━━━━━━━━━\n")
        lines.append(f"🏆 *Paling Mahal: {esc(winner_name)}*")

    await update.message.reply_text("\n".join(lines), parse_mode="MarkdownV2")

# ── /top10 ────────────────────────────────────────────────────────────────────
async def top10_inventory(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT card_name, card_set, price_usd, price_idr, condition FROM inventory WHERE user_id=? ORDER BY price_usd DESC LIMIT 10",
            (user_id,),
        ) as cur:
            items = await cur.fetchall()

    if not items:
        await update.message.reply_text("📂 Inventory kosong\\! Tambah kartu dulu dengan `/add`\\.", parse_mode="MarkdownV2")
        return

    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
    lines  = [f"🏆 *Top {len(items)} Kartu Termahal di Inventorymu:*\n"]

    for i, (name, card_set, p_usd, p_idr, condition) in enumerate(items):
        medal   = medals[i] if i < len(medals) else f"{i+1}\\."
        usd_str = f"{esc_usd(p_usd)}" if p_usd > 0 else "N/A"
        idr_str = f"Rp {p_idr:,.0f}" if p_idr > 0 else "N/A"
        set_prefix = f"📦 {esc(card_set)} \\| " if card_set else ""
        lines.append(
            f"{medal} *{esc(name)}*\n"
            f"   {set_prefix}💵 {usd_str} \\| {idr_str}\n"
        )

    total_usd = sum(r[2] for r in items)
    lines.append("━━━━━━━━━━━━━━━━━━━━━━\n")
    lines.append(f"💰 Total top {len(items)}: *{esc_usd(total_usd)}*")

    await update.message.reply_text("\n".join(lines), parse_mode="MarkdownV2")

# ── /history ──────────────────────────────────────────────────────────────────
async def price_history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    card_name = " ".join(context.args).strip()
    if not card_name:
        await update.message.reply_text(
            "⚠️ Format: `/history Charizard`\n_Lihat riwayat perubahan harga kartu_",
            parse_mode="MarkdownV2"
        )
        return

    user_id = update.effective_user.id
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT card_name, price_usd, price_idr, recorded_at FROM price_history WHERE user_id=? AND card_name LIKE ? ORDER BY recorded_at DESC LIMIT 10",
            (user_id, f"%{card_name}%"),
        ) as cur:
            history = await cur.fetchall()

    if not history:
        await update.message.reply_text(
            f"📊 Belum ada riwayat harga untuk *{esc(card_name)}*\\.\n"
            f"_Riwayat tercatat saat kamu /add atau /refresh kartu\\._",
            parse_mode="MarkdownV2"
        )
        return

    actual_name = history[0][0]
    lines       = [f"📊 *Riwayat Harga: {esc(actual_name)}*\n"]

    for _, price_usd, price_idr, recorded_at in history:
        date_str = esc(recorded_at[:10]) if recorded_at else "\\-"
        time_str = esc(recorded_at[11:16]) if recorded_at and len(recorded_at) > 10 else ""
        usd_str  = f"{esc_usd(price_usd)}" if price_usd > 0 else "N/A"
        idr_str  = f"Rp {price_idr:,.0f}" if price_idr > 0 else "N/A"
        lines.append(f"📅 {date_str} {time_str}: *{usd_str}* \\| {idr_str}")

    if len(history) >= 2:
        latest = history[0][1]
        oldest = history[-1][1]
        diff   = latest - oldest
        if diff > 0:
            trend = f"📈 Naik {esc_usd(diff)} dari awal pencatatan"
        elif diff < 0:
            trend = f"📉 Turun {esc_usd(abs(diff))} dari awal pencatatan"
        else:
            trend = "➡️ Harga stabil"
        lines.append(f"\n{trend}")

    await update.message.reply_text("\n".join(lines), parse_mode="MarkdownV2")

# ── /alert — 2 ARAH ──────────────────────────────────────────────────────────
async def set_alert(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) < 2:
        await update.message.reply_text(
            "⚠️ Format:\n"
            "• `/alert Charizard 50` → notif harga ≤ \\$50 📉\n"
            "• `/alert Charizard 100 naik` → notif harga ≥ \\$100 📈",
            parse_mode="MarkdownV2"
        )
        return

    args       = list(context.args)
    alert_type = "turun"

    if args[-1].lower() == "naik":
        alert_type = "naik"
        args = args[:-1]
    elif args[-1].lower() == "turun":
        args = args[:-1]

    try:
        threshold = float(args[-1])
        card_name = " ".join(args[:-1]).strip()
    except ValueError:
        await update.message.reply_text(
            "⚠️ Harga harus angka\\! Contoh: `/alert Pikachu 25` atau `/alert Pikachu 100 naik`",
            parse_mode="MarkdownV2"
        )
        return

    if threshold <= 0:
        await update.message.reply_text("⚠️ Harga harus lebih dari 0\\!", parse_mode="MarkdownV2")
        return

    user_id = update.effective_user.id
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO price_alerts (user_id, card_name, threshold_usd, alert_type) VALUES (?,?,?,?)",
            (user_id, card_name, threshold, alert_type)
        )
        await db.commit()

    if alert_type == "turun":
        desc = f"Notif kalau harga ≤ *{esc_usd(threshold)}* 📉"
    else:
        desc = f"Notif kalau harga ≥ *{esc_usd(threshold)}* 📈"

    await update.message.reply_text(
        f"🔔 *Alert diset\\!*\n\n"
        f"🃏 Kartu: *{esc(card_name)}*\n"
        f"🎯 {desc}\n\n"
        f"_Bot cek harga setiap 6 jam sekali\\._\n"
        f"_Lihat semua alert: /alerts_",
        parse_mode="MarkdownV2"
    )

# ── /alerts ───────────────────────────────────────────────────────────────────
async def show_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, card_name, threshold_usd, COALESCE(alert_type,'turun'), created_at FROM price_alerts WHERE user_id=? ORDER BY id",
            (user_id,),
        ) as cur:
            items = await cur.fetchall()

    if not items:
        await update.message.reply_text(
            "🔕 Belum ada alert aktif\\.\n"
            "Set alert dengan `/alert \\[nama\\] \\[harga\\]`\\.",
            parse_mode="MarkdownV2"
        )
        return

    lines = [f"🔔 *Price Alerts Aktif \\({len(items)}\\):*\n"]
    for i, (_, name, threshold, alert_type, created_at) in enumerate(items, 1):
        date_str  = esc(created_at[:10]) if created_at else "\\-"
        direction = f"≤ {esc_usd(threshold)} 📉" if alert_type == "turun" else f"≥ {esc_usd(threshold)} 📈"
        lines.append(
            f"{i}\\. *{esc(name)}* {direction}\n"
            f"   📅 Set: {date_str}\n"
        )

    lines.append("_/removealert \\[no\\] untuk hapus alert_")
    await update.message.reply_text("\n".join(lines), parse_mode="MarkdownV2")

# ── /removealert ──────────────────────────────────────────────────────────────
async def remove_alert(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id

    if not context.args:
        await update.message.reply_text("⚠️ Format: `/removealert 1`", parse_mode="MarkdownV2")
        return
    try:
        idx = int(context.args[0])
    except ValueError:
        await update.message.reply_text("⚠️ Nomor harus angka\\!", parse_mode="MarkdownV2")
        return

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, card_name FROM price_alerts WHERE user_id=? ORDER BY id", (user_id,)
        ) as cur:
            items = await cur.fetchall()
        if idx < 1 or idx > len(items):
            await update.message.reply_text("❌ Nomor alert tidak ditemukan\\.", parse_mode="MarkdownV2")
            return
        db_id, card_name = items[idx - 1]
        await db.execute("DELETE FROM price_alerts WHERE id=? AND user_id=?", (db_id, user_id))
        await db.commit()

    await update.message.reply_text(f"🗑️ Alert *{esc(card_name)}* dihapus\\!", parse_mode="MarkdownV2")

# ── Background job: cek price alerts (2 arah) ─────────────────────────────────
async def check_price_alerts(context) -> None:
    logger.info("Menjalankan cek price alerts...")
    bot = context.bot

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, user_id, card_name, threshold_usd, COALESCE(alert_type,'turun') FROM price_alerts"
        ) as cur:
            alerts = await cur.fetchall()

    triggered = []
    for (alert_id, user_id, card_name, threshold, alert_type) in alerts:
        card = await search_pokemon_card(card_name)
        if card and not (isinstance(card, dict) and card.get("error")):
            current_price  = card.get("price_usd", 0)
            triggered_flag = False
            action_msg     = ""
            emoji          = ""

            if alert_type == "turun" and current_price > 0 and current_price <= threshold:
                triggered_flag = True
                emoji          = "📉"
                action_msg     = "Ini saat yang tepat untuk beli\\!"
            elif alert_type == "naik" and current_price > 0 and current_price >= threshold:
                triggered_flag = True
                emoji          = "📈"
                action_msg     = "Ini saat yang tepat untuk jual\\!"

            if triggered_flag:
                try:
                    idr       = current_price * EXCHANGE_RATE
                    direction = "≤" if alert_type == "turun" else "≥"
                    await bot.send_message(
                        chat_id=user_id,
                        text=(
                            f"🔔 *PRICE ALERT TRIGGERED\\!* {emoji}\n\n"
                            f"🃏 *{esc(card_name)}*\n"
                            f"💵 Harga sekarang: *{esc_usd(current_price)}* \\| Rp {idr:,.0f}\n"
                            f"🎯 Target kamu: {direction} {esc_usd(threshold)}\n\n"
                            f"💡 _{action_msg}_ 🚀\n"
                            f"_Alert ini otomatis dihapus\\._"
                        ),
                        parse_mode="MarkdownV2"
                    )
                    triggered.append(alert_id)
                    logger.info(f"Alert triggered: {card_name} @ ${current_price:.2f} for user {user_id}")
                except Exception as e:
                    logger.error(f"Gagal kirim alert ke {user_id}: {e}")
        await asyncio.sleep(0.5)

    if triggered:
        async with aiosqlite.connect(DB_PATH) as db:
            for aid in triggered:
                await db.execute("DELETE FROM price_alerts WHERE id=?", (aid,))
            await db.commit()

# ── /scanset — FITUR BARU ─────────────────────────────────────────────────────
async def scan_set(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    set_name = " ".join(context.args).strip()
    if not set_name:
        await update.message.reply_text(
            "⚠️ Format: `/scanset Base Set`\n"
            "_Tampilkan semua kartu dalam satu set_",
            parse_mode="MarkdownV2"
        )
        return

    await update.message.reply_text(
        f"🔍 Mencari semua kartu di set *{esc(set_name)}*\\.\\.\\.",
        parse_mode="MarkdownV2"
    )

    result = await search_pokemon_set(set_name)

    if result is None:
        await update.message.reply_text(f"❌ Set '{esc(set_name)}' tidak ditemukan\\.", parse_mode="MarkdownV2")
        return
    if isinstance(result, dict) and result.get("error"):
        err = result["error"]
        msg = "⚠️ Rate limit\\!" if err == "rate_limit" else "⏱️ Timeout\\!" if err == "timeout" else "🔧 Pokemontcg\\.io lagi gangguan bre\\! Coba lagi nanti ya\\." if err == "api_down" else "❌ Gagal fetch data kartu bre, coba lagi\\!"
        await update.message.reply_text(msg, parse_mode="MarkdownV2")
        return

    cards       = result["cards"]
    total_count = result["total"]
    actual_set  = cards[0]["set"] if cards else set_name

    cards_with_price = sorted([c for c in cards if c["price_usd"] > 0], key=lambda x: x["price_usd"], reverse=True)
    total_value      = sum(c["price_usd"] for c in cards_with_price)
    avg_value        = total_value / len(cards_with_price) if cards_with_price else 0

    lines = [
        f"📦 *Set: {esc(actual_set)}*\n",
        f"🃏 Kartu terambil: *{len(cards)}* \\(total di set: {total_count}\\)\n",
        f"💰 Total estimasi nilai: *{esc_usd(total_value)}* \\| Rp {total_value * EXCHANGE_RATE:,.0f}\n",
        f"📊 Rata\\-rata/kartu: *{esc_usd(avg_value)}*\n",
        f"\n🏆 *Top 10 Paling Mahal:*\n"
    ]

    for i, card in enumerate(cards_with_price[:10], 1):
        usd_str = esc_usd(card['price_usd'])
        idr_str = f"Rp {card['price_idr']:,.0f}"
        lines.append(f"{i}\\. *{esc(card['name'])}* — {usd_str} \\| {idr_str}")

    if cards_with_price:
        lines.append(f"\n💡 _/add {esc(cards_with_price[0]['name'])} untuk simpan kartu termahal_")

    await update.message.reply_text("\n".join(lines), parse_mode="MarkdownV2")

# ── /portfoliochart — FITUR BARU ──────────────────────────────────────────────
async def portfolio_chart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT card_set, SUM(price_usd) FROM inventory WHERE user_id=? GROUP BY card_set ORDER BY SUM(price_usd) DESC",
            (user_id,),
        ) as cur:
            set_data = await cur.fetchall()

        async with db.execute(
            "SELECT condition, SUM(price_usd) FROM inventory WHERE user_id=? GROUP BY condition ORDER BY SUM(price_usd) DESC",
            (user_id,),
        ) as cur:
            cond_data = await cur.fetchall()

    if not set_data:
        await update.message.reply_text(
            "📂 Inventory kosong\\! Tambah kartu dulu dengan `/add`\\.",
            parse_mode="MarkdownV2"
        )
        return

    total = sum(r[1] for r in set_data)

    if not HAS_MATPLOTLIB:
        # Fallback: text-based bar chart
        lines = ["📊 *Distribusi Nilai Portfolio per Set:*\n"]
        for set_name, value in set_data:
            pct = (value / total * 100) if total > 0 else 0
            bar = "█" * int(pct / 5)
            lines.append(f"*{esc(set_name or 'Unknown')}*\n`{bar}` {esc(f"{pct:.1f}")}% \\({esc_usd(value)}\\)\n")
        lines.append(f"\n💰 *Total: {esc_usd(total)}* \\| Rp {total * EXCHANGE_RATE:,.0f}")
        await update.message.reply_text("\n".join(lines), parse_mode="MarkdownV2")
        return

    # Generate chart dengan matplotlib
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(13, 6))
    fig.patch.set_facecolor('#1a1a2e')

    # Chart 1: Pie per Set
    set_labels = [r[0] or "Unknown" for r in set_data]
    set_values = [r[1] for r in set_data]
    if len(set_labels) > 7:
        other_val  = sum(set_values[7:])
        set_labels = set_labels[:7] + ["Lainnya"]
        set_values = set_values[:7] + [other_val]

    colors1 = ['#e94560', '#0f3460', '#533483', '#e0a50a', '#4ecca3', '#16213e', '#c77dff', '#c0c0c0']
    wedges, _, autotexts = ax1.pie(
        set_values, labels=None, colors=colors1[:len(set_labels)],
        autopct='%1.1f%%', startangle=90, pctdistance=0.82,
        wedgeprops=dict(width=0.55, edgecolor='#1a1a2e', linewidth=2)
    )
    for at in autotexts:
        at.set_color('white'); at.set_fontsize(8.5)
    ax1.set_title('Distribusi per Set', color='white', fontsize=13, pad=15, fontweight='bold')
    ax1.legend(set_labels, loc='lower center', bbox_to_anchor=(0.5, -0.22),
               fontsize=8, facecolor='#16213e', labelcolor='white', ncol=2, framealpha=0.8)
    ax1.set_facecolor('#1a1a2e')

    # Chart 2: Horizontal bar per Kondisi
    cond_labels = [r[0] or "Near Mint" for r in cond_data]
    cond_values = [r[1] for r in cond_data]
    colors2     = ['#e94560', '#e0a50a', '#4ecca3', '#533483', '#0f3460', '#c0c0c0']
    bars        = ax2.barh(cond_labels, cond_values, color=colors2[:len(cond_labels)],
                           edgecolor='none', height=0.6)
    for bar, val in zip(bars, cond_values):
        ax2.text(bar.get_width() + max(cond_values) * 0.02, bar.get_y() + bar.get_height() / 2,
                 f'${val:.2f}', va='center', color='white', fontsize=9)
    ax2.set_title('Distribusi per Kondisi', color='white', fontsize=13, fontweight='bold')
    ax2.set_facecolor('#16213e')
    ax2.tick_params(colors='white')
    for spine in ['bottom', 'left']:
        ax2.spines[spine].set_color('#444')
    ax2.spines['top'].set_visible(False)
    ax2.spines['right'].set_visible(False)
    ax2.set_xlabel('Nilai (USD)', color='white', fontsize=10)

    plt.tight_layout(pad=3)

    buf = io.BytesIO()
    plt.savefig(buf, format='PNG', facecolor=fig.get_facecolor(), dpi=130, bbox_inches='tight')
    buf.seek(0)
    plt.close(fig)

    total_idr = total * EXCHANGE_RATE
    await update.message.reply_photo(
        photo=buf,
        caption=(
            f"📊 *Portfolio Chart*\n"
            f"💰 Total: {esc_usd(total)} \\| Rp {total_idr:,.0f}\n"
            f"🃏 Dari {sum(1 for _ in set_data)} set berbeda"
        ),
        parse_mode="MarkdownV2"
    )

# ── /findcheap — FITUR BARU ───────────────────────────────────────────────────
async def find_cheap(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    card_name = " ".join(context.args).strip()
    if not card_name:
        await update.message.reply_text(
            "⚠️ Format: `/findcheap Charizard`\n"
            "_Cari versi termurah dari semua set_",
            parse_mode="MarkdownV2"
        )
        return

    await update.message.reply_text(
        f"💸 Mencari versi termurah *{esc(card_name)}* dari semua set\\.\\.\\.",
        parse_mode="MarkdownV2"
    )

    results = await search_all_versions(card_name)

    if results is None:
        await update.message.reply_text(f"❌ '{esc(card_name)}' tidak ditemukan di manapun\\.", parse_mode="MarkdownV2")
        return
    if isinstance(results, dict) and results.get("error"):
        err = results["error"]
        msg = "⚠️ Rate limit\\!" if err == "rate_limit" else "⏱️ Timeout\\!" if err == "timeout" else "🔧 Pokemontcg\\.io lagi gangguan bre\\! Coba lagi nanti ya\\." if err == "api_down" else "❌ Gagal fetch data kartu bre, coba lagi\\!"
        await update.message.reply_text(msg, parse_mode="MarkdownV2")
        return

    with_price = [r for r in results if r["price_usd"] > 0]

    if not with_price:
        await update.message.reply_text(f"❌ Tidak ada harga tersedia untuk *{esc(card_name)}*\\.", parse_mode="MarkdownV2")
        return

    diff = with_price[-1]["price_usd"] - with_price[0]["price_usd"]

    lines = [
        f"💸 *Versi Termurah: {esc(with_price[0]['name'])}*\n",
        f"_Ditemukan {len(results)} versi, {len(with_price)} dengan harga_\n",
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
    ]

    for i, card in enumerate(with_price[:8], 1):
        emoji   = "🟢" if i == 1 else "🟡" if i <= 3 else "🔴"
        usd_str = esc_usd(card['price_usd'])
        idr_str = f"Rp {card['price_idr']:,.0f}"
        lines.append(
            f"{emoji} {i}\\. *{esc(card['name'])}*\n"
            f"   📦 {esc(card['set'])}\n"
            f"   ⭐ {esc(card['rarity'])}\n"
            f"   💵 {usd_str} \\| {idr_str}\n"
        )

    lines.append(f"━━━━━━━━━━━━━━━━━━━━━━\n")
    lines.append(f"💡 Selisih termurah vs termahal: *{esc_usd(diff)}*")

    await update.message.reply_text("\n".join(lines), parse_mode="MarkdownV2")

# ── /duplikat — FITUR BARU ────────────────────────────────────────────────────
async def show_duplikat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT card_name, COUNT(*) as cnt, SUM(price_usd) as total_usd
               FROM inventory WHERE user_id=?
               GROUP BY card_name HAVING cnt > 1
               ORDER BY cnt DESC, total_usd DESC""",
            (user_id,),
        ) as cur:
            dupes = await cur.fetchall()

    if not dupes:
        await update.message.reply_text(
            "✅ Tidak ada kartu duplikat di inventorymu\\!\n_Semua kartu unik\\._",
            parse_mode="MarkdownV2"
        )
        return

    total_extra = sum(r[1] - 1 for r in dupes)
    lines = [
        f"🔁 *Kartu Duplikat di Inventory:*\n",
        f"_{len(dupes)} nama kartu, {total_extra} salinan ekstra_\n"
    ]

    for name, cnt, total_usd in dupes:
        usd_str = f"{esc_usd(total_usd)}" if total_usd > 0 else "N/A"
        lines.append(
            f"📋 *{esc(name)}*\n"
            f"   🔢 Jumlah: {cnt}x \\| 💵 Total: {usd_str}\n"
        )

    lines.append("💡 _Gunakan /inventory untuk lihat nomor, /delete \\[no\\] untuk hapus ekstra_")
    await update.message.reply_text("\n".join(lines), parse_mode="MarkdownV2")

# ── /nilai — FITUR BARU ───────────────────────────────────────────────────────
async def nilai_kondisi(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT card_name, card_set, price_usd, condition FROM inventory WHERE user_id=? ORDER BY price_usd DESC",
            (user_id,),
        ) as cur:
            items = await cur.fetchall()

    if not items:
        await update.message.reply_text(
            "📂 Inventory kosong\\! Tambah kartu dulu dengan `/add`\\.",
            parse_mode="MarkdownV2"
        )
        return

    total_original = 0.0
    total_adjusted = 0.0

    lines = ["💎 *Nilai Portfolio Berdasar Kondisi:*\n"]

    for name, card_set, price_usd, condition in items:
        multiplier     = CONDITION_MULTIPLIERS.get(condition or "Near Mint", 1.0)
        adjusted       = price_usd * multiplier
        total_original += price_usd
        total_adjusted += adjusted

        if price_usd > 0:
            orig_str = f"{esc_usd(price_usd)}"
            adj_str  = f"{esc_usd(adjusted)}"
            pct      = int(multiplier * 100)
            cond_esc = esc(condition or "Near Mint")
            lines.append(
                f"🃏 *{esc(name)}* \\({cond_esc}\\)\n"
                f"   Market: {orig_str} → Real: *{adj_str}* \\({pct}%\\)\n"
            )

    diff     = total_adjusted - total_original
    diff_str = f"\\-{esc_usd(abs(diff))}" if diff < 0 else f"\\+{esc_usd(diff)}"

    lines.append(
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 *Ringkasan:*\n"
        f"• Market value: {esc_usd(total_original)}\n"
        f"• Nilai real \\(kondisi\\): *{esc_usd(total_adjusted)}* \\| Rp {total_adjusted * EXCHANGE_RATE:,.0f}\n"
        f"• Selisih: {diff_str}\n\n"
        f"🏷️ *Multiplier:* Mint/NM 100% \\| LP 80% \\| MP 65% \\| HP 50% \\| D 25%"
    )

    await update.message.reply_text("\n".join(lines), parse_mode="MarkdownV2")

# ── /lang — FITUR BARU ────────────────────────────────────────────────────────
async def set_language(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id

    if not context.args:
        lang = await get_user_lang(user_id)
        flag = "🇮🇩 Bahasa Indonesia" if lang == "id" else "🇬🇧 English"
        await update.message.reply_text(
            f"🌐 Bahasa aktif: *{flag}*\n\n"
            f"Ganti dengan:\n"
            f"• `/lang id` → Bahasa Indonesia 🇮🇩\n"
            f"• `/lang en` → English 🇬🇧",
            parse_mode="MarkdownV2"
        )
        return

    lang = context.args[0].lower()
    if lang not in ["id", "en"]:
        await update.message.reply_text("⚠️ Pilih: `/lang id` atau `/lang en`", parse_mode="MarkdownV2")
        return

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO user_settings (user_id, lang) VALUES (?,?)",
            (user_id, lang)
        )
        await db.commit()

    msg = (
        "✅ Bahasa diset ke *Bahasa Indonesia* 🇮🇩\n_Ketik /start untuk lihat menu_"
        if lang == "id" else
        "✅ Language set to *English* 🇬🇧\n_Type /start to see the menu_"
    )
    await update.message.reply_text(msg, parse_mode="MarkdownV2")

# ── /backup — FITUR BARU ──────────────────────────────────────────────────────
async def backup_db(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id  = update.effective_user.id
    username = update.effective_user.first_name or "user"

    await update.message.reply_text("⏳ Menyiapkan backup data\\.\\.\\.", parse_mode="MarkdownV2")

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, card_name, card_set, price_usd, price_idr, condition, psa_grade FROM inventory WHERE user_id=?",
            (user_id,)
        ) as cur:
            inventory = [
                {"id": r[0], "card_name": r[1], "card_set": r[2], "price_usd": r[3],
                 "price_idr": r[4], "condition": r[5], "psa_grade": r[6]}
                for r in await cur.fetchall()
            ]

        async with db.execute(
            "SELECT id, card_name, card_set, price_usd, price_idr, added_at FROM wishlist WHERE user_id=?",
            (user_id,)
        ) as cur:
            wishlist = [
                {"id": r[0], "card_name": r[1], "card_set": r[2], "price_usd": r[3],
                 "price_idr": r[4], "added_at": r[5]}
                for r in await cur.fetchall()
            ]

        async with db.execute(
            "SELECT card_name, card_set, price_usd, price_idr, recorded_at FROM price_history WHERE user_id=? ORDER BY recorded_at DESC LIMIT 500",
            (user_id,)
        ) as cur:
            history = [
                {"card_name": r[0], "card_set": r[1], "price_usd": r[2],
                 "price_idr": r[3], "recorded_at": r[4]}
                for r in await cur.fetchall()
            ]

        async with db.execute(
            "SELECT card_name, threshold_usd, COALESCE(alert_type,'turun'), created_at FROM price_alerts WHERE user_id=?",
            (user_id,)
        ) as cur:
            alerts = [
                {"card_name": r[0], "threshold_usd": r[1], "alert_type": r[2], "created_at": r[3]}
                for r in await cur.fetchall()
            ]

    total_usd   = sum(r["price_usd"] for r in inventory)
    backup_data = {
        "backup_info": {
            "user_id":     user_id,
            "username":    username,
            "backup_time": datetime.now().isoformat(),
            "version":     "2.0"
        },
        "summary": {
            "total_cards":          len(inventory),
            "total_wishlist":       len(wishlist),
            "total_history":        len(history),
            "total_alerts":         len(alerts),
            "portfolio_value_usd":  total_usd,
            "portfolio_value_idr":  total_usd * EXCHANGE_RATE,
        },
        "inventory":     inventory,
        "wishlist":      wishlist,
        "price_history": history,
        "price_alerts":  alerts,
    }

    json_bytes = json.dumps(backup_data, indent=2, ensure_ascii=False).encode("utf-8")
    timestamp  = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename   = f"pokemon_backup_{username}_{timestamp}.json"
    total_idr  = total_usd * EXCHANGE_RATE

    await update.message.reply_document(
        document=io.BytesIO(json_bytes),
        filename=filename,
        caption=(
            f"💾 *Backup Lengkap*\n\n"
            f"🃏 Inventory: *{len(inventory)} kartu*\n"
            f"⭐ Wishlist: *{len(wishlist)} kartu*\n"
            f"📊 History: *{len(history)} records*\n"
            f"🔔 Alerts: *{len(alerts)} aktif*\n\n"
            f"💰 Nilai Portfolio: {esc_usd(total_usd)} \\| Rp {total_idr:,.0f}\n\n"
            f"📅 {esc(datetime.now().strftime('%d %b %Y %H:%M'))}"
        ),
        parse_mode="MarkdownV2"
    )

# ── /setcondition ─────────────────────────────────────────────────────────────
async def set_condition(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id

    if len(context.args) < 2:
        cond_list = " \\| ".join(esc(c) for c in CARD_CONDITIONS)
        await update.message.reply_text(
            f"⚠️ Format: `/setcondition \\[no\\] \\[kondisi\\]`\n\nKondisi: {cond_list}",
            parse_mode="MarkdownV2"
        )
        return

    try:
        idx = int(context.args[0])
    except ValueError:
        await update.message.reply_text("⚠️ Nomor harus angka\\!", parse_mode="MarkdownV2")
        return

    condition = " ".join(context.args[1:]).strip()
    matched   = next((c for c in CARD_CONDITIONS if c.lower() == condition.lower()), None)
    if not matched:
        await update.message.reply_text(
            f"❌ Kondisi tidak valid\\! Pilihan: {esc(', '.join(CARD_CONDITIONS))}",
            parse_mode="MarkdownV2"
        )
        return

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT id, card_name FROM inventory WHERE user_id=? ORDER BY id", (user_id,)) as cur:
            items = await cur.fetchall()
        if idx < 1 or idx > len(items):
            await update.message.reply_text("❌ Nomor tidak ditemukan\\.", parse_mode="MarkdownV2")
            return
        db_id, card_name = items[idx - 1]
        await db.execute("UPDATE inventory SET condition=? WHERE id=?", (matched, db_id))
        await db.commit()

    await update.message.reply_text(
        f"✅ Kondisi *{esc(card_name)}* → *{esc(matched)}*",
        parse_mode="MarkdownV2"
    )

# ── /setgrade ─────────────────────────────────────────────────────────────────
# Grade valid: PSA 1-10 (bulat), BGS/CGC 1-10 (step 0.5)
_VALID_GRADERS = ["PSA", "BGS", "CGC"]
_GRADER_EMOJI  = {"PSA": "🏆", "BGS": "🎖", "CGC": "🥇"}

def _valid_grade_values(grader: str) -> list[str]:
    """Return daftar nilai grade valid untuk grader tertentu."""
    if grader == "PSA":
        return [str(i) for i in range(1, 11)]          # 1–10 bulat
    else:  # BGS / CGC
        vals = []
        v = 1.0
        while v <= 10.0:
            vals.append(str(int(v)) if v == int(v) else str(v))
            v = round(v + 0.5, 1)
        return vals

def _parse_grade_direct(args: list[str]) -> tuple[str, str] | None:
    """
    Parse args menjadi (grader, nilai) dari input langsung.
    Contoh: ["PSA", "10"] → ("PSA", "10")
            ["BGS", "9.5"] → ("BGS", "9.5")
    Return None kalau format tidak valid.
    """
    if len(args) < 2:
        return None
    grader = args[0].upper()
    if grader not in _VALID_GRADERS:
        return None
    nilai = args[1]
    valid = _valid_grade_values(grader)
    if nilai not in valid:
        return None
    return (grader, nilai)

def _build_setgrade_grader_kb(db_id: int) -> InlineKeyboardMarkup:
    """Keyboard pilih grader."""
    buttons = [
        InlineKeyboardButton(f"{_GRADER_EMOJI[g]} {g}", callback_data=f"setgrade_grader:{db_id}:{g}")
        for g in _VALID_GRADERS
    ]
    return InlineKeyboardMarkup([
        buttons,
        [InlineKeyboardButton("❌ Hapus Grade", callback_data=f"setgrade_grader:{db_id}:HAPUS")],
    ])

def _build_setgrade_nilai_kb(db_id: int, grader: str) -> InlineKeyboardMarkup:
    """Keyboard pilih nilai grade setelah grader dipilih."""
    vals  = _valid_grade_values(grader)
    rows  = []
    chunk = 5
    for i in range(0, len(vals), chunk):
        rows.append([
            InlineKeyboardButton(v, callback_data=f"setgrade_nilai:{db_id}:{grader}:{v}")
            for v in vals[i:i + chunk]
        ])
    rows.append([InlineKeyboardButton("◀️ Kembali", callback_data=f"setgrade_grader:{db_id}:BACK")])
    return InlineKeyboardMarkup(rows)

async def set_grade(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id

    if not context.args:
        await update.message.reply_text(
            "⚠️ Format: `/setgrade \\[no\\]` atau `/setgrade \\[no\\] \\[PSA/BGS/CGC\\] \\[nilai\\]`\n"
            "Contoh: `/setgrade 1` • `/setgrade 2 PSA 10` • `/setgrade 3 BGS 9\\.5`\n"
            "Hapus grade: `/setgrade 1 hapus`",
            parse_mode="MarkdownV2"
        )
        return

    try:
        idx = int(context.args[0])
    except ValueError:
        await update.message.reply_text("⚠️ Nomor harus angka\\!", parse_mode="MarkdownV2")
        return

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, card_name, psa_grade FROM inventory WHERE user_id=? ORDER BY id", (user_id,)
        ) as cur:
            items = await cur.fetchall()

    if idx < 1 or idx > len(items):
        await update.message.reply_text("❌ Nomor tidak ditemukan\\.", parse_mode="MarkdownV2")
        return

    db_id, card_name, current_grade = items[idx - 1]
    current_str = f" \\(sekarang: *{esc(current_grade)}*\\)" if current_grade else ""

    rest = context.args[1:]

    # ── mode hapus ────────────────────────────────────────────────────────────
    if rest and rest[0].lower() in ("hapus", "remove", "-", "none"):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE inventory SET psa_grade=NULL WHERE id=?", (db_id,))
            await db.commit()
        await update.message.reply_text(
            f"🗑️ Grade *{esc(card_name)}* dihapus\\.",
            parse_mode="MarkdownV2"
        )
        return

    # ── mode langsung: /setgrade 1 PSA 10 ────────────────────────────────────
    if len(rest) >= 2:
        parsed = _parse_grade_direct(rest)
        if parsed is None:
            valid_psa = "1\\-10"
            valid_bgs = "1\\-10 \\(step 0\\.5\\)"
            await update.message.reply_text(
                f"❌ Format grade tidak valid\\!\n"
                f"• PSA: `/setgrade {idx} PSA 10` \\(angka {valid_psa}\\)\n"
                f"• BGS: `/setgrade {idx} BGS 9\\.5` \\(angka {valid_bgs}\\)\n"
                f"• CGC: `/setgrade {idx} CGC 9` \\(angka {valid_bgs}\\)",
                parse_mode="MarkdownV2"
            )
            return
        grader, nilai = parsed
        grade_str = f"{grader} {nilai}"
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE inventory SET psa_grade=? WHERE id=?", (grade_str, db_id))
            await db.commit()
        emoji = _GRADER_EMOJI.get(grader, "🏆")
        await update.message.reply_text(
            f"{emoji} Grade *{esc(card_name)}* → *{esc(grade_str)}*",
            parse_mode="MarkdownV2"
        )
        return

    # ── mode inline keyboard: /setgrade 1 ────────────────────────────────────
    await update.message.reply_text(
        f"🏆 Set grade untuk *{esc(card_name)}*{current_str}\nPilih lembaga grading:",
        reply_markup=_build_setgrade_grader_kb(db_id),
        parse_mode="MarkdownV2"
    )


async def setgrade_grader_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Callback: user memilih grader atau tombol Kembali/Hapus."""
    query = update.callback_query
    await query.answer()
    _, db_id_str, grader = query.data.split(":", 2)
    db_id   = int(db_id_str)
    user_id = query.from_user.id

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT card_name, psa_grade FROM inventory WHERE id=? AND user_id=?", (db_id, user_id)
        ) as cur:
            row = await cur.fetchone()

    if not row:
        await query.edit_message_text("❌ Kartu tidak ditemukan\\.", parse_mode="MarkdownV2")
        return

    card_name, current_grade = row
    current_str = f" \\(sekarang: *{esc(current_grade)}*\\)" if current_grade else ""

    if grader == "BACK":
        await query.edit_message_text(
            f"🏆 Set grade untuk *{esc(card_name)}*{current_str}\nPilih lembaga grading:",
            reply_markup=_build_setgrade_grader_kb(db_id),
            parse_mode="MarkdownV2"
        )
        return

    if grader == "HAPUS":
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE inventory SET psa_grade=NULL WHERE id=?", (db_id,))
            await db.commit()
        await query.edit_message_text(
            f"🗑️ Grade *{esc(card_name)}* dihapus\\.",
            parse_mode="MarkdownV2"
        )
        return

    emoji = _GRADER_EMOJI.get(grader, "🏆")
    await query.edit_message_text(
        f"{emoji} *{esc(grader)}* — Pilih nilai grade untuk *{esc(card_name)}*:",
        reply_markup=_build_setgrade_nilai_kb(db_id, grader),
        parse_mode="MarkdownV2"
    )


async def setgrade_nilai_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Callback: user memilih nilai grade."""
    query = update.callback_query
    await query.answer()
    _, db_id_str, grader, nilai = query.data.split(":", 3)
    db_id   = int(db_id_str)
    user_id = query.from_user.id

    # Validasi sekali lagi di server side
    if nilai not in _valid_grade_values(grader):
        await query.answer("❌ Nilai tidak valid!", show_alert=True)
        return

    grade_str = f"{grader} {nilai}"
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT card_name FROM inventory WHERE id=? AND user_id=?", (db_id, user_id)
        ) as cur:
            row = await cur.fetchone()
        if not row:
            await query.edit_message_text("❌ Kartu tidak ditemukan\\.", parse_mode="MarkdownV2")
            return
        card_name = row[0]
        await db.execute("UPDATE inventory SET psa_grade=? WHERE id=?", (grade_str, db_id))
        await db.commit()

    emoji = _GRADER_EMOJI.get(grader, "🏆")
    await query.edit_message_text(
        f"{emoji} Grade *{esc(card_name)}* → *{esc(grade_str)}* ✅",
        parse_mode="MarkdownV2"
    )

# ── /wish ─────────────────────────────────────────────────────────────────────
async def add_wishlist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    card_query = " ".join(context.args).strip()
    if not card_query:
        await update.message.reply_text("⚠️ Format: `/wish Mewtwo ex`", parse_mode="MarkdownV2")
        return

    await update.message.reply_text(f"🌟 Menambahkan *{esc(card_query)}* ke wishlist\\.\\.\\.", parse_mode="MarkdownV2")

    card    = await search_pokemon_card(card_query)
    user_id = update.effective_user.id

    if not card or (isinstance(card, dict) and card.get("error")):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO wishlist (user_id, card_name, card_set, price_usd, price_idr) VALUES (?,?,?,?,?)",
                (user_id, card_query, "Unknown", 0.0, 0.0),
            )
            await db.commit()
        await update.message.reply_text(
            f"⭐ *{esc(card_query)}* ditambahkan ke wishlist \\(harga belum tersedia\\)\\.",
            parse_mode="MarkdownV2"
        )
        return

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO wishlist (user_id, card_name, card_set, price_usd, price_idr) VALUES (?,?,?,?,?)",
            (user_id, card["name"], card["set"], card["price_usd"], card["price_idr"]),
        )
        await db.commit()

    price_str = esc_usd(card['price_usd']) if card["price_usd"] > 0 else "N/A"
    idr_str   = f"Rp {card['price_idr']:,.0f}" if card["price_idr"] > 0 else "N/A"
    await update.message.reply_text(
        f"⭐ *{esc(card['name'])}* masuk wishlist\\!\n"
        f"📦 {esc(card['set'])} \\| 💵 {price_str} \\| {idr_str}",
        parse_mode="MarkdownV2",
    )

# ── /wishlist ─────────────────────────────────────────────────────────────────
async def show_wishlist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, card_name, card_set, price_usd, price_idr, added_at FROM wishlist WHERE user_id=? ORDER BY id",
            (user_id,),
        ) as cur:
            items = await cur.fetchall()

    if not items:
        await update.message.reply_text(
            "🌟 Wishlist kosong\\! Tambah dengan `/wish \\[nama\\]`\\.",
            parse_mode="MarkdownV2",
        )
        return

    total_usd = sum(r[3] for r in items)
    total_idr = sum(r[4] for r in items)

    lines = ["⭐ *Wishlist Pokémon:*\n"]
    for idx, (_, name, card_set, p_usd, p_idr, added_at) in enumerate(items, 1):
        usd_str  = f"{esc_usd(p_usd)}" if p_usd > 0 else "N/A"
        idr_str  = f"Rp {p_idr:,.0f}" if p_idr > 0 else "N/A"
        date_str = esc(added_at[:10]) if added_at else "\\-"
        set_str2 = f" \\({esc(card_set)}\\)" if card_set else ""
        lines.append(
            f"{idx}\\. *{esc(name)}*{set_str2}\n"
            f"   ├ 📅 {date_str}\n"
            f"   └ 💵 {usd_str} \\| {idr_str}\n"
        )

    lines.append(
        f"\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\n"
        f"💰 *Total Estimasi: {esc_usd(total_usd)} \\| Rp {total_idr:,.0f}*\n\n"
        f"🗑️ _/removewish \\[no\\]_"
    )

    await update.message.reply_text("\n".join(lines), parse_mode="MarkdownV2")

# ── /removewish ───────────────────────────────────────────────────────────────
async def remove_wishlist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id

    if not context.args:
        await update.message.reply_text("⚠️ Format: `/removewish 1`", parse_mode="MarkdownV2")
        return
    try:
        idx = int(context.args[0])
    except ValueError:
        await update.message.reply_text("⚠️ Nomor harus angka\\!", parse_mode="MarkdownV2")
        return

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT id, card_name FROM wishlist WHERE user_id=? ORDER BY id", (user_id,)) as cur:
            items = await cur.fetchall()
        if idx < 1 or idx > len(items):
            await update.message.reply_text("❌ Nomor tidak ditemukan di wishlist\\.", parse_mode="MarkdownV2")
            return
        db_id, card_name = items[idx - 1]
        await db.execute("DELETE FROM wishlist WHERE id=? AND user_id=?", (db_id, user_id))
        await db.commit()

    await update.message.reply_text(
        f"🗑️ *{esc(card_name)}* dihapus dari wishlist\\!",
        parse_mode="MarkdownV2",
    )

# ── /stats ────────────────────────────────────────────────────────────────────
async def show_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT card_name, card_set, price_usd, price_idr, condition, psa_grade FROM inventory WHERE user_id=? ORDER BY price_usd DESC",
            (user_id,),
        ) as cur:
            items = await cur.fetchall()

        async with db.execute("SELECT COUNT(*) FROM wishlist WHERE user_id=?", (user_id,)) as cur:
            wishlist_count = (await cur.fetchone())[0]

        async with db.execute("SELECT COUNT(*) FROM price_alerts WHERE user_id=?", (user_id,)) as cur:
            alerts_count = (await cur.fetchone())[0]

    if not items:
        await update.message.reply_text(
            "📊 Belum ada data\\. Tambah kartu dulu dengan `/add`\\!",
            parse_mode="MarkdownV2"
        )
        return

    total_usd = sum(r[2] for r in items)
    total_idr = sum(r[3] for r in items)
    avg_usd   = total_usd / len(items)

    # Nilai disesuaikan kondisi
    adjusted_total = sum(
        r[2] * CONDITION_MULTIPLIERS.get(r[4] or "Near Mint", 1.0)
        for r in items
    )

    top_card     = items[0]
    cheapest     = min((r for r in items if r[2] > 0), key=lambda x: x[2], default=None)
    graded_count = sum(1 for r in items if r[5])

    cond_counts: dict[str, int] = {}
    for r in items:
        c = r[4] or "Near Mint"
        cond_counts[c] = cond_counts.get(c, 0) + 1

    cond_lines = "\n".join(f"  • {esc(k)}: {v}" for k, v in sorted(cond_counts.items()))

    msg = (
        f"📊 *Statistik Portfolio v2*\n\n"
        f"🃏 Total Kartu: *{len(items)}*\n"
        f"⭐ Wishlist: *{wishlist_count}*\n"
        f"🏆 Kartu Graded: *{graded_count}*\n"
        f"🔔 Alerts Aktif: *{alerts_count}*\n\n"
        f"💰 *Nilai Portfolio:*\n"
        f"• Market value: {esc_usd(total_usd)} \\| Rp {total_idr:,.0f}\n"
        f"• Nilai real \\(kondisi\\): *{esc_usd(adjusted_total)}*\n"
        f"• Rata\\-rata: {esc_usd(avg_usd)}/kartu\n\n"
        f"🥇 *Termahal:*\n"
        f"  {esc(top_card[0])} \\— {esc_usd(top_card[2])}\n\n"
    )
    if cheapest:
        msg += f"💸 *Termurah:*\n  {esc(cheapest[0])} \\— {esc_usd(cheapest[2])}\n\n"
    msg += (
        f"🏷️ *Kondisi:*\n{cond_lines}\n\n"
        f"📈 _/history \\[nama\\]_ \\| 🏆 _/top10_ \\| 📊 _/portfoliochart_\n"
        f"💎 _/nilai_ \\| 💸 _/findcheap \\[nama\\]_ \\| 💾 _/backup_"
    )

    await update.message.reply_text(msg, parse_mode="MarkdownV2")

# ── /delete — soft delete ke tong sampah ─────────────────────────────────────
async def delete_inventory(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id

    if not context.args:
        await update.message.reply_text(
            "⚠️ Format: `/delete \\<nomor\\>`\n_Contoh: `/delete 1`_\n\n"
            "Kartu masuk tong sampah dulu, bisa di\\-restore dengan /trash",
            parse_mode="MarkdownV2",
        )
        return
    try:
        idx = int(context.args[0])
    except ValueError:
        await update.message.reply_text("⚠️ Nomor harus angka\\!", parse_mode="MarkdownV2")
        return

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, card_name, card_set, price_usd, price_idr, condition, psa_grade, "
            "buy_price_usd, photo_file_id, photo_file_id_back, tags "
            "FROM inventory WHERE user_id=? ORDER BY id",
            (user_id,),
        ) as cur:
            items = await cur.fetchall()
        if idx < 1 or idx > len(items):
            await update.message.reply_text("❌ Nomor tidak ditemukan\\.", parse_mode="MarkdownV2")
            return

        row = items[idx - 1]
        db_id, card_name = row[0], row[1]

        # Pindah ke tong sampah
        await db.execute(
            """INSERT INTO deleted_inventory
               (orig_id, user_id, card_name, card_set, price_usd, price_idr,
                condition, psa_grade, buy_price_usd, photo_file_id, photo_file_id_back, tags)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (db_id, user_id, row[1], row[2], row[3], row[4],
             row[5], row[6], row[7], row[8], row[9], row[10]),
        )
        # Hapus dari inventory
        await db.execute("DELETE FROM inventory WHERE id=? AND user_id=?", (db_id, user_id))
        # Ambil trash id yang baru dibuat
        async with db.execute(
            "SELECT id FROM deleted_inventory WHERE user_id=? AND orig_id=? ORDER BY id DESC LIMIT 1",
            (user_id, db_id),
        ) as cur:
            trash_row = await cur.fetchone()
        await db.commit()

    trash_id = trash_row[0] if trash_row else 0
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("↩️ Restore", callback_data=f"restore_card:{trash_id}"),
        InlineKeyboardButton("🗑️ Hapus Permanen", callback_data=f"purge_card:{trash_id}"),
    ]])
    await update.message.reply_text(
        f"🗑️ *{esc(card_name)}* dipindah ke tong sampah\\.\n\n"
        f"_Auto\\-hapus permanen dalam 30 hari\\._",
        parse_mode="MarkdownV2",
        reply_markup=keyboard,
    )


async def restore_card_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Callback: restore kartu dari tong sampah ke inventory."""
    query   = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    trash_id = int(query.data.split(":")[1])

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT card_name, card_set, price_usd, price_idr, condition, psa_grade, "
            "buy_price_usd, photo_file_id, photo_file_id_back, tags "
            "FROM deleted_inventory WHERE id=? AND user_id=?",
            (trash_id, user_id),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            await query.edit_message_text("⚠️ Kartu tidak ditemukan di tong sampah\\.", parse_mode="MarkdownV2")
            return
        await db.execute(
            """INSERT INTO inventory
               (user_id, card_name, card_set, price_usd, price_idr, condition,
                psa_grade, buy_price_usd, photo_file_id, photo_file_id_back, tags)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (user_id, row[0], row[1], row[2], row[3], row[4],
             row[5], row[6], row[7], row[8], row[9]),
        )
        await db.execute("DELETE FROM deleted_inventory WHERE id=?", (trash_id,))
        await db.commit()

    await query.edit_message_text(
        f"✅ *{esc(row[0])}* berhasil di\\-restore ke inventory\\!\n"
        f"_Cek dengan /inventory_",
        parse_mode="MarkdownV2",
    )


async def purge_card_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Callback: hapus permanen dari tong sampah."""
    query   = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    trash_id = int(query.data.split(":")[1])

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT card_name FROM deleted_inventory WHERE id=? AND user_id=?",
            (trash_id, user_id),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            await query.edit_message_text("⚠️ Kartu tidak ditemukan\\.", parse_mode="MarkdownV2")
            return
        await db.execute("DELETE FROM deleted_inventory WHERE id=? AND user_id=?", (trash_id, user_id))
        await db.commit()

    await query.edit_message_text(
        f"💀 *{esc(row[0])}* dihapus permanen\\.",
        parse_mode="MarkdownV2",
    )


# ── /trash — lihat & restore kartu yang dihapus ───────────────────────────────
async def show_trash(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id

    # Auto-purge kartu > 30 hari
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM deleted_inventory WHERE user_id=? AND deleted_at < datetime('now', '-30 days')",
            (user_id,),
        )
        await db.commit()
        async with db.execute(
            "SELECT id, card_name, card_set, price_usd, deleted_at "
            "FROM deleted_inventory WHERE user_id=? ORDER BY deleted_at DESC LIMIT 20",
            (user_id,),
        ) as cur:
            items = await cur.fetchall()

    if not items:
        await update.message.reply_text(
            "🗑️ Tong sampah kosong bre\\!\n_Kartu yang dihapus akan muncul di sini selama 30 hari\\._",
            parse_mode="MarkdownV2",
        )
        return

    lines = ["🗑️ *Tong Sampah* \\(30 hari terakhir\\)\n"]
    keyboard_rows = []
    for trash_id, name, card_set, price_usd, deleted_at in items:
        set_str   = f" \\({esc(card_set)}\\)" if card_set else ""
        price_str = esc_usd(price_usd) if price_usd else "N/A"
        date_str  = deleted_at[:10] if deleted_at else "?"
        lines.append(f"• *{esc(name)}*{set_str} — {price_str} \\| _dihapus {esc(date_str)}_")
        keyboard_rows.append([
            InlineKeyboardButton(f"↩️ {name[:20]}", callback_data=f"restore_card:{trash_id}"),
            InlineKeyboardButton("💀", callback_data=f"purge_card:{trash_id}"),
        ])

    lines.append(f"\n_↩️ Restore \\| 💀 Hapus permanen_")
    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="MarkdownV2",
        reply_markup=InlineKeyboardMarkup(keyboard_rows),
    )

# ── /export ───────────────────────────────────────────────────────────────────
async def export_inventory(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id  = update.effective_user.id
    username = update.effective_user.first_name or "user"

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT card_name, card_set, price_usd, price_idr, condition, psa_grade FROM inventory WHERE user_id=? ORDER BY id",
            (user_id,),
        ) as cur:
            items = await cur.fetchall()

    if not items:
        await update.message.reply_text("📂 Inventory kosong, belum bisa di\\-export\\.", parse_mode="MarkdownV2")
        return

    buf    = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["No", "Card Name", "Set", "Price USD", "Price IDR", "Condition", "PSA Grade", "Adjusted Value USD"])
    for idx, (name, card_set, p_usd, p_idr, condition, psa_grade) in enumerate(items, 1):
        mult     = CONDITION_MULTIPLIERS.get(condition or "Near Mint", 1.0)
        adjusted = p_usd * mult
        writer.writerow([idx, name, card_set, f"{p_usd:.2f}", f"{p_idr:.0f}", condition or "Near Mint", psa_grade or "", f"{adjusted:.2f}"])

    total_usd = sum(r[2] for r in items)
    total_idr = sum(r[3] for r in items)
    writer.writerow([])
    writer.writerow(["", "TOTAL", "", f"{total_usd:.2f}", f"{total_idr:.0f}", "", "", ""])

    csv_bytes = buf.getvalue().encode("utf-8-sig")

    await update.message.reply_document(
        document=io.BytesIO(csv_bytes),
        filename=f"pokemon_inventory_{username}.csv",
        caption=(
            f"📥 *Inventory Export*\n"
            f"Total: *{len(items)} kartu*\n"
            f"💵 {esc_usd(total_usd)} \\| 🇮🇩 Rp {esc(f'{total_idr:,.0f}')}"
        ),
        parse_mode="MarkdownV2",
    )

# ══════════════════════════════════════════════════════════════════════════════
# ── FITUR BARU v4 — Generation browse, newsets, cache, autocomplete ───────────
# ══════════════════════════════════════════════════════════════════════════════

# Mapping generasi → series TCG
GEN_SERIES_MAP: dict[int, list[str]] = {
    1: ["Base", "Gym"],
    2: ["Neo", "Southern Islands"],
    3: ["EX", "e-Card"],
    4: ["Diamond & Pearl", "Platinum", "HeartGold & SoulSilver"],
    5: ["Black & White"],
    6: ["XY"],
    7: ["Sun & Moon"],
    8: ["Sword & Shield"],
    9: ["Scarlet & Violet"],
}
GEN_LABEL = {
    1: "Gen 1 — Kanto 🔴", 2: "Gen 2 — Johto 🌿", 3: "Gen 3 — Hoenn 🌊",
    4: "Gen 4 — Sinnoh 💎", 5: "Gen 5 — Unova ⚫", 6: "Gen 6 — Kalos 🌸",
    7: "Gen 7 — Alola 🌺", 8: "Gen 8 — Galar 🗡️", 9: "Gen 9 — Paldea 🟣",
}

# ── Sync card cache dari TCG API ──────────────────────────────────────────────
async def sync_card_cache(max_pages: int = 20) -> int:
    """Ambil kartu dari TCG API dan simpan ke card_cache. Return jumlah yang di-insert."""
    inserted = 0
    page     = 1
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM card_cache")
        await db.commit()

        while page <= max_pages:
            try:
                async with httpx.AsyncClient(timeout=20) as client:
                    resp = await client.get(
                        "https://api.pokemontcg.io/v2/cards",
                        headers=_tcg_headers(),
                        params={
                            "page": page, "pageSize": 250,
                            "select": "id,name,set,tcgplayer",
                        },
                    )
                if resp.status_code != 200:
                    break
                data  = resp.json()
                cards = data.get("data", [])
                if not cards:
                    break

                rows = []
                for c in cards:
                    name     = c.get("name", "")
                    s        = c.get("set", {})
                    card_set = s.get("name", "")
                    series   = s.get("series", "")
                    set_id   = s.get("id", "")
                    rel_date = s.get("releaseDate", "")
                    prices   = (c.get("tcgplayer") or {}).get("prices", {})
                    price_usd = 0.0
                    for ptype in ("holofoil", "normal", "reverseHolofoil", "1stEditionHolofoil"):
                        pdata = prices.get(ptype, {})
                        mid   = pdata.get("mid") or pdata.get("market") or 0
                        if mid:
                            price_usd = float(mid)
                            break
                    rows.append((name, card_set, series, set_id, price_usd, rel_date))

                await db.executemany(
                    "INSERT INTO card_cache (name, card_set, set_series, set_id, price_usd, release_date) VALUES (?,?,?,?,?,?)",
                    rows,
                )
                await db.commit()
                inserted += len(rows)

                total_count = data.get("totalCount", 0)
                if page * 250 >= total_count:
                    break
                page += 1

            except Exception as e:
                logger.warning(f"sync_card_cache page {page} error: {e}")
                break

    return inserted


# ── /synccards — Sync manual ──────────────────────────────────────────────────
async def sync_cards_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = await update.message.reply_text(
        "🔄 Sinkronisasi database kartu dari TCG API\\.\\.\\.\n"
        "_Ini bisa makan waktu 1\\-2 menit_ ☕",
        parse_mode="MarkdownV2",
    )
    try:
        n = await sync_card_cache(max_pages=30)
        await msg.edit_text(
            f"✅ *Sinkronisasi selesai\\!*\n"
            f"📦 {esc(str(n))} kartu tersimpan di database lokal\\.",
            parse_mode="MarkdownV2",
        )
    except Exception as e:
        await msg.edit_text(f"❌ Gagal sync: {esc(str(e))}", parse_mode="MarkdownV2")


# ── Background job: auto-sync setiap 24 jam ───────────────────────────────────
async def auto_sync_cache(context) -> None:
    logger.info("Auto-sync card cache dimulai...")
    n = await sync_card_cache(max_pages=30)
    logger.info(f"Auto-sync selesai: {n} kartu")


# ── Autocomplete helper ───────────────────────────────────────────────────────
async def get_autocomplete_suggestions(query: str, limit: int = 5) -> list[str]:
    """Cari nama kartu mirip dari cache lokal."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT DISTINCT name FROM card_cache WHERE LOWER(name) LIKE LOWER(?) LIMIT 50",
            (f"%{query}%",),
        ) as cur:
            rows = await cur.fetchall()

    if rows:
        names = [r[0] for r in rows]
        return names[:limit]

    # fallback: fuzzy match dari semua nama
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT DISTINCT name FROM card_cache LIMIT 5000") as cur:
            all_names = [r[0] for r in await cur.fetchall()]

    if not all_names:
        return []

    matches = difflib.get_close_matches(query, all_names, n=limit, cutoff=0.5)
    return matches


# ── Patch handle_card_search → tambah autocomplete saat tidak ditemukan ───────
async def handle_card_search_v4(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    text    = update.message.text.strip()

    # ── Cek pending_editkartu_nama (/editkartu → edit nama) ───────────────────
    _ek_nama_id = await pop_ustate(user_id, "pending_editkartu_nama")
    if _ek_nama_id is not None:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE inventory SET card_name=? WHERE id=? AND user_id=?",
                (text, _ek_nama_id, user_id),
            )
            await db.commit()
        await update.message.reply_text(
            f"✅ Nama kartu \\#{_ek_nama_id} diubah jadi: *{esc(text)}*",
            parse_mode="MarkdownV2",
        )
        return

    # ── Cek pending_editkartu_harga (/editkartu → edit harga) ─────────────────
    _ek_harga_id = await get_ustate(user_id, "pending_editkartu_harga")
    if _ek_harga_id is not None:
        try:
            price_idr = parse_rupiah(text)
            if price_idr <= 0:
                raise ValueError("harga nol")
            price_usd = round(price_idr / EXCHANGE_RATE, 2)
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "UPDATE inventory SET price_usd=?, price_idr=? WHERE id=? AND user_id=?",
                    (price_usd, price_idr, _ek_harga_id, user_id),
                )
                await db.commit()
            await del_ustate(user_id, "pending_editkartu_harga")
            await update.message.reply_text(
                f"✅ Harga kartu \\#{_ek_harga_id} diubah jadi:\n"
                f"💵 {esc_usd(price_usd)} \\| Rp {esc(f'{price_idr:,.0f}')}",
                parse_mode="MarkdownV2",
            )
        except (ValueError, ZeroDivisionError):
            await update.message.reply_text(
                "⚠️ Masukkan angka Rupiah yang valid bre\\!\n_Contoh: `1500000`_",
                parse_mode="MarkdownV2",
            )
        return

    # ── Cek pending_manual_edit_nama ──────────────────────────────────────────
    _edit_nama = await pop_ustate(user_id, "pending_manual_edit_nama")
    if _edit_nama is not None:
        try:
            _edit_nama["name"] = text
            await set_ustate(user_id, "pending_manual_confirm", _edit_nama)
            await _show_manual_confirm(update.message, user_id, _edit_nama)
        except Exception as e:
            logger.error(f"[edit_nama] Error: {e}", exc_info=True)
            # Kembalikan state supaya user bisa coba lagi
            await set_ustate(user_id, "pending_manual_edit_nama", _edit_nama)
            await update.message.reply_text("⚠️ Ada error bre, coba ketik nama lagi\\.", parse_mode="MarkdownV2")
        return

    # ── Cek pending_manual_edit_harga ─────────────────────────────────────────
    card_name = await get_ustate(user_id, "pending_manual_edit_harga")
    if card_name is not None:
        try:
            price_idr = parse_rupiah(text)
            if price_idr <= 0:
                raise ValueError("harga nol")
            price_usd = round(price_idr / EXCHANGE_RATE, 2)
            data      = {"name": card_name, "price_idr": price_idr, "price_usd": price_usd}
            await set_ustate(user_id, "pending_manual_confirm", data)
            await _show_manual_confirm(update.message, user_id, data)
            await del_ustate(user_id, "pending_manual_edit_harga")  # hapus SETELAH berhasil
        except (ValueError, ZeroDivisionError):
            await update.message.reply_text(
                "⚠️ Masukkan angka Rupiah yang valid bre\\!\n_Contoh: `900000`_",
                parse_mode="MarkdownV2",
            )
        except Exception as e:
            logger.error(f"[edit_harga] Error: {e}", exc_info=True)
            await update.message.reply_text("⚠️ Ada error bre, coba ketik harga lagi\\.", parse_mode="MarkdownV2")
        return

    # ── Cek pending_manual_price: user ketik harga IDR ──────────────────────────
    card_name_pending = await get_ustate(user_id, "pending_manual_price")
    if card_name_pending and isinstance(card_name_pending, str):
        try:
            price_idr = parse_rupiah(text)
            if price_idr <= 0:
                raise ValueError("harga nol")
            price_usd = round(price_idr / EXCHANGE_RATE, 2)
            data      = {"name": card_name_pending, "price_idr": price_idr, "price_usd": price_usd}
            await set_ustate(user_id, "pending_manual_confirm", data)
            await _show_manual_confirm(update.message, user_id, data)
            await del_ustate(user_id, "pending_manual_price")  # hapus SETELAH berhasil kirim
        except (ValueError, ZeroDivisionError):
            await update.message.reply_text(
                "⚠️ Masukkan angka Rupiah yang valid bre\\!\n_Contoh: `900000`_",
                parse_mode="MarkdownV2",
            )
        except Exception as e:
            logger.error(f"[pending_manual_price] Error untuk user {user_id}: {e}", exc_info=True)
            # State TIDAK dihapus → user bisa ketik ulang
            await update.message.reply_text(
                "⚠️ Gagal kirim konfirmasi bre\\. Coba ketik nominalnya lagi\\:",
                parse_mode="MarkdownV2",
            )
        return

    # ── Cek pending_photo_name: user ketik nama manual setelah foto ──────────────
    _photo_name_flag = await pop_ustate(user_id, "pending_photo_name")
    if _photo_name_flag:
        # Langsung minta harga, tidak perlu cari API
        try:
            await set_ustate(user_id, "pending_manual_price", text)  # simpan nama langsung
            await update.message.reply_text(
                f"✅ Nama kartu: *{esc(text)}*\n\n"
                f"💰 Masukkan harga beli kamu \\(Rupiah\\)\\:\n_Contoh: `900000`_",
                parse_mode="MarkdownV2",
            )
        except Exception as e:
            logger.error(f"[photo_name] Error: {e}", exc_info=True)
            # Kembalikan flag supaya user bisa coba lagi
            await set_ustate(user_id, "pending_photo_name", True)
            await update.message.reply_text("⚠️ Ada error bre, coba ketik nama kartunya lagi\\.", parse_mode="MarkdownV2")
        return

    # ── Cek pending_buy: user balas harga modal setelah klik "Simpan + Set Modal" ──
    pending_id = await get_ustate(user_id, "pending_buy")
    if pending_id is not None:
        try:
            buy_usd = float(text.replace(",", "."))
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "UPDATE inventory SET buy_price_usd=? WHERE id=? AND user_id=?",
                    (buy_usd, pending_id, user_id),
                )
                await db.commit()
            buy_idr = buy_usd * EXCHANGE_RATE
            await del_ustate(user_id, "pending_buy")
            await update.message.reply_text(
                f"✅ Modal disimpan\\!\n"
                f"💵 *{esc_usd(buy_usd)}* \\(Rp {esc(f'{buy_idr:,.0f}')}\\) untuk inventory ID *\\#{pending_id}*\\.\n"
                f"_Gunakan /jual {pending_id} \\<harga\\> kalau mau jual nanti\\._",
                parse_mode="MarkdownV2",
            )
            return
        except ValueError:
            pass  # bukan angka → lanjut ke pencarian biasa

    query = text
    await update.message.reply_text(f"🔍 Mencari kartu *{esc(query)}*\\.\\.\\.", parse_mode="MarkdownV2")

    results = await search_pokemon_cards_multi(query, limit=5)

    not_found = (
        results is None
        or (isinstance(results, list) and len(results) == 0)
    )
    has_error = isinstance(results, dict) and results.get("error")

    if has_error:
        err = results["error"]
        if err == "rate_limit":
            await update.message.reply_text("⚠️ API rate limit, tunggu sebentar\\!", parse_mode="MarkdownV2")
        elif err == "timeout":
            await update.message.reply_text("⏱️ Timeout\\! Coba lagi ya\\.", parse_mode="MarkdownV2")
        elif err == "api_down":
            await update.message.reply_text(
                "🔧 Pokemontcg\\.io lagi gangguan bre\\! Coba lagi beberapa menit\\.",
                parse_mode="MarkdownV2",
            )
        else:
            await update.message.reply_text("❌ Gagal fetch data kartu bre, coba lagi\\!", parse_mode="MarkdownV2")
        return

    if not_found:
        # Coba autocomplete dari cache lokal
        suggestions = await get_autocomplete_suggestions(query, limit=6)
        if suggestions:
            lines = [f"❌ *'{esc(query)}'* tidak ditemukan\\.\n\n💡 *Maksud kamu mungkin:*"]
            for s in suggestions:
                lines.append(f"• `{esc(s)}`")
            lines.append("\n_Ketik nama yang tepat untuk cari\\._")
            await update.message.reply_text("\n".join(lines), parse_mode="MarkdownV2")
        else:
            await update.message.reply_text(
                f"❌ Kartu '{esc(query)}' tidak ditemukan, Bre\\!\n"
                "_Coba /synccards untuk update database lokal\\._",
                parse_mode="MarkdownV2",
            )
        return

    if len(results) == 1:
        await send_card(update, results[0])
        return

    keyboard = []
    for i, card in enumerate(results):
        price_str = f"${card['price_usd']:.2f}" if card["price_usd"] > 0 else "N/A"
        btn_label = f"{card['name']} ({card['set']}) — {price_str}"
        keyboard.append([InlineKeyboardButton(btn_label, callback_data=f"card_select:{i}:{update.effective_user.id}")])

    context.bot_data[f"search_{update.effective_user.id}"] = results

    await update.message.reply_text(
        f"🃏 Ditemukan *{len(results)} kartu* untuk *{esc(query)}*\\:\n_Pilih yang sesuai:_",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="MarkdownV2",
    )


# ── /newsets — Set TCG yang rilis dalam 1 tahun terakhir ─────────────────────
async def new_sets_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("📅 Mengambil daftar set terbaru\\.\\.\\.", parse_mode="MarkdownV2")
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                "https://api.pokemontcg.io/v2/sets",
                headers=_tcg_headers(),
                params={"orderBy": "-releaseDate", "pageSize": 50},
            )
        if resp.status_code != 200:
            await update.message.reply_text("❌ Gagal ambil data set\\.", parse_mode="MarkdownV2")
            return
        sets = resp.json().get("data", [])
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {esc(str(e))}", parse_mode="MarkdownV2")
        return

    cutoff  = (datetime.now() - timedelta(days=365)).strftime("%Y/%m/%d")
    recent  = [s for s in sets if s.get("releaseDate", "0") >= cutoff]

    if not recent:
        await update.message.reply_text("📭 Tidak ada set baru dalam 1 tahun terakhir\\.", parse_mode="MarkdownV2")
        return

    lines = [f"🆕 *Set Pokémon TCG — 1 Tahun Terakhir \\({len(recent)} set\\)*\n"]
    for s in recent[:20]:
        name     = s.get("name", "?")
        series   = s.get("series", "?")
        rel_date = s.get("releaseDate", "?")
        total    = s.get("total", "?")
        printed  = s.get("printedTotal", total)
        lines.append(
            f"📦 *{esc(name)}*\n"
            f"   Series: {esc(series)} \\| Rilis: {esc(rel_date)}\n"
            f"   🃏 {esc(str(printed))}/{esc(str(total))} kartu\n"
        )

    await update.message.reply_text("\n".join(lines), parse_mode="MarkdownV2")


# ── /newcards — Kartu mahal dari set terbaru ──────────────────────────────────
async def new_cards_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🆕 Mengambil kartu dari set terbaru\\.\\.\\.", parse_mode="MarkdownV2"
    )
    try:
        # Ambil set terbaru
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                "https://api.pokemontcg.io/v2/sets",
                headers=_tcg_headers(),
                params={"orderBy": "-releaseDate", "pageSize": 3},
            )
        if resp.status_code != 200:
            raise Exception("Gagal ambil set")
        latest_sets = resp.json().get("data", [])[:3]
        if not latest_sets:
            raise Exception("Tidak ada data set")

        all_cards = []
        async with httpx.AsyncClient(timeout=20) as client:
            for s in latest_sets:
                sid  = s.get("id", "")
                sname = s.get("name", "")
                r2 = await client.get(
                    "https://api.pokemontcg.io/v2/cards",
                    headers=_tcg_headers(),
                    params={"q": f"set.id:{sid}", "pageSize": 30,
                            "select": "id,name,set,tcgplayer,rarity"},
                )
                if r2.status_code == 200:
                    for c in r2.json().get("data", []):
                        prices = (c.get("tcgplayer") or {}).get("prices", {})
                        p_usd  = 0.0
                        for pt in ("holofoil", "normal", "reverseHolofoil"):
                            mid = (prices.get(pt) or {}).get("mid") or (prices.get(pt) or {}).get("market") or 0
                            if mid:
                                p_usd = float(mid)
                                break
                        all_cards.append({
                            "name": c.get("name", "?"),
                            "set":  sname,
                            "price_usd": p_usd,
                            "rarity": c.get("rarity", "?"),
                        })

        if not all_cards:
            await update.message.reply_text("📭 Tidak ada data kartu terbaru\\.", parse_mode="MarkdownV2")
            return

        # Sort by harga
        all_cards.sort(key=lambda x: x["price_usd"], reverse=True)
        top = all_cards[:15]

        lines = [f"🏆 *Kartu Termahal dari {len(latest_sets)} Set Terbaru*\n"]
        for i, c in enumerate(top, 1):
            p_str = esc_usd(c['price_usd']) if c["price_usd"] > 0 else "N/A"
            lines.append(
                f"{i}\\. *{esc(c['name'])}*\n"
                f"   📦 {esc(c['set'])} \\| ✨ {esc(c['rarity'])} \\| 💵 {p_str}\n"
            )

        await update.message.reply_text("\n".join(lines), parse_mode="MarkdownV2")

    except Exception as e:
        await update.message.reply_text(f"❌ Gagal: {esc(str(e))}", parse_mode="MarkdownV2")


# ── /gen <N> — Browse kartu berdasarkan generasi ─────────────────────────────
async def gen_browse(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        lines = ["🎮 *Browse per Generasi*\n", "_Gunakan: /gen \\<nomor\\>_\n"]
        for g, label in GEN_LABEL.items():
            lines.append(f"• /gen {g} — {esc(label)}")
        await update.message.reply_text("\n".join(lines), parse_mode="MarkdownV2")
        return

    try:
        gen_num = int(context.args[0])
    except ValueError:
        await update.message.reply_text("⚠️ Nomor generasi harus angka 1\\-9\\.", parse_mode="MarkdownV2")
        return

    if gen_num not in GEN_SERIES_MAP:
        await update.message.reply_text("⚠️ Generasi tersedia: 1 sampai 9\\.", parse_mode="MarkdownV2")
        return

    series_list = GEN_SERIES_MAP[gen_num]
    label       = GEN_LABEL[gen_num]

    await update.message.reply_text(
        f"🎮 Mengambil kartu *{esc(label)}*\\.\\.\\.", parse_mode="MarkdownV2"
    )

    # Coba dari cache lokal dulu
    async with aiosqlite.connect(DB_PATH) as db:
        placeholders = ",".join("?" * len(series_list))
        async with db.execute(
            f"SELECT name, card_set, price_usd FROM card_cache "
            f"WHERE set_series IN ({placeholders}) AND price_usd > 0 "
            f"ORDER BY price_usd DESC LIMIT 20",
            series_list,
        ) as cur:
            cached = await cur.fetchall()

    if cached:
        lines = [f"🎮 *{esc(label)}*\n_Top 20 by harga \\(dari cache lokal\\)_\n"]
        for i, (name, card_set, p_usd) in enumerate(cached, 1):
            p_str = f"{esc_usd(p_usd)}"
            lines.append(f"{i}\\. *{esc(name)}* — {esc(card_set or '-')} \\| {p_str}")
        lines.append(f"\n_Update cache: /synccards_")
        await update.message.reply_text("\n".join(lines), parse_mode="MarkdownV2")
        return

    # Fallback: ambil dari API
    try:
        query_parts = [f'set.series:"{s}"' for s in series_list]
        q_str       = " OR ".join(query_parts)
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(
                "https://api.pokemontcg.io/v2/cards",
                headers=_tcg_headers(),
                params={
                    "q":        q_str,
                    "pageSize": 30,
                    "orderBy":  "-tcgplayer.prices.holofoil.mid",
                    "select":   "id,name,set,tcgplayer,rarity",
                },
            )
        if resp.status_code != 200:
            raise Exception("API error")
        api_cards = resp.json().get("data", [])
        if not api_cards:
            await update.message.reply_text(
                f"📭 Tidak ada data kartu untuk {esc(label)}\\.\n_Coba /synccards dulu\\._",
                parse_mode="MarkdownV2",
            )
            return

        extracted = []
        for c in api_cards:
            prices = (c.get("tcgplayer") or {}).get("prices", {})
            p_usd  = 0.0
            for pt in ("holofoil", "normal", "reverseHolofoil"):
                mid = (prices.get(pt) or {}).get("mid") or (prices.get(pt) or {}).get("market") or 0
                if mid:
                    p_usd = float(mid)
                    break
            extracted.append({
                "name": c.get("name", "?"),
                "set":  (c.get("set") or {}).get("name", "?"),
                "price_usd": p_usd,
                "rarity": c.get("rarity", "?"),
            })
        extracted.sort(key=lambda x: x["price_usd"], reverse=True)

        lines = [f"🎮 *{esc(label)}*\n_Top kartu \\(dari API\\)_\n"]
        for i, c in enumerate(extracted[:20], 1):
            p_str = esc_usd(c['price_usd']) if c["price_usd"] > 0 else "N/A"
            lines.append(
                f"{i}\\. *{esc(c['name'])}* — {esc(c['set'])} \\| {esc(c['rarity'])} \\| {p_str}"
            )
        lines.append(f"\n_Untuk hasil lebih lengkap jalankan /synccards_")
        await update.message.reply_text("\n".join(lines), parse_mode="MarkdownV2")

    except Exception as e:
        await update.message.reply_text(
            f"❌ Gagal ambil data: {esc(str(e))}\n_Coba /synccards untuk build cache lokal\\._",
            parse_mode="MarkdownV2",
        )


# ── /cari <query> — Cari dari cache lokal ────────────────────────────────────
async def cari_lokal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text(
            "⚠️ Format: `/cari \\<nama\\>`\nContoh: `/cari Charizard`",
            parse_mode="MarkdownV2",
        )
        return

    query   = " ".join(context.args).strip()
    user_id = update.effective_user.id

    async with aiosqlite.connect(DB_PATH) as db:
        # Cari di inventory user dulu
        async with db.execute(
            "SELECT id, card_name, card_set, price_usd, price_idr, condition, psa_grade, photo_file_id "
            "FROM inventory WHERE user_id=? AND LOWER(card_name) LIKE LOWER(?) ORDER BY id",
            (user_id, f"%{query}%"),
        ) as cur:
            inv_rows = await cur.fetchall()
        # Cari di card cache (market)
        async with db.execute(
            "SELECT name, card_set, set_series, price_usd FROM card_cache "
            "WHERE LOWER(name) LIKE LOWER(?) ORDER BY price_usd DESC LIMIT 10",
            (f"%{query}%",),
        ) as cur:
            cache_rows = await cur.fetchall()

    if not inv_rows and not cache_rows:
        suggestions = await get_autocomplete_suggestions(query, limit=5)
        if suggestions:
            lines = [f"🔍 *'{esc(query)}'* tidak ditemukan\\.\n\n💡 *Mungkin maksudnya:*"]
            for s in suggestions:
                lines.append(f"• `{esc(s)}`")
            await update.message.reply_text("\n".join(lines), parse_mode="MarkdownV2")
        else:
            await update.message.reply_text(
                f"📭 Tidak ada kartu *'{esc(query)}'*\\.\n_Ketik nama langsung untuk cek harga real\\-time_",
                parse_mode="MarkdownV2",
            )
        return

    lines = [f"🔍 *Hasil Cari: '{esc(query)}'*\n"]

    if inv_rows:
        lines.append(f"*📦 Inventory Kamu \\({len(inv_rows)} kartu\\):*")
        for inv_id, name, card_set, p_usd, p_idr, condition, psa_grade, photo_file_id in inv_rows:
            usd_str   = f"{esc_usd(p_usd)}" if p_usd > 0 else "N/A"
            grade_str = f" \\| 🏆 {esc(psa_grade)}" if psa_grade else ""
            photo_str = " 📷" if photo_file_id else ""
            set_str   = f" \\({esc(card_set)}\\)" if card_set else ""
            lines.append(
                f"  \\#{inv_id} *{esc(name)}*{set_str}{grade_str}{photo_str}\n"
                f"  💵 {usd_str} \\| Rp {esc(f'{p_idr:,.0f}')} \\| {esc(condition or 'Near Mint')}"
            )
        lines.append("")

    if cache_rows:
        lines.append(f"*🌐 Market \\({len(cache_rows)} hasil\\):*")
        for name, card_set, series, p_usd in cache_rows:
            p_str = f"{esc_usd(p_usd)}" if p_usd > 0 else "N/A"
            lines.append(f"  • *{esc(name)}* — {esc(card_set or '?')} \\| {p_str}")
        lines.append("\n_Ketik nama kartu langsung untuk cek harga real\\-time_")

    await update.message.reply_text("\n".join(lines), parse_mode="MarkdownV2")


# ══════════════════════════════════════════════════════════════════════════════
# ── FITUR BARU v3 ─────────────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════

# ── /buyprice <id> <harga_usd> ────────────────────────────────────────────────
async def set_buyprice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if len(context.args) < 2:
        await update.message.reply_text(
            "⚠️ Format: `/buyprice \\<id\\> \\<harga_usd\\>`\n"
            "Contoh: `/buyprice 3 12\\.5`\n"
            "_Lihat ID kartu di /inventory_",
            parse_mode="MarkdownV2",
        )
        return

    try:
        card_id   = int(context.args[0])
        buy_price = float(context.args[1])
    except ValueError:
        await update.message.reply_text("⚠️ ID dan harga harus berupa angka\\.", parse_mode="MarkdownV2")
        return

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT card_name, card_set, price_usd FROM inventory WHERE id=? AND user_id=?",
            (card_id, user_id),
        ) as cur:
            row = await cur.fetchone()

        if not row:
            await update.message.reply_text("❌ Kartu tidak ditemukan di inventory kamu\\.", parse_mode="MarkdownV2")
            return

        card_name, card_set, market_usd = row
        await db.execute(
            "UPDATE inventory SET buy_price_usd=? WHERE id=? AND user_id=?",
            (buy_price, card_id, user_id),
        )
        await db.commit()

    profit     = market_usd - buy_price
    pct        = ((market_usd - buy_price) / buy_price * 100) if buy_price > 0 else 0
    emoji      = "📈" if profit >= 0 else "📉"
    profit_str = f"\\+{esc_usd(profit)}" if profit >= 0 else f"\\-{esc_usd(abs(profit))}"

    await update.message.reply_text(
        f"✅ *Harga beli disimpan\\!*\n\n"
        f"🃏 *{esc(card_name)}* \\({esc(card_set or '-')}\\)\n"
        f"💸 Harga Beli : {esc_usd(buy_price)}\n"
        f"💵 Market    : {esc_usd(market_usd)}\n"
        f"{emoji} Profit    : {profit_str} \\({esc(f'{esc(f"{pct:.1f}")}')}%\\)",
        parse_mode="MarkdownV2",
    )


# ── /roi — Ringkasan ROI seluruh inventory ────────────────────────────────────
async def show_roi(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT card_name, card_set, price_usd, price_idr, buy_price_usd, condition "
            "FROM inventory WHERE user_id=? ORDER BY (price_usd - buy_price_usd) DESC",
            (user_id,),
        ) as cur:
            items = await cur.fetchall()

    if not items:
        await update.message.reply_text("📂 Inventory kosong\\.", parse_mode="MarkdownV2")
        return

    tagged   = [(r) for r in items if r[4] > 0]
    untagged = [(r) for r in items if r[4] <= 0]

    if not tagged:
        await update.message.reply_text(
            "ℹ️ Belum ada kartu dengan harga beli\\.\n"
            "Set dulu dengan `/buyprice \\<id\\> \\<harga\\>`\\.",
            parse_mode="MarkdownV2",
        )
        return

    total_buy    = sum(r[4] for r in tagged)
    total_market = sum(r[2] for r in tagged)
    total_profit = total_market - total_buy
    total_pct    = (total_profit / total_buy * 100) if total_buy > 0 else 0
    emoji_total  = "📈" if total_profit >= 0 else "📉"

    lines = [f"💼 *ROI Portfolio \\({len(tagged)} kartu\\)*\n"]

    for name, card_set, p_usd, p_idr, buy_usd, condition in tagged[:15]:
        mult        = CONDITION_MULTIPLIERS.get(condition or "Near Mint", 1.0)
        adj_market  = p_usd * mult
        profit      = adj_market - buy_usd
        pct         = (profit / buy_usd * 100) if buy_usd > 0 else 0
        em          = "📈" if profit >= 0 else "📉"
        p_str       = f"\\+{esc(f"{profit:.2f}")}" if profit >= 0 else f"\\-{abs(profit):.2f}"
        lines.append(
            f"{em} *{esc(name[:25])}*\n"
            f"   Beli {esc_usd(buy_usd)} → Market {esc_usd(adj_market)} \\| {esc(f'{esc(f"{pct:.1f}")}')}%  \\(\\${p_str}\\)\n"
        )

    if len(tagged) > 15:
        lines.append(f"_\\.\\.\\. dan {len(tagged)-15} kartu lainnya_\n")

    lines.append(
        f"\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\n"
        f"{emoji_total} *Total Modal  : {esc_usd(total_buy)}*\n"
        f"{emoji_total} *Nilai Pasar  : {esc_usd(total_market)}*\n"
        f"{emoji_total} *Profit/Loss  : {esc(f"{total_profit:+.2f}")} \\({total_pct:+.1f}%\\)*"
    )
    if untagged:
        lines.append(f"\n_\\({len(untagged)} kartu belum ada harga beli — pakai /buyprice\\)_")

    await update.message.reply_text("\n".join(lines), parse_mode="MarkdownV2")


# ── /trend <nama kartu> — Grafik tren harga ───────────────────────────────────
async def price_trend_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text(
            "⚠️ Format: `/trend \\<nama kartu\\>`\nContoh: `/trend Charizard`",
            parse_mode="MarkdownV2",
        )
        return

    user_id   = update.effective_user.id
    card_query = " ".join(context.args).strip()

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT card_name, price_usd, recorded_at FROM price_history "
            "WHERE user_id=? AND LOWER(card_name) LIKE LOWER(?) "
            "ORDER BY recorded_at ASC LIMIT 60",
            (user_id, f"%{card_query}%"),
        ) as cur:
            rows = await cur.fetchall()

    if not rows:
        await update.message.reply_text(
            f"📭 Tidak ada data harga untuk *{esc(card_query)}*\\.\n"
            "_Data dicatat setiap kali kamu /refresh inventory\\._",
            parse_mode="MarkdownV2",
        )
        return

    if len(rows) < 2:
        await update.message.reply_text(
            f"📊 Baru *1 data poin* untuk *{esc(card_query)}*\\.\n"
            "_Butuh minimal 2 data untuk menampilkan tren\\. Lakukan /refresh beberapa kali\\._",
            parse_mode="MarkdownV2",
        )
        return

    if not HAS_MATPLOTLIB:
        lines = [f"📈 *Tren Harga: {esc(rows[0][0])}*\n"]
        for name, price, ts in rows:
            lines.append(f"• {esc(ts[:10])} — {esc_usd(price)}")
        await update.message.reply_text("\n".join(lines), parse_mode="MarkdownV2")
        return

    card_name_display = rows[0][0]
    dates  = [r[2][:10] for r in rows]
    prices = [r[1] for r in rows]

    fig, ax = plt.subplots(figsize=(9, 4))
    ax.plot(dates, prices, marker="o", linewidth=2, color="#E3350D", markersize=5)
    ax.fill_between(range(len(dates)), prices, alpha=0.15, color="#E3350D")
    ax.set_title(f"Tren Harga: {card_name_display}", fontsize=13, fontweight="bold")
    ax.set_ylabel("Harga (USD)")
    ax.set_xlabel("Tanggal")
    step = max(1, len(dates) // 6)
    ax.set_xticks(range(0, len(dates), step))
    ax.set_xticklabels(dates[::step], rotation=30, ha="right", fontsize=8)
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"${v:.2f}"))
    ax.grid(True, alpha=0.3)

    # Anotasi min/max
    min_i = prices.index(min(prices))
    max_i = prices.index(max(prices))
    ax.annotate(f"Low\n${prices[min_i]:.2f}", xy=(min_i, prices[min_i]),
                xytext=(0, -30), textcoords="offset points",
                ha="center", fontsize=8, color="blue",
                arrowprops=dict(arrowstyle="->", color="blue"))
    ax.annotate(f"High\n${prices[max_i]:.2f}", xy=(max_i, prices[max_i]),
                xytext=(0, 15), textcoords="offset points",
                ha="center", fontsize=8, color="green",
                arrowprops=dict(arrowstyle="->", color="green"))

    plt.tight_layout()
    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=130)
    buf.seek(0)
    plt.close(fig)

    delta     = prices[-1] - prices[0]
    delta_pct = (delta / prices[0] * 100) if prices[0] > 0 else 0
    trend_em  = "📈" if delta >= 0 else "📉"
    caption   = (
        f"{trend_em} {card_name_display}\n"
        f"Perubahan: ${delta:+.2f} ({delta_pct:+.1f}%) dari {len(rows)} data poin"
    )
    await update.message.reply_photo(photo=buf, caption=caption)


# ── /setkomplit <nama set> — Set completion tracker ───────────────────────────
async def set_completion(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text(
            "⚠️ Format: `/setkomplit \\<nama set\\>`\nContoh: `/setkomplit Base Set`",
            parse_mode="MarkdownV2",
        )
        return

    user_id  = update.effective_user.id
    set_query = " ".join(context.args).strip()

    await update.message.reply_text(
        f"🔍 Mengambil data set *{esc(set_query)}* dari TCG API\\.\\.\\.",
        parse_mode="MarkdownV2",
    )

    set_data = await search_pokemon_set(set_query, limit=200)

    if not set_data or set_data.get("error") or not set_data.get("cards"):
        await update.message.reply_text(
            f"❌ Set *{esc(set_query)}* tidak ditemukan\\.",
            parse_mode="MarkdownV2",
        )
        return

    set_name   = set_data.get("set_name", set_query)
    all_cards  = set_data["cards"]   # list of {name, ...}
    total_in_set = len(all_cards)

    # Ambil semua kartu user dari set ini
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT LOWER(card_name) FROM inventory WHERE user_id=?",
            (user_id,),
        ) as cur:
            owned_raw = await cur.fetchall()

    owned_names = {r[0] for r in owned_raw}

    have     = []
    missing  = []
    for c in all_cards:
        cname = c.get("name", "")
        if cname.lower() in owned_names:
            have.append(c)
        else:
            missing.append(c)

    pct      = (len(have) / total_in_set * 100) if total_in_set > 0 else 0
    bar_fill = int(pct / 10)
    bar      = "█" * bar_fill + "░" * (10 - bar_fill)

    # Estimasi biaya untuk kartu yang belum dimiliki
    missing_cost = sum(c.get("price_usd", 0) for c in missing if c.get("price_usd", 0) > 0)
    missing_idr  = missing_cost * EXCHANGE_RATE

    lines = [
        f"🏆 *{esc(set_name)}*\n",
        f"📊 Kelengkapan: *{esc(f'{esc(f"{pct:.1f}")}')}%* \\[{esc(bar)}\\]\n",
        f"✅ Dimiliki   : *{len(have)}/{total_in_set}* kartu\n",
        f"❌ Belum punya: *{len(missing)}* kartu\n",
    ]

    if missing_cost > 0:
        lines.append(
            f"💸 Estimasi beli semua yang kurang:\n"
            f"   {esc_usd(missing_cost)} \\| Rp {missing_idr:,.0f}\n"
        )

    if missing:
        lines.append(f"\n❌ *Belum punya \\({min(len(missing), 15)} ditampilkan\\):*")
        for c in missing[:15]:
            p = c.get("price_usd", 0)
            p_str = f" — {esc_usd(p)}" if p > 0 else ""
            lines.append(f"• {esc(c.get('name','?'))}{p_str}")
        if len(missing) > 15:
            lines.append(f"_\\.\\.\\. dan {len(missing)-15} kartu lagi_")

    await update.message.reply_text("\n".join(lines), parse_mode="MarkdownV2")


# ── /jual <id> <harga_usd> — Catat penjualan kartu ───────────────────────────
async def jual_kartu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id

    if len(context.args) < 2:
        await update.message.reply_text(
            "⚠️ Format: `/jual \\<id\\> \\<harga\\_jual\\_usd\\>`\n"
            "Contoh: `/jual 3 25\\.00`\n"
            "_Lihat ID di /inventory — kartu akan DIHAPUS dari inventory_",
            parse_mode="MarkdownV2",
        )
        return

    try:
        card_id    = int(context.args[0])
        sell_price = float(context.args[1])
    except ValueError:
        await update.message.reply_text("⚠️ ID dan harga harus berupa angka\\.", parse_mode="MarkdownV2")
        return

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT card_name, card_set, price_usd, price_idr, buy_price_usd "
            "FROM inventory WHERE id=? AND user_id=?",
            (card_id, user_id),
        ) as cur:
            row = await cur.fetchone()

        if not row:
            await update.message.reply_text("❌ Kartu tidak ditemukan di inventory kamu\\.", parse_mode="MarkdownV2")
            return

        card_name, card_set, market_usd, market_idr, buy_price = row
        sell_idr = sell_price * EXCHANGE_RATE
        profit   = sell_price - (buy_price or 0.0)

        # Simpan ke trade_log
        await db.execute(
            "INSERT INTO trade_log (user_id, card_name, card_set, sell_price_usd, sell_price_idr, buy_price_usd, profit_usd) "
            "VALUES (?,?,?,?,?,?,?)",
            (user_id, card_name, card_set or "", sell_price, sell_idr, buy_price or 0.0, profit),
        )
        # Hapus dari inventory
        await db.execute("DELETE FROM inventory WHERE id=? AND user_id=?", (card_id, user_id))
        await db.commit()

    em     = "📈" if profit >= 0 else "📉"
    p_str  = f"\\+{esc_usd(profit)}" if profit >= 0 else f"\\-{esc_usd(abs(profit))}"
    buy_str = f"{esc_usd(buy_price)}" if buy_price and buy_price > 0 else "N/A"

    await update.message.reply_text(
        f"💰 *Kartu Terjual\\!*\n\n"
        f"🃏 *{esc(card_name)}* \\({esc(card_set or '-')}\\)\n"
        f"💸 Harga Beli : {buy_str}\n"
        f"💵 Harga Jual : {esc_usd(sell_price)} \\| Rp {sell_idr:,.0f}\n"
        f"{em} Profit      : {p_str}\n\n"
        f"_Kartu dihapus dari inventory\\. Lihat riwayat di /riwayatjual_",
        parse_mode="MarkdownV2",
    )


# ── /riwayatjual — Riwayat semua transaksi jual ───────────────────────────────
async def riwayat_jual(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT card_name, card_set, sell_price_usd, sell_price_idr, buy_price_usd, profit_usd, sold_at "
            "FROM trade_log WHERE user_id=? ORDER BY sold_at DESC LIMIT 30",
            (user_id,),
        ) as cur:
            rows = await cur.fetchall()

    if not rows:
        await update.message.reply_text(
            "📭 Belum ada riwayat penjualan\\.\nJual kartu dengan `/jual \\<id\\> \\<harga\\>`\\.",
            parse_mode="MarkdownV2",
        )
        return

    total_profit  = sum(r[5] for r in rows)
    total_revenue = sum(r[2] for r in rows)
    em_total      = "📈" if total_profit >= 0 else "📉"

    lines = [f"📋 *Riwayat Penjualan \\({len(rows)} transaksi\\)*\n"]

    for name, card_set, sell_usd, sell_idr, buy_usd, profit, sold_at in rows:
        em    = "📈" if profit >= 0 else "📉"
        p_str = f"\\+{esc_usd(profit)}" if profit >= 0 else f"\\-{esc_usd(abs(profit))}"
        date  = esc(sold_at[:10]) if sold_at else "\\-"
        buy_s = f"{esc_usd(buy_usd)}" if buy_usd and buy_usd > 0 else "N/A"
        lines.append(
            f"{em} *{esc(name[:22])}*\n"
            f"   📅 {date} \\| Beli {buy_s} → Jual {esc_usd(sell_usd)} \\| {p_str}\n"
        )

    lines.append(
        f"\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\n"
        f"{em_total} *Total Revenue : {esc_usd(total_revenue)}*\n"
        f"{em_total} *Total Profit  : {esc(f"{total_profit:+.2f}")}*"
    )

    await update.message.reply_text("\n".join(lines), parse_mode="MarkdownV2")


# ── /wish override — tambah target_price ──────────────────────────────────────
# Ganti fungsi add_wishlist yang lama supaya support target harga opsional
# Format baru: /wish Charizard [target_usd]
async def add_wishlist_v3(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text(
            "⚠️ Format: `/wish \\<nama\\>` atau `/wish \\<nama\\> \\<target\\_usd\\>`\n"
            "Contoh: `/wish Charizard 20`",
            parse_mode="MarkdownV2",
        )
        return

    user_id      = update.effective_user.id
    target_price = 0.0

    # Cek apakah arg terakhir adalah angka (target harga)
    args = list(context.args)
    try:
        target_price = float(args[-1])
        card_query   = " ".join(args[:-1]).strip()
        if not card_query:
            raise ValueError
    except ValueError:
        card_query   = " ".join(args).strip()
        target_price = 0.0

    await update.message.reply_text(
        f"🌟 Menambahkan *{esc(card_query)}* ke wishlist\\.\\.\\.",
        parse_mode="MarkdownV2",
    )

    card = await search_pokemon_card(card_query)

    async with aiosqlite.connect(DB_PATH) as db:
        if not card or (isinstance(card, dict) and card.get("error")):
            await db.execute(
                "INSERT INTO wishlist (user_id, card_name, card_set, price_usd, price_idr, target_price_usd) VALUES (?,?,?,?,?,?)",
                (user_id, card_query, "Unknown", 0.0, 0.0, target_price),
            )
            await db.commit()
            target_str = f" \\| 🎯 Target: {esc_usd(target_price)}" if target_price > 0 else ""
            await update.message.reply_text(
                f"⭐ *{esc(card_query)}* ditambahkan ke wishlist{target_str} \\(harga belum tersedia\\)\\.",
                parse_mode="MarkdownV2",
            )
            return

        await db.execute(
            "INSERT INTO wishlist (user_id, card_name, card_set, price_usd, price_idr, target_price_usd) VALUES (?,?,?,?,?,?)",
            (user_id, card["name"], card["set"], card["price_usd"], card["price_idr"], target_price),
        )
        await db.commit()

    price_str  = esc_usd(card['price_usd']) if card["price_usd"] > 0 else "N/A"
    idr_str    = f"Rp {card['price_idr']:,.0f}" if card["price_idr"] > 0 else "N/A"
    target_str = f"\n🎯 Target alert: {esc_usd(target_price)}" if target_price > 0 else ""

    # Cek apakah harga sudah di bawah target
    hint = ""
    if target_price > 0 and card["price_usd"] > 0 and card["price_usd"] <= target_price:
        hint = "\n🔔 *Harga sekarang sudah di bawah target\\!*"

    await update.message.reply_text(
        f"⭐ *{esc(card['name'])}* masuk wishlist\\!\n"
        f"📦 {esc(card['set'])} \\| 💵 {price_str} \\| {idr_str}"
        f"{target_str}{hint}",
        parse_mode="MarkdownV2",
    )


# ── Background job: cek target harga wishlist ─────────────────────────────────
async def check_wishlist_targets(context) -> None:
    """Dijalankan bersamaan dengan price_alert_checker. Cek wishlist target."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT DISTINCT user_id FROM wishlist WHERE target_price_usd > 0"
        ) as cur:
            users = [r[0] for r in await cur.fetchall()]

    for user_id in users:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT id, card_name, price_usd, target_price_usd "
                "FROM wishlist WHERE user_id=? AND target_price_usd > 0",
                (user_id,),
            ) as cur:
                items = await cur.fetchall()

        for wid, card_name, current_usd, target_usd in items:
            if current_usd <= 0:
                continue
            if current_usd <= target_usd:
                try:
                    await context.bot.send_message(
                        chat_id=user_id,
                        text=(
                            f"🔔 *Wishlist Alert\\!*\n\n"
                            f"⭐ *{esc(card_name)}*\n"
                            f"Harga sekarang {esc_usd(current_usd)} sudah ≤ target {esc_usd(target_usd)}\\!\n"
                            f"_Saatnya beli\\!_ 🛒"
                        ),
                        parse_mode="MarkdownV2",
                    )
                except Exception as e:
                    logger.warning(f"Wishlist target notif gagal untuk user {user_id}: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# FITUR BARU v6

# ── /editprice <id> <harga_usd> — Edit harga manual kartu di inventory ────────
async def edit_price_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    args    = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "⚠️ Format: `/editprice <id> <harga_usd>`\n"
            "Contoh: `/editprice 3 25.5`\n\n"
            "_Gunakan /inventory untuk lihat ID kartu\\._",
            parse_mode="MarkdownV2",
        )
        return
    try:
        inv_id    = int(args[0])
        new_price = float(args[1].replace(",", "."))
        if new_price < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "❌ ID harus angka bulat, harga harus angka positif\\.\nContoh: `/editprice 3 25\\.5`",
            parse_mode="MarkdownV2",
        )
        return

    new_idr = new_price * EXCHANGE_RATE
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT card_name, card_set, price_usd FROM inventory WHERE id=? AND user_id=?",
            (inv_id, user_id),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            await update.message.reply_text(
                f"❌ Kartu ID \\#{inv_id} tidak ditemukan di inventory kamu\\.",
                parse_mode="MarkdownV2",
            )
            return
        card_name, card_set, old_price = row
        await db.execute(
            "UPDATE inventory SET price_usd=?, price_idr=? WHERE id=? AND user_id=?",
            (new_price, new_idr, inv_id, user_id),
        )
        await db.commit()

    diff      = new_price - (old_price or 0)
    diff_sign = f"\\+{esc_usd(diff)}" if diff >= 0 else f"\\-{esc_usd(abs(diff))}"
    diff_tag  = f"📈 {diff_sign}" if diff > 0 else (f"📉 {diff_sign}" if diff < 0 else "➡️ Sama")

    await update.message.reply_text(
        f"✅ *Harga diupdate\\!*\n\n"
        f"🃏 *{esc(card_name)}* _{esc(card_set or '')}_\n"
        f"ID: \\#{inv_id}\n\n"
        f"Harga lama: {esc_usd(old_price)}\n"
        f"Harga baru: *{esc_usd(new_price)}* \\(Rp {new_idr:,.0f}\\)\n"
        f"Selisih: {diff_tag}",
        parse_mode="MarkdownV2",
    )

# ══════════════════════════════════════════════════════════════════════════════

# ── 1. /hargalokal <nama> — Harga marketplace Tokopedia ──────────────────────
async def harga_lokal_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if not args:
        await update.message.reply_text(
            "⚠️ Format: `/hargalokal Charizard VMAX`", parse_mode="MarkdownV2"
        )
        return
    query = " ".join(args)
    await update.message.reply_text(
        f"🛒 Mencari harga lokal untuk *{esc(query)}*\\.\\.\\.", parse_mode="MarkdownV2"
    )
    search_q = f"{query} pokemon card"
    url = "https://ace.tokopedia.com/search/product/v3"
    params = {"q": search_q, "rows": 8, "start": 0, "ob": "23", "source": "search"}
    headers = {
        "User-Agent": "Mozilla/5.0 (Linux; Android 12) AppleWebKit/537.36",
        "Referer": "https://www.tokopedia.com/",
    }
    try:
        async with httpx.AsyncClient(timeout=15, headers=headers) as client:
            resp = await client.get(url, params=params)
        if resp.status_code != 200:
            raise ValueError(f"HTTP {resp.status_code}")
        data = resp.json()
        items = (
            data.get("data", {}).get("products", [])
            or data.get("products", [])
            or []
        )
    except Exception as e:
        # Fallback: link pencarian langsung
        toko_url = f"https://www.tokopedia.com/search?q={search_q.replace(' ', '+')}"
        await update.message.reply_text(
            f"⚠️ Gagal fetch otomatis\\.\n"
            f"[🔗 Cari di Tokopedia langsung]({toko_url})",
            parse_mode="MarkdownV2",
            disable_web_page_preview=False,
        )
        return

    if not items:
        toko_url = f"https://www.tokopedia.com/search?q={search_q.replace(' ', '+')}"
        await update.message.reply_text(
            f"❌ Tidak ada hasil di Tokopedia untuk *{esc(query)}*\\.\n"
            f"[🔗 Cari manual di Tokopedia]({toko_url})",
            parse_mode="MarkdownV2",
        )
        return

    lines = [f"🛒 *Harga Tokopedia — {esc(query)}*\n"]
    shown = 0
    for item in items[:6]:
        name  = item.get("name", "")[:45]
        price = item.get("price", {})
        if isinstance(price, dict):
            price_val = price.get("value", 0)
        else:
            price_val = int(str(price).replace(".", "").replace(",", "").replace("Rp", "").strip() or 0)
        shop  = item.get("shop", {}).get("name", "") or item.get("shopName", "")
        if price_val <= 0:
            continue
        price_usd = price_val / EXCHANGE_RATE
        lines.append(
            f"• *{esc(name[:40])}*\n"
            f"  Rp {price_val:,} \\(≈{esc_usd(price_usd)}\\) — _{esc(shop)}_"
        )
        shown += 1
    if shown == 0:
        lines.append("_Tidak ada harga yang bisa ditampilkan\\._")
    toko_url = f"https://www.tokopedia.com/search?q={search_q.replace(' ', '+')}"
    lines.append(f"\n[🔗 Lihat semua di Tokopedia]({toko_url})")
    await update.message.reply_text("\n".join(lines), parse_mode="MarkdownV2", disable_web_page_preview=True)


# ── 2. /portohistory — Grafik nilai portfolio dari waktu ke waktu ─────────────
async def save_portfolio_snapshot(user_id: int) -> None:
    """Simpan snapshot nilai portfolio hari ini (dipanggil otomatis)."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT price_usd, condition FROM inventory WHERE user_id=?", (user_id,)
        ) as cur:
            rows = await cur.fetchall()
    if not rows:
        return
    CONDITION_MULT = {
        "Mint": 1.0, "Near Mint": 1.0, "Lightly Played": 0.8,
        "Moderately Played": 0.65, "Heavily Played": 0.5, "Damaged": 0.25,
    }
    total_usd = sum(
        (r[0] or 0) * CONDITION_MULT.get(r[1] or "Near Mint", 1.0) for r in rows
    )
    total_idr = total_usd * EXCHANGE_RATE
    today = datetime.now().strftime("%Y-%m-%d")
    async with aiosqlite.connect(DB_PATH) as db:
        # Upsert: 1 snapshot per hari per user
        await db.execute(
            "DELETE FROM portfolio_snapshots WHERE user_id=? AND DATE(snapped_at)=?",
            (user_id, today),
        )
        await db.execute(
            "INSERT INTO portfolio_snapshots(user_id, total_usd, total_idr, card_count) VALUES(?,?,?,?)",
            (user_id, total_usd, total_idr, len(rows)),
        )
        await db.commit()


async def porto_history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    # Simpan snapshot hari ini dulu
    await save_portfolio_snapshot(user_id)
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT DATE(snapped_at), total_usd, card_count "
            "FROM portfolio_snapshots WHERE user_id=? "
            "ORDER BY snapped_at ASC LIMIT 30",
            (user_id,),
        ) as cur:
            rows = await cur.fetchall()
    if len(rows) < 2:
        await update.message.reply_text(
            "📈 Belum cukup data untuk grafik\\.\n"
            "_Gunakan bot setiap hari, data akan terkumpul otomatis\\._",
            parse_mode="MarkdownV2",
        )
        return
    if not HAS_MATPLOTLIB:
        lines = [f"📅 *Riwayat Nilai Portfolio*\n"]
        for date, usd, cnt in rows[-10:]:
            lines.append(f"• `{date}` — {esc_usd(usd)} \\({cnt} kartu\\)")
        await update.message.reply_text("\n".join(lines), parse_mode="MarkdownV2")
        return

    dates  = [r[0] for r in rows]
    values = [r[1] for r in rows]
    fig, ax = plt.subplots(figsize=(10, 5))
    ax.plot(dates, values, marker="o", color="#5C85FF", linewidth=2.5, markersize=5)
    ax.fill_between(range(len(dates)), values, alpha=0.15, color="#5C85FF")
    ax.set_xticks(range(len(dates)))
    ax.set_xticklabels(dates, rotation=45, ha="right", fontsize=8)
    ax.set_title(f"📈 Portfolio Value History", fontsize=13, fontweight="bold")
    ax.set_ylabel("USD ($)")
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"${x:.0f}"))
    ax.grid(True, alpha=0.3)
    plt.tight_layout()
    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=130)
    buf.seek(0)
    plt.close(fig)
    await update.message.reply_photo(
        buf,
        caption=f"📈 Portfolio kamu selama {len(rows)} hari terakhir\nNilai terkini: ${values[-1]:.2f}",
    )


# ── Auto-snapshot harian ──────────────────────────────────────────────────────
async def auto_snapshot_all(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Background job: simpan snapshot portfolio semua user aktif."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT DISTINCT user_id FROM inventory") as cur:
            user_ids = [r[0] for r in await cur.fetchall()]
    for uid in user_ids:
        try:
            await save_portfolio_snapshot(uid)
        except Exception as e:
            logger.warning(f"Snapshot gagal user {uid}: {e}")


# ── 3. Bulk Import CSV — /importcsv ──────────────────────────────────────────
async def handle_csv_upload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    User upload file CSV dengan kolom: card_name, card_set, buy_price_usd, condition
    (kolom lain diabaikan). Bot import semua ke inventory.
    """
    doc = update.message.document
    if not doc or not doc.file_name.lower().endswith(".csv"):
        return  # bukan CSV, abaikan

    await update.message.reply_text("📂 File CSV diterima\\! Sedang import\\.\\.\\.", parse_mode="MarkdownV2")
    user_id = update.effective_user.id

    try:
        file_obj = await doc.get_file()
        raw_bytes = await file_obj.download_as_bytearray()
        text = raw_bytes.decode("utf-8", errors="replace")
        reader = csv.DictReader(io.StringIO(text))
        rows = list(reader)
    except Exception as e:
        await update.message.reply_text(f"❌ Gagal baca CSV: {esc(str(e))}", parse_mode="MarkdownV2")
        return

    if not rows:
        await update.message.reply_text("⚠️ File CSV kosong\\.", parse_mode="MarkdownV2")
        return

    # Normalisasi header (case-insensitive, strip spasi)
    def get_col(row: dict, *keys: str) -> str:
        normalized = {k.strip().lower(): v for k, v in row.items()}
        for k in keys:
            if k.lower() in normalized:
                return normalized[k.lower()].strip()
        return ""

    imported = 0
    skipped  = 0
    async with aiosqlite.connect(DB_PATH) as db:
        for row in rows:
            name = get_col(row, "card_name", "name", "kartu", "nama")
            if not name:
                skipped += 1
                continue
            card_set  = get_col(row, "card_set", "set", "seri")
            condition = get_col(row, "condition", "kondisi") or "Near Mint"
            try:
                buy_price = float(get_col(row, "buy_price_usd", "harga_beli", "buy_price", "modal") or 0)
            except ValueError:
                buy_price = 0.0

            await db.execute(
                "INSERT INTO inventory(user_id, card_name, card_set, condition, buy_price_usd) VALUES(?,?,?,?,?)",
                (user_id, name, card_set, condition, buy_price),
            )
            imported += 1
        await db.commit()

    await update.message.reply_text(
        f"✅ *Import selesai\\!*\n"
        f"• Berhasil: *{imported} kartu*\n"
        f"• Dilewati \\(baris kosong\\): {skipped}\n\n"
        f"_Gunakan /refresh untuk update harga semua kartu\\._",
        parse_mode="MarkdownV2",
    )


# ── 4. /saraanjual — Rekomendasi kartu yang bagus dijual sekarang ─────────────
async def saran_jual_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, card_name, card_set, price_usd, buy_price_usd, condition "
            "FROM inventory WHERE user_id=? AND price_usd > 0",
            (user_id,),
        ) as cur:
            rows = await cur.fetchall()

    if not rows:
        await update.message.reply_text(
            "📦 Inventory kosong atau belum ada harga\\.\n_Coba /refresh dulu\\._",
            parse_mode="MarkdownV2",
        )
        return

    CONDITION_MULT = {
        "Mint": 1.0, "Near Mint": 1.0, "Lightly Played": 0.8,
        "Moderately Played": 0.65, "Heavily Played": 0.5, "Damaged": 0.25,
    }
    scored = []
    for inv_id, name, card_set, price_usd, buy_price, condition in rows:
        mult = CONDITION_MULT.get(condition or "Near Mint", 1.0)
        real_price = price_usd * mult
        buy = buy_price or 0.0
        roi = ((real_price - buy) / buy * 100) if buy > 0 else None
        scored.append({
            "id": inv_id, "name": name, "set": card_set or "",
            "price": real_price, "buy": buy, "roi": roi,
            "condition": condition or "Near Mint",
        })

    # Sort: ROI tertinggi (kalau ada modal), lalu harga terbesar
    with_roi    = sorted([s for s in scored if s["roi"] is not None], key=lambda x: x["roi"], reverse=True)
    without_roi = sorted([s for s in scored if s["roi"] is None], key=lambda x: x["price"], reverse=True)
    top = (with_roi + without_roi)[:8]

    if not top:
        await update.message.reply_text("⚠️ Tidak cukup data untuk saran jual\\.", parse_mode="MarkdownV2")
        return

    lines = ["💡 *SARAN KARTU YANG BAGUS DIJUAL SEKARANG*\n"]
    for i, c in enumerate(top, 1):
        price_idr = c["price"] * EXCHANGE_RATE
        roi_tag = f"ROI *\\+{c['roi']:.0f}%*" if c["roi"] and c["roi"] > 0 else (
                  f"ROI *{c['roi']:.0f}%* ⚠️" if c["roi"] else "ROI _belum diset_")
        lines.append(
            f"*{i}\\. {esc(c['name'])}* _\\({esc(c['set'])}\\)_\n"
            f"   💵 {esc_usd(c['price'])} \\(Rp {esc(f'{price_idr:,.0f}')}\\) — {roi_tag}\n"
            f"   Kondisi: {esc(c['condition'])} \\| ID: \\#{c['id']}"
        )
    lines.append("\n_Gunakan /jual \\<id\\> \\<harga\\> untuk catat penjualan\\._")
    await update.message.reply_text("\n\n".join(lines), parse_mode="MarkdownV2")


# ── Bootstrap ─────────────────────────────────────────────────────────────────
async def post_init(application) -> None:
    await init_db()
    logger.info("Database siap.")
    application.job_queue.run_repeating(
        check_price_alerts,
        interval=6 * 3600,
        first=60,
        name="price_alert_checker",
    )
    application.job_queue.run_repeating(
        check_wishlist_targets,
        interval=6 * 3600,
        first=120,
        name="wishlist_target_checker",
    )
    application.job_queue.run_repeating(
        auto_sync_cache,
        interval=24 * 3600,
        first=180,
        name="card_cache_syncer",
    )
    logger.info("Price alert + wishlist + card cache syncer dijadwalkan.")

    # v11 — auto-refresh harga harian jam 10:00 WIB (03:00 UTC)
    import datetime as _dt
    application.job_queue.run_daily(
        auto_refresh_all_users,
        time=_dt.time(3, 0, 0),
        name="auto_refresh_prices",
    )
    logger.info("Auto-refresh harga terjadwal (03:00 UTC / 10:00 WIB).")

async def debug_state(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT state_key, state_val FROM user_states WHERE user_id=?", (user_id,)
            ) as cur:
                rows = await cur.fetchall()
        if rows:
            lines = [f"🗃️ *Debug State untuk user {user_id}:*"]
            for k, v in rows:
                lines.append(f"• `{k}` = `{v}`")
            await update.message.reply_text("\n".join(lines), parse_mode="MarkdownV2")
        else:
            await update.message.reply_text(f"✅ Tidak ada state aktif untuk user {user_id}\\.", parse_mode="MarkdownV2")
    except Exception as e:
        await update.message.reply_text(f"❌ DB Error: `{str(e)}`", parse_mode="MarkdownV2")

# ══════════════════════════════════════════════════════════════════════════════
# ── FITUR BARU v7 ─────────────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════

# ── /setphoto <id> — Upload/update foto untuk kartu yang sudah ada ────────────
async def set_card_photo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not context.args:
        await update.message.reply_text(
            "📸 Format: `/setphoto \\<id\\>`\n_Contoh: `/setphoto 4`_\n\nID bisa dilihat di /inventory",
            parse_mode="MarkdownV2",
        )
        return
    try:
        card_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("⚠️ ID harus angka bre\\.", parse_mode="MarkdownV2")
        return

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT card_name FROM inventory WHERE id=? AND user_id=?",
            (card_id, user_id),
        ) as cur:
            row = await cur.fetchone()

    if not row:
        await update.message.reply_text("⚠️ Kartu tidak ditemukan bre\\.", parse_mode="MarkdownV2")
        return

    await set_ustate(user_id, "pending_setphoto_id", card_id)
    await update.message.reply_text(
        f"📸 Siap update foto *{esc(row[0])}* \\(ID: \\#{card_id}\\)\\!\n\n"
        f"Sekarang *kirim fotonya* bre 👇",
        parse_mode="MarkdownV2",
    )


# ── /editkartu <id> — Edit nama atau harga kartu ─────────────────────────────
async def editkartu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not context.args:
        await update.message.reply_text(
            "⚠️ Format: `/editkartu \\<id\\>`\n_Contoh: `/editkartu 4`_",
            parse_mode="MarkdownV2",
        )
        return
    try:
        card_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("⚠️ ID harus angka bre\\.", parse_mode="MarkdownV2")
        return

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT card_name, price_usd, price_idr, condition FROM inventory WHERE id=? AND user_id=?",
            (card_id, user_id),
        ) as cur:
            row = await cur.fetchone()

    if not row:
        await update.message.reply_text("⚠️ Kartu tidak ditemukan bre\\.", parse_mode="MarkdownV2")
        return

    card_name, price_usd, price_idr, condition = row
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Edit Nama",  callback_data=f"editkartu_nama:{card_id}:{user_id}")],
        [InlineKeyboardButton("💰 Edit Harga", callback_data=f"editkartu_harga:{card_id}:{user_id}")],
        [InlineKeyboardButton("❌ Batal",       callback_data=f"editkartu_batal:{card_id}:{user_id}")],
    ])
    await update.message.reply_text(
        f"✏️ *Edit Kartu \\#{card_id}*\n\n"
        f"🃏 Nama: *{esc(card_name)}*\n"
        f"💵 Harga: {esc_usd(price_usd)} \\| Rp {esc(f'{price_idr:,.0f}')}\n"
        f"🏷️ Kondisi: {esc(condition or 'Near Mint')}\n\n"
        f"Mau edit apa?",
        parse_mode="MarkdownV2",
        reply_markup=keyboard,
    )


async def editkartu_nama_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    parts   = query.data.split(":")
    card_id = int(parts[1])
    user_id = int(parts[2])
    if update.effective_user.id != user_id:
        return
    await set_ustate(user_id, "pending_editkartu_nama", card_id)
    await query.message.reply_text(
        f"✏️ Ketik *nama baru* untuk kartu \\#{card_id}:",
        parse_mode="MarkdownV2",
    )


async def editkartu_harga_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    parts   = query.data.split(":")
    card_id = int(parts[1])
    user_id = int(parts[2])
    if update.effective_user.id != user_id:
        return
    await set_ustate(user_id, "pending_editkartu_harga", card_id)
    await query.message.reply_text(
        f"💰 Ketik *harga baru* \\(Rupiah\\) untuk kartu \\#{card_id}:\n_Contoh: `1500000`_",
        parse_mode="MarkdownV2",
    )


async def editkartu_batal_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.message.edit_text("❌ Edit dibatalkan\\.", parse_mode="MarkdownV2")


# ── /portfolio — Ringkasan portfolio lengkap ──────────────────────────────────
async def show_portfolio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*), COALESCE(SUM(price_usd),0), COALESCE(SUM(price_idr),0), COALESCE(SUM(buy_price_usd),0) "
            "FROM inventory WHERE user_id=?",
            (user_id,),
        ) as cur:
            count, total_market_usd, total_market_idr, total_buy_usd = await cur.fetchone()
        async with db.execute(
            "SELECT COUNT(*), COALESCE(SUM(profit_usd),0) FROM trade_log WHERE user_id=?",
            (user_id,),
        ) as cur:
            sold_count, realized_profit = await cur.fetchone()
        async with db.execute(
            "SELECT COUNT(*) FROM inventory WHERE user_id=? AND photo_file_id IS NOT NULL",
            (user_id,),
        ) as cur:
            (photo_count,) = await cur.fetchone()
        async with db.execute(
            "SELECT COUNT(*) FROM inventory WHERE user_id=? AND psa_grade IS NOT NULL",
            (user_id,),
        ) as cur:
            (graded_count,) = await cur.fetchone()

    unrealized = total_market_usd - total_buy_usd
    roi_pct    = (unrealized / total_buy_usd * 100) if total_buy_usd > 0 else 0
    em_u       = "📈" if unrealized >= 0 else "📉"
    em_r       = "📈" if realized_profit >= 0 else "📉"

    await update.message.reply_text(
        f"💼 *Portfolio Summary*\n\n"
        f"🃏 Total Kartu     : *{count}* kartu\n"
        f"📷 Punya Foto      : *{photo_count}* kartu\n"
        f"🏆 Sudah Graded    : *{graded_count}* kartu\n\n"
        f"💰 Nilai Market    : *{esc_usd(total_market_usd)}*\n"
        f"   \\(≈ Rp {esc(f'{total_market_idr:,.0f}')}\\)\n"
        f"💸 Total Modal     : *{esc_usd(total_buy_usd)}*\n"
        f"{em_u} Unrealized P/L : *{esc(f'{unrealized:+.2f}')}* \\({roi_pct:+.1f}%\\)\n\n"
        f"📋 Sudah Terjual   : *{sold_count}* kartu\n"
        f"{em_r} Realized Profit: *{esc(f'{realized_profit:+.2f}')}*\n\n"
        f"_Detail: /roi \\| Riwayat: /riwayatjual \\| Grafik: /portfoliochart_",
        parse_mode="MarkdownV2",
    )


# ── /setalert <id> <persen> — Alert harga ±X% dari harga beli ────────────────
async def setalert_persen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if len(context.args) < 2:
        await update.message.reply_text(
            "⚠️ Format: `/setalert \\<id\\> \\<persen\\>`\n"
            "_Contoh: `/setalert 4 20` → notif kalau harga naik/turun 20% dari harga beli_\n\n"
            "ID kartu bisa dilihat di /inventory",
            parse_mode="MarkdownV2",
        )
        return
    try:
        card_id = int(context.args[0])
        persen  = float(context.args[1])
    except ValueError:
        await update.message.reply_text("⚠️ ID dan persen harus angka bre\\.", parse_mode="MarkdownV2")
        return

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT card_name, buy_price_usd, price_usd FROM inventory WHERE id=? AND user_id=?",
            (card_id, user_id),
        ) as cur:
            row = await cur.fetchone()

    if not row:
        await update.message.reply_text("⚠️ Kartu tidak ditemukan bre\\.", parse_mode="MarkdownV2")
        return

    card_name, buy_price, market_price = row
    if not buy_price or buy_price <= 0:
        await update.message.reply_text(
            f"⚠️ Kartu *{esc(card_name)}* belum ada harga beli\\.\n"
            f"Set dulu dengan `/buyprice {card_id} \\<harga\\_usd\\>`",
            parse_mode="MarkdownV2",
        )
        return

    threshold_naik  = round(buy_price * (1 + persen / 100), 2)
    threshold_turun = round(buy_price * (1 - persen / 100), 2)

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO price_alerts (user_id, card_name, threshold_usd, alert_type) VALUES (?,?,?,?)",
            (user_id, card_name, threshold_naik, "naik"),
        )
        if threshold_turun > 0:
            await db.execute(
                "INSERT INTO price_alerts (user_id, card_name, threshold_usd, alert_type) VALUES (?,?,?,?)",
                (user_id, card_name, threshold_turun, "turun"),
            )
        await db.commit()

    current_str = f"{esc_usd(market_price)}" if market_price and market_price > 0 else "N/A"
    await update.message.reply_text(
        f"🔔 *Alert ±{esc(str(persen))}% diset\\!*\n\n"
        f"🃏 *{esc(card_name)}*\n"
        f"💰 Harga Beli    : {esc_usd(buy_price)}\n"
        f"📊 Harga Market  : {current_str}\n\n"
        f"📈 Notif NAIK  ≥ {esc_usd(threshold_naik)} \\(\\+{esc(str(persen))}%\\)\n"
        f"📉 Notif TURUN ≤ {esc_usd(threshold_turun)} \\(\\-{esc(str(persen))}%\\)\n\n"
        f"_Bot cek harga setiap 6 jam\\. Lihat: /alerts_",
        parse_mode="MarkdownV2",
    )


# ── /share <id> — Generate gambar cantik kartu untuk di-share ────────────────
def _generate_share_image(
    name: str, card_set: str, price_usd: float, price_idr: float,
    condition: str, psa_grade: str
) -> bytes:
    """Buat gambar kartu bergaya untuk di-share. Return bytes PNG."""
    W, H = 800, 320
    # Background gelap bergradasi
    img  = Image.new("RGB", (W, H), (12, 12, 28))
    draw = ImageDraw.Draw(img)

    # Accent bar atas & bawah (gold)
    GOLD = (255, 215, 0)
    draw.rectangle([0, 0, W, 7], fill=GOLD)
    draw.rectangle([0, H - 7, W, H], fill=GOLD)

    # Helper font loader
    def load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
        font_paths = [
            f"/usr/share/fonts/truetype/dejavu/DejaVuSans{'\\-Bold' if bold else ''}.ttf",
            f"/usr/share/fonts/truetype/liberation/LiberationSans{'-Bold' if bold else ''}.ttf",
            "/system/fonts/Roboto-Regular.ttf",
        ]
        for fp in font_paths:
            try:
                return ImageFont.truetype(fp, size)
            except Exception:
                continue
        return ImageFont.load_default()

    f_big  = load_font(34, bold=True)
    f_med  = load_font(22)
    f_sm   = load_font(17)
    f_tiny = load_font(14)

    # Card name (potong kalau > 38 char)
    display_name = (name[:36] + "…") if len(name) > 38 else name
    draw.text((30, 25), display_name, fill=(255, 255, 255), font=f_big)

    # Card set
    if card_set:
        draw.text((30, 72), card_set, fill=(160, 160, 200), font=f_med)

    # Divider line
    draw.rectangle([30, 108, W - 30, 110], fill=(70, 70, 110))

    # Harga USD besar
    usd_text = f"${price_usd:.2f}"
    draw.text((30, 125), usd_text, fill=GOLD, font=load_font(48, bold=True))

    # Harga IDR
    idr_text = f"Rp {price_idr:,.0f}"
    draw.text((30, 185), idr_text, fill=(130, 190, 255), font=f_med)

    # Kondisi & grade chip
    info_parts = []
    if condition:
        info_parts.append(condition)
    if psa_grade:
        info_parts.append(f"🏆 {psa_grade}")
    if info_parts:
        chip_text = "  ".join(info_parts)
        draw.text((30, 222), chip_text, fill=(80, 220, 120), font=f_sm)

    # Dekorasi: Pokéball outline kanan
    cx, cy, r = 700, 160, 80
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=(50, 50, 90), width=2)
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=GOLD, width=1)
    draw.rectangle([cx - r, cy - 3, cx + r, cy + 3], fill=(50, 50, 90))
    draw.rectangle([cx - r + 1, cy - 2, cx + r - 1, cy + 2], fill=GOLD)
    draw.ellipse([cx - 18, cy - 18, cx + 18, cy + 18], outline=GOLD, width=1)

    # Watermark
    draw.text((30, H - 32), "PokeDex Price Bot", fill=(50, 50, 80), font=f_tiny)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


async def share_card(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not context.args:
        await update.message.reply_text(
            "🎨 Format: `/share \\<id\\>`\n_Contoh: `/share 4`_\n\nID bisa dilihat di /inventory",
            parse_mode="MarkdownV2",
        )
        return
    try:
        card_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("⚠️ ID harus angka bre\\.", parse_mode="MarkdownV2")
        return

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT card_name, card_set, price_usd, price_idr, condition, psa_grade "
            "FROM inventory WHERE id=? AND user_id=?",
            (card_id, user_id),
        ) as cur:
            row = await cur.fetchone()

    if not row:
        await update.message.reply_text("⚠️ Kartu tidak ditemukan bre\\.", parse_mode="MarkdownV2")
        return

    card_name, card_set, price_usd, price_idr, condition, psa_grade = row
    status_msg = await update.message.reply_text("🎨 Generating gambar\\.\\.\\.", parse_mode="MarkdownV2")

    try:
        img_bytes = await asyncio.to_thread(
            _generate_share_image,
            card_name, card_set or "", price_usd or 0, price_idr or 0,
            condition or "Near Mint", psa_grade or "",
        )
        caption_parts = [f"🃏 *{card_name}*"]
        if card_set:
            caption_parts.append(f"📦 {card_set}")
        if psa_grade:
            caption_parts.append(f"🏆 {psa_grade}")
        caption_parts.append(f"💵 ${price_usd:.2f} | Rp {price_idr:,.0f}")
        caption = "\n".join(caption_parts)

        await update.message.reply_photo(
            photo=io.BytesIO(img_bytes),
            caption=caption,
        )
        await status_msg.delete()
    except Exception as e:
        logger.error(f"share_card error: {e}", exc_info=True)
        await status_msg.edit_text("⚠️ Gagal buat gambar bre\\. Coba lagi\\.", parse_mode="MarkdownV2")


# ═══════════════════════════════════════════════════════════════════════════════
# ── v8 FEATURES ──────────────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════

# ── /setphoto2 <id> — set foto belakang ──────────────────────────────────────
async def set_card_photo2_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not context.args:
        await update.message.reply_text(
            "📸 Format: `/setphoto2 \\<id\\>`\n_Contoh: `/setphoto2 4`_\n\nID bisa dilihat di /inventory",
            parse_mode="MarkdownV2",
        )
        return
    try:
        card_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("⚠️ ID harus angka bre\\.", parse_mode="MarkdownV2")
        return
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT card_name FROM inventory WHERE id=? AND user_id=?",
            (card_id, user_id),
        ) as cur:
            row = await cur.fetchone()
    if not row:
        await update.message.reply_text("⚠️ Kartu tidak ditemukan bre\\.", parse_mode="MarkdownV2")
        return
    await set_ustate(user_id, "pending_setphoto_back_id", card_id)
    await update.message.reply_text(
        f"📸 Siap update *foto belakang* kartu *{esc(row[0])}* \\(ID: \\#{card_id}\\)\\!\n\n"
        f"Sekarang *kirim fotonya* bre 👇",
        parse_mode="MarkdownV2",
    )


# ── /photo2 <id> — lihat foto belakang ───────────────────────────────────────
async def show_card_photo2(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not context.args:
        await update.message.reply_text(
            "Format: `/photo2 \\<id\\>`\n_Contoh: `/photo2 3`_\n\nID bisa dilihat di /inventory",
            parse_mode="MarkdownV2",
        )
        return
    try:
        card_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("⚠️ ID harus angka bre\\.", parse_mode="MarkdownV2")
        return
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT card_name, card_set, price_usd, photo_file_id_back FROM inventory WHERE id=? AND user_id=?",
            (card_id, user_id),
        ) as cur:
            row = await cur.fetchone()
    if not row:
        await update.message.reply_text("⚠️ Kartu tidak ditemukan bre\\.", parse_mode="MarkdownV2")
        return
    card_name, card_set, price_usd, photo_back = row
    if not photo_back:
        await update.message.reply_text(
            f"📷 *{esc(card_name)}* belum punya foto belakang bre\\.\n"
            f"_Tambah dengan: /setphoto2 {card_id}_",
            parse_mode="MarkdownV2",
        )
        return
    caption = (
        f"📷 *{esc(card_name)}* \\(belakang\\)"
        + (f" \\| {esc(card_set)}" if card_set else "")
        + (f"\n💵 {esc_usd(price_usd)}" if price_usd and price_usd > 0 else "")
    )
    await update.message.reply_photo(
        photo=photo_back,
        caption=caption,
        parse_mode="MarkdownV2",
    )


# ── /updateharga — update semua harga market inventory ───────────────────────
async def updateharga_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, card_name, card_set, price_usd FROM inventory WHERE user_id=? ORDER BY id",
            (user_id,),
        ) as cur:
            cards = await cur.fetchall()

    if not cards:
        await update.message.reply_text("📂 Inventory kosong bre\\.", parse_mode="MarkdownV2")
        return

    status_msg = await update.message.reply_text(
        f"🔄 Update harga *{len(cards)} kartu*\\.\\.\\.\n_Ini mungkin butuh beberapa menit_",
        parse_mode="MarkdownV2",
    )

    updated, failed, changes = 0, 0, []
    for card_id, card_name, card_set, old_usd in cards:
        result = await search_pokemon_card(card_name)
        if result and not (isinstance(result, dict) and result.get("error")):
            new_usd = result.get("price_usd") or 0
            if new_usd > 0:
                new_idr = round(new_usd * EXCHANGE_RATE)
                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute(
                        "UPDATE inventory SET price_usd=?, price_idr=? WHERE id=? AND user_id=?",
                        (new_usd, new_idr, card_id, user_id),
                    )
                    await db.execute(
                        "INSERT INTO price_history (user_id, card_name, card_set, price_usd, price_idr) VALUES (?,?,?,?,?)",
                        (user_id, card_name, card_set, new_usd, new_idr),
                    )
                    await db.commit()
                updated += 1
                diff = new_usd - (old_usd or 0)
                if abs(diff) > 0.01:
                    arrow = "📈" if diff > 0 else "📉"
                    changes.append(f"{arrow} *{esc(card_name)}*: {esc_usd(old_usd or 0)} → {esc_usd(new_usd)}")
            else:
                failed += 1
        else:
            failed += 1
        await asyncio.sleep(0.3)  # rate limit protection

    change_text = "\n".join(changes[:15]) if changes else "_Tidak ada perubahan signifikan_"
    if len(changes) > 15:
        change_text += f"\n_\\.\\.\\. \\+{len(changes)-15} lainnya_"

    await status_msg.edit_text(
        f"✅ *Update Harga Selesai\\!*\n\n"
        f"🔄 Diperbarui: *{updated}* kartu\n"
        f"⚠️ Tidak ada data: *{failed}* kartu\n\n"
        f"📊 *Perubahan:*\n{change_text}",
        parse_mode="MarkdownV2",
    )


# ── /prediksi <nama> — trend analysis + rekomendasi ──────────────────────────
async def prediksi_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not context.args:
        await update.message.reply_text(
            "📊 Format: `/prediksi \\<nama\\>`\n_Contoh: `/prediksi Pikachu`_",
            parse_mode="MarkdownV2",
        )
        return

    card_name = " ".join(context.args)
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT price_usd, recorded_at FROM price_history
               WHERE user_id=? AND LOWER(card_name) LIKE LOWER(?)
               ORDER BY recorded_at ASC LIMIT 60""",
            (user_id, f"%{card_name}%"),
        ) as cur:
            rows = await cur.fetchall()

    # Fetch current market price
    market = await search_pokemon_card(card_name)
    current_usd = 0.0
    if market and not (isinstance(market, dict) and market.get("error")):
        current_usd = market.get("price_usd") or 0

    if not rows or len(rows) < 2:
        current_str = f" Harga pasar saat ini: {esc_usd(current_usd)}" if current_usd > 0 else ""
        await update.message.reply_text(
            f"📊 *{esc(card_name)}*\n\n"
            f"⚠️ Data history minimal 2 titik dibutuhkan untuk prediksi\\.\n"
            f"{current_str}\n\n"
            f"_Gunakan /refresh untuk simpan data harga ke history_",
            parse_mode="MarkdownV2",
        )
        return

    prices = [r[0] for r in rows]
    dates  = [r[1] for r in rows]
    n      = len(prices)

    first_price = prices[0]
    last_price  = prices[-1]
    avg_price   = sum(prices) / n
    min_price   = min(prices)
    max_price   = max(prices)

    # Simple linear regression slope
    x_vals = list(range(n))
    x_mean = (n - 1) / 2.0
    y_mean = avg_price
    num = sum((x_vals[i] - x_mean) * (prices[i] - y_mean) for i in range(n))
    den = sum((x_vals[i] - x_mean) ** 2 for i in range(n))
    slope = num / den if den else 0

    change_pct = ((last_price - first_price) / first_price * 100) if first_price > 0 else 0

    # Predict next price (next data point)
    predicted = last_price + slope
    predicted_idr = round(predicted * EXCHANGE_RATE)

    # Recommendation
    if change_pct >= 15:
        rekomendasi = "🟢 *JUAL* — Harga naik signifikan\\! Waktu bagus untuk jual\\."
        trend_emoji = "📈"
    elif change_pct >= 5:
        rekomendasi = "🟡 *HOLD* — Harga naik, tunggu lebih tinggi atau jual sekarang\\."
        trend_emoji = "📈"
    elif change_pct <= -15:
        rekomendasi = "🔵 *BELI* — Harga turun banyak\\! Kesempatan beli sekarang\\."
        trend_emoji = "📉"
    elif change_pct <= -5:
        rekomendasi = "🟡 *HOLD* — Harga sedang turun, tunggu lebih rendah dulu\\."
        trend_emoji = "📉"
    else:
        rekomendasi = "⚪ *HOLD* — Harga stabil, tidak ada sinyal kuat beli/jual\\."
        trend_emoji = "➡️"

    date_from = dates[0][:10] if dates else "?"
    date_to   = dates[-1][:10] if dates else "?"
    change_sign = "+" if change_pct >= 0 else ""

    lines = [
        f"{trend_emoji} *Analisis Tren: {esc(card_name)}*",
        f"",
        f"📅 Data: {esc(date_from)} → {esc(date_to)} \\({n} titik\\)",
        f"",
        f"📊 *Ringkasan Harga \\(History\\)*",
        f"├ Pertama: {esc_usd(first_price)}",
        f"├ Terakhir: {esc_usd(last_price)}",
        f"├ Rata\\-rata: {esc_usd(avg_price)}",
        f"├ Min: {esc_usd(min_price)} \\| Max: {esc_usd(max_price)}",
        f"└ Perubahan: *{esc(f'{change_sign}{change_pct:.1f}%')}*",
        f"",
    ]
    if current_usd > 0:
        lines.append(f"💹 Harga Pasar Saat Ini: {esc_usd(current_usd)}")
    lines += [
        f"🔮 Prediksi Berikutnya: *{esc_usd(max(predicted, 0))}* \\| Rp {max(predicted_idr, 0):,.0f}",
        f"",
        f"💡 *Rekomendasi*",
        f"{rekomendasi}",
    ]
    await update.message.reply_text("\n".join(lines), parse_mode="MarkdownV2")


# ── /tag <id> <label> — label custom per kartu ───────────────────────────────
async def tag_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if len(context.args) < 2:
        await update.message.reply_text(
            "🏷 Format: `/tag \\<id\\> \\<label\\>`\n"
            "_Contoh: `/tag 4 favorit`_\n"
            "_Hapus tag: `/tag 4 hapus`_\n\n"
            "ID bisa dilihat di /inventory",
            parse_mode="MarkdownV2",
        )
        return
    try:
        card_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("⚠️ ID harus angka bre\\.", parse_mode="MarkdownV2")
        return

    label = " ".join(context.args[1:])
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT card_name FROM inventory WHERE id=? AND user_id=?",
            (card_id, user_id),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            await update.message.reply_text("⚠️ Kartu tidak ditemukan bre\\.", parse_mode="MarkdownV2")
            return
        card_name = row[0]
        if label.lower() in ("hapus", "clear", "delete"):
            await db.execute(
                "UPDATE inventory SET tags=NULL WHERE id=? AND user_id=?",
                (card_id, user_id),
            )
            await db.commit()
            await update.message.reply_text(
                f"🗑️ Tag kartu *{esc(card_name)}* \\(\\#{card_id}\\) dihapus\\.",
                parse_mode="MarkdownV2",
            )
        else:
            await db.execute(
                "UPDATE inventory SET tags=? WHERE id=? AND user_id=?",
                (label, card_id, user_id),
            )
            await db.commit()
            await update.message.reply_text(
                f"🏷 Tag *\\#{esc(label)}* berhasil ditambahkan ke *{esc(card_name)}* \\(\\#{card_id}\\)\\!\n"
                f"_Lihat di /inventory_",
                parse_mode="MarkdownV2",
            )


# ── /remind <id> <hari> — reminder cek harga ─────────────────────────────────
async def remind_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if len(context.args) < 2:
        await update.message.reply_text(
            "⏰ Format: `/remind \\<id\\> \\<hari\\>`\n"
            "_Contoh: `/remind 4 7`_ \\(ingatkan 7 hari lagi\\)\n\n"
            "ID bisa dilihat di /inventory",
            parse_mode="MarkdownV2",
        )
        return
    try:
        card_id = int(context.args[0])
        days    = int(context.args[1])
        if days < 1 or days > 365:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "⚠️ Format salah\\. Gunakan: `/remind \\<id\\> \\<hari\\>`\n_Hari harus 1–365_",
            parse_mode="MarkdownV2",
        )
        return

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT card_name FROM inventory WHERE id=? AND user_id=?",
            (card_id, user_id),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            await update.message.reply_text("⚠️ Kartu tidak ditemukan bre\\.", parse_mode="MarkdownV2")
            return
        card_name  = row[0]
        remind_at  = (datetime.utcnow() + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        await db.execute(
            "INSERT INTO reminders (user_id, card_id, card_name, remind_at) VALUES (?,?,?,?)",
            (user_id, card_id, card_name, remind_at),
        )
        await db.commit()

    await update.message.reply_text(
        f"⏰ Reminder set\\!\n\n"
        f"🃏 *{esc(card_name)}* \\(ID: \\#{card_id}\\)\n"
        f"📅 Akan diingatkan dalam *{days} hari* \\({esc(remind_at[:10])}\\)",
        parse_mode="MarkdownV2",
    )


async def check_reminders(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Background job: kirim reminder yang sudah jatuh tempo."""
    now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, user_id, card_id, card_name, remind_at FROM reminders WHERE sent=0 AND remind_at <= ?",
            (now_str,),
        ) as cur:
            due = await cur.fetchall()

        for rem_id, user_id, card_id, card_name, remind_at in due:
            try:
                # Get current price
                async with db.execute(
                    "SELECT price_usd, price_idr FROM inventory WHERE id=? AND user_id=?",
                    (card_id, user_id),
                ) as cur2:
                    inv_row = await cur2.fetchone()
                price_str = ""
                if inv_row:
                    p_usd, p_idr = inv_row
                    price_str = f"\n💵 Harga tercatat: {esc_usd(p_usd or 0)} \\| Rp {(p_idr or 0):,.0f}"
                await context.bot.send_message(
                    chat_id=user_id,
                    text=(
                        f"⏰ *Reminder Harga Kartu\\!*\n\n"
                        f"🃏 *{esc(card_name)}* \\(ID: \\#{card_id}\\)\n"
                        f"📅 Set pada: {esc(remind_at[:10])}\n"
                        f"{price_str}\n\n"
                        f"_Cek harga terbaru dengan /refresh atau /cari_"
                    ),
                    parse_mode="MarkdownV2",
                )
                await db.execute("UPDATE reminders SET sent=1 WHERE id=?", (rem_id,))
            except Exception as e:
                logger.error(f"check_reminders: gagal kirim reminder {rem_id}: {e}")
        await db.commit()


# ── /listing <id> — generate teks listing marketplace ─────────────────────────
async def listing_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not context.args:
        await update.message.reply_text(
            "🛒 Format: `/listing \\<id\\>`\n_Contoh: `/listing 4`_\n\nID bisa dilihat di /inventory",
            parse_mode="MarkdownV2",
        )
        return
    try:
        card_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("⚠️ ID harus angka bre\\.", parse_mode="MarkdownV2")
        return

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT card_name, card_set, price_usd, price_idr, condition, psa_grade, tags FROM inventory WHERE id=? AND user_id=?",
            (card_id, user_id),
        ) as cur:
            row = await cur.fetchone()

    if not row:
        await update.message.reply_text("⚠️ Kartu tidak ditemukan bre\\.", parse_mode="MarkdownV2")
        return

    card_name, card_set, price_usd, price_idr, condition, psa_grade, tags = row
    price_idr_int = int(price_idr or 0)

    # Build condition description
    cond_desc_map = {
        "Mint":             "kondisi Mint \\(mulus sempurna, belum pernah dipakai\\)",
        "Near Mint":        "kondisi Near Mint \\(sangat bagus, disimpan dengan baik\\)",
        "Lightly Played":   "kondisi Lightly Played \\(sedikit bekas pakai, masih bagus\\)",
        "Moderately Played":"kondisi Moderately Played \\(ada bekas pakai tapi kartu masih jelas\\)",
        "Heavily Played":   "kondisi Heavily Played \\(bekas pakai jelas\\)",
        "Damaged":          "kondisi Damaged \\(ada kerusakan fisik\\)",
    }
    cond_line = cond_desc_map.get(condition or "Near Mint", f"kondisi {esc(condition or 'Near Mint')}")

    # Hashtags
    name_slug  = re.sub(r'[^a-zA-Z0-9]', '', card_name.lower())
    set_slug   = re.sub(r'[^a-zA-Z0-9]', '', (card_set or "").lower())
    grade_line = f"🏆 Grade: {esc(psa_grade)} \\(card sudah di\\-grading\\!\\)\n" if psa_grade else ""
    tag_line   = f"🔖 Label: {esc(tags)}\n" if tags else ""
    set_line   = f"📦 Set: {esc(card_set)}\n" if card_set else ""

    listing_text = (
        f"🃏 *\\[DIJUAL\\] {esc(card_name)}*\n"
        f"{set_line}"
        f"✨ Kartu {cond_line}\n"
        f"{grade_line}"
        f"{tag_line}"
        f"💵 Harga: *Rp {price_idr_int:,}*\n\n"
        f"Detail:\n"
        f"• Kartu original Pokémon TCG\n"
        f"• Sudah disimpan dalam sleeve pelindung\n"
        f"• Siap kirim ke seluruh Indonesia\n"
        f"• Bisa COD area sekitar \\(tanyakan dulu\\)\n\n"
        f"Minat? Chat WA/Telegram dulu ya\\!\n\n"
        f"\\#{esc(name_slug)} \\#pokemontcg \\#jualpokemon \\#pokemon"
        + (f" \\#{esc(set_slug)}" if set_slug else "")
        + (f" \\#{esc(psa_grade.lower().replace(' ',''))}" if psa_grade else "")
    )

    await update.message.reply_text(
        f"🛒 *Template Listing — {esc(card_name)}*\n\n"
        f"_Copy teks di bawah ini untuk Tokopedia / Shopee / WA:_\n\n"
        + listing_text,
        parse_mode="MarkdownV2",
    )


# ── /exportpdf — export katalog PDF ──────────────────────────────────────────
def _build_pdf_bytes(cards: list, username: str) -> bytes:
    """Buat PDF katalog inventory. Dipanggil via asyncio.to_thread."""
    try:
        from fpdf import FPDF
    except ImportError:
        raise ImportError("fpdf2")

    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()

    # Title
    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 10, f"Pokemon TCG Inventory — {username}", ln=True, align="C")
    pdf.set_font("Helvetica", "", 9)
    pdf.cell(0, 6, f"Generated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}", ln=True, align="C")
    pdf.ln(4)

    # Table header
    col_widths = [8, 60, 40, 22, 22, 20, 18]
    headers    = ["No", "Nama Kartu", "Set", "USD", "IDR", "Kondisi", "PSA"]
    pdf.set_font("Helvetica", "B", 9)
    pdf.set_fill_color(220, 220, 220)
    for w, h in zip(col_widths, headers):
        pdf.cell(w, 7, h, border=1, fill=True, align="C")
    pdf.ln()

    # Rows
    pdf.set_font("Helvetica", "", 8)
    fill = False
    total_idr = 0.0
    for idx, (card_id, name, card_set, p_usd, p_idr, cond, grade, tags) in enumerate(cards, 1):
        total_idr += p_idr or 0
        pdf.set_fill_color(245, 245, 245) if fill else pdf.set_fill_color(255, 255, 255)
        row_data = [
            str(idx),
            name[:30],
            (card_set or "")[:20],
            f"${p_usd:.2f}" if p_usd else "-",
            f"Rp{int(p_idr):,}" if p_idr else "-",
            (cond or "NM")[:12],
            (grade if grade else "-")[:10],
        ]
        for w, d in zip(col_widths, row_data):
            pdf.cell(w, 6, d, border=1, fill=True)
        pdf.ln()
        fill = not fill

    # Total
    pdf.ln(3)
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(0, 8, f"Total Koleksi: {len(cards)} kartu  |  Total Nilai: Rp {total_idr:,.0f}", ln=True)

    return bytes(pdf.output())


async def exportpdf_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id  = update.effective_user.id
    username = update.effective_user.first_name or "User"

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, card_name, card_set, price_usd, price_idr, condition, psa_grade, tags "
            "FROM inventory WHERE user_id=? ORDER BY id",
            (user_id,),
        ) as cur:
            cards = await cur.fetchall()

    if not cards:
        await update.message.reply_text("📂 Inventory kosong bre\\.", parse_mode="MarkdownV2")
        return

    status_msg = await update.message.reply_text(
        f"📄 Membuat PDF katalog *{len(cards)} kartu*\\.\\.\\.",
        parse_mode="MarkdownV2",
    )
    try:
        pdf_bytes = await asyncio.to_thread(_build_pdf_bytes, cards, username)
        filename  = f"inventory_{user_id}_{datetime.utcnow().strftime('%Y%m%d')}.pdf"
        await update.message.reply_document(
            document=io.BytesIO(pdf_bytes),
            filename=filename,
            caption=f"📄 Katalog Pokemon TCG — {len(cards)} kartu",
        )
        await status_msg.delete()
    except ImportError:
        await status_msg.edit_text(
            "⚠️ Library *fpdf2* belum terinstall\\.\n\n"
            "Install dulu dengan:\n`pip install fpdf2`\n\nLalu restart bot\\.",
            parse_mode="MarkdownV2",
        )
    except Exception as e:
        logger.error(f"exportpdf_cmd error: {e}", exc_info=True)
        await status_msg.edit_text(
            f"⚠️ Gagal buat PDF bre\\: {esc(str(e)[:100])}",
            parse_mode="MarkdownV2",
        )


# ── /comparebulan — bandingkan nilai portfolio bulan ini vs bulan lalu ────────
async def comparebulan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    now     = datetime.utcnow()
    # First day of current month
    first_this = now.replace(day=1, hour=0, minute=0, second=0)
    # First day of last month
    if first_this.month == 1:
        first_last = first_this.replace(year=first_this.year - 1, month=12)
    else:
        first_last = first_this.replace(month=first_this.month - 1)

    async with aiosqlite.connect(DB_PATH) as db:
        # Portfolio snapshot bulan ini (earliest)
        async with db.execute(
            """SELECT total_usd, total_idr, snapped_at FROM portfolio_snapshots
               WHERE user_id=? AND snapped_at >= ?
               ORDER BY snapped_at ASC LIMIT 1""",
            (user_id, first_this.strftime("%Y-%m-%d")),
        ) as cur:
            this_start = await cur.fetchone()

        # Latest snapshot
        async with db.execute(
            """SELECT total_usd, total_idr, snapped_at FROM portfolio_snapshots
               WHERE user_id=?
               ORDER BY snapped_at DESC LIMIT 1""",
            (user_id,),
        ) as cur:
            latest = await cur.fetchone()

        # Snapshot bulan lalu (earliest of last month)
        async with db.execute(
            """SELECT total_usd, total_idr, snapped_at FROM portfolio_snapshots
               WHERE user_id=? AND snapped_at >= ? AND snapped_at < ?
               ORDER BY snapped_at ASC LIMIT 1""",
            (user_id, first_last.strftime("%Y-%m-%d"), first_this.strftime("%Y-%m-%d")),
        ) as cur:
            last_month = await cur.fetchone()

        # Current inventory total
        async with db.execute(
            "SELECT SUM(price_usd), SUM(price_idr), COUNT(*) FROM inventory WHERE user_id=?",
            (user_id,),
        ) as cur:
            inv_row = await cur.fetchone()

    cur_usd   = inv_row[0] or 0 if inv_row else 0
    cur_idr   = inv_row[1] or 0 if inv_row else 0
    cur_count = inv_row[2] or 0 if inv_row else 0

    lines = [f"📅 *Perbandingan Portfolio Bulanan*\n"]

    # Current
    lines.append(f"📊 *Saat Ini*")
    lines.append(f"├ Kartu: *{cur_count}*")
    lines.append(f"└ Nilai: *{esc_usd(cur_usd)}* \\| Rp {cur_idr:,.0f}\n")

    if last_month:
        lm_usd  = last_month[0] or 0
        lm_idr  = last_month[1] or 0
        lm_date = last_month[2][:10]
        diff_usd = cur_usd - lm_usd
        diff_idr = cur_idr - lm_idr
        pct      = (diff_usd / lm_usd * 100) if lm_usd > 0 else 0
        arrow    = "📈" if diff_usd >= 0 else "📉"
        sign     = "+" if diff_usd >= 0 else ""
        lines.append(f"🗓 *Bulan Lalu* \\(sejak {esc(lm_date)}\\)")
        lines.append(f"└ Nilai: {esc_usd(lm_usd)} \\| Rp {lm_idr:,.0f}\n")
        lines.append(
            f"{arrow} *Perubahan: {esc(f'{sign}{diff_usd:.2f}')} USD "
            f"\\({esc(f'{sign}{pct:.1f}%')}\\)*\n"
            f"   Rp {diff_idr:+,.0f}"
        )
    else:
        lines.append(f"⚠️ Belum ada snapshot bulan lalu\\.\n_Jalankan bot setiap hari supaya data terekam otomatis\\._")

    if this_start:
        ts_usd  = this_start[0] or 0
        ts_date = this_start[2][:10]
        diff_month = cur_usd - ts_usd
        sign_m     = "+" if diff_month >= 0 else ""
        pct_m      = (diff_month / ts_usd * 100) if ts_usd > 0 else 0
        lines.append(f"\n📆 *Awal Bulan Ini* \\({esc(ts_date)}\\): {esc_usd(ts_usd)}")
        lines.append(f"   Δ bulan ini: *{esc(f'{sign_m}{diff_month:.2f}')} USD \\({esc(f'{sign_m}{pct_m:.1f}%')}\\)*")

    lines.append(f"\n_Data dari snapshot harian otomatis\\. Snapshot terakhir: {esc((latest[2] if latest else 'belum ada')[:16])}_")
    await update.message.reply_text("\n".join(lines), parse_mode="MarkdownV2")


# ═══════════════════════════════════════════════════════════════════════════════
# v10 — UX, MANAJEMEN, ANALITIK LOKAL
# ═══════════════════════════════════════════════════════════════════════════════

# ── /menu — papan tombol interaktif ──────────────────────────────────────────
async def menu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📦 Inventory",     callback_data="menu:/inventory"),
            InlineKeyboardButton("📊 Stats",         callback_data="menu:/stats"),
        ],
        [
            InlineKeyboardButton("💰 ROI",           callback_data="menu:/roi"),
            InlineKeyboardButton("🏆 Top 10",        callback_data="menu:/top10"),
        ],
        [
            InlineKeyboardButton("📋 Wishlist",      callback_data="menu:/wishlist"),
            InlineKeyboardButton("📈 Portfolio",     callback_data="menu:/portfolio"),
        ],
        [
            InlineKeyboardButton("🏷️ For Sale",     callback_data="menu:/salejual"),
            InlineKeyboardButton("🗑️ Trash",        callback_data="menu:/trash"),
        ],
        [
            InlineKeyboardButton("🏆 Winner/Loser",  callback_data="menu:/winnerloser"),
            InlineKeyboardButton("📅 Rekap",         callback_data="menu:/rekap"),
        ],
        [
            InlineKeyboardButton("📂 Album Set",     callback_data="menu:/albumset"),
            InlineKeyboardButton("📚 Katalog",       callback_data="menu:/katalog"),
        ],
    ])
    await update.message.reply_text(
        "🎮 *PokéDex Price — Menu Utama*\n\n_Tap tombol untuk membuka fitur:_",
        parse_mode="MarkdownV2",
        reply_markup=keyboard,
    )


async def menu_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle tap tombol di /menu."""
    query = update.callback_query
    await query.answer()

    cmd     = query.data.split(":", 1)[1]
    uid     = query.from_user.id
    msg     = query.message

    if cmd == "/inventory":
        text, page, total_pages, total = await _build_inventory_page(uid, 0)
        if total == 0:
            await msg.reply_text("📂 Inventory kosong\\! Tambah dengan `/add \\[nama\\]`\\.", parse_mode="MarkdownV2")
        else:
            kb = _inventory_nav_keyboard(page, total_pages)
            await msg.reply_text(text, parse_mode="MarkdownV2", reply_markup=kb)
        return

    if cmd == "/salejual":
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT COUNT(*), SUM(ask_price_usd) FROM inventory WHERE user_id=? AND for_sale=1",
                (uid,)
            ) as cur:
                row = await cur.fetchone()
        cnt, total_ask = (row[0] or 0), (row[1] or 0.0)
        if cnt == 0:
            await msg.reply_text(
                "🏷️ Tidak ada kartu for sale\\.\n_Gunakan `/forsale <id> <harga>`_",
                parse_mode="MarkdownV2"
            )
        else:
            await msg.reply_text(
                f"🏷️ *{cnt} kartu for sale* \\| Total ask: {esc_usd(total_ask)}\n"
                f"_Ketik /salejual untuk daftar lengkap\\._",
                parse_mode="MarkdownV2"
            )
        return

    # Untuk command lain: kirim sebagai teks command Telegram (auto jadi link biru yg bisa di-tap)
    hints = {
        "/stats":       "📊 /stats",
        "/roi":         "💰 /roi",
        "/top10":       "🏆 /top10",
        "/wishlist":    "📋 /wishlist",
        "/portfolio":   "📈 /portfolio",
        "/trash":       "🗑️ /trash",
        "/winnerloser": "🏆 /winnerloser",
        "/rekap":       "📅 /rekap",
        "/albumset":    "📂 /albumset",
        "/katalog":     "📚 /katalog",
    }
    await msg.reply_text(hints.get(cmd, cmd))


# ── /note <id> [teks|hapus] — catatan per kartu ──────────────────────────────
async def note_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid  = update.effective_user.id
    args = context.args

    if not args:
        await update.message.reply_text(
            "Gunakan:\n"
            "`/note <id> <teks>` — simpan catatan\n"
            "`/note <id> hapus` — hapus catatan\n"
            "`/note <id>` — lihat catatan\n\n"
            "_Contoh: `/note 5 beli di JakCard Expo`_",
            parse_mode="MarkdownV2"
        )
        return

    try:
        card_id = int(args[0].lstrip('#'))
    except ValueError:
        await update.message.reply_text("⚠️ ID harus angka\\.", parse_mode="MarkdownV2")
        return

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT card_name, notes FROM inventory WHERE id=? AND user_id=?",
            (card_id, uid)
        ) as cur:
            row = await cur.fetchone()

    if not row:
        await update.message.reply_text(f"⚠️ Kartu \\#{card_id} tidak ditemukan\\.", parse_mode="MarkdownV2")
        return

    card_name, existing_note = row

    if len(args) == 1:
        # Tampilkan catatan saat ini
        if existing_note:
            await update.message.reply_text(
                f"📝 *Catatan {esc(card_name)}* `\\#{card_id}`:\n_{esc(existing_note)}_",
                parse_mode="MarkdownV2"
            )
        else:
            await update.message.reply_text(
                f"📝 *{esc(card_name)}* belum punya catatan\\.\n"
                f"_Gunakan `/note {card_id} <teks>` untuk menambah\\._",
                parse_mode="MarkdownV2"
            )
        return

    teks = " ".join(args[1:])

    async with aiosqlite.connect(DB_PATH) as db:
        if teks.lower() == "hapus":
            await db.execute(
                "UPDATE inventory SET notes=NULL WHERE id=? AND user_id=?", (card_id, uid)
            )
            await db.commit()
            await update.message.reply_text(
                f"🗑️ Catatan *{esc(card_name)}* `\\#{card_id}` dihapus\\.",
                parse_mode="MarkdownV2"
            )
        else:
            await db.execute(
                "UPDATE inventory SET notes=? WHERE id=? AND user_id=?", (teks, card_id, uid)
            )
            await db.commit()
            await update.message.reply_text(
                f"📝 Catatan *{esc(card_name)}* `\\#{card_id}` disimpan:\n_{esc(teks)}_",
                parse_mode="MarkdownV2"
            )


# ── /forsale <id> <harga> — tandai kartu mau dijual ──────────────────────────
async def forsale_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid  = update.effective_user.id
    args = context.args

    if len(args) < 2:
        await update.message.reply_text(
            "Gunakan: `/forsale <id> <harga_ask_USD>`\n"
            "_Contoh: `/forsale 5 25.00`_\n\n"
            "Tandai kartu sebagai mau dijual dengan harga ask\\.\n"
            "Lihat semua: /salejual \\| Batal: /unsale \\<id\\>",
            parse_mode="MarkdownV2"
        )
        return

    try:
        card_id   = int(args[0].lstrip('#'))
        ask_price = float(args[1].replace(",", ""))
        if ask_price < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("⚠️ Format: `/forsale <id> <harga>`", parse_mode="MarkdownV2")
        return

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT card_name, card_set, price_usd, buy_price_usd FROM inventory WHERE id=? AND user_id=?",
            (card_id, uid)
        ) as cur:
            row = await cur.fetchone()

    if not row:
        await update.message.reply_text(f"⚠️ Kartu \\#{card_id} tidak ditemukan\\.", parse_mode="MarkdownV2")
        return

    card_name, card_set, price_usd, buy_usd = row
    ask_idr = ask_price * EXCHANGE_RATE
    profit  = ask_price - (buy_usd or price_usd or 0)
    sign_p  = "\\+" if profit >= 0 else ""

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE inventory SET for_sale=1, ask_price_usd=? WHERE id=? AND user_id=?",
            (ask_price, card_id, uid)
        )
        await db.commit()

    set_str = f" \\({esc(card_set)}\\)" if card_set else ""
    await update.message.reply_text(
        f"🏷️ *{esc(card_name)}*{set_str} `\\#{card_id}` ditandai *FOR SALE*\\!\n\n"
        f"Ask: *{esc_usd(ask_price)}* \\(Rp {ask_idr:,.0f}\\)\n"
        f"Harga beli: {esc_usd(buy_usd or price_usd or 0)}\n"
        f"Estimasi profit: *{sign_p}{esc(f'{profit:.2f}')} USD*\n\n"
        f"_/salejual — lihat semua kartu dijual_\n"
        f"_/unsale {card_id} — batal jual_",
        parse_mode="MarkdownV2"
    )


# ── /unsale <id> — batalkan for-sale ─────────────────────────────────────────
async def unsale_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid  = update.effective_user.id
    args = context.args

    if not args:
        await update.message.reply_text("Gunakan: `/unsale <id>`", parse_mode="MarkdownV2")
        return

    try:
        card_id = int(args[0].lstrip('#'))
    except ValueError:
        await update.message.reply_text("⚠️ ID harus angka\\.", parse_mode="MarkdownV2")
        return

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT card_name FROM inventory WHERE id=? AND user_id=?", (card_id, uid)
        ) as cur:
            row = await cur.fetchone()
        if not row:
            await update.message.reply_text(f"⚠️ Kartu \\#{card_id} tidak ditemukan\\.", parse_mode="MarkdownV2")
            return
        await db.execute(
            "UPDATE inventory SET for_sale=0, ask_price_usd=0 WHERE id=? AND user_id=?",
            (card_id, uid)
        )
        await db.commit()

    await update.message.reply_text(
        f"✅ *{esc(row[0])}* `\\#{card_id}` dikeluarkan dari daftar jual\\.",
        parse_mode="MarkdownV2"
    )


# ── /salejual — list semua kartu for sale ────────────────────────────────────
async def salejual_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT id, card_name, card_set, buy_price_usd, price_usd,
                   ask_price_usd, condition, psa_grade, notes
            FROM inventory
            WHERE user_id=? AND for_sale=1
            ORDER BY ask_price_usd DESC
        """, (uid,)) as cur:
            rows = await cur.fetchall()

    if not rows:
        await update.message.reply_text(
            "🏷️ Tidak ada kartu yang sedang dijual\\.\n"
            "_Gunakan `/forsale <id> <harga>` untuk menandai kartu\\._",
            parse_mode="MarkdownV2"
        )
        return

    total_ask = sum(r[5] or 0 for r in rows)
    lines     = [f"🏷️ *KARTU FOR SALE* \\({len(rows)} kartu\\)\n"]

    for inv_id, name, card_set, buy_usd, price_usd, ask_p, cond, grade, notes in rows:
        set_str   = f" \\({esc(card_set)}\\)" if card_set else ""
        grade_str = f" \\| 🏆 {esc(str(grade))}" if grade else ""
        base_usd  = buy_usd or price_usd or 0
        profit    = (ask_p or 0) - base_usd
        sign_p    = "\\+" if profit >= 0 else ""
        note_str  = f"\n  📝 _{esc(notes)}_" if notes else ""
        lines.append(
            f"• *{esc(name)}*{set_str} `\\#{inv_id}`\n"
            f"  {esc(cond)}{grade_str}\n"
            f"  Ask: *{esc_usd(ask_p or 0)}* \\| Beli: {esc_usd(base_usd)}\n"
            f"  💰 Profit: *{sign_p}{esc(f'{profit:.2f}')} USD*"
            f"{note_str}"
        )

    lines.append(f"\n💵 *Total Ask: {esc_usd(total_ask)}*")
    lines.append(f"_/listing \\[id\\] untuk teks marketplace_")
    await update.message.reply_text("\n".join(lines), parse_mode="MarkdownV2")


# ── /albumset — inventory dikelompokkan per set ───────────────────────────────
async def albumset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT COALESCE(card_set, '(Tanpa Set)') as s,
                   COUNT(*) as cnt,
                   SUM(price_usd) as total_usd,
                   SUM(price_idr) as total_idr
            FROM inventory
            WHERE user_id=?
            GROUP BY s
            ORDER BY total_usd DESC
        """, (uid,)) as cur:
            rows = await cur.fetchall()

    if not rows:
        await update.message.reply_text("📂 Inventory kosong\\.", parse_mode="MarkdownV2")
        return

    grand_usd   = sum(r[2] or 0 for r in rows)
    grand_idr   = sum(r[3] or 0 for r in rows)
    grand_count = sum(r[1] for r in rows)

    lines = [f"📂 *Album per Set* \\({grand_count} kartu total\\)\n"]

    for card_set, cnt, p_usd, p_idr in rows:
        pct = ((p_usd or 0) / grand_usd * 100) if grand_usd > 0 else 0
        filled = int(pct / 10)
        bar    = "█" * filled + "░" * (10 - filled)
        lines.append(
            f"📦 *{esc(card_set)}*  `{bar}` {esc(f'{pct:.0f}')}%\n"
            f"   {cnt} kartu \\| {esc_usd(p_usd or 0)} \\| Rp {(p_idr or 0):,.0f}"
        )

    lines.append(f"\n💰 *Grand Total: {esc_usd(grand_usd)} \\| Rp {grand_idr:,.0f}*")
    await update.message.reply_text("\n".join(lines), parse_mode="MarkdownV2")


# ── /rekap — rekap bulanan ───────────────────────────────────────────────────
async def rekap_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid      = update.effective_user.id
    now      = datetime.now()
    month_s  = now.strftime("%Y-%m")
    month_nm = now.strftime("%B %Y")

    async with aiosqlite.connect(DB_PATH) as db:
        # Penjualan bulan ini
        async with db.execute("""
            SELECT COUNT(*), SUM(sell_price_usd), SUM(profit_usd)
            FROM trade_log
            WHERE user_id=? AND sold_at LIKE ?
        """, (uid, f"{month_s}%")) as cur:
            trade = await cur.fetchone()

        # Portofolio saat ini
        async with db.execute(
            "SELECT SUM(price_usd), SUM(price_idr), COUNT(*) FROM inventory WHERE user_id=?", (uid,)
        ) as cur:
            porto = await cur.fetchone()

        # Snapshot awal bulan (untuk delta)
        async with db.execute("""
            SELECT total_usd FROM portfolio_snapshots
            WHERE user_id=? AND snapped_at LIKE ?
            ORDER BY snapped_at ASC LIMIT 1
        """, (uid, f"{month_s}%")) as cur:
            snap = await cur.fetchone()

        # Reminders pending bulan ini
        async with db.execute(
            "SELECT COUNT(*) FROM reminders WHERE user_id=? AND remind_at LIKE ? AND sent=0",
            (uid, f"{month_s}%")
        ) as cur:
            rem_row = await cur.fetchone()

        # For-sale saat ini
        async with db.execute(
            "SELECT COUNT(*), SUM(ask_price_usd) FROM inventory WHERE user_id=? AND for_sale=1", (uid,)
        ) as cur:
            sale_row = await cur.fetchone()

        # Wishlist
        async with db.execute("SELECT COUNT(*) FROM wishlist WHERE user_id=?", (uid,)) as cur:
            wish_row = await cur.fetchone()

    sold_cnt   = trade[0] or 0
    sold_usd   = trade[1] or 0.0
    profit_usd = trade[2] or 0.0
    cur_usd    = porto[0] or 0.0
    cur_idr    = porto[1] or 0.0
    cur_count  = porto[2] or 0

    lines = [f"📅 *Rekap Bulan {esc(month_nm)}*\n"]

    # Portofolio
    lines.append("💼 *Portofolio Sekarang*")
    lines.append(f"   {cur_count} kartu \\| {esc_usd(cur_usd)} \\| Rp {cur_idr:,.0f}")
    if snap and snap[0]:
        delta   = cur_usd - snap[0]
        sign    = "\\+" if delta >= 0 else ""
        emoji_d = "📈" if delta >= 0 else "📉"
        lines.append(f"   {emoji_d} Δ bulan ini: *{sign}{esc(f'{delta:.2f}')} USD*")

    # Penjualan
    lines.append("\n💸 *Penjualan Bulan Ini*")
    if sold_cnt > 0:
        sign_p = "\\+" if profit_usd >= 0 else ""
        lines.append(f"   {sold_cnt} kartu dijual \\| Hasil: {esc_usd(sold_usd)}")
        lines.append(f"   Profit: *{sign_p}{esc(f'{profit_usd:.2f}')} USD*")
    else:
        lines.append("   \\- Belum ada penjualan bulan ini")

    # For sale & wishlist & reminders
    sale_cnt  = sale_row[0] or 0
    sale_ask  = sale_row[1] or 0.0
    wish_cnt  = wish_row[0] if wish_row else 0
    rem_cnt   = rem_row[0]  if rem_row  else 0

    lines.append("\n📋 *Lainnya*")
    if sale_cnt:
        lines.append(f"   🏷️ For sale: {sale_cnt} kartu \\(ask {esc_usd(sale_ask)}\\)")
    lines.append(f"   📌 Wishlist: {wish_cnt} kartu")
    if rem_cnt:
        lines.append(f"   ⏰ Reminder pending: {rem_cnt}")

    lines.append("\n_/stats \\| /roi \\| /winnerloser \\| /riwayatjual_")
    await update.message.reply_text("\n".join(lines), parse_mode="MarkdownV2")


# ── /duplikatjual — rekomendasi jual duplikat terbaik ─────────────────────────
async def duplikatjual_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT LOWER(card_name) as lname,
                   card_name,
                   COUNT(*) as cnt,
                   MIN(price_usd) as min_p,
                   MAX(price_usd) as max_p
            FROM inventory
            WHERE user_id=?
            GROUP BY lname
            HAVING cnt >= 2
            ORDER BY max_p DESC
        """, (uid,)) as cur:
            dupes = await cur.fetchall()

    if not dupes:
        await update.message.reply_text(
            "✅ Tidak ada duplikat di inventory kamu\\. Semua kartu unik\\!",
            parse_mode="MarkdownV2"
        )
        return

    # Untuk setiap duplikat: cari detail (ID + kondisi) untuk saran jual
    COND_RANK = {
        "Mint": 1, "Near Mint": 2, "Lightly Played": 3,
        "Moderately Played": 4, "Heavily Played": 5, "Damaged": 6,
    }

    lines = ["🔄 *REKOMENDASI JUAL DUPLIKAT*\n", "_Simpan kondisi terbaik, jual yang lebih rendah:_\n"]

    async with aiosqlite.connect(DB_PATH) as db:
        for lname, name, cnt, min_p, max_p in dupes:
            async with db.execute("""
                SELECT id, condition, price_usd FROM inventory
                WHERE user_id=? AND LOWER(card_name)=?
                ORDER BY price_usd DESC
            """, (uid, lname)) as cur:
                copies = await cur.fetchall()

            # Urutkan: yang paling jelek kondisinya → saran jual pertama
            copies_sorted = sorted(
                copies,
                key=lambda r: COND_RANK.get(r[1] or "Damaged", 6),
                reverse=True
            )
            sell_id, sell_cond, sell_price = copies_sorted[0]
            keep_id, keep_cond, keep_price = copies_sorted[-1]

            lines.append(
                f"• *{esc(name)}* \\({cnt}x\\)\n"
                f"  💡 Jual `\\#{sell_id}` \\({esc(sell_cond or '?')}\\) {esc_usd(sell_price or 0)}\n"
                f"  ✅ Simpan `\\#{keep_id}` \\({esc(keep_cond or '?')}\\) {esc_usd(keep_price or 0)}"
            )

    lines.append(f"\n_/jual \\[id\\] untuk menjual \\| /forsale \\[id\\] \\[harga\\] untuk listing_")
    await update.message.reply_text("\n".join(lines), parse_mode="MarkdownV2")


# ── /hitung <harga> [kondisi] — kalkulator beli cepat ────────────────────────
async def hitung_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args

    if not args:
        await update.message.reply_text(
            "Gunakan: `/hitung <harga> [kondisi]`\n\n"
            "Harga dalam USD \\(misal `15.50`\\) atau IDR \\(misal `250000`\\)\\.\n"
            "Kondisi opsional: NM, LP, MP, HP, Mint, Damaged\n\n"
            "_Contoh:_\n"
            "`/hitung 15.00 NM`\n"
            "`/hitung 250000`",
            parse_mode="MarkdownV2"
        )
        return

    try:
        price_raw = float(args[0].replace(",", ""))
        if price_raw < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("⚠️ Harga tidak valid\\.", parse_mode="MarkdownV2")
        return

    # Deteksi mata uang: nilai >= 1000 dianggap IDR
    if price_raw >= 1000:
        price_usd = price_raw / EXCHANGE_RATE
        price_idr = price_raw
    else:
        price_usd = price_raw
        price_idr = price_raw * EXCHANGE_RATE

    # Match kondisi dari argumen
    cond_input = " ".join(args[1:]).strip().lower() if len(args) > 1 else ""
    cond_aliases = {
        "nm": "Near Mint", "nearmint": "Near Mint",
        "m": "Mint", "mint": "Mint",
        "lp": "Lightly Played", "lightlyplayed": "Lightly Played",
        "mp": "Moderately Played", "moderatelyplayed": "Moderately Played",
        "hp": "Heavily Played", "heavilyplayed": "Heavily Played",
        "d": "Damaged", "damaged": "Damaged",
    }
    cond_match = "Near Mint"
    if cond_input:
        key = cond_input.replace(" ", "").lower()
        cond_match = cond_aliases.get(key) or next(
            (c for c in CARD_CONDITIONS if cond_input in c.lower()), "Near Mint"
        )

    multiplier = CONDITION_MULTIPLIERS.get(cond_match, 1.0)
    adj_usd    = price_usd * multiplier   # nilai riil dengan diskon kondisi
    adj_idr    = price_idr * multiplier

    # Harga NM-equivalent (seolah kondisi NM)
    nm_equiv = price_usd / multiplier if multiplier > 0 else price_usd

    # Skenario resale
    sell_low = nm_equiv * 0.70
    sell_mid = nm_equiv * 1.00
    sell_hi  = nm_equiv * 1.30

    roi = lambda sell: (sell - price_usd) / price_usd * 100 if price_usd > 0 else 0

    # Verdict
    if price_usd <= nm_equiv * 0.85:
        verdict = "✅ *BELI* — harga di bawah nilai NM, margin bagus"
    elif price_usd <= nm_equiv * 1.0:
        verdict = "🟡 *PERTIMBANGKAN* — harga wajar, margin tipis"
    else:
        verdict = "⚠️ *MAHAL* — harga di atas nilai kondisi, resiko rugi"

    disc_pct = (1 - multiplier) * 100

    lines = [
        f"🧮 *KALKULATOR BELI KARTU*\n",
        f"💵 Harga beli: *{esc_usd(price_usd)}* \\(Rp {price_idr:,.0f}\\)",
        f"📦 Kondisi: _{esc(cond_match)}_"
        + (f" \\(diskon {esc(f'{disc_pct:.0f}')}%\\)" if disc_pct > 0 else ""),
        f"📊 Nilai efektif: {esc_usd(adj_usd)} \\(Rp {adj_idr:,.0f}\\)",
        f"📈 Setara NM: \\~{esc_usd(nm_equiv)}",
        f"\n🔮 *Skenario Resale*",
        f"   Konservatif \\(\\-30%\\): {esc_usd(sell_low)} → ROI *{esc(f'{roi(sell_low):+.1f}')}%*",
        f"   Fair value: {esc_usd(sell_mid)} → ROI *{esc(f'{roi(sell_mid):+.1f}')}%*",
        f"   Optimis \\(\\+30%\\): {esc_usd(sell_hi)} → ROI *{esc(f'{roi(sell_hi):+.1f}')}%*",
        f"\n{verdict}",
        f"\n_/add untuk tambah ke inventory_",
    ]
    await update.message.reply_text("\n".join(lines), parse_mode="MarkdownV2")


# ═══════════════════════════════════════════════════════════════════════════════
# v9 — FITUR LOKAL (tanpa API harga)
# ═══════════════════════════════════════════════════════════════════════════════

# ── 1. /winnerloser — kartu yang paling naik & turun dari price_history ───────
async def winnerloser_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT card_name, price_usd, recorded_at
            FROM price_history
            WHERE user_id=?
            ORDER BY card_name, recorded_at ASC
        """, (uid,)) as cur:
            rows = await cur.fetchall()

    if not rows:
        await update.message.reply_text(
            "📊 Belum ada data history harga\\.\n"
            "_Tambah harga via /editprice atau /updateharga terlebih dulu\\._",
            parse_mode="MarkdownV2"
        )
        return

    # Kumpulkan harga pertama & terakhir per nama kartu
    card_data: dict = {}
    for name, price, rec_at in rows:
        if name not in card_data:
            card_data[name] = {"first": price, "last": price}
        else:
            card_data[name]["last"] = price

    # Hitung delta
    deltas = []
    for name, d in card_data.items():
        delta = d["last"] - d["first"]
        pct   = (delta / d["first"] * 100) if d["first"] > 0 else 0.0
        deltas.append((name, d["first"], d["last"], delta, pct))

    if len(deltas) < 2:
        await update.message.reply_text(
            "📊 Minimal 2 kartu dengan data history untuk menampilkan winner/loser\\.",
            parse_mode="MarkdownV2"
        )
        return

    winners = sorted(deltas, key=lambda x: x[3], reverse=True)[:3]
    losers  = sorted(deltas, key=lambda x: x[3])[:3]

    lines = ["🏆 *WINNER \\& LOSER KARTU*\n"]

    lines.append("📈 *Top Naik \\(Winners\\)*")
    for i, (name, fp, lp, delta, pct) in enumerate(winners, 1):
        sign = "\\+" if delta >= 0 else ""
        lines.append(
            f"{i}\\. *{esc(name)}*\n"
            f"   {esc_usd(fp)} → {esc_usd(lp)}"
            f"  \\(*{sign}{esc(f'{delta:.2f}')} USD / {sign}{esc(f'{pct:.1f}')}%*\\)"
        )

    lines.append("\n📉 *Top Turun \\(Losers\\)*")
    for i, (name, fp, lp, delta, pct) in enumerate(losers, 1):
        sign = "\\+" if delta >= 0 else ""
        lines.append(
            f"{i}\\. *{esc(name)}*\n"
            f"   {esc_usd(fp)} → {esc_usd(lp)}"
            f"  \\(*{sign}{esc(f'{delta:.2f}')} USD / {sign}{esc(f'{pct:.1f}')}%*\\)"
        )

    lines.append(f"\n_Data dari {len(deltas)} kartu di price history\\._")
    await update.message.reply_text("\n".join(lines), parse_mode="MarkdownV2")


# ── 2. /setcomplete <set> — progress checklist kelengkapan set ────────────────
async def setcomplete_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid  = update.effective_user.id
    args = context.args
    if not args:
        await update.message.reply_text(
            "Gunakan: `/setcomplete <nama set>`\n"
            "Contoh: `/setcomplete Base Set`\n\n"
            "_Data kartu diambil dari cache lokal \\(/synccards untuk update\\)\\._",
            parse_mode="MarkdownV2"
        )
        return

    set_name = " ".join(args)

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT name FROM card_cache
            WHERE LOWER(card_set) LIKE LOWER(?)
            ORDER BY name
        """, (f"%{set_name}%",)) as cur:
            cache_rows = await cur.fetchall()

        async with db.execute("""
            SELECT LOWER(card_name) FROM inventory
            WHERE user_id=? AND LOWER(card_set) LIKE LOWER(?)
        """, (uid, f"%{set_name}%")) as cur:
            inv_rows = await cur.fetchall()

    if not cache_rows:
        await update.message.reply_text(
            f"⚠️ Set `{esc(set_name)}` tidak ditemukan di database lokal\\.\n"
            "_Coba `/synccards` untuk mengisi data kartu dari API\\._",
            parse_mode="MarkdownV2"
        )
        return

    cache_names  = [r[0] for r in cache_rows]
    owned_lower  = {r[0] for r in inv_rows}
    total        = len(cache_names)
    owned_count  = sum(1 for n in cache_names if n.lower() in owned_lower)
    pct          = (owned_count / total * 100) if total > 0 else 0.0
    missing      = [n for n in cache_names if n.lower() not in owned_lower]

    filled = int(pct / 10)
    bar    = "█" * filled + "░" * (10 - filled)

    lines = [
        f"📦 *Set: {esc(set_name)}*\n",
        f"✅ Punya: *{owned_count}/{total}* kartu \\({esc(f'{pct:.1f}')}%\\)",
        f"`{bar}`",
    ]

    if not missing:
        lines.append("\n🎉 *SET KOMPLIT\\!* Kamu punya semua kartu di set ini\\! 🎊")
    else:
        lines.append(f"\n❌ *Belum punya \\({len(missing)} kartu\\):*")
        for m in missing[:25]:
            lines.append(f"  • {esc(m)}")
        if len(missing) > 25:
            lines.append(f"  _\\.\\.\\. dan {len(missing) - 25} kartu lagi_")

    await update.message.reply_text("\n".join(lines), parse_mode="MarkdownV2")


# ── 3. /graderekomendasi — kartu yang layak di-PSA ───────────────────────────
async def graderekomendasi_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid       = update.effective_user.id
    THRESHOLD = 10.0  # USD minimum untuk layak di-grade

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT id, card_name, card_set, condition, price_usd, price_idr, psa_grade
            FROM inventory
            WHERE user_id=?
              AND psa_grade IS NULL
              AND (LOWER(condition) LIKE '%near mint%' OR LOWER(condition) LIKE '%mint%')
              AND price_usd >= ?
            ORDER BY price_usd DESC
        """, (uid, THRESHOLD)) as cur:
            rows = await cur.fetchall()

    if not rows:
        await update.message.reply_text(
            f"🔍 Tidak ada kartu yang memenuhi kriteria rekomendasi grading\\.\n\n"
            f"_Kriteria:_\n"
            f"  • Kondisi Near Mint atau Mint\n"
            f"  • Nilai ≥ {esc_usd(THRESHOLD)}\n"
            f"  • Belum memiliki PSA grade",
            parse_mode="MarkdownV2"
        )
        return

    lines = [
        "🎓 *REKOMENDASI PSA GRADING*\n",
        f"_Kartu kondisi NM/Mint, nilai ≥ {esc_usd(THRESHOLD)}, belum di\\-grade:_\n",
    ]

    for inv_id, name, card_set, cond, price_usd, price_idr, _ in rows:
        set_str      = f" \\({esc(card_set)}\\)" if card_set else ""
        potential    = price_usd * 3.0
        lines.append(
            f"• *{esc(name)}*{set_str} `\\#{inv_id}`\n"
            f"  {esc(cond)} \\| {esc_usd(price_usd)} \\| Rp {price_idr:,.0f}\n"
            f"  💡 _Potensi PSA 10: \\~{esc_usd(potential)}_"
        )

    lines.append(
        f"\n_Biaya grading \\~\\$20\\-50/kartu via PSA\\. "
        f"Kartu PSA 10 bisa bernilai 2\\-5× lipat\\._"
    )
    await update.message.reply_text("\n".join(lines), parse_mode="MarkdownV2")


# ── 4. /tradein <id1> <id2> — simulasi tukar kartu ───────────────────────────
async def tradein_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid  = update.effective_user.id
    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "Gunakan: `/tradein <id1> <id2>`\n"
            "Simulasi tukar kartu \\#id1 dengan \\#id2\\.\n"
            "_Gunakan /inventory untuk melihat ID kartu\\._",
            parse_mode="MarkdownV2"
        )
        return

    try:
        id1 = int(args[0].lstrip('#'))
        id2 = int(args[1].lstrip('#'))
    except ValueError:
        await update.message.reply_text("⚠️ ID harus berupa angka\\.", parse_mode="MarkdownV2")
        return

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, card_name, card_set, price_usd, price_idr, condition, psa_grade "
            "FROM inventory WHERE id=? AND user_id=?",
            (id1, uid)
        ) as cur:
            card1 = await cur.fetchone()
        async with db.execute(
            "SELECT id, card_name, card_set, price_usd, price_idr, condition, psa_grade "
            "FROM inventory WHERE id=? AND user_id=?",
            (id2, uid)
        ) as cur:
            card2 = await cur.fetchone()

    if not card1:
        await update.message.reply_text(f"⚠️ Kartu \\#{esc(str(id1))} tidak ditemukan\\.", parse_mode="MarkdownV2")
        return
    if not card2:
        await update.message.reply_text(f"⚠️ Kartu \\#{esc(str(id2))} tidak ditemukan\\.", parse_mode="MarkdownV2")
        return

    _, n1, s1, p1, pi1, c1, g1 = card1
    _, n2, s2, p2, pi2, c2, g2 = card2

    delta_usd = p1 - p2
    delta_idr = pi1 - pi2

    if delta_usd > 0.005:
        verdict_emoji = "✅"
        verdict_text  = (
            f"Kamu *untung* {esc_usd(abs(delta_usd))} "
            f"\\(Rp {abs(delta_idr):,.0f}\\) dengan menukar \\#{id1} → \\#{id2}"
        )
    elif delta_usd < -0.005:
        verdict_emoji = "⚠️"
        verdict_text  = (
            f"Kamu *rugi* {esc_usd(abs(delta_usd))} "
            f"\\(Rp {abs(delta_idr):,.0f}\\) dengan menukar \\#{id1} → \\#{id2}"
        )
    else:
        verdict_emoji = "⚖️"
        verdict_text  = "Nilai *setara*\\! Trade ini impas\\."

    s1_str  = f" \\({esc(s1)}\\)" if s1 else ""
    s2_str  = f" \\({esc(s2)}\\)" if s2 else ""
    g1_str  = f" \\| 🏆 {esc(str(g1))}" if g1 else ""
    g2_str  = f" \\| 🏆 {esc(str(g2))}" if g2 else ""

    msg = (
        f"🔄 *SIMULASI TRADE\\-IN*\n\n"
        f"*Kartu yang kamu berikan:*\n"
        f"  `\\#{id1}` *{esc(n1)}*{s1_str}\n"
        f"  {esc(c1)}{g1_str} \\| {esc_usd(p1)} \\| Rp {pi1:,.0f}\n\n"
        f"*Kartu yang kamu terima:*\n"
        f"  `\\#{id2}` *{esc(n2)}*{s2_str}\n"
        f"  {esc(c2)}{g2_str} \\| {esc_usd(p2)} \\| Rp {pi2:,.0f}\n\n"
        f"{verdict_emoji} {verdict_text}"
    )
    await update.message.reply_text(msg, parse_mode="MarkdownV2")


# ── 5. /daily on|off — toggle laporan harian ──────────────────────────────────
async def toggle_daily_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid     = update.effective_user.id
    chat_id = update.effective_chat.id
    args    = context.args

    if not args or args[0].lower() not in ("on", "off"):
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT daily_enabled FROM user_settings WHERE user_id=?", (uid,)
            ) as cur:
                row = await cur.fetchone()
        enabled = (row[0] if row else 0)
        status  = "✅ *ON*" if enabled else "❌ *OFF*"
        await update.message.reply_text(
            f"📅 *Laporan Harian*\n\nStatus saat ini: {status}\n\n"
            f"Aktifkan dengan `/daily on`\n"
            f"Nonaktifkan dengan `/daily off`\n\n"
            f"_Laporan dikirim setiap hari pukul 08:00\\._",
            parse_mode="MarkdownV2"
        )
        return

    turn_on = args[0].lower() == "on"
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO user_settings (user_id, daily_enabled, daily_chat_id)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                daily_enabled = excluded.daily_enabled,
                daily_chat_id = excluded.daily_chat_id
        """, (uid, 1 if turn_on else 0, chat_id if turn_on else None))
        await db.commit()

    if turn_on:
        await update.message.reply_text(
            "✅ *Laporan Harian* diaktifkan\\!\n"
            "_Kamu akan menerima ringkasan portofolio setiap pagi pukul 08:00\\._",
            parse_mode="MarkdownV2"
        )
    else:
        await update.message.reply_text(
            "❌ *Laporan Harian* dinonaktifkan\\.",
            parse_mode="MarkdownV2"
        )


async def send_daily_report(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Background job — kirim laporan harian ke semua user opt-in."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT user_id, daily_chat_id FROM user_settings "
            "WHERE daily_enabled=1 AND daily_chat_id IS NOT NULL"
        ) as cur:
            users = await cur.fetchall()

    for user_id, chat_id in users:
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                async with db.execute(
                    "SELECT SUM(price_usd), SUM(price_idr), COUNT(*) FROM inventory WHERE user_id=?",
                    (user_id,)
                ) as cur:
                    prow = await cur.fetchone()

                yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
                async with db.execute("""
                    SELECT total_usd FROM portfolio_snapshots
                    WHERE user_id=? AND snapped_at LIKE ?
                    ORDER BY snapped_at DESC LIMIT 1
                """, (user_id, f"{yesterday}%")) as cur:
                    yrow = await cur.fetchone()

                today_str = datetime.now().strftime("%Y-%m-%d")
                async with db.execute("""
                    SELECT card_name FROM reminders
                    WHERE user_id=? AND sent=0 AND remind_at LIKE ?
                    LIMIT 5
                """, (user_id, f"{today_str}%")) as cur:
                    rems = await cur.fetchall()

                async with db.execute(
                    "SELECT COUNT(*) FROM wishlist WHERE user_id=?", (user_id,)
                ) as cur:
                    wrow = await cur.fetchone()

            total_usd  = prow[0] or 0.0
            total_idr  = prow[1] or 0.0
            card_count = prow[2] or 0

            delta_str = ""
            if yrow and yrow[0]:
                delta = total_usd - yrow[0]
                sign  = "\\+" if delta >= 0 else ""
                emoji_d = "📈" if delta >= 0 else "📉"
                delta_str = f"\n   {emoji_d} Δ kemarin: *{sign}{esc(f'{delta:.2f}')} USD*"

            tanggal = datetime.now().strftime("%d %B %Y")
            lines = [
                f"🌅 *Laporan Harian PokéDex*",
                f"_{esc(tanggal)}_\n",
                f"💼 *Portofolio*",
                f"   {card_count} kartu \\| {esc_usd(total_usd)}{delta_str}",
                f"   Rp {total_idr:,.0f}",
            ]

            if rems:
                lines.append(f"\n⏰ *Reminder Hari Ini:*")
                for (cname,) in rems:
                    lines.append(f"   • {esc(cname)}")

            wish_count = wrow[0] if wrow else 0
            if wish_count:
                lines.append(f"\n📋 Wishlist: *{wish_count}* kartu")

            lines.append(f"\n_/inventory \\| /stats \\| /roi \\| /winnerloser_")

            await context.bot.send_message(
                chat_id=chat_id,
                text="\n".join(lines),
                parse_mode="MarkdownV2"
            )
            logger.info(f"[daily_report] Sent to user {user_id}")
        except Exception as e:
            logger.error(f"[daily_report] Error user {user_id}: {e}")


# ── 6. /qr <id> — QR code kartu ──────────────────────────────────────────────
async def qr_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid  = update.effective_user.id
    args = context.args
    if not args:
        await update.message.reply_text(
            "Gunakan: `/qr <id>`\nContoh: `/qr 5`\n"
            "_Membuat QR code berisi info kartu dengan ID tersebut\\._",
            parse_mode="MarkdownV2"
        )
        return

    try:
        card_id = int(args[0].lstrip('#'))
    except ValueError:
        await update.message.reply_text("⚠️ ID harus berupa angka\\.", parse_mode="MarkdownV2")
        return

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, card_name, card_set, price_usd, price_idr, condition, psa_grade, tags "
            "FROM inventory WHERE id=? AND user_id=?",
            (card_id, uid)
        ) as cur:
            row = await cur.fetchone()

    if not row:
        await update.message.reply_text(
            f"⚠️ Kartu \\#{esc(str(card_id))} tidak ditemukan\\.",
            parse_mode="MarkdownV2"
        )
        return

    inv_id, name, card_set, price_usd, price_idr, cond, grade, tags = row

    # Isi QR code
    parts = [f"PokeDex:{name}"]
    if card_set:  parts.append(f"Set:{card_set}")
    parts.append(f"Price:${price_usd:.2f}")
    parts.append(f"Cond:{cond}")
    if grade:     parts.append(f"PSA:{grade}")
    if tags:      parts.append(f"Tags:{tags}")
    qr_data = " | ".join(parts)

    def _make_qr_image() -> bytes:
        QR_SIZE = 300
        LABEL_H = 64
        img_w   = QR_SIZE
        img_h   = QR_SIZE

        try:
            import qrcode as qrmod
            qr = qrmod.QRCode(version=1, box_size=9, border=3,
                               error_correction=qrmod.constants.ERROR_CORRECT_M)
            qr.add_data(qr_data)
            qr.make(fit=True)
            qr_img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
            img_w  = qr_img.width
            img_h  = qr_img.height
        except ImportError:
            # Fallback tanpa library qrcode
            qr_img = Image.new("RGB", (QR_SIZE, QR_SIZE), "white")
            d = ImageDraw.Draw(qr_img)
            d.rectangle([20, 20, QR_SIZE - 20, QR_SIZE - 20], outline="black", width=3)
            d.text((QR_SIZE // 2, QR_SIZE // 2 - 10), "QR CODE", fill="black", anchor="mm")
            d.text((QR_SIZE // 2, QR_SIZE // 2 + 12), "(install qrcode)", fill="#888", anchor="mm")

        # Canvas: QR + label strip di bawah
        final = Image.new("RGB", (img_w, img_h + LABEL_H), "white")
        final.paste(qr_img, (0, 0))
        draw = ImageDraw.Draw(final)

        try:
            font_b  = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 15)
            font_sm = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 12)
        except Exception:
            font_b = font_sm = ImageFont.load_default()

        label_name  = name[:38] if len(name) > 38 else name
        label_price = f"${price_usd:.2f}  |  {cond}"
        cx = img_w // 2

        draw.text((cx, img_h + 8),  label_name,  font=font_b,  fill="#111", anchor="mt")
        draw.text((cx, img_h + 32), label_price, font=font_sm, fill="#555", anchor="mt")
        draw.text((cx, img_h + 50), f"ID #{inv_id}", font=font_sm, fill="#aaa", anchor="mt")

        buf = io.BytesIO()
        final.save(buf, format="PNG")
        buf.seek(0)
        return buf.read()

    await update.message.reply_text("🔄 Membuat QR code\\.\\.\\.", parse_mode="MarkdownV2")
    img_bytes = await asyncio.to_thread(_make_qr_image)

    buf = io.BytesIO(img_bytes)
    buf.name = f"qr_{card_id}.png"

    set_str = f" ({card_set})" if card_set else ""
    caption = (
        f"🔳 QR Code: {name}{set_str}\n"
        f"💵 ${price_usd:.2f}  |  {cond}"
        + (f"\n🏆 {grade}" if grade else "")
    )
    await update.message.reply_photo(photo=buf, caption=caption)


# ── 7. /katalog — HTML katalog koleksi ───────────────────────────────────────
async def katalog_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT id, card_name, card_set, price_usd, price_idr,
                   condition, psa_grade, tags
            FROM inventory
            WHERE user_id=?
            ORDER BY card_set, card_name
        """, (uid,)) as cur:
            rows = await cur.fetchall()

    if not rows:
        await update.message.reply_text(
            "📚 Inventory kamu kosong\\. Tambah kartu dulu dengan /add\\.",
            parse_mode="MarkdownV2"
        )
        return

    await update.message.reply_text("🔄 Membuat katalog HTML\\.\\.\\.", parse_mode="MarkdownV2")

    def _build_html() -> str:
        total_usd = sum(r[3] for r in rows)
        total_idr = sum(r[4] for r in rows)
        generated = datetime.now().strftime("%d %B %Y, %H:%M")

        cards_html_parts = []
        for inv_id, name, card_set, price_usd, price_idr, cond, grade, tags in rows:
            grade_badge = (
                f'<span class="badge psa">🏆 {grade}</span>' if grade else ""
            )
            set_html  = f'<div class="card-set">{card_set or "—"}</div>'
            tags_html = f'<div class="tags">🔖 {tags}</div>' if tags else ""
            cards_html_parts.append(f"""
        <div class="card-item">
          <div class="card-header">
            <span class="card-id">#{inv_id}</span>
            {grade_badge}
          </div>
          <div class="card-name">{name}</div>
          {set_html}
          <div class="card-cond">{cond}</div>
          <div class="card-price">
            <span class="usd">${price_usd:.2f}</span>
            <span class="idr">Rp {price_idr:,.0f}</span>
          </div>
          {tags_html}
        </div>""")

        cards_block = "\n".join(cards_html_parts)
        return f"""<!DOCTYPE html>
<html lang="id">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>PokéDex Price — Katalog Koleksi</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:'Segoe UI',Arial,sans-serif;background:#1a1a2e;color:#eee;padding:20px}}
h1{{text-align:center;color:#f5c518;font-size:2em;margin-bottom:4px}}
.subtitle{{text-align:center;color:#aaa;margin-bottom:20px;font-size:.9em}}
.summary{{display:flex;gap:16px;justify-content:center;margin-bottom:28px;flex-wrap:wrap}}
.summary-box{{background:#16213e;border-radius:12px;padding:14px 24px;text-align:center;border:1px solid #0f3460}}
.summary-box .val{{font-size:1.4em;font-weight:bold;color:#f5c518}}
.summary-box .lbl{{font-size:.8em;color:#aaa;margin-top:4px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:16px}}
.card-item{{background:#16213e;border-radius:12px;padding:16px;border:1px solid #0f3460;transition:transform .2s}}
.card-item:hover{{transform:translateY(-2px);border-color:#f5c518}}
.card-header{{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}}
.card-id{{color:#888;font-size:.8em}}
.badge.psa{{background:#e63946;color:#fff;border-radius:4px;padding:2px 6px;font-size:.75em;font-weight:bold}}
.card-name{{font-weight:bold;font-size:1.05em;color:#fff;margin-bottom:4px}}
.card-set{{color:#aaa;font-size:.82em;margin-bottom:6px}}
.card-cond{{color:#80c9ff;font-size:.82em;margin-bottom:8px}}
.card-price .usd{{color:#f5c518;font-weight:bold;font-size:1.1em;display:block}}
.card-price .idr{{color:#aaa;font-size:.8em}}
.tags{{color:#a8d8a8;font-size:.78em;margin-top:8px}}
footer{{text-align:center;color:#555;margin-top:32px;font-size:.8em}}
</style>
</head>
<body>
<h1>🃏 PokéDex Price</h1>
<p class="subtitle">Katalog Koleksi — {generated}</p>
<div class="summary">
  <div class="summary-box"><div class="val">{len(rows)}</div><div class="lbl">Total Kartu</div></div>
  <div class="summary-box"><div class="val">${total_usd:,.2f}</div><div class="lbl">Nilai USD</div></div>
  <div class="summary-box"><div class="val">Rp {total_idr:,.0f}</div><div class="lbl">Nilai IDR</div></div>
</div>
<div class="grid">
{cards_block}
</div>
<footer>Generated by PokéDex Price Bot · {generated}</footer>
</body>
</html>"""

    html_content = await asyncio.to_thread(_build_html)
    buf = io.BytesIO(html_content.encode("utf-8"))
    buf.name = "katalog_koleksi.html"

    await update.message.reply_document(
        document=buf,
        filename="katalog_koleksi.html",
        caption=(
            f"📚 Katalog koleksi kamu ({len(rows)} kartu)\n"
            f"Buka file .html di browser untuk tampilan penuh! 🌐"
        )
    )


# ═══════════════════════════════════════════════════════════════════════════════
# FITUR v11 — Auto-Refresh Terjadwal, Folder Koleksi, P&L Bulanan, Worth Grading
# ═══════════════════════════════════════════════════════════════════════════════

FOLDER_CHOICES = ["Pribadi", "Dijual", "Graded", "Trade", "Display"]
FOLDER_EMOJI   = {"Pribadi": "🏠", "Dijual": "🏷️", "Graded": "🏆", "Trade": "🔄", "Display": "🖼️"}


# ─── 1. AUTO-REFRESH HARGA TERJADWAL ─────────────────────────────────────────

async def autorefresh_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /autorefresh       — lihat status
    /autorefresh on    — aktifkan auto-refresh harian (10:00 WIB)
    /autorefresh off   — matikan
    """
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    arg     = (context.args[0].lower() if context.args else "").strip()

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO user_settings (user_id) VALUES (?)", (user_id,))

        if arg == "on":
            await db.execute(
                "UPDATE user_settings SET autorefresh_enabled=1, autorefresh_chat_id=? WHERE user_id=?",
                (chat_id, user_id),
            )
            await db.commit()
            await update.message.reply_text(
                "✅ *Auto\\-Refresh Harga* diaktifkan\\!\n\n"
                "🕙 Setiap hari pukul *10\\.00 WIB* bot akan otomatis refresh\n"
                "harga semua kartu di inventory kamu\\.\n\n"
                "📬 Kamu akan dapat notif ringkasan perubahan harga\\.\n"
                "_Matikan kapan saja: `/autorefresh off`_",
                parse_mode="MarkdownV2",
            )

        elif arg == "off":
            await db.execute(
                "UPDATE user_settings SET autorefresh_enabled=0 WHERE user_id=?", (user_id,)
            )
            await db.commit()
            await update.message.reply_text(
                "🔕 *Auto\\-Refresh* dimatikan\\.\n"
                "_Refresh manual tetap bisa kapan saja dengan /refresh_",
                parse_mode="MarkdownV2",
            )

        else:
            async with db.execute(
                "SELECT autorefresh_enabled FROM user_settings WHERE user_id=?", (user_id,)
            ) as cur:
                row = await cur.fetchone()
            is_on  = row and row[0]
            status = "✅ *Aktif* \\— refresh tiap hari jam 10\\.00 WIB" if is_on else "❌ *Tidak aktif*"
            await update.message.reply_text(
                f"🔄 *Status Auto\\-Refresh:*\n{status}\n\n"
                "Gunakan:\n"
                "• `/autorefresh on`  — aktifkan\n"
                "• `/autorefresh off` — matikan",
                parse_mode="MarkdownV2",
            )


async def auto_refresh_all_users(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Background job — auto-refresh harga untuk semua user opt-in. Jalan tiap hari 03:00 UTC."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT user_id, autorefresh_chat_id FROM user_settings "
            "WHERE autorefresh_enabled=1 AND autorefresh_chat_id IS NOT NULL"
        ) as cur:
            users = await cur.fetchall()

    logger.info(f"[auto_refresh] Mulai untuk {len(users)} user")

    for user_id, chat_id in users:
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                async with db.execute(
                    "SELECT id, card_name, card_set, price_usd FROM inventory WHERE user_id=?",
                    (user_id,),
                ) as cur:
                    items = await cur.fetchall()

            if not items:
                continue

            updated, errors = 0, 0
            naik, turun     = [], []

            for (item_id, card_name, card_set, old_usd) in items:
                try:
                    card = await search_pokemon_card(card_name)
                    if card and not (isinstance(card, dict) and card.get("error")):
                        new_usd = card["price_usd"]
                        new_idr = card["price_idr"]
                        diff    = new_usd - (old_usd or 0.0)

                        async with aiosqlite.connect(DB_PATH) as db:
                            await db.execute(
                                "UPDATE inventory SET price_usd=?, price_idr=? WHERE id=?",
                                (new_usd, new_idr, item_id),
                            )
                            await db.execute(
                                "INSERT INTO price_history "
                                "(user_id, card_name, card_set, price_usd, price_idr) VALUES (?,?,?,?,?)",
                                (user_id, card_name, card_set, new_usd, new_idr),
                            )
                            await db.commit()
                        updated += 1

                        if diff > 0.50:
                            naik.append(f"📈 {esc(card_name)}: \\+{esc(f'{diff:.2f}')} USD")
                        elif diff < -0.50:
                            turun.append(f"📉 {esc(card_name)}: {esc(f'{diff:.2f}')} USD")
                    else:
                        errors += 1
                except Exception as e:
                    logger.warning(f"[auto_refresh] Gagal update {card_name}: {e}")
                    errors += 1

                await asyncio.sleep(0.5)

            # Susun ringkasan notif
            lines = [
                f"🔄 *Auto\\-Refresh Harga Selesai*",
                f"📅 {esc(datetime.now().strftime('%d %b %Y, %H:%M WIB'))}",
                f"✅ {updated}/{len(items)} kartu diperbarui",
            ]
            if naik:
                lines.append(f"\n*📈 Naik Signifikan:*")
                lines.extend(naik[:5])
                if len(naik) > 5:
                    lines.append(f"_\\+ {len(naik)-5} lainnya_")
            if turun:
                lines.append(f"\n*📉 Turun Signifikan:*")
                lines.extend(turun[:5])
                if len(turun) > 5:
                    lines.append(f"_\\+ {len(turun)-5} lainnya_")
            if not naik and not turun:
                lines.append("_Harga stabil, tidak ada perubahan \\>\\$0\\.50_")
            if errors:
                lines.append(f"\n⚠️ {errors} kartu gagal diupdate \\(timeout API\\)")
            lines.append(f"\n_/inventory \\| /portfolio \\| /roi_")

            await context.bot.send_message(
                chat_id=chat_id,
                text="\n".join(lines),
                parse_mode="MarkdownV2",
            )
            logger.info(f"[auto_refresh] User {user_id}: {updated}/{len(items)} updated, {errors} errors")

        except Exception as e:
            logger.error(f"[auto_refresh] Error user {user_id}: {e}")


# ─── 2. FOLDER / KATEGORI KOLEKSI ────────────────────────────────────────────

async def folder_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /folder                 — ringkasan semua folder
    /folder <id>            — pilih folder via keyboard
    /folder <id> <nama>     — langsung set folder
    """
    user_id = update.effective_user.id
    args    = context.args

    if not args:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT COALESCE(folder,'Pribadi') AS f, COUNT(*), SUM(price_usd) "
                "FROM inventory WHERE user_id=? GROUP BY f ORDER BY SUM(price_usd) DESC",
                (user_id,),
            ) as cur:
                rows = await cur.fetchall()

        if not rows:
            await update.message.reply_text("📂 Inventory kosong\\.", parse_mode="MarkdownV2")
            return

        lines = ["📁 *Folder Koleksi Kamu:*\n"]
        for (fname, count, total_usd) in rows:
            em = FOLDER_EMOJI.get(fname, "📂")
            lines.append(f"{em} *{esc(fname)}* — {count} kartu \\| {esc_usd(total_usd or 0)}")
        lines.append(f"\n📌 _Folder: Pribadi, Dijual, Graded, Trade, Display_")
        lines.append(f"_/folder \\<id\\> untuk pindah folder kartu_")
        lines.append(f"_/infolder \\<nama\\> untuk lihat isi folder_")
        await update.message.reply_text("\n".join(lines), parse_mode="MarkdownV2")
        return

    if len(args) == 1:
        try:
            card_id = int(args[0])
        except ValueError:
            await update.message.reply_text(
                "⚠️ Format: `/folder <id_kartu>` atau `/folder <id> <nama_folder>`\n"
                "_Lihat ID di /inventory_",
                parse_mode="MarkdownV2",
            )
            return

        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT card_name, COALESCE(folder,'Pribadi') FROM inventory WHERE id=? AND user_id=?",
                (card_id, user_id),
            ) as cur:
                row = await cur.fetchone()

        if not row:
            await update.message.reply_text("❌ Kartu tidak ditemukan\\.", parse_mode="MarkdownV2")
            return

        card_name, current_folder = row
        buttons = []
        for fname in FOLDER_CHOICES:
            tick  = "✅ " if fname == current_folder else ""
            label = f"{tick}{FOLDER_EMOJI.get(fname,'📂')} {fname}"
            buttons.append(InlineKeyboardButton(label, callback_data=f"folder_set:{card_id}:{fname}"))

        keyboard = InlineKeyboardMarkup([buttons[:3], buttons[3:]])
        await update.message.reply_text(
            f"📁 Pindahkan *{esc(card_name)}* ke folder mana?\n"
            f"_Sekarang: {FOLDER_EMOJI.get(current_folder,'📂')} {esc(current_folder)}_",
            parse_mode="MarkdownV2",
            reply_markup=keyboard,
        )
        return

    try:
        card_id = int(args[0])
    except ValueError:
        await update.message.reply_text(
            "⚠️ ID harus angka\\. Contoh: `/folder 5 Graded`", parse_mode="MarkdownV2"
        )
        return

    raw_folder  = " ".join(args[1:]).strip().title()
    folder_map  = {f.lower(): f for f in FOLDER_CHOICES}
    folder_name = folder_map.get(raw_folder.lower(), raw_folder)

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT card_name FROM inventory WHERE id=? AND user_id=?", (card_id, user_id)
        ) as cur:
            row = await cur.fetchone()
        if not row:
            await update.message.reply_text("❌ Kartu tidak ditemukan di inventory kamu\\.", parse_mode="MarkdownV2")
            return
        await db.execute(
            "UPDATE inventory SET folder=? WHERE id=? AND user_id=?",
            (folder_name, card_id, user_id),
        )
        await db.commit()

    em = FOLDER_EMOJI.get(folder_name, "📂")
    await update.message.reply_text(
        f"{em} *{esc(row[0])}* dipindah ke folder *{esc(folder_name)}*\\!",
        parse_mode="MarkdownV2",
    )


async def folder_set_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Callback dari inline keyboard /folder <id>"""
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id

    _, card_id_str, folder_name = query.data.split(":", 2)
    card_id = int(card_id_str)

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT card_name FROM inventory WHERE id=? AND user_id=?", (card_id, user_id)
        ) as cur:
            row = await cur.fetchone()
        if not row:
            await query.edit_message_text("❌ Kartu tidak ditemukan\\.", parse_mode="MarkdownV2")
            return
        await db.execute(
            "UPDATE inventory SET folder=? WHERE id=? AND user_id=?",
            (folder_name, card_id, user_id),
        )
        await db.commit()

    em = FOLDER_EMOJI.get(folder_name, "📂")
    await query.edit_message_text(
        f"✅ *{esc(row[0])}* → {em} *{esc(folder_name)}*",
        parse_mode="MarkdownV2",
    )


async def infolder_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/infolder <nama> — lihat semua kartu di folder tertentu"""
    user_id = update.effective_user.id

    if not context.args:
        folder_list = " \\| ".join(
            f"{FOLDER_EMOJI.get(f,'📂')} {esc(f)}" for f in FOLDER_CHOICES
        )
        await update.message.reply_text(
            f"📁 *Folder tersedia:*\n{folder_list}\n\nContoh: `/infolder Graded`",
            parse_mode="MarkdownV2",
        )
        return

    raw_folder  = " ".join(context.args).strip().title()
    folder_map  = {f.lower(): f for f in FOLDER_CHOICES}
    folder_name = folder_map.get(raw_folder.lower(), raw_folder)

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, card_name, card_set, condition, price_usd, psa_grade "
            "FROM inventory WHERE user_id=? AND COALESCE(folder,'Pribadi')=? "
            "ORDER BY price_usd DESC",
            (user_id, folder_name),
        ) as cur:
            rows = await cur.fetchall()

    em = FOLDER_EMOJI.get(folder_name, "📂")

    if not rows:
        await update.message.reply_text(
            f"{em} Folder *{esc(folder_name)}* kosong\\.\n"
            f"_Pindah kartu: /folder \\<id\\> {esc(folder_name)}_",
            parse_mode="MarkdownV2",
        )
        return

    total = sum(r[4] or 0 for r in rows)
    lines = [f"{em} *Folder {esc(folder_name)}* \\({len(rows)} kartu\\)\n"]
    for (cid, name, cset, cond, price_usd, grade) in rows:
        grade_str = f" \\| 🏆 *{esc(str(grade))}*" if grade else ""
        lines.append(f"\\[{cid}\\] *{esc(name)}* — {esc_usd(price_usd or 0)}{grade_str}")
    lines.append(f"\n💰 *Total: {esc_usd(total)}*")
    lines.append(f"_/folder \\<id\\> \\<nama\\> untuk pindah kartu_")
    await update.message.reply_text("\n".join(lines), parse_mode="MarkdownV2")


# ─── 3. LAPORAN P&L BULANAN ──────────────────────────────────────────────────

_BULAN_ID = {
    "januari": 1,  "februari": 2,  "maret": 3,    "april": 4,
    "mei": 5,      "juni": 6,      "juli": 7,      "agustus": 8,
    "september": 9,"oktober": 10,  "november": 11, "desember": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4,
    "jun": 6, "jul": 7, "agu": 8, "ags": 8,
    "sep": 9, "okt": 10, "nov": 11, "des": 12,
}
_BULAN_NAMA = [
    "", "Januari","Februari","Maret","April","Mei","Juni",
    "Juli","Agustus","September","Oktober","November","Desember",
]

async def pl_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /pl              — P&L bulan ini
    /pl Oktober      — bulan Oktober tahun ini
    /pl Oktober 2025 — bulan + tahun spesifik
    /pl 10 2025      — angka bulan + tahun
    """
    user_id = update.effective_user.id
    now     = datetime.now()
    args    = context.args

    target_month, target_year = now.month, now.year

    if args:
        a0 = args[0].lower().strip()
        if a0.isdigit():
            target_month = int(a0)
            if len(args) >= 2 and args[1].isdigit():
                target_year = int(args[1])
        elif a0 in _BULAN_ID:
            target_month = _BULAN_ID[a0]
            if len(args) >= 2 and args[1].isdigit():
                target_year = int(args[1])
        else:
            await update.message.reply_text(
                "⚠️ Format: `/pl` \\| `/pl Oktober` \\| `/pl 10 2025`",
                parse_mode="MarkdownV2",
            )
            return

    if not (1 <= target_month <= 12):
        await update.message.reply_text("⚠️ Bulan tidak valid \\(1\\-12\\)\\.", parse_mode="MarkdownV2")
        return

    month_str   = f"{target_year}-{target_month:02d}"
    bulan_label = f"{_BULAN_NAMA[target_month]} {target_year}"

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT card_name, card_set, sell_price_usd, buy_price_usd, profit_usd, sold_at "
            "FROM trade_log WHERE user_id=? AND sold_at LIKE ? ORDER BY sold_at DESC",
            (user_id, f"{month_str}%"),
        ) as cur:
            rows = await cur.fetchall()

    if not rows:
        await update.message.reply_text(
            f"📊 *P&L {esc(bulan_label)}*\n\n"
            f"_Tidak ada transaksi jual di bulan ini\\._\n\n"
            f"Catat penjualan dengan: `/jual <id> <harga_usd>`",
            parse_mode="MarkdownV2",
        )
        return

    total_sell   = sum(r[2] or 0 for r in rows)
    total_buy    = sum(r[3] or 0 for r in rows)
    total_profit = sum(r[4] or 0 for r in rows)
    em_total = "📈" if total_profit >= 0 else "📉"
    sign     = "\\+" if total_profit >= 0 else ""

    lines = [
        f"📊 *Laporan P&L — {esc(bulan_label)}*\n",
        f"🔢 Transaksi   : *{len(rows)} kartu terjual*",
        f"🛒 Total Modal : {esc_usd(total_buy)}",
        f"💵 Total Jual  : {esc_usd(total_sell)}",
        f"{em_total} *Net Profit  : {sign}{esc_usd(abs(total_profit))}*",
    ]

    if total_buy > 0:
        roi_pct  = (total_profit / total_buy) * 100
        roi_sign = "\\+" if roi_pct >= 0 else ""
        lines.append(f"📐 ROI         : {roi_sign}{esc(f'{roi_pct:.1f}')}%")

    winners = sorted([(r[0], r[4] or 0) for r in rows if (r[4] or 0) > 0],
                     key=lambda x: x[1], reverse=True)
    losers  = sorted([(r[0], r[4] or 0) for r in rows if (r[4] or 0) < 0],
                     key=lambda x: x[1])

    if winners:
        lines.append(f"\n🏆 *Paling Untung:*")
        for name, profit in winners[:3]:
            lines.append(f"   \\+ {esc(name)}: {esc_usd(profit)}")
    if losers:
        lines.append(f"\n⚠️ *Rugi:*")
        for name, profit in losers[:3]:
            lines.append(f"   \\- {esc(name)}: \\-{esc_usd(abs(profit))}")

    lines.append(f"\n*📋 Semua Transaksi {esc(bulan_label)}:*")
    for (name, cset, sell, buy, profit, sold_at) in rows:
        tgl      = sold_at[:10] if sold_at else "?"
        em       = "📈" if (profit or 0) >= 0 else "📉"
        buy_str  = esc_usd(buy) if buy else "N/A"
        prof_str = (f"\\+{esc_usd(profit)}" if (profit or 0) >= 0
                    else f"\\-{esc_usd(abs(profit or 0))}")
        lines.append(
            f"  {em} *{esc(name)}*\n"
            f"     Modal {buy_str} → Jual {esc_usd(sell or 0)} \\| *{prof_str}* \\({esc(tgl)}\\)"
        )

    lines.append(f"\n_/riwayatjual untuk semua riwayat_")
    await update.message.reply_text("\n".join(lines), parse_mode="MarkdownV2")


# ─── 4. KALKULASI WORTH GRADING ──────────────────────────────────────────────

_GRADING_SERVICES = {
    "PSA Economy":  {"cost": 25.0,  "turnaround": "~45 hari"},
    "PSA Value":    {"cost": 50.0,  "turnaround": "~20 hari"},
    "PSA Express":  {"cost": 150.0, "turnaround": "~10 hari"},
    "CGC Standard": {"cost": 18.0,  "turnaround": "~65 hari"},
    "CGC Express":  {"cost": 75.0,  "turnaround": "~15 hari"},
}
_PSA_MULTIPLIERS = {
    "PSA 7":  (0.90, 1.30),
    "PSA 8":  (1.40, 2.00),
    "PSA 9":  (2.50, 4.00),
    "PSA 10": (5.00, 15.0),
}

async def hitunggrade_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /hitunggrade <nama kartu>   — cari harga dari API lalu kalkulasi
    /hitunggrade id:<id>        — pakai kartu dari inventory kamu
    """
    user_id = update.effective_user.id

    if not context.args:
        await update.message.reply_text(
            "📐 *Kalkulator Worth Grading*\n\n"
            "Hitung apakah biaya grading PSA/CGC sepadan dengan\n"
            "kenaikan nilai kartu kamu\\.\n\n"
            "*Format:*\n"
            "• `/hitunggrade Charizard Base Set`\n"
            "• `/hitunggrade id:5` \\(dari inventory kamu\\)\n\n"
            "_Estimasi berdasarkan rata\\-rata harga pasar\\._",
            parse_mode="MarkdownV2",
        )
        return

    raw_input = " ".join(context.args).strip()
    card_name = None
    raw_price = None
    buy_price = 0.0
    card_cond = "Near Mint"

    if raw_input.lower().startswith("id:"):
        try:
            card_id = int(raw_input[3:].strip())
        except ValueError:
            await update.message.reply_text(
                "⚠️ Format: `/hitunggrade id:<nomor_id>`", parse_mode="MarkdownV2"
            )
            return

        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT card_name, card_set, price_usd, buy_price_usd, condition "
                "FROM inventory WHERE id=? AND user_id=?",
                (card_id, user_id),
            ) as cur:
                row = await cur.fetchone()

        if not row:
            await update.message.reply_text("❌ Kartu tidak ditemukan di inventory\\.", parse_mode="MarkdownV2")
            return

        card_name = row[0] + (f" ({row[1]})" if row[1] else "")
        raw_price = row[2] or 0.0
        buy_price = row[3] or 0.0
        card_cond = row[4] or "Near Mint"
    else:
        card_name = raw_input

    if raw_price is None or raw_price == 0:
        status_msg = await update.message.reply_text(
            f"🔍 Mencari harga *{esc(card_name)}*\\.\\.\\.", parse_mode="MarkdownV2"
        )
        card_data = await search_pokemon_card(card_name)
        if not card_data or (isinstance(card_data, dict) and card_data.get("error")):
            await status_msg.edit_text(
                f"❌ *{esc(card_name)}* tidak ketemu di database harga\\.\n"
                f"Coba nama lebih spesifik, misal: `Charizard Base Set`",
                parse_mode="MarkdownV2",
            )
            return
        raw_price = card_data["price_usd"]
        card_name = card_data.get("name", card_name)
        await status_msg.delete()

    if not raw_price or raw_price <= 0:
        await update.message.reply_text(
            f"⚠️ Harga *{esc(card_name)}* tidak diketahui \\(\\$0\\)\\. Tidak bisa menghitung\\.",
            parse_mode="MarkdownV2",
        )
        return

    lines = [
        f"📐 *Kalkulator Worth Grading*",
        f"🃏 *{esc(card_name)}*",
        f"💵 Harga Raw   : *{esc_usd(raw_price)}*",
    ]
    if buy_price > 0:
        lines.append(f"🛒 Harga Beli  : *{esc_usd(buy_price)}*")
    if card_cond:
        lines.append(f"📋 Kondisi     : _{esc(card_cond)}_")

    lines.append(f"\n*📊 Estimasi Nilai per Grade PSA:*")
    for grade, (lo, hi) in _PSA_MULTIPLIERS.items():
        est_lo  = raw_price * lo
        est_hi  = raw_price * hi
        gain_lo = est_lo - raw_price
        gain_hi = est_hi - raw_price
        lines.append(
            f"🏅 *{esc(grade)}*: {esc_usd(est_lo)} — {esc_usd(est_hi)}"
            f"  \\(\\+{esc_usd(gain_lo)} s/d \\+{esc_usd(gain_hi)}\\)"
        )

    lines.append(f"\n*💸 Biaya Grading:*")
    for service, info in _GRADING_SERVICES.items():
        lines.append(f"   • *{esc(service)}*: {esc_usd(info['cost'])} \\| {esc(info['turnaround'])}")

    psa_economy = 25.0
    lines.append(f"\n*🎯 Break\\-Even \\(PSA Economy {esc_usd(psa_economy)}\\):*")
    for grade, (lo, hi) in _PSA_MULTIPLIERS.items():
        mid_val    = raw_price * ((lo + hi) / 2)
        net_profit = mid_val - raw_price - psa_economy
        em         = "✅" if net_profit > 0 else "❌"
        sign       = "\\+" if net_profit >= 0 else ""
        lines.append(f"   {em} *{esc(grade)}*: {sign}{esc_usd(net_profit)} net profit")

    lines.append(f"\n*💡 Rekomendasi:*")
    if raw_price >= 100:
        rek = ("✅ *Worth banget\\!* Kartu mahal sangat layak di\\-grade\\. "
               "Gap PSA 9 vs 10 bisa sangat besar\\.")
    elif raw_price >= 50:
        rek = ("✅ *Worth di\\-grade* jika kondisi Mint/Near Mint\\. "
               "PSA 9 estimasi 2\\.5\\-4x dari harga raw\\.")
    elif raw_price >= 15:
        rek = ("⚖️ *Pertimbangkan dulu\\.* Worth jika kartu punya sentimen tinggi "
               "atau yakin dapat PSA 10\\.")
    else:
        rek = ("❌ *Kurang worth untuk kartu murah\\.* "
               "Biaya grading bisa melebihi potensi kenaikan nilai\\.")
    lines.append(rek)

    lines.append(f"\n_⚠️ Estimasi berdasarkan rata\\-rata historis pasar\\._")
    lines.append(f"_Cek harga grading terkini: psa\\.com / cgccomics\\.com_")

    await update.message.reply_text("\n".join(lines), parse_mode="MarkdownV2")


def main() -> None:
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(post_init).build()

    # Commands lama
    app.add_handler(CommandHandler("start",         start))
    app.add_handler(CommandHandler("help",          help_command))
    app.add_handler(CommandHandler("add",           add_inventory))
    app.add_handler(CommandHandler("inventory",     show_inventory))
    app.add_handler(CommandHandler("refresh",       refresh_inventory))
    app.add_handler(CommandHandler("delete",        delete_inventory))
    app.add_handler(CommandHandler("setcondition",  set_condition))
    app.add_handler(CommandHandler("setgrade",      set_grade))
    app.add_handler(CommandHandler("wish",          add_wishlist_v3))
    app.add_handler(CommandHandler("wishlist",      show_wishlist))
    app.add_handler(CommandHandler("removewish",    remove_wishlist))
    app.add_handler(CommandHandler("stats",         show_stats))
    app.add_handler(CommandHandler("export",        export_inventory))
    app.add_handler(CommandHandler("compare",       compare_cards))
    app.add_handler(CommandHandler("top10",         top10_inventory))
    app.add_handler(CommandHandler("history",       price_history_cmd))
    app.add_handler(CommandHandler("alert",         set_alert))
    app.add_handler(CommandHandler("alerts",        show_alerts))
    app.add_handler(CommandHandler("removealert",   remove_alert))
    # Commands baru v2
    app.add_handler(CommandHandler("scanset",       scan_set))
    app.add_handler(CommandHandler("portfoliochart",portfolio_chart))
    app.add_handler(CommandHandler("findcheap",     find_cheap))
    app.add_handler(CommandHandler("duplikat",      show_duplikat))
    app.add_handler(CommandHandler("nilai",         nilai_kondisi))
    app.add_handler(CommandHandler("lang",          set_language))
    app.add_handler(CommandHandler("backup",        backup_db))
    # Commands baru v3
    app.add_handler(CommandHandler("buyprice",      set_buyprice))
    app.add_handler(CommandHandler("roi",           show_roi))
    app.add_handler(CommandHandler("trend",         price_trend_cmd))
    app.add_handler(CommandHandler("setkomplit",    set_completion))
    app.add_handler(CommandHandler("jual",          jual_kartu))
    app.add_handler(CommandHandler("riwayatjual",   riwayat_jual))
    app.add_handler(CommandHandler("debugstate",    debug_state))
    # Commands baru v4
    app.add_handler(CommandHandler("newsets",       new_sets_cmd))
    app.add_handler(CommandHandler("newcards",      new_cards_cmd))
    app.add_handler(CommandHandler("gen",           gen_browse))
    app.add_handler(CommandHandler("synccards",     sync_cards_cmd))
    app.add_handler(CommandHandler("cari",          cari_lokal))
    # Commands baru v6
    app.add_handler(CommandHandler("hargalokal",   harga_lokal_cmd))
    app.add_handler(CommandHandler("portohistory", porto_history_cmd))
    app.add_handler(CommandHandler("saraanjual",   saran_jual_cmd))
    app.add_handler(CommandHandler("editprice",    edit_price_cmd))
    app.add_handler(CommandHandler("photo",        show_card_photo))

    app.add_handler(CallbackQueryHandler(handle_snap_save,    pattern=r"^snap_(save|buy|wish):"))
    app.add_handler(CallbackQueryHandler(handle_card_select,  pattern=r"^card_select:"))
    app.add_handler(CallbackQueryHandler(handle_manual_save,      pattern=r"^manual_save:"))
    app.add_handler(CallbackQueryHandler(handle_manual_retry,     pattern=r"^manual_retry:"))
    app.add_handler(CallbackQueryHandler(handle_manual_confirm,   pattern=r"^manual_confirm:"))
    app.add_handler(CallbackQueryHandler(handle_manual_editnama,  pattern=r"^manual_editnama:"))
    app.add_handler(CallbackQueryHandler(handle_manual_editharga, pattern=r"^manual_editharga:"))
    app.add_handler(CallbackQueryHandler(handle_manual_cancel,    pattern=r"^manual_cancel:"))
    app.add_handler(CallbackQueryHandler(handle_ocr_use,          pattern=r"^ocr_use:"))
    app.add_handler(CallbackQueryHandler(handle_ocr_manual,       pattern=r"^ocr_manual:"))
    app.add_handler(MessageHandler(filters.PHOTO,                      handle_photo_search))
    app.add_handler(MessageHandler(filters.Document.ALL,               handle_csv_upload))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,    handle_card_search_v4))

    # Global error handler — tangkap semua exception yang lolos dari handler
    async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.error("Unhandled exception:", exc_info=context.error)
        if isinstance(update, Update) and update.effective_message:
            try:
                await update.effective_message.reply_text(
                    "⚠️ Ada error tak terduga bre\\. Coba lagi atau ketik `/start`\\.",
                    parse_mode="MarkdownV2",
                )
            except Exception:
                pass  # jangan crash error handler itu sendiri

    app.add_error_handler(global_error_handler)

    # Commands baru v7
    app.add_handler(CommandHandler("setphoto",   set_card_photo_cmd))
    app.add_handler(CommandHandler("editkartu",  editkartu_cmd))
    app.add_handler(CommandHandler("portfolio",  show_portfolio))
    app.add_handler(CommandHandler("setalert",   setalert_persen))
    app.add_handler(CommandHandler("share",      share_card))
    app.add_handler(CallbackQueryHandler(editkartu_nama_cb,  pattern=r"^editkartu_nama:"))
    app.add_handler(CallbackQueryHandler(editkartu_harga_cb, pattern=r"^editkartu_harga:"))
    app.add_handler(CallbackQueryHandler(editkartu_batal_cb, pattern=r"^editkartu_batal:"))

    # Commands baru v8
    app.add_handler(CommandHandler("setphoto2",   set_card_photo2_cmd))
    app.add_handler(CommandHandler("photo2",      show_card_photo2))
    app.add_handler(CommandHandler("updateharga", updateharga_cmd))
    app.add_handler(CommandHandler("prediksi",    prediksi_cmd))
    app.add_handler(CommandHandler("tag",         tag_cmd))
    app.add_handler(CommandHandler("remind",      remind_cmd))
    app.add_handler(CommandHandler("listing",     listing_cmd))
    app.add_handler(CommandHandler("exportpdf",   exportpdf_cmd))
    app.add_handler(CommandHandler("comparebulan",comparebulan_cmd))
    # Tong sampah (soft-delete + restore)
    app.add_handler(CommandHandler("trash",       show_trash))
    app.add_handler(CallbackQueryHandler(restore_card_cb, pattern=r"^restore_card:"))
    app.add_handler(CallbackQueryHandler(purge_card_cb,   pattern=r"^purge_card:"))

    # Commands baru v9 (lokal, tanpa API harga)
    app.add_handler(CommandHandler("winnerloser",       winnerloser_cmd))
    app.add_handler(CommandHandler("setcomplete",       setcomplete_cmd))
    app.add_handler(CommandHandler("graderekomendasi",  graderekomendasi_cmd))
    app.add_handler(CommandHandler("tradein",           tradein_cmd))
    app.add_handler(CommandHandler("daily",             toggle_daily_cmd))
    app.add_handler(CommandHandler("qr",                qr_cmd))
    app.add_handler(CommandHandler("katalog",           katalog_cmd))

    # v10 — UX + Manajemen Koleksi + Analitik Lokal
    app.add_handler(CommandHandler("menu",         menu_cmd))
    app.add_handler(CommandHandler("note",         note_cmd))
    app.add_handler(CommandHandler("forsale",      forsale_cmd))
    app.add_handler(CommandHandler("unsale",       unsale_cmd))
    app.add_handler(CommandHandler("salejual",     salejual_cmd))
    app.add_handler(CommandHandler("albumset",     albumset_cmd))
    app.add_handler(CommandHandler("rekap",        rekap_cmd))
    app.add_handler(CommandHandler("duplikatjual", duplikatjual_cmd))
    app.add_handler(CommandHandler("hitung",       hitung_cmd))
    app.add_handler(CallbackQueryHandler(inventory_page_cb, pattern=r"^inv_page:"))
    app.add_handler(CallbackQueryHandler(menu_cb,           pattern=r"^menu:"))

    # v11 — Auto-Refresh, Folder, P&L, Worth Grading
    app.add_handler(CommandHandler("autorefresh",  autorefresh_cmd))
    app.add_handler(CommandHandler("folder",       folder_cmd))
    app.add_handler(CommandHandler("infolder",     infolder_cmd))
    app.add_handler(CommandHandler("pl",           pl_cmd))
    app.add_handler(CommandHandler("hitunggrade",  hitunggrade_cmd))
    app.add_handler(CallbackQueryHandler(folder_set_cb, pattern=r"^folder_set:"))

    # setgrade inline keyboard callbacks
    app.add_handler(CallbackQueryHandler(setgrade_grader_cb, pattern=r"^setgrade_grader:"))
    app.add_handler(CallbackQueryHandler(setgrade_nilai_cb,  pattern=r"^setgrade_nilai:"))

    # Background jobs
    jq = app.job_queue
    jq.run_repeating(auto_snapshot_all,  interval=86400, first=60)    # snapshot harian
    jq.run_repeating(check_reminders,    interval=3600,  first=120)   # cek reminder tiap jam
    # Laporan harian pukul 08:00 (UTC+7 = 01:00 UTC)
    jq.run_daily(send_daily_report, time=__import__("datetime").time(1, 0, 0))

    logger.info("Bot Pokémon Vision & Portfolio v11 aktif! (+autorefresh, folder, infolder, pl, hitunggrade)")
    app.run_polling()

if __name__ == "__main__":
    main()