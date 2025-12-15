#!/usr/bin/env python3
import nntplib
import sys
import termios
import tty
import re
import quopri
import base64
from email.message import EmailMessage
import getpass
import os
import tempfile
import subprocess

# ================= USER CONFIG =================
NNTP_SERVER = "usnews.blocknews.net"
NNTP_PORT   = 563
USERNAME    = "name"
PASSWORD    = "password"
PAGE_LINES             = 12
MAX_ARTICLES_LIST      = 200
MAX_REPLY_SCAN         = 300
START_GROUP            = "alt.test"
SHOW_REPLY_COUNT       = False
SHOW_REPLY_COUNT_MAIN  = True
# ==============================================

RE_REPLY = re.compile(r"^(re|fwd):", re.IGNORECASE)
CLEAN_RE = re.compile(r"[\x00-\x08\x0b-\x0c\x0e-\x1f]")

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

def press_any_key(msg="Press any key to continue..."):
    print(msg)
    get_key()

# ---------- HARD-CODED PAGER ----------
def paged_print(lines):
    global PAGE_LINES
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
        k = get_key()
        if k == " ":
            break
        elif k in ("\r", "\n"):
            continue

# ---------- BODY DECODER ----------
def decode_body_line(line_bytes):
    s = line_bytes.decode("utf-8", errors="replace")
    s = CLEAN_RE.sub("", s)
    if "=" in s:
        try:
            s = quopri.decodestring(s).decode("utf-8", errors="replace")
        except Exception:
            pass
    b64chars = re.fullmatch(r'[A-Za-z0-9+/=\s]+', s)
    if b64chars and len(s.strip()) > 20:
        try:
            s = base64.b64decode(s, validate=True).decode("utf-8", errors="replace")
        except Exception:
            pass
    return s

# ---------- POST BODY EDITOR ----------
def edit_body(initial=""):
    EDITOR = os.environ.get("EDITOR","nano")
    fd, path = tempfile.mkstemp(suffix=".txt")
    try:
        with os.fdopen(fd, 'w') as f:
            f.write(initial)
        subprocess.call([EDITOR, path])
        with open(path, 'r') as f:
            content = f.read()
        return content
    finally:
        os.unlink(path)

# ---------- POSTING ----------
def post_article(nntp, group, subject=None, references=None):
    name = prompt("Enter your display name: ")
    email = prompt("Enter your email: ")
    if not subject:
        subject = prompt("Enter subject: ")
    body = edit_body()
    if not body.strip():
        print("Empty body. Aborting post.")
        return
    msg = EmailMessage()
    msg["From"] = f"{name} <{email}>"
    msg["Newsgroups"] = group
    msg["Subject"] = subject
    if references:
        msg["References"] = references
    msg.set_content(body)
    try:
        resp = nntp.post(msg.as_bytes())
        print("Posted successfully!")
    except Exception as e:
        print(f"Failed to post article: {e}")
    press_any_key()

# ---------- REPLY POSTING ----------
def post_reply(nntp, group, article_num):
    try:
        _, hinfo = nntp.head(str(article_num))
        headers = {}
        for raw in hinfo.lines:
            line = decode_body_line(raw)
            if ":" in line:
                k, v = line.split(":",1)
                headers[k.lower()]=v.strip()
        subject = headers.get("subject", "(no subject)")
        if not RE_REPLY.match(subject):
            subject = "Re: " + subject
        references = headers.get("references", "") + " " + headers.get("message-id", "")
        post_article(nntp, group, subject=subject, references=references.strip())
    except Exception as e:
        print(f"Cannot reply to article {article_num}: {e}")
        press_any_key()

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
        header_lines = [
            f"From: {headers.get('from','?')}",
            f"Date: {headers.get('date','?')}",
            f"Subject: {headers.get('subject','(no subject)')}",
        ]
        all_lines = header_lines + [""] + lines
        paged_print(all_lines)
        if allow_reply and group:
            print("\nP=reply  (any key to continue)")
            k = get_key().lower()
            if k == "p":
                post_reply(nntp, group, num)
        else:
            press_any_key()
    except Exception as e:
        print(f"Failed to fetch article: {e}")
        press_any_key()

# ---------- REPLY SCANNING ----------
def scan_replies_xover(nntp, msgid, first, last):
    global MAX_REPLY_SCAN
    replies = []
    start = max(first, last - MAX_REPLY_SCAN)
    print("\nScanning for replies...")
    try:
        _, overviews = nntp.over((start, last))
    except:
        return []
    total = len(overviews)
    for i, (num, hdr) in enumerate(overviews, 1):
        if msgid in hdr.get("references", ""):
            replies.append(int(num))
        print(f"\rScanned {i}/{total}", end="", flush=True)
    print()
    return replies

# ---------- GROUP BROWSER ----------
last_seen_article = {}

