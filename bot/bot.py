"""
Telegram bot that auto-creates product landing pages.

Flow (for a shop owner):
  1. /start -> the bot asks for the shop owner's WhatsApp number ONCE
     and saves it (per Telegram user id) in owners.json.
  2. /newproduct (or just send a photo) -> up to 4 photos, name,
     description, price, then optional extras (original price, stock,
     delivery charges) -> the bot builds a landing page and replies
     with its link.
  Shop name is asked once at /start (like the WhatsApp number) and
  shown on every page; /myshop views/changes it.
  3. A buyer opens the link in any browser, fills the order form and
     taps the WhatsApp button -> the order opens in WhatsApp addressed
     to the shop owner's saved number. Cash on Delivery only.

Pages are published in one of two modes (LINK_MODE in .env):
  - github: image + HTML are pushed to a GitHub repo with Pages enabled
  - local:  files are written under ./pages/ (for testing)

Deployment (GitHub Actions, free): the bot runs on a schedule with
`python bot.py --once` — it polls Telegram for ~50 seconds, processes
pending updates, then exits. State (owners, products, plans, conversation
state) lives in STATE_DIR and is restored from the Actions cache each run.
In that mode files are written to PAGES_DIR and the owner is sent links
under PUBLIC_BASE_URL. See README.md.

Environment variables (plain environment wins over .env — Actions has
no .env file):
  BOT_TOKEN, OWNER_ID, LINK_MODE, GITHUB_TOKEN, GITHUB_REPO,
  GITHUB_BRANCH, PAGES_BASE_URL, PUBLIC_BASE_URL, STATE_DIR, PAGES_DIR.

Cost: $0. No paid service is used anywhere.
Never log or print BOT_TOKEN or GITHUB_TOKEN.
"""

import asyncio
import base64
import html
import json
import logging
import os
import re
import secrets
import sys
import time
import unicodedata
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv
from telegram import ReplyKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.error import InvalidToken, NetworkError, TimedOut
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    PicklePersistence,
    filters,
)

load_dotenv()

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("landing-bot")

# ---------------------------------------------------------------- config ---

BASE_DIR = Path(__file__).resolve().parent


def _env_dir(name: str, default: Path) -> Path:
    """Directory from an environment variable.

    Relative values resolve against the current working directory, so in
    GitHub Actions (run from the repo root) STATE_DIR=bot-state and
    PAGES_DIR=shop land in the repo root. Locally (run from this folder)
    the defaults keep the old behaviour.
    """
    value = os.environ.get(name, "").strip()
    path = Path(value) if value else default
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve()


# State files (owners, products, plans, conversation state) live here.
STATE_DIR = _env_dir("STATE_DIR", BASE_DIR)
# Generated pages + images land here (local mode).
PAGES_DIR = _env_dir("PAGES_DIR", Path("pages"))
IMAGES_DIR = PAGES_DIR / "images"
TEMPLATE_FILE = BASE_DIR / "landing_template.html"
OWNERS_FILE = STATE_DIR / "owners.json"
PRODUCTS_FILE = STATE_DIR / "products.json"
PLANS_FILE = STATE_DIR / "plans.json"
OFFSET_FILE = STATE_DIR / "update_offset.json"
PERSISTENCE_FILE = STATE_DIR / "persistence.pickle"

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
OWNER_ID = os.environ.get("OWNER_ID", "").strip()  # bot owner's Telegram user id
LINK_MODE = os.environ.get("LINK_MODE", "local").strip().lower()
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "")          # "owner/repo"
GITHUB_BRANCH = os.environ.get("GITHUB_BRANCH", "main")
PAGES_BASE_URL = os.environ.get("PAGES_BASE_URL", "").rstrip("/")
# Public base URL for page links. In Actions the files are written locally
# (the workflow commits them) but the owner is sent links under this URL.
# In github mode it falls back to PAGES_BASE_URL when unset.
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")


def page_base_url() -> str:
    """Base URL for page links (empty in plain local mode -> file paths)."""
    return PUBLIC_BASE_URL or PAGES_BASE_URL


# How long one scheduled (--once) run polls Telegram.
ONCE_POLL_SECONDS = 50

# Subscription plans: price in BDT per month -> landing pages per calendar month.
# Free tier: each shop owner gets 5 pages FREE in total (lifetime, not per month).
PLANS = {"300": 30, "600": 80, "1000": 200}
FREE_LIMIT = 5
WAITING_NUMBER, WAITING_SHOP, PHOTO, MORE_PHOTOS, NAME, DESCRIPTION, PRICE, ORIG_PRICE, STOCK, CHARGE_DHAKA, CHARGE_OUTSIDE, CONFIRM, CHANGE_NUMBER, CHANGE_SHOP = range(14)

# Bengali digits -> ASCII digits (users often type prices in Bengali)
BN_DIGITS = str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789")


def to_ascii_digits(text: str) -> str:
    return text.strip().translate(BN_DIGITS)


# ------------------------------------------------------------- storage ----

def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        logger.warning("Could not read %s, starting fresh.", path.name)
        return default


def save_json(path: Path, data) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def get_owner(user_id: int):
    owners = load_json(OWNERS_FILE, {})
    return owners.get(str(user_id))


def save_owner(user_id: int, whatsapp: str) -> None:
    owners = load_json(OWNERS_FILE, {})
    record = owners.get(str(user_id), {})
    record["whatsapp"] = whatsapp
    record["updated"] = datetime.now().isoformat()
    owners[str(user_id)] = record
    save_json(OWNERS_FILE, owners)


def save_shop_name(user_id: int, shop_name: str) -> None:
    """Save the owner's shop name. Empty string = owner skipped it
    (template then falls back to 'অনলাইন শপ')."""
    owners = load_json(OWNERS_FILE, {})
    record = owners.get(str(user_id), {})
    record["shop_name"] = shop_name
    record["updated"] = datetime.now().isoformat()
    owners[str(user_id)] = record
    save_json(OWNERS_FILE, owners)


def get_shop_name(user_id: int):
    owner = get_owner(user_id)
    return (owner or {}).get("shop_name")


def add_product(user_id: int, record: dict) -> None:
    products = load_json(PRODUCTS_FILE, {})
    products.setdefault(str(user_id), []).append(record)
    save_json(PRODUCTS_FILE, products)


def list_products(user_id: int):
    return load_json(PRODUCTS_FILE, {}).get(str(user_id), [])


def load_offset():
    """Last processed Telegram update id, persisted so each scheduled
    (--once) run only fetches new updates."""
    data = load_json(OFFSET_FILE, {})
    offset = data.get("offset")
    return offset if isinstance(offset, int) else None


