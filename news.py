#!/usr/bin/env python3
import nntplib
import sys
import termios
import tty
import re
import quopri
import base64
from email.message import EmailMessage
import os
import tempfile
import subprocess
import time

# ================= USER CONFIG =================
NNTP_SERVER = "usnews.blocknews.net"
NNTP_PORT   = 563
USERNAME    = ""
PASSWORD    = ""
PAGE_LINES             = 12
MAX_ARTICLES_LIST      = 200
START_GROUP            = "alt.test"
SHOW_REPLY_COUNT_MAIN  = True
# ==============================================

RE_REPLY = re.compile(r"^(re|fwd):", re.IGNORECASE)
CLEAN_RE = re.compile(r"[\x00-\x08\x0b-\x0c\x0e-\x1f]")

# ---------- STATUS ----------
STATUS_LINE = ""
def set_status(msg):
    global STATUS_LINE
    STATUS_LINE = msg

def show_status():
    global STATUS_LINE
    if STATUS_LINE:
        print(f"\n[{STATUS_LINE}]")
        STATUS_LINE = ""

# ---------- RAW KEY INPUT ----------
def get_key():
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        return sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)

def prompt(text):
    sys.stdout.write(text)
    sys.stdout.flush()
    return sys.stdin.readline().strip()

# ---------- PAGER ----------
def paged_print(lines):
    i = 0
    while i < len(lines):
        for line in lines[i:i+PAGE_LINES]:
            print(line)
        i += PAGE_LINES
        if i >= len(lines):
            break
        print("\n--- ENTER = next page | SPACE = skip ---")
        if get_key() == " ":
            break

# ---------- BODY DECODER ----------
def decode_body_line(line_bytes):
    s = line_bytes.decode("utf-8", errors="replace")
    s = CLEAN_RE.sub("", s)
    if "=" in s:
        try:
            s = quopri.decodestring(s).decode("utf-8", errors="replace")
        except Exception:
            pass
    if re.fullmatch(r"[A-Za-z0-9+/=\s]+", s) and len(s.strip()) > 20:
        try:
            s = base64.b64decode(s, validate=True).decode("utf-8", errors="replace")
        except Exception:
            pass
    return s

# ---------- DISPLAY ----------
def show_article(nntp, num):
    try:
        _, body = nntp.body(str(num))
        paged_print([decode_body_line(l) for l in body.lines])
    except Exception as e:
        set_status(f"Fetch failed: {e}")

# ---------- GROUP LOAD ----------
def reload_group(nntp, group):
    try:
        _, _, first, last, _ = nntp.group(group)
        first, last = int(first), int(last)
        _, overviews = nntp.over((max(first, last - MAX_ARTICLES_LIST), last))
        posts = []
        rel = 1
        for num, hdr in reversed(overviews):
            subject = hdr.get("subject", "")
            if RE_REPLY.match(subject):
                continue
            msgid = hdr.get("message-id", "")
            replies = sum(
                1 for _, h in overviews if msgid in h.get("references", "")
            ) if SHOW_REPLY_COUNT_MAIN else 0
            posts.append({
                "rel_num": rel,
                "num": int(num),
                "from": CLEAN_RE.sub("", hdr.get("from", "?")),
                "date": hdr.get("date", "?"),
                "subject": CLEAN_RE.sub("", subject),
                "replies": replies
            })
            rel += 1
        return posts
    except Exception as e:
        set_status(f"Reload failed: {e}")
        return []

# ---------- HEADER SEARCH WITH PROGRESS ----------
def header_search(nntp, group, field, keyword, count):
    _, _, first, last, _ = nntp.group(group)
    start = max(int(first), int(last) - count + 1)
    _, overviews = nntp.over((start, last))
    results = []
    rel = 1
    total = len(overviews)

    for idx, (num, hdr) in enumerate(reversed(overviews), 1):
        sys.stdout.write(f"\rSearching headers... ({idx}/{total})")
        sys.stdout.flush()

        value = CLEAN_RE.sub("", hdr.get(field, ""))
        if keyword.lower() in value.lower():
            results.append({
                "rel_num": rel,
                "num": int(num),
                "from": CLEAN_RE.sub("", hdr.get("from", "?")),
                "date": hdr.get("date", "?"),
                "subject": CLEAN_RE.sub("", hdr.get("subject", "")),
                "replies": 0
            })
        rel += 1

    sys.stdout.write("\rSearch complete!           \n")
    return results

