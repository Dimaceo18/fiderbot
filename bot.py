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
from openai import OpenAI

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
DEEPSEEK_API_KEY = (os.getenv("DEEPSEEK_API_KEY") or "").strip()

if not TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")
if " " in TOKEN:
    raise ValueError("BOT_TOKEN must not contain spaces")
if not DEEPSEEK_API_KEY:
    logger.warning("DEEPSEEK_API_KEY is not set. AI adaptation will not work.")

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
# FONTS
# =========================
FONT_MAIN = "Inter-Black.ttf"

TARGET_W, TARGET_H = 750, 938
CHP_GRADIENT_PCT = 0.48


# =========================
# BOT + SESSION
# =========================
bot = telebot.TeleBot(TOKEN)

SESSION = requests.Session()
retry_strategy = Retry(total=0, backoff_factor=0, status_forcelist=[])
adapter = HTTPAdapter(max_retries=retry_strategy, pool_connections=10, pool_maxsize=10)
SESSION.mount("http://", adapter)
SESSION.mount("https://", adapter)
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
})

URL_RE = re.compile(r"(https?://[^\s]+)", re.IGNORECASE)

user_state: Dict[int, Dict] = {}

# DeepSeek client
deepseek_client = None
if DEEPSEEK_API_KEY:
    deepseek_client = OpenAI(
        api_key=DEEPSEEK_API_KEY,
        base_url="https://api.deepseek.com"
    )


# =========================
# AI: адаптация новости
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
    """Отправляет текст в DeepSeek и возвращает короткую новость (одна строка)."""
    if not deepseek_client:
        logger.error("DeepSeek client is not initialized")
        return None

    try:
        response = deepseek_client.chat.completions.create(
            model="deepseek-flash",
            messages=[
                {"role": "system", "content": AI_NEWS_PROMPT},
                {"role": "user", "content": text}
            ],
            temperature=0.3,
            max_tokens=300,
            stream=False
        )
        result = response.choices[0].message.content.strip()
        # Убираем возможные кавычки по краям
        result = result.strip('"').strip("'").strip()
        if len(result) > 250:
            result = result[:247] + "..."
        return result
    except Exception as e:
        logger.error(f"DeepSeek API error: {e}")
        return None


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


def extract_source_url(text: str) -> str:
    m = URL_RE.search(text or "")
    return m.group(1) if m else ""


def ensure_fonts():
    if not os.path.exists(FONT_MAIN):
        raise RuntimeError(f"Font not found: {FONT_MAIN}")


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


def split_title_and_body(text: str) -> Tuple[str, str]:
    text = (text or "").strip()
    if not text:
        return "", ""
    parts = text.split("\n", 1)
    title = parts[0].strip()
    body = parts[1].strip() if len(parts) > 1 else ""
    return title, body


def extract_repost_content(message) -> Optional[Dict]:
    """Извлекает (title, body, photo_bytes) из репоста/поста."""
    caption = (message.caption or message.text or "").strip()
    if not caption:
        return None

    photo_bytes = None
    try:
        if message.content_type == "photo":
            photo_bytes = tg_file_bytes(message.photo[-1].file_id)
        elif message.content_type == "document" and message.document:
            mime = message.document.mime_type or ""
            if mime.startswith("image/"):
                photo_bytes = tg_file_bytes(message.document.file_id)
    except Exception as e:
        logger.error(f"extract_repost_content: cannot get photo: {e}")
        return None

    if not photo_bytes:
        return None

    title, body = split_title_and_body(caption)
    return {"title": title, "body": body, "photo_bytes": photo_bytes}


# =========================
# Image enhancement
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
        logger.error(f"Error enhancing image: {e}")
        output = BytesIO(image_bytes)
        output.seek(0)
        return output


