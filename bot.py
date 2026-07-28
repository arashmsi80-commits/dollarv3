#!/usr/bin/env python3
"""
Telegram Bot for tracking USD/IRR and EUR/IRR exchange rates.
"""

import asyncio
import json
import logging
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

load_dotenv()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ALLOWED_CHAT_ID = os.getenv("ALLOWED_CHAT_ID")

BASE_DIR = Path(__file__).parent
DATA_FILE = BASE_DIR / "currency_data.json"
LOG_FILE = BASE_DIR / "currency_bot.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"), logging.StreamHandler()],
)
logger = logging.getLogger(__name__)

IRAN_TZ = "Asia/Tehran"
FETCH_INTERVAL_HOURS = 3


@dataclass
class CurrencyRate:
    source: str
    source_name: str
    currency: str
    price: int
    timestamp: str

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, data):
        return cls(**data)


@dataclass
class CurrencyData:
    rates: List[CurrencyRate]
    last_update: str

    def to_dict(self):
        return {"rates": [r.to_dict() for r in self.rates], "last_update": self.last_update}

    @classmethod
    def from_dict(cls, data):
        return cls(
            rates=[CurrencyRate.from_dict(r) for r in data.get("rates", [])],
            last_update=data.get("last_update", "")
        )

    def get_latest_for_currency(self, currency: str) -> List[CurrencyRate]:
        rates = [r for r in self.rates if r.currency == currency]
        rates.sort(key=lambda x: x.timestamp, reverse=True)
        return rates


class CurrencyScraper:
    def __init__(self):
        self.client = httpx.AsyncClient(
            timeout=30.0,
            headers={"User-Agent": "Mozilla/5.0"},
            follow_redirects=True
        )

    async def close(self):
        await self.client.aclose()

    @staticmethod
    def norm_price(text: str) -> Optional[int]:
        if not text:
            return None
        trans = str.maketrans(
            "\u06f0\u06f1\u06f2\u06f3\u06f4\u06f5\u06f6\u06f7\u06f8\u06f9"
            "\u0660\u0661\u0662\u0663\u0664\u0665\u0666\u0667\u0668\u0669",
            "0123456789" * 2
        )
        text = text.translate(trans).replace(",", "").replace("\u060c", "").strip()
        nums = re.findall(r"\d+", text)
        return int("".join(nums)) if nums else None

    async def fetch_tgju(self) -> List[CurrencyRate]:
        rates = []
        now = datetime.now().astimezone().isoformat()
        try:
            for cur, url, selectors in [
                ("usd", "https://www.tgju.org/profile/price_dollar_rl", [".price-value", ".price", ".text-left.price", "td.price", "span.price"]),
                ("eur", "https://www.tgju.org/profile/price_eur", [".price-value", ".price", ".text-left.price", "td.price", "span.price"])
            ]:
                resp = await self.client.get(url)
                soup = BeautifulSoup(resp.text, "lxml")
                price = None
                for sel in selectors:
                    elem = soup.select_one(sel)
                    if elem:
                        price = self.norm_price(elem.get_text())
                        if price and price > 10000:
                            break
                if price:
                    rates.append(CurrencyRate("tgju", "TGJU.org", cur, price, now))
        except Exception as e:
            logger.error(f"TGJU error: {e}")
        return rates

    async def fetch_alanchand(self) -> List[CurrencyRate]:
        rates = []
        now = datetime.now().astimezone().isoformat()
        try:
            for cur, url in [
                ("usd", "https://alanchand.com/currencies-price/usd"),
                ("eur", "https://alanchand.com/currencies-price/eur")
            ]:
                resp = await self.client.get(url)
                match = re.search(r'"price":"([0-9]+)"', resp.text)
                price = int(match.group(1)) if match else None
                if price and price > 10000:
                    rates.append(CurrencyRate("alanchand", "AlanChand.com", cur, price, now))
        except Exception as e:
            logger.error(f"AlanChand error: {e}")
        return rates

    async def fetch_all(self) -> List[CurrencyRate]:
        results = await asyncio.gather(
            self.fetch_tgju(),
            self.fetch_alanchand(),
            return_exceptions=True
        )
        all_rates = []
        for result in results:
            if isinstance(result, list):
                all_rates.extend(result)
            elif isinstance(result, Exception):
                logger.error(f"Fetch error: {result}")
        return all_rates


