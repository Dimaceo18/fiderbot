# -*- coding: utf-8 -*-

import os
import re
import html
import time
import logging
import signal
import sys
import fcntl
import atexit
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from io import BytesIO
from typing import List, Dict, Optional, Tuple
from collections import defaultdict

import requests
import telebot
from telebot.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton
)
from telebot.apihelper import ApiTelegramException
from PIL import Image, ImageDraw, ImageFont, ImageEnhance
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    from openai import OpenAI
    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False
    OpenAI = None

# =========================
# Single instance lock
# =========================
lock_file = '/tmp/feeder_bot_instance.lock'

def check_single_instance():
    try:
        fd = open(lock_file, 'w')
        fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        def unlock():
            try:
                fcntl.lockf(fd, fcntl.LOCK_UN)
                fd.close()
                if os.path.exists(lock_file):
                    os.unlink(lock_file)
            except:
                pass
        atexit.register(unlock)
        return True
    except IOError:
        return False

if not check_single_instance():
    print("Another instance is already running. Exiting.")
    sys.exit(1)


# =========================
# Logging
# =========================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler('feeder_bot.log'), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)


# =========================
# ENV
# =========================
TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
DEEPSEEK_API_KEY = (os.getenv("DEEPSEEK_API_KEY") or "").strip()

if not TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")
if " " in TOKEN:
    raise ValueError("BOT_TOKEN must not contain spaces")
if not DEEPSEEK_API_KEY:
    logger.warning("DEEPSEEK_API_KEY is not set. AI adaptation will not work.")
if not OPENAI_AVAILABLE:
    logger.warning("openai package is not installed. AI adaptation will not work.")

MAX_FILE_SIZE = 20 * 1024 * 1024
FDR_POST_PURPLE_COLOR = (122, 58, 240)
FDR_STORY_W = 720
FDR_STORY_H = 1280
FONT_MAIN = "Inter-Black.ttf"
TARGET_W, TARGET_H = 750, 938
CHP_GRADIENT_PCT = 0.48

# =========================
# UI
# =========================
BTN_POST = "📝 Оформить пост"
BTN_STORY = "📱 Оформить сторис"
BTN_ENHANCE = "✨ Улучшить качество"

def main_menu_kb():
    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(KeyboardButton(BTN_POST), KeyboardButton(BTN_STORY))
    kb.row(KeyboardButton(BTN_ENHANCE))
    return kb


# =========================
# Bot + Session
# =========================
bot = telebot.TeleBot(TOKEN)

SESSION = requests.Session()
SESSION.mount("http://", HTTPAdapter(max_retries=Retry(total=0)))
SESSION.mount("https://", HTTPAdapter(max_retries=Retry(total=0)))
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
})

user_state: Dict[int, Dict] = {}
media_groups: Dict[str, Dict] = {}
media_group_lock = threading.Lock()

deepseek_client = None
if DEEPSEEK_API_KEY and OPENAI_AVAILABLE:
    deepseek_client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com")


# =========================
# AI (deepseek-v4-flash — актуальная модель)
# =========================
AI_NEWS_PROMPT = """Ты — редактор новостной ленты. Преобразуй присланный текст в новость в одну строку по правилам:

Объём — до 200 символов (включая пробелы и знаки препинания).
Формат — новостное сообщение СМИ: факт + ключевые детали (кто/что, где, когда, следствие/статус).
Стиль — нейтральный, информационный, без оценок, эмоций и воды.
Структура — одно предложение (допустимы точка с запятой и тире внутри).
Главное — вынеси в начало самое важное (что произошло), затем уточнения.
Не выдумывай факты, которых нет в тексте; числа, имена и даты — точно как в источнике.
Если в тексте есть чувствительная тема (ЧП, суицид, трагедия) — без лишних подробностей и с корректными формулировками.
Выведи только одну строку новости, без пояснений и заголовков."""


def adapt_news_with_ai(text: str) -> Optional[str]:
    if not deepseek_client:
        logger.error("DeepSeek client is not initialized")
        return None
    try:
        response = deepseek_client.chat.completions.create(
            model="deepseek-v4-flash",  # актуальная модель, deepseek-chat/flash устарели
            messages=[
                {"role": "system", "content": AI_NEWS_PROMPT},
                {"role": "user", "content": text}
            ],
            temperature=0.3,
            max_tokens=300,
            stream=False
        )
        msg = response.choices[0].message
        content = getattr(msg, "content", None)
        if not content:
            logger.error(f"DeepSeek returned empty content: {response}")
            return None
        result = content.strip().strip('"').strip("'").strip()
        if len(result) > 250:
            result = result[:247] + "..."
        logger.info(f"AI adapted: len={len(result)}")
        return result
    except Exception as e:
        logger.error(f"DeepSeek API error: {type(e).__name__}: {e}")
        return None


# =========================
# Graceful shutdown
# =========================
def signal_handler(sig, frame):
    logger.info("Shutting down gracefully...")
    try:
        bot.stop_polling()
    except:
        pass
    try:
        if os.path.exists(lock_file):
            os.unlink(lock_file)
    except:
        pass
    sys.exit(0)


signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


# =========================
# Helpers
# =========================
def check_file_size(b: bytes) -> bool:
    return len(b) <= MAX_FILE_SIZE


def ensure_fonts():
    if not os.path.exists(FONT_MAIN):
        raise RuntimeError(f"Font not found: {FONT_MAIN}")


def warn_if_too_small(chat_id, photo_bytes: bytes):
    try:
        im = Image.open(BytesIO(photo_bytes))
        if im.width < 900 or im.height < 1100:
            bot.send_message(chat_id, "⚠️ Фото маленького разрешения. Лучше от 1080×1350.")
    except:
        pass


def tg_file_bytes(file_id: str) -> bytes:
    file_info = bot.get_file(file_id)
    file_url = f"https://api.telegram.org/file/bot{TOKEN}/{file_info.file_path}"
    r = SESSION.get(file_url, timeout=30)
    r.raise_for_status()
    return r.content


def split_title_and_body(text: str) -> Tuple[str, str]:
    text = (text or "").strip()
    if not text:
        return "", ""
    parts = text.split("\n", 1)
    return parts[0].strip(), (parts[1].strip() if len(parts) > 1 else "")