# =========================
# Gradient
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
# Text helpers
# =========================
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
    max_w, max_h = x2 - x1, y2 - y1
    selected_font = ImageFont.truetype(FONT_MAIN, min_size)
    selected_gap, selected_paragraph_gap = 8, 12
    for size in range(max_size, min_size - 1, -1):
        font = ImageFont.truetype(FONT_MAIN, size)
        lines = _wrap_text_preserve_paragraphs(draw, text, font, max_w)
        if not lines:
            continue
        line_h = font.getbbox("Ag")[3] - font.getbbox("Ag")[1]
        gap = max(4, int(line_h * line_gap_ratio))
        paragraph_gap = max(gap + 2, int(line_h * paragraph_gap_ratio))
        total_h, max_line_w = 0, 0
        for line in lines:
            if line == "":
                total_h += paragraph_gap
                continue
            lw = font.getbbox(line)[2] - font.getbbox(line)[0]
            max_line_w = max(max_line_w, lw)
            total_h += line_h + gap
        if total_h <= max_h and max_line_w <= max_w:
            selected_font, selected_gap, selected_paragraph_gap = font, gap, paragraph_gap
            break
    return selected_font, selected_gap, selected_paragraph_gap


def _draw_story_text(draw, text, box, font, fill=(255, 255, 255), align="center", valign="center",
                     line_gap=10, paragraph_gap_extra=10):
    x1, y1, x2, y2 = box
    max_w, max_h = x2 - x1, y2 - y1
    lines = _wrap_text_preserve_paragraphs(draw, text, font, max_w)
    if not lines:
        return
    line_h = font.getbbox("Ag")[3] - font.getbbox("Ag")[1]
    total_h = 0
    for line in lines:
        total_h += paragraph_gap_extra if line == "" else line_h + line_gap
    y = y1 if valign == "top" else y1 + (max_h - total_h) // 2
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
# Card: пост
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
    title_text_upper = title_text.strip().upper()
    highlight_phrase_upper = highlight_phrase.strip().upper()
    highlight_words = set(highlight_phrase_upper.split())
    title_max_h = int(img.height * 0.23)
    font, lines, heights, spacing, total_h = fit_text_block(
        draw=draw, text=title_text_upper, font_path=FONT_MAIN, safe_w=safe_w,
        max_block_h=title_max_h, max_lines=6, start_size=int(img.height * 0.11),
        min_size=16, line_spacing_ratio=0.22
    )
    base_y = img.height - margin_bottom - total_h
    y = base_y
    for line_idx, line in enumerate(lines):
        line_words = line.split()
        current_x = margin_x
        for word in line_words:
            word_bbox = draw.textbbox((current_x, y), word, font=font)
            word_x1, word_y1, word_x2, word_y2 = word_bbox
            if word in highlight_words:
                padding = 10
                draw.rectangle([word_x1 - padding, word_y1 - padding,
                                word_x2 + padding, word_y2 + padding],
                               fill=FDR_POST_PURPLE_COLOR)
            if word != line_words[-1]:
                space_width = text_width(draw, " ", font)
                current_x += text_width(draw, word, font) + space_width
            else:
                current_x += text_width(draw, word, font)
        y += heights[line_idx] + spacing
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
    out = BytesIO()
    img.save(out, format="JPEG", quality=95, subsampling=0, optimize=True)
    out.seek(0)
    return out


