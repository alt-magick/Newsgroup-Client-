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

# ================= USER CONFIG =================
NNTP_SERVER = "usnews.blocknews.net"
NNTP_PORT   = 563
USERNAME    = ""
PASSWORD    = ""
PAGE_LINES             = 12
MAX_ARTICLES_LIST      = 200
MAX_REPLY_SCAN         = 300
START_GROUP            = "alt.test"
SHOW_REPLY_COUNT       = False
SHOW_REPLY_COUNT_MAIN  = True
# ==============================================

RE_REPLY = re.compile(r"^(re|fwd):", re.IGNORECASE)
CLEAN_RE = re.compile(r"[\x00-\x08\x0b-\x0c\x0e-\x1f]")

# ---------- STATUS LINE ----------
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

# ---------- HARD-CODED PAGER ----------
def paged_print(lines):
    i = 0
    total = len(lines)
    while i < total:
        end = min(i + PAGE_LINES, total)
        for line in lines[i:end]:
            print(line)
        i = end
        if i >= total:
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

# ---------- POST BODY EDITOR ----------
def edit_body(initial=""):
    editor = os.environ.get("EDITOR", "nano")
    fd, path = tempfile.mkstemp(suffix=".txt")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(initial)
        subprocess.call([editor, path])
        with open(path, "r") as f:
            return f.read()
    finally:
        os.unlink(path)

# ---------- POST BODY SOURCE ----------
def get_post_body():
    print("\nPost body source:")
    print("  E = Edit in editor")
    print("  F = Load from external text file")
    print("  T = Type directly in terminal")
    choice = get_key().lower()

    if choice == "f":
        path = prompt("\nEnter path to text file: ")
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                body = f.read()
            if not body.strip():
                set_status("Post aborted (file is empty)")
                return None
            return body
        except Exception as e:
            set_status(f"Failed to read file: {e}")
            return None

    elif choice == "t":
        print("\nType your post below. End with a period on a line by itself.")
        lines = []
        while True:
            line = input()
            if line.strip() == ".":
                break
            lines.append(line)
        body = "\n".join(lines)
        if not body.strip():
            set_status("Post aborted (empty input)")
            return None
        return body

    body = edit_body()
    if not body.strip():
        set_status("Post aborted (empty body)")
        return None
    return body

# ---------- POSTING ----------
def post_article(nntp, group, subject=None, references=None):
    name = prompt("Enter your display name: ")
    email = prompt("Enter your email: ")
    if not subject:
        subject = prompt("Enter subject: ")

    body = get_post_body()
    if not body:
        return False

    msg = EmailMessage()
    msg["From"] = f"{name} <{email}>"
    msg["Newsgroups"] = group
    msg["Subject"] = subject
    if references:
        msg["References"] = references
    msg.set_content(body)

    try:
        nntp.post(msg.as_bytes())
        set_status("Article posted successfully")
        return True
    except Exception as e:
        set_status(f"Post failed: {e}")
        return False

# ---------- REPLY POSTING ----------
def post_reply(nntp, group, article_num):
    try:
        _, hinfo = nntp.head(str(article_num))
        headers = {}
        for raw in hinfo.lines:
            line = decode_body_line(raw)
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.lower()] = v.strip()

        subject = headers.get("subject", "(no subject)")
        if not RE_REPLY.match(subject):
            subject = "Re: " + subject

        refs = []
        if "references" in headers:
            refs.append(headers["references"])
        if "message-id" in headers:
            refs.append(headers["message-id"])

        return post_article(nntp, group, subject, " ".join(refs))
    except Exception as e:
        set_status(f"Reply failed: {e}")
        return False

