#!/usr/bin/env python3
"""Read, analyze, clean, organize and write browser bookmarks.

Supports Chrome, Chromium, Brave (Chromium JSON), Firefox (places.sqlite),
Safari (Bookmarks.plist) and Netscape bookmarks.html, on Linux, macOS and
Windows. Standard library only; Python 3.9+.

Every command works on a normalized JSON document:

  {"format": "bookmarks-normalized/1",
   "sources": [{"source": "chrome:Default", "browser": "chrome", "profile": "Default",
                "context": "work", "path": "..."}],
   "roots": {"toolbar": FOLDER, "menu": FOLDER, "other": FOLDER, "mobile": FOLDER}}

  FOLDER   = {"type": "folder", "title": str, "children": [NODE...], "added": float}
  BOOKMARK = {"type": "bookmark", "id": str, "title": str, "url": str,
              "added": float (unix seconds), "source": str, "native": {...}}

Run `bookmarks.py <command> -h` for each command's options.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import configparser
import datetime as dt
import hashlib
import html
import html.parser
import json
import os
import plistlib
import re
import shutil
import socket
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

FORMAT = "bookmarks-normalized/1"
ROOTS = ("toolbar", "menu", "other", "mobile")
ROOT_TITLES = {"toolbar": "Bookmarks Toolbar", "menu": "Bookmarks Menu",
               "other": "Other Bookmarks", "mobile": "Mobile Bookmarks"}
CHROMIUM_BROWSERS = ("chrome", "chromium", "brave")
BROWSERS = CHROMIUM_BROWSERS + ("firefox", "safari")
HOME = Path.home()
SYSTEM = sys.platform  # linux, darwin, win32
CONFIG_PATH = Path(os.environ.get(
    "BOOKMARKS_CONFIG",
    (Path(os.environ.get("XDG_CONFIG_HOME", HOME / ".config")) / "browser-bookmarks" / "contexts.json")
    if SYSTEM != "win32" else Path(os.environ.get("APPDATA", HOME)) / "browser-bookmarks" / "contexts.json"))
BACKUP_DIR = CONFIG_PATH.parent / "backups"

CONSUMER_EMAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com", "msn.com",
    "icloud.com", "me.com", "mac.com", "yahoo.com", "ymail.com", "aol.com", "proton.me",
    "protonmail.com", "pm.me", "fastmail.com", "gmx.com", "gmx.net", "hey.com", "zoho.com",
    "mail.com", "yandex.com", "tutanota.com", "duck.com",
}
# Hosts that are almost always a company's tenant, so a hit in a personal profile is worth a look.
WORK_TENANT_SUFFIXES = (
    ".atlassian.net", ".sharepoint.com", ".okta.com", ".service-now.com", ".my.salesforce.com",
    ".lightning.force.com", ".zendesk.com", ".myworkday.com", ".slack.com", ".awsapps.com",
    ".onelogin.com", ".bamboohr.com", ".greenhouse.io", ".pagerduty.com", ".datadoghq.com",
    ".ghe.com", ".visualstudio.com", ".freshdesk.com", ".monday.com", ".clickup.com",
)
WORK_NAME_RE = re.compile(r"\b(work|corp|corporate|office|job|company|business|employer|client)\b", re.I)
PERSONAL_NAME_RE = re.compile(r"\b(personal|home|private|family|me|mine|person)\b", re.I)
TRACKING_PARAMS = re.compile(
    r"^(utm_\w+|fbclid|gclid|dclid|gbraid|wbraid|msclkid|mc_cid|mc_eid|igshid|yclid|_hsenc|_hsmi|"
    r"ref_src|ref_url|si|spm|share_source|trk|trkCampaign|oly_anon_id|oly_enc_id|vero_id|__s)$", re.I)


# --------------------------------------------------------------------------- utils

def die(msg: str, code: int = 1):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def dump_json(obj, path):
    if path in (None, "-"):
        json.dump(obj, sys.stdout, indent=2, ensure_ascii=False)
        print()
        return
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
        f.write("\n")


def new_folder(title, children=None, added=None):
    return {"type": "folder", "title": title, "children": children or [], "added": added or 0}


def empty_doc():
    return {"format": FORMAT, "sources": [], "roots": {r: new_folder(ROOT_TITLES[r]) for r in ROOTS}}


def load_doc(path):
    try:
        doc = load_json(path)
    except ValueError:
        doc = {}
    if not isinstance(doc, dict) or doc.get("format") != FORMAT:
        die(f"{path} is not a normalized bookmarks document (run `read` first)")
    return doc


def walk(node, path=()):
    """Yield (path_tuple, node) for every node below a folder, depth first, in order."""
    for child in node.get("children", []):
        yield path, child
        if child["type"] == "folder":
            yield from walk(child, path + (child["title"],))


def walk_doc(doc):
    for root in ROOTS:
        yield from walk(doc["roots"][root], (root,))


def bookmarks(doc):
    return [(p, n) for p, n in walk_doc(doc) if n["type"] == "bookmark"]


def host_of(url):
    try:
        return (urllib.parse.urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def is_web(url):
    return url.lower().startswith(("http://", "https://"))


def normalize_url(url):
    """Key used for duplicate detection. Two URLs with the same key open the same page."""
    try:
        parts = urllib.parse.urlsplit(url.strip())
    except ValueError:
        return url.strip()
    if parts.scheme.lower() not in ("http", "https"):
        return url.strip()
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    port = parts.port
    if port and port not in (80, 443):
        host = f"{host}:{port}"
    path = re.sub(r"/{2,}", "/", parts.path or "/")
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    for idx in ("/index.html", "/index.htm", "/index.php"):
        if path.endswith(idx):
            path = path[: -len(idx)] or "/"
    query = sorted((k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
                   if not TRACKING_PARAMS.match(k))
    frag = parts.fragment
    # Keep hash-router fragments (#/route, #!/route): they select different pages in SPAs.
    frag = frag if frag.startswith(("/", "!")) else ""
    return urllib.parse.urlunsplit(("", host, path, urllib.parse.urlencode(query), frag)).lstrip("/")


def strip_tracking(url):
    """Drop tracking query parameters (utm_*, fbclid, ...) and leave the rest of the URL alone."""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return url
    if not parts.query or parts.scheme.lower() not in ("http", "https"):
        return url
    q = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
         if not TRACKING_PARAMS.match(k)]
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(q)))


IMPORT_FOLDER_RE = re.compile(r"^(imported|from (firefox|chrome|safari|brave|edge|ie)|"
                              r"imported from|bookmarks? (bar|menu) \(\d+\))", re.I)


def chrome_time(value):
    """Chromium stores microseconds since 1601-01-01 as a string."""
    try:
        v = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0.0, v / 1e6 - 11644473600) if v else 0


def to_chrome_time(seconds):
    return str(int((seconds or dt.datetime.now().timestamp()) + 11644473600) * 1_000_000)


# --------------------------------------------------------------------------- config / contexts

def load_config():
    if CONFIG_PATH.exists():
        cfg = load_json(CONFIG_PATH)
    else:
        cfg = {}
    cfg.setdefault("profiles", {})
    cfg.setdefault("work_domains", [])
    cfg.setdefault("personal_domains", [])
    return cfg


def guess_context(name, email, hosted_domain):
    """Return (context, reason). context is work | personal | unknown."""
    domain = email.split("@")[-1].lower() if email and "@" in email else ""
    if hosted_domain and hosted_domain.upper() != "NO_HOSTED_DOMAIN":
        return "work", f"Google Workspace account (hosted domain {hosted_domain})"
    if domain and domain not in CONSUMER_EMAIL_DOMAINS:
        return "work", f"signed in with non-consumer email domain {domain}"
    if name and WORK_NAME_RE.search(name):
        return "work", f"profile name '{name}'"
    if name and PERSONAL_NAME_RE.search(name):
        return "personal", f"profile name '{name}'"
    if domain:
        return "personal", f"signed in with consumer email domain {domain}"
    return "unknown", "no account or name signal"


def context_for(source, cfg=None, guess=("unknown", "")):
    cfg = cfg if cfg is not None else load_config()
    configured = cfg["profiles"].get(source)
    if configured:
        return configured, "configured in contexts.json"
    return guess


# --------------------------------------------------------------------------- discovery

def chromium_user_data_dirs():
    local = Path(os.environ.get("LOCALAPPDATA", HOME / "AppData" / "Local"))
    support = HOME / "Library" / "Application Support"
    config = Path(os.environ.get("XDG_CONFIG_HOME", HOME / ".config"))
    table = {
        "chrome": {"linux": [config / "google-chrome", config / "google-chrome-beta",
                             HOME / ".var/app/com.google.Chrome/config/google-chrome"],
                   "darwin": [support / "Google" / "Chrome"],
                   "win32": [local / "Google" / "Chrome" / "User Data"]},
        "chromium": {"linux": [config / "chromium", HOME / "snap/chromium/common/chromium",
                               HOME / ".var/app/org.chromium.Chromium/config/chromium"],
                     "darwin": [support / "Chromium"],
                     "win32": [local / "Chromium" / "User Data"]},
        "brave": {"linux": [config / "BraveSoftware" / "Brave-Browser",
                            HOME / "snap/brave/current/.config/BraveSoftware/Brave-Browser",
                            HOME / ".var/app/com.brave.Browser/config/BraveSoftware/Brave-Browser"],
                  "darwin": [support / "BraveSoftware" / "Brave-Browser"],
                  "win32": [local / "BraveSoftware" / "Brave-Browser" / "User Data"]},
    }
    plat = SYSTEM if SYSTEM in ("darwin", "win32") else "linux"
    return {b: [p for p in table[b][plat] if p.is_dir()] for b in table}


def firefox_roots():
    plat = SYSTEM if SYSTEM in ("darwin", "win32") else "linux"
    if plat == "darwin":
        cands = [HOME / "Library" / "Application Support" / "Firefox"]
    elif plat == "win32":
        cands = [Path(os.environ.get("APPDATA", HOME / "AppData" / "Roaming")) / "Mozilla" / "Firefox"]
    else:
        config = Path(os.environ.get("XDG_CONFIG_HOME", HOME / ".config"))
        cands = [HOME / ".mozilla" / "firefox", config / "mozilla" / "firefox",
                 HOME / "snap/firefox/common/.mozilla/firefox",
                 HOME / ".var/app/org.mozilla.firefox/.mozilla/firefox"]
    return [p for p in cands if (p / "profiles.ini").exists()]


def count_chromium(path):
    try:
        data = load_json(path)
    except (OSError, ValueError):
        return None
    return sum(1 for r in data.get("roots", {}).values() if isinstance(r, dict)
               for _ in _chromium_urls(r))


def _chromium_urls(node):
    if node.get("type") == "url":
        yield node
    for c in node.get("children", []):
        yield from _chromium_urls(c)


def discover_profiles():
    cfg = load_config()
    out = []
    for browser, dirs in chromium_user_data_dirs().items():
        for udd in dirs:
            try:
                info = load_json(udd / "Local State").get("profile", {}).get("info_cache", {})
            except (OSError, ValueError):
                info = {}
            names = set(info) | {p.name for p in udd.iterdir()
                                 if p.is_dir() and (p / "Preferences").exists()}
            for prof in sorted(names):
                pdir = udd / prof
                if not pdir.is_dir():
                    continue
                meta = info.get(prof, {})
                email, hd = meta.get("user_name", ""), meta.get("hosted_domain", "")
                try:
                    accts = load_json(pdir / "Preferences").get("account_info") or []
                    if accts:
                        email = email or accts[0].get("email", "")
                        hd = hd or accts[0].get("hd", "")
                except (OSError, ValueError):
                    pass
                name = meta.get("name", prof)
                source = f"{browser}:{prof}"
                ctx, why = context_for(source, cfg, guess_context(name, email, hd))
                bfile = pdir / "Bookmarks"
                acct_file = pdir / "AccountBookmarks"
                out.append({
                    "source": source, "browser": browser, "profile": prof, "name": name,
                    "email": email, "hosted_domain": hd if hd != "NO_HOSTED_DOMAIN" else "",
                    "context": ctx, "context_reason": why, "dir": str(pdir),
                    "bookmarks_file": str(bfile) if bfile.exists() else None,
                    "bookmark_count": count_chromium(bfile) if bfile.exists() else 0,
                    "account_bookmarks_count": count_chromium(acct_file) if acct_file.exists() else 0,
                    "sync_signed_in": bool(email),
                    "user_data_dir": str(udd),
                })
    for root in firefox_roots():
        ini = configparser.RawConfigParser()
        ini.read(root / "profiles.ini", encoding="utf-8")
        for sec in ini.sections():
            if not sec.startswith("Profile"):
                continue
            rel = ini.get(sec, "IsRelative", fallback="1") == "1"
            pdir = (root / ini.get(sec, "Path")) if rel else Path(ini.get(sec, "Path"))
            if not pdir.is_dir():
                continue
            name = ini.get(sec, "Name", fallback=pdir.name)
            email = ""
            try:
                email = load_json(pdir / "signedInUser.json").get("accountData", {}).get("email", "")
            except (OSError, ValueError):
                pass
            source = f"firefox:{pdir.name}"
            ctx, why = context_for(source, cfg, guess_context(name, email, ""))
            places = pdir / "places.sqlite"
            out.append({
                "source": source, "browser": "firefox", "profile": pdir.name, "name": name,
                "email": email, "hosted_domain": "", "context": ctx, "context_reason": why,
                "dir": str(pdir), "bookmarks_file": str(places) if places.exists() else None,
                "bookmark_count": _count_firefox(places) if places.exists() else 0,
                "sync_signed_in": bool(email),
                "default": ini.get(sec, "Default", fallback="0") == "1",
            })
    safari = HOME / "Library" / "Safari" / "Bookmarks.plist"
    if SYSTEM == "darwin":
        source = "safari:default"
        ctx, why = context_for(source, cfg, ("shared", "Safari profiles share one bookmark store"))
        readable = os.access(safari, os.R_OK)
        out.append({
            "source": source, "browser": "safari", "profile": "default", "name": "Safari",
            "email": "", "hosted_domain": "", "context": ctx, "context_reason": why,
            "dir": str(safari.parent), "bookmarks_file": str(safari) if safari.exists() else None,
            "bookmark_count": None if not readable else _count_safari(safari),
            "note": "Safari profiles (macOS 14+) share bookmarks; each profile only picks its own "
                    "Favorites folder." + ("" if readable else
                    " Not readable: grant the terminal Full Disk Access."),
        })
    return out


def _count_firefox(places):
    try:
        with _firefox_copy(places) as db:
            return db.execute("SELECT count(*) FROM moz_bookmarks WHERE type=1 AND parent NOT IN "
                              "(SELECT id FROM moz_bookmarks WHERE parent IN "
                              "(SELECT id FROM moz_bookmarks WHERE guid='tags________'))"
                              " AND parent != (SELECT id FROM moz_bookmarks WHERE guid='tags________')"
                              ).fetchone()[0]
    except Exception:
        return None


def _count_safari(path):
    try:
        with open(path, "rb") as f:
            data = plistlib.load(f)
    except Exception:
        return None
    return sum(1 for _ in _safari_leaves(data))


def _safari_leaves(node):
    if node.get("WebBookmarkType") == "WebBookmarkTypeLeaf":
        yield node
    for c in node.get("Children", []) or []:
        yield from _safari_leaves(c)


def find_profile(browser, profile):
    matches = [p for p in discover_profiles() if p["browser"] == browser
               and (profile is None or profile in (p["profile"], p["name"], p["source"]))]
    if not matches:
        die(f"no {browser} profile matching {profile!r}; run `discover`")
    if len(matches) > 1 and profile is None:
        default = [p for p in matches if p.get("default") or p["profile"] == "Default"]
        if len(default) == 1:
            return default[0]
        die(f"several {browser} profiles found ({', '.join(p['profile'] for p in matches)}); "
            "pass --profile")
    return matches[0]


# --------------------------------------------------------------------------- browser state

PROCESS_NAMES = {
    "chrome": {"linux": ["chrome"], "darwin": ["Google Chrome"], "win32": ["chrome.exe"]},
    "chromium": {"linux": ["chromium", "chromium-browser"], "darwin": ["Chromium"],
                 "win32": ["chrome.exe"]},
    "brave": {"linux": ["brave"], "darwin": ["Brave Browser"], "win32": ["brave.exe"]},
    "firefox": {"linux": ["firefox", "firefox-bin", "firefox.real", "firefox-esr"],
                "darwin": ["firefox"], "win32": ["firefox.exe"]},
    "safari": {"linux": [], "darwin": ["Safari"], "win32": []},
}


def browser_running(browser, prof=None):
    plat = SYSTEM if SYSTEM in ("darwin", "win32") else "linux"
    # Prefer per-profile lock symlinks, which exist only while that profile is open, so another
    # open profile of the same browser does not block. Fall back to process names elsewhere.
    if prof and browser in CHROMIUM_BROWSERS and plat != "win32":
        return os.path.lexists(Path(prof["user_data_dir"]) / "SingletonLock")
    if prof and browser == "firefox" and plat == "linux":
        return os.path.lexists(Path(prof["dir"]) / "lock")
    for name in PROCESS_NAMES[browser][plat]:
        try:
            if plat == "win32":
                r = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {name}"],
                                   capture_output=True, text=True)
                if name.lower() in r.stdout.lower():
                    return True
            else:
                r = subprocess.run(["pgrep", "-x", name], capture_output=True)
                if r.returncode == 0:
                    return True
        except FileNotFoundError:
            continue
    return False


def backup(paths, label):
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = BACKUP_DIR / f"{label}-{stamp}"
    dest.mkdir(parents=True, exist_ok=True)
    for p in paths:
        p = Path(p)
        if p.exists():
            shutil.copy2(p, dest / p.name)
    return dest


# --------------------------------------------------------------------------- readers

def read_chromium(path, source):
    data = load_json(path)
    roots = data.get("roots", {})
    doc = empty_doc()

    def conv(node):
        if node.get("type") == "url":
            return {"type": "bookmark", "id": f"{source}#{node.get('id')}", "title": node.get("name", ""),
                    "url": node.get("url", ""), "added": chrome_time(node.get("date_added")),
                    "source": source, "native": {"id": node.get("id"), "guid": node.get("guid")}}
        return new_folder(node.get("name", ""), [conv(c) for c in node.get("children", [])],
                          chrome_time(node.get("date_added")))

    for key, root in (("bookmark_bar", "toolbar"), ("other", "other"), ("synced", "mobile")):
        if isinstance(roots.get(key), dict):
            doc["roots"][root]["children"] = [conv(c) for c in roots[key].get("children", [])]
    return doc


class _firefox_copy:
    """Open a copy of places.sqlite, so a running Firefox's lock and WAL do not get in the way."""

    def __init__(self, places):
        self.places = Path(places)

    def __enter__(self):
        self.tmp = tempfile.mkdtemp(prefix="places-")
        for suffix in ("", "-wal"):
            src = Path(str(self.places) + suffix)
            if src.exists():
                shutil.copy2(src, Path(self.tmp) / ("places.sqlite" + suffix))
        self.db = sqlite3.connect(Path(self.tmp) / "places.sqlite")
        return self.db

    def __exit__(self, *exc):
        self.db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)