class CurrencyStorage:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> CurrencyData:
        if not self.path.exists():
            return CurrencyData([], "")
        try:
            return CurrencyData.from_dict(json.loads(self.path.read_text(encoding="utf-8")))
        except Exception as e:
            logger.error(f"Load error: {e}")
            return CurrencyData([], "")

    def save(self, data: CurrencyData):
        try:
            self.path.write_text(
                json.dumps(data.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8"
            )
        except Exception as e:
            logger.error(f"Save error: {e}")

    def add_rates(self, new_rates: List[CurrencyRate]):
        data = self.load()
        data.rates.extend(new_rates)
        data.last_update = datetime.now().astimezone().isoformat()
        if len(data.rates) > 1000:
            data.rates = data.rates[-1000:]
        self.save(data)


def fmt_price(p: int) -> str:
    return f"{p:,}".replace(",", "،")


def fmt_ts(ts: str) -> str:
    try:
        return datetime.fromisoformat(ts).strftime("%Y/%m/%d ساعت %H:%M:%S")
    except Exception:
        return ts


class CurrencyBot:
    def __init__(self):
        self.scraper = CurrencyScraper()
        self.storage = CurrencyStorage(DATA_FILE)
        self.scheduler = AsyncIOScheduler(timezone=IRAN_TZ)
        self.app: Optional[Application] = None

    def build(self):
        """ساخت اپلیکیشن و ثبت handlerها. Event loop نداره، فقط setup سینک."""
        if not TELEGRAM_BOT_TOKEN:
            raise ValueError("TELEGRAM_BOT_TOKEN not set")

        self.app = (
            Application.builder()
            .token(TELEGRAM_BOT_TOKEN)
            .post_init(self.on_post_init)
            .post_shutdown(self.on_post_shutdown)
            .build()
        )

        self.app.add_handler(CommandHandler("start", self.cmd_start))
        self.app.add_handler(CommandHandler("help", self.cmd_help))
        self.app.add_handler(CommandHandler("price", self.cmd_price))
        self.app.add_handler(CommandHandler("usd", self.cmd_usd))
        self.app.add_handler(CommandHandler("eur", self.cmd_eur))
        self.app.add_handler(CommandHandler("sources", self.cmd_sources))
        self.app.add_handler(CommandHandler("update", self.cmd_update))
        self.app.add_handler(CallbackQueryHandler(self.on_callback))

    async def on_post_init(self, application: Application):
        """
        این متد توسط خود python-telegram-bot، داخل event loop خودش،
        بعد از initialize صدا زده میشه. اینجا جای درستیه برای
        استارت کردن scheduler و گرفتن اولین نرخ‌ها.
        """
        self.scheduler.add_job(
            self.scheduled_update,
            IntervalTrigger(hours=FETCH_INTERVAL_HOURS),
            id="currency_update",
            replace_existing=True
        )
        self.scheduler.start()
        await self.scheduled_update()
        logger.info("Bot started successfully")

    async def on_post_shutdown(self, application: Application):
        """موقع خاموش شدن بات، هم داخل event loop خود PTB اجرا میشه."""
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
        await self.scraper.close()

    def _ok(self, update: Update) -> bool:
        return not ALLOWED_CHAT_ID or str(update.effective_chat.id) == ALLOWED_CHAT_ID

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._ok(update):
            return
        kb = [
            [InlineKeyboardButton("💵 دلار (USD)", callback_data="usd"), InlineKeyboardButton("💶 یورو (EUR)", callback_data="eur")],
            [InlineKeyboardButton("📊 همه", callback_data="all"), InlineKeyboardButton("🔄 به‌روزرسانی", callback_data="ref")],
            [InlineKeyboardButton("📋 مأخذ‌ها", callback_data="src")]
        ]
        await update.message.reply_text(
            "🤖 <b>ربات رصد نرخ ارز</b>\n\nهر ۳ ساعت نرخ دلار و یورو را از مأخذ‌های معتبر بررسی می‌کند.",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="HTML"
        )

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._ok(update):
            return
        await update.message.reply_text(
            "📖 <b>راهنما</b>\n\n/start منوی اصلی\n/price همه نرخ‌ها\n/usd دلار\n/eur یورو\n/sources مأخذ‌ها\n/update به‌روزرسانی",
            parse_mode="HTML"
        )

    async def cmd_price(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._ok(update):
            return
        await self._show(update.message, "all")

    async def cmd_usd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._ok(update):
            return
        await self._show(update.message, "usd")

    async def cmd_eur(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._ok(update):
            return
        await self._show(update.message, "eur")

    async def cmd_sources(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._ok(update):
            return
        await self._src(update.message)

    async def cmd_update(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._ok(update):
            return
        msg = await update.message.reply_text("🔄 در حال به‌روزرسانی...")
        await self.scheduled_update()
        await msg.edit_text("✅ به‌روزرسانی شد.")
        await self._show(update.message, "all")

    async def on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        if not self._ok(update):
            return

        data = query.data
        if data in ("usd", "eur"):
            await self._show(query.message, data, edit=True)
        elif data == "all":
            await self._show(query.message, "all", edit=True)
        elif data == "ref":
            await query.edit_message_text("🔄 در حال به‌روزرسانی...")
            await self.scheduled_update()
            await self._show(query.message, "all", edit=True)
        elif data == "src":
            await self._src(query.message, edit=True)

    async def _show(self, message, cur: str, edit: bool = False):
        data = self.storage.load()

        if cur == "all":
            usd = data.get_latest_for_currency("usd")
            eur = data.get_latest_for_currency("eur")

            txt = "📊 <b>نرخ‌های لحظه‌ای ارز</b>\n\n💵 <b>دلار (USD)</b>\n"
            if usd:
                for r in usd:
                    txt += f"  • {r.source_name}: <b>{fmt_price(r.price)}</b> ریال\n    ساعت {fmt_ts(r.timestamp)}\n"
            else:
                txt += "  داده‌ای نیست\n"

            txt += "\n💶 <b>یورو (EUR)</b>\n"
            if eur:
                for r in eur:
                    txt += f"  • {r.source_name}: <b>{fmt_price(r.price)}</b> ریال\n    ساعت {fmt_ts(r.timestamp)}\n"
            else:
                txt += "  داده‌ای نیست\n"

            txt += f"\nآخرین به‌روزرسانی: {fmt_ts(data.last_update) if data.last_update else 'هرگز'}"

            kb = [
                [InlineKeyboardButton("💵 دلار", callback_data="usd"), InlineKeyboardButton("💶 یورو", callback_data="eur")],
                [InlineKeyboardButton("🔄 به‌روزرسانی", callback_data="ref"), InlineKeyboardButton("📋 مأخذ‌ها", callback_data="src")]
            ]

        else:
            name = "دلار (USD)" if cur == "usd" else "یورو (EUR)"
            emoji = "💵" if cur == "usd" else "💶"
            rates = data.get_latest_for_currency(cur)

            txt = f"{emoji} <b>نرخ {name} از تمام مأخذ‌ها</b>\n\n"
            if rates:
                for r in rates:
                    txt += f"• <b>{r.source_name}</b>\n  💰 <b>{fmt_price(r.price)}</b> ریال\n  ساعت {fmt_ts(r.timestamp)}\n\n"
            else:
                txt += "داده‌ای موجود نیست."

            other = "eur" if cur == "usd" else "usd"
            o_emoji = "💶" if other == "eur" else "💵"
            o_name = "یورو (EUR)" if other == "eur" else "دلار (USD)"

            kb = [
                [InlineKeyboardButton(f"{o_emoji} {o_name}", callback_data=other), InlineKeyboardButton("📊 همه", callback_data="all")],
                [InlineKeyboardButton("🔄 به‌روزرسانی", callback_data="ref"), InlineKeyboardButton("📋 مأخذ‌ها", callback_data="src")]
            ]

        if edit:
            await message.edit_text(txt, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")
        else:
            await message.reply_text(txt, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")

    async def _src(self, message, edit: bool = False):
        txt = (
            "📋 <b>مأخذ‌های داده</b>\n\n"
            "ربات از ۲ سایت معتبر ایرانی نرخ ارز را واکشی می‌کند:\n\n"
            "1️⃣ <b>TGJU.org</b>\n   مرجع بورس، طلا و ارز ایران\n   🌐 https://www.tgju.org\n\n"
            "2️⃣ <b>AlanChand.com</b>\n   سایت مقایسه قیمت و ارز\n   🌐 https://www.alanchand.com\n\n"
            "⚠️ نرخ‌ها فقط برای اطلاع‌رسانی هستند."
        )
        kb = [[InlineKeyboardButton("🔙 بازگشت", callback_data="all")]]
        if edit:
            await message.edit_text(txt, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")
        else:
            await message.reply_text(txt, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")

    async def scheduled_update(self):
        logger.info("Fetching rates...")
        try:
            rates = await self.scraper.fetch_all()
            if rates:
                self.storage.add_rates(rates)
                logger.info(f"OK: {len(rates)} rates fetched")
            else:
                logger.warning("No rates fetched")
        except Exception as e:
            logger.error(f"Scheduled update failed: {e}")


def main():
    bot = CurrencyBot()
    bot.build()
    # نکته مهم: run_polling خودش event loop رو مدیریت می‌کنه (initialize,
    # start, polling, shutdown و بستن loop). به همین دلیل نباید await بشه
    # و نباید داخل asyncio.run() یا هر async def دیگه‌ای صدا زده بشه؛
    # وگرنه با تلاش برای بستن loopِ در حال اجرا تداخل پیدا می‌کنه
    # (همون خطای "Cannot close a running event loop").
    bot.app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