# =========================
# Card: сторис (старый формат)
# =========================
def make_fdr_story_card(photo_bytes: bytes, title: str, body_text: str) -> BytesIO:
    ensure_fonts()
    canvas = Image.new("RGB", (FDR_STORY_W, FDR_STORY_H), (0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    photo_h, header_h = 410, 220
    photo = Image.open(BytesIO(photo_bytes)).convert("RGB")

    def fit_cover(im, target_w, target_h):
        src_w, src_h = im.size
        scale = max(target_w / src_w, target_h / src_h)
        nw, nh = int(src_w * scale), int(src_h * scale)
        resized = im.resize((nw, nh), Image.LANCZOS)
        left, top = max(0, (nw - target_w) // 2), max(0, (nh - target_h) // 2)
        return resized.crop((left, top, left + target_w, top + target_h))

    story_photo = fit_cover(photo, FDR_STORY_W, photo_h)
    canvas.paste(story_photo, (0, 0))
    purple_color = (122, 58, 240)
    canvas.paste(Image.new("RGB", (FDR_STORY_W, header_h), purple_color), (0, photo_h))
    draw.rectangle([0, photo_h + header_h, FDR_STORY_W, FDR_STORY_H], fill=(0, 0, 0))
    padding = 34
    header_box = (padding, photo_h + padding, FDR_STORY_W - padding, photo_h + header_h - padding)
    body_box = (padding, photo_h + header_h + padding, FDR_STORY_W - padding, FDR_STORY_H - padding)
    title_font, title_gap, title_paragraph_gap = _fit_story_text(
        draw, title, header_box, min_size=28, max_size=54,
        line_gap_ratio=0.08, paragraph_gap_ratio=0.18
    )
    _draw_story_text(draw, title, header_box, title_font, fill=(255, 255, 255),
                     align="center", valign="center", line_gap=title_gap,
                     paragraph_gap_extra=title_paragraph_gap)
    body_font, body_gap, body_paragraph_gap = _fit_story_text(
        draw, body_text, body_box, min_size=14, max_size=30,
        line_gap_ratio=0.10, paragraph_gap_ratio=0.32
    )
    _draw_story_text(draw, body_text, body_box, body_font, fill=(255, 255, 255),
                     align="left", valign="top", line_gap=body_gap,
                     paragraph_gap_extra=body_paragraph_gap)
    out = BytesIO()
    canvas.save(out, format="JPEG", quality=92, optimize=True)
    out.seek(0)
    return out


# =========================
# Card: сторис-новость (НОВЫЙ ФОРМАТ)
# фото по центру, ниже новость крупным чёрным шрифтом,
# выделенная фраза — фиолетовым
# =========================
def make_news_story_card(photo_bytes: bytes, news_text: str, highlight_phrase: str) -> BytesIO:
    ensure_fonts()
    canvas = Image.new("RGB", (FDR_STORY_W, FDR_STORY_H), (255, 255, 255))
    draw = ImageDraw.Draw(canvas)

    # Фото по центру (квадрат ~720x720)
    photo = Image.open(BytesIO(photo_bytes)).convert("RGB")
    photo_size = 640
    photo_top = 120

    def fit_cover(im, target_w, target_h):
        src_w, src_h = im.size
        scale = max(target_w / src_w, target_h / src_h)
        nw, nh = int(src_w * scale), int(src_h * scale)
        resized = im.resize((nw, nh), Image.LANCZOS)
        left, top = max(0, (nw - target_w) // 2), max(0, (nh - target_h) // 2)
        return resized.crop((left, top, left + target_w, top + target_h))

    story_photo = fit_cover(photo, photo_size, photo_size)
    photo_left = (FDR_STORY_W - photo_size) // 2
    canvas.paste(story_photo, (photo_left, photo_top))

    # Область под новость
    news_top = photo_top + photo_size + 60
    news_box = (50, news_top, FDR_STORY_W - 50, FDR_STORY_H - 80)

    # Подбираем шрифт
    news_font, news_gap, news_paragraph_gap = _fit_story_text(
        draw, news_text.upper(), news_box, min_size=24, max_size=52,
        line_gap_ratio=0.12, paragraph_gap_ratio=0.30
    )

    # Рисуем новость с выделением фиолетовым
    highlight_upper = highlight_phrase.strip().upper()
    highlight_words = set(highlight_upper.split()) if highlight_upper else set()

    lines = _wrap_text_preserve_paragraphs(draw, news_text.upper(), news_font, news_box[2] - news_box[0])
    line_h = news_font.getbbox("Ag")[3] - news_font.getbbox("Ag")[1]

    # Вычисляем общую высоту для центрирования
    total_h = 0
    for line in lines:
        total_h += news_paragraph_gap if line == "" else line_h + news_gap
    y = news_box[1] + (news_box[3] - news_box[1] - total_h) // 2

    for line in lines:
        if line == "":
            y += news_paragraph_gap
            continue
        words = line.split()
        # Вычисляем ширину строки
        line_w = 0
        for i, w in enumerate(words):
            line_w += text_width(draw, w, news_font)
            if i < len(words) - 1:
                line_w += text_width(draw, " ", news_font)
        # Центрируем
        x = news_box[0] + (news_box[2] - news_box[0] - line_w) // 2
        for i, w in enumerate(words):
            color = FDR_POST_PURPLE_COLOR if w in highlight_words else (0, 0, 0)
            draw.text((x, y), w, font=news_font, fill=color)
            x += text_width(draw, w, news_font)
            if i < len(words) - 1:
                x += text_width(draw, " ", news_font)
        y += line_h + news_gap

    out = BytesIO()
    canvas.save(out, format="JPEG", quality=92, optimize=True)
    out.seek(0)
    return out


# =========================
# Caption
# =========================
def build_caption_html(title: str, body: str) -> str:
    title_safe = html.escape((title or "").strip())
    body_preview = (body or "").strip()[:200] + "..." if len(body or "") > 200 else (body or "")
    return f"<b>{title_safe}</b>\n\n{html.escape(body_preview)}"


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
@bot.callback_query_handler(func=lambda c: True)
def on_action(call):
    uid = call.from_user.id
    st = user_state.get(uid)

    if not st:
        bot.answer_callback_query(call.id, "Сессия устарела. Начни заново.")
        return

    # === AI выбор ===
    if call.data == "ai_keep_original":
        # Оставляем оригинальный текст → сразу к формату
        bot.answer_callback_query(call.id, "Ок, оригинальный текст")
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text="📌 Выбери формат:",
            reply_markup=format_choice_kb()
        )
        st["ai_choice"] = "original"
        user_state[uid] = st
        return

    if call.data == "ai_adapt":
        # Адаптируем через ИИ
        bot.answer_callback_query(call.id, "🤖 Адаптирую через ИИ...")
        original_text = st.get("body_raw") or st.get("title") or ""
        if not original_text:
            bot.send_message(call.message.chat.id, "❌ Нет текста для адаптации.")
            return

        adapted = adapt_news_with_ai(original_text)
        if not adapted:
            bot.send_message(call.message.chat.id, "❌ Не удалось адаптировать текст. Попробуй позже.")
            return

        st["adapted_text"] = adapted
        st["ai_choice"] = "adapted"
        user_state[uid] = st

        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=f"🤖 <b>Новость (одна строка):</b>\n\n<i>{html.escape(adapted)}</i>\n\n📌 Выбери формат:",
            parse_mode="HTML",
            reply_markup=format_choice_kb()
        )
        return

    # === Выбор формата ===
    if call.data == "format_post":
        bot.answer_callback_query(call.id, "📝 Пост")
        text_for_card = st.get("adapted_text") if st.get("ai_choice") == "adapted" else st.get("title", "")
        # Если адаптировано — заголовок = новость, body = пусто (или тот же)
        if st.get("ai_choice") == "adapted":
            st["title"] = st["adapted_text"]
            st["body_raw"] = ""
        st["step"] = "waiting_post_highlight"
        user_state[uid] = st
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text="💜 Пришли <b>ФРАЗУ</b>, которую нужно выделить фиолетовой плашкой (или напиши «нет»):",
            parse_mode="HTML"
        )
        return

    if call.data == "format_story":
        bot.answer_callback_query(call.id, "📱 Сторис")
        if st.get("ai_choice") == "adapted":
            st["news_text"] = st["adapted_text"]
        else:
            # Для оригинала в сторис-новости берём весь текст
            st["news_text"] = st.get("body_raw") or st.get("title", "")
        st["step"] = "waiting_news_highlight"
        user_state[uid] = st
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text="💜 Пришли <b>ФРАЗУ</b> для выделения фиолетовым (или напиши «нет»):",
            parse_mode="HTML"
        )
        return

    # === Редактирование поста ===
    if call.data == "edit_post_body":
        st["step"] = "waiting_post_body"
        user_state[uid] = st
        bot.answer_callback_query(call.id, "Ок")
        bot.edit_message_caption(chat_id=call.message.chat.id, message_id=call.message.message_id,
                                 caption="📝 Пришли новый ОСНОВНОЙ ТЕКСТ для поста:")
    elif call.data == "edit_post_title":
        st["step"] = "waiting_post_title"
        user_state[uid] = st
        bot.answer_callback_query(call.id, "Ок")
        bot.edit_message_caption(chat_id=call.message.chat.id, message_id=call.message.message_id,
                                 caption="📝 Пришли новый ЗАГОЛОВОК для поста:")
    elif call.data == "edit_post_highlight":
        st["step"] = "waiting_post_highlight"
        user_state[uid] = st
        bot.answer_callback_query(call.id, "Ок")
        bot.edit_message_caption(chat_id=call.message.chat.id, message_id=call.message.message_id,
                                 caption="💜 Пришли новую ФРАЗУ для выделения фиолетовой плашкой:")
    elif call.data == "restart_post":
        user_state[uid] = {"step": "waiting_post_photo", "type": "post"}
        bot.answer_callback_query(call.id, "Начинаем заново")
        bot.delete_message(call.message.chat.id, call.message.message_id)
        bot.send_message(call.message.chat.id, "🔄 Начинаем создание поста заново.\n\n📸 Пришли фото:",
                         reply_markup=main_menu_kb())
    elif call.data == "finish_post":
        bot.answer_callback_query(call.id, "Готово ✅")
        bot.send_message(call.message.chat.id, "✅ Пост готов!", reply_markup=main_menu_kb())
        user_state[uid] = {"step": "idle"}
    elif call.data == "cancel_post":
        bot.answer_callback_query(call.id, "Отменено")
        bot.delete_message(call.message.chat.id, call.message.message_id)
        bot.send_message(call.message.chat.id, "❌ Отменено", reply_markup=main_menu_kb())
        user_state[uid] = {"step": "idle"}

    # === Редактирование сторис-новости ===
    elif call.data == "edit_news_text":
        st["step"] = "waiting_news_text"
        user_state[uid] = st
        bot.answer_callback_query(call.id, "Ок")
        bot.edit_message_caption(chat_id=call.message.chat.id, message_id=call.message.message_id,
                                 caption="📝 Пришли новый текст новости (одна строка):")
    elif call.data == "edit_news_highlight":
        st["step"] = "waiting_news_highlight"
        user_state[uid] = st
        bot.answer_callback_query(call.id, "Ок")
        bot.edit_message_caption(chat_id=call.message.chat.id, message_id=call.message.message_id,
                                 caption="💜 Пришли новую ФРАЗУ для выделения:")
    elif call.data == "restart_news_story":
        user_state[uid] = {"step": "waiting_post_photo", "type": "post"}
        bot.answer_callback_query(call.id, "Начинаем заново")
        bot.delete_message(call.message.chat.id, call.message.message_id)
        bot.send_message(call.message.chat.id, "🔄 Начинаем заново.\n\n📸 Пришли фото:",
                         reply_markup=main_menu_kb())
    elif call.data == "finish_news_story":
        bot.answer_callback_query(call.id, "Готово ✅")
        bot.send_message(call.message.chat.id, "✅ Сторис готова!", reply_markup=main_menu_kb())
        user_state[uid] = {"step": "idle"}
    elif call.data == "cancel_news_story":
        bot.answer_callback_query(call.id, "Отменено")
        bot.delete_message(call.message.chat.id, call.message.message_id)
        bot.send_message(call.message.chat.id, "❌ Отменено", reply_markup=main_menu_kb())
        user_state[uid] = {"step": "idle"}

    # === Сторис (старый) ===
    elif call.data == "edit_story_body":
        st["step"] = "waiting_story_body"
        user_state[uid] = st
        bot.answer_callback_query(call.id, "Ок")
        bot.edit_message_caption(chat_id=call.message.chat.id, message_id=call.message.message_id,
                                 caption="📝 Пришли новый ОСНОВНОЙ ТЕКСТ для сторис:")
    elif call.data == "edit_story_title":
        st["step"] = "waiting_story_title"
        user_state[uid] = st
        bot.answer_callback_query(call.id, "Ок")
        bot.edit_message_caption(chat_id=call.message.chat.id, message_id=call.message.message_id,
                                 caption="📝 Пришли новый ЗАГОЛОВОК для сторис:")
    elif call.data == "restart_story":
        user_state[uid] = {"step": "waiting_story_photo", "type": "story"}
        bot.answer_callback_query(call.id, "Начинаем заново")
        bot.delete_message(call.message.chat.id, call.message.message_id)
        bot.send_message(call.message.chat.id, "🔄 Начинаем создание сторис заново.\n\n📸 Пришли фото:",
                         reply_markup=main_menu_kb())
    elif call.data == "finish_story":
        bot.answer_callback_query(call.id, "Готово ✅")
        bot.send_message(call.message.chat.id, "✅ Сторис готова!", reply_markup=main_menu_kb())
        user_state[uid] = {"step": "idle"}
    elif call.data == "cancel_story":
        bot.answer_callback_query(call.id, "Отменено")
        bot.delete_message(call.message.chat.id, call.message.message_id)
        bot.send_message(call.message.chat.id, "❌ Отменено", reply_markup=main_menu_kb())
        user_state[uid] = {"step": "idle"}

    # === Улучшение ===
    elif call.data == "enhance_another":
        st["step"] = "waiting_enhance_photo"
        user_state[uid] = st
        bot.answer_callback_query(call.id, "Ок")
        bot.send_message(call.message.chat.id, "✨ Отправь следующее фото:", reply_markup=main_menu_kb())
    elif call.data == "enhance_main_menu":
        user_state[uid] = {"step": "idle"}
        bot.answer_callback_query(call.id, "Главное меню")
        bot.send_message(call.message.chat.id, "🏠 Главное меню", reply_markup=main_menu_kb())


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
        "<b>📝 Доступно:</b>\n"
        "• Оформление постов и сторис\n"
        "• Адаптация текста в короткую новость через ИИ\n"
        "• Улучшение качества фото\n\n"
        "<b>🚀 Быстрый старт:</b> просто перешли пост из канала "
        "или отправь фото с подписью.",
        parse_mode="HTML",
        reply_markup=main_menu_kb()
    )


