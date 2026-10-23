#!/usr/bin/env python3
"""Send new RSS articles to a Kindle as one EPUB digest, via email.

Config (environment variables):
  KINDLE_EMAIL        your Kindle's @kindle.com address
  GMAIL_ADDRESS       email address used to send (must be on Amazon's approved list);
                      despite the name, any provider works
  GMAIL_APP_PASSWORD  that account's app password (not your normal password)
  SMTP_HOST           optional; defaults to smtp.gmail.com (Yahoo: smtp.mail.yahoo.com)
  SMTP_PORT           optional; defaults to 465 (SSL)
  MAX_PER_FEED        newest unseen articles to take per feed (default 5)
  MAX_ARTICLES        cap on articles per digest (default 20)
  DRY_RUN=1           write digest.epub locally; don't email or update seen.json

Feeds are listed one per line in feeds.txt.
"""
import html
import io
import json
import os
import smtplib
import sys
import uuid
from datetime import date
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urljoin

import feedparser
import requests
from bs4 import BeautifulSoup
from ebooklib import epub
from PIL import Image
from readability import Document

ROOT = Path(__file__).parent
FEEDS_FILE = ROOT / "feeds.txt"
SEEN_FILE = ROOT / "seen.json"

MAX_PER_FEED = int(os.environ.get("MAX_PER_FEED", "2"))
MAX_ARTICLES = int(os.environ.get("MAX_ARTICLES", "20"))
DRY_RUN = os.environ.get("DRY_RUN") == "1"

IMAGE_BUDGET_BYTES = 12 * 1024 * 1024  # total images per digest
MAX_IMG_WIDTH = 900
MIN_IMG_SIDE = 50  # skip tracking pixels and tiny icons
MIN_TEXT_CHARS = 300  # below this, extraction probably failed (paywall/JS site)
SEEN_LIMIT = 5000
TIMEOUT = 20
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; rss2kindle/1.0)"}
LAZY_ATTRS = ["data-src", "data-lazy-src", "data-original", "data-td-src-property"]


# ---------- state ----------

def load_feeds():
    lines = FEEDS_FILE.read_text().splitlines()
    return [l.strip() for l in lines if l.strip() and not l.strip().startswith("#")]


def load_seen():
    if SEEN_FILE.exists():
        return json.loads(SEEN_FILE.read_text())
    return []


def save_seen(seen_list):
    SEEN_FILE.write_text(json.dumps(seen_list[-SEEN_LIMIT:], indent=0) + "\n")


def entry_id(entry):
    return entry.get("id") or entry.get("link")


# ---------- article extraction ----------

def text_length(html_str):
    return len(BeautifulSoup(html_str or "", "lxml").get_text(" ", strip=True))


def feed_content(entry):
    if entry.get("content"):
        return entry["content"][0].get("value", "")
    return entry.get("summary", "")


def get_article(entry):
    """Return dict(title, author, content, base_url). Falls back to feed content."""
    url = entry.get("link")
    title = entry.get("title") or url
    from_feed = feed_content(entry)
    extracted, base = "", url

    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        if r.encoding and r.encoding.lower() == "iso-8859-1":
            r.encoding = r.apparent_encoding  # no charset sent; guess instead
        doc = Document(r.text)
        extracted = doc.summary(html_partial=True)
        base = r.url
        title = entry.get("title") or doc.short_title() or url
    except Exception as exc:
        print(f"  ! could not fetch {url}: {exc}")

    # Prefer the fetched page, unless it looks empty and the feed has more.
    if text_length(extracted) >= MIN_TEXT_CHARS or text_length(extracted) >= text_length(from_feed):
        content = extracted
    else:
        content = from_feed
        base = url
    if text_length(content) == 0:
        raise ValueError("no usable content")

    return {
        "title": title,
        "author": entry.get("author", ""),
        "content": content,
        "base_url": base,
    }