def is_forwarded(message) -> bool:
    return bool(
        getattr(message, "forward_from_chat", None) or
        getattr(message, "forward_from", None) or
        getattr(message, "forward_origin", None)
    )


def get_message_text(message) -> str:
    if getattr(message, "caption", None):
        return message.caption.strip()
    if getattr(message, "text", None):
        return message.text.strip()
    fwd = getattr(message, "forward_origin", None)
    if fwd is not None:
        try:
            kwargs = getattr(fwd, "api_kwargs", None) or {}
            for key in ("text", "caption"):
                val = kwargs.get(key)
                if val and isinstance(val, str):
                    return val.strip()
        except:
            pass
    return ""


def get_photo_bytes(message) -> Optional[bytes]:
    try:
        if message.content_type == "photo" and message.photo:
            return tg_file_bytes(message.photo[-1].file_id)
        if message.content_type == "document" and message.document:
            mime = message.document.mime_type or ""
            if mime.startswith("image/"):
                return tg_file_bytes(message.document.file_id)
    except Exception as e:
        logger.error(f"get_photo_bytes error: {e}")
    return None


# =========================
# Image processing
# =========================
def enhance_image_simple(image_bytes: bytes) -> BytesIO:
    try:
        img = Image.open(BytesIO(image_bytes)).convert("RGB")
        img = ImageEnhance.Sharpness(img).enhance(1.20)
        img = ImageEnhance.Color(img).enhance(1.15)
        output = BytesIO()
        img.save(output, format="JPEG", quality=98, optimize=True)
        output.seek(0)
        return output
    except Exception as e:
        logger.error(f"Enhance error: {e}")
        out = BytesIO(image_bytes)
        out.seek(0)
        return out


def apply_bottom_gradient(img: Image.Image, height_pct: float, max_alpha: int = 220) -> Image.Image:
    w, h = img.size
    gh = int(h * height_pct)
    if gh <= 0:
        return img
    overlay_alpha = Image.new("L", (w, h), 0)
    grad = Image.new("L", (1, gh), 0)
    for y in range(gh):
        a = int(max_alpha * (y / max(1, gh - 1)))
        grad.putpixel((0, y), a)
    grad = grad.resize((w, gh))
    overlay_alpha.paste(grad, (0, h - gh))
    black = Image.new("RGBA", (w, h), (0, 0, 0, 255))
    base = img.convert("RGBA")
    overlay = Image.composite(black, Image.new("RGBA", (w, h), (0, 0, 0, 0)), overlay_alpha)
    return Image.alpha_composite(base, overlay).convert("RGB")


def text_width(draw, s, font):
    bb = draw.textbbox((0, 0), s, font=font)
    return bb[2] - bb[0]


def wrap_no_truncate(draw, text, font, max_width, max_lines=6):
    words = [w for w in (text or "").split() if w.strip()]
    if not words:
        return [""], True
    lines, cur, i = [], "", 0
    while i < len(words):
        w = words[i]
        test = (cur + " " + w).strip()
        if text_width(draw, test, font) <= max_width:
            cur = test
            i += 1
        else:
            if not cur:
                return [words[i]], False
            lines.append(cur)
            cur = ""
            if len(lines) >= max_lines:
                return lines, False
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        return lines[:max_lines], False
    return lines, True


def fit_text_block(draw, text, font_path, safe_w, max_block_h, max_lines=6,
                   start_size=90, min_size=16, line_spacing_ratio=0.22):
    text = (text or "").strip().upper() or " "
    size = start_size
    while size >= min_size:
        font = ImageFont.truetype(font_path, size)
        lines, ok = wrap_no_truncate(draw, text, font, safe_w, max_lines=max_lines)
        spacing = int(size * line_spacing_ratio)
        heights, total_h, max_w = [], 0, 0
        for ln in lines:
            bb = draw.textbbox((0, 0), ln, font=font)
            lw, lh = bb[2] - bb[0], bb[3] - bb[1]
            heights.append(lh)
            total_h += lh
            max_w = max(max_w, lw)
        total_h += spacing * (len(lines) - 1)
        if ok and max_w <= safe_w and total_h <= max_block_h:
            return font, lines, heights, spacing, total_h
        size -= 2
    font = ImageFont.truetype(font_path, min_size)
    lines, _ = wrap_no_truncate(draw, text, font, safe_w, max_lines=max_lines)
    spacing = int(min_size * line_spacing_ratio)
    heights, total_h = [], 0
    for ln in lines:
        bb = draw.textbbox((0, 0), ln, font=font)
        heights.append(bb[3] - bb[1])
        total_h += bb[3] - bb[1]
    total_h += spacing * (len(lines) - 1)
    return font, lines, heights, spacing, total_h


def crop_to_4x5(img: Image.Image) -> Image.Image:
    w, h = img.size
    target_ratio = 4 / 5
    cur_ratio = w / h
    if cur_ratio > target_ratio:
        new_w = int(h * target_ratio)
        left = (w - new_w) // 2
        return img.crop((left, 0, left + new_w, h))
    new_h = int(w / target_ratio)
    top = (h - new_h) // 2
    return img.crop((0, top, w, top + new_h))


def _wrap_paragraphs(draw, text, font, max_w):
    paragraphs = [p.strip() for p in (text or "").replace("\r", "\n").split("\n")]
    all_lines = []
    for p in paragraphs:
        if not p:
            if all_lines and all_lines[-1] != "":
                all_lines.append("")
            continue
        words = p.split()
        if not words:
            continue
        current = words[0]
        for word in words[1:]:
            test = current + " " + word
            bbox = draw.textbbox((0, 0), test, font=font)
            if (bbox[2] - bbox[0]) <= max_w:
                current = test
            else:
                all_lines.append(current)
                current = word
        all_lines.append(current)
        all_lines.append("")
    while all_lines and all_lines[-1] == "":
        all_lines.pop()
    return all_lines


