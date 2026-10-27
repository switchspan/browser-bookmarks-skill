"""Build a fake HOME with browser profiles for testing the browser-bookmarks skill.

usage: python3 make_fixtures.py <fake_home> [<firefox_places_template.sqlite>]
"""
import json, plistlib, shutil, sqlite3, sys, uuid
from pathlib import Path

home = Path(sys.argv[1])
template = Path(sys.argv[2]) if len(sys.argv) > 2 else None
T0 = 13300000000000000  # ~2022 in Chromium time


def chrome_node(i, name, url=None, children=None):
    n = {"date_added": str(T0 + i * 1_000_000), "date_last_used": "0",
         "guid": str(uuid.UUID(int=i)), "id": str(i), "name": name}
    if url is None:
        n.update(type="folder", children=children or [], date_modified="0")
    else:
        n.update(type="url", url=url)
    return n


def chromium_profile(udd, prof, name, email, hd, tree):
    pdir = udd / prof
    pdir.mkdir(parents=True, exist_ok=True)
    ctr = [3]

    def build(items):
        out = []
        for it in items:
            ctr[0] += 1
            if isinstance(it, tuple) and isinstance(it[1], list):
                i = ctr[0]
                out.append(chrome_node(i, it[0], children=build(it[1])))
            else:
                out.append(chrome_node(ctr[0], it[0], it[1]))
        return out

    roots = {"bookmark_bar": chrome_node(1, "Bookmarks bar", children=build(tree["bar"])),
             "other": chrome_node(2, "Other bookmarks", children=build(tree["other"])),
             "synced": chrome_node(3, "Mobile bookmarks", children=build(tree.get("mobile", [])))}
    (pdir / "Bookmarks").write_text(json.dumps({"checksum": "0" * 32, "roots": roots, "version": 1}, indent=3))
    (pdir / "Preferences").write_text(json.dumps(
        {"account_info": [{"email": email, "hd": hd}] if email else []}))
    ls = udd / "Local State"
    state = json.loads(ls.read_text()) if ls.exists() else {"profile": {"info_cache": {}}}
    state["profile"]["info_cache"][prof] = {"name": name, "user_name": email, "hosted_domain": hd}
    ls.write_text(json.dumps(state))


personal = {
    "bar": [("Gmail", "https://mail.google.com/mail/u/0/"), ("GitHub", "https://github.com/"),
            ("YouTube", "https://www.youtube.com/")],
    "other": [
        ("Python docs", "https://docs.python.org/3/"),
        ("python docs again", "http://docs.python.org/3/"),
        ("GitHub", "https://www.github.com"),
        ("Serious Eats - Best Chili", "https://www.seriouseats.com/the-best-chili-recipe?utm_source=newsletter&utm_medium=email"),
        ("Serious Eats chili", "https://www.seriouseats.com/the-best-chili-recipe"),
        ("Budget Bytes", "https://www.budgetbytes.com/"),
        ("NYT Cooking", "https://cooking.nytimes.com/"),
        ("Hacker News", "https://news.ycombinator.com/"),
        ("Ars Technica", "https://arstechnica.com/"),
        ("The Verge", "https://www.theverge.com/"),
        ("Rust Book", "https://doc.rust-lang.org/book/"),
        ("MDN", "https://developer.mozilla.org/en-US/"),
        ("Stack Overflow", "https://stackoverflow.com/"),
        ("ACME Jira board", "https://acme.atlassian.net/jira/software/projects/PLAT/boards/12"),
        ("ACME wiki", "https://wiki.acme.com/display/ENG/Onboarding"),
        ("Old blog", "https://this-domain-does-not-exist-zz9.invalid/posts/1"),
        ("", "https://www.rei.com/"),
        ("Strava", "https://www.strava.com/dashboard"),
        ("AllTrails", "https://www.alltrails.com/"),
        ("Chase", "https://www.chase.com/"),
        ("Vanguard", "https://investor.vanguard.com/"),
        ("Netflix", "https://www.netflix.com/browse"),
        ("Empty", []),
        ("Imported From Firefox", [("Rust Book", "https://doc.rust-lang.org/book/"),
                                   ("Recipes", [("Budget Bytes", "https://budgetbytes.com")])]),
    ],
    "mobile": [("Weather", "https://weather.com/")],
}
work = {
    "bar": [("Jira", "https://acme.atlassian.net/jira/your-work"), ("Confluence", "https://acme.atlassian.net/wiki/home"),
            ("Okta", "https://acme.okta.com/app/UserHome")],
    "other": [
        ("Grafana", "https://grafana.internal.acme.com/d/abc/api-latency"),
        ("Runbooks", "https://wiki.acme.com/display/SRE/Runbooks"),
        ("Runbooks", "https://wiki.acme.com/display/SRE/Runbooks/"),
        ("AWS console", "https://console.aws.amazon.com/"),
        ("Fantasy football league", "https://fantasy.espn.com/football/league?leagueId=123"),
        ("Vacation rentals", "https://www.airbnb.com/"),
        ("Datadog", "https://app.datadoghq.com/dashboard/xyz"),
        ("Jenkins", "http://jenkins/job/deploy/"),
    ],
}