def save_offset(offset) -> None:
    if offset is None:
        return
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    save_json(OFFSET_FILE, {"offset": offset})


# ------------------------------------------------------- plans / quota ----

def current_month_key() -> str:
    return datetime.now().strftime("%Y-%m")


def get_plan_record(user_id: int):
    """Returns (record, all_plans). Record: plan, plan_month, month_used, free_used."""
    plans = load_json(PLANS_FILE, {})
    record = plans.get(str(user_id))
    if not record:
        record = {"plan": None, "plan_month": None, "month_used": 0, "free_used": 0}
    return record, plans


def save_plan_record(user_id: int, record: dict, plans: dict) -> None:
    plans[str(user_id)] = record
    save_json(PLANS_FILE, plans)


def quota_status(user_id: int):
    """Check whether the user may create another page.

    Returns (allowed, remaining, plan_label, record, plans).
    plan_label is "free" or the plan price ("300"/"600"/"1000").
    A plan is only valid in the calendar month it was activated for;
    when the month changes, usage resets and the plan lapses.
    """
    month = current_month_key()
    record, plans = get_plan_record(user_id)
    if record.get("plan_month") != month:
        record["month_used"] = 0
        record["plan"] = None
        record["plan_month"] = None
        save_plan_record(user_id, record, plans)
    plan = record.get("plan")
    if plan in PLANS:
        quota = PLANS[plan]
        return (record.get("month_used", 0) < quota,
                quota - record.get("month_used", 0), plan, record, plans)
    remaining_free = FREE_LIMIT - record.get("free_used", 0)
    return (remaining_free > 0, remaining_free, "free", record, plans)


def record_page_created(user_id: int) -> None:
    """Count one created page against the plan quota, or the free 5."""
    _allowed, _remaining, plan_label, record, plans = quota_status(user_id)
    if plan_label in PLANS:
        record["month_used"] = record.get("month_used", 0) + 1
    else:
        record["free_used"] = record.get("free_used", 0) + 1
    save_plan_record(user_id, record, plans)


def quota_exceeded_text(plan_label: str) -> str:
    if plan_label in PLANS:
        head = "এই মাসের প্ল্যানের সব পেজ ব্যবহার হয়ে গেছে।"
    else:
        head = "আপনার ফ্রি ৫টি ল্যান্ডিং পেজ শেষ হয়ে গেছে।"
    return (
        f"{head}\n\n"
        "আরও পেজ বানাতে মাসিক প্ল্যান নিন:\n"
        "• ৳৩০০/মাস — ৩০টি পেজ\n"
        "• ৳৬০০/মাস — ৮০টি পেজ\n"
        "• ৳১০০০/মাস — ২০০টি পেজ\n\n"
        "পেমেন্ট করুন bKash-এ, তারপর পেমেন্টের TrxID টি বটের মালিককে পাঠান। "
        "মালিক আপনার প্ল্যান চালু করে দিলে আবার পেজ বানাতে পারবেন।"
    )


# ---------------------------------------------------------- validation ----

def valid_bd_number(text: str):
    """Bangladesh mobile: 11 digits, starts with 01. Returns cleaned number or None."""
    cleaned = re.sub(r"[\s\-]", "", to_ascii_digits(text))
    if re.fullmatch(r"01[3-9]\d{8}", cleaned):
        return cleaned
    return None


def to_wa_number(bd_number: str) -> str:
    """01XXXXXXXXX -> 8801XXXXXXXXX (wa.me needs the country code, no +)."""
    return "880" + bd_number[1:]


def parse_price(text: str):
    """Price in taka. Accepts Bengali digits, commas, ৳. Returns float or None."""
    cleaned = to_ascii_digits(text).replace(",", "").replace("৳", "").replace("টাকা", "").strip()
    try:
        value = float(cleaned)
    except ValueError:
        return None
    if value <= 0 or value > 10_000_000:
        return None
    return value


def format_price(value: float) -> str:
    if value == int(value):
        return f"{int(value):,}"
    return f"{value:,.2f}"


SKIP_WORDS = {"নেই", "নাই", "না", "skip", "নেই।"}


def is_skip(text: str) -> bool:
    """Owner typed 'নেই' (or similar) — this optional value is skipped."""
    return (text or "").strip().lower() in SKIP_WORDS


def parse_stock(text: str):
    """Stock count. Bengali digits accepted. Returns int or None."""
    cleaned = to_ascii_digits(text).replace(",", "").strip()
    try:
        value = int(cleaned)
    except ValueError:
        return None
    if value <= 0 or value > 100_000:
        return None
    return value


def parse_charge(text: str):
    """Delivery charge in taka. Returns a float >= 0, or None if the
    text is not a number. 0 is valid (free delivery) — callers store
    it as None so the page hides the charge line."""
    cleaned = to_ascii_digits(text).replace(",", "").replace("৳", "").replace("টাকা", "").strip()
    try:
        value = float(cleaned)
    except ValueError:
        return None
    if value < 0 or value > 100_000:
        return None
    return value