def read_firefox(path, source):
    doc = empty_doc()
    with _firefox_copy(path) as db:
        rows = db.execute(
            "SELECT b.id, b.type, b.parent, b.position, b.title, p.url, b.dateAdded, b.guid "
            "FROM moz_bookmarks b LEFT JOIN moz_places p ON b.fk = p.id "
            "ORDER BY b.parent, b.position").fetchall()
    kids, by_guid = {}, {}
    for r in rows:
        kids.setdefault(r[2], []).append(r)
        by_guid[r[7]] = r

    def conv(row):
        bid, typ, _parent, _pos, title, url, added, guid = row
        added = (added or 0) / 1e6
        if typ == 1:
            return {"type": "bookmark", "id": f"{source}#{guid}", "title": title or "",
                    "url": url or "", "added": added, "source": source, "native": {"guid": guid}}
        if typ == 2:
            return new_folder(title or "", [c for c in (conv(k) for k in kids.get(bid, [])) if c], added)
        return None  # separators

    for guid, root in (("toolbar_____", "toolbar"), ("menu________", "menu"),
                       ("unfiled_____", "other"), ("mobile______", "mobile")):
        if guid in by_guid:
            doc["roots"][root]["children"] = [c for c in (conv(k) for k in kids.get(by_guid[guid][0], []))
                                              if c]
    return doc


