# -*- coding: utf-8 -*-

import os
import re
import html
import time
import hashlib
import json
import logging
import signal
import sys
import functools
import fcntl
import atexit
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from io import BytesIO
from typing import List, Dict, Optional, Tuple
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
import telebot
from telebot.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton
)
from PIL import Image, ImageDraw, ImageFont, ImageEnhance, ImageFilter
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Проверка на единственный экземпляр
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
# Logging setup
# =========================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('feeder_bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


# =========================
# ENV
# =========================
TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
BOT_USERNAME = (os.getenv("BOT_USERNAME") or "").strip().lstrip("@")

if not TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")
if " " in TOKEN:
    raise ValueError("BOT_TOKEN must not contain spaces")

# Constants
MAX_FILE_SIZE = 20 * 1024 * 1024
REQUEST_TIMEOUT = 15

# Константы для дизайна
FDR_POST_PURPLE_COLOR = (122, 58, 240)
FDR_STORY_W = 720
FDR_STORY_H = 1280

# =========================
# UI BUTTONS
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
# FONTS - ВЕЗДЕ ИСПОЛЬЗУЕМ INTER-BLACK
# =========================
FONT_MAIN = "Inter-Black.ttf"  # Единый шрифт для всего
FOOTER_TEXT = "FEEDER.NEWS"

TARGET_W, TARGET_H = 750, 938  # 4:5 для постов
CHP_GRADIENT_PCT = 0.48


# =========================
# BOT + SESSION
# =========================
bot = telebot.TeleBot(TOKEN)

SESSION = requests.Session()
retry_strategy = Retry(
    total=0,
    backoff_factor=0,
    status_forcelist=[],
)
adapter = HTTPAdapter(
    max_retries=retry_strategy,
    pool_connections=10,
    pool_maxsize=10
)
SESSION.mount("http://", adapter)
SESSION.mount("https://", adapter)

SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
})

URL_RE = re.compile(r"(https?://[^\s]+)", re.IGNORECASE)

user_state: Dict[int, Dict] = {}


# =========================
# Graceful shutdown
# =========================
def signal_handler(sig, frame):
    logger.info("Shutting down gracefully...")
    bot.stop_polling()
    try:
        if os.path.exists(lock_file):
            os.unlink(lock_file)
    except:
        pass
    sys.exit(0)


signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


# =========================
# Helper functions
# =========================
def validate_url(url: str) -> bool:
    try:
        result = urlparse(url)
        return all([result.scheme, result.netloc]) and result.scheme in ['http', 'https']
    except Exception:
        return False


def check_file_size(file_bytes: bytes) -> bool:
    return len(file_bytes) <= MAX_FILE_SIZE


def http_get_bytes(url: str, timeout: int = REQUEST_TIMEOUT) -> Optional[bytes]:
    if not validate_url(url):
        return None
    try:
        r = SESSION.get(url, timeout=timeout)
        r.raise_for_status()
        return r.content
    except Exception as e:
        logger.debug(f"Failed to get bytes from {url}: {e}")
        return None


def extract_source_url(text: str) -> str:
    m = URL_RE.search(text or "")
    return m.group(1) if m else ""


def ensure_fonts():
    fonts = [FONT_MAIN]
    for font in fonts:
        if not os.path.exists(font):
            raise RuntimeError(f"Font not found: {font}")


def warn_if_too_small(chat_id, photo_bytes: bytes):
    try:
        im = Image.open(BytesIO(photo_bytes))
        if im.width < 900 or im.height < 1100:
            bot.send_message(
                chat_id,
                "⚠️ Фото маленького разрешения. Лучше присылать больше (от 1080×1350 и выше), "
                "чтобы текст был максимально чёткий."
            )
    except Exception as e:
        logger.error(f"Error checking image size: {e}")


def clear_state(user_id: int):
    if user_id in user_state:
        user_state[user_id] = {"step": "idle"}
        logger.info(f"Cleared state for user {user_id}")


def tg_file_bytes(file_id: str) -> bytes:
    try:
        file_info = bot.get_file(file_id)
        file_url = f"https://api.telegram.org/file/bot{TOKEN}/{file_info.file_path}"
        r = SESSION.get(file_url, timeout=30)
        r.raise_for_status()
        return r.content
    except Exception as e:
        logger.error(f"Failed to download file {file_id}: {e}")
        raise


# =========================
# Image enhancement
# =========================
def enhance_image_simple(image_bytes: bytes) -> BytesIO:
    try:
        img = Image.open(BytesIO(image_bytes)).convert("RGB")
        
        enhancer_sharpness = ImageEnhance.Sharpness(img)
        img = enhancer_sharpness.enhance(1.20)
        
        enhancer_color = ImageEnhance.Color(img)
        img = enhancer_color.enhance(1.15)
        
        output = BytesIO()
        img.save(output, format="JPEG", quality=98, optimize=True)
        output.seek(0)
        return output
        
    except Exception as e:
        logger.error(f"Error enhancing image: {e}")
        output = BytesIO(image_bytes)
        output.seek(0)
        return output