def slugify(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")
    return slug or "product"


# ------------------------------------------------------------- GitHub ------

def github_upload_file(path_in_repo: str, content: bytes, message: str) -> None:
    """Create (or overwrite) one file in the GitHub repo via the Contents API."""
    if not GITHUB_TOKEN or not GITHUB_REPO:
        raise RuntimeError("GitHub mode needs GITHUB_TOKEN and GITHUB_REPO in .env")
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{path_in_repo}"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }
    payload = {
        "message": message,
        "content": base64.b64encode(content).decode("ascii"),
        "branch": GITHUB_BRANCH,
    }
    existing = requests.get(url, headers=headers, params={"ref": GITHUB_BRANCH}, timeout=30)
    if existing.status_code == 200:
        payload["sha"] = existing.json().get("sha")
    resp = requests.put(url, headers=headers, json=payload, timeout=60)
    resp.raise_for_status()


# ------------------------------------------------------- page building ----

def build_features_html(description: str) -> str:
    """Feature bullets derived ONLY from the owner's own description.

    Multi-line description: each line becomes a bullet.
    Single line: split into sentences (Bengali danda). One sentence only
    -> no features block (nothing is invented).
    """
    lines = [line.strip() for line in (description or "").split("\n") if line.strip()]
    if len(lines) > 1:
        bullets = lines[:5]
    else:
        sentences = [s.strip() for s in (description or "").replace("।", "।\n").split("\n")
                     if s.strip()]
        bullets = sentences[:4] if len(sentences) > 1 else []
    if not bullets:
        return ""
    items = "".join(f"<li>{html.escape(b)}</li>" for b in bullets)
    return f'<h2>এই পণ্যের বৈশিষ্ট্য</h2>\n<ul class="features">{items}</ul>'


def build_other_products_html(products: list) -> str:
    """'এই দোকানের আরও প্রোডাক্ট' section. Empty list -> "" (section hidden).

    Each record needs: title, price, image (URL or relative path) and a
    link — the public url when it starts with http, else the page file
    name (pages link to each other relatively in local mode).
    """
    cards = []
    for product in products:
        title = product.get("title", "")
        if not title:
            continue
        link = product.get("url", "") or ""
        if not link.startswith("http"):
            # Local mode stores an absolute file path as url — not a link.
            # Use the page file name (pages link to each other relatively).
            # No page name -> no usable link -> skip this product.
            link = product.get("page_name", "") or ""
        if not link:
            continue
        image = product.get("image", "")
        img_tag = (
            f'<img src="{html.escape(image, quote=True)}" alt="{html.escape(title)}" loading="lazy">'
            if image else ""
        )
        cards.append(
            f'<a class="other-card" href="{html.escape(link, quote=True)}">'
            f"{img_tag}"
            f'<div class="other-name">{html.escape(title)}</div>'
            f'<div class="other-price">৳{format_price(product.get("price", 0))}</div>'
            "</a>"
        )
    if not cards:
        return ""
    return (
        '<h2>এই দোকানের আরও প্রোডাক্ট</h2>\n'
        '<div class="other-grid">' + "".join(cards) + "</div>"
    )


def build_gallery_html(image_urls: list, title: str) -> str:
    """Thumbnail strip for the photo gallery (up to 4 photos).

    One photo (or none) -> "" — the single main image already shows it.
    More photos -> thumbnail buttons that swap the main image.
    """
    urls = [u for u in (image_urls or []) if u][:4]
    if len(urls) <= 1:
        return ""
    buttons = []
    for i, url in enumerate(urls):
        active = " active" if i == 0 else ""
        safe = html.escape(url, quote=True)
        buttons.append(
            f'<button type="button" class="thumb{active}" '
            f'onclick="setMainImage(\'{safe}\', this)">'
            f'<img src="{safe}" alt="">'
            "</button>"
        )
    return '<div class="thumbs">' + "".join(buttons) + "</div>"


def render_template(title: str, description: str, price: float,
                    image_url: str, wa_number: str,
                    other_products_html: str = "",
                    shop_name: str = None,
                    original_price: float = None,
                    stock: int = None,
                    image_urls: list = None,
                    charge_dhaka: float = None,
                    charge_outside: float = None) -> str:
    template = TEMPLATE_FILE.read_text(encoding="utf-8")
    safe_title = html.escape(title)
    safe_desc = html.escape(description).replace("\n", "<br>")
    page = template.replace("__PRODUCT_NAME__", safe_title)
    page = page.replace("__DESCRIPTION__", safe_desc)
    page = page.replace("__PRICE__", format_price(price))
    page = page.replace("__PRICE_RAW__", str(int(price)) if price == int(price) else str(price))
    page = page.replace("__IMAGE_URL__", html.escape(image_url, quote=True))
    page = page.replace("__WHATSAPP_NUMBER__", wa_number)
    page = page.replace("__FEATURES__", build_features_html(description))
    page = page.replace("__OTHER_PRODUCTS__", other_products_html)
    # Reviews: no review data is collected yet, so the section stays hidden.
    # Never invent reviews.
    page = page.replace("__REVIEWS__", "")
    # Optional extras — all default to "not collected yet", so pages built
    # before the bot collects these values keep working unchanged:
    # shop name (header + footer), original price (struck through),
    # real stock count, photo gallery, delivery charges.
    page = page.replace("__SHOP_NAME__",
                        html.escape(shop_name) if shop_name else "অনলাইন শপ")
    page = page.replace("__ORIGINAL_PRICE__",
                        f"৳{format_price(original_price)}" if original_price else "")
    page = page.replace("__STOCK__",
                        f"⚡ স্টক: {int(stock)}টি" if stock is not None else "⚡ স্টক সীমিত")
    page = page.replace("__DELIVERY_CHARGE_DHAKA__",
                        f"৳{format_price(charge_dhaka)}" if charge_dhaka else "")
    page = page.replace("__DELIVERY_CHARGE_OUTSIDE__",
                        f"৳{format_price(charge_outside)}" if charge_outside else "")
    page = page.replace("__IMAGE_URLS__",
                        build_gallery_html(image_urls or [image_url], title))
    return page


def publish_page(title: str, description: str, price: float,
                 photo_paths: list, image_filenames: list, wa_number: str,
                 shop_name=None, original_price=None, stock=None,
                 charge_dhaka=None, charge_outside=None) -> tuple:
    """Write/upload the images + HTML page.

    photo_paths / image_filenames hold up to 4 photos. Returns
    (page_url, html_filename, image_url, image_urls) — image_url is
    the first (main) photo, image_urls holds all photos.
    """
    shortid = secrets.token_hex(3)  # 6 hex chars, keeps URLs unique
    html_name = f"{slugify(title)}-{shortid}.html"
    PAGES_DIR.mkdir(parents=True, exist_ok=True)
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)

    if LINK_MODE == "github":
        base = page_base_url()
        if not base:
            raise RuntimeError("GitHub mode needs PAGES_BASE_URL (or PUBLIC_BASE_URL) in .env")
        image_urls = []
        for photo_path, image_filename in zip(photo_paths, image_filenames):
            image_bytes = photo_path.read_bytes()
            github_upload_file(f"pages/images/{image_filename}", image_bytes,
                               f"Add product image {image_filename}")
            image_urls.append(f"{base}/pages/images/{image_filename}")
        page_html = render_template(
            title, description, price, image_urls[0], wa_number,
            shop_name=shop_name, original_price=original_price, stock=stock,
            image_urls=image_urls, charge_dhaka=charge_dhaka,
            charge_outside=charge_outside,
        )
        github_upload_file(f"pages/{html_name}", page_html.encode("utf-8"),
                           f"Add landing page {html_name}")
        return f"{base}/pages/{html_name}", html_name, image_urls[0], image_urls

    # local mode (testing): write files under PAGES_DIR
    image_urls = []
    for photo_path, image_filename in zip(photo_paths, image_filenames):
        local_image = IMAGES_DIR / image_filename
        local_image.write_bytes(photo_path.read_bytes())
        image_urls.append(f"images/{image_filename}")
    page_html = render_template(
        title, description, price, image_urls[0], wa_number,
        shop_name=shop_name, original_price=original_price, stock=stock,
        image_urls=image_urls, charge_dhaka=charge_dhaka,
        charge_outside=charge_outside,
    )
    (PAGES_DIR / html_name).write_text(page_html, encoding="utf-8")
    if PUBLIC_BASE_URL:
        # Files are written locally (the workflow commits them), but the
        # owner gets the real public link.
        return f"{PUBLIC_BASE_URL}/{html_name}", html_name, image_urls[0], image_urls
    return str(PAGES_DIR / html_name), html_name, image_urls[0], image_urls