def read_safari(path, source):
    with open(path, "rb") as f:
        data = plistlib.load(f)
    doc = empty_doc()

    def conv(node):
        kind = node.get("WebBookmarkType")
        if kind == "WebBookmarkTypeLeaf":
            return {"type": "bookmark", "id": f"{source}#{node.get('WebBookmarkUUID')}",
                    "title": (node.get("URIDictionary") or {}).get("title", ""),
                    "url": node.get("URLString", ""), "added": 0, "source": source,
                    "native": {"uuid": node.get("WebBookmarkUUID")}}
        if kind == "WebBookmarkTypeList":
            return new_folder(node.get("Title", ""),
                              [c for c in (conv(k) for k in node.get("Children", []) or []) if c])
        return None  # proxies (History) and anything unknown

    for child in data.get("Children", []) or []:
        title = child.get("Title", "")
        if title == "com.apple.ReadingList":
            continue  # Reading List is a queue, not bookmarks; leave it alone
        node = conv(child)
        if node is None:
            continue
        if title == "BookmarksBar":
            doc["roots"]["toolbar"]["children"] = node["children"]
        elif title == "BookmarksMenu":
            doc["roots"]["menu"]["children"] = node["children"]
        else:
            doc["roots"]["other"]["children"].append(node)
    return doc