# =========================
# Gradient functions
# =========================
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
    out = Image.alpha_composite(base, overlay)
    return out.convert("RGB")


# =========================
# Text wrapping functions
# =========================
def text_width(draw: ImageDraw.ImageDraw, s: str, font: ImageFont.FreeTypeFont) -> int:
    bb = draw.textbbox((0, 0), s, font=font)
    return bb[2] - bb[0]


def wrap_no_truncate(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont,
                     max_width: int, max_lines: int = 6) -> Tuple[List[str], bool]:
    words = [w for w in (text or "").split() if w.strip()]
    if not words:
        return [""], True

    lines: List[str] = []
    cur = ""
    i = 0

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


def fit_text_block(
    draw: ImageDraw.ImageDraw,
    text: str,
    font_path: str,
    safe_w: int,
    max_block_h: int,
    max_lines: int = 6,
    start_size: int = 90,
    min_size: int = 16,
    line_spacing_ratio: float = 0.22,
) -> Tuple[ImageFont.FreeTypeFont, List[str], List[int], int, int]:
    text = (text or "").strip().upper()
    if not text:
        text = " "

    size = start_size
    while size >= min_size:
        font = ImageFont.truetype(font_path, size)
        lines, ok = wrap_no_truncate(draw, text, font, safe_w, max_lines=max_lines)
        spacing = int(size * line_spacing_ratio)

        heights = []
        total_h = 0
        max_w = 0
        for ln in lines:
            bb = draw.textbbox((0, 0), ln, font=font)
            lw = bb[2] - bb[0]
            lh = bb[3] - bb[1]
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
    heights = []
    total_h = 0
    for ln in lines:
        bb = draw.textbbox((0, 0), ln, font=font)
        lh = bb[3] - bb[1]
        heights.append(lh)
        total_h += lh
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
    else:
        new_h = int(w / target_ratio)
        top = (h - new_h) // 2
        return img.crop((0, top, w, top + new_h))


def _wrap_text_preserve_paragraphs(draw, text, font, max_w):
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
    max_w = x2 - x1
    max_h = y2 - y1

    selected_font = ImageFont.truetype(FONT_MAIN, min_size)  # Используем Inter-Black
    selected_gap = 8
    selected_paragraph_gap = 12

    for size in range(max_size, min_size - 1, -1):
        font = ImageFont.truetype(FONT_MAIN, size)  # Используем Inter-Black
        lines = _wrap_text_preserve_paragraphs(draw, text, font, max_w)
        if not lines:
            continue

        line_h = font.getbbox("Ag")[3] - font.getbbox("Ag")[1]
        gap = max(4, int(line_h * line_gap_ratio))
        paragraph_gap = max(gap + 2, int(line_h * paragraph_gap_ratio))

        total_h = 0
        max_line_w = 0
        for line in lines:
            if line == "":
                total_h += paragraph_gap
                continue
            lw = font.getbbox(line)[2] - font.getbbox(line)[0]
            max_line_w = max(max_line_w, lw)
            total_h += line_h + gap

        if total_h <= max_h and max_line_w <= max_w:
            selected_font = font
            selected_gap = gap
            selected_paragraph_gap = paragraph_gap
            break

    return selected_font, selected_gap, selected_paragraph_gap


def _draw_story_text(draw, text, box, font, fill=(255, 255, 255), align="center", valign="center",
                     line_gap=10, paragraph_gap_extra=10):
    x1, y1, x2, y2 = box
    max_w = x2 - x1
    max_h = y2 - y1

    lines = _wrap_text_preserve_paragraphs(draw, text, font, max_w)
    if not lines:
        return

    line_h = font.getbbox("Ag")[3] - font.getbbox("Ag")[1]
    total_h = 0
    for line in lines:
        if line == "":
            total_h += paragraph_gap_extra
        else:
            total_h += line_h + line_gap

    if valign == "top":
        y = y1
    else:
        y = y1 + (max_h - total_h) // 2

    for line in lines:
        if line == "":
            y += paragraph_gap_extra
            continue
        line_w = font.getbbox(line)[2] - font.getbbox(line)[0]
        if align == "center":
            x = x1 + (max_w - line_w) // 2
        elif align == "left":
            x = x1
        else:
            x = x2 - line_w
        draw.text((x, y), line, font=font, fill=fill)
        y += line_h + line_gap


