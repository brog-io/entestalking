#!/usr/bin/env python3
"""
Ente development digest -> RSS feed.

Fetches PRs opened and merged in ente-io/ente over the last LOOKBACK_HOURS,
asks Claude to write a human-readable digest focused on new features, and
prepends it as an item in an RSS 2.0 feed (docs/feed.xml), keeping history.

Stdlib only - no pip installs needed.

Environment variables:
  GITHUB_TOKEN        optional, raises API rate limit (provided free in Actions)
  OPENROUTER_API_KEY  optional; without it you get a plain PR listing, no AI summary
  OPENROUTER_MODEL    default: anthropic/claude-haiku-4.5
  REPO               default: ente-io/ente
  FEED_PATH          default: docs/feed.xml
  FEED_URL           public URL of the feed (used in <atom:link>), optional
  LOOKBACK_HOURS     default: 26 (daily cron + drift margin)
  MAX_ITEMS          default: 30 feed items kept
"""

import json
import os
import re
import sys
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from html import escape

REPO = os.environ.get("REPO", "ente-io/ente")
FEED_PATH = os.environ.get("FEED_PATH", "docs/feed.xml")
FEED_URL = os.environ.get("FEED_URL", "")
LOOKBACK_HOURS = int(os.environ.get("LOOKBACK_HOURS", "26"))
MAX_ITEMS = int(os.environ.get("MAX_ITEMS", "30"))
MODEL = os.environ.get("OPENROUTER_MODEL", "anthropic/claude-haiku-4.5")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")

NOW = datetime.now(timezone.utc)
SINCE = NOW - timedelta(hours=LOOKBACK_HOURS)


def gh_api(path, params=""):
    """GET a GitHub API path, return parsed JSON."""
    url = f"https://api.github.com{path}{params}"
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "ente-rss-digest",
        **({"Authorization": f"Bearer {GITHUB_TOKEN}"} if GITHUB_TOKEN else {}),
    })
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def parse_ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def is_bot(pr):
    u = pr.get("user") or {}
    return u.get("type") == "Bot" or (u.get("login", "")).endswith("[bot]")


def slim(pr, kind):
    return {
        "kind": kind,  # "merged" | "opened"
        "number": pr["number"],
        "title": pr["title"],
        "author": (pr.get("user") or {}).get("login", "?"),
        "labels": [l["name"] for l in pr.get("labels", [])],
        "url": pr["html_url"],
        "body": (pr.get("body") or "")[:600],
    }


def fetch_activity():
    """PRs merged or opened since SINCE. Returns (merged, opened)."""
    merged, opened = [], []
    seen = set()

    for page in (1, 2):
        closed = gh_api(f"/repos/{REPO}/pulls",
                        f"?state=closed&sort=updated&direction=desc&per_page=100&page={page}")
        if not closed:
            break
        for pr in closed:
            m = parse_ts(pr.get("merged_at"))
            if m and m >= SINCE and not is_bot(pr) \
                    and pr["number"] not in seen:
                seen.add(pr["number"])
                merged.append(slim(pr, "merged"))
        # stop paging once everything on the page is older than the window
        if all((parse_ts(p.get("updated_at")) or SINCE) < SINCE for p in closed):
            break

    for page in (1, 2):
        new = gh_api(f"/repos/{REPO}/pulls",
                     f"?state=open&sort=created&direction=desc&per_page=100&page={page}")
        if not new:
            break
        stop = False
        for pr in new:
            c = parse_ts(pr.get("created_at"))
            if c and c >= SINCE:
                if not is_bot(pr) and pr["number"] not in seen:
                    seen.add(pr["number"])
                    opened.append(slim(pr, "opened"))
            else:
                stop = True
        if stop:
            break

    return merged, opened


# ---------------------------------------------------------------- LLM digest

PROMPT = """You are writing a daily digest of development activity in the Ente \
open-source monorepo (https://github.com/{repo}). Ente builds end-to-end \
encrypted photo storage (Ente Photos) and a 2FA app (Ente Auth); the monorepo \
contains mobile, web, desktop, server, and CLI code. PR title prefixes and \
branch names often indicate which app/platform is affected.

Below is JSON with pull requests MERGED (shipped) and OPENED (in progress) in \
the last ~24 hours.

Write the digest as clean HTML (only <h3>, <p>, <ul>, <li>, <a>, <strong> tags):
1. Start with 1-2 sentences summarizing the day at a high level.
2. "<h3>Shipped</h3>": merged PRs, grouped by app/platform where obvious. Lead \
with user-facing features and notable changes; explain what each means for \
users in plain language. Fold trivial chores (deps, version bumps, typos, CI) \
into a single short line or omit them.
3. "<h3>In progress</h3>": newly opened PRs, same treatment - flag anything \
that looks like an upcoming feature.
Link every PR you mention to its URL. Do not invent anything not supported by \
the data. Keep it scannable; total length under ~400 words.

DATA:
{data}"""