# ---------- BODY SEARCH WITH PROGRESS ----------
def body_search(nntp, group, keyword, count):
    _, _, first, last, _ = nntp.group(group)
    start = max(int(first), int(last) - count + 1)
    _, overviews = nntp.over((start, last))

    matches = []
    rel = 1
    total = len(overviews)

    for idx, (num, hdr) in enumerate(reversed(overviews), 1):
        sys.stdout.write(f"\rSearching bodies... ({idx}/{total})")
        sys.stdout.flush()

        try:
            _, body = nntp.body(str(num))
            text = "\n".join(decode_body_line(l) for l in body.lines)
            if keyword.lower() in text.lower():
                matches.append({
                    "rel_num": rel,
                    "num": int(num),
                    "from": CLEAN_RE.sub("", hdr.get("from", "?")),
                    "date": hdr.get("date", "?"),
                    "subject": CLEAN_RE.sub("", hdr.get("subject", "")),
                    "replies": 0
                })
        except Exception:
            pass
        rel += 1

    sys.stdout.write("\rSearch complete!           \n")
    return matches

# ---------- BROWSER ----------
def browse_group(nntp, group):
    posts = reload_group(nntp, group)
    index = 0

    while True:
        if not posts:
            set_status("No posts")
            posts = reload_group(nntp, group)
            continue

        p = posts[index]
        print(f"\n[{p['rel_num']}] #{p['num']}")
        print(f"From: {p['from']}")
        print(f"Date: {p['date']}")
        print(f"Replies: {p['replies']}")
        print(f"Subject: {p['subject']}")
        show_status()

        print(
            "\nENTER=read  SPACE=next  BACKSPACE=prev  "
            "L=reload  J=jump  G=group  "
            "B=batch  F=author  S=subject  M=body  P=page  Q=quit"
        )

        k = get_key().lower()

        if k == "q":
            sys.exit(0)
        elif k == " ":
            index = min(index + 1, len(posts) - 1)
        elif k == "\x7f":
            index = max(index - 1, 0)
        elif k in ("\r", "\n"):
            show_article(nntp, p["num"])
        elif k == "l":
            posts = reload_group(nntp, group)
            index = 0
        elif k == "g":
            group = prompt("New group: ")
            posts = reload_group(nntp, group)
            index = 0
        elif k in ("f", "s", "m"):
            kw = prompt("Keyword: ")
            c = prompt("Articles to scan: ")
            if not c.isdigit():
                continue
            if k == "f":
                results = header_search(nntp, group, "from", kw, int(c))
            elif k == "s":
                results = header_search(nntp, group, "subject", kw, int(c))
            else:
                results = body_search(nntp, group, kw, int(c))

            print(f"\nFound {len(results)} posts:\n")
            for r in results:
                print(f"[{r['rel_num']}] #{r['num']}")
                print(f"From: {r['from']}")
                print(f"Date: {r['date']}")
                print(f"Replies: {r['replies']}")
                print(f"Subject: {r['subject']}\n")

        elif k == "b":
            c = prompt("How many posts? ")
            if c.isdigit():
                for p2 in posts[index:index+int(c)]:
                    print(f"[{p2['rel_num']}] #{p2['num']}")
                    print(f"From: {p2['from']}")
                    print(f"Date: {p2['date']}")
                    print(f"Replies: {p2['replies']}")
                    print(f"Subject: {p2['subject']}\n")

        elif k == "p":
            v = prompt("Lines per page: ")
            if v.isdigit():
                global PAGE_LINES
                PAGE_LINES = int(v)

# ---------- MAIN ----------
def main():
    print(f"Connecting to {NNTP_SERVER}:{NNTP_PORT}")
    nntp = nntplib.NNTP_SSL(NNTP_SERVER, NNTP_PORT, USERNAME, PASSWORD)
    browse_group(nntp, START_GROUP)
    nntp.quit()

if __name__ == "__main__":
    main()