class _NetscapeParser(html.parser.HTMLParser):
    def __init__(self, source):
        super().__init__(convert_charrefs=True)
        self.source, self.doc = source, empty_doc()
        self.stack = [self.doc["roots"]["menu"]]
        self.pending, self.text, self.in_tag, self.n = None, "", None, 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "h3":
            self.in_tag, self.text = "h3", ""
            if a.get("personal_toolbar_folder") == "true":
                self.pending = self.doc["roots"]["toolbar"]
            elif a.get("unfiled_bookmarks_folder") == "true":
                self.pending = self.doc["roots"]["other"]
            else:
                self.pending = new_folder("", added=float(a.get("add_date") or 0))
        elif tag == "a":
            self.in_tag, self.text = "a", ""
            self.n += 1
            self.cur = {"type": "bookmark", "id": f"{self.source}#{self.n}", "title": "",
                        "url": a.get("href", ""), "added": float(a.get("add_date") or 0),
                        "source": self.source, "native": {}}
        elif tag == "dl" and self.pending is not None:
            self.stack.append(self.pending)
            self.pending = None

    def handle_endtag(self, tag):
        if tag == "h3" and self.in_tag == "h3":
            if self.pending is not None and self.pending not in self.doc["roots"].values():
                self.pending["title"] = self.text.strip()
                self.stack[-1]["children"].append(self.pending)
            self.in_tag = None
        elif tag == "a" and self.in_tag == "a":
            self.cur["title"] = self.text.strip()
            self.stack[-1]["children"].append(self.cur)
            self.in_tag = None
        elif tag == "dl" and len(self.stack) > 1:
            self.stack.pop()

    def handle_data(self, data):
        if self.in_tag:
            self.text += data


def read_html(path, source):
    p = _NetscapeParser(source)
    p.feed(Path(path).read_text(encoding="utf-8", errors="replace"))
    doc = p.doc
    # Chrome/Safari exports have one top-level "Bookmarks bar"/"Favorites" folder and no toolbar flag.
    menu = doc["roots"]["menu"]["children"]
    if not doc["roots"]["toolbar"]["children"] and menu and menu[0]["type"] == "folder" and \
            menu[0]["title"].lower() in ("bookmarks bar", "bookmarks toolbar", "favorites", "favourites"):
        doc["roots"]["toolbar"]["children"] = menu.pop(0)["children"]
    for i, c in enumerate(menu):  # render() writes the mobile root as a top-level folder
        if c["type"] == "folder" and c["title"] == ROOT_TITLES["mobile"] and not doc["roots"]["mobile"]["children"]:
            doc["roots"]["mobile"]["children"] = menu.pop(i)["children"]
            break
    return doc


def cmd_read(args):
    cfg = load_config()
    if args.file:
        path = Path(args.file)
        fmt = args.format or {".json": "chromium", ".sqlite": "firefox", ".plist": "safari",
                              ".html": "html", ".htm": "html"}.get(path.suffix.lower()) or \
            ("chromium" if path.name in ("Bookmarks", "AccountBookmarks") else None)
        if not fmt:
            die("cannot tell the file's format; pass --format")
        source = args.source or f"{args.browser or fmt}:{path.stem}"
        ctx = args.context or context_for(source, cfg)[0]
        info = {"source": source, "browser": args.browser or fmt, "profile": path.stem,
                "context": ctx, "path": str(path)}
    else:
        if not args.browser:
            die("pass --browser (and --profile) or --file")
        prof = find_profile(args.browser, args.profile)
        if not prof["bookmarks_file"]:
            die(f"{prof['source']} has no bookmarks file")
        path = Path(prof["bookmarks_file"])
        fmt = "chromium" if args.browser in CHROMIUM_BROWSERS else args.browser
        source = prof["source"]
        info = {"source": source, "browser": args.browser, "profile": prof["profile"],
                "name": prof["name"], "email": prof["email"],
                "context": args.context or prof["context"], "path": str(path)}
        if args.account and args.browser in CHROMIUM_BROWSERS:
            path = path.with_name("AccountBookmarks")
            info["path"] = str(path)
    reader = {"chromium": read_chromium, "firefox": read_firefox, "safari": read_safari,
              "html": read_html}[fmt]
    try:
        doc = reader(path, source)
    except PermissionError:
        die(f"permission denied reading {path}"
            + (" — on macOS give the terminal Full Disk Access" if fmt == "safari" else ""))
    for _, n in walk_doc(doc):
        if n["type"] == "bookmark":
            n["context"] = info["context"]
    doc["sources"] = [info]
    dump_json(doc, args.output)
    n = len(bookmarks(doc))
    print(f"read {n} bookmarks from {source} ({info['context']}) -> {args.output}", file=sys.stderr)


# --------------------------------------------------------------------------- analysis

def work_domains(doc, cfg):
    doms = {d.lower().lstrip("*.") for d in cfg["work_domains"]}
    for p in discover_profiles_cached():
        if p["context"] == "work":
            if p.get("hosted_domain"):
                doms.add(p["hosted_domain"].lower())
            email_dom = p["email"].split("@")[-1].lower() if "@" in p.get("email", "") else ""
            if email_dom and email_dom not in CONSUMER_EMAIL_DOMAINS:
                doms.add(email_dom)
    for s in doc["sources"]:
        email_dom = s.get("email", "").split("@")[-1].lower() if "@" in s.get("email", "") else ""
        if s.get("context") == "work" and email_dom and email_dom not in CONSUMER_EMAIL_DOMAINS:
            doms.add(email_dom)
    return doms


_DISCOVERED = None


def discover_profiles_cached():
    global _DISCOVERED
    if _DISCOVERED is None:
        try:
            _DISCOVERED = discover_profiles()
        except Exception:
            _DISCOVERED = []
    return _DISCOVERED


def host_matches(host, patterns):
    for pat in patterns:
        pat = pat.lower().lstrip("*.")
        if host == pat or host.endswith("." + pat):
            return pat
    return None


def classify_bookmark(url, work_doms, personal_doms):
    """Return (likely_context, reason) for one bookmark, or (None, None) without a signal."""
    host = host_of(url)
    if not host:
        return None, None
    hit = host_matches(host, work_doms)
    if hit:
        return "work", f"work domain {hit}"
    hit = host_matches(host, personal_doms)
    if hit:
        return "personal", f"personal domain {hit}"
    for suf in WORK_TENANT_SUFFIXES:
        if host.endswith(suf) and host.count(".") >= 2 and not host.startswith(("www.", "app.")):
            return "work?", f"company tenant host ({suf[1:]})"
    if "." not in host or host.endswith((".local", ".internal", ".corp", ".lan", ".intranet")) \
            or re.match(r"^(10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.)", host):
        return "work?", "internal/intranet host"
    return None, None


