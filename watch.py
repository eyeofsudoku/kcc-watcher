#!/usr/bin/env python3
"""
KCC Berlin board watcher.

Checks the Korean Cultural Centre Berlin boards for new posts and notifies
via ntfy push + email. Designed to fail loudly rather than silently.
"""

import hashlib
import json
import os
import smtplib
import time
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

BOARDS = {
    "Kurse/Seminare": "https://germany.korean-culture.org/de/1517/board/1185/list",
    "Veranstaltungen": "https://germany.korean-culture.org/de/2071/board/1181/list",
    "Mitteilungen": "https://germany.korean-culture.org/de/2070/board/1182/list",
}

# Any of these in a post title or body raises the alert to max priority.
KEYWORDS = [
    "kalligraf", "kalligraph", "calligraph",
    "서예", "seoye", "schriftkunst", "pinselschrift",
]

STATE_FILE = "state.json"
FAIL_ALERT_AFTER = 3        # consecutive failures before crying wolf
MAX_REMEMBERED = 400        # cap on stored fingerprints per board
BERLIN = ZoneInfo("Europe/Berlin")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------

def fetch(url: str) -> str:
    """Fetch a board page, defeating intermediate caches."""
    busted = f"{url}?_cb={int(time.time())}"
    resp = requests.get(busted, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    resp.encoding = resp.encoding or "utf-8"
    return resp.text


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

def _title_from_repetition(text: str):
    """
    Board entries render the title twice: "Title Title body text...".
    Detect the doubled prefix and return it.
    """
    words = text.split()
    upper = min(40, len(words) // 2)
    for n in range(upper, 2, -1):
        if words[:n] == words[n:2 * n]:
            return " ".join(words[:n])
    return None


def parse_posts(html: str):
    """Return a list of {title, date, excerpt} for every post on the page."""
    soup = BeautifulSoup(html, "html.parser")
    posts = []

    for anchor in soup.find_all("a"):
        text = anchor.get_text(" ", strip=True)
        if "Beitragstag" not in text:
            continue

        head = text.split("Beitragstag")[0].strip()

        # Strategy 1: an explicit emphasis tag holding the title.
        title = None
        for tag in anchor.find_all(["strong", "b", "h3", "h4"]):
            candidate = tag.get_text(" ", strip=True)
            if candidate and len(candidate) > 5:
                title = candidate
                break

        # Strategy 2: the doubled-title pattern.
        if not title:
            title = _title_from_repetition(head)

        # Strategy 3: give up and truncate.
        if not title:
            title = head[:160]

        date = text.rsplit("Beitragstag", 1)[-1].strip()[:24]

        posts.append({
            "title": " ".join(title.split())[:250],
            "date": " ".join(date.split()),
            "excerpt": " ".join(head.split())[:600],
        })

    return posts


def fingerprint(title: str) -> str:
    normalised = " ".join(title.lower().split())
    return hashlib.sha1(normalised.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------

def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as fh:
                return json.load(fh)
        except (json.JSONDecodeError, OSError) as exc:
            print(f"WARNING: state file unreadable ({exc}), starting fresh")
    return {}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=2, sort_keys=True)


# --------------------------------------------------------------------------
# Notifications
# --------------------------------------------------------------------------

def notify_push(title: str, message: str, priority: int = 3,
                tags=None, click: str = None) -> None:
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        print("WARNING: NTFY_TOPIC not set, skipping push")
        return

    payload = {
        "topic": topic,
        "title": title,
        "message": message,
        "priority": priority,
        "tags": tags or ["bell"],
    }
    if click:
        payload["click"] = click

    try:
        resp = requests.post("https://ntfy.sh", json=payload, timeout=20)
        resp.raise_for_status()
        print(f"push sent: {title}")
    except requests.RequestException as exc:
        print(f"ERROR: push failed: {exc}")


def notify_email(subject: str, body: str) -> None:
    user = os.environ.get("MAIL_USERNAME")
    password = os.environ.get("MAIL_PASSWORD")
    recipient = os.environ.get("MAIL_TO")

    if not all([user, password, recipient]):
        print("WARNING: mail secrets incomplete, skipping email")
        return

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = recipient
    msg.set_content(body)

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as server:
            server.login(user, password)
            server.send_message(msg)
        print(f"email sent: {subject}")
    except Exception as exc:
        print(f"ERROR: email failed: {exc}")


def is_hot(post: dict) -> bool:
    haystack = (post["title"] + " " + post["excerpt"]).lower()
    return any(kw in haystack for kw in KEYWORDS)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main() -> None:
    state = load_state()
    new_posts = []
    problems = []

    for name, url in BOARDS.items():
        board = state.setdefault(name, {"seen": [], "fails": 0, "warned": False})
        first_run = not board["seen"]

        try:
            posts = parse_posts(fetch(url))
            if not posts:
                raise ValueError("0 posts parsed — page structure may have changed")
        except Exception as exc:
            board["fails"] += 1
            print(f"ERROR [{name}]: {exc} (failure #{board['fails']})")
            if board["fails"] >= FAIL_ALERT_AFTER and not board["warned"]:
                problems.append(f"{name}: {exc}")
                board["warned"] = True
            continue

        if board["fails"] or board["warned"]:
            print(f"[{name}] recovered after {board['fails']} failures")
            board["fails"] = 0
            board["warned"] = False

        seen = set(board["seen"])
        fresh = [p for p in posts if fingerprint(p["title"]) not in seen]

        board["seen"].extend(fingerprint(p["title"]) for p in fresh)
        board["seen"] = board["seen"][-MAX_REMEMBERED:]

        print(f"[{name}] {len(posts)} posts on page, {len(fresh)} new")

        if first_run:
            print(f"[{name}] first run — seeding baseline, no alerts sent")
            for p in posts:
                print(f"    · {p['title']}  ({p['date']})")
            continue

        for p in fresh:
            p["board"] = name
            p["url"] = url
            new_posts.append(p)
            print(f"    NEW: {p['title']}  ({p['date']})")

    # ---- alert on new posts ------------------------------------------------
    if new_posts:
        hot = [p for p in new_posts if is_hot(p)]
        priority = 5 if hot else 4
        tags = ["rotating_light"] if hot else ["mega"]

        if len(new_posts) == 1:
            p = new_posts[0]
            push_title = ("KALLIGRAFIE?! " if hot else "") + p["board"]
            push_body = f"{p['title']}\n({p['date']})"
            click = p["url"]
        else:
            push_title = ("KALLIGRAFIE?! " if hot else "") + f"{len(new_posts)} new posts"
            push_body = "\n".join(f"· {p['title']}" for p in new_posts)
            click = new_posts[0]["url"]

        notify_push(push_title, push_body, priority=priority, tags=tags, click=click)

        lines = ["New post(s) on the Korean Cultural Centre Berlin boards:", ""]
        for p in new_posts:
            lines += [
                f"BOARD:  {p['board']}",
                f"TITLE:  {p['title']}",
                f"DATE:   {p['date']}",
                f"LINK:   {p['url']}",
                "",
                p["excerpt"],
                "",
                "-" * 60,
                "",
            ]
        lines.append(
            "Reminder: registration usually opens days later at a fixed time. "
            "Open the post, find the Anmeldestart, set a phone alarm 5 minutes early."
        )
        subject = ("[KALLIGRAFIE?] " if hot else "[KCC] ") + \
                  (new_posts[0]["title"] if len(new_posts) == 1
                   else f"{len(new_posts)} new posts")
        notify_email(subject[:180], "\n".join(lines))

    # ---- alert on breakage -------------------------------------------------
    if problems:
        detail = "\n".join(problems)
        notify_push("Watcher is broken", detail, priority=4, tags=["warning"])
        notify_email(
            "[KCC] Watcher is failing",
            "The watcher could not read one or more boards:\n\n"
            + detail
            + "\n\nCheck the site manually until this is fixed.",
        )

    # ---- weekly heartbeat --------------------------------------------------
    now = datetime.now(BERLIN)
    today = now.date().isoformat()
    if now.weekday() == 0 and now.hour >= 9 and state.get("last_heartbeat") != today:
        state["last_heartbeat"] = today
        notify_push(
            "Watcher alive",
            "Weekly check-in. Boards are being monitored.",
            priority=1,
            tags=["white_check_mark"],
        )

    save_state(state)
    print("done")


if __name__ == "__main__":
    main()
