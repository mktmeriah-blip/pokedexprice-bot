import asyncio
import base64
import csv
import io
import json
import logging
import os
import re
from datetime import datetime

import aiosqlite
import httpx
from dotenv import load_dotenv
from openai import AsyncOpenAI
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

load_dotenv(dotenv_path=Path(__file__).parent / ".env", override=True)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

OPENROUTER_API_KEY  = os.getenv("OPENROUTER_API_KEY")
TELEGRAM_TOKEN      = os.getenv("TELEGRAM_TOKEN")
EXCHANGE_RATE       = int(os.getenv("EXCHANGE_RATE", 16000))
DB_PATH             = os.getenv("DB_PATH", "pokemon_inventory.db")
OPENROUTER_MODEL    = os.getenv("OPENROUTER_MODEL", "meta-llama/llama-3.2-11b-vision-instruct:free")
POKEMON_TCG_API_KEY = os.getenv("POKEMON_TCG_API_KEY", "")

if not OPENROUTER_API_KEY or not TELEGRAM_TOKEN:
    raise RuntimeError("OPENROUTER_API_KEY dan TELEGRAM_TOKEN wajib diisi di .env!")

openrouter_client = AsyncOpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_API_KEY,
)

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
            ("condition", "TEXT DEFAULT 'Near Mint'"),
            ("psa_grade",  "TEXT DEFAULT NULL"),
        ]:
            try:
                await db.execute(f"ALTER TABLE inventory ADD COLUMN {col} {definition}")
            except Exception:
                pass

        await db.execute("""
            CREATE TABLE IF NOT EXISTS wishlist (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id   INTEGER NOT NULL,
                card_name TEXT    NOT NULL,
                card_set  TEXT,
                price_usd REAL    DEFAULT 0.0,
                price_idr REAL    DEFAULT 0.0,
                added_at  TEXT    DEFAULT CURRENT_TIMESTAMP
            )
        """)

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
def card_message(card: dict, label: str = "") -> str:
    suffix    = f" \\({label}\\)" if label else ""
    price_usd = f"\\${card['price_usd']:.2f}" if card["price_usd"] > 0 else "Tidak tersedia"
    price_idr = f"Rp {card['price_idr']:,.0f}" if card["price_idr"] > 0 else "Tidak tersedia"
    return (
        f"✨ *{esc(card['name'])}*{suffix} ✨\n"
        f"📦 Set: {esc(card['set'])}\n"
        f"⭐ Rarity: {esc(card['rarity'])}\n\n"
        f"💰 *Estimasi Harga:*\n"
        f"• Internasional: {price_usd}\n"
        f"• Pasaran Lokal \\(IDR\\): {price_idr}\n\n"
        f"💡 _Mau simpan? Ketik: /add {esc(card['name'])}_"
    )

