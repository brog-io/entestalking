#!/usr/bin/env python3
"""
Per-channel Discord -> RSS feeds (static, GitHub Actions edition).

Port of the VPS discord-rss bot: instead of a live gateway bot + HTTP server,
this re-fetches the last MAX_ITEMS messages of each watched channel over REST
and rewrites one RSS 2.0 file per channel under docs/feed/<channel_id>.xml.
Because every run regenerates the feeds from scratch, edits and deletions are
reflected on the next run. An index of watched channels is written to
docs/feed/index.html.

Stdlib only - no pip installs needed.

Environment variables:
  DISCORD_BOT_TOKEN  (required) bot token
  CHANNEL_IDS        (required) comma-separated channel IDs to watch
  FEED_BASE_URL      public base URL, e.g. https://feeds.brog.io
                     (used in <atom:link rel="self">), optional
  OUT_DIR            default: docs/feed
  MAX_ITEMS          max items per feed (default: 50, max 100)
"""

import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import format_datetime
from html import escape

BOT_TOKEN = os.environ.get(
    "DISCORD_BOT_TOKEN", "948956829982031912, 1503370083685236896"
)
CHANNEL_IDS = [
    c.strip() for c in os.environ.get("CHANNEL_IDS", "").split(",") if c.strip()
]
FEED_BASE_URL = os.environ.get("FEED_BASE_URL", "").rstrip("/")
OUT_DIR = os.environ.get("OUT_DIR", "docs/feed")
MAX_ITEMS = min(int(os.environ.get("MAX_ITEMS", "50")), 100)