def _fit_story_text(draw, text, box, min_size, max_size, line_gap_ratio=0.18, paragraph_gap_ratio=0.35):
    x1, y1, x2, y2 = box
    max_w, max_h = x2 - x1, y2 - y1
    sel_font = ImageFont.truetype(FONT_MAIN, min_size)
    sel_gap, sel_pgap = 8, 12
    for size in range(max_size, min_size - 1, -1):
        font = ImageFont.truetype(FONT_MAIN, size)
        lines = _wrap_paragraphs(draw, text, font, max_w)
        if not lines:
            continue
        line_h = font.getbbox("Ag")[3] - font.getbbox("Ag")[1]
        gap = max(4, int(line_h * line_gap_ratio))
        pgap = max(gap + 2, int(line_h * paragraph_gap_ratio))
        total_h, max_lw = 0, 0
        for line in lines:
            if line == "":
                total_h += pgap
                continue
            lw = font.getbbox(line)[2] - font.getbbox(line)[0]
            max_lw = max(max_lw, lw)
            total_h += line_h + gap
        if total_h <= max_h and max_lw <= max_w:
            sel_font, sel_gap, sel_pgap = font, gap, pgap
            break
    return sel_font, sel_gap, sel_pgap


def _draw_story_text(draw, text, box, font, fill=(255, 255, 255), align="center",
                     valign="center", line_gap=10, paragraph_gap_extra=10):
    x1, y1, x2, y2 = box
    max_w, max_h = x2 - x1, y2 - y1
    lines = _wrap_paragraphs(draw, text, font, max_w)
    if not lines:
        return
    line_h = font.getbbox("Ag")[3] - font.getbbox("Ag")[1]
    total_h = sum(paragraph_gap_extra if l == "" else line_h + line_gap for l in lines)
    y = y1 if valign == "top" else y1 + (max_h - total_h) // 2
    for line in lines:
        if line == "":
            y += paragraph_gap_extra
            continue
        lw = font.getbbox(line)[2] - font.getbbox(line)[0]
        if align == "center":
            x = x1 + (max_w - lw) // 2
        elif align == "left":
            x = x1
        else:
            x = x2 - lw
        draw.text((x, y), line, font=font, fill=fill)
        y += line_h + line_gap


# =========================
# Cards
# =========================
def make_fdr_post_card(photo_bytes: bytes, title_text: str, highlight_phrase: str) -> BytesIO:
    ensure_fonts()
    img = Image.open(BytesIO(photo_bytes)).convert("RGB")
    img = crop_to_4x5(img)
    img = img.resize((TARGET_W, TARGET_H), resample=Image.Resampling.LANCZOS)
    img = ImageEnhance.Brightness(img).enhance(0.85)
    img = apply_bottom_gradient(img, height_pct=CHP_GRADIENT_PCT, max_alpha=220)
    draw = ImageDraw.Draw(img)
    margin_x = int(img.width * 0.06)
    margin_bottom = int(img.height * 0.08)
    safe_w = img.width - 2 * margin_x
    title_upper = title_text.strip().upper()
    hl_upper = highlight_phrase.strip().upper()
    hl_words = set(hl_upper.split())
    title_max_h = int(img.height * 0.23)
    font, lines, heights, spacing, total_h = fit_text_block(
        draw, title_upper, FONT_MAIN, safe_w, title_max_h,
        max_lines=6, start_size=int(img.height * 0.11), min_size=16
    )
    base_y = img.height - margin_bottom - total_h
    y = base_y
    for i, line in enumerate(lines):
        words = line.split()
        cx = margin_x
        for w in words:
            bb = draw.textbbox((cx, y), w, font=font)
            if w in hl_words:
                pad = 10
                draw.rectangle([bb[0]-pad, bb[1]-pad, bb[2]+pad, bb[3]+pad], fill=FDR_POST_PURPLE_COLOR)
            if w != words[-1]:
                cx += text_width(draw, w, font) + text_width(draw, " ", font)
            else:
                cx += text_width(draw, w, font)
        y += heights[i] + spacing
    y = base_y
    for i, line in enumerate(lines):
        words = line.split()
        cx = margin_x
        for w in words:
            draw.text((cx, y), w, font=font, fill="white")
            if w != words[-1]:
                cx += text_width(draw, w, font) + text_width(draw, " ", font)
            else:
                cx += text_width(draw, w, font)
        y += heights[i] + spacing
    out = BytesIO()
    img.save(out, format="JPEG", quality=95, subsampling=0, optimize=True)
    out.seek(0)
    return out