def refresh_owner_pages(user_id: int, wa_number: str) -> None:
    """Re-render ALL of one owner's pages so every page's
    'এই দোকানের আরও প্রোডাক্ট' section lists the owner's other products.

    Records created before this feature (no page_name/image stored) are
    left untouched.
    """
    PAGES_DIR.mkdir(parents=True, exist_ok=True)
    products = list_products(user_id)
    shop_name = get_shop_name(user_id)
    for product in products:
        page_name = product.get("page_name")
        image_url = product.get("image")
        if not page_name or not image_url:
            continue
        others = [p for p in products if p.get("url") != product.get("url")]
        image_urls = product.get("image_urls") or [image_url]
        page_html = render_template(
            product.get("title", ""),
            product.get("description", ""),
            product.get("price", 0),
            image_url,
            wa_number,
            other_products_html=build_other_products_html(others),
            shop_name=shop_name,
            original_price=product.get("original_price"),
            stock=product.get("stock"),
            image_urls=image_urls,
            charge_dhaka=product.get("charge_dhaka"),
            charge_outside=product.get("charge_outside"),
        )
        if LINK_MODE == "github":
            github_upload_file(f"pages/{page_name}", page_html.encode("utf-8"),
                               f"Update landing page {page_name}")
        else:
            (PAGES_DIR / page_name).write_text(page_html, encoding="utf-8")


# ------------------------------------------------------------ handlers ----