config = home / ".config"
chromium_profile(config / "google-chrome", "Default", "Alex", "alex.example@gmail.com", "", personal)
chromium_profile(config / "BraveSoftware" / "Brave-Browser", "Profile 1", "ACME", "alex@acme.com", "acme.com", work)

# Firefox: copy a real places.sqlite (schema from a real Firefox) and add bookmarks.
ff = home / ".mozilla" / "firefox"
pdir = ff / "abcd1234.default-release"
pdir.mkdir(parents=True, exist_ok=True)
(ff / "profiles.ini").write_text(
    "[Profile0]\nName=default-release\nIsRelative=1\nPath=abcd1234.default-release\nDefault=1\n\n"
    "[General]\nStartWithLastProfile=1\nVersion=2\n")
(pdir / "prefs.js").write_text('user_pref("browser.startup.homepage", "about:home");\n')
if template:
    shutil.copy(template, pdir / "places.sqlite")
    db = sqlite3.connect(pdir / "places.sqlite")
    roots = dict(db.execute("SELECT guid, id FROM moz_bookmarks WHERE guid IN "
                            "('toolbar_____','menu________','unfiled_____')").fetchall())
    db.execute("DELETE FROM moz_bookmarks WHERE guid NOT IN ('root________','menu________','toolbar_____','unfiled_____','mobile______','tags________')")
    n = [0]

    def add(parent, title, url=None, pos=0):
        n[0] += 1
        guid = f"fx{n[0]:010d}"
        fk = None
        if url:
            db.execute("INSERT OR IGNORE INTO moz_places(url, url_hash, guid, rev_host, title) "
                       "VALUES (?, 0, ?, ?, ?)", (url, f"pl{n[0]:010d}", "", title))
            fk = db.execute("SELECT id FROM moz_places WHERE url=?", (url,)).fetchone()[0]
        cur = db.execute("INSERT INTO moz_bookmarks(type, fk, parent, position, title, dateAdded, lastModified, guid) "
                         "VALUES (?,?,?,?,?,?,?,?)", (1 if url else 2, fk, parent, pos, title,
                                                       1_650_000_000_000_000 + n[0], 0, guid))
        return cur.lastrowid

    for i, (t, u) in enumerate([("Getting Started", "https://www.mozilla.org/firefox/central/"),
                                ("GitHub", "https://github.com")]):
        add(roots["toolbar_____"], t, u, i)
    m = roots["menu________"]
    add(m, "Hacker News", "https://news.ycombinator.com", 0)
    f = add(m, "Cooking", None, 1)
    add(f, "Serious Eats", "https://www.seriouseats.com/", 0)
    add(f, "NYT Cooking", "https://cooking.nytimes.com/?campaign_id=9", 1)
    add(m, "Rust", "https://www.rust-lang.org/", 2)
    add(roots["unfiled_____"], "Ars", "https://arstechnica.com/", 0)
    db.commit()
    db.close()

# Safari plist (read via --file on Linux; discover only looks for it on macOS)
def leaf(t, u):
    return {"WebBookmarkType": "WebBookmarkTypeLeaf", "WebBookmarkUUID": str(uuid.uuid4()).upper(),
            "URLString": u, "URIDictionary": {"title": t}}

def lst(t, kids):
    return {"WebBookmarkType": "WebBookmarkTypeList", "WebBookmarkUUID": str(uuid.uuid4()).upper(),
            "Title": t, "Children": kids}

safari = {"WebBookmarkFileVersion": 1, "WebBookmarkType": "WebBookmarkTypeList", "Title": "",
          "WebBookmarkUUID": "ROOT", "Children": [
              {"WebBookmarkType": "WebBookmarkTypeProxy", "Title": "History", "WebBookmarkUUID": "H"},
              lst("BookmarksBar", [leaf("Apple", "https://www.apple.com/"), leaf("iCloud", "https://www.icloud.com/")]),
              lst("BookmarksMenu", [leaf("Hacker News", "https://news.ycombinator.com/"),
                                    lst("Travel", [leaf("Kayak", "https://www.kayak.com/"),
                                                   leaf("Kayak", "https://kayak.com")])]),
              lst("com.apple.ReadingList", [leaf("Some article", "https://example.com/a")]),
          ]}
sdir = home / "Library" / "Safari"
sdir.mkdir(parents=True, exist_ok=True)
with open(sdir / "Bookmarks.plist", "wb") as fh:
    plistlib.dump(safari, fh, fmt=plistlib.FMT_BINARY)
print("fixtures in", home)
