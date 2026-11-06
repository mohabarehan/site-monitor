"""
Site Monitor
Мониторинг сайта с авторизацией (Playwright) -> SQLite -> уведомления в Telegram.

Запуск:
    set TG_TOKEN=ваш_токен
    set TG_CHAT_ID=ваш_chat_id
    python monitor.py          # постоянный мониторинг
    python monitor.py --once   # один проход
"""
import hashlib
import html
import logging
import os
import sqlite3
import sys
import time

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ---------- Настройки ----------
TOKEN = os.getenv("TG_TOKEN")
CHAT_ID = os.getenv("TG_CHAT_ID")
INTERVAL = int(os.getenv("INTERVAL", "300"))  # секунд между проверками

SITE = "https://quotes.toscrape.com"  # учебный сайт для парсинга, есть страница логина
LOGIN = os.getenv("SITE_LOGIN", "demo")
PASSWORD = os.getenv("SITE_PASSWORD", "demo")
MAX_PAGES = 3

DB_PATH = "data.db"
SESSION_PATH = "session.json"  # сохранённая браузерная сессия (cookies)
MAX_RETRIES = 3

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler("monitor.log", encoding="utf-8"), logging.StreamHandler()],
)
log = logging.getLogger("monitor")


# ---------- База данных ----------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS items (
               id TEXT PRIMARY KEY,
               text TEXT NOT NULL,
               author TEXT,
               tags TEXT,
               created_at TEXT DEFAULT CURRENT_TIMESTAMP
           )"""
    )
    conn.commit()
    return conn


def save_new(conn, items):
    """Сохраняет записи, возвращает только новые (которых ещё не было в БД)."""
    new = []
    for it in items:
        cur = conn.execute(
            "INSERT OR IGNORE INTO items (id, text, author, tags) VALUES (?, ?, ?, ?)",
            (it["id"], it["text"], it["author"], it["tags"]),
        )
        if cur.rowcount == 1:
            new.append(it)
    conn.commit()
    return new


# ---------- Telegram ----------
def send_telegram(text):
    if not TOKEN or not CHAT_ID:
        log.warning("TG_TOKEN или TG_CHAT_ID не заданы — уведомление пропущено")
        return
    r = requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"},
        timeout=15,
    )
    if not r.ok:
        log.error("Ошибка Telegram: %s", r.text)


def notify(new_items):
    if not new_items:
        return
    # чтобы не спамить при первом запуске — показываем максимум 5 записей
    for it in new_items[:5]:
        send_telegram(
            f"🆕 <b>{html.escape(it['author'])}</b>\n"
            f"{html.escape(it['text'])}\n"
            f"🏷 {html.escape(it['tags'])}"
        )
    if len(new_items) > 5:
        send_telegram(f"…и ещё {len(new_items) - 5} новых записей")


# ---------- Браузер ----------
def is_logged_in(page):
    return page.locator("a[href='/logout']").count() > 0


def login(page):
    log.info("Авторизация на сайте…")
    page.goto(f"{SITE}/login")
    page.fill("#username", LOGIN)
    page.fill("#password", PASSWORD)
    page.click("input[type=submit]")
    page.wait_for_load_state("networkidle")
    if not is_logged_in(page):
        raise RuntimeError("Не удалось авторизоваться")
    log.info("Авторизация успешна")


def scrape(page):
    items = []
    for n in range(1, MAX_PAGES + 1):
        page.goto(f"{SITE}/page/{n}/")
        quotes = page.locator(".quote")
        for i in range(quotes.count()):
            q = quotes.nth(i)
            text = q.locator(".text").inner_text().strip("“”")
            author = q.locator(".author").inner_text()
            tags = ", ".join(q.locator(".tag").all_inner_texts())
            uid = hashlib.md5(f"{text}|{author}".encode()).hexdigest()
            items.append({"id": uid, "text": text, "author": author, "tags": tags})
    return items


def run_check(conn):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        # используем сохранённую сессию, чтобы не логиниться каждый раз
        state = SESSION_PATH if os.path.exists(SESSION_PATH) else None
        context = browser.new_context(storage_state=state)
        page = context.new_page()
        try:
            page.goto(SITE)
            if not is_logged_in(page):
                login(page)
                context.storage_state(path=SESSION_PATH)
            items = scrape(page)
        finally:
            browser.close()

    new = save_new(conn, items)
    log.info("Найдено записей: %d, новых: %d", len(items), len(new))
    notify(new)


def run_with_retries(conn):
    """Повторяет проверку при сбоях; при ошибке сессии удаляет её и логинится заново."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            run_check(conn)
            return
        except (PWTimeout, RuntimeError, requests.RequestException) as e:
            log.error("Попытка %d/%d не удалась: %s", attempt, MAX_RETRIES, e)
            if os.path.exists(SESSION_PATH):
                os.remove(SESSION_PATH)
            time.sleep(5 * attempt)
    send_telegram("⚠️ Мониторинг: все попытки не удались, проверьте лог")


def main():
    conn = init_db()
    once = "--once" in sys.argv
    log.info("Мониторинг запущен (%s)", "один проход" if once else f"каждые {INTERVAL} с")
    while True:
        run_with_retries(conn)
        if once:
            break
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