def make_fdr_story_card(photo_bytes: bytes, title: str, body_text: str) -> BytesIO:
    ensure_fonts()
    canvas = Image.new("RGB", (FDR_STORY_W, FDR_STORY_H), (0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    photo_h, header_h = 410, 220
    photo = Image.open(BytesIO(photo_bytes)).convert("RGB")

    def fit_cover(im, tw, th):
        sw, sh = im.size
        scale = max(tw/sw, th/sh)
        nw, nh = int(sw*scale), int(sh*scale)
        r = im.resize((nw, nh), Image.LANCZOS)
        l, t = max(0, (nw-tw)//2), max(0, (nh-th)//2)
        return r.crop((l, t, l+tw, t+th))

    canvas.paste(fit_cover(photo, FDR_STORY_W, photo_h), (0, 0))
    canvas.paste(Image.new("RGB", (FDR_STORY_W, header_h), (122, 58, 240)), (0, photo_h))
    draw.rectangle([0, photo_h+header_h, FDR_STORY_W, FDR_STORY_H], fill=(0, 0, 0))
    pad = 34
    h_box = (pad, photo_h+pad, FDR_STORY_W-pad, photo_h+header_h-pad)
    b_box = (pad, photo_h+header_h+pad, FDR_STORY_W-pad, FDR_STORY_H-pad)
    tf, tg, tpg = _fit_story_text(draw, title, h_box, 28, 54, 0.08, 0.18)
    _draw_story_text(draw, title, h_box, tf, line_gap=tg, paragraph_gap_extra=tpg)
    bf, bg, bpg = _fit_story_text(draw, body_text, b_box, 14, 30, 0.10, 0.32)
    _draw_story_text(draw, body_text, b_box, bf, align="left", valign="top", line_gap=bg, paragraph_gap_extra=bpg)
    out = BytesIO()
    canvas.save(out, format="JPEG", quality=92, optimize=True)
    out.seek(0)
    return out


def make_news_story_card(photo_bytes: bytes, news_text: str, highlight_phrase: str) -> BytesIO:
    ensure_fonts()
    canvas = Image.new("RGB", (FDR_STORY_W, FDR_STORY_H), (255, 255, 255))
    draw = ImageDraw.Draw(canvas)
    photo = Image.open(BytesIO(photo_bytes)).convert("RGB")
    photo_size, photo_top = 640, 120

    def fit_cover(im, tw, th):
        sw, sh = im.size
        scale = max(tw/sw, th/sh)
        nw, nh = int(sw*scale), int(sh*scale)
        r = im.resize((nw, nh), Image.LANCZOS)
        l, t = max(0, (nw-tw)//2), max(0, (nh-th)//2)
        return r.crop((l, t, l+tw, t+th))

    canvas.paste(fit_cover(photo, photo_size, photo_size), ((FDR_STORY_W-photo_size)//2, photo_top))
    news_top = photo_top + photo_size + 60
    news_box = (50, news_top, FDR_STORY_W-50, FDR_STORY_H-80)
    nf, ng, npg = _fit_story_text(draw, news_text.upper(), news_box, 24, 52, 0.12, 0.30)
    hl_upper = highlight_phrase.strip().upper()
    hl_words = set(hl_upper.split()) if hl_upper else set()
    lines = _wrap_paragraphs(draw, news_text.upper(), nf, news_box[2]-news_box[0])
    lh = nf.getbbox("Ag")[3] - nf.getbbox("Ag")[1]
    total_h = sum(npg if l == "" else lh+ng for l in lines)
    y = news_box[1] + (news_box[3]-news_box[1]-total_h)//2
    for line in lines:
        if line == "":
            y += npg
            continue
        words = line.split()
        lw = sum(text_width(draw, w, nf) for w in words) + text_width(draw, " ", nf)*(len(words)-1)
        x = news_box[0] + (news_box[2]-news_box[0]-lw)//2
        for i, w in enumerate(words):
            color = FDR_POST_PURPLE_COLOR if w in hl_words else (0, 0, 0)
            draw.text((x, y), w, font=nf, fill=color)
            x += text_width(draw, w, nf)
            if i < len(words)-1:
                x += text_width(draw, " ", nf)
        y += lh + ng
    out = BytesIO()
    canvas.save(out, format="JPEG", quality=92, optimize=True)
    out.seek(0)
    return out


# =========================
# Keyboards
# =========================
def post_preview_kb():
    kb = InlineKeyboardMarkup()
    kb.row(InlineKeyboardButton("✏️ Изменить текст", callback_data="edit_post_body"),
           InlineKeyboardButton("✏️ Изменить заголовок", callback_data="edit_post_title"))
    kb.row(InlineKeyboardButton("✏️ Изменить плашку", callback_data="edit_post_highlight"),
           InlineKeyboardButton("🔄 Создать заново", callback_data="restart_post"))
    kb.row(InlineKeyboardButton("✅ Готово", callback_data="finish_post"),
           InlineKeyboardButton("❌ Отмена", callback_data="cancel_post"))
    return kb


def story_preview_kb():
    kb = InlineKeyboardMarkup()
    kb.row(InlineKeyboardButton("✏️ Изменить текст", callback_data="edit_story_body"),
           InlineKeyboardButton("✏️ Изменить заголовок", callback_data="edit_story_title"))
    kb.row(InlineKeyboardButton("🔄 Создать заново", callback_data="restart_story"),
           InlineKeyboardButton("✅ Готово", callback_data="finish_story"))
    kb.row(InlineKeyboardButton("❌ Отмена", callback_data="cancel_story"))
    return kb


def news_story_preview_kb():
    kb = InlineKeyboardMarkup()
    kb.row(InlineKeyboardButton("✏️ Изменить новость", callback_data="edit_news_text"),
           InlineKeyboardButton("💜 Изменить выделение", callback_data="edit_news_highlight"))
    kb.row(InlineKeyboardButton("🔄 Создать заново", callback_data="restart_news_story"),
           InlineKeyboardButton("✅ Готово", callback_data="finish_news_story"))
    kb.row(InlineKeyboardButton("❌ Отмена", callback_data="cancel_news_story"))
    return kb


def ai_choice_kb():
    kb = InlineKeyboardMarkup()
    kb.row(InlineKeyboardButton("📄 Оставить оригинальный", callback_data="ai_keep_original"),
           InlineKeyboardButton("🤖 Адаптировать через ИИ", callback_data="ai_adapt"))
    return kb


def format_choice_kb():
    kb = InlineKeyboardMarkup()
    kb.row(InlineKeyboardButton("📝 Пост", callback_data="format_post"),
           InlineKeyboardButton("📱 Сторис", callback_data="format_story"))
    return kb


# =========================
# Health check
# =========================
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/html; charset=utf-8')
        self.end_headers()
        self.wfile.write("Feeder Bot запущен! 🤖".encode('utf-8'))
    def log_message(self, format, *args):
        return


def run_http_server():
    try:
        port = int(os.environ.get('PORT', 10000))
        httpd = HTTPServer(('0.0.0.0', port), HealthCheckHandler)
        logger.info(f"🌐 Health check on port {port}")
        httpd.serve_forever()
    except Exception as e:
        logger.error(f"Health check failed: {e}")


# =========================
# Show AI choice
# =========================
def show_ai_choice(chat_id, uid, title, body, photo_bytes, reply_to_msg=None):
    user_state[uid] = {
        "step": "waiting_ai_choice",
        "photo_bytes": photo_bytes,
        "title": title,
        "body_raw": body,
        "type": "post",
    }
    title_safe = html.escape(title or "—")
    body_preview = html.escape((body or "")[:300])
    text = (
        "✅ Принял пост!\n\n"
        f"<b>Заголовок:</b> {title_safe}\n"
        f"<b>Текст:</b> {body_preview}\n\n"
        "🤖 Оставить оригинальный текст или адаптировать через ИИ?"
    )
    if reply_to_msg is not None:
        bot.reply_to(reply_to_msg, text, parse_mode="HTML", reply_markup=ai_choice_kb())
    else:
        bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=ai_choice_kb())


# =========================
# Callbacks
# =========================
@bot.callback_query_handler(func=lambda c: True)
def on_action(call):
    uid = call.from_user.id
    st = user_state.get(uid)
    if not st:
        bot.answer_callback_query(call.id, "Сессия устарела. Начни заново.")
        return

    if call.data == "ai_keep_original":
        bot.answer_callback_query(call.id, "Ок")
        bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id,
                              text="📌 Выбери формат:", reply_markup=format_choice_kb())
        st["ai_choice"] = "original"
        user_state[uid] = st
        return

    if call.data == "ai_adapt":
        bot.answer_callback_query(call.id, "🤖 Адаптирую...")
        original = st.get("body_raw") or st.get("title") or ""
        if not original:
            bot.send_message(call.message.chat.id, "❌ Нет текста.")
            return
        if not deepseek_client:
            bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id,
                                  text="❌ ИИ недоступен. Выбери формат с оригиналом:",
                                  reply_markup=format_choice_kb())
            st["ai_choice"] = "original"
            user_state[uid] = st
            return
        adapted = adapt_news_with_ai(original)
        if not adapted:
            bot.send_message(call.message.chat.id, "❌ Не удалось адаптировать. Проверь логи (DeepSeek API error).")
            return
        st["adapted_text"] = adapted
        st["ai_choice"] = "adapted"
        user_state[uid] = st
        bot.edit_message_text(
            chat_id=call.message.chat.id, message_id=call.message.message_id,
            text=f"🤖 <b>Новость:</b>\n\n<i>{html.escape(adapted)}</i>\n\n📌 Выбери формат:",
            parse_mode="HTML", reply_markup=format_choice_kb()
        )
        return

    if call.data == "format_post":
        bot.answer_callback_query(call.id, "📝 Пост")
        if st.get("ai_choice") == "adapted":
            st["title"] = st["adapted_text"]
            st["body_raw"] = ""
        st["step"] = "waiting_post_highlight"
        user_state[uid] = st
        bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id,
                              text="💜 Пришли <b>ФРАЗУ</b> для выделения (или «нет»):", parse_mode="HTML")
        return

    if call.data == "format_story":
        bot.answer_callback_query(call.id, "📱 Сторис")
        if st.get("ai_choice") == "adapted":
            st["news_text"] = st["adapted_text"]
        else:
            st["news_text"] = st.get("body_raw") or st.get("title", "")
        st["step"] = "waiting_news_highlight"
        user_state[uid] = st
        bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id,
                              text="💜 Пришли <b>ФРАЗУ</b> для выделения (или «нет»):", parse_mode="HTML")
        return

    if call.data == "edit_post_body":
        st["step"] = "waiting_post_body"; user_state[uid] = st
        bot.answer_callback_query(call.id, "Ок")
        bot.edit_message_caption(chat_id=call.message.chat.id, message_id=call.message.message_id,
                                 caption="📝 Пришли новый ОСНОВНОЙ ТЕКСТ:")
    elif call.data == "edit_post_title":
        st["step"] = "waiting_post_title"; user_state[uid] = st
        bot.answer_callback_query(call.id, "Ок")
        bot.edit_message_caption(chat_id=call.message.chat.id, message_id=call.message.message_id,
                                 caption="📝 Пришли новый ЗАГОЛОВОК:")
    elif call.data == "edit_post_highlight":
        st["step"] = "waiting_post_highlight"; user_state[uid] = st
        bot.answer_callback_query(call.id, "Ок")
        bot.edit_message_caption(chat_id=call.message.chat.id, message_id=call.message.message_id,
                                 caption="💜 Пришли новую ФРАЗУ:")
    elif call.data == "restart_post":
        user_state[uid] = {"step": "waiting_post_photo"}
        bot.answer_callback_query(call.id, "Заново")
        bot.delete_message(call.message.chat.id, call.message.message_id)
        bot.send_message(call.message.chat.id, "📸 Пришли фото:", reply_markup=main_menu_kb())
    elif call.data == "finish_post":
        bot.answer_callback_query(call.id, "✅")
        bot.send_message(call.message.chat.id, "✅ Готово!", reply_markup=main_menu_kb())
        user_state[uid] = {"step": "idle"}
    elif call.data == "cancel_post":
        bot.answer_callback_query(call.id, "❌")
        bot.delete_message(call.message.chat.id, call.message.message_id)
        bot.send_message(call.message.chat.id, "Отменено", reply_markup=main_menu_kb())
        user_state[uid] = {"step": "idle"}
    elif call.data == "edit_news_text":
        st["step"] = "waiting_news_text"; user_state[uid] = st
        bot.answer_callback_query(call.id, "Ок")
        bot.edit_message_caption(chat_id=call.message.chat.id, message_id=call.message.message_id,
                                 caption="📝 Пришли новый текст новости:")
    elif call.data == "edit_news_highlight":
        st["step"] = "waiting_news_highlight"; user_state[uid] = st
        bot.answer_callback_query(call.id, "Ок")
        bot.edit_message_caption(chat_id=call.message.chat.id, message_id=call.message.message_id,
                                 caption="💜 Пришли новую ФРАЗУ:")
    elif call.data == "restart_news_story":
        user_state[uid] = {"step": "waiting_post_photo"}
        bot.answer_callback_query(call.id, "Заново")
        bot.delete_message(call.message.chat.id, call.message.message_id)
        bot.send_message(call.message.chat.id, "📸 Пришли фото:", reply_markup=main_menu_kb())
    elif call.data == "finish_news_story":
        bot.answer_callback_query(call.id, "✅")
        bot.send_message(call.message.chat.id, "✅ Готово!", reply_markup=main_menu_kb())
        user_state[uid] = {"step": "idle"}
    elif call.data == "cancel_news_story":
        bot.answer_callback_query(call.id, "❌")
        bot.delete_message(call.message.chat.id, call.message.message_id)
        bot.send_message(call.message.chat.id, "Отменено", reply_markup=main_menu_kb())
        user_state[uid] = {"step": "idle"}
    elif call.data == "edit_story_body":
        st["step"] = "waiting_story_body"; user_state[uid] = st
        bot.answer_callback_query(call.id, "Ок")
        bot.edit_message_caption(chat_id=call.message.chat.id, message_id=call.message.message_id,
                                 caption="📝 Пришли новый текст:")
    elif call.data == "edit_story_title":
        st["step"] = "waiting_story_title"; user_state[uid] = st
        bot.answer_callback_query(call.id, "Ок")
        bot.edit_message_caption(chat_id=call.message.chat.id, message_id=call.message.message_id,
                                 caption="📝 Пришли новый заголовок:")
    elif call.data == "restart_story":
        user_state[uid] = {"step": "waiting_story_photo"}
        bot.answer_callback_query(call.id, "Заново")
        bot.delete_message(call.message.chat.id, call.message.message_id)
        bot.send_message(call.message.chat.id, "📸 Пришли фото:", reply_markup=main_menu_kb())
    elif call.data == "finish_story":
        bot.answer_callback_query(call.id, "✅")
        bot.send_message(call.message.chat.id, "✅ Готово!", reply_markup=main_menu_kb())
        user_state[uid] = {"step": "idle"}
    elif call.data == "cancel_story":
        bot.answer_callback_query(call.id, "❌")
        bot.delete_message(call.message.chat.id, call.message.message_id)
        bot.send_message(call.message.chat.id, "Отменено", reply_markup=main_menu_kb())
        user_state[uid] = {"step": "idle"}
    elif call.data == "enhance_another":
        st["step"] = "waiting_enhance_photo"; user_state[uid] = st
        bot.answer_callback_query(call.id, "Ок")
        bot.send_message(call.message.chat.id, "✨ Отправь фото:", reply_markup=main_menu_kb())
    elif call.data == "enhance_main_menu":
        user_state[uid] = {"step": "idle"}
        bot.answer_callback_query(call.id, "Меню")
        bot.send_message(call.message.chat.id, "🏠 Меню", reply_markup=main_menu_kb())


# =========================
# Commands
# =========================
@bot.message_handler(commands=["start", "help"])
def cmd_start(message):
    user_state[message.from_user.id] = {"step": "idle"}
    bot.send_message(
        message.chat.id,
        "👋 <b>Feeder Bot</b>\n\n"
        "• Оформление постов и сторис\n"
        "• Адаптация текста через ИИ (deepseek-v4-flash)\n"
        "• Улучшение фото\n\n"
        "🚀 Перешли пост из канала или отправь фото с текстом.",
        parse_mode="HTML", reply_markup=main_menu_kb()
    )


@bot.message_handler(commands=["post"])
def cmd_post(message):
    user_state[message.from_user.id] = {"step": "waiting_post_photo"}
    bot.send_message(message.chat.id, "📸 Пришли фото:", reply_markup=main_menu_kb())


@bot.message_handler(commands=["story"])
def cmd_story(message):
    user_state[message.from_user.id] = {"step": "waiting_story_photo"}
    bot.send_message(message.chat.id, "📸 Пришли фото для сторис:", reply_markup=main_menu_kb())


@bot.message_handler(commands=["enhance"])
def cmd_enhance(message):
    user_state[message.from_user.id] = {"step": "waiting_enhance_photo"}
    bot.send_message(message.chat.id, "✨ Отправь фото:", reply_markup=main_menu_kb())


@bot.message_handler(commands=["stop"])
def cmd_stop(message):
    user_state[message.from_user.id] = {"step": "idle"}
    bot.send_message(message.chat.id, "🛑 Сброшено.", reply_markup=main_menu_kb())


@bot.message_handler(func=lambda m: m.text == BTN_POST)
def h_post(m): cmd_post(m)


@bot.message_handler(func=lambda m: m.text == BTN_STORY)
def h_story(m): cmd_story(m)


@bot.message_handler(func=lambda m: m.text == BTN_ENHANCE)
def h_enhance(m): cmd_enhance(m)


# =========================
# Media groups (альбомы)
# =========================
def _process_media_group(gid: str):
    time.sleep(1.5)
    with media_group_lock:
        group = media_groups.pop(gid, None)
    if not group:
        return
    msgs = group["messages"]
    if not msgs:
        return
    text = ""
    for m in msgs:
        t = get_message_text(m)
        if t:
            text = t
            break
    first = msgs[0]
    photo_bytes = get_photo_bytes(first)
    if not photo_bytes:
        return
    chat_id = first.chat.id
    uid = first.from_user.id if first.from_user else 0
    title, body = split_title_and_body(text)
    logger.info(f"MEDIA_GROUP processed: {len(msgs)} msgs, text_len={len(text)}")
    show_ai_choice(chat_id, uid, title, body, photo_bytes)


# =========================
# Photo/document handler
# =========================
@bot.message_handler(content_types=["photo", "document"])
def on_photo_or_document(message):
    uid = message.from_user.id if message.from_user else 0
    st = user_state.get(uid) or {"step": "idle"}
    step = st.get("step")
    caption_text = get_message_text(message)
    forwarded = is_forwarded(message)

    logger.info(f"PHOTO uid={uid} step={step} fwd={forwarded} "
                f"caption_len={len(caption_text)} mgid={message.media_group_id}")

    # === Медиа-группа (альбом) ===
    if message.media_group_id:
        gid = message.media_group_id
        with media_group_lock:
            if gid not in media_groups:
                media_groups[gid] = {"messages": [], "timer": None}
            media_groups[gid]["messages"].append(message)
            if media_groups[gid]["timer"] is not None:
                try:
                    media_groups[gid]["timer"].cancel()
                except:
                    pass
            t = threading.Timer(2.0, _process_media_group, args=(gid,))
            media_groups[gid]["timer"] = t
            t.daemon = True
            t.start()
        return

    # === Досылка фото к репосту без фото ===
    if step == "waiting_repost_photo":
        photo_bytes = get_photo_bytes(message)
        if not photo_bytes:
            bot.reply_to(message, "❌ Пришли JPG/PNG.")
            return
        if not check_file_size(photo_bytes):
            bot.reply_to(message, "❌ Файл > 20MB.")
            return
        warn_if_too_small(message.chat.id, photo_bytes)
        show_ai_choice(message.chat.id, uid, st.get("title", ""), st.get("body_raw", ""),
                       photo_bytes, reply_to_msg=message)
        return

    # === Авто-приём: фото + текст, либо репост с фото ===
    if step in ("idle", "waiting_post_photo", None):
        if caption_text:
            photo_bytes = get_photo_bytes(message)
            if not photo_bytes:
                bot.reply_to(message, "⚠️ Не удалось получить фото. Пришли ещё раз.",
                             reply_markup=main_menu_kb())
                return
            if not check_file_size(photo_bytes):
                bot.reply_to(message, "❌ Файл > 20MB.")
                return
            warn_if_too_small(message.chat.id, photo_bytes)
            title, body = split_title_and_body(caption_text)
            show_ai_choice(message.chat.id, uid, title, body, photo_bytes, reply_to_msg=message)
            return
        else:
            if not forwarded:
                bot.reply_to(message, "⚠️ К фото нет текста. Пришли пост с текстом.",
                             reply_markup=main_menu_kb())
                return

    # === Улучшение ===
    if step == "waiting_enhance_photo":
        photo_bytes = get_photo_bytes(message)
        if not photo_bytes:
            bot.reply_to(message, "❌ Не фото.")
            return
        if not check_file_size(photo_bytes):
            bot.reply_to(message, "❌ Файл > 20MB.")
            return
        msg = bot.reply_to(message, "⏳ Улучшаю...")
        enhanced = enhance_image_simple(photo_bytes)
        bot.send_document(message.chat.id, enhanced, visible_file_name="enhanced.jpg",
                          caption="✨ Готово!", reply_markup=main_menu_kb())
        bot.delete_message(message.chat.id, msg.message_id)
        return

    # === Фото для поста (ручной режим) ===
    if step == "waiting_post_photo":
        photo_bytes = get_photo_bytes(message)
        if not photo_bytes:
            bot.reply_to(message, "❌ Пришли фото.")
            return
        if not check_file_size(photo_bytes):
            bot.reply_to(message, "❌ Файл > 20MB.")
            return
        warn_if_too_small(message.chat.id, photo_bytes)
        st["photo_bytes"] = photo_bytes
        st["step"] = "waiting_post_title"
        user_state[uid] = st
        bot.reply_to(message, "📸 Фото сохранено!\n\nОтправь <b>ЗАГОЛОВОК</b>:", parse_mode="HTML")
        return

    # === Фото для сторис ===
    if step == "waiting_story_photo":
        photo_bytes = get_photo_bytes(message)
        if not photo_bytes:
            bot.reply_to(message, "❌ Пришли фото.")
            return
        if not check_file_size(photo_bytes):
            bot.reply_to(message, "❌ Файл > 20MB.")
            return
        warn_if_too_small(message.chat.id, photo_bytes)
        st["photo_bytes"] = photo_bytes
        st["step"] = "waiting_story_title"
        user_state[uid] = st
        bot.reply_to(message, "📸 Фото сохранено!\n\nОтправь <b>ЗАГОЛОВОК</b>:", parse_mode="HTML")
        return

    bot.reply_to(message, "Выбери действие 👇", reply_markup=main_menu_kb())


# =========================
# Text handler
# =========================
@bot.message_handler(content_types=["text"])
def on_text(message):
    uid = message.from_user.id
    text = (message.text or "").strip()
    st = user_state.get(uid) or {"step": "idle"}
    step = st.get("step")
    forwarded = is_forwarded(message)

    logger.info(f"TEXT uid={uid} step={step} fwd={forwarded} len={len(text)}")

    if text == BTN_POST:
        cmd_post(message); return
    if text == BTN_STORY:
        cmd_story(message); return
    if text == BTN_ENHANCE:
        cmd_enhance(message); return

    # === Репост текста без фото ===
    if forwarded and step in ("idle", "waiting_post_action", None):
        title, body = split_title_and_body(text)
        user_state[uid] = {
            "step": "waiting_repost_photo",
            "type": "post",
            "title": title,
            "body_raw": body,
        }
        bot.reply_to(
            message,
            "✅ Принял пост, но в нём <b>нет фото</b>.\n\n📸 Дошли картинку:",
            parse_mode="HTML", reply_markup=main_menu_kb()
        )
        return

    # === Пост: заголовок ===
    if step == "waiting_post_title":
        if not text:
            bot.reply_to(message, "❌ Пустой заголовок."); return
        st["title"] = text
        st["step"] = "waiting_post_highlight"
        user_state[uid] = st
        bot.reply_to(message, f"✅ Заголовок: <b>{html.escape(text)}</b>\n\n💜 Пришли <b>ФРАЗУ</b>:", parse_mode="HTML")
        return

    # === Пост: плашка ===
    if step == "waiting_post_highlight":
        if not text:
            bot.reply_to(message, "❌ Пустая фраза."); return
        st["highlight_phrase"] = text if text.lower() != "нет" else ""
        if st.get("body_raw"):
            try:
                card = make_fdr_post_card(st["photo_bytes"], st["title"], st.get("highlight_phrase", ""))
                caption = f"<b>{html.escape(st['title'])}</b>\n\n{html.escape(st['body_raw'][:200])}"
                msg = bot.send_photo(message.chat.id, photo=card, caption=caption,
                                     parse_mode="HTML", reply_markup=post_preview_kb())
                bot.send_message(message.chat.id, "✅ Пост готов!", reply_markup=main_menu_kb())
                st["step"] = "waiting_post_action"
                st["card_bytes"] = card.getvalue()
                st["preview_message_id"] = msg.message_id
                user_state[uid] = st
                return
            except Exception as e:
                logger.error(f"Post card error: {e}")
                bot.reply_to(message, f"❌ Ошибка: {e}")
                return
        st["step"] = "waiting_post_body"
        user_state[uid] = st
        bot.reply_to(message, "✅ Фраза сохранена!\n\nОтправь <b>ОСНОВНОЙ ТЕКСТ</b>:", parse_mode="HTML")
        return

    # === Пост: тело ===
    if step == "waiting_post_body":
        if not text:
            bot.reply_to(message, "❌ Пустой текст."); return
        st["body_raw"] = text
        try:
            card = make_fdr_post_card(st["photo_bytes"], st["title"], st.get("highlight_phrase", ""))
            caption = f"<b>{html.escape(st['title'])}</b>\n\n{html.escape(st['body_raw'][:200])}"
            msg = bot.send_photo(message.chat.id, photo=card, caption=caption,
                                 parse_mode="HTML", reply_markup=post_preview_kb())
            bot.send_message(message.chat.id, "✅ Пост готов!", reply_markup=main_menu_kb())
            st["step"] = "waiting_post_action"
            st["card_bytes"] = card.getvalue()
            st["preview_message_id"] = msg.message_id
            user_state[uid] = st
        except Exception as e:
            logger.error(f"Post body error: {e}")
            bot.reply_to(message, f"❌ Ошибка: {e}")
        return

    # === Сторис-новость: плашка ===
    if step == "waiting_news_highlight":
        if not text:
            bot.reply_to(message, "❌ Пустая фраза."); return
        st["highlight_phrase"] = text if text.lower() != "нет" else ""
        try:
            card = make_news_story_card(st["photo_bytes"], st["news_text"], st.get("highlight_phrase", ""))
            msg = bot.send_photo(message.chat.id, photo=card, caption="📱 Сторис",
                                 reply_markup=news_story_preview_kb())
            bot.send_message(message.chat.id, "✅ Готово!", reply_markup=main_menu_kb())
            st["step"] = "waiting_news_story_action"
            st["card_bytes"] = card.getvalue()
            st["preview_message_id"] = msg.message_id
            user_state[uid] = st
        except Exception as e:
            logger.error(f"News story error: {e}")
            bot.reply_to(message, f"❌ Ошибка: {e}")
        return

    # === Сторис-новость: текст ===
    if step == "waiting_news_text":
        if not text:
            bot.reply_to(message, "❌ Пустой текст."); return
        st["news_text"] = text
        try:
            card = make_news_story_card(st["photo_bytes"], st["news_text"], st.get("highlight_phrase", ""))
            bot.edit_message_media(
                chat_id=message.chat.id,
                message_id=st.get("preview_message_id"),
                media=telebot.types.InputMediaPhoto(card, caption="📱 Обновлено")
            )
            st["step"] = "waiting_news_story_action"
            user_state[uid] = st
            bot.reply_to(message, "✅ Обновлено!")
        except Exception as e:
            logger.error(f"News text error: {e}")
            bot.reply_to(message, f"❌ Ошибка: {e}")
        return

    # === Сторис: заголовок ===
    if step == "waiting_story_title":
        if not text:
            bot.reply_to(message, "❌ Пустой заголовок."); return
        st["title"] = text
        st["step"] = "waiting_story_body"
        user_state[uid] = st
        bot.reply_to(message, f"✅ Заголовок: <b>{html.escape(text)}</b>\n\nОтправь <b>ТЕКСТ</b>:", parse_mode="HTML")
        return

    # === Сторис: тело ===
    if step == "waiting_story_body":
        if not text:
            bot.reply_to(message, "❌ Пустой текст."); return
        st["body_raw"] = text
        try:
            card = make_fdr_story_card(st["photo_bytes"], st["title"], st["body_raw"])
            msg = bot.send_photo(message.chat.id, photo=card, caption="📱 Сторис",
                                 reply_markup=story_preview_kb())
            bot.send_message(message.chat.id, "✅ Готово!", reply_markup=main_menu_kb())
            st["step"] = "waiting_story_action"
            st["card_bytes"] = card.getvalue()
            st["preview_message_id"] = msg.message_id
            user_state[uid] = st
        except Exception as e:
            logger.error(f"Story error: {e}")
            bot.reply_to(message, f"❌ Ошибка: {e}")
        return

    bot.send_message(message.chat.id, "Выбери действие 👇", reply_markup=main_menu_kb())


# =========================
# Channel post handler
# =========================
@bot.channel_post_handler(content_types=["photo", "document", "text"])
def on_channel_post(message):
    logger.info(f"CHANNEL_POST type={message.content_type} "
                f"caption_len={len(message.caption or '')} "
                f"text_len={len(message.text or '')}")
    text = get_message_text(message)
    photo_bytes = get_photo_bytes(message)

    if not text and not photo_bytes:
        return

    chat_id = message.chat.id
    uid = message.from_user.id if message.from_user else chat_id

    if photo_bytes:
        title, body = split_title_and_body(text)
        show_ai_choice(chat_id, uid, title, body, photo_bytes)
    else:
        title, body = split_title_and_body(text)
        user_state[uid] = {
            "step": "waiting_repost_photo",
            "type": "post",
            "title": title,
            "body_raw": body,
        }
        bot.reply_to(message, "✅ Принял пост, но нет фото. Дошли картинку:")


# =========================
# Polling with 409 protection
# =========================
def prepare_telegram_before_polling():
    try:
        bot.delete_webhook(drop_pending_updates=False)
        logger.info("Webhook removed")
    except Exception as e:
        logger.warning(f"delete_webhook failed: {e}")


def wait_for_other_instance(max_wait_sec: int = 90):
    deadline = time.time() + max_wait_sec
    delay = 5
    while time.time() < deadline:
        try:
            bot.get_updates(limit=1, timeout=1)
            logger.info("Telegram free, starting")
            return True
        except ApiTelegramException as e:
            if e.error_code == 409:
                remaining = int(deadline - time.time())
                logger.warning(f"409: другой экземпляр. Ждём {delay}s (осталось {remaining}s)...")
                time.sleep(delay)
                delay = min(delay * 2, 30)
            else:
                logger.error(f"Wait error: {e}")
                return True
        except Exception as e:
            logger.error(f"Wait error: {e}")
            time.sleep(delay)
            delay = min(delay * 2, 30)
    return False


def start_polling_with_retry():
    max_retries = 30
    delay = 5
    attempt = 0
    while attempt < max_retries:
        attempt += 1
        try:
            logger.info(f"Polling (attempt {attempt})...")
            bot.infinity_polling(timeout=60, long_polling_timeout=60,
                                 logger_level=logging.ERROR, skip_pending=True)
            return
        except ApiTelegramException as e:
            if e.error_code == 409:
                logger.warning(f"409: retry {delay}s ({attempt}/{max_retries})")
                time.sleep(delay)
                delay = min(delay * 2, 120)
            else:
                logger.error(f"Telegram error: {e}")
                time.sleep(delay)
                delay = min(delay * 2, 120)
        except Exception as e:
            logger.error(f"Polling error: {e}")
            time.sleep(delay)
            delay = min(delay * 2, 120)
    logger.error("Max retries, exit")
    sys.exit(1)


# =========================
# Main
# =========================
if __name__ == "__main__":
    logger.info("Starting Feeder Bot...")
    try:
        ensure_fonts()
        logger.info("Fonts loaded")
        threading.Thread(target=run_http_server, daemon=True).start()
        logger.info("🌐 Health check started")
        prepare_telegram_before_polling()
        wait_for_other_instance(max_wait_sec=90)
        logger.info("🤖 Bot polling...")
        start_polling_with_retry()
    except Exception as e:
        logger.error(f"❌ Bot crashed: {e}")
        try:
            if os.path.exists(lock_file):
                os.unlink(lock_file)
        except:
            pass
        raise
