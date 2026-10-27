---
name: browser-bookmarks
description: Clean up, dedupe, organize, link-check, merge and back up browser bookmarks in Firefox, Chrome, Chromium, Brave and Safari, on Linux, macOS or Windows, while keeping work and personal browser profiles separate. Use this whenever the user wants to tidy, sort, declutter, deduplicate, reorganize or categorize bookmarks or favorites, find dead or broken bookmark links, merge or move bookmarks between browsers or profiles, export or import bookmarks.html, or sort out work vs personal bookmarks, even if they only say "my bookmarks are a mess".
metadata:
  author: Ken Taylor
  version: "1.0.0"
---

# Browser bookmarks

Everything goes through `scripts/bookmarks.py` in this skill's folder (Python 3.9+, standard library only). It reads each browser's own store into a common JSON tree, transforms that tree, and writes the result back out. Start by pointing a variable at its absolute path, using the folder this SKILL.md was loaded from, so the commands work from any working directory:

```bash
BM="/absolute/path/to/browser-bookmarks/scripts/bookmarks.py"   # the folder holding this SKILL.md
python3 "$BM" --help
```

Run `python3 "$BM" <command> -h` for each command's options. Put working files in a scratch or temp directory, not in the user's home.

Some agents sandbox shell commands. Reading bookmarks only needs read access. Writing in place (and the backups under `~/.config/browser-bookmarks/`) needs write access outside the workspace, and `checklinks` needs network access. When the sandbox blocks one of these, ask the user to approve that one command; don't look for a way around the sandbox.

Bookmarks are years of someone's curation. Losing them is the one outcome that is hard to undo, so the workflow is always: read → propose → user approves → write, with a backup.

## 1. Discover profiles and settle work vs personal

```bash
python3 "$BM" discover
```

This lists every profile it finds, with its bookmark count, signed-in account, and a **context**: `work`, `personal` or `unknown`, plus the reason. The guess comes from a Google Workspace hosted domain, the account's email domain (gmail/outlook/icloud and similar mean personal; a company domain means work), and the profile name. A saved setting in `contexts.json` overrides the guess.

Show the user the list. Ask them to confirm any guess, and to classify every `unknown` profile, before you change anything. Then save the answers so future runs know them:

```bash
python3 "$BM" set-context "chrome:Profile 1" work --work-domain acme.com --work-domain acme.atlassian.net
python3 "$BM" set-context firefox:abcd.default-release personal --personal-domain mybank.com
```

The work domains matter. They are how the tool spots a work bookmark sitting in a personal profile.

Why the separation matters: work profiles are often managed, synced to an employer account and visible to IT. Personal bookmarks in there leak private life to the employer, and work links in a personal profile can leak company internals or vanish with the job. Therefore:

- Never merge, move or copy bookmarks across contexts unless the user explicitly asks. `merge` and `write` refuse mixed contexts unless you pass `--allow-mixed-contexts`, and you pass it only after the user says so for this run.
- Treat each context as its own cleanup. When the user says "clean up my bookmarks" and has both kinds, do one pass per context and say which is which.
- Report cross-context items, but don't move them silently. `analyze` lists `context_mismatches` (for example, the company Jira in a personal profile). Also skim titles in work profiles for obviously personal things (banking, fantasy sports, travel, dating, health) that domain rules can't catch. Suggest a destination, such as "Move to work profile", and let the user decide.
- Safari profiles (macOS 14+) all share one bookmark store, so Safari's context is usually `shared`. Don't reorganize it as if it were purely one or the other without asking.

## 2. Read and analyze

```bash
python3 "$BM" read --browser brave --profile "Profile 1" -o /tmp/bm/brave-work.json
python3 "$BM" analyze /tmp/bm/brave-work.json -o /tmp/bm/brave-work.report.json
```

`read` also takes `--file` for any `Bookmarks` JSON, `places.sqlite`, `Bookmarks.plist` or exported `bookmarks.html`. It reads a copy of the file, so the browser can stay open while you read.

The report holds: totals, duplicate groups (by normalized URL, so http/https, `www.`, trailing slashes and tracking parameters are ignored), empty and very deep folders, the current folder list with counts, top domains with sample titles, and context mismatches. Read the report rather than the full tree. Large libraries run to thousands of bookmarks, so open the tree only for specific lookups (for example, to find ids for `overrides`).

Summarize the findings for the user in a few lines: how many bookmarks, how many duplicates, empty folders, mismatches, and what the current structure looks like.

## 3. Clean up

Run only the operations the user wants:

**Dedupe.** `dedupe in.json -o out.json --log removed.json --prune-empty`. It keeps the copy the user filed most deliberately: the toolbar first, then a folder that isn't an "Imported from…" dump, then the deepest folder, then a real title, then the oldest. It prefers the https URL, and strips `utm_*`, `fbclid` and similar tracking parameters from every URL (`--keep-tracking` disables that). Show the user a sample of what was removed.