# ---------- ARTICLE DISPLAY ----------
def show_article(nntp, num, group=None, allow_reply=False):
    try:
        _, hinfo = nntp.head(str(num))
        headers = {}
        for raw in hinfo.lines:
            line = decode_body_line(raw)
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.lower()] = v.strip()

        _, body = nntp.body(str(num))
        lines = [decode_body_line(l) for l in body.lines]

        paged_print([
            f"From: {headers.get('from','?')}",
            f"Date: {headers.get('date','?')}",
            f"Subject: {headers.get('subject','(no subject)')}",
            ""
        ] + lines)

        if allow_reply and group:
            print("\nP=reply  (any other key to continue)")
            if get_key().lower() == "p":
                post_reply(nntp, group, num)

    except Exception as e:
        set_status(f"Fetch failed: {e}")

# ---------- REPLY SCANNING ----------
def scan_replies_xover(nntp, msgid, first, last):
    replies = []
    start = max(first, last - MAX_REPLY_SCAN)
    try:
        _, overviews = nntp.over((start, last))
    except:
        return replies

    for num, hdr in overviews:
        if msgid in hdr.get("references", ""):
            replies.append(int(num))
    return replies

# ---------- GROUP RELOAD ----------
def reload_group(nntp, group):
    try:
        _, _, first, last, _ = nntp.group(group)
        first = int(first)
        last = int(last)

        _, overviews = nntp.over((max(first, last - MAX_ARTICLES_LIST), last))

        posts = []
        for num, hdr in reversed(overviews):
            subject = hdr.get("subject", "")
            if RE_REPLY.match(subject):
                continue

            msgid = hdr.get("message-id", "")
            replies = sum(
                1 for _, h in overviews if msgid in h.get("references", "")
            ) if SHOW_REPLY_COUNT_MAIN else 0

            posts.append({
                "num": int(num),
                "subject": CLEAN_RE.sub("", subject),
                "from": CLEAN_RE.sub("", hdr.get("from", "?")),
                "date": hdr.get("date", "?"),
                "msgid": msgid,
                "replies": replies
            })

        return posts, first, last

    except Exception as e:
        set_status(f"Reload failed: {e}")
        return None, None, None