@bot.message_handler(commands=["post"])
def cmd_post(message):
    uid = message.from_user.id
    user_state[uid] = {"step": "waiting_post_photo", "type": "post"}
    bot.send_message(message.chat.id, "📸 Пришли фото для поста:", reply_markup=main_menu_kb())


@bot.message_handler(commands=["story"])
def cmd_story(message):
    uid = message.from_user.id
    user_state[uid] = {"step": "waiting_story_photo", "type": "story"}
    bot.send_message(message.chat.id, "📸 Пришли фото для сторис:", reply_markup=main_menu_kb())


@bot.message_handler(commands=["enhance"])
def cmd_enhance(message):
    uid = message.from_user.id
    user_state[uid] = {"step": "waiting_enhance_photo"}
    bot.send_message(message.chat.id, "✨ Отправь фото для улучшения:", reply_markup=main_menu_kb())


@bot.message_handler(commands=["stop"])
def cmd_stop(message):
    user_state[message.from_user.id] = {"step": "idle"}
    bot.send_message(message.chat.id, "🛑 Сброшено.", reply_markup=main_menu_kb())


@bot.message_handler(func=lambda m: m.text == BTN_POST)
def handle_post_button(message):
    cmd_post(message)


@bot.message_handler(func=lambda m: m.text == BTN_STORY)
def handle_story_button(message):
    cmd_story(message)