MENU_TEXT = (
    "কী করবেন? নিচের যেকোনো একটা লিখুন:\n\n"
    "/newproduct — নতুন প্রোডাক্টের ল্যান্ডিং পেজ বানান\n"
    "/myproducts — আপনার বানানো পেজগুলোর লিংক দেখুন\n"
    "/myplan — আপনার প্ল্যান ও বাকি পেজ দেখুন\n"
    "/mynumber — আপনার সংরক্ষিত WhatsApp নম্বর দেখুন\n"
    "/myshop — আপনার দোকানের নাম দেখুন/পরিবর্তন করুন\n"
    "/changenumber — WhatsApp নম্বর পরিবর্তন করুন\n"
    "/help — সাহায্য\n\n"
    "প্রথম ৫টি পেজ ফ্রি — তারপর মাসিক প্ল্যান (৳৩০০/৩০টি, ৳৬০০/৮০টি, ৳১০০০/২০০টি)।"
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    owner = get_owner(user_id)
    if owner:
        if "shop_name" not in owner:
            # Owner registered before the shop-name step existed.
            await update.message.reply_text(
                "আসসালামু আলাইকুম! আবার স্বাগতম।\n\n"
                "এবার আপনার দোকানের নামটি লিখুন (শুধু একবারই দিতে হবে)।\n"
                "এই নামটি আপনার প্রতিটি ল্যান্ডিং পেজে দেখা যাবে।\n\n"
                "যেমন: রহিম স্টোর\n"
                "(দোকানের নাম না থাকলে 'নেই' লিখুন)"
            )
            return WAITING_SHOP
        await update.message.reply_text(
            f"আসসালামু আলাইকুম! আবার স্বাগতম।\n"
            f"আপনার WhatsApp নম্বর সংরক্ষিত আছে: {owner['whatsapp']}\n\n{MENU_TEXT}"
        )
        return ConversationHandler.END
    await update.message.reply_text(
        "আসসালামু আলাইকুম! আমি ল্যান্ডিং পেজ বট।\n\n"
        "প্রথমে আপনার WhatsApp নম্বরটি দিন (শুধু একবারই দিতে হবে)।\n"
        "অর্ডারগুলো এই নম্বরেই আসবে।\n\n"
        "যেমন: 01712345678"
    )
    return WAITING_NUMBER


async def number_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    number = valid_bd_number(update.message.text or "")
    if not number:
        await update.message.reply_text(
            "নম্বরটি ঠিক হয়নি। ১১ সংখ্যার মোবাইল নম্বর দিন, যেমন: 01712345678"
        )
        return WAITING_NUMBER
    save_owner(update.effective_user.id, number)
    await update.message.reply_text(
        f"ধন্যবাদ! আপনার WhatsApp নম্বর সংরক্ষণ করা হয়েছে: {number}\n\n"
        "এবার আপনার দোকানের নামটি লিখুন (শুধু একবারই দিতে হবে)।\n"
        "এই নামটি আপনার প্রতিটি ল্যান্ডিং পেজে দেখা যাবে।\n\n"
        "যেমন: রহিম স্টোর\n"
        "(দোকানের নাম না থাকলে 'নেই' লিখুন)"
    )
    return WAITING_SHOP


async def shop_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Shop name collected once at /start (after the WhatsApp number)."""
    text = (update.message.text or "").strip()
    if not text:
        await update.message.reply_text("দোকানের নামটি লিখুন (অথবা 'নেই' লিখুন)।")
        return WAITING_SHOP
    shop_name = "" if is_skip(text) else text
    save_shop_name(update.effective_user.id, shop_name)
    if shop_name:
        line = f"আপনার দোকানের নাম সংরক্ষণ করা হয়েছে: {shop_name}\n\n"
    else:
        line = "ঠিক আছে, দোকানের নাম ছাড়াই চলবে।\n\n"
    await update.message.reply_text(
        line +
        "এখন থেকে প্রতিবার শুধু প্রোডাক্টের ছবি, নাম, বিবরণ আর দাম দিলেই হবে।\n\n"
        f"{MENU_TEXT}"
    )
    return ConversationHandler.END


async def my_shop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """View the saved shop name; send a new one to change it."""
    shop_name = get_shop_name(update.effective_user.id)
    current = (f"আপনার দোকানের নাম: {shop_name}"
               if shop_name else "দোকানের নাম এখনো দেওয়া হয়নি।")
    await update.message.reply_text(
        f"{current}\n\n"
        "পরিবর্তন করতে নতুন নামটি লিখে পাঠান।\n"
        "(বাতিল করতে /cancel লিখুন)"
    )
    return CHANGE_SHOP


async def change_shop_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()
    if not text:
        await update.message.reply_text("নতুন নামটি লিখুন।")
        return CHANGE_SHOP
    shop_name = "" if is_skip(text) else text
    save_shop_name(update.effective_user.id, shop_name)
    if shop_name:
        await update.message.reply_text(
            f"আপনার দোকানের নাম পরিবর্তন করা হয়েছে: {shop_name}\n"
            "নতুন পেজগুলোতে এই নামটি দেখা যাবে।"
        )
    else:
        await update.message.reply_text("দোকানের নাম মুছে দেওয়া হয়েছে।")
    return ConversationHandler.END


async def newproduct_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not get_owner(update.effective_user.id):
        await update.message.reply_text(
            "আগে /start দিয়ে আপনার WhatsApp নম্বরটি দিন। তারপর প্রোডাক্ট বানাতে পারবেন।"
        )
        return ConversationHandler.END
    allowed, _remaining, plan_label, _rec, _plans = quota_status(update.effective_user.id)
    if not allowed:
        await update.message.reply_text(quota_exceeded_text(plan_label))
        return ConversationHandler.END
    await update.message.reply_text(
        "নতুন প্রোডাক্টের ছবি পাঠান।\n\n(বাতিল করতে /cancel লিখুন)"
    )
    return PHOTO


async def photo_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not get_owner(update.effective_user.id):
        await update.message.reply_text(
            "আগে /start দিয়ে আপনার WhatsApp নম্বরটি দিন। তারপর প্রোডাক্ট বানাতে পারবেন।"
        )
        return ConversationHandler.END
    allowed, _remaining, plan_label, _rec, _plans = quota_status(update.effective_user.id)
    if not allowed:
        await update.message.reply_text(quota_exceeded_text(plan_label))
        return ConversationHandler.END
    photo = update.message.photo[-1]  # largest size
    context.user_data["photo_file_ids"] = [photo.file_id]
    keyboard = ReplyKeyboardMarkup([["শেষ"]], one_time_keyboard=True, resize_keyboard=True)
    await update.message.reply_text(
        "ছবি পেয়েছি (১টি)। আরও ছবি দিতে চান? সর্বোচ্চ ৪টি ছবি দেওয়া যাবে।\n"
        "আরও ছবি পাঠান, অথবা 'শেষ' লিখুন।",
        reply_markup=keyboard,
    )
    return MORE_PHOTOS


async def more_photo_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Extra photo in MORE_PHOTOS state (up to 4 in total)."""
    photo = update.message.photo[-1]  # largest size
    ids = context.user_data.setdefault("photo_file_ids", [])
    ids.append(photo.file_id)
    if len(ids) >= 4:
        await update.message.reply_text(
            "৪টি ছবি পেয়েছি। এবার প্রোডাক্টের নাম লিখুন।",
            reply_markup=ReplyKeyboardRemove(),
        )
        return NAME
    keyboard = ReplyKeyboardMarkup([["শেষ"]], one_time_keyboard=True, resize_keyboard=True)
    await update.message.reply_text(
        f"ছবি পেয়েছি ({len(ids)}টি)। আরও ছবি পাঠান, অথবা 'শেষ' লিখুন।",
        reply_markup=keyboard,
    )
    return MORE_PHOTOS


async def more_photos_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Owner typed 'শেষ' (or না/নেই) — move on to the product name."""
    text = (update.message.text or "").strip()
    if text not in ("শেষ", "শেষ।", "না", "নেই"):
        await update.message.reply_text("আরও ছবি পাঠান, অথবা 'শেষ' লিখুন।")
        return MORE_PHOTOS
    if not context.user_data.get("photo_file_ids"):
        await update.message.reply_text("প্রথমে প্রোডাক্টের ছবি পাঠান।")
        return MORE_PHOTOS
    await update.message.reply_text(
        "এবার প্রোডাক্টের নাম লিখুন।", reply_markup=ReplyKeyboardRemove()
    )
    return NAME


async def name_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    name = (update.message.text or "").strip()
    if not name:
        await update.message.reply_text("নামটি লিখুন।")
        return NAME
    context.user_data["product_name"] = name
    await update.message.reply_text("এবার প্রোডাক্টের বিবরণ লিখুন (২-৩ লাইন)।")
    return DESCRIPTION


async def description_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    description = (update.message.text or "").strip()
    if not description:
        await update.message.reply_text("বিবরণটি লিখুন।")
        return DESCRIPTION
    context.user_data["product_description"] = description
    await update.message.reply_text("এবার দাম লিখুন (শুধু সংখ্যা, টাকায়)। যেমন: ৫৫০")
    return PRICE


async def price_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    price = parse_price(update.message.text or "")
    if price is None:
        await update.message.reply_text(
            "দামটি ঠিক হয়নি। শুধু সংখ্যা লিখুন, যেমন: ৫৫০"
        )
        return PRICE
    context.user_data["product_price"] = price
    keyboard = ReplyKeyboardMarkup([["নেই"]], one_time_keyboard=True, resize_keyboard=True)
    await update.message.reply_text(
        "আগের দাম (অফার দাম) আছে কি? থাকলে লিখুন — পেজে আগের দাম কেটে দেখাবে।\n"
        "না থাকলে 'নেই' লিখুন। যেমন: ৮৫০",
        reply_markup=keyboard,
    )
    return ORIG_PRICE


async def orig_price_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()
    if is_skip(text):
        context.user_data["original_price"] = None
    else:
        original = parse_price(text)
        price = context.user_data.get("product_price")
        if original is None or (price and original <= price):
            await update.message.reply_text(
                "আগের দামটি ঠিক হয়নি — বিক্রির দামের চেয়ে বেশি হতে হবে।\n"
                "আবার লিখুন, অথবা 'নেই' লিখুন। যেমন: ৮৫০"
            )
            return ORIG_PRICE
        context.user_data["original_price"] = original
    keyboard = ReplyKeyboardMarkup([["নেই"]], one_time_keyboard=True, resize_keyboard=True)
    await update.message.reply_text(
        "স্টক কতটি আছে? সংখ্যা লিখুন।\nনা থাকলে 'নেই' লিখুন। যেমন: ২০",
        reply_markup=keyboard,
    )
    return STOCK


async def stock_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()
    if is_skip(text):
        context.user_data["stock"] = None
    else:
        stock = parse_stock(text)
        if stock is None:
            await update.message.reply_text(
                "স্টকটি ঠিক হয়নি। শুধু সংখ্যা লিখুন, অথবা 'নেই' লিখুন। যেমন: ২০"
            )
            return STOCK
        context.user_data["stock"] = stock
    keyboard = ReplyKeyboardMarkup([["নেই"]], one_time_keyboard=True, resize_keyboard=True)
    await update.message.reply_text(
        "ঢাকার ভিতরে ডেলিভারি চার্জ কত? সংখ্যা লিখুন।\n"
        "না থাকলে 'নেই' লিখুন। যেমন: ৬০",
        reply_markup=keyboard,
    )
    return CHARGE_DHAKA


async def charge_dhaka_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()
    if is_skip(text):
        context.user_data["charge_dhaka"] = None
    else:
        charge = parse_charge(text)
        if charge is None:
            await update.message.reply_text(
                "চার্জটি ঠিক হয়নি। শুধু সংখ্যা লিখুন, অথবা 'নেই' লিখুন। যেমন: ৬০"
            )
            return CHARGE_DHAKA
        context.user_data["charge_dhaka"] = charge if charge > 0 else None
    keyboard = ReplyKeyboardMarkup([["নেই"]], one_time_keyboard=True, resize_keyboard=True)
    await update.message.reply_text(
        "ঢাকার বাইরে ডেলিভারি চার্জ কত? সংখ্যা লিখুন।\n"
        "না থাকলে 'নেই' লিখুন। যেমন: ১২০",
        reply_markup=keyboard,
    )
    return CHARGE_OUTSIDE


async def charge_outside_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()
    if is_skip(text):
        context.user_data["charge_outside"] = None
    else:
        charge = parse_charge(text)
        if charge is None:
            await update.message.reply_text(
                "চার্জটি ঠিক হয়নি। শুধু সংখ্যা লিখুন, অথবা 'নেই' লিখুন। যেমন: ১২০"
            )
            return CHARGE_OUTSIDE
        context.user_data["charge_outside"] = charge if charge > 0 else None
    await show_summary(update, context)
    return CONFIRM


async def show_summary(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Summary before publishing, so the owner can check everything."""
    owner = get_owner(update.effective_user.id)
    data = context.user_data
    lines = [
        "সব ঠিক আছে কি? একবার দেখে নিন:\n",
        f"দোকান: {get_shop_name(update.effective_user.id) or '—'}",
        f"পণ্য: {data['product_name']}",
        f"বিবরণ: {data['product_description']}",
        f"দাম: ৳{format_price(data['product_price'])}",
    ]
    if data.get("original_price"):
        lines.append(f"আগের দাম: ৳{format_price(data['original_price'])}")
    if data.get("stock"):
        lines.append(f"স্টক: {data['stock']}টি")
    if data.get("charge_dhaka"):
        lines.append(f"ডেলিভারি চার্জ (ঢাকা): ৳{format_price(data['charge_dhaka'])}")
    if data.get("charge_outside"):
        lines.append(f"ডেলিভারি চার্জ (বাইরে): ৳{format_price(data['charge_outside'])}")
    lines.append(f"ছবি: {len(data.get('photo_file_ids', []))}টি")
    lines.append(f"WhatsApp: {owner['whatsapp']}")
    lines.append("\nঠিক থাকলে নিচের বোতামে চাপ দিন।")
    keyboard = ReplyKeyboardMarkup(
        [["✅ নিশ্চিত করুন", "❌ বাতিল"]],
        one_time_keyboard=True, resize_keyboard=True,
    )
    await update.message.reply_text("\n".join(lines), reply_markup=keyboard)


async def confirm_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()
    if text.startswith("❌"):
        context.user_data.clear()
        await update.message.reply_text("বাতিল করা হয়েছে।", reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END
    if not text.startswith("✅"):
        await update.message.reply_text("নিশ্চিত করতে ✅ নিশ্চিত করুন চাপ দিন, অথবা ❌ বাতিল।")
        return CONFIRM

    await update.message.reply_text("ল্যান্ডিং পেজ তৈরি হচ্ছে, একটু অপেক্ষা করুন...",
                                    reply_markup=ReplyKeyboardRemove())
    user_id = update.effective_user.id
    owner = get_owner(user_id)
    wa_number = to_wa_number(owner["whatsapp"])
    # Re-check the quota right before publishing (plan may have run out mid-flow).
    allowed, _remaining, plan_label, _rec, _plans = quota_status(user_id)
    if not allowed:
        await update.message.reply_text(quota_exceeded_text(plan_label))
        context.user_data.clear()
        return ConversationHandler.END
    tmp_photos = []
    try:
        photo_paths = []
        image_filenames = []
        for i, file_id in enumerate(context.user_data.get("photo_file_ids", [])):
            photo_file = await context.bot.get_file(file_id)
            tmp_photo = BASE_DIR / f".tmp-photo-{user_id}-{i}.jpg"
            await photo_file.download_to_drive(tmp_photo)
            tmp_photos.append(tmp_photo)
            photo_paths.append(tmp_photo)
            image_filenames.append(f"{secrets.token_hex(3)}.jpg")
        url, html_name, image_url, image_urls = publish_page(
            context.user_data["product_name"],
            context.user_data["product_description"],
            context.user_data["product_price"],
            photo_paths,
            image_filenames,
            wa_number,
            shop_name=get_shop_name(user_id),
            original_price=context.user_data.get("original_price"),
            stock=context.user_data.get("stock"),
            charge_dhaka=context.user_data.get("charge_dhaka"),
            charge_outside=context.user_data.get("charge_outside"),
        )
        for tmp_photo in tmp_photos:
            tmp_photo.unlink(missing_ok=True)
    except Exception as exc:  # noqa: BLE001 - report any publish failure to the user
        logger.exception("Failed to publish page for user %s", user_id)
        for tmp_photo in tmp_photos:
            tmp_photo.unlink(missing_ok=True)
        await update.message.reply_text(
            "দুঃখিত, পেজ তৈরি করা যায়নি। একটু পরে আবার চেষ্টা করুন।\n"
            f"কারণ: {exc}"
        )
        context.user_data.clear()
        return ConversationHandler.END

    add_product(user_id, {
        "title": context.user_data["product_name"],
        "description": context.user_data["product_description"],
        "price": context.user_data["product_price"],
        "url": url,
        "image": image_url,
        "image_urls": image_urls,
        "page_name": html_name,
        "shop_name": get_shop_name(user_id),
        "original_price": context.user_data.get("original_price"),
        "stock": context.user_data.get("stock"),
        "charge_dhaka": context.user_data.get("charge_dhaka"),
        "charge_outside": context.user_data.get("charge_outside"),
        "created": datetime.now().isoformat(),
    })
    record_page_created(user_id)
    # Refresh the owner's other pages so the new product appears in
    # their "এই দোকানের আরও প্রোডাক্ট" section. A refresh failure must
    # not hide the new page (it is already live), so log and continue.
    try:
        refresh_owner_pages(user_id, wa_number)
    except Exception:  # noqa: BLE001 - new page is live; old pages just lag
        logger.exception("Failed to refresh other pages for user %s", user_id)
    _allowed, remaining, plan_label, _rec, _plans = quota_status(user_id)
    if plan_label in PLANS:
        quota_line = f"\nএই মাসে আর {remaining}টি পেজ বানাতে পারবেন।"
    else:
        quota_line = f"\nফ্রি পেজ বাকি আছে: {remaining}টি।"
    context.user_data.clear()
    await update.message.reply_text(
        "✅ আপনার ল্যান্ডিং পেজ তৈরি হয়েছে!\n\n"
        f"লিংক: {url}\n\n"
        "এই লিংকটি ক্রেতাদের পাঠান — তারা যেকোনো ব্রাউজার (Chrome সহ) থেকে খুলে অর্ডার করতে পারবে।"
        f"{quota_line}"
    )
    return ConversationHandler.END


async def my_products(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    products = list_products(update.effective_user.id)
    if not products:
        await update.message.reply_text(
            "আপনি এখনো কোনো পেজ বানাননি। /newproduct দিয়ে শুরু করুন।"
        )
        return
    lines = ["আপনার ল্যান্ডিং পেজগুলো:\n"]
    for i, product in enumerate(products, start=1):
        lines.append(
            f"{i}. {product['title']} — ৳{format_price(product['price'])}\n"
            f"   {product['url']}"
        )
    await update.message.reply_text("\n\n".join(lines))


async def my_number(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    owner = get_owner(update.effective_user.id)
    if not owner:
        await update.message.reply_text(
            "আপনার নম্বর এখনো সংরক্ষিত হয়নি। /start দিয়ে নম্বর দিন।"
        )
        return
    await update.message.reply_text(
        f"আপনার সংরক্ষিত WhatsApp নম্বর: {owner['whatsapp']}\n\n"
        "পরিবর্তন করতে /changenumber লিখুন।"
    )


async def change_number_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "নতুন WhatsApp নম্বরটি লিখুন (১১ সংখ্যা, যেমন: 01712345678)।\n\n"
        "(বাতিল করতে /cancel লিখুন)"
    )
    return CHANGE_NUMBER


async def change_number_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    number = valid_bd_number(update.message.text or "")
    if not number:
        await update.message.reply_text(
            "নম্বরটি ঠিক হয়নি। ১১ সংখ্যার মোবাইল নম্বর দিন, যেমন: 01712345678"
        )
        return CHANGE_NUMBER
    save_owner(update.effective_user.id, number)
    await update.message.reply_text(
        f"আপনার WhatsApp নম্বর পরিবর্তন করা হয়েছে: {number}\n"
        "এখন থেকে নতুন অর্ডারগুলো এই নম্বরে আসবে।"
    )
    return ConversationHandler.END


async def my_plan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _allowed, remaining, plan_label, _rec, _plans = quota_status(update.effective_user.id)
    if plan_label in PLANS:
        await update.message.reply_text(
            f"আপনার প্ল্যান: ৳{plan_label}/মাস\n"
            f"এই মাসে বাকি আছে: {remaining}টি পেজ\n\n"
            "প্ল্যানের পেজ শেষ হলে আবার পেমেন্ট করে নবায়ন করতে হবে।"
        )
    else:
        await update.message.reply_text(
            "আপনার প্ল্যান: ফ্রি\n"
            f"ফ্রি পেজ বাকি আছে: {remaining}টি (মোট ৫টি)\n\n"
            "ফ্রি পেজ শেষ হলে মাসিক প্ল্যান নিন:\n"
            "• ৳৩০০/মাস — ৩০টি পেজ\n"
            "• ৳৬০০/মাস — ৮০টি পেজ\n"
            "• ৳১০০০/মাস — ২০০টি পেজ\n\n"
            "bKash-এ পেমেন্ট করে TrxID টি বটের মালিককে পাঠান।"
        )


async def activate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only: activate a monthly plan for a shop owner.

    Usage: /activate <telegram_user_id> <300|600|1000>
    The shop owner pays via bKash and sends the TrxID; the owner checks
    the payment and runs this command. No automatic bKash verification
    is built in (that needs a merchant account).
    """
    if not OWNER_ID or str(update.effective_user.id) != OWNER_ID:
        await update.message.reply_text("এই কমান্ডটি শুধু বটের মালিক ব্যবহার করতে পারবেন।")
        return
    args = context.args or []
    if len(args) != 2 or not args[0].isdigit() or args[1] not in PLANS:
        await update.message.reply_text(
            "ব্যবহার: /activate <Telegram user id> <300|600|1000>\n"
            "যেমন: /activate 123456789 300"
        )
        return
    target_id = int(args[0])
    plan = args[1]
    month = current_month_key()
    plans = load_json(PLANS_FILE, {})
    record = plans.get(str(target_id),
                       {"plan": None, "plan_month": None, "month_used": 0, "free_used": 0})
    record["plan"] = plan
    record["plan_month"] = month
    record["month_used"] = 0
    plans[str(target_id)] = record
    save_json(PLANS_FILE, plans)
    await update.message.reply_text(
        f"✅ চালু হয়েছে! User {target_id}-এর প্ল্যান: ৳{plan}/মাস "
        f"({PLANS[plan]}টি পেজ, {month} মাসের জন্য)。"
    )
    try:
        await context.bot.send_message(
            chat_id=target_id,
            text=(f"🎉 আপনার প্ল্যান চালু হয়েছে! ৳{plan}/মাস — "
                  f"এই মাসে {PLANS[plan]}টি ল্যান্ডিং পেজ বানাতে পারবেন।\n"
                  "/newproduct দিয়ে শুরু করুন।"),
        )
    except Exception:  # noqa: BLE001 - user may have blocked the bot; owner already confirmed
        logger.warning("Could not notify user %s about activation.", target_id)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "ল্যান্ডিং পেজ বট — সাহায্য\n\n"
        "/start — শুরু করুন / WhatsApp নম্বর দিন (শুধু একবার)\n"
        "/newproduct — নতুন প্রোডাক্টের পেজ বানান\n"
        "   (অথবা সরাসরি প্রোডাক্টের ছবি পাঠান)\n"
        "/myproducts — আপনার বানানো পেজগুলোর লিংক\n"
        "/myplan — আপনার প্ল্যান ও বাকি পেজ দেখুন\n"
        "/mynumber — সংরক্ষিত WhatsApp নম্বর দেখুন\n"
        "/myshop — দোকানের নাম দেখুন/পরিবর্তন করুন\n"
        "/changenumber — WhatsApp নম্বর পরিবর্তন করুন\n"
        "/cancel — চলমান কাজ বাতিল করুন\n\n"
        "দাম: প্রথম ৫টি পেজ ফ্রি। তারপর মাসিক প্ল্যান:\n"
        "• ৳৩০০/মাস — ৩০টি পেজ\n"
        "• ৳৬০০/মাস — ৮০টি পেজ\n"
        "• ৳১০০০/মাস — ২০০টি পেজ\n"
        "bKash-এ পেমেন্ট করে TrxID টি মালিককে পাঠান — মালিক প্ল্যান চালু করে দেবেন।\n\n"
        "ব্যবহার: নম্বর আর দোকানের নাম একবার দিন, তারপর প্রতিটি প্রোডাক্টের জন্য "
        "ছবি (সর্বোচ্চ ৪টি — আরও ছবি পাঠান, না চাইলে 'শেষ' লিখুন) + নাম + বিবরণ + দাম পাঠান। "
        "আগের দাম, স্টক ও ডেলিভারি চার্জ চাইলে দিতে পারবেন, না চাইলে 'নেই' লিখুন। "
        "বট লিংক দেবে। ক্রেতা সেই লিংক "
        "থেকে অর্ডার করলে অর্ডারটি আপনার WhatsApp-এ চলে যাবে।"
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text("বাতিল করা হয়েছে।", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


# ------------------------------------------------------------------ main ---

def build_application() -> Application:
    """Build the bot application with all handlers.

    Conversation state is persisted (PicklePersistence in STATE_DIR) so a
    shop owner mid-conversation survives restarts — including the short
    scheduled runs on GitHub Actions, where the bot only wakes up every
    few minutes.
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    persistence = PicklePersistence(filepath=str(PERSISTENCE_FILE), single_file=True)

    register_conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            WAITING_NUMBER: [MessageHandler(filters.TEXT & ~filters.COMMAND, number_received)],
            WAITING_SHOP: [MessageHandler(filters.TEXT & ~filters.COMMAND, shop_received)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        name="register",
        persistent=True,
    )

    product_conv = ConversationHandler(
        entry_points=[
            CommandHandler("newproduct", newproduct_start),
            MessageHandler(filters.PHOTO & ~filters.COMMAND, photo_received),
        ],
        states={
            PHOTO: [MessageHandler(filters.PHOTO & ~filters.COMMAND, photo_received)],
            MORE_PHOTOS: [
                MessageHandler(filters.PHOTO & ~filters.COMMAND, more_photo_received),
                MessageHandler(filters.TEXT & ~filters.COMMAND, more_photos_done),
            ],
            NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, name_received)],
            DESCRIPTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, description_received)],
            PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, price_received)],
            ORIG_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, orig_price_received)],
            STOCK: [MessageHandler(filters.TEXT & ~filters.COMMAND, stock_received)],
            CHARGE_DHAKA: [MessageHandler(filters.TEXT & ~filters.COMMAND, charge_dhaka_received)],
            CHARGE_OUTSIDE: [MessageHandler(filters.TEXT & ~filters.COMMAND, charge_outside_received)],
            CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, confirm_received)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        name="product",
        persistent=True,
    )

    change_conv = ConversationHandler(
        entry_points=[CommandHandler("changenumber", change_number_start)],
        states={CHANGE_NUMBER: [MessageHandler(filters.TEXT & ~filters.COMMAND, change_number_received)]},
        fallbacks=[CommandHandler("cancel", cancel)],
        name="change_number",
        persistent=True,
    )

    change_shop_conv = ConversationHandler(
        entry_points=[CommandHandler("myshop", my_shop)],
        states={CHANGE_SHOP: [MessageHandler(filters.TEXT & ~filters.COMMAND, change_shop_received)]},
        fallbacks=[CommandHandler("cancel", cancel)],
        name="change_shop",
        persistent=True,
    )

    app = Application.builder().token(BOT_TOKEN).persistence(persistence).build()
    app.add_handler(register_conv)
    app.add_handler(product_conv)
    app.add_handler(change_conv)
    app.add_handler(change_shop_conv)
    app.add_handler(CommandHandler("myproducts", my_products))
    app.add_handler(CommandHandler("myplan", my_plan))
    app.add_handler(CommandHandler("mynumber", my_number))
    app.add_handler(CommandHandler("activate", activate))
    app.add_handler(CommandHandler("help", help_command))
    return app


