#!/usr/bin/env python3
import nntplib
import sys
import termios
import tty
import re
import quopri
import base64
import os
import uuid

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
        print(f"\n[{STATUS_LINE}]\n")
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
    print()
    sys.stdout.write(text)
    sys.stdout.flush()
    return sys.stdin.readline().strip()

# ---------- PAGER ----------
def paged_print(lines):
    i = 0
    while i < len(lines):
        for line in lines[i:i + PAGE_LINES]:
            print(line)
        i += PAGE_LINES
        if i >= len(lines):
            break
        print("\n--- ENTER = next page | SPACE = skip ---\n")
        if get_key() == " ":
            break
    print()

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

# ---------- HEADER SANITIZER ----------
def sanitize_header(s):
    return "".join(c if 32 <= ord(c) <= 126 else " " for c in s).strip()

# ---------- DISPLAY ----------
def show_article(nntp, num):
    try:
        _, body = nntp.body(str(num))
        print()
        paged_print([decode_body_line(l) for l in body.lines])
        print()
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
            replies = sum(1 for _, h in overviews if msgid in h.get("references", "")) if SHOW_REPLY_COUNT_MAIN else 0
            posts.append({
                "rel_num": rel,
                "num": int(num),
                "from": CLEAN_RE.sub("", hdr.get("from", "?")),
                "date": hdr.get("date", "?"),
                "subject": CLEAN_RE.sub("", subject),
                "replies": replies,
                "msgid": msgid
            })
            rel += 1
        return posts
    except Exception as e:
        set_status(f"Reload failed: {e}")
        return []

# ---------- FETCH REPLIES ----------
def fetch_replies(nntp, group, msgid):
    try:
        _, _, first, last, _ = nntp.group(group)
        first, last = int(first), int(last)
        start = max(first, last - MAX_ARTICLES_LIST + 1)
        _, overviews = nntp.over((start, last))
    except Exception as e:
        print(f"\nFailed to fetch overviews: {e}\n")
        return []

    replies = []
    rel = 1
    for num, hdr in reversed(overviews):
        if msgid in hdr.get("references", ""):
            r_msgid = hdr.get("message-id", "")
            r_replies = sum(1 for _, h in overviews if r_msgid in h.get("references", "")) if SHOW_REPLY_COUNT_MAIN else 0
            replies.append({
                "rel_num": rel,
                "num": int(num),
                "from": CLEAN_RE.sub("", hdr.get("from", "?")),
                "date": hdr.get("date", "?"),
                "subject": CLEAN_RE.sub("", hdr.get("subject", "")),
                "replies": r_replies,
                "msgid": r_msgid
            })
            rel += 1
    return replies

# ---------- POST NEW OR REPLY (RFC 1036 SAFE) ----------
def post_article(nntp, group, reply_to_msgid=None, reply_subject=None):
    print()
    name = prompt("Your name: ")
    email = prompt("Your email: ")
    from_addr = f"{sanitize_header(name)} <{sanitize_header(email)}>"

    if reply_subject:
        subject = reply_subject
        if not subject.lower().startswith("re:"):
            subject = "Re: " + subject
    else:
        subject = prompt("Subject: ")

    subject = sanitize_header(subject)

    print("\nEnter message body. End with a single '.' on a line:\n")

    lines = []
    while True:
        line = input()
        if line.strip() == ".":
            break
        lines.append(line)

    body = "\r\n".join(lines)
    msgid = f"<{uuid.uuid4().hex}@{NNTP_SERVER}>"

    headers = [
        f"From: {from_addr}",
        f"Newsgroups: {group}",
        f"Subject: {subject}",
        f"Message-ID: {msgid}",
    ]

    if reply_to_msgid:
        headers.append(f"References: {reply_to_msgid}")

    article = "\r\n".join(headers) + "\r\n\r\n" + body + "\r\n"

    try:
        nntp.post(article.encode("utf-8", errors="replace"))
        print("\nPost successful!\n")
    except Exception as e:
        print(f"\nPost failed: {e}\n")

# ---------- SHOW REPLIES (THREADED) ----------
def show_replies_thread(nntp, group, msgid, level=0):
    replies = fetch_replies(nntp, group, msgid)
    if not replies:
        print("\nNo replies found.\n")
        return

    while True:
        print("\nReplies:\n")
        for idx, r in enumerate(replies, 1):
            indent = "  " * level
            print(f"{indent}[{idx}]")
            print(f"{indent}From: {r['from']}")
            print(f"{indent}Date: {r['date']}")
            print(f"{indent}Subject: {r['subject']}")
            print(f"{indent}Replies: {r['replies']}\n")

        sel = prompt("Enter reply number to read, R=reply, ENTER=back: ").lower()
        if not sel:
            return
        if sel == "r":
            rnum = prompt("Reply to which number: ")
            if rnum.isdigit():
                r = replies[int(rnum) - 1]
                post_article(nntp, group, r["msgid"], r["subject"])
            continue
        if sel.isdigit():
            r = replies[int(sel) - 1]
            print(f"\n--- Reading Reply #{r['num']} ---\n")
            print(f"From: {r['from']}")
            print(f"Date: {r['date']}")
            print(f"Subject: {r['subject']}\n")
            show_article(nntp, r["num"])
            if r["replies"]:
                if prompt("Press R to view replies to this reply, ENTER to continue: ").lower() == "r":
                    show_replies_thread(nntp, group, r["msgid"], level + 1)

# ---------- BROWSER ----------
def browse_group(nntp, group):
    posts = reload_group(nntp, group)
    index = 0

    while True:
        p = posts[index]
        print(f"\n[{p['rel_num']}] #{p['num']}")
        print(f"From: {p['from']}")
        print(f"Date: {p['date']}")
        print(f"Replies: {p['replies']}")
        print(f"Subject: {p['subject']}")
        show_status()

        print("\nENTER=read SPACE=next BACKSPACE=prev "
              "L=reload G=group "
              "F=author S=subject M=body "
              "R=replies N=new post Y=reply P=page Q=quit\n")

        k = get_key().lower()
        if k == "q":
            sys.exit(0)
        elif k == " ":
            index = min(index + 1, len(posts) - 1)
        elif k == "\x7f":
            index = max(index - 1, 0)
        elif k in ("\r", "\n"):
            show_article(nntp, p["num"])
            if prompt("Press R to reply, ENTER to continue: ").lower() == "r":
                post_article(nntp, group, p["msgid"], p["subject"])
        elif k == "l":
            posts = reload_group(nntp, group)
            index = 0
        elif k == "g":
            group = prompt("New group: ")
            posts = reload_group(nntp, group)
            index = 0
        elif k == "r":
            show_replies_thread(nntp, group, p["msgid"])
        elif k == "n":
            post_article(nntp, group)
        elif k == "y":
            post_article(nntp, group, p["msgid"], p["subject"])

# ---------- MAIN ----------
def main():
    print(f"\nConnecting to {NNTP_SERVER}:{NNTP_PORT}\n")
    nntp = nntplib.NNTP_SSL(NNTP_SERVER, NNTP_PORT, USERNAME, PASSWORD)
    browse_group(nntp, START_GROUP)
    nntp.quit()

if __name__ == "__main__":
    main()