def find_duplicates(doc):
    groups = {}
    for path, n in bookmarks(doc):
        groups.setdefault(normalize_url(n["url"]), []).append(("/".join(path), n))
    return {k: v for k, v in groups.items() if len(v) > 1}


def cmd_analyze(args):
    doc = load_doc(args.input)
    cfg = load_config()
    bms = bookmarks(doc)
    dupes = find_duplicates(doc)
    folders = {}
    empty, deep = [], []
    for path, n in walk_doc(doc):
        if n["type"] == "folder":
            fpath = "/".join(path + (n["title"],))
            folders[fpath] = sum(1 for _, c in walk(n) if c["type"] == "bookmark")
            if not n["children"]:
                empty.append(fpath)
            if len(path) > 5:
                deep.append(fpath)
    domains = {}
    for path, n in bms:
        h = host_of(n["url"])
        h = h[4:] if h.startswith("www.") else h
        d = domains.setdefault(h or n["url"].split(":")[0] + ":", {"count": 0, "titles": []})
        d["count"] += 1
        if len(d["titles"]) < 3 and n["title"]:
            d["titles"].append(n["title"][:80])
    wd, pd = work_domains(doc, cfg), {d.lower() for d in cfg["personal_domains"]}
    mismatches = []
    for path, n in bms:
        likely, why = classify_bookmark(n["url"], wd, pd)
        ctx = n.get("context", "unknown")
        if likely and ctx in ("work", "personal") and likely.rstrip("?") != ctx:
            mismatches.append({"id": n["id"], "title": n["title"], "url": n["url"],
                               "folder": "/".join(path), "profile_context": ctx,
                               "looks": likely, "reason": why})
    report = {
        "sources": doc["sources"],
        "totals": {"bookmarks": len(bms), "folders": len(folders),
                   "duplicate_groups": len(dupes),
                   "redundant_copies": sum(len(v) - 1 for v in dupes.values()),
                   "empty_folders": len(empty), "untitled": sum(1 for _, n in bms if not n["title"].strip()),
                   "non_web": sum(1 for _, n in bms if not is_web(n["url"]))},
        "contexts": sorted({n.get("context", "unknown") for _, n in bms}),
        "work_domains_used": sorted(wd),
        "context_mismatches": mismatches,
        "duplicates": [{"key": k, "copies": [{"folder": f, "title": n["title"], "url": n["url"],
                                              "id": n["id"], "source": n.get("source")}
                                             for f, n in v]}
                       for k, v in list(dupes.items())[: args.max_dupes]],
        "empty_folders": empty,
        "deep_folders": deep,
        "folders": folders,
        "top_domains": dict(sorted(domains.items(), key=lambda kv: -kv[1]["count"])[: args.top]),
        "singleton_domain_count": sum(1 for d in domains.values() if d["count"] == 1),
    }
    dump_json(report, args.output)


# --------------------------------------------------------------------------- transforms

def prune_empty(folder):
    kept = []
    for c in folder["children"]:
        if c["type"] == "folder":
            prune_empty(c)
            if not c["children"]:
                continue
        kept.append(c)
    folder["children"] = kept


def cmd_dedupe(args):
    doc = load_doc(args.input)
    order = {r: i for i, r in enumerate(ROOTS)}
    cleaned = 0
    if not args.keep_tracking:
        for _, n in bookmarks(doc):
            u = strip_tracking(n["url"])
            cleaned += u != n["url"]
            n["url"] = u

    groups = {}
    for path, n in bookmarks(doc):
        groups.setdefault(normalize_url(n["url"]), []).append((path, n))
    drop, log = set(), []
    for key, items in groups.items():
        if len(items) < 2:
            continue
        # Keep the copy a person filed most deliberately: toolbar first, then not inside an
        # "Imported from ..." dump, then the deepest folder, then a real title, then the oldest.
        items.sort(key=lambda pn: (order[pn[0][0]],
                                   any(IMPORT_FOLDER_RE.match(t) for t in pn[0][1:]),
                                   -len(pn[0]), not pn[1]["title"].strip(),
                                   pn[1]["added"] or float("inf")))
        keep = items[0][1]
        if not keep["title"].strip():
            keep["title"] = next((n["title"] for _, n in items if n["title"].strip()), "")
        if is_web(keep["url"]) and keep["url"].startswith("http://"):
            https = next((n["url"] for _, n in items if n["url"].startswith("https://")), None)
            keep["url"] = https or keep["url"]
        for path, n in items[1:]:
            drop.add(id(n))
            log.append({"kept": {"folder": "/".join(items[0][0]), "url": keep["url"]},
                        "removed": {"folder": "/".join(path), "title": n["title"], "url": n["url"],
                                    "source": n.get("source")}})

    def filt(folder):
        folder["children"] = [c for c in folder["children"] if id(c) not in drop]
        for c in folder["children"]:
            if c["type"] == "folder":
                filt(c)

    for r in ROOTS:
        filt(doc["roots"][r])
        if args.prune_empty:
            prune_empty(doc["roots"][r])
    dump_json(doc, args.output)
    if args.log:
        dump_json(log, args.log)
    print(f"removed {len(log)} duplicate bookmarks, stripped tracking parameters from {cleaned} "
          f"URLs -> {args.output}", file=sys.stderr)