@bot.message_handler(func=lambda m: m.text == BTN_ENHANCE)
def handle_enhance_button(message):
    cmd_enhance(message)


@bot.message_handler(content_types=["photo", "document"])
def on_photo_or_document(message):
    uid = message.from_user.id
    st = user_state.get(uid) or {"step": "idle"}
    step = st.get("step")

    # === АВТО-ПРИЁМ РЕПОСТА ===
    if step in ("idle", "waiting_post_photo", None):
        caption_text = (message.caption or "").strip()
        if caption_text:
            parsed = extract_repost_content(message)
            if parsed:
                st = {
                    "step": "waiting_ai_choice",
                    "type": "post",
                    "photo_bytes": parsed["photo_bytes"],
                    "title": parsed["title"],
                    "body_raw": parsed["body"],
                }
                user_state[uid] = st
                title_safe = html.escape(parsed["title"] or "—")
                body_preview = html.escape((parsed["body"] or "")[:200])
                bot.reply_to(
                    message,
                    "✅ Принял пост!\n\n"
                    f"<b>Заголовок:</b> {title_safe}\n"
                    f"<b>Текст:</b> {body_preview}\n\n"
                    "🤖 Оставить оригинальный текст или адаптировать через ИИ в короткую новость?",
                    parse_mode="HTML",
                    reply_markup=ai_choice_kb()
                )
                return
            else:
                bot.reply_to(message, "⚠️ Нет фото. Пришли пост с картинкой.", reply_markup=main_menu_kb())
                return

    # Улучшение
    if step == "waiting_enhance_photo":
        try:
            if message.content_type == "photo":
                file_id = message.photo[-1].file_id
            else:
                doc = message.document
                if not doc.mime_type or not doc.mime_type.startswith("image/"):
                    bot.reply_to(message, "❌ Это не изображение.")
                    return
                file_id = doc.file_id
            photo_bytes = tg_file_bytes(file_id)
            if not check_file_size(photo_bytes):
                bot.reply_to(message, "❌ Файл > 20MB.")
                return
            processing_msg = bot.reply_to(message, "⏳ Улучшаю...")
            enhanced = enhance_image_simple(photo_bytes)
            bot.send_document(message.chat.id, document=enhanced, visible_file_name="enhanced.jpg",
                              caption="✨ Готово!", reply_markup=main_menu_kb())
            bot.delete_message(message.chat.id, processing_msg.message_id)
            return
        except Exception as e:
            logger.error(f"Enhance error: {e}")
            bot.reply_to(message, f"❌ Ошибка: {e}")
            return

    # Фото для поста (ручной режим)
    if step == "waiting_post_photo":
        try:
            if message.content_type == "photo":
                file_id = message.photo[-1].file_id
            else:
                file_id = message.document.file_id
            photo_bytes = tg_file_bytes(file_id)
            if not check_file_size(photo_bytes):
                bot.reply_to(message, "❌ Файл > 20MB.")
                return
            warn_if_too_small(message.chat.id, photo_bytes)
            st["photo_bytes"] = photo_bytes
            st["step"] = "waiting_post_title"
            user_state[uid] = st
            bot.reply_to(message, "📸 Фото сохранено!\n\nОтправь <b>ЗАГОЛОВОК</b>:", parse_mode="HTML")
            return
        except Exception as e:
            logger.error(f"Post photo error: {e}")
            bot.reply_to(message, f"❌ Ошибка: {e}")
            return

    # Фото для сторис (ручной режим)
    if step == "waiting_story_photo":
        try:
            if message.content_type == "photo":
                file_id = message.photo[-1].file_id
            else:
                file_id = message.document.file_id
            photo_bytes = tg_file_bytes(file_id)
            if not check_file_size(photo_bytes):
                bot.reply_to(message, "❌ Файл > 20MB.")
                return
            warn_if_too_small(message.chat.id, photo_bytes)
            st["photo_bytes"] = photo_bytes
            st["step"] = "waiting_story_title"
            user_state[uid] = st
            bot.reply_to(message, "📸 Фото сохранено!\n\nОтправь <b>ЗАГОЛОВОК</b> для сторис:", parse_mode="HTML")
            return
        except Exception as e:
            logger.error(f"Story photo error: {e}")
            bot.reply_to(message, f"❌ Ошибка: {e}")
            return

    else:
        bot.reply_to(message, "Выбери действие 👇", reply_markup=main_menu_kb())