def browse_group(nntp, group):
    global PAGE_LINES, MAX_ARTICLES_LIST, MAX_REPLY_SCAN, START_GROUP, SHOW_REPLY_COUNT, SHOW_REPLY_COUNT_MAIN, last_seen_article
    try:
        _, _, first, last, _ = nntp.group(group)
    except Exception as e:
        print(f"Failed to open group: {e}")
        return
    first = int(first)
    last  = int(last)
    # fetch recent articles
    try:
        _, overviews = nntp.over((max(first, last - MAX_ARTICLES_LIST), last))
    except Exception as e:
        print(f"Error loading articles: {e}")
        return
    posts = []
    for num, hdr in reversed(overviews):
        subject = hdr.get("subject", "")
        msgid = hdr.get("message-id", "")
        if RE_REPLY.match(subject):
            continue
        reply_count = 0
        if msgid and SHOW_REPLY_COUNT_MAIN:
            for _, h in overviews:
                if msgid in h.get("references", ""):
                    reply_count += 1
        posts.append({
            "num": int(num),
            "subject": CLEAN_RE.sub("", subject),
            "from": CLEAN_RE.sub("", hdr.get("from", "?")),
            "date": hdr.get("date", "?"),
            "msgid": msgid,
            "replies": reply_count
        })
    last_seen_article[group] = posts[0]["num"] if posts else last
    index = 0
    while index < len(posts):
        p = posts[index]
        print(f"\n[{index+1}] #{p['num']}")
        print(f"From: {p['from']}")
        print(f"Date: {p['date']}")
        print(f"Replies: {p['replies'] if SHOW_REPLY_COUNT_MAIN else '?'}")
        print(f"Subject: {p['subject']}")
        print("\nENTER=read  SPACE=next  R=replies  N=new post  J=jump  G=group  L=reload  C=config  Q=quit")
        key = get_key().lower()
        if key == "q":
            sys.exit(0)
        elif key == " ":
            index += 1
        elif key in ("\r","\n"):
            show_article(nntp,p["num"], group=group, allow_reply=True)
        elif key == "r":
            replies = scan_replies_xover(nntp, p["msgid"], first, last)
            if not replies:
                print("No replies found.")
                press_any_key()
                continue
            print(f"\n{len(replies)} replies found.")
            for i, rnum in enumerate(replies):
                if i < len(replies) - 1:
                    print("\nENTER = next reply | SPACE = skip remaining replies | P=reply")
                else:
                    print("\nEnd of replies | P=reply")
                k = get_key().lower()
                if k == " " and i < len(replies) - 1:
                    break
                elif k in ("\r","\n"):
                    show_article(nntp,rnum, group=group, allow_reply=True)
                elif k == "p":
                    post_reply(nntp, group, rnum)
        elif key == "n":
            post_article(nntp, group)
        elif key == "j":
            val = prompt("Jump to post number: ")
            if val.isdigit():
                idx = int(val)-1
                if 0 <= idx < len(posts):
                    index = idx
        elif key == "g":
            newg = prompt("New group: ")
            browse_group(nntp,newg)
            return
        elif key == "l":
            last_num = last_seen_article.get(group,last)
            try:
                _, overviews = nntp.over((last_num+1,last))
            except:
                print("No new articles.")
                press_any_key()
                continue
            new_posts=[]
            for num,hdr in reversed(overviews):
                subject = hdr.get("subject","")
                msgid = hdr.get("message-id","")
                if RE_REPLY.match(subject):
                    continue
                reply_count = 0
                if msgid and SHOW_REPLY_COUNT_MAIN:
                    for _, h in overviews:
                        if msgid in h.get("references",""):
                            reply_count+=1
                new_posts.append({
                    "num": int(num),
                    "subject": CLEAN_RE.sub("",subject),
                    "from": CLEAN_RE.sub("",hdr.get("from","?")),
                    "date": hdr.get("date","?"),
                    "msgid": msgid,
                    "replies": reply_count
                })
            if new_posts:
                print(f"{len(new_posts)} new articles found!")
                posts = new_posts + posts
                last_seen_article[group] = new_posts[0]["num"]
            else:
                print("No new articles.")
                press_any_key()
        elif key == "c":
            while True:
                print("\n=== CONFIG MENU ===")
                print(f"1. Set page lines (current {PAGE_LINES})")
                print(f"2. Set max articles to list (current {MAX_ARTICLES_LIST})")
                print(f"3. Set max replies to scan (current {MAX_REPLY_SCAN})")
                print(f"4. Set default group (current {START_GROUP})")
                print(f"5. Toggle showing reply counts when reading replies (current {'ON' if SHOW_REPLY_COUNT else 'OFF'})")
                print(f"6. Toggle showing reply counts on main menu (current {'ON' if SHOW_REPLY_COUNT_MAIN else 'OFF'})")
                print("0. Return to main menu")
                choice = prompt("Select option: ")
                if choice=="0":
                    break
                elif choice=="1":
                    val = prompt("Enter number of lines per page: ")
                    if val.isdigit() and int(val)>0:
                        PAGE_LINES=int(val)
                elif choice=="2":
                    val=prompt("Enter max articles to list: ")
                    if val.isdigit() and int(val)>0:
                        MAX_ARTICLES_LIST=int(val)
                elif choice=="3":
                    val=prompt("Enter max replies to scan: ")
                    if val.isdigit() and int(val)>0:
                        MAX_REPLY_SCAN=int(val)
                elif choice=="4":
                    val=prompt("Enter default NNTP group: ")
                    if val:
                        START_GROUP=val
                elif choice=="5":
                    SHOW_REPLY_COUNT=not SHOW_REPLY_COUNT
                elif choice=="6":
                    SHOW_REPLY_COUNT_MAIN=not SHOW_REPLY_COUNT_MAIN

# ---------- MAIN ----------
def main():
    print(f"Connecting to {NNTP_SERVER}:{NNTP_PORT}...")
    nntp = nntplib.NNTP_SSL(NNTP_SERVER, NNTP_PORT, USERNAME, PASSWORD)
    print("Connected successfully!")
    browse_group(nntp, START_GROUP)
    nntp.quit()

if __name__=="__main__":
    main()