def embed_images(soup, base_url, book, article_no, budget):
    """Download images, convert to JPEG, add to the EPUB, rewrite <img> tags."""
    for i, img in enumerate(soup.find_all("img")):
        src = next((img[a] for a in LAZY_ATTRS if img.get(a)), None) or img.get("src")
        alt = img.get("alt", "")
        if not src or src.startswith("data:"):
            img.decompose()
            continue
        try:
            r = requests.get(urljoin(base_url, src), headers=HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            im = Image.open(io.BytesIO(r.content))
            if im.width < MIN_IMG_SIDE or im.height < MIN_IMG_SIDE:
                img.decompose()
                continue
            if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
                im = im.convert("RGBA")
                bg = Image.new("RGB", im.size, "white")
                bg.paste(im, mask=im.split()[3])
                im = bg
            else:
                im = im.convert("RGB")
            if im.width > MAX_IMG_WIDTH:
                im = im.resize((MAX_IMG_WIDTH, round(im.height * MAX_IMG_WIDTH / im.width)))
            out = io.BytesIO()
            im.save(out, "JPEG", quality=80)
            data = out.getvalue()
            if budget["bytes"] < len(data):
                img.decompose()
                continue
            budget["bytes"] -= len(data)
        except Exception:
            img.decompose()  # unreadable image (SVG, broken link, ...): drop it
            continue

        name = f"images/a{article_no}_{i}.jpg"
        book.add_item(epub.EpubItem(uid=f"img_{article_no}_{i}", file_name=name,
                                    media_type="image/jpeg", content=data))
        img.attrs = {"src": name, "alt": alt}


def make_chapter(article, feed_title, article_no, book, css, budget):
    soup = BeautifulSoup(article["content"], "lxml")
    for tag in soup(["script", "style", "iframe", "form", "noscript", "svg", "video", "audio"]):
        tag.decompose()
    embed_images(soup, article["base_url"], book, article_no, budget)
    body = soup.body.decode_contents() if soup.body else str(soup)

    byline = " · ".join(x for x in [article["author"], feed_title] if x)
    chapter = epub.EpubHtml(title=article["title"], file_name=f"article_{article_no}.xhtml", lang="en")
    chapter.content = (
        f"<h1>{html.escape(article['title'])}</h1>"
        f"<p><em>{html.escape(byline)}</em></p>{body}"
    )
    chapter.add_item(css)
    return chapter


# ---------- EPUB + email ----------

def build_epub(chapters, book, css, today):
    for c in chapters:
        book.add_item(c)
    book.add_item(css)
    book.toc = chapters
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav"] + chapters
    path = ROOT / "digest.epub"
    epub.write_epub(str(path), book)
    data = path.read_bytes()
    if not DRY_RUN:
        path.unlink()
    return data


def send_email(epub_bytes, filename):
    sender = os.environ["GMAIL_ADDRESS"]
    password = os.environ["GMAIL_APP_PASSWORD"].replace(" ", "")  # app passwords show with spaces
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = os.environ["KINDLE_EMAIL"]
    msg["Subject"] = filename
    msg.set_content("Your reading digest is attached.")
    msg.add_attachment(epub_bytes, maintype="application", subtype="epub+zip", filename=filename)
    host = os.environ.get("SMTP_HOST") or "smtp.gmail.com"
    port = int(os.environ.get("SMTP_PORT") or 465)
    with smtplib.SMTP_SSL(host, port, timeout=60) as smtp:
        smtp.login(sender, password)
        smtp.send_message(msg)


# ---------- main ----------

def main():
    seen_list = load_seen()
    seen = set(seen_list)
    to_mark = []  # ids to record as seen once the email goes out
    queue = []    # (feed_title, entry)

    for feed_url in load_feeds():
        parsed = feedparser.parse(feed_url, agent=HEADERS["User-Agent"])
        if not parsed.entries:
            print(f"! no entries from {feed_url} ({parsed.get('bozo_exception', 'empty feed')})")
            continue
        feed_title = parsed.feed.get("title", feed_url)
        new = [e for e in parsed.entries if entry_id(e) and entry_id(e) not in seen]
        # Older backlog beyond the per-feed limit is marked seen so it never floods in later.
        to_mark += [entry_id(e) for e in new[MAX_PER_FEED:]]
        for e in new[:MAX_PER_FEED]:
            if len(queue) < MAX_ARTICLES:
                queue.append((feed_title, e))  # beyond the cap: left unseen, sent next run
        print(f"{feed_title}: {len(new)} new")

    if not queue:
        print("Nothing new to send.")
        if to_mark and not DRY_RUN:
            save_seen(seen_list + to_mark)
        return

    book = epub.EpubBook()
    today = date.today().strftime("%d-%m-%Y")
    book.set_identifier(str(uuid.uuid4()))
    book.set_title(f"RSS Reads {today}")
    book.set_language("en")
    book.add_author("rss2kindle")
    css = epub.EpubItem(uid="style", file_name="style/style.css", media_type="text/css",
                        content="img{max-width:100%;height:auto} body{line-height:1.4}")

    budget = {"bytes": IMAGE_BUDGET_BYTES}
    chapters = []
    for feed_title, entry in queue:
        print(f"- {entry.get('title', entry.get('link'))}")
        try:
            article = get_article(entry)
            chapters.append(make_chapter(article, feed_title, len(chapters), book, css, budget))
            to_mark.append(entry_id(entry))
        except Exception as exc:
            print(f"  ! skipped (will retry next run): {exc}")

    if not chapters:
        print("No articles could be extracted.")
        return

    data = build_epub(chapters, book, css, today)
    print(f"Digest: {len(chapters)} articles, {len(data) / 1e6:.1f} MB")

    if DRY_RUN:
        print("DRY_RUN: wrote digest.epub, no email sent, seen.json unchanged.")
        return

    send_email(data, f"RSS Reads {today}.epub")
    save_seen(seen_list + to_mark)  # only after the email succeeds
    print("Sent.")


if __name__ == "__main__":
    sys.exit(main())
