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
from PIL import Image
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
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

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
            ("condition",     "TEXT DEFAULT 'Near Mint'"),
            ("psa_grade",     "TEXT DEFAULT NULL"),
            ("buy_price_usd", "REAL DEFAULT 0.0"),
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

        await db.commit()

# ── Helper: get user language ─────────────────────────────────────────────────
async def get_user_lang(user_id: int) -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT lang FROM user_settings WHERE user_id=?", (user_id,)) as cur:
            row = await cur.fetchone()
    return row[0] if row else "id"

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
    price_usd = f"\\${card['price_usd']:.2f}" if card["price_usd"] > 0 else "Tidak tersedia"
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

    price_str = f"\\${card['price_usd']:.2f}" if card["price_usd"] > 0 else "N/A"
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
        context.bot_data[f"pending_buy_{user_id}"] = new_id
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
        "HP: 50% \\| Damaged: 25%",
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
        f"💵 Harga: Rp {price_idr:,.0f} \\(\\${price_usd:.2f}\\)\n\n"
        "_Sudah benar?_",
        reply_markup=keyboard,
        parse_mode="MarkdownV2",
    )

# ── Handler manual save (tombol setelah kartu tidak ditemukan) ───────────────
async def handle_manual_save(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query     = update.callback_query
    await query.answer()
    user_id   = update.effective_user.id
    card_name = context.bot_data.pop(f"pending_manual_name_{user_id}", "Unknown Card")
    context.bot_data.pop(f"pending_photo_name_{user_id}", None)
    context.bot_data[f"pending_manual_price_{user_id}"] = card_name
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
    context.bot_data.pop(f"pending_manual_name_{user_id}", None)
    context.bot_data[f"pending_photo_name_{user_id}"] = True
    await query.message.reply_text(
        "📝 Ketik nama kartunya lagi bre:",
        parse_mode="MarkdownV2",
    )

async def handle_manual_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query   = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    data    = context.bot_data.pop(f"pending_manual_confirm_{user_id}", None)
    if not data:
        await query.message.reply_text("⚠️ Data expired, coba ulangi bre\\.", parse_mode="MarkdownV2")
        return
    name      = data["name"]
    price_idr = data["price_idr"]
    price_usd = data["price_usd"]
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO inventory (user_id, card_name, price_usd, price_idr, condition) VALUES (?,?,?,?,?)",
            (user_id, name, price_usd, price_idr, "Near Mint"),
        )
        new_id = cur.lastrowid
        await db.execute(
            "INSERT INTO price_history (user_id, card_name, price_usd, price_idr) VALUES (?,?,?,?)",
            (user_id, name, price_usd, price_idr),
        )
        await db.commit()
    await query.message.reply_text(
        f"✅ *{esc(name)}* disimpan\\! \\(ID: \\#{new_id}\\)\n"
        f"💵 \\${price_usd:.2f} \\| Rp {price_idr:,.0f}\n\n"
        f"_Set kondisi: /setcondition {new_id}_",
        parse_mode="MarkdownV2",
    )