# =========================
# Card making functions
# =========================
def make_fdr_post_card(photo_bytes: bytes, title_text: str, highlight_phrase: str) -> BytesIO:
    """Создание карточки для поста в стиле ФДР с использованием Inter-Black"""
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
    
    title_text_upper = title_text.strip().upper()
    highlight_phrase_upper = highlight_phrase.strip().upper()
    highlight_words = set(highlight_phrase_upper.split())
    
    title_max_h = int(img.height * 0.23)
    
    # Используем Inter-Black для текста поста
    font, lines, heights, spacing, total_h = fit_text_block(
        draw=draw,
        text=title_text_upper,
        font_path=FONT_MAIN,  # Inter-Black.ttf
        safe_w=safe_w,
        max_block_h=title_max_h,
        max_lines=6,
        start_size=int(img.height * 0.11),
        min_size=16,
        line_spacing_ratio=0.22
    )
    
    base_y = img.height - margin_bottom - total_h
    
    # Рисуем фиолетовые плашки под выделенными словами
    y = base_y
    for line_idx, line in enumerate(lines):
        line_words = line.split()
        current_x = margin_x
        
        for word in line_words:
            word_bbox = draw.textbbox((current_x, y), word, font=font)
            word_x1, word_y1, word_x2, word_y2 = word_bbox
            
            if word in highlight_words:
                padding = 10
                draw.rectangle(
                    [word_x1 - padding, word_y1 - padding,
                     word_x2 + padding, word_y2 + padding],
                    fill=FDR_POST_PURPLE_COLOR
                )
            
            if word != line_words[-1]:
                space_width = text_width(draw, " ", font)
                current_x += text_width(draw, word, font) + space_width
            else:
                current_x += text_width(draw, word, font)
        
        y += heights[line_idx] + spacing
    
    # Рисуем текст поверх плашек
    y = base_y
    for line_idx, line in enumerate(lines):
        line_words = line.split()
        current_x = margin_x
        
        for word in line_words:
            draw.text((current_x, y), word, font=font, fill="white")
            if word != line_words[-1]:
                space_width = text_width(draw, " ", font)
                current_x += text_width(draw, word, font) + space_width
            else:
                current_x += text_width(draw, word, font)
        
        y += heights[line_idx] + spacing
    
    # Добавляем подпись FEEDER.NEWS (тоже Inter-Black)
    footer_font = ImageFont.truetype(FONT_MAIN, 30)
    footer_bbox = draw.textbbox((0, 0), FOOTER_TEXT, font=footer_font)
    footer_w = footer_bbox[2] - footer_bbox[0]
    footer_x = (img.width - footer_w) // 2
    footer_y = 20
    draw.text((footer_x, footer_y), FOOTER_TEXT, font=footer_font, fill=(255, 255, 255, 180))
    
    out = BytesIO()
    img.save(out, format="JPEG", quality=95, subsampling=0, optimize=True)
    out.seek(0)
    return out