def check_url(url, timeout):
    ctx = ssl.create_default_context()
    headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                             "Chrome/124.0 Safari/537.36", "Accept": "text/html,*/*;q=0.8"}
    last = None
    for method in ("HEAD", "GET"):
        req = urllib.request.Request(url, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                final = resp.geturl()
                return {"status": "ok", "code": resp.status,
                        "final_url": final if final != url else None}
        except urllib.error.HTTPError as e:
            last = e.code
            if method == "HEAD" and e.code in (400, 403, 405, 406, 429, 500, 501, 503):
                continue  # many servers mishandle HEAD; retry with GET
            if e.code in (404, 410):
                return {"status": "dead", "code": e.code, "reason": f"HTTP {e.code}"}
            return {"status": "uncertain", "code": e.code,
                    "reason": f"HTTP {e.code} (often bot-blocking, login or rate limit — not proof it is gone)"}
        except urllib.error.URLError as e:
            reason = e.reason
            if isinstance(reason, socket.gaierror):
                return {"status": "dead", "code": None, "reason": f"DNS lookup failed ({reason})"}
            if isinstance(reason, ssl.SSLError):
                return {"status": "uncertain", "code": None, "reason": f"TLS error ({reason})"}
            if isinstance(reason, ConnectionRefusedError):
                return {"status": "dead", "code": None, "reason": "connection refused"}
            return {"status": "uncertain", "code": None, "reason": str(reason)}
        except (TimeoutError, socket.timeout):
            return {"status": "uncertain", "code": None, "reason": "timeout"}
        except Exception as e:  # noqa: BLE001 — a link checker must never crash on one URL
            return {"status": "uncertain", "code": None, "reason": f"{type(e).__name__}: {e}"}
    return {"status": "uncertain", "code": last, "reason": f"HTTP {last}"}


def cmd_checklinks(args):
    doc = load_doc(args.input)
    cfg = load_config()
    wd = work_domains(doc, cfg)
    targets, skipped = {}, 0
    for path, n in bookmarks(doc):
        host = host_of(n["url"])
        intranet = classify_bookmark(n["url"], wd, set())[0] in ("work", "work?")
        if not is_web(n["url"]) or (intranet and not args.include_work):
            skipped += 1
            continue
        targets.setdefault(n["url"], []).append(n)
    if args.limit:
        targets = dict(list(targets.items())[: args.limit])
    results = {}
    print(f"checking {len(targets)} URLs ({skipped} skipped: non-web, or work/intranet hosts that "
          "only resolve on the work network — pass --include-work to check them)", file=sys.stderr)
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(check_url, u, args.timeout): u for u in targets}
        for i, fut in enumerate(concurrent.futures.as_completed(futs), 1):
            results[futs[fut]] = fut.result()
            if i % 50 == 0:
                print(f"  {i}/{len(targets)}", file=sys.stderr)
    dead_ids = {id(n) for u, r in results.items() if r["status"] == "dead" for n in targets[u]}
    if args.output:
        holding = new_folder(args.dead_folder)
        for r in ROOTS:
            def pull(folder):
                keep = []
                for c in folder["children"]:
                    if c["type"] == "bookmark" and id(c) in dead_ids:
                        holding["children"].append(c)
                    else:
                        if c["type"] == "folder":
                            pull(c)
                        keep.append(c)
                folder["children"] = keep
            pull(doc["roots"][r])
        if holding["children"]:
            doc["roots"]["other"]["children"].append(holding)
        dump_json(doc, args.output)
    summary = {s: sum(1 for r in results.values() if r["status"] == s) for s in ("ok", "dead", "uncertain")}
    dump_json({"summary": summary, "results": results}, args.results)
    print(f"ok={summary['ok']} dead={summary['dead']} uncertain={summary['uncertain']}"
          + (f"; dead links moved to other/{args.dead_folder}" if args.output else ""), file=sys.stderr)


def cmd_merge(args):
    docs = [load_doc(p) for p in args.inputs]
    contexts = {s.get("context", "unknown") for d in docs for s in d["sources"]}
    known = contexts - {"unknown", "shared"}
    if len(known) > 1 and not args.allow_mixed_contexts:
        die(f"inputs mix contexts {sorted(contexts)}; merging work and personal bookmarks is "
            "refused unless the user explicitly asks (--allow-mixed-contexts)")
    out = empty_doc()
    for d in docs:
        out["sources"].extend(d["sources"])
        label = ", ".join(s["source"] for s in d["sources"])
        for r in ROOTS:
            kids = d["roots"][r]["children"]
            if not kids:
                continue
            if args.layout == "per-source":
                out["roots"][r]["children"].append(new_folder(f"From {label}", kids))
            else:
                out["roots"][r]["children"].extend(kids)
    dump_json(out, args.output)
    print(f"merged {len(docs)} documents ({len(bookmarks(out))} bookmarks) -> {args.output}",
          file=sys.stderr)


def ensure_folder(doc, path):
    parts = [p for p in path.strip("/").split("/") if p]
    if not parts or parts[0] not in ROOTS:
        die(f"folder path {path!r} must start with one of {', '.join(ROOTS)}")
    node = doc["roots"][parts[0]]
    for title in parts[1:]:
        nxt = next((c for c in node["children"] if c["type"] == "folder" and c["title"] == title), None)
        if nxt is None:
            nxt = new_folder(title, added=dt.datetime.now().timestamp())
            node["children"].append(nxt)
        node = nxt
    return node


def rule_matches(rule, n):
    host = host_of(n["url"])
    host = host[4:] if host.startswith("www.") else host
    for pat in rule.get("match", []):
        pat = pat.lower()
        if "/" in pat:
            hp = host + urllib.parse.urlsplit(n["url"]).path
            if hp.startswith(pat.lstrip("*.").removeprefix("www.")):
                return True
        elif host_matches(host, [pat]):
            return True
    title = n["title"].lower()
    if any(t.lower() in title for t in rule.get("title_contains", [])):
        return True
    url = n["url"].lower()
    return any(re.search(rx, url) for rx in rule.get("url_regex", []))


def cmd_organize(args):
    doc = load_doc(args.input)
    rules = load_json(args.rules)
    overrides = rules.get("overrides", {})
    unmatched = rules.get("unmatched", "keep")
    keep_toolbar = rules.get("keep_toolbar", True)
    titles = rules.get("titles", {})
    placed, moved = [], 0
    for path, n in bookmarks(doc):
        if n["id"] in titles:
            n["title"] = titles[n["id"]]
        here = "/".join(path)
        target = overrides.get(n["id"])
        if target is None and keep_toolbar and path[0] == "toolbar":
            target = here  # the toolbar is hand-curated and visible; leave it as the user built it
        if target is None:
            target = next((r["folder"] for r in rules.get("rules", []) if rule_matches(r, n)), None)
        if target is None:
            target = here if unmatched == "keep" else unmatched
        moved += target != here
        placed.append((target, n))
    out = {"format": FORMAT, "sources": doc["sources"],
           "roots": {r: new_folder(ROOT_TITLES[r]) for r in ROOTS}}
    # Create rule folders in rule order first so the result follows the proposed taxonomy.
    for r in rules.get("rules", []):
        if any(t == r["folder"] for t, _ in placed):
            ensure_folder(out, r["folder"])
    for target, n in placed:
        ensure_folder(out, target)["children"].append(n)
    if rules.get("sort") == "title":
        def srt(folder):
            folder["children"].sort(key=lambda c: (c["type"] != "folder", c["title"].lower()))
            for c in folder["children"]:
                if c["type"] == "folder":
                    srt(c)
        for r in ROOTS:
            if r != "toolbar":
                srt(out["roots"][r])
    for r in ROOTS:
        prune_empty(out["roots"][r])
    dump_json(out, args.output)
    print(f"organized {len(placed)} bookmarks ({moved} moved) -> {args.output}", file=sys.stderr)


# --------------------------------------------------------------------------- writers

def render_html(doc):
    lines = ["<!DOCTYPE NETSCAPE-Bookmark-file-1>",
             "<!-- This is an automatically generated file. It will be read and overwritten. -->",
             '<META HTTP-EQUIV="Content-Type" CONTENT="text/html; charset=UTF-8">',
             "<TITLE>Bookmarks</TITLE>", "<H1>Bookmarks Menu</H1>", "<DL><p>"]
    now = int(dt.datetime.now().timestamp())

    def emit(node, depth):
        pad = "    " * depth
        for c in node["children"]:
            added = int(c.get("added") or now)
            if c["type"] == "folder":
                lines.append(f'{pad}<DT><H3 ADD_DATE="{added}">{html.escape(c["title"])}</H3>')
                lines.append(f"{pad}<DL><p>")
                emit(c, depth + 1)
                lines.append(f"{pad}</DL><p>")
            else:
                lines.append(f'{pad}<DT><A HREF="{html.escape(c["url"], quote=True)}" '
                             f'ADD_DATE="{added}">{html.escape(c["title"])}</A>')

    emit(doc["roots"]["menu"], 1)
    for root, flag in (("toolbar", "PERSONAL_TOOLBAR_FOLDER"), ("other", "UNFILED_BOOKMARKS_FOLDER"),
                       ("mobile", None)):
        if not doc["roots"][root]["children"]:
            continue
        attr = f' {flag}="true"' if flag else ""
        lines.append(f'    <DT><H3 ADD_DATE="{now}"{attr}>{ROOT_TITLES[root]}</H3>')
        lines.append("    <DL><p>")
        emit(doc["roots"][root], 2)
        lines.append("    </DL><p>")
    lines.append("</DL><p>")
    return "\n".join(lines) + "\n"