async def handle_manual_editnama(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query   = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    data    = context.bot_data.pop(f"pending_manual_confirm_{user_id}", None)
    if not data:
        await query.message.reply_text("⚠️ Data expired, coba ulangi bre\\.", parse_mode="MarkdownV2")
        return
    context.bot_data[f"pending_manual_edit_nama_{user_id}"] = data
    await query.message.reply_text(
        f"✏️ Ketik nama kartu yang baru bre:\n_Nama sekarang: {esc(data['name'])}_",
        parse_mode="MarkdownV2",
    )

async def handle_manual_editharga(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query   = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    data    = context.bot_data.pop(f"pending_manual_confirm_{user_id}", None)
    if not data:
        await query.message.reply_text("⚠️ Data expired, coba ulangi bre\\.", parse_mode="MarkdownV2")
        return
    context.bot_data[f"pending_manual_edit_harga_{user_id}"] = data["name"]
    await query.message.reply_text(
        f"💰 Ketik harga baru \\(Rupiah\\) bre:\n"
        f"_Harga sekarang: Rp {data['price_idr']:,.0f}_",
        parse_mode="MarkdownV2",
    )

async def handle_manual_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query   = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    for key in [
        f"pending_manual_confirm_{user_id}", f"pending_manual_price_{user_id}",
        f"pending_manual_name_{user_id}",    f"pending_manual_edit_nama_{user_id}",
        f"pending_manual_edit_harga_{user_id}", f"pending_photo_name_{user_id}",
    ]:
        context.bot_data.pop(key, None)
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
    name    = context.bot_data.pop(f"pending_ocr_name_{user_id}", None)
    if not name:
        await query.message.reply_text("⚠️ Data expired, kirim foto lagi bre\\.", parse_mode="MarkdownV2")
        return
    # Langsung minta harga, tidak perlu cari API
    context.bot_data[f"pending_manual_name_{user_id}"] = name
    context.bot_data[f"pending_manual_price_{user_id}"] = True
    await query.message.reply_text(
        f"✅ Nama kartu: *{esc(name)}*\n\n"
        f"💰 Masukkan harga beli kamu \\(Rupiah\\)\\:\n_Contoh: `900000`_",
        parse_mode="MarkdownV2",
    )

async def handle_ocr_manual(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query   = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    context.bot_data.pop(f"pending_ocr_name_{user_id}", None)
    context.bot_data[f"pending_photo_name_{user_id}"] = True
    await query.message.reply_text(
        "📝 Ketik nama kartunya bre:",
        parse_mode="MarkdownV2",
    )

# ── Handler foto ─────────────────────────────────────────────────────────────
async def handle_photo_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id

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
            context.bot_data[f"pending_ocr_name_{user_id}"] = detected
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
            context.bot_data[f"pending_photo_name_{user_id}"] = True
            await status.edit_text(
                "📸 Foto diterima\\!\n\n"
                "📝 *Ketik nama kartunya bre:*\n"
                "_Contoh: `Pikachu Gym Event Campaign`_",
                parse_mode="MarkdownV2",
            )
    else:
        # Tesseract tidak terinstall → fallback manual
        context.bot_data[f"pending_photo_name_{user_id}"] = True
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

    price_str = f"\\${card['price_usd']:.2f}" if card["price_usd"] > 0 else "Tidak tersedia"
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
async def show_inventory(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, card_name, card_set, price_usd, price_idr, condition, psa_grade FROM inventory WHERE user_id=? ORDER BY id",
            (user_id,),
        ) as cur:
            items = await cur.fetchall()

    if not items:
        await update.message.reply_text(
            "📂 Inventory kosong\\! Tambah dengan `/add \\[nama\\]`\\.",
            parse_mode="MarkdownV2",
        )
        return

    total_usd = sum(r[3] for r in items)
    total_idr = sum(r[4] for r in items)

    lines = ["📦 *Portfolio Koleksi Pokémon:*\n"]
    for idx, (_, name, card_set, p_usd, p_idr, condition, psa_grade) in enumerate(items, 1):
        usd_str   = f"\\${p_usd:.2f}" if p_usd > 0 else "N/A"
        idr_str   = f"Rp {p_idr:,.0f}" if p_idr > 0 else "N/A"
        cond_str  = esc(condition or "Near Mint")
        grade_str = f" \\| 🏆 PSA {esc(psa_grade)}" if psa_grade else ""
        lines.append(
            f"{idx}\\. *{esc(name)}* \\({esc(card_set)}\\)\n"
            f"   ├ 🏷️ {cond_str}{grade_str}\n"
            f"   └ 💵 {usd_str} \\| {idr_str}\n"
        )

    lines.append(
        f"\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\n"
        f"💰 *Total: \\${total_usd:.2f} \\| Rp {total_idr:,.0f}*\n\n"
        f"⚙️ _/setcondition \\[no\\] \\[kondisi\\]_\n"
        f"🏆 _/setgrade \\[no\\] \\[grade\\]_\n"
        f"🗑️ _/delete \\[no\\]_ \\| 📥 _/export_"
    )

    await update.message.reply_text("\n".join(lines), parse_mode="MarkdownV2")

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
                changes.append(f"{arrow} *{esc(card_name)}*: \\${old_usd:.2f} → \\${new_usd:.2f}")

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
            price_usd = f"\\${usd:.2f}" if usd > 0 else "N/A"
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
        usd_str = f"\\${p_usd:.2f}" if p_usd > 0 else "N/A"
        idr_str = f"Rp {p_idr:,.0f}" if p_idr > 0 else "N/A"
        lines.append(
            f"{medal} *{esc(name)}*\n"
            f"   📦 {esc(card_set)} \\| 💵 {usd_str} \\| {idr_str}\n"
        )

    total_usd = sum(r[2] for r in items)
    lines.append("━━━━━━━━━━━━━━━━━━━━━━\n")
    lines.append(f"💰 Total top {len(items)}: *\\${total_usd:.2f}*")

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
        usd_str  = f"\\${price_usd:.2f}" if price_usd > 0 else "N/A"
        idr_str  = f"Rp {price_idr:,.0f}" if price_idr > 0 else "N/A"
        lines.append(f"📅 {date_str} {time_str}: *{usd_str}* \\| {idr_str}")

    if len(history) >= 2:
        latest = history[0][1]
        oldest = history[-1][1]
        diff   = latest - oldest
        if diff > 0:
            trend = f"📈 Naik \\${diff:.2f} dari awal pencatatan"
        elif diff < 0:
            trend = f"📉 Turun \\${abs(diff):.2f} dari awal pencatatan"
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
        desc = f"Notif kalau harga ≤ *\\${threshold:.2f}* 📉"
    else:
        desc = f"Notif kalau harga ≥ *\\${threshold:.2f}* 📈"

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
        direction = f"≤ \\${threshold:.2f} 📉" if alert_type == "turun" else f"≥ \\${threshold:.2f} 📈"
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
                            f"💵 Harga sekarang: *\\${current_price:.2f}* \\| Rp {idr:,.0f}\n"
                            f"🎯 Target kamu: {direction} \\${threshold:.2f}\n\n"
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
        f"💰 Total estimasi nilai: *\\${total_value:.2f}* \\| Rp {total_value * EXCHANGE_RATE:,.0f}\n",
        f"📊 Rata\\-rata/kartu: *\\${avg_value:.2f}*\n",
        f"\n🏆 *Top 10 Paling Mahal:*\n"
    ]

    for i, card in enumerate(cards_with_price[:10], 1):
        usd_str = f"\\${card['price_usd']:.2f}"
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
            lines.append(f"*{esc(set_name or 'Unknown')}*\n`{bar}` {pct:.1f}% \\(\\${value:.2f}\\)\n")
        lines.append(f"\n💰 *Total: \\${total:.2f}* \\| Rp {total * EXCHANGE_RATE:,.0f}")
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
            f"💰 Total: \\${total:.2f} \\| Rp {total_idr:,.0f}\n"
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
        usd_str = f"\\${card['price_usd']:.2f}"
        idr_str = f"Rp {card['price_idr']:,.0f}"
        lines.append(
            f"{emoji} {i}\\. *{esc(card['name'])}*\n"
            f"   📦 {esc(card['set'])}\n"
            f"   ⭐ {esc(card['rarity'])}\n"
            f"   💵 {usd_str} \\| {idr_str}\n"
        )

    lines.append(f"━━━━━━━━━━━━━━━━━━━━━━\n")
    lines.append(f"💡 Selisih termurah vs termahal: *\\${diff:.2f}*")

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
        usd_str = f"\\${total_usd:.2f}" if total_usd > 0 else "N/A"
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
            orig_str = f"\\${price_usd:.2f}"
            adj_str  = f"\\${adjusted:.2f}"
            pct      = int(multiplier * 100)
            cond_esc = esc(condition or "Near Mint")
            lines.append(
                f"🃏 *{esc(name)}* \\({cond_esc}\\)\n"
                f"   Market: {orig_str} → Real: *{adj_str}* \\({pct}%\\)\n"
            )

    diff     = total_adjusted - total_original
    diff_str = f"\\-\\${abs(diff):.2f}" if diff < 0 else f"\\+\\${diff:.2f}"

    lines.append(
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 *Ringkasan:*\n"
        f"• Market value: \\${total_original:.2f}\n"
        f"• Nilai real \\(kondisi\\): *\\${total_adjusted:.2f}* \\| Rp {total_adjusted * EXCHANGE_RATE:,.0f}\n"
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
            f"💰 Nilai Portfolio: \\${total_usd:.2f} \\| Rp {total_idr:,.0f}\n\n"
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
async def set_grade(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id

    if len(context.args) < 2:
        await update.message.reply_text(
            "⚠️ Format: `/setgrade \\[no\\] \\[grade\\]`\n"
            "Contoh: `/setgrade 1 PSA 10`",
            parse_mode="MarkdownV2"
        )
        return

    try:
        idx = int(context.args[0])
    except ValueError:
        await update.message.reply_text("⚠️ Nomor harus angka\\!", parse_mode="MarkdownV2")
        return

    grade = " ".join(context.args[1:]).strip()

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT id, card_name FROM inventory WHERE user_id=? ORDER BY id", (user_id,)) as cur:
            items = await cur.fetchall()
        if idx < 1 or idx > len(items):
            await update.message.reply_text("❌ Nomor tidak ditemukan\\.", parse_mode="MarkdownV2")
            return
        db_id, card_name = items[idx - 1]
        await db.execute("UPDATE inventory SET psa_grade=? WHERE id=?", (grade, db_id))
        await db.commit()

    await update.message.reply_text(
        f"🏆 Grade *{esc(card_name)}* → *{esc(grade)}*",
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

    price_str = f"\\${card['price_usd']:.2f}" if card["price_usd"] > 0 else "N/A"
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
        usd_str  = f"\\${p_usd:.2f}" if p_usd > 0 else "N/A"
        idr_str  = f"Rp {p_idr:,.0f}" if p_idr > 0 else "N/A"
        date_str = esc(added_at[:10]) if added_at else "\\-"
        lines.append(
            f"{idx}\\. *{esc(name)}* \\({esc(card_set)}\\)\n"
            f"   ├ 📅 {date_str}\n"
            f"   └ 💵 {usd_str} \\| {idr_str}\n"
        )

    lines.append(
        f"\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\n"
        f"💰 *Total Estimasi: \\${total_usd:.2f} \\| Rp {total_idr:,.0f}*\n\n"
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
        f"• Market value: \\${total_usd:.2f} \\| Rp {total_idr:,.0f}\n"
        f"• Nilai real \\(kondisi\\): *\\${adjusted_total:.2f}*\n"
        f"• Rata\\-rata: \\${avg_usd:.2f}/kartu\n\n"
        f"🥇 *Termahal:*\n"
        f"  {esc(top_card[0])} \\— \\${top_card[2]:.2f}\n\n"
    )
    if cheapest:
        msg += f"💸 *Termurah:*\n  {esc(cheapest[0])} \\— \\${cheapest[2]:.2f}\n\n"
    msg += (
        f"🏷️ *Kondisi:*\n{cond_lines}\n\n"
        f"📈 _/history \\[nama\\]_ \\| 🏆 _/top10_ \\| 📊 _/portfoliochart_\n"
        f"💎 _/nilai_ \\| 💸 _/findcheap \\[nama\\]_ \\| 💾 _/backup_"
    )

    await update.message.reply_text(msg, parse_mode="MarkdownV2")

# ── /delete ───────────────────────────────────────────────────────────────────
async def delete_inventory(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id

    if not context.args:
        await update.message.reply_text("⚠️ Format: `/delete 1`", parse_mode="MarkdownV2")
        return
    try:
        idx = int(context.args[0])
    except ValueError:
        await update.message.reply_text("⚠️ Nomor harus angka\\!", parse_mode="MarkdownV2")
        return

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT id, card_name FROM inventory WHERE user_id=? ORDER BY id", (user_id,)) as cur:
            items = await cur.fetchall()
        if idx < 1 or idx > len(items):
            await update.message.reply_text("❌ Nomor tidak ditemukan\\.", parse_mode="MarkdownV2")
            return
        db_id, card_name = items[idx - 1]
        await db.execute("DELETE FROM inventory WHERE id=? AND user_id=?", (db_id, user_id))
        await db.commit()

    await update.message.reply_text(
        f"🗑️ *{esc(card_name)}* berhasil dihapus\\!",
        parse_mode="MarkdownV2",
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
            f"💵 ${total_usd:.2f} \\| 🇮🇩 Rp {total_idr:,.0f}"
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

    # ── Cek pending_manual_edit_nama ──────────────────────────────────────────
    if f"pending_manual_edit_nama_{user_id}" in context.bot_data:
        data         = context.bot_data.pop(f"pending_manual_edit_nama_{user_id}")
        data["name"] = text
        context.bot_data[f"pending_manual_confirm_{user_id}"] = data
        await _show_manual_confirm(update.message, user_id, data)
        return

    # ── Cek pending_manual_edit_harga ─────────────────────────────────────────
    if f"pending_manual_edit_harga_{user_id}" in context.bot_data:
        card_name = context.bot_data.pop(f"pending_manual_edit_harga_{user_id}")
        try:
            price_idr = float(re.sub(r'[^\d.]', '', text))
            price_usd = round(price_idr / EXCHANGE_RATE, 2)
            data      = {"name": card_name, "price_idr": price_idr, "price_usd": price_usd}
            context.bot_data[f"pending_manual_confirm_{user_id}"] = data
            await _show_manual_confirm(update.message, user_id, data)
        except (ValueError, ZeroDivisionError):
            context.bot_data[f"pending_manual_edit_harga_{user_id}"] = card_name
            await update.message.reply_text(
                "⚠️ Masukkan angka Rupiah yang valid bre\\!\n_Contoh: `900000`_",
                parse_mode="MarkdownV2",
            )
        return

    # ── Cek pending_manual_price: user ketik harga IDR setelah klik Simpan Manual ──
    pending_manual = context.bot_data.get(f"pending_manual_price_{user_id}")
    if pending_manual is not None:
        # Nama bisa tersimpan langsung di key ini (alur lama) atau di pending_manual_name (alur foto)
        if pending_manual is True:
            card_name = context.bot_data.pop(f"pending_manual_name_{user_id}", None)
        else:
            card_name = pending_manual
        if not card_name:
            del context.bot_data[f"pending_manual_price_{user_id}"]
            await update.message.reply_text("⚠️ Data expired, kirim foto lagi bre\\.", parse_mode="MarkdownV2")
            return
        try:
            price_idr = float(re.sub(r'[^\d.]', '', text))
            price_usd = round(price_idr / EXCHANGE_RATE, 2)
            data      = {"name": card_name, "price_idr": price_idr, "price_usd": price_usd}
            del context.bot_data[f"pending_manual_price_{user_id}"]
            context.bot_data[f"pending_manual_confirm_{user_id}"] = data
            await _show_manual_confirm(update.message, user_id, data)
        except (ValueError, ZeroDivisionError):
            await update.message.reply_text(
                "⚠️ Masukkan angka Rupiah yang valid bre\\!\n_Contoh: `900000`_",
                parse_mode="MarkdownV2",
            )
        return

    # ── Cek pending_photo_name: user ketik nama manual setelah foto ──────────────
    if context.bot_data.pop(f"pending_photo_name_{user_id}", False):
        # Langsung minta harga, tidak perlu cari API
        context.bot_data[f"pending_manual_name_{user_id}"] = text
        context.bot_data[f"pending_manual_price_{user_id}"] = True
        await update.message.reply_text(
            f"✅ Nama kartu: *{esc(text)}*\n\n"
            f"💰 Masukkan harga beli kamu \\(Rupiah\\)\\:\n_Contoh: `900000`_",
            parse_mode="MarkdownV2",
        )
        return

    # ── Cek pending_buy: user balas harga modal setelah klik "Simpan + Set Modal" ──
    pending_id = context.bot_data.get(f"pending_buy_{user_id}")
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
            del context.bot_data[f"pending_buy_{user_id}"]
            await update.message.reply_text(
                f"✅ Modal disimpan\\!\n"
                f"💵 *${buy_usd:.2f}* \\(Rp {buy_idr:,.0f}\\) untuk inventory ID *#{pending_id}*\\.\n"
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
            p_str = f"\\${c['price_usd']:.2f}" if c["price_usd"] > 0 else "N/A"
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
            p_str = f"\\${p_usd:.2f}"
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
            p_str = f"\\${c['price_usd']:.2f}" if c["price_usd"] > 0 else "N/A"
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

    query = " ".join(context.args).strip()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT name, card_set, set_series, price_usd FROM card_cache "
            "WHERE LOWER(name) LIKE LOWER(?) ORDER BY price_usd DESC LIMIT 15",
            (f"%{query}%",),
        ) as cur:
            rows = await cur.fetchall()

    if not rows:
        suggestions = await get_autocomplete_suggestions(query, limit=5)
        if suggestions:
            lines = [f"🔍 *'{esc(query)}'* tidak ditemukan di cache\\.\n\n💡 *Mungkin maksudnya:*"]
            for s in suggestions:
                lines.append(f"• `{esc(s)}`")
            await update.message.reply_text("\n".join(lines), parse_mode="MarkdownV2")
        else:
            await update.message.reply_text(
                f"📭 Tidak ada kartu '{esc(query)}' di cache lokal\\.\n_Jalankan /synccards untuk update\\._",
                parse_mode="MarkdownV2",
            )
        return

    lines = [f"🔍 *Hasil Cache: '{esc(query)}' \\({len(rows)} kartu\\)*\n"]
    for name, card_set, series, p_usd in rows:
        p_str = f"\\${p_usd:.2f}" if p_usd > 0 else "N/A"
        lines.append(f"• *{esc(name)}* — {esc(card_set or '?')} \\| {esc(series or '?')} \\| {p_str}")
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
    profit_str = f"\\+\\${profit:.2f}" if profit >= 0 else f"\\-\\${abs(profit):.2f}"

    await update.message.reply_text(
        f"✅ *Harga beli disimpan\\!*\n\n"
        f"🃏 *{esc(card_name)}* \\({esc(card_set or '-')}\\)\n"
        f"💸 Harga Beli : \\${buy_price:.2f}\n"
        f"💵 Market    : \\${market_usd:.2f}\n"
        f"{emoji} Profit    : {profit_str} \\({esc(f'{pct:.1f}')}%\\)",
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
        p_str       = f"\\+{profit:.2f}" if profit >= 0 else f"\\-{abs(profit):.2f}"
        lines.append(
            f"{em} *{esc(name[:25])}*\n"
            f"   Beli \\${buy_usd:.2f} → Market \\${adj_market:.2f} \\| {esc(f'{pct:.1f}')}%  \\(\\${p_str}\\)\n"
        )

    if len(tagged) > 15:
        lines.append(f"_\\.\\.\\. dan {len(tagged)-15} kartu lainnya_\n")

    lines.append(
        f"\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\n"
        f"{emoji_total} *Total Modal  : \\${total_buy:.2f}*\n"
        f"{emoji_total} *Nilai Pasar  : \\${total_market:.2f}*\n"
        f"{emoji_total} *Profit/Loss  : \\${total_profit:+.2f} \\({total_pct:+.1f}%\\)*"
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
            lines.append(f"• {esc(ts[:10])} — \\${price:.2f}")
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
        f"📊 Kelengkapan: *{esc(f'{pct:.1f}')}%* \\[{esc(bar)}\\]\n",
        f"✅ Dimiliki   : *{len(have)}/{total_in_set}* kartu\n",
        f"❌ Belum punya: *{len(missing)}* kartu\n",
    ]

    if missing_cost > 0:
        lines.append(
            f"💸 Estimasi beli semua yang kurang:\n"
            f"   \\${missing_cost:.2f} \\| Rp {missing_idr:,.0f}\n"
        )

    if missing:
        lines.append(f"\n❌ *Belum punya \\({min(len(missing), 15)} ditampilkan\\):*")
        for c in missing[:15]:
            p = c.get("price_usd", 0)
            p_str = f" — \\${p:.2f}" if p > 0 else ""
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
    p_str  = f"\\+\\${profit:.2f}" if profit >= 0 else f"\\-\\${abs(profit):.2f}"
    buy_str = f"\\${buy_price:.2f}" if buy_price and buy_price > 0 else "N/A"

    await update.message.reply_text(
        f"💰 *Kartu Terjual\\!*\n\n"
        f"🃏 *{esc(card_name)}* \\({esc(card_set or '-')}\\)\n"
        f"💸 Harga Beli : {buy_str}\n"
        f"💵 Harga Jual : \\${sell_price:.2f} \\| Rp {sell_idr:,.0f}\n"
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
        p_str = f"\\+\\${profit:.2f}" if profit >= 0 else f"\\-\\${abs(profit):.2f}"
        date  = esc(sold_at[:10]) if sold_at else "\\-"
        buy_s = f"\\${buy_usd:.2f}" if buy_usd and buy_usd > 0 else "N/A"
        lines.append(
            f"{em} *{esc(name[:22])}*\n"
            f"   📅 {date} \\| Beli {buy_s} → Jual \\${sell_usd:.2f} \\| {p_str}\n"
        )

    lines.append(
        f"\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\n"
        f"{em_total} *Total Revenue : \\${total_revenue:.2f}*\n"
        f"{em_total} *Total Profit  : \\${total_profit:+.2f}*"
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
            target_str = f" \\| 🎯 Target: \\${target_price:.2f}" if target_price > 0 else ""
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

    price_str  = f"\\${card['price_usd']:.2f}" if card["price_usd"] > 0 else "N/A"
    idr_str    = f"Rp {card['price_idr']:,.0f}" if card["price_idr"] > 0 else "N/A"
    target_str = f"\n🎯 Target alert: \\${target_price:.2f}" if target_price > 0 else ""

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
                            f"Harga sekarang \\${current_usd:.2f} sudah ≤ target \\${target_usd:.2f}\\!\n"
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
    diff_sign = f"\\+${diff:.2f}" if diff >= 0 else f"\\-${abs(diff):.2f}"
    diff_tag  = f"📈 {diff_sign}" if diff > 0 else (f"📉 {diff_sign}" if diff < 0 else "➡️ Sama")

    await update.message.reply_text(
        f"✅ *Harga diupdate\\!*\n\n"
        f"🃏 *{esc(card_name)}* _{esc(card_set or '')}_\n"
        f"ID: \\#{inv_id}\n\n"
        f"Harga lama: \\${old_price:.2f}\n"
        f"Harga baru: *\\${new_price:.2f}* \\(Rp {new_idr:,.0f}\\)\n"
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
            f"  Rp {price_val:,} \\(≈\\${price_usd:.2f}\\) — _{esc(shop)}_"
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
            lines.append(f"• `{date}` — \\${usd:.2f} \\({cnt} kartu\\)")
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
            f"   💵 \\${c['price']:.2f} \\(Rp {price_idr:,.0f}\\) — {roi_tag}\n"
            f"   Kondisi: {esc(c['condition'])} \\| ID: #{c['id']}"
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

    # Background jobs
    jq = app.job_queue
    jq.run_repeating(auto_snapshot_all, interval=86400, first=60)   # snapshot harian

    logger.info("Bot Pokémon Vision & Portfolio v6 aktif! (+hargalokal, AI grading, portohistory, CSV import, saraanjual)")
    app.run_polling()

if __name__ == "__main__":
    main()