# ---------- GROUP BROWSER ----------
def browse_group(nntp, group):
    posts, first, last = reload_group(nntp, group)
    if not posts:
        return

    index = 0

    while index < len(posts):
        p = posts[index]

        print(f"\n[{index+1}] #{p['num']}")
        print(f"From: {p['from']}")
        print(f"Date: {p['date']}")
        print(f"Replies: {p['replies'] if SHOW_REPLY_COUNT_MAIN else '?'}")
        print(f"Subject: {p['subject']}")

        show_status()
        print("\nENTER=read  SPACE=next  BACKSPACE=previous  R=replies  N=new post  L=reload  J=jump  G=group  B=batch list  P=set page lines  S=save post  F=find author  Q=quit")

        key = get_key()
        if key == "q":
            sys.exit(0)
        elif key == " ":
            index += 1
        elif key in ("\r", "\n"):
            show_article(nntp, p["num"], group, True)
        elif key == "\x7f":  # BACKSPACE
            if index > 0:
                index -= 1
        elif key == "n":
            if post_article(nntp, group):
                posts, first, last = reload_group(nntp, group)
                index = 0
        elif key == "l":
            posts, first, last = reload_group(nntp, group)
            index = 0
            set_status("Group reloaded")
        elif key == "j":
            val = prompt("Jump to post number: ")
            if val.isdigit():
                idx = int(val) - 1
                if 0 <= idx < len(posts):
                    index = idx
        elif key == "g":
            browse_group(nntp, prompt("New group: "))
            return
        elif key == "r":
            replies = scan_replies_xover(nntp, p["msgid"], first, last)
            if not replies:
                set_status("No replies found")
                continue

            idx = 0
            while 0 <= idx < len(replies):
                rnum = replies[idx]
                if idx < len(replies) - 1:
                    print("\nENTER=next reply | BACKSPACE=previous | SPACE=skip remaining | P=reply")
                else:
                    print("\nEnd of replies | BACKSPACE=previous | P=reply")

                k = get_key()
                if k == " ":
                    set_status("Skipped remaining replies")
                    break
                elif k in ("\r", "\n"):
                    show_article(nntp, rnum, group, True)
                    idx += 1
                elif k == "\x7f":  # BACKSPACE
                    if idx > 0:
                        idx -= 1
                elif k.lower() == "p":
                    post_reply(nntp, group, rnum)
        elif key.lower() == "b":
            val = prompt("How many posts to list from current position? ")
            if val.isdigit():
                count = int(val)
                end = min(index + count, len(posts))
                print(f"\nListing posts {index+1} to {end}:\n")
                for i in range(index, end):
                    p = posts[i]
                    print(f"[{i+1}] #{p['num']}")
                    print(f"From: {p['from']}")
                    print(f"Date: {p['date']}")
                    print(f"Replies: {p['replies'] if SHOW_REPLY_COUNT_MAIN else '?'}")
                    print(f"Subject: {p['subject']}\n")
                print("--- End of list ---")
                set_status(f"Displayed {end-index} posts")
        elif key.lower() == "p":
            val = prompt("Enter number of lines per page: ")
            if val.isdigit() and int(val) > 0:
                global PAGE_LINES
                PAGE_LINES = int(val)
                set_status(f"Page size set to {PAGE_LINES} lines")
            else:
                set_status("Invalid input, page size unchanged")
        elif key.lower() == "s":
            val = prompt("Enter post number to save: ")
            if val.isdigit():
                idx = int(val) - 1
                if 0 <= idx < len(posts):
                    p = posts[idx]
                    filename = prompt("Enter file name to save as: ")
                    if filename.strip():
                        try:
                            _, hinfo = nntp.head(str(p["num"]))
                            _, body = nntp.body(str(p["num"]))
                            headers_text = "\n".join([decode_body_line(l) for l in hinfo.lines])
                            body_text = "\n".join([decode_body_line(l) for l in body.lines])
                            with open(filename, "w", encoding="utf-8") as f:
                                f.write(headers_text)
                                f.write("\n\n")
                                f.write(body_text)
                            set_status(f"Post saved to {filename}")
                        except Exception as e:
                            set_status(f"Failed to save post: {e}")
                    else:
                        set_status("Invalid file name, save aborted")
                else:
                    set_status("Invalid post number")
            else:
                set_status("Invalid input")
        elif key.lower() == "f":
            keyword = prompt("Enter author keyword: ").strip()
            if not keyword:
                set_status("No keyword entered")
                continue

            val = prompt("How many posts to display? ")
            if not val.isdigit() or int(val) <= 0:
                set_status("Invalid number, search aborted")
                continue
            count = int(val)

            # Fuzzy match function (simple case-insensitive substring)
            def fuzzy_match(author_text, keyword):
                return keyword.lower() in author_text.lower()

            # Find matches
            matches = [p for p in posts if fuzzy_match(p['from'], keyword)]
            if not matches:
                set_status("No matching posts found")
                continue

            # Limit to requested number
            matches = matches[:count]

            print(f"\nListing {len(matches)} matching posts:\n")
            for i, p in enumerate(matches, 1):
                print(f"[{i}] #{p['num']}")
                print(f"From: {p['from']}")
                print(f"Date: {p['date']}")
                print(f"Replies: {p['replies'] if SHOW_REPLY_COUNT_MAIN else '?'}")
                print(f"Subject: {p['subject']}\n")
            print("--- End of list ---")
            set_status(f"Displayed {len(matches)} matching posts")

# ---------- MAIN ----------
def main():
    print(f"Connecting to {NNTP_SERVER}:{NNTP_PORT}...")
    nntp = nntplib.NNTP_SSL(NNTP_SERVER, NNTP_PORT, USERNAME, PASSWORD)
    set_status("Connected")
    browse_group(nntp, START_GROUP)
    nntp.quit()

if __name__ == "__main__":
    main()