def cmd_render(args):
    doc = load_doc(args.input)
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(render_html(doc), encoding="utf-8")
    print(f"wrote {len(bookmarks(doc))} bookmarks to {args.output}", file=sys.stderr)


def chromium_checksum(roots):
    md5, sha = hashlib.md5(), hashlib.sha256()

    def upd(s, utf16=False):
        b = s.encode("utf-16-le") if utf16 else s.encode("utf-8")
        md5.update(b)
        sha.update(b)

    def node(n):
        upd(n["id"])
        upd(n.get("name", ""), utf16=True)
        if n["type"] == "url":
            upd("url")
            upd(n["url"])
        else:
            upd("folder")
            for c in n.get("children", []):
                node(c)

    for key in ("bookmark_bar", "other", "synced"):
        node(roots[key])
    return md5.hexdigest(), sha.hexdigest()


def build_chromium(original, doc, source):
    roots = original["roots"]
    used = set()
    max_id = [0]

    def scan(n):
        try:
            max_id[0] = max(max_id[0], int(n.get("id", 0)))
        except ValueError:
            pass
        for c in n.get("children", []):
            scan(c)
    for k in ("bookmark_bar", "other", "synced"):
        scan(roots[k])
        used.add(roots[k]["id"])

    def fresh_id():
        max_id[0] += 1
        return str(max_id[0])

    def conv(n):
        if n["type"] == "bookmark":
            nat = n.get("native", {}) if n.get("source") == source else {}
            nid = nat.get("id")
            if not nid or nid in used:
                nid = fresh_id()
            used.add(nid)
            return {"date_added": to_chrome_time(n.get("added")), "date_last_used": "0",
                    "guid": nat.get("guid") or str(uuid.uuid4()), "id": nid,
                    "name": n["title"], "type": "url", "url": n["url"]}
        nid = fresh_id()
        used.add(nid)
        return {"children": [conv(c) for c in n["children"]],
                "date_added": to_chrome_time(n.get("added")),
                "date_last_used": "0", "date_modified": to_chrome_time(None),
                "guid": str(uuid.uuid4()), "id": nid, "name": n["title"], "type": "folder"}

    new = json.loads(json.dumps(original))
    nr = new["roots"]
    nr["bookmark_bar"]["children"] = [conv(c) for c in doc["roots"]["toolbar"]["children"]]
    nr["other"]["children"] = [conv(c) for c in doc["roots"]["other"]["children"]]
    nr["synced"]["children"] = [conv(c) for c in doc["roots"]["mobile"]["children"]]
    if doc["roots"]["menu"]["children"]:  # Chromium has no menu root
        nr["other"]["children"].insert(0, conv(new_folder("Bookmarks Menu", doc["roots"]["menu"]["children"])))
    for k in ("bookmark_bar", "other", "synced"):
        nr[k]["date_modified"] = to_chrome_time(None)
    md5, sha = chromium_checksum(nr)
    new["checksum"] = md5
    if "checksum_sha256" in new:
        new["checksum_sha256"] = sha
    return new


def build_safari(original, doc):
    def leaf(n):
        nat = n.get("native", {})
        return {"WebBookmarkType": "WebBookmarkTypeLeaf",
                "WebBookmarkUUID": nat.get("uuid") or str(uuid.uuid4()).upper(),
                "URLString": n["url"], "URIDictionary": {"title": n["title"]}}

    def conv(n):
        if n["type"] == "bookmark":
            return leaf(n)
        return {"WebBookmarkType": "WebBookmarkTypeList", "WebBookmarkUUID": str(uuid.uuid4()).upper(),
                "Title": n["title"], "Children": [conv(c) for c in n["children"]]}

    new = dict(original)
    kids = []
    for child in original.get("Children", []):
        t = child.get("Title")
        if t == "BookmarksBar":
            kids.append({**child, "Children": [conv(c) for c in doc["roots"]["toolbar"]["children"]]})
        elif t == "BookmarksMenu":
            menu = [conv(c) for c in doc["roots"]["menu"]["children"]]
            if doc["roots"]["mobile"]["children"]:
                menu.append(conv(new_folder("Mobile Bookmarks", doc["roots"]["mobile"]["children"])))
            kids.append({**child, "Children": menu})
            kids.extend(conv(c) for c in doc["roots"]["other"]["children"])
        elif child.get("WebBookmarkType") == "WebBookmarkTypeList" and t != "com.apple.ReadingList":
            continue  # old top-level folders are replaced by doc["roots"]["other"]
        else:
            kids.append(child)  # History proxy, Reading List
    new["Children"] = kids
    return new