API = "https://discord.com/api/v10"
NOW = datetime.now(timezone.utc)
URL_RE = re.compile(r"(https?://[^\s<]+)")
CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def discord_api(path):
    """GET a Discord API path. None on 403/404; one retry on 429."""
    for attempt in (1, 2):
        req = urllib.request.Request(
            f"{API}{path}",
            headers={
                "Authorization": f"Bot {BOT_TOKEN}",
                "User-Agent": "DiscordBot (https://github.com/brog-io/entestalking, 1.0)",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in (403, 404):
                return None
            if e.code == 429 and attempt == 1:
                try:
                    wait = float(json.load(e).get("retry_after", 2))
                except Exception:
                    wait = 2.0
                time.sleep(min(wait, 30) + 0.5)
                continue
            raise
    return None


def clean(text):
    return CONTROL_CHARS.sub("", text or "")


# ---------------------------------------------------------------- rendering


def message_body(m):
    """HTML body for one message: content + attachments + embeds."""
    parts = []
    content = clean(m.get("content", ""))
    if content:
        text = escape(content)
        text = URL_RE.sub(r'<a href="\1">\1</a>', text)
        text = text.replace("\n", "<br>")
        parts.append(f"<p>{text}</p>")
    for att in m.get("attachments", []):
        url = escape(att.get("url", ""), {'"': "&quot;"})
        ct = (att.get("content_type") or "").lower()
        if ct.startswith("image/"):
            parts.append(f'<p><img src="{url}" alt=""></p>')
        else:
            parts.append(
                f'<p><a href="{url}">{escape(att.get("filename", "file"))}</a></p>'
            )
    for emb in m.get("embeds", []):
        title, e_url = clean(emb.get("title", "")), emb.get("url", "")
        if title and e_url:
            parts.append(
                f'<p><a href="{escape(e_url, {chr(34): "&quot;"})}">{escape(title)}</a></p>'
            )
        elif title:
            parts.append(f"<p><strong>{escape(title)}</strong></p>")
        if emb.get("description"):
            parts.append(f"<p>{escape(clean(emb['description']))}</p>")
    return "\n".join(parts) or "<p>(no content)</p>"


def message_item(m, channel):
    author = m.get("author") or {}
    name = clean(author.get("global_name") or author.get("username", "?"))
    content = clean(m.get("content", "")).strip()
    title = content.split("\n", 1)[0][:140] or (f"{name} posted in #{channel['name']}")
    ts = datetime.fromisoformat(m["timestamp"].replace("Z", "+00:00"))
    jump = (
        f"https://discord.com/channels/{channel['guild_id']}"
        f"/{channel['id']}/{m['id']}"
    )
    it = ET.Element("item")
    ET.SubElement(it, "title").text = title
    ET.SubElement(it, "link").text = jump
    g = ET.SubElement(it, "guid", isPermaLink="false")
    g.text = f"discord-msg-{m['id']}"
    ET.SubElement(it, "pubDate").text = format_datetime(ts)
    ET.SubElement(it, "dc:creator").text = name
    ET.SubElement(it, "description").text = message_body(m)
    return it


def write_channel_feed(channel, messages):
    rss = ET.Element(
        "rss",
        version="2.0",
        attrib={
            "xmlns:atom": "http://www.w3.org/2005/Atom",
            "xmlns:dc": "http://purl.org/dc/elements/1.1/",
        },
    )
    ch = ET.SubElement(rss, "channel")
    ET.SubElement(ch, "title").text = f"#{channel['name']}"
    ET.SubElement(ch, "link").text = (
        f"https://discord.com/channels/{channel['guild_id']}/{channel['id']}"
    )
    ET.SubElement(ch, "description").text = f"Discord channel #{channel['name']}"
    ET.SubElement(ch, "lastBuildDate").text = format_datetime(NOW)
    ET.SubElement(ch, "generator").text = "discord-rss (github actions)"
    if FEED_BASE_URL:
        ET.SubElement(
            ch,
            "atom:link",
            attrib={
                "href": f"{FEED_BASE_URL}/feed/{channel['id']}.xml",
                "rel": "self",
                "type": "application/rss+xml",
            },
        )
    for m in messages[:MAX_ITEMS]:
        ch.append(message_item(m, channel))
    path = os.path.join(OUT_DIR, f"{channel['id']}.xml")
    os.makedirs(OUT_DIR, exist_ok=True)
    ET.indent(rss)
    ET.ElementTree(rss).write(path, encoding="unicode", xml_declaration=True)
    return path


def write_index(channels):
    rows = "".join(
        f'<li><a href="{c["id"]}.xml">#{escape(c["name"])}</a></li>' for c in channels
    )
    html = (
        "<!doctype html><meta charset=utf-8><title>Discord channel feeds</title>"
        "<style>body{font-family:system-ui;max-width:40rem;margin:4rem auto;"
        "padding:0 1rem}</style>"
        f"<h1>Discord channel feeds</h1><ul>{rows}</ul>"
        '<p><a href="../">Back to all feeds</a></p>'
    )
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "index.html"), "w", encoding="utf-8") as f:
        f.write(html)


# ---------------------------------------------------------------- main


def main():
    if not BOT_TOKEN:
        sys.exit("ERROR: DISCORD_BOT_TOKEN is required")
    if not CHANNEL_IDS:
        sys.exit("ERROR: CHANNEL_IDS is required (comma-separated channel IDs)")

    done = []
    for cid in CHANNEL_IDS:
        info = discord_api(f"/channels/{cid}")
        if info is None:
            print(f"WARN: no access to channel {cid}, skipping", file=sys.stderr)
            continue
        channel = {
            "id": info["id"],
            "name": clean(info.get("name", cid)),
            "guild_id": info.get("guild_id", ""),
        }
        messages = discord_api(f"/channels/{cid}/messages?limit=100")
        if messages is None:
            print(f"WARN: cannot read messages in #{channel['name']}", file=sys.stderr)
            continue
        # API returns newest first, which is what RSS wants
        path = write_channel_feed(channel, messages)
        done.append(channel)
        print(f"Wrote {path} ({min(len(messages), MAX_ITEMS)} items)")
        time.sleep(0.3)

    write_index(done)
    print(f"Wrote {OUT_DIR}/index.html ({len(done)} channels)")
    if not done:
        sys.exit("ERROR: no channels could be read")


if __name__ == "__main__":
    main()