def make_fdr_story_card(photo_bytes: bytes, title: str, body_text: str) -> BytesIO:
    """Создание карточки для сторис в стиле ФДР с использованием Inter-Black"""
    ensure_fonts()

    canvas = Image.new("RGB", (FDR_STORY_W, FDR_STORY_H), (0, 0, 0))
    draw = ImageDraw.Draw(canvas)

    photo_h = 410
    header_h = 220

    photo = Image.open(BytesIO(photo_bytes)).convert("RGB")
    
    def fit_cover(im: Image.Image, target_w: int, target_h: int) -> Image.Image:
        src_w, src_h = im.size
        scale = max(target_w / src_w, target_h / src_h)
        nw, nh = int(src_w * scale), int(src_h * scale)
        resized = im.resize((nw, nh), Image.LANCZOS)
        left = max(0, (nw - target_w) // 2)
        top = max(0, (nh - target_h) // 2)
        return resized.crop((left, top, left + target_w, top + target_h))
    
    story_photo = fit_cover(photo, FDR_STORY_W, photo_h)
    canvas.paste(story_photo, (0, 0))

    purple_color = (122, 58, 240)
    canvas.paste(Image.new("RGB", (FDR_STORY_W, header_h), purple_color), (0, photo_h))

    draw.rectangle([0, photo_h + header_h, FDR_STORY_W, FDR_STORY_H], fill=(0, 0, 0))

    padding = 34

    header_box = (padding, photo_h + padding, FDR_STORY_W - padding, photo_h + header_h - padding)
    body_box = (padding, photo_h + header_h + padding, FDR_STORY_W - padding, FDR_STORY_H - padding)

    # Для заголовка сторис используем Inter-Black
    title_font, title_gap, title_paragraph_gap = _fit_story_text(
        draw, title, header_box, min_size=28, max_size=54,
        line_gap_ratio=0.08, paragraph_gap_ratio=0.18
    )

    _draw_story_text(draw, title, header_box, title_font, fill=(255, 255, 255),
                     align="center", valign="center", line_gap=title_gap,
                     paragraph_gap_extra=title_paragraph_gap)

    # Для основного текста сторис тоже Inter-Black
    body_font, body_gap, body_paragraph_gap = _fit_story_text(
        draw, body_text, body_box, min_size=14, max_size=30,
        line_gap_ratio=0.10, paragraph_gap_ratio=0.32
    )

    _draw_story_text(draw, body_text, body_box, body_font, fill=(255, 255, 255),
                     align="left", valign="top", line_gap=body_gap,
                     paragraph_gap_extra=body_paragraph_gap)

    # Добавляем подпись FEEDER.NEWS (тоже Inter-Black)
    footer_font = ImageFont.truetype(FONT_MAIN, 30)
    footer_bbox = draw.textbbox((0, 0), FOOTER_TEXT, font=footer_font)
    footer_w = footer_bbox[2] - footer_bbox[0]
    footer_x = (FDR_STORY_W - footer_w) // 2
    footer_y = 20
    draw.text((footer_x, footer_y), FOOTER_TEXT, font=footer_font, fill=(255, 255, 255, 180))

    out = BytesIO()
    canvas.save(out, format="JPEG", quality=92, optimize=True)
    out.seek(0)
    return out


# =========================
# Caption formatting
# =========================
def build_caption_html(title: str, body: str) -> str:
    title_safe = html.escape((title or "").strip())
    body_preview = (body or "").strip()[:200] + "..." if len(body or "") > 200 else (body or "")
    body_high = html.escape(body_preview)
    
    return f"<b>{title_safe}</b>\n\n{body_high}"


def build_story_caption_html(title: str, body: str) -> str:
    title_safe = html.escape((title or "").strip())
    body_preview = (body or "").strip()[:100] + "..." if len(body or "") > 100 else (body or "")
    
    return f"<b>{title_safe}</b>\n\n{body_preview}"


# =========================
# Keyboard layouts
# =========================
def post_preview_kb():
    kb = InlineKeyboardMarkup()
    kb.row(
        InlineKeyboardButton("✏️ Изменить текст", callback_data="edit_post_body"),
        InlineKeyboardButton("✏️ Изменить заголовок", callback_data="edit_post_title"),
    )
    kb.row(
        InlineKeyboardButton("✏️ Изменить плашку", callback_data="edit_post_highlight"),
        InlineKeyboardButton("🔄 Создать заново", callback_data="restart_post"),
    )
    kb.row(
        InlineKeyboardButton("✅ Готово", callback_data="finish_post"),
        InlineKeyboardButton("❌ Отмена", callback_data="cancel_post"),
    )
    return kb


def story_preview_kb():
    kb = InlineKeyboardMarkup()
    kb.row(
        InlineKeyboardButton("✏️ Изменить текст", callback_data="edit_story_body"),
        InlineKeyboardButton("✏️ Изменить заголовок", callback_data="edit_story_title"),
    )
    kb.row(
        InlineKeyboardButton("🔄 Создать заново", callback_data="restart_story"),
        InlineKeyboardButton("✅ Готово", callback_data="finish_story"),
    )
    kb.row(
        InlineKeyboardButton("❌ Отмена", callback_data="cancel_story"),
    )
    return kb


def enhance_menu_kb():
    kb = InlineKeyboardMarkup()
    kb.row(
        InlineKeyboardButton("✨ Улучшить другое фото", callback_data="enhance_another"),
        InlineKeyboardButton("🏠 Главное меню", callback_data="enhance_main_menu")
    )
    return kb


# =========================
# Health check server
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
        server_address = ('0.0.0.0', port)
        httpd = HTTPServer(server_address, HealthCheckHandler)
        logger.info(f"🌐 Health check server started on port {port}")
        httpd.serve_forever()
    except Exception as e:
        logger.error(f"Failed to start health check server: {e}")


# =========================
# Callback handlers
# =========================
@bot.callback_query_handler(func=lambda c: c.data.startswith(("edit_", "restart_", "finish_", "cancel_", "enhance_")))
def on_action(call):
    uid = call.from_user.id
    st = user_state.get(uid)

    if not st:
        bot.answer_callback_query(call.id, "Сессия устарела. Начни заново.")
        return

    # Пост ФДР
    if call.data == "edit_post_body":
        st["step"] = "waiting_post_body"
        user_state[uid] = st
        bot.answer_callback_query(call.id, "Ок")
        bot.edit_message_caption(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            caption="📝 Пришли новый ОСНОВНОЙ ТЕКСТ для поста:"
        )

    elif call.data == "edit_post_title":
        st["step"] = "waiting_post_title"
        user_state[uid] = st
        bot.answer_callback_query(call.id, "Ок")
        bot.edit_message_caption(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            caption="📝 Пришли новый ЗАГОЛОВОК для поста:"
        )

    elif call.data == "edit_post_highlight":
        st["step"] = "waiting_post_highlight"
        user_state[uid] = st
        bot.answer_callback_query(call.id, "Ок")
        bot.edit_message_caption(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            caption="💜 Пришли новую ФРАЗУ для выделения фиолетовой плашкой:"
        )

    elif call.data == "restart_post":
        user_state[uid] = {"step": "waiting_post_photo", "type": "post"}
        bot.answer_callback_query(call.id, "Начинаем заново")
        bot.delete_message(call.message.chat.id, call.message.message_id)
        bot.send_message(
            call.message.chat.id,
            "🔄 Начинаем создание поста заново.\n\n📸 Пришли фото:",
            reply_markup=main_menu_kb()
        )

    elif call.data == "finish_post":
        bot.answer_callback_query(call.id, "Готово ✅")
        bot.send_message(
            call.message.chat.id,
            "✅ Пост готов! Фото выше можно скачать и использовать.",
            reply_markup=main_menu_kb()
        )
        user_state[uid] = {"step": "idle"}

    elif call.data == "cancel_post":
        bot.answer_callback_query(call.id, "Отменено")
        bot.delete_message(call.message.chat.id, call.message.message_id)
        bot.send_message(
            call.message.chat.id,
            "❌ Создание поста отменено",
            reply_markup=main_menu_kb()
        )
        user_state[uid] = {"step": "idle"}

    # Сторис ФДР
    elif call.data == "edit_story_body":
        st["step"] = "waiting_story_body"
        user_state[uid] = st
        bot.answer_callback_query(call.id, "Ок")
        bot.edit_message_caption(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            caption="📝 Пришли новый ОСНОВНОЙ ТЕКСТ для сторис:"
        )

    elif call.data == "edit_story_title":
        st["step"] = "waiting_story_title"
        user_state[uid] = st
        bot.answer_callback_query(call.id, "Ок")
        bot.edit_message_caption(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            caption="📝 Пришли новый ЗАГОЛОВОК для сторис:"
        )

    elif call.data == "restart_story":
        user_state[uid] = {"step": "waiting_story_photo", "type": "story"}
        bot.answer_callback_query(call.id, "Начинаем заново")
        bot.delete_message(call.message.chat.id, call.message.message_id)
        bot.send_message(
            call.message.chat.id,
            "🔄 Начинаем создание сторис заново.\n\n📸 Пришли фото:",
            reply_markup=main_menu_kb()
        )

    elif call.data == "finish_story":
        bot.answer_callback_query(call.id, "Готово ✅")
        bot.send_message(
            call.message.chat.id,
            "✅ Сторис готова! Фото выше можно скачать и использовать.",
            reply_markup=main_menu_kb()
        )
        user_state[uid] = {"step": "idle"}

    elif call.data == "cancel_story":
        bot.answer_callback_query(call.id, "Отменено")
        bot.delete_message(call.message.chat.id, call.message.message_id)
        bot.send_message(
            call.message.chat.id,
            "❌ Создание сторис отменено",
            reply_markup=main_menu_kb()
        )
        user_state[uid] = {"step": "idle"}

    # Улучшение фото
    elif call.data == "enhance_another":
        st["step"] = "waiting_enhance_photo"
        user_state[uid] = st
        bot.answer_callback_query(call.id, "Ок")
        bot.send_message(
            call.message.chat.id,
            "✨ Отправь следующее фото для улучшения:",
            reply_markup=main_menu_kb()
        )
        
    elif call.data == "enhance_main_menu":
        user_state[uid] = {"step": "idle"}
        bot.answer_callback_query(call.id, "Главное меню")
        bot.send_message(
            call.message.chat.id,
            "🏠 Главное меню",
            reply_markup=main_menu_kb()
        )


# =========================
# Message handlers
# =========================
@bot.message_handler(commands=["start", "help"])
def cmd_start(message):
    uid = message.from_user.id
    user_state[uid] = {"step": "idle"}

    bot.send_message(
        message.chat.id,
        "👋 <b>Добро пожаловать в Feeder Bot!</b>\n\n"
        "<b>📝 Доступные функции:</b>\n"
        "• Оформление постов в стиле ФДР (шрифт Inter Black)\n"
        "• Оформление сторис в стиле ФДР (шрифт Inter Black)\n"
        "• Улучшение качества фото\n\n"
        "Выбери действие 👇",
        parse_mode="HTML",
        reply_markup=main_menu_kb()
    )


@bot.message_handler(commands=["post"])
def cmd_post(message):
    uid = message.from_user.id
    user_state[uid] = {"step": "waiting_post_photo", "type": "post"}
    bot.send_message(
        message.chat.id, 
        "📸 <b>Создание поста (шрифт Inter Black)</b>\n\nПришли фото для поста:", 
        parse_mode="HTML",
        reply_markup=main_menu_kb()
    )


@bot.message_handler(commands=["story"])
def cmd_story(message):
    uid = message.from_user.id
    user_state[uid] = {"step": "waiting_story_photo", "type": "story"}
    bot.send_message(
        message.chat.id, 
        "📱 <b>Создание сторис (шрифт Inter Black)</b>\n\nПришли фото для сторис:", 
        parse_mode="HTML",
        reply_markup=main_menu_kb()
    )


@bot.message_handler(commands=["enhance"])
def cmd_enhance(message):
    uid = message.from_user.id
    user_state[uid] = {"step": "waiting_enhance_photo"}
    bot.send_message(
        message.chat.id,
        "✨ <b>Улучшение качества фото</b>\n\n"
        "Отправь фото, и я:\n"
        "• 🔍 Увеличу резкость на +20%\n"
        "• 🎨 Увеличу насыщенность на +15%\n\n"
        "<i>Лучше отправлять фото как файл (документ) для сохранения качества</i>",
        parse_mode="HTML",
        reply_markup=main_menu_kb()
    )


@bot.message_handler(commands=["stop"])
def cmd_stop(message):
    uid = message.from_user.id
    user_state[uid] = {"step": "idle"}
    bot.send_message(
        message.chat.id, 
        "🛑 Бот сброшен в исходное состояние.", 
        reply_markup=main_menu_kb()
    )


@bot.message_handler(func=lambda message: message.text == BTN_POST)
def handle_post_button(message):
    cmd_post(message)


@bot.message_handler(func=lambda message: message.text == BTN_STORY)
def handle_story_button(message):
    cmd_story(message)


@bot.message_handler(func=lambda message: message.text == BTN_ENHANCE)
def handle_enhance_button(message):
    cmd_enhance(message)


@bot.message_handler(content_types=["photo", "document"])
def on_photo_or_document(message):
    uid = message.from_user.id
    st = user_state.get(uid) or {"step": "idle"}
    
    step = st.get("step")
    
    # Улучшение фото
    if step == "waiting_enhance_photo":
        try:
            if message.content_type == "photo":
                file_id = message.photo[-1].file_id
            else:
                doc = message.document
                if not doc.mime_type or not doc.mime_type.startswith("image/"):
                    bot.reply_to(message, "❌ Это не изображение. Отправь JPG или PNG файл.")
                    return
                file_id = doc.file_id
            
            photo_bytes = tg_file_bytes(file_id)
            
            if not check_file_size(photo_bytes):
                bot.reply_to(message, "❌ Файл слишком большой. Максимум 20MB.")
                return
            
            processing_msg = bot.reply_to(message, "⏳ Улучшаю качество...")
            
            enhanced = enhance_image_simple(photo_bytes)
            
            bot.send_document(
                message.chat.id,
                document=enhanced,
                visible_file_name="enhanced_photo.jpg",
                caption="✨ Фото улучшено!\n\n• Резкость +20%\n• Насыщенность +15%",
                reply_markup=enhance_menu_kb()
            )
            
            bot.delete_message(message.chat.id, processing_msg.message_id)
            return
            
        except Exception as e:
            logger.error(f"Error enhancing photo: {e}")
            bot.reply_to(message, f"❌ Ошибка при улучшении: {e}")
            return
    
    # Обработка фото для поста
    if step == "waiting_post_photo":
        try:
            if message.content_type == "photo":
                file_id = message.photo[-1].file_id
            else:
                file_id = message.document.file_id
            
            photo_bytes = tg_file_bytes(file_id)

            if not check_file_size(photo_bytes):
                bot.reply_to(message, "❌ Файл слишком большой. Максимальный размер 20MB.")
                return

            warn_if_too_small(message.chat.id, photo_bytes)

            st["photo_bytes"] = photo_bytes
            st["step"] = "waiting_post_title"
            user_state[uid] = st

            bot.reply_to(message, "📸 Фото сохранено!\n\nТеперь отправь <b>ЗАГОЛОВОК</b> поста (будет использован шрифт Inter Black):", parse_mode="HTML")
            return
        except Exception as e:
            logger.error(f"Error processing photo for post: {e}")
            bot.reply_to(message, f"❌ Ошибка при обработке фото: {e}")
            return
    
    # Обработка фото для сторис
    if step == "waiting_story_photo":
        try:
            if message.content_type == "photo":
                file_id = message.photo[-1].file_id
            else:
                file_id = message.document.file_id
            
            photo_bytes = tg_file_bytes(file_id)

            if not check_file_size(photo_bytes):
                bot.reply_to(message, "❌ Файл слишком большой. Максимальный размер 20MB.")
                return

            warn_if_too_small(message.chat.id, photo_bytes)

            st["photo_bytes"] = photo_bytes
            st["step"] = "waiting_story_title"
            user_state[uid] = st

            bot.reply_to(message, "📸 Фото сохранено!\n\nТеперь отправь <b>ЗАГОЛОВОК</b> для сторис (шрифт Inter Black):", parse_mode="HTML")
            return
        except Exception as e:
            logger.error(f"Error processing photo for story: {e}")
            bot.reply_to(message, f"❌ Ошибка при обработке фото: {e}")
            return
    
    # Если пользователь не в нужном состоянии
    else:
        bot.reply_to(message, "Выбери действие в меню 👇", reply_markup=main_menu_kb())


@bot.message_handler(content_types=["text"])
def on_text(message):
    uid = message.from_user.id
    text = (message.text or "").strip()
    st = user_state.get(uid) or {"step": "idle"}
    
    # Обработка кнопок главного меню
    if text == BTN_POST:
        cmd_post(message)
        return
    if text == BTN_STORY:
        cmd_story(message)
        return
    if text == BTN_ENHANCE:
        cmd_enhance(message)
        return
    
    step = st.get("step")

    # === Обработка поста ФДР ===
    if step == "waiting_post_title":
        if not text:
            bot.reply_to(message, "❌ Заголовок не может быть пустым. Отправь текст:")
            return
        
        st["title"] = text
        st["step"] = "waiting_post_highlight"
        user_state[uid] = st
        
        bot.reply_to(
            message, 
            f"✅ Заголовок сохранён! (шрифт Inter Black)\n\n<b>{html.escape(text)}</b>\n\n🎯 Теперь отправь <b>ФРАЗУ</b>, которую нужно выделить фиолетовой плашкой:", 
            parse_mode="HTML"
        )
        return
    
    if step == "waiting_post_highlight":
        if not text:
            bot.reply_to(message, "❌ Фраза не может быть пустой. Отправь текст:")
            return
        
        st["highlight_phrase"] = text
        st["step"] = "waiting_post_body"
        user_state[uid] = st
        
        bot.reply_to(message, f"✅ Фраза сохранена!\n\nТеперь отправь <b>ОСНОВНОЙ ТЕКСТ</b> поста:", parse_mode="HTML")
        return
    
    if step == "waiting_post_body":
        if not text:
            bot.reply_to(message, "❌ Текст не может быть пустым. Отправь текст:")
            return
        
        st["body_raw"] = text
        
        try:
            # Создаем карточку с шрифтом Inter Black
            card = make_fdr_post_card(st["photo_bytes"], st["title"], st["highlight_phrase"])
            
            # Отправляем готовое фото
            caption = build_caption_html(st["title"], st["body_raw"])
            msg = bot.send_photo(
                chat_id=message.chat.id, 
                photo=card, 
                caption=caption, 
                parse_mode="HTML", 
                reply_markup=post_preview_kb()
            )
            bot.send_message(
                message.chat.id, 
                "✅ Пост готов! Фото выше можно скачать. Кнопки ниже помогут что-то изменить.",
                reply_markup=main_menu_kb()
            )
            
            # Сохраняем данные для возможного редактирования
            st["step"] = "waiting_post_action"
            st["card_bytes"] = card.getvalue()
            st["preview_message_id"] = msg.message_id
            user_state[uid] = st
            
        except Exception as e:
            logger.error(f"Error creating post card: {e}")
            bot.reply_to(message, f"❌ Ошибка при создании карточки: {e}")
            st["step"] = "waiting_post_photo"
            user_state[uid] = st
        return
    
    # Редактирование поста
    if step == "waiting_post_title" and st.get("card_bytes"):
        st["title"] = text
        try:
            card = make_fdr_post_card(st["photo_bytes"], st["title"], st["highlight_phrase"])
            st["card_bytes"] = card.getvalue()
            
            caption = build_caption_html(st["title"], st["body_raw"])
            
            # Обновляем существующее сообщение с фото
            bot.edit_message_media(
                chat_id=message.chat.id,
                message_id=st.get("preview_message_id"),
                media=telebot.types.InputMediaPhoto(card, caption=caption, parse_mode="HTML")
            )
            
            st["step"] = "waiting_post_action"
            user_state[uid] = st
            bot.reply_to(message, "✅ Заголовок обновлён!")
        except Exception as e:
            logger.error(f"Error updating post title: {e}")
            bot.reply_to(message, f"❌ Ошибка при обновлении: {e}")
        return
    
    if step == "waiting_post_highlight" and st.get("card_bytes"):
        st["highlight_phrase"] = text
        try:
            card = make_fdr_post_card(st["photo_bytes"], st["title"], st["highlight_phrase"])
            st["card_bytes"] = card.getvalue()
            
            caption = build_caption_html(st["title"], st["body_raw"])
            
            bot.edit_message_media(
                chat_id=message.chat.id,
                message_id=st.get("preview_message_id"),
                media=telebot.types.InputMediaPhoto(card, caption=caption, parse_mode="HTML")
            )
            
            st["step"] = "waiting_post_action"
            user_state[uid] = st
            bot.reply_to(message, "✅ Фраза обновлена!")
        except Exception as e:
            logger.error(f"Error updating post highlight: {e}")
            bot.reply_to(message, f"❌ Ошибка при обновлении: {e}")
        return
    
    if step == "waiting_post_body" and st.get("card_bytes"):
        st["body_raw"] = text
        try:
            card = make_fdr_post_card(st["photo_bytes"], st["title"], st["highlight_phrase"])
            st["card_bytes"] = card.getvalue()
            
            caption = build_caption_html(st["title"], st["body_raw"])
            
            bot.edit_message_media(
                chat_id=message.chat.id,
                message_id=st.get("preview_message_id"),
                media=telebot.types.InputMediaPhoto(card, caption=caption, parse_mode="HTML")
            )
            
            st["step"] = "waiting_post_action"
            user_state[uid] = st
            bot.reply_to(message, "✅ Текст обновлён!")
        except Exception as e:
            logger.error(f"Error updating post body: {e}")
            bot.reply_to(message, f"❌ Ошибка при обновлении: {e}")
        return
    
    # === Обработка сторис ФДР ===
    if step == "waiting_story_title":
        if not text:
            bot.reply_to(message, "❌ Заголовок не может быть пустым. Отправь текст:")
            return
        
        st["title"] = text
        st["step"] = "waiting_story_body"
        user_state[uid] = st
        
        bot.reply_to(message, f"✅ Заголовок сохранён! (шрифт Inter Black)\n\nТеперь отправь <b>ОСНОВНОЙ ТЕКСТ</b> для сторис:", parse_mode="HTML")
        return
    
    if step == "waiting_story_body":
        if not text:
            bot.reply_to(message, "❌ Текст не может быть пустым. Отправь текст:")
            return
        
        st["body_raw"] = text
        
        try:
            # Создаем карточку для сторис с шрифтом Inter Black
            card = make_fdr_story_card(st["photo_bytes"], st["title"], st["body_raw"])
            
            # Отправляем готовое фото
            caption = build_story_caption_html(st["title"], st["body_raw"])
            msg = bot.send_photo(
                chat_id=message.chat.id, 
                photo=card, 
                caption=caption, 
                parse_mode="HTML", 
                reply_markup=story_preview_kb()
            )
            bot.send_message(
                message.chat.id, 
                "✅ Сторис готова! Фото выше можно скачать. Кнопки ниже помогут что-то изменить.",
                reply_markup=main_menu_kb()
            )
            
            # Сохраняем данные для возможного редактирования
            st["step"] = "waiting_story_action"
            st["card_bytes"] = card.getvalue()
            st["preview_message_id"] = msg.message_id
            user_state[uid] = st
            
        except Exception as e:
            logger.error(f"Error creating story card: {e}")
            bot.reply_to(message, f"❌ Ошибка при создании сторис: {e}")
            st["step"] = "waiting_story_photo"
            user_state[uid] = st
        return
    
    # Редактирование сторис
    if step == "waiting_story_title" and st.get("card_bytes"):
        st["title"] = text
        try:
            card = make_fdr_story_card(st["photo_bytes"], st["title"], st["body_raw"])
            st["card_bytes"] = card.getvalue()
            
            caption = build_story_caption_html(st["title"], st["body_raw"])
            
            bot.edit_message_media(
                chat_id=message.chat.id,
                message_id=st.get("preview_message_id"),
                media=telebot.types.InputMediaPhoto(card, caption=caption, parse_mode="HTML")
            )
            
            st["step"] = "waiting_story_action"
            user_state[uid] = st
            bot.reply_to(message, "✅ Заголовок обновлён!")
        except Exception as e:
            logger.error(f"Error updating story title: {e}")
            bot.reply_to(message, f"❌ Ошибка при обновлении: {e}")
        return
    
    if step == "waiting_story_body" and st.get("card_bytes"):
        st["body_raw"] = text
        try:
            card = make_fdr_story_card(st["photo_bytes"], st["title"], st["body_raw"])
            st["card_bytes"] = card.getvalue()
            
            caption = build_story_caption_html(st["title"], st["body_raw"])
            
            bot.edit_message_media(
                chat_id=message.chat.id,
                message_id=st.get("preview_message_id"),
                media=telebot.types.InputMediaPhoto(card, caption=caption, parse_mode="HTML")
            )
            
            st["step"] = "waiting_story_action"
            user_state[uid] = st
            bot.reply_to(message, "✅ Текст обновлён!")
        except Exception as e:
            logger.error(f"Error updating story body: {e}")
            bot.reply_to(message, f"❌ Ошибка при обновлении: {e}")
        return
    
    # Если пользователь не в нужном состоянии
    else:
        bot.send_message(message.chat.id, "Выбери действие 👇", reply_markup=main_menu_kb())


# =========================
# Main execution
# =========================
if __name__ == "__main__":
    logger.info("Starting Feeder Bot...")
    try:
        ensure_fonts()
        logger.info("Fonts loaded successfully")
        
        http_thread = threading.Thread(target=run_http_server, daemon=True)
        http_thread.start()
        logger.info("🌐 Health check server thread started")
        
        logger.info("🤖 Feeder Bot started polling...")
        bot.infinity_polling(timeout=60, long_polling_timeout=60, logger_level=logging.ERROR)
    except Exception as e:
        logger.error(f"❌ Bot crashed: {e}")
        try:
            if os.path.exists(lock_file):
                os.unlink(lock_file)
        except:
            pass
        raise
