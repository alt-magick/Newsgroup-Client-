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
import uuid

# ================= USER CONFIG =================
NNTP_SERVER = "usnews.blocknews.net"
NNTP_PORT   = 563
USERNAME    = ""
PASSWORD    = ""
PAGE_LINES             = 25
MAX_ARTICLES_LIST      = 200
START_GROUP            = "alt.magick"
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
def show_article(nntp, num, group=None):
    try:
        _, body = nntp.body(str(num))
        print()
        if group:
            print(f"Group: {group}\n")
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

# ---------- HEADER SEARCH ----------
def header_search(nntp, group, field, keyword, count, start_index=0, posts=None):
    if posts is None:
        posts = reload_group(nntp, group)
    results = []
    rel = 1
    total = min(count, len(posts) - start_index)
    for idx, p in enumerate(posts[start_index:start_index + count], 1):
        sys.stdout.write(f"\rSearching headers... ({idx}/{total})")
        sys.stdout.flush()
        value = CLEAN_RE.sub(p.get(field, ""), "")
        if keyword.lower() in value.lower():
            results.append(p)
        rel += 1
    sys.stdout.write("\rSearch complete!           \n")
    return results

# ---------- BODY SEARCH ----------
def body_search(nntp, group, keyword, count, start_index=0, posts=None):
    if posts is None:
        posts = reload_group(nntp, group)
    matches = []
    total = min(count, len(posts) - start_index)
    for idx, p in enumerate(posts[start_index:start_index + count], 1):
        sys.stdout.write(f"\rSearching bodies... ({idx}/{total})")
        sys.stdout.flush()
        try:
            _, body = nntp.body(str(p["num"]))
            text = "\n".join(decode_body_line(l) for l in body.lines)
            if keyword.lower() in text.lower():
                matches.append(p)
        except Exception:
            pass
    sys.stdout.write("\rSearch complete!           \n")
    return matches

# ---------- FETCH REPLIES ----------
def fetch_replies(nntp, group, msgid):
    try:
        _, _, first, last, _ = nntp.group(group)
        first, last = int(first), int(last)
        start = max(first, last - MAX_ARTICLES_LIST + 1)
        _, overviews = nntp.over((start, last))
    except Exception as e:
        print(f"\nFailed to fetch overviews: {e}")
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

# ---------- POST NEW OR REPLY ----------
def post_article(nntp, group, reply_to_msgid=None, reply_subject=None):
    name = prompt("Your name: ")
    email = prompt("Your email: ")
    FROM_ADDR = f"{sanitize_header(name)} <{sanitize_header(email)}>"

    if reply_subject:
        subject = f"Re: {reply_subject}" if not reply_subject.lower().startswith("re:") else reply_subject
    else:
        subject = prompt("Subject: ")
    subject = sanitize_header(subject)

    print("Enter message body. End with a single '.' on a line:")
    lines = []
    while True:
        line = input()
        if line.strip() == ".":
            break
        lines.append(line)
    body = "\r\n".join(lines)

    msg = EmailMessage()
    msg["From"] = FROM_ADDR
    msg["Newsgroups"] = group
    msg["Subject"] = subject
    if reply_to_msgid:
        msg["References"] = reply_to_msgid
    msg["Message-ID"] = f"<{uuid.uuid4().hex}@{NNTP_SERVER}>"
    msg.set_content(body)

    try:
        nntp.post(msg.as_bytes())
        print("Post successful!")
    except Exception as e:
        print(f"Post failed: {e}")

# ---------- SHOW REPLIES ----------
def show_replies_thread(nntp, group, msgid, level=0):
    replies = fetch_replies(nntp, group, msgid)
    if not replies:
        print("\nNo replies found.")
        return

    while True:
        print("\nReplies:")
        for idx, r in enumerate(replies, 1):
            indent = "  " * level
            print(f"{indent}[{idx}]")
            print(f"{indent}From: {r['from']}")
            print(f"{indent}Date: {r['date']}")
            print(f"{indent}Subject: {r['subject']}")
            print(f"{indent}Replies: {r['replies']}\n")

        sel = prompt("\nEnter reply number to read, R=reply, ENTER=back: ").strip()
        if not sel:
            return
        if sel.lower() == "r":
            rnum = prompt("Enter reply number to respond to: ").strip()
            if rnum.isdigit():
                idx = int(rnum) - 1
                if 0 <= idx < len(replies):
                    r = replies[idx]
                    post_article(nntp, group, r["msgid"], r["subject"])
            continue
        if not sel.isdigit():
            continue
        sel_idx = int(sel) - 1
        if 0 <= sel_idx < len(replies):
            r = replies[sel_idx]
            print(f"\n--- Reading Reply #{r['num']} ---")
            print(f"Group: {group}")
            print(f"From: {r['from']}")
            print(f"Date: {r['date']}")
            print(f"Subject: {r['subject']}\n")
            show_article(nntp, r["num"], group)
            if r["replies"] > 0:
                k = prompt("Press R to view replies to this reply, ENTER to continue: ").lower()
                if k == "r":
                    show_replies_thread(nntp, group, r["msgid"], level + 1)