def ai_digest(merged, opened):
    if not OPENROUTER_API_KEY:
        return None
    data = json.dumps({"merged": merged, "opened": opened}, indent=1)
    body = json.dumps({
        "model": MODEL,
        "max_tokens": 1500,
        "messages": [{"role": "user",
                      "content": PROMPT.format(repo=REPO, data=data)}],
    }).encode()
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions", data=body,
        method="POST",
        headers={"content-type": "application/json",
                 "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                 "HTTP-Referer": "https://github.com",
                 "X-Title": "ente-rss-digest"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            resp = json.load(r)
        text = (resp.get("choices") or [{}])[0] \
            .get("message", {}).get("content", "")
        # strip accidental markdown fences
        text = re.sub(r"^```(?:html)?\s*|\s*```$", "", text.strip())
        return text or None
    except (urllib.error.URLError, urllib.error.HTTPError, KeyError) as e:
        print(f"WARN: LLM call failed, falling back to plain listing: {e}",
              file=sys.stderr)
        return None


def plain_digest(merged, opened):
    def section(title, prs):
        if not prs:
            return ""
        lis = "".join(
            f'<li><a href="{escape(p["url"])}">#{p["number"]}</a> '
            f'{escape(p["title"])} <em>({escape(p["author"])})</em></li>'
            for p in prs)
        return f"<h3>{title}</h3><ul>{lis}</ul>"
    return (section("Shipped", merged) + section("In progress", opened)) \
        or "<p>No activity.</p>"


# ---------------------------------------------------------------- RSS output

def load_existing_items(path):
    if not os.path.exists(path):
        return []
    try:
        tree = ET.parse(path)
        return tree.findall("./channel/item")
    except ET.ParseError:
        return []


def item_xml(title, html_desc, guid, pubdate):
    it = ET.Element("item")
    ET.SubElement(it, "title").text = title
    ET.SubElement(it, "link").text = f"https://github.com/{REPO}/pulls"
    g = ET.SubElement(it, "guid", isPermaLink="false")
    g.text = guid
    ET.SubElement(it, "pubDate").text = format_datetime(pubdate)
    ET.SubElement(it, "description").text = html_desc
    return it


def write_feed(path, items):
    rss = ET.Element("rss", version="2.0",
                     attrib={"xmlns:atom": "http://www.w3.org/2005/Atom"})
    ch = ET.SubElement(rss, "channel")
    ET.SubElement(ch, "title").text = "Ente development digest"
    ET.SubElement(ch, "link").text = f"https://github.com/{REPO}"
    ET.SubElement(ch, "description").text = \
        "AI-generated daily digest of PRs opened and merged in the Ente monorepo"
    ET.SubElement(ch, "lastBuildDate").text = format_datetime(NOW)
    if FEED_URL:
        ET.SubElement(ch, "atom:link",
                      attrib={"href": FEED_URL, "rel": "self",
                              "type": "application/rss+xml"})
    for it in items[:MAX_ITEMS]:
        ch.append(it)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    ET.indent(rss)
    ET.ElementTree(rss).write(path, encoding="unicode", xml_declaration=True)


def main():
    merged, opened = fetch_activity()
    print(f"Found {len(merged)} merged, {len(opened)} opened PRs since "
          f"{SINCE:%Y-%m-%d %H:%M} UTC")

    if not merged and not opened:
        print("No activity in window; feed left untouched.")
        return

    guid = f"ente-digest-{NOW:%Y-%m-%d}"
    existing = load_existing_items(FEED_PATH)
    if any(i.findtext("guid") == guid for i in existing):
        print("Digest for today already exists; feed left untouched.")
        return

    html = ai_digest(merged, opened) or plain_digest(merged, opened)
    title = (f"Ente digest {NOW:%b %d, %Y} - "
             f"{len(merged)} shipped, {len(opened)} in progress")
    new_item = item_xml(title, html, guid, NOW)
    write_feed(FEED_PATH, [new_item] + existing)
    print(f"Wrote {FEED_PATH} ({1 + len(existing)} items before trim).")


if __name__ == "__main__":
    main()