@bot.message_handler(content_types=["text"])
def on_text(message):
    uid = message.from_user.id
    text = (message.text or "").strip()
    st = user_state.get(uid) or {"step": "idle"}

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

    # === Пост: заголовок ===
    if step == "waiting_post_title":
        if not text:
            bot.reply_to(message, "❌ Пустой заголовок.")
            return
        st["title"] = text
        st["step"] = "waiting_post_highlight"
        user_state[uid] = st
        bot.reply_to(message, f"✅ Заголовок: <b>{html.escape(text)}</b>\n\n💜 Пришли <b>ФРАЗУ</b> для выделения:",
                     parse_mode="HTML")
        return

    # === Пост: выделение ===
    if step == "waiting_post_highlight":
        if not text:
            bot.reply_to(message, "❌ Пустая фраза.")
            return
        st["highlight_phrase"] = text
        if st.get("body_raw"):
            try:
                card = make_fdr_post_card(st["photo_bytes"], st["title"], st["highlight_phrase"])
                caption = build_caption_html(st["title"], st["body_raw"])
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
        bot.reply_to(message, "✅ Фраза сохранена!\n\nТеперь отправь <b>ОСНОВНОЙ ТЕКСТ</b>:", parse_mode="HTML")
        return

    # === Пост: тело ===
    if step == "waiting_post_body":
        if not text:
            bot.reply_to(message, "❌ Пустой текст.")
            return
        st["body_raw"] = text
        try:
            card = make_fdr_post_card(st["photo_bytes"], st["title"], st["highlight_phrase"])
            caption = build_caption_html(st["title"], st["body_raw"])
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
            st["step"] = "waiting_post_photo"
            user_state[uid] = st
        return

    # === Сторис-новость: выделение ===
    if step == "waiting_news_highlight":
        if not text:
            bot.reply_to(message, "❌ Пустая фраза.")
            return
        st["highlight_phrase"] = text if text.lower() != "нет" else ""
        try:
            card = make_news_story_card(st["photo_bytes"], st["news_text"], st.get("highlight_phrase", ""))
            msg = bot.send_photo(message.chat.id, photo=card, caption="📱 Сторис-новость",
                                 reply_markup=news_story_preview_kb())
            bot.send_message(message.chat.id, "✅ Сторис готова!", reply_markup=main_menu_kb())
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
            bot.reply_to(message, "❌ Пустой текст.")
            return
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
            logger.error(f"News text update error: {e}")
            bot.reply_to(message, f"❌ Ошибка: {e}")
        return

    # === Сторис (старый): заголовок ===
    if step == "waiting_story_title":
        if not text:
            bot.reply_to(message, "❌ Пустой заголовок.")
            return
        st["title"] = text
        st["step"] = "waiting_story_body"
        user_state[uid] = st
        bot.reply_to(message, f"✅ Заголовок: <b>{html.escape(text)}</b>\n\nОтправь <b>ОСНОВНОЙ ТЕКСТ</b>:",
                     parse_mode="HTML")
        return

    # === Сторис (старый): тело ===
    if step == "waiting_story_body":
        if not text:
            bot.reply_to(message, "❌ Пустой текст.")
            return
        st["body_raw"] = text
        try:
            card = make_fdr_story_card(st["photo_bytes"], st["title"], st["body_raw"])
            msg = bot.send_photo(message.chat.id, photo=card, caption="📱 Сторис",
                                 reply_markup=story_preview_kb())
            bot.send_message(message.chat.id, "✅ Сторис готова!", reply_markup=main_menu_kb())
            st["step"] = "waiting_story_action"
            st["card_bytes"] = card.getvalue()
            st["preview_message_id"] = msg.message_id
            user_state[uid] = st
        except Exception as e:
            logger.error(f"Story error: {e}")
            bot.reply_to(message, f"❌ Ошибка: {e}")
            st["step"] = "waiting_story_photo"
            user_state[uid] = st
        return

    else:
        bot.send_message(message.chat.id, "Выбери действие 👇", reply_markup=main_menu_kb())


# =========================
# Main
# =========================
if __name__ == "__main__":
    logger.info("Starting Feeder Bot...")
    try:
        ensure_fonts()
        logger.info("Fonts loaded")
        http_thread = threading.Thread(target=run_http_server, daemon=True)
        http_thread.start()
        logger.info("🌐 Health check started")
        logger.info("🤖 Bot polling...")
        bot.infinity_polling(timeout=60, long_polling_timeout=60, logger_level=logging.ERROR)
    except Exception as e:
        logger.error(f"❌ Bot crashed: {e}")
        try:
            if os.path.exists(lock_file):
                os.unlink(lock_file)
        except:
            pass
        raise