async def run_once() -> None:
    """Poll Telegram for ~50 seconds, process pending updates, then exit.

    Used by the scheduled GitHub Actions runs. The getUpdates offset is
    persisted in STATE_DIR, so each run only fetches new updates, and
    conversation state survives between runs via PicklePersistence.
    """
    app = build_application()
    await app.initialize()
    try:
        await app.start()
    except Exception:
        try:
            await app.shutdown()
        except Exception:
            pass
        raise
    offset = load_offset()
    deadline = time.monotonic() + ONCE_POLL_SECONDS
    try:
        while time.monotonic() < deadline:
            try:
                updates = await app.bot.get_updates(offset=offset, timeout=10)
            except InvalidToken:
                # Wrong/expired token: fail fast instead of retrying forever.
                raise
            except (NetworkError, TimedOut) as exc:
                logger.warning("Telegram poll failed (%s); retrying.", exc)
                await asyncio.sleep(2)
                continue
            for update in updates:
                offset = update.update_id + 1
                await app.process_update(update)
            if updates:
                save_offset(offset)
    finally:
        # Let in-flight handlers finish (e.g. a page being published)
        # before shutting down.
        try:
            await asyncio.wait_for(app.update_queue.join(), timeout=15)
        except (TimeoutError, asyncio.TimeoutError):
            logger.warning("Some updates were still processing; continuing shutdown.")
        try:
            await app.updater.stop()
        except Exception:
            pass
        try:
            await app.stop()
        except Exception:
            logger.exception("Error while stopping the bot.")
        try:
            await app.shutdown()
        except Exception:
            logger.exception("Error while shutting down the bot.")
    if offset is not None:
        save_offset(offset)
    logger.info("One-shot run finished.")


def main() -> None:
    once = "--once" in sys.argv[1:]
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN .env ফাইলে নেই। README.md দেখে token বসান।")
    if LINK_MODE == "github" and (not GITHUB_TOKEN or not GITHUB_REPO or not PAGES_BASE_URL):
        raise SystemExit(
            "LINK_MODE=github হলে GITHUB_TOKEN, GITHUB_REPO ও PAGES_BASE_URL লাগবে। "
            "README.md দেখুন।"
        )
    if once:
        logger.info("One-shot run: polling Telegram for ~%s seconds.", ONCE_POLL_SECONDS)
        asyncio.run(run_once())
        return

    app = build_application()
    logger.info("Bot started (LINK_MODE=%s).", LINK_MODE)
    app.run_polling()


if __name__ == "__main__":
    main()