**Dead links** (network, opt-in; tell the user first that it contacts every site). `checklinks in.json --results links.json -o out.json` moves only clear failures (404, 410, DNS failure, connection refused) into `other/Dead links (review)` and never deletes. Results marked `uncertain` (403, 429, timeouts, TLS errors) are usually bot-blocking or login walls, not dead sites, so leave them in place and just list them. Work and intranet hosts are skipped by default, because off the VPN they all look dead. If most links fail, suspect the network, not the bookmarks.

**Organize** into folders. Use the report's `top_domains`, `folders` and sample titles to propose a taxonomy. Show it to the user as an indented tree with rough counts and get a yes before applying it. Good taxonomies are shallow (two levels at most) and hold 5–12 top-level folders named the way the user talks ("Cooking", not "Culinary Resources"). They reuse the user's existing good folders instead of renaming everything. Leave the toolbar alone; it is hand-curated and always visible. Then write a rules file and apply it:

```json
{
  "rules": [
    {"folder": "other/Dev/Python", "match": ["python.org", "pypi.org", "*.readthedocs.io"]},
    {"folder": "other/Cooking", "match": ["seriouseats.com", "budgetbytes.com"], "title_contains": ["recipe"]},
    {"folder": "other/Reading", "match": ["github.com/trending", "news.ycombinator.com"], "url_regex": ["/blog/"]}
  ],
  "overrides": {"chrome:Default#412": "other/Move to work profile"},
  "titles": {"chrome:Default#77": "REI"},
  "unmatched": "keep",
  "keep_toolbar": true,
  "sort": "title"
}
```

```bash
python3 "$BM" organize in.json --rules rules.json -o organized.json
```

Folder paths start with a root: `toolbar`, `menu` (Firefox/Safari), `other`, or `mobile`. The first matching rule wins. A `match` entry is a domain (subdomains included) or a `host/path` prefix. `overrides` and `titles` key on bookmark `id`s from the tree. `unmatched` is `"keep"` (stay where they are) or a folder such as `"other/Unsorted"`. Empty folders are pruned.

**Merge across browsers or profiles.** Read each profile, then run `merge a.json b.json -o merged.json`, then `dedupe`. Use `--layout per-source` to keep a "From chrome:Default" folder per input instead of interleaving. Merges stay within one context (see section 1).

## 4. Write the result

Ask how the user wants it applied, and recommend based on sync status:

**Export (the default, and safe everywhere).** `render organized.json -o ~/bookmarks-cleaned.html`. The user imports it themselves, and nothing in the live profile is touched. Recommend this for any profile signed in to Chrome, Brave or Firefox Sync, or to iCloud: an import goes through the browser, so sync propagates it properly. Give per-browser import steps from `references/browsers.md`, including clearing the old bookmarks first so the import doesn't duplicate everything.

**In place** (only when the user picks it). The browser must be fully quit:

```bash
python3 "$BM" write organized.json --browser chrome --profile Default --dry-run
python3 "$BM" write organized.json --browser chrome --profile Default
```

`write` backs up the original files to `~/.config/browser-bookmarks/backups/<profile>-<timestamp>/` and prints the path; give it to the user. It refuses to run while that profile is open. It refuses Chrome or Brave profiles signed in to sync unless you pass `--force-synced`, which you pass only after the user has turned sync off and understands the risk. Per browser:

- **Chrome / Chromium / Brave**: rewrites the `Bookmarks` JSON with a valid checksum, and keeps ids and guids for bookmarks that stay.
- **Firefox**: stages the result as `bookmarks.html` in the profile and sets `browser.places.importBookmarksHTML`. On next start, Firefox itself replaces all bookmarks with the file and resets the pref. Tell the user the change happens when they next open Firefox. With Firefox Sync on, this propagates as a normal edit. This *is* the hands-off, in-place route. When a user says "no import file" or "edit the profile directly", they mean they don't want to import by hand, so use `write` and explain that Firefox applies it by itself. Don't write your own SQL against `places.sqlite`. Firefox keeps url hashes, `foreign_count`, frecency, positions and sync counters consistent across several tables, and a hand edit that misses one can silently lose bookmarks later (history expiry deletes places it believes are unreferenced).
- **Safari**: rewrites `Bookmarks.plist` and leaves the Reading List and History proxy untouched. The terminal needs Full Disk Access. If iCloud bookmark sync is on, prefer export, because iCloud can overwrite a local edit.

After writing, re-`read` the profile (or, for Firefox, after the user restarts it), confirm the counts match, and report. Always end with the backup path and the restore steps below, so the user can undo without asking.

To restore, quit the browser and copy the files from the backup folder back over the profile's files. For Firefox, restore `places.sqlite` and delete `places.sqlite-wal`, and remove the `importBookmarksHTML` line from `prefs.js` if Firefox has not started since.

## Reference

`references/browsers.md` has file locations per OS, the storage formats, sync behavior, and import/export steps for each browser. Read it when you are importing, exporting, troubleshooting a missing profile, or working on a platform you haven't handled yet.
