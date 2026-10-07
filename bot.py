import asyncio
import csv
import io
import logging
import os
import re

import aiosqlite
import httpx
from dotenv import load_dotenv
from pathlib import Path
from google import genai
from google.genai import types
from PIL import Image
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv(dotenv_path=Path(__file__).parent / ".env", override=True)

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
EXCHANGE_RATE  = int(os.getenv("EXCHANGE_RATE", 16000))
DB_PATH        = os.getenv("DB_PATH", "pokemon_inventory.db")

if not GEMINI_API_KEY or not TELEGRAM_TOKEN:
    raise RuntimeError("GEMINI_API_KEY dan TELEGRAM_TOKEN wajib diisi di .env!")

# ── Gemini ────────────────────────────────────────────────────────────────────
gemini = genai.Client(api_key=GEMINI_API_KEY)

# ── MarkdownV2 escape ─────────────────────────────────────────────────────────
def esc(text: str) -> str:
    """Escape semua karakter spesial MarkdownV2 Telegram."""
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
                price_idr REAL    DEFAULT 0.0
            )
        """)
        await db.commit()

# ── Pokemon TCG API ───────────────────────────────────────────────────────────
async def search_pokemon_card(card_name: str) -> dict | None:
    clean  = card_name.strip()
    term   = clean.split()[0] if clean.split() else clean
    url    = f"https://api.pokemontcg.io/v2/cards?q=name:*{term}*&pageSize=20"

    try:
        async with httpx.AsyncClient(timeout=15) as http:
            r = await http.get(url)
            if r.status_code == 429:
                logger.warning("Rate limit dari pokemontcg.io!")
                return {"error": "rate_limit"}
            r.raise_for_status()
            cards_list = r.json().get("data")

        if not cards_list:
            return None

        # Pilih kartu paling relevan
        selected   = cards_list[0]
        clean_low  = clean.lower()
        for card in cards_list:
            title = card.get("name", "").lower()
            if "ex" in clean_low and "ex" in title:
                selected = card
                break
            if " v" in clean_low and any(x in title for x in ["vmax", "vstar", " v"]):
                selected = card
                break

        # Ambil harga market TCGPlayer
        market_usd = 0.0
        prices     = selected.get("tcgplayer", {}).get("prices", {})
        for ptype in ["holofoil", "normal", "reverseHolofoil", "1stEditionHolofoil", "unlimitedHolofoil"]:
            if ptype in prices:
                val = prices[ptype].get("market") or 0.0
                if val > 0:
                    market_usd = val
                    break
        if market_usd == 0.0:
            for ptype in prices.values():
                if isinstance(ptype, dict) and ptype.get("market"):
                    market_usd = ptype["market"]
                    break

        return {
            "name":      selected.get("name", "Unknown"),
            "set":       selected.get("set", {}).get("name", "Unknown"),
            "rarity":    selected.get("rarity", "Common/Unspecified"),
            "price_usd": market_usd,
            "price_idr": market_usd * EXCHANGE_RATE,
            "image":     selected.get("images", {}).get("large"),
        }

    except httpx.TimeoutException:
        logger.error(f"Timeout saat fetch kartu '{card_name}'")
        return {"error": "timeout"}
    except Exception as e:
        logger.error(f"Error fetch kartu '{card_name}': {e}")
        return {"error": str(e)}

# ── Format pesan kartu ────────────────────────────────────────────────────────
def card_message(card: dict, label: str = "") -> str:
    suffix     = f" \\({label}\\)" if label else ""
    price_usd  = f"\\${card['price_usd']:.2f}" if card["price_usd"] > 0 else "Tidak tersedia"
    price_idr  = f"Rp {card['price_idr']:,.0f}" if card["price_idr"] > 0 else "Tidak tersedia"
    name_safe  = esc(card["name"])
    set_safe   = esc(card["set"])
    rar_safe   = esc(card["rarity"])

    return (
        f"✨ *{name_safe}*{suffix} ✨\n"
        f"📦 Set: {set_safe}\n"
        f"⭐ Rarity: {rar_safe}\n\n"
        f"💰 *Estimasi Harga:*\n"
        f"• Internasional: {price_usd}\n"
        f"• Pasaran Lokal \\(IDR\\): {price_idr}\n\n"
        f"💡 _Mau simpan? Ketik: /add {name_safe}_"
    )

async def send_card(update: Update, card: dict, label: str = "") -> None:
    msg = card_message(card, label)
    try:
        if card.get("image"):
            await update.message.reply_photo(
                photo=card["image"], caption=msg, parse_mode="MarkdownV2"
            )
        else:
            await update.message.reply_text(msg, parse_mode="MarkdownV2")
    except Exception as e:
        logger.error(f"MarkdownV2 error, fallback plain text: {e}")
        plain = (
            f"✨ {card['name']} ✨\n"
            f"Set: {card['set']}\n"
            f"Rarity: {card['rarity']}\n\n"
            f"Harga Internasional: ${card['price_usd']:.2f}\n"
            f"Harga IDR: Rp {card['price_idr']:,.0f}\n\n"
            f"Mau simpan? Ketik: /add {card['name']}"
        )
        if card.get("image"):
            await update.message.reply_photo(photo=card["image"], caption=plain)
        else:
            await update.message.reply_text(plain)

# ── /start ────────────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    name = esc(update.effective_user.first_name)
    await update.message.reply_text(
        f"Halo, {name}\\! ⚡\n"
        "Bot Pokémon TCG Portfolio & Vision Scanner aktif\\!\n\n"
        "📸 *Fitur Scan:* Kirim foto kartu → AI baca nama, cek harga otomatis\\!\n"
        "🃏 *Multi\\-Card Scan:* Foto banyak kartu sekaligus → semua harga keluar\\!\n\n"
        "📖 *Perintah:*\n"
        "• Ketik nama kartu → Cek harga\n"
        "• `/add \\[nama\\]` → Simpan ke inventory\n"
        "• `/inventory` → Lihat koleksi\n"
        "• `/delete \\[nomor\\]` → Hapus kartu\n"
        "• `/export` → Download inventory sebagai CSV",
        parse_mode="MarkdownV2",
    )

# ── Helper: compress image ────────────────────────────────────────────────────
async def compress_image(photo_bytes: bytearray) -> bytes:
    image = Image.open(io.BytesIO(photo_bytes))
    image.thumbnail((1024, 1024))
    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=85)
    return buf.getvalue()

# ── Helper: parse card names dari response Gemini ─────────────────────────────
def parse_card_names(raw: str) -> list[str]:
    """
    Bersihin dan pecah response Gemini jadi list nama kartu.
    Hapus bullet/numbering, baris kosong, dan duplikat.
    """
    lines = raw.strip().splitlines()
    names = []
    seen  = set()
    for line in lines:
        # Hapus prefix: "1.", "-", "•", "*", dll
        cleaned = re.sub(r'^[\s\d\.\-\*•]+', '', line).strip()
        # Hapus trailing keterangan dalam kurung jika ada, e.g. "(Detective)"
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
        # Download & compress
        photo_file  = await update.message.photo[-1].get_file()
        photo_bytes = await photo_file.download_as_bytearray()
        compressed  = await compress_image(photo_bytes)

        # Kirim ke Gemini Vision — minta deteksi SEMUA kartu
        response = gemini.models.generate_content(
            model="gemini-2.5-flash",
            contents=[
                types.Part.from_bytes(data=compressed, mime_type="image/jpeg"),
                (
                    "Identifikasi SEMUA kartu Pokémon yang terlihat dalam foto ini. "
                    "Untuk setiap kartu, tulis nama karakter dan variannya saja "
                    "(contoh: Mewtwo ex, Pikachu ex, Charizard VMAX). "
                    "Jawab dalam format list, satu kartu per baris, tanpa penomoran, "
                    "tanpa bullet, tanpa nomor set atau angka lain. "
                    "Jika hanya ada satu kartu, tulis satu baris saja."
                ),
            ],
            config=types.GenerateContentConfig(http_options={"timeout": 60000}),
        )

        if not response.text:
            await status.edit_text(
                "❌ AI gagal membaca gambar\\. Coba foto lebih jelas ya Bre\\!",
                parse_mode="MarkdownV2"
            )
            return

        card_names = parse_card_names(response.text)

        if not card_names:
            await status.edit_text(
                "❌ Tidak ada kartu Pokémon terdeteksi\\. Coba foto lebih jelas\\!",
                parse_mode="MarkdownV2"
            )
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

        # Fetch harga semua kartu secara concurrent
        tasks   = [search_pokemon_card(name) for name in card_names]
        results = await asyncio.gather(*tasks)

        found_count = 0
        for name, card in zip(card_names, results):
            if card is None:
                await update.message.reply_text(
                    f"❌ Kartu *{esc(name)}* tidak ditemukan di database\\.",
                    parse_mode="MarkdownV2"
                )
                continue
            if isinstance(card, dict) and card.get("error"):
                err = card["error"]
                if err == "rate_limit":
                    await update.message.reply_text(
                        f"⚠️ Rate limit saat cari *{esc(name)}*, coba lagi sebentar\\!",
                        parse_mode="MarkdownV2"
                    )
                elif err == "timeout":
                    await update.message.reply_text(
                        f"⏱️ Timeout saat cari *{esc(name)}*\\.",
                        parse_mode="MarkdownV2"
                    )
                else:
                    await update.message.reply_text(
                        f"❌ Error cari *{esc(name)}*: {esc(err)}",
                        parse_mode="MarkdownV2"
                    )
                continue

            label = "Scan Foto" if count == 1 else f"Scan Foto {found_count + 1}/{count}"
            await send_card(update, card, label=label)
            found_count += 1

        # Summary kalau multi-card
        if count > 1 and found_count > 0:
            valid_results = [
                r for r in results
                if r and not (isinstance(r, dict) and r.get("error"))
            ]
            total_usd = sum(r.get("price_usd", 0) for r in valid_results)
            total_idr = total_usd * EXCHANGE_RATE
            await update.message.reply_text(
                f"📊 *Ringkasan Scan:* {found_count}/{count} kartu ditemukan\n"
                f"💵 Total estimasi: \\${total_usd:.2f} \\| Rp {total_idr:,.0f}",
                parse_mode="MarkdownV2",
            )

    except Exception as e:
        logger.error(f"Error processing photo: {e}")
        await update.message.reply_text(
            "❌ Gagal memproses foto, Bre\\. Coba lagi\\!",
            parse_mode="MarkdownV2"
        )

# ── Handler teks ──────────────────────────────────────────────────────────────
async def handle_card_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.message.text.strip()
    await update.message.reply_text(f"🔍 Mencari kartu *{esc(query)}*\\.\\.\\.", parse_mode="MarkdownV2")

    card = await search_pokemon_card(query)

    if card is None:
        await update.message.reply_text(f"❌ Kartu '{esc(query)}' tidak ditemukan, Bre\\!", parse_mode="MarkdownV2")
        return
    if isinstance(card, dict) and card.get("error"):
        err = card["error"]
        if err == "rate_limit":
            await update.message.reply_text("⚠️ API rate limit Bre, tunggu sebentar lalu coba lagi\\!", parse_mode="MarkdownV2")
        elif err == "timeout":
            await update.message.reply_text("⏱️ Timeout\\! Coba lagi ya Bre\\.", parse_mode="MarkdownV2")
        else:
            await update.message.reply_text(f"❌ Error: {esc(err)}", parse_mode="MarkdownV2")
        return

    await send_card(update, card)

# ── /add ──────────────────────────────────────────────────────────────────────
async def add_inventory(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    card_query = " ".join(context.args).strip()
    if not card_query:
        await update.message.reply_text("⚠️ Format salah, Bre\\! Contoh: `/add Charizard`", parse_mode="MarkdownV2")
        return

    await update.message.reply_text(f"⏳ Memproses *{esc(card_query)}*\\.\\.\\.", parse_mode="MarkdownV2")

    card = await search_pokemon_card(card_query)
    if not card or (isinstance(card, dict) and card.get("error")):
        await update.message.reply_text(f"❌ Kartu '{esc(card_query)}' tidak ditemukan\\.", parse_mode="MarkdownV2")
        return

    user_id = update.effective_user.id
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO inventory (user_id, card_name, card_set, price_usd, price_idr) VALUES (?,?,?,?,?)",
            (user_id, card["name"], card["set"], card["price_usd"], card["price_idr"]),
        )
        await db.commit()

    price_str = f"\\${card['price_usd']:.2f}" if card["price_usd"] > 0 else "Tidak tersedia"
    idr_str   = f"Rp {card['price_idr']:,.0f}" if card["price_idr"] > 0 else "Tidak tersedia"

    await update.message.reply_text(
        f"✅ Berhasil ditambahkan ke Inventory\\!\n\n"
        f"📌 Kartu: *{esc(card['name'])}*\n"
        f"📦 Set: {esc(card['set'])}\n"
        f"💵 USD: {price_str}\n"
        f"🇮🇩 IDR: {idr_str}",
        parse_mode="MarkdownV2",
    )

# ── /inventory ────────────────────────────────────────────────────────────────
async def show_inventory(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, card_name, card_set, price_usd, price_idr FROM inventory WHERE user_id = ? ORDER BY id",
            (user_id,),
        ) as cur:
            items = await cur.fetchall()

    if not items:
        await update.message.reply_text(
            "📂 Inventory kamu kosong, Bre\\! Tambah dengan `/add \\[nama kartu\\]`\\.",
            parse_mode="MarkdownV2",
        )
        return

    total_usd = sum(row[3] for row in items)
    total_idr = sum(row[4] for row in items)

    lines = ["📦 *Portfolio Koleksi Kartu Pokémon:*\n"]
    for idx, (_, name, set_name, p_usd, p_idr) in enumerate(items, 1):
        usd_str = f"\\${p_usd:.2f}" if p_usd > 0 else "N/A"
        idr_str = f"Rp {p_idr:,.0f}" if p_idr > 0 else "N/A"
        lines.append(
            f"{idx}\\. *{esc(name)}* \\({esc(set_name)}\\)\n"
            f"   └ 💵 {usd_str} \\| {idr_str}\n"
        )

    lines.append(
        f"\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\\-\n"
        f"💰 *Total Portfolio:*\n"
        f"• USD: \\${total_usd:.2f}\n"
        f"• IDR: Rp {total_idr:,.0f}\n\n"
        f"🗑️ _Hapus: `/delete \\[nomor\\]`_\n"
        f"📥 _Export: `/export`_"
    )

    await update.message.reply_text("\n".join(lines), parse_mode="MarkdownV2")

# ── /delete ───────────────────────────────────────────────────────────────────
async def delete_inventory(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id

    if not context.args:
        await update.message.reply_text("⚠️ Format salah, Bre\\! Contoh: `/delete 1`", parse_mode="MarkdownV2")
        return

    try:
        target_idx = int(context.args[0])
    except ValueError:
        await update.message.reply_text("⚠️ Nomor urut harus berupa angka, Bre\\!", parse_mode="MarkdownV2")
        return

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT id FROM inventory WHERE user_id = ? ORDER BY id", (user_id,)) as cur:
            items = await cur.fetchall()

        if target_idx < 1 or target_idx > len(items):
            await update.message.reply_text("❌ Nomor urut tidak ditemukan di inventory kamu\\.", parse_mode="MarkdownV2")
            return

        db_id = items[target_idx - 1][0]
        await db.execute("DELETE FROM inventory WHERE id = ? AND user_id = ?", (db_id, user_id))
        await db.commit()

    await update.message.reply_text(
        f"🗑️ Kartu nomor {target_idx} berhasil dihapus dari inventory\\!",
        parse_mode="MarkdownV2",
    )

# ── /export ───────────────────────────────────────────────────────────────────
async def export_inventory(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id  = update.effective_user.id
    username = update.effective_user.first_name or "user"

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT card_name, card_set, price_usd, price_idr FROM inventory WHERE user_id = ? ORDER BY id",
            (user_id,),
        ) as cur:
            items = await cur.fetchall()

    if not items:
        await update.message.reply_text(
            "📂 Inventory kamu kosong, Bre\\! Belum ada yang bisa di\\-export\\.",
            parse_mode="MarkdownV2",
        )
        return

    # Build CSV in memory
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["No", "Card Name", "Set", "Price USD", "Price IDR"])
    for idx, (name, card_set, p_usd, p_idr) in enumerate(items, 1):
        writer.writerow([idx, name, card_set, f"{p_usd:.2f}", f"{p_idr:.0f}"])

    # Tambah baris total
    total_usd = sum(row[2] for row in items)
    total_idr = sum(row[3] for row in items)
    writer.writerow([])
    writer.writerow(["", "TOTAL", "", f"{total_usd:.2f}", f"{total_idr:.0f}"])

    csv_bytes = buf.getvalue().encode("utf-8-sig")  # utf-8-sig agar Excel bisa buka langsung
    filename  = f"pokemon_inventory_{username}.csv"

    await update.message.reply_document(
        document=io.BytesIO(csv_bytes),
        filename=filename,
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

def main() -> None:
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start",     start))
    app.add_handler(CommandHandler("add",       add_inventory))
    app.add_handler(CommandHandler("inventory", show_inventory))
    app.add_handler(CommandHandler("delete",    delete_inventory))
    app.add_handler(CommandHandler("export",    export_inventory))
    app.add_handler(MessageHandler(filters.PHOTO,                   handle_photo_search))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_card_search))

    logger.info("Bot Pokémon Vision & Portfolio aktif!")
    app.run_polling()

if __name__ == "__main__":
    main()