def cmd_write(args):
    doc = load_doc(args.input)
    cfg = load_config()
    if args.file:
        target = Path(args.file)
        prof = {"source": args.source or f"{args.browser}:{target.stem}", "dir": str(target.parent),
                "user_data_dir": str(target.parent.parent), "bookmarks_file": str(target),
                "context": context_for(args.source or "", cfg)[0], "sync_signed_in": False,
                "account_bookmarks_count": 0, "profile": target.stem}
    else:
        prof = find_profile(args.browser, args.profile)
        target = Path(prof["bookmarks_file"] or Path(prof["dir"]) / "Bookmarks")
    doc_contexts = {s.get("context") for s in doc["sources"]} - {"unknown", "shared", None}
    if prof["context"] in ("work", "personal") and doc_contexts and doc_contexts != {prof["context"]} \
            and not args.allow_mixed_contexts:
        die(f"document holds {sorted(doc_contexts)} bookmarks but {prof['source']} is a "
            f"{prof['context']} profile; pass --allow-mixed-contexts only if the user asked for this")
    if not args.file and browser_running(args.browser, prof):
        die(f"{args.browser} is running; quit it completely first (the browser rewrites its "
            "bookmarks on exit and would undo the change)")
    if args.browser in CHROMIUM_BROWSERS and (prof.get("sync_signed_in") or prof.get("account_bookmarks_count")) \
            and not args.force_synced:
        die(f"{prof['source']} is signed in to sync. Writing the file directly fights sync: the "
            "server copy can restore or duplicate what you removed. Prefer the import route "
            "(render HTML, delete bookmarks in the browser, import). Pass --force-synced to "
            "write anyway after turning sync off.")
    n = len(bookmarks(doc))
    if args.browser in CHROMIUM_BROWSERS:
        original = load_json(target)
        new = build_chromium(original, doc, prof["source"])
        if args.dry_run:
            print(f"dry run: would write {n} bookmarks to {target}")
            return
        bdir = backup([target, target.with_name("Bookmarks.bak")], prof["source"].replace(":", "-"))
        tmp = target.with_name("Bookmarks.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(new, f, indent=3, ensure_ascii=False)
        os.replace(tmp, target)
        bak = target.with_name("Bookmarks.bak")
        if bak.exists():
            bak.unlink()  # stale .bak can be preferred over a file the browser thinks is newer-but-odd
    elif args.browser == "firefox":
        pdir = Path(prof["dir"])
        if args.dry_run:
            print(f"dry run: would stage {n} bookmarks in {pdir / 'bookmarks.html'} for replace-import")
            return
        bdir = backup([pdir / "places.sqlite", pdir / "places.sqlite-wal", pdir / "bookmarks.html",
                       pdir / "prefs.js"], prof["source"].replace(":", "-"))
        (pdir / "bookmarks.html").write_text(render_html(doc), encoding="utf-8")
        prefs = pdir / "prefs.js"
        text = prefs.read_text(encoding="utf-8") if prefs.exists() else ""
        text = re.sub(r'^user_pref\("browser\.(places\.importBookmarksHTML|bookmarks\.file)".*\n?', "",
                      text, flags=re.M)
        text += 'user_pref("browser.places.importBookmarksHTML", true);\n'
        prefs.write_text(text, encoding="utf-8")
        print("Firefox will REPLACE all bookmarks with the staged bookmarks.html on its next start, "
              "then reset the pref itself.", file=sys.stderr)
    elif args.browser == "safari":
        with open(target, "rb") as f:
            original = plistlib.load(f)
        new = build_safari(original, doc)
        if args.dry_run:
            print(f"dry run: would write {n} bookmarks to {target}")
            return
        bdir = backup([target], "safari")
        with open(target, "wb") as f:
            plistlib.dump(new, f, fmt=plistlib.FMT_BINARY)
    else:
        die(f"unsupported browser {args.browser}")
    print(f"wrote {n} bookmarks for {prof['source']}; backup in {bdir}")


def cmd_discover(args):
    profiles = discover_profiles()
    if args.json:
        dump_json(profiles, "-")
        return
    if not profiles:
        print("no browser profiles found")
        return
    for p in profiles:
        who = p["email"] or "not signed in"
        print(f"{p['source']:<38} {p['context']:<9} {str(p['bookmark_count']):>6} bookmarks  "
              f"name={p['name']!r} account={who}")
        print(f"{'':<38} why: {p['context_reason']}; file: {p['bookmarks_file']}")
        if p.get("account_bookmarks_count"):
            print(f"{'':<38} also {p['account_bookmarks_count']} account (sync-only) bookmarks "
                  "in AccountBookmarks — read with --account")
        if p.get("note"):
            print(f"{'':<38} note: {p['note']}")
    print(f"\ncontext config: {CONFIG_PATH}" + ("" if CONFIG_PATH.exists() else " (not created yet)"))


def cmd_set_context(args):
    cfg = load_config()
    if args.context == "clear":
        cfg["profiles"].pop(args.source, None)
    elif args.source:
        cfg["profiles"][args.source] = args.context
    for d in args.work_domain or []:
        if d not in cfg["work_domains"]:
            cfg["work_domains"].append(d)
    for d in args.personal_domain or []:
        if d not in cfg["personal_domains"]:
            cfg["personal_domains"].append(d)
    dump_json(cfg, CONFIG_PATH)
    print(f"saved {CONFIG_PATH}")


# --------------------------------------------------------------------------- CLI

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("discover", help="list browser profiles with work/personal context")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_discover)

    p = sub.add_parser("set-context", help="record a profile's context and work/personal domains")
    p.add_argument("source", nargs="?", help="profile source id from discover, e.g. chrome:Default")
    p.add_argument("context", nargs="?", default=None, choices=["work", "personal", "shared", "clear"])
    p.add_argument("--work-domain", action="append", help="domain that marks a bookmark as work")
    p.add_argument("--personal-domain", action="append", help="domain that marks a bookmark as personal")
    p.set_defaults(func=cmd_set_context)

    p = sub.add_parser("read", help="read one profile (or file) into normalized JSON")
    p.add_argument("--browser", choices=BROWSERS)
    p.add_argument("--profile", help="profile dir, display name or source id")
    p.add_argument("--file", help="read this file instead of a discovered profile")
    p.add_argument("--format", choices=["chromium", "firefox", "safari", "html"])
    p.add_argument("--source", help="source id to stamp when using --file")
    p.add_argument("--context", choices=["work", "personal", "shared", "unknown"])
    p.add_argument("--account", action="store_true", help="Chromium: read AccountBookmarks instead")
    p.add_argument("-o", "--output", required=True)
    p.set_defaults(func=cmd_read)

    p = sub.add_parser("analyze", help="report duplicates, empty folders, domains, context mismatches")
    p.add_argument("input")
    p.add_argument("-o", "--output", default="-")
    p.add_argument("--top", type=int, default=60)
    p.add_argument("--max-dupes", type=int, default=200)
    p.set_defaults(func=cmd_analyze)

    p = sub.add_parser("dedupe", help="remove duplicate URLs, keeping the best-filed copy")
    p.add_argument("input")
    p.add_argument("-o", "--output", required=True)
    p.add_argument("--log", help="write a JSON list of what was removed")
    p.add_argument("--prune-empty", action="store_true", help="also drop folders left empty")
    p.add_argument("--keep-tracking", action="store_true", help="do not strip utm_*/fbclid/... params")
    p.set_defaults(func=cmd_dedupe)

    p = sub.add_parser("checklinks", help="check URLs over the network (opt-in)")
    p.add_argument("input")
    p.add_argument("--results", required=True, help="JSON file for per-URL results")
    p.add_argument("-o", "--output", help="also write a document with dead links moved aside")
    p.add_argument("--dead-folder", default="Dead links (review)")
    p.add_argument("--workers", type=int, default=16)
    p.add_argument("--timeout", type=float, default=10)
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--include-work", action="store_true", help="also check work/intranet hosts")
    p.set_defaults(func=cmd_checklinks)

    p = sub.add_parser("merge", help="combine several normalized documents")
    p.add_argument("inputs", nargs="+")
    p.add_argument("-o", "--output", required=True)
    p.add_argument("--layout", choices=["combined", "per-source"], default="combined")
    p.add_argument("--allow-mixed-contexts", action="store_true")
    p.set_defaults(func=cmd_merge)

    p = sub.add_parser("organize", help="refile bookmarks into folders using a rules file")
    p.add_argument("input")
    p.add_argument("--rules", required=True)
    p.add_argument("-o", "--output", required=True)
    p.set_defaults(func=cmd_organize)

    p = sub.add_parser("render", help="write a Netscape bookmarks.html for import")
    p.add_argument("input")
    p.add_argument("-o", "--output", required=True)
    p.set_defaults(func=cmd_render)

    p = sub.add_parser("write", help="write in place (backup first; browser must be closed)")
    p.add_argument("input")
    p.add_argument("--browser", required=True, choices=BROWSERS)
    p.add_argument("--profile")
    p.add_argument("--file", help="write to this bookmarks file instead of a discovered profile")
    p.add_argument("--source", help="source id of --file, so native ids are kept")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--force-synced", action="store_true")
    p.add_argument("--allow-mixed-contexts", action="store_true")
    p.set_defaults(func=cmd_write)

    args = ap.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