# ---------- JUMP FUNCTION ----------
def jump_post(posts):
    val = prompt("Jump to post #: ").strip()
    if not val:
        return None
    if val.startswith("#"):
        try:
            n = int(val[1:])
            for idx, p in enumerate(posts):
                if p["num"] == n:
                    return idx
        except:
            pass
    else:
        try:
            n = int(val)
            if 1 <= n <= len(posts):
                return n - 1
        except:
            pass
    set_status("Invalid jump number")
    return None

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
        print(f"Group: {group}")
        print(f"From: {p['from']}")
        print(f"Date: {p['date']}")
        print(f"Replies: {p['replies']}")
        print(f"Subject: {p['subject']}")
        show_status()

        print(
            "\nENTER=read  SPACE=next  BACKSPACE=prev  "
            "L=reload  J=jump  G=group  "
            "B=batch  F=author  S=subject  M=body  "
            "R=replies  N=new post  Y=reply  P=page  C=reconnect  Q=quit"
        )

        k = get_key().lower()
        if k == "q":
            try: nntp.quit()
            except: pass
            sys.exit(0)
        elif k == "c":
            set_status("Reconnecting...")
            try:
                nntp.quit()
            except:
                pass
            try:
                nntp = nntplib.NNTP_SSL(NNTP_SERVER, NNTP_PORT, USERNAME, PASSWORD)
                posts = reload_group(nntp, group)
                index = 0
                set_status("Reconnected successfully")
            except Exception as e:
                set_status(f"Reconnect failed: {e}")
            continue
        elif k == " ":
            index = min(index + 1, len(posts) - 1)
        elif k == "\x7f":
            index = max(index - 1, 0)
        elif k in ("\r", "\n"):
            show_article(nntp, p["num"], group)
            if prompt("Press R to reply, ENTER to continue: ").lower() == "r":
                post_article(nntp, group, p["msgid"], p["subject"])
        elif k == "l":
            posts = reload_group(nntp, group)
            index = 0
        elif k == "g":
            group = prompt("New group: ")
            posts = reload_group(nntp, group)
            index = 0
        elif k == "j":
            idx = jump_post(posts)
            if idx is not None:
                index = idx
        elif k in ("f", "s", "m"):
            kw = prompt("Keyword: ")
            num_to_search = prompt("Articles to scan: ")
            if not num_to_search.isdigit():
                continue
            num_to_search = int(num_to_search)
            if k == "f":
                results = header_search(nntp, group, "from", kw, num_to_search, start_index=index, posts=posts)
            elif k == "s":
                results = header_search(nntp, group, "subject", kw, num_to_search, start_index=index, posts=posts)
            else:
                results = body_search(nntp, group, kw, num_to_search, start_index=index, posts=posts)
            print(f"\nFound {len(results)} posts:\n")
            for r in results:
                print(f"[{r['rel_num']}] #{r['num']}")
                print(f"Group: {group}")
                print(f"From: {r['from']}")
                print(f"Date: {r['date']}")
                print(f"Replies: {r['replies']}")
                print(f"Subject: {r['subject']}\n")
        elif k == "b":
            c = prompt("How many posts? ")
            if c.isdigit():
                print()  # Blank line before first article
                for p2 in posts[index:index+int(c)]:
                    print(f"[{p2['rel_num']}] #{p2['num']}")
                    print(f"Group: {group}")
                    print(f"From: {p2['from']}")
                    print(f"Date: {p2['date']}")
                    print(f"Replies: {p2['replies']}")
                    print(f"Subject: {p2['subject']}\n")
        elif k == "p":
            v = prompt("Lines per page: ")
            if v.isdigit():
                global PAGE_LINES
                PAGE_LINES = int(v)
        elif k == "r":
            show_replies_thread(nntp, group, p["msgid"])
        elif k == "n":
            post_article(nntp, group)
        elif k == "y":
            post_article(nntp, group, p["msgid"], p["subject"])

# ---------- MAIN ----------
def main():
    print(f"\nConnecting to {NNTP_SERVER}:{NNTP_PORT}\n")
    try:
        nntp = nntplib.NNTP_SSL(NNTP_SERVER, NNTP_PORT, USERNAME, PASSWORD)
    except Exception as e:
        print(f"Initial connection failed: {e}")
        sys.exit(1)
    browse_group(nntp, START_GROUP)
    try:
        nntp.quit()
    except:
        pass

if __name__ == "__main__":
    main()