async def send_card(update: Update, card: dict, label: str = "") -> None:
    msg = card_message(card, label)
    try:
        if card.get("image"):
            await update.message.reply_photo(photo=card["image"], caption=msg, parse_mode="MarkdownV2")
        else:
            await update.message.reply_text(msg, parse_mode="MarkdownV2")
    except Exception as e:
        logger.error(f"MarkdownV2 error, fallback plain: {e}")
        plain = (
            f"✨ {card['name']} ✨\nSet: {card['set']}\nRarity: {card['rarity']}\n\n"
            f"Harga: ${card['price_usd']:.2f} | Rp {card['price_idr']:,.0f}\n"
            f"Mau simpan? Ketik: /add {card['name']}"
        )
        if card.get("image"):
            await update.message.reply_photo(photo=card["image"], caption=plain)
        else:
            await update.message.reply_text(plain)

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
            "📸 *Fitur Scan:* Kirim foto kartu → AI baca nama, cek harga otomatis\\!\n"
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
        "📖 *PANDUAN BOT POKÉMON TCG v2*\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "🔍 *CEK HARGA*\n"
        "• Ketik nama kartu → pilih dari beberapa hasil\n"
        "• Kirim foto kartu → AI scan & cek harga\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "📦 *INVENTORY*\n"
        "• `/add Charizard` → Tambah ke koleksi\n"
        "• `/inventory` → Lihat semua koleksi\n"
        "• `/refresh` → Update harga semua kartu\n"
        "• `/delete 1` → Hapus kartu nomor 1\n"
        "• `/setcondition 1 Mint` → Set kondisi kartu\n"
        "• `/setgrade 1 PSA 10` → Set grade PSA/BGS\n"
        "• `/export` → Download CSV\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "🆕 *FITUR BARU*\n"
        "• `/scanset Base Set` → Semua kartu di set\n"
        "• `/portfoliochart` → Grafik distribusi nilai\n"
        "• `/findcheap Pikachu` → Versi termurah Pikachu\n"
        "• `/duplikat` → Kartu dobel di inventory\n"
        "• `/nilai` → Nilai real berdasar kondisi kartu\n"
        "• `/lang id` atau `/lang en` → Ganti bahasa\n"
        "• `/backup` → Backup semua data jadi JSON\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "⚖️ *COMPARE & RANKING*\n"
        "• `/compare Pikachu \\| Charizard` → Bandingkan\n"
        "• `/top10` → 10 kartu termahal di inventory\n"
        "• `/history Charizard` → Riwayat harga\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "🔔 *PRICE ALERT \\(2 ARAH\\)*\n"
        "• `/alert Charizard 50` → Notif harga ≤ \\$50 📉\n"
        "• `/alert Charizard 100 naik` → Notif harga ≥ \\$100 📈\n"
        "• `/alerts` → Lihat semua alert aktif\n"
        "• `/removealert 1` → Hapus alert nomor 1\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "⭐ *WISHLIST*\n"
        "• `/wish Mewtwo ex` → Tambah ke wishlist\n"
        "• `/wishlist` → Lihat wishlist\n"
        "• `/removewish 1` → Hapus dari wishlist\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "📊 *STATISTIK*\n"
        "• `/stats` → Statistik & ringkasan portfolio\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "🏷️ *KONDISI KARTU \\(Multiplier Harga\\)*\n"
        "Mint/Near Mint: 100% \\| Lightly Played: 80%\n"
        "Moderately Played: 65% \\| Heavily Played: 50%\n"
        "Damaged: 25%",
        parse_mode="MarkdownV2",
    )

# ── Helper: compress image ────────────────────────────────────────────────────
async def compress_image(photo_bytes: bytearray) -> bytes:
    image = Image.open(io.BytesIO(photo_bytes))
    image.thumbnail((1024, 1024))
    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=85)
    return buf.getvalue()

# ── Helper: parse card names dari AI ─────────────────────────────────────────
def parse_card_names(raw: str) -> list[str]:
    lines = raw.strip().splitlines()
    names = []
    seen  = set()
    for line in lines:
        cleaned = re.sub(r'^[\s\d\.\-\*•]+', '', line).strip()
        cleaned = re.sub(r'\s*\(.*?\)\s*$', '', cleaned).strip()
        if cleaned and cleaned.lower() not in seen:
            seen.add(cleaned.lower())
            names.append(cleaned)
    return names

# ── Handler foto (MULTI-CARD) ─────────────────────────────────────────────────
async def handle_photo_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    status = await update.message.reply_text(
        "🤖 Foto diterima\\! Sedang dianalisis AI\\.\\.\\.",
        parse_mode="MarkdownV2"
    )
    try:
        photo_file  = await update.message.photo[-1].get_file()
        photo_bytes = await photo_file.download_as_bytearray()
        compressed  = await compress_image(photo_bytes)
        image_b64   = base64.b64encode(compressed).decode("utf-8")

        response = await openrouter_client.chat.completions.create(
            model=OPENROUTER_MODEL,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"},
                    },
                    {
                        "type": "text",
                        "text": (
                            "Identifikasi SEMUA kartu Pokémon yang terlihat dalam foto ini. "
                            "Untuk setiap kartu, tulis nama karakter dan variannya saja "
                            "(contoh: Mewtwo ex, Pikachu ex, Charizard VMAX). "
                            "Jawab dalam format list, satu kartu per baris, tanpa penomoran, "
                            "tanpa bullet, tanpa nomor set atau angka lain. "
                            "Jika hanya ada satu kartu, tulis satu baris saja."
                        ),
                    },
                ],
            }],
            max_tokens=1024,
        )

        raw_text   = response.choices[0].message.content if response.choices else None
        if not raw_text:
            await status.edit_text("❌ AI gagal membaca gambar\\. Coba foto lebih jelas ya Bre\\!", parse_mode="MarkdownV2")
            return

        card_names = parse_card_names(raw_text)
        if not card_names:
            await status.edit_text("❌ Tidak ada kartu Pokémon terdeteksi\\. Coba foto lebih jelas\\!", parse_mode="MarkdownV2")
            return

        count = len(card_names)
        if count == 1:
            await status.edit_text(
                f"🔍 AI mendeteksi: *{esc(card_names[0])}*\nSedang cari harga\\.\\.\\.",
                parse_mode="MarkdownV2",
            )
        else:
            names_preview = "\n".join(f"• {esc(n)}" for n in card_names)
            await status.edit_text(
                f"🃏 AI mendeteksi *{count} kartu*:\n{names_preview}\n\nSedang cek semua harga\\.\\.\\.",
                parse_mode="MarkdownV2",
            )

        tasks   = [search_pokemon_card(name) for name in card_names]
        results = await asyncio.gather(*tasks)

        found_count = 0
        for name, card in zip(card_names, results):
            if card is None:
                await update.message.reply_text(f"❌ *{esc(name)}* tidak ditemukan\\.", parse_mode="MarkdownV2")
                continue
            if isinstance(card, dict) and card.get("error"):
                err = card["error"]
                msg = "⚠️ Rate limit\\!" if err == "rate_limit" else "⏱️ Timeout\\!" if err == "timeout" else "🔧 API down, coba lagi\\!" if err == "api_down" else "❌ Gagal fetch data\\!"
                await update.message.reply_text(f"{msg} \\({esc(name)}\\)", parse_mode="MarkdownV2")
                continue
            label = "Scan Foto" if count == 1 else f"Scan {found_count + 1}/{count}"
            await send_card(update, card, label=label)
            found_count += 1

        if count > 1 and found_count > 0:
            valid     = [r for r in results if r and not (isinstance(r, dict) and r.get("error"))]
            total_usd = sum(r.get("price_usd", 0) for r in valid)
            total_idr = total_usd * EXCHANGE_RATE
            await update.message.reply_text(
                f"📊 *Ringkasan:* {found_count}/{count} kartu ditemukan\n"
                f"💵 Total: \\${total_usd:.2f} \\| Rp {total_idr:,.0f}",
                parse_mode="MarkdownV2",
            )

    except Exception as e:
        logger.error(f"Error processing photo: {e}")
        await update.message.reply_text("❌ Gagal memproses foto, Bre\\. Coba lagi\\!", parse_mode="MarkdownV2")

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

    msg   = card_message(card)
    plain = (
        f"✨ {card['name']}\nSet: {card['set']}\nRarity: {card['rarity']}\n"
        f"Harga: ${card['price_usd']:.2f} | Rp {card['price_idr']:,.0f}\n"
        f"Mau simpan? Ketik: /add {card['name']}"
    )
    try:
        if card.get("image"):
            await query.message.reply_photo(photo=card["image"], caption=msg, parse_mode="MarkdownV2")
        else:
            await query.message.reply_text(msg, parse_mode="MarkdownV2")
    except Exception as e:
        logger.error(f"MarkdownV2 error, trying plain fallback: {e}")
        try:
            if card.get("image"):
                await query.message.reply_photo(photo=card["image"], caption=plain)
            else:
                await query.message.reply_text(plain)
        except Exception as e2:
            logger.error(f"Photo fallback also failed: {e2}")
            await query.message.reply_text(plain)

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
    logger.info("Price alert checker dijadwalkan setiap 6 jam.")

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
    app.add_handler(CommandHandler("wish",          add_wishlist))
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

    app.add_handler(CallbackQueryHandler(handle_card_select, pattern=r"^card_select:"))
    app.add_handler(MessageHandler(filters.PHOTO,                   handle_photo_search))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_card_search))

    logger.info("Bot Pokémon Vision & Portfolio v2 aktif!")
    app.run_polling()

if __name__ == "__main__":
    main()
