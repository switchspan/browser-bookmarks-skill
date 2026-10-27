# browser-bookmarks

An agent skill for cleaning up, deduplicating, organizing, link-checking and merging browser bookmarks in **Firefox, Chrome, Chromium, Brave and Safari**, on Linux, macOS and Windows. It keeps **work and personal browser profiles separate**.

The skill follows the [Agent Skills](https://agentskills.io) format (`SKILL.md` plus scripts), so it works in Claude Code, Codex, Cursor, Grok, Pi, and any other agent that loads skills.

## Install

```bash
npx skills add switchspan/browser-bookmarks-skill
```

Useful flags:

```bash
npx skills add switchspan/browser-bookmarks-skill -g                    # user-level instead of project-level
npx skills add switchspan/browser-bookmarks-skill -g -a claude-code -a codex
npx skills add switchspan/browser-bookmarks-skill --list                # show what would be installed
```

To install by hand, copy `skills/browser-bookmarks/` into your agent's skills folder (for example `~/.claude/skills/` or `~/.codex/skills/`).

**Requirements:** Python 3.9 or newer, with only the standard library. On macOS, reading Safari bookmarks needs Full Disk Access for the terminal or app your agent runs in.

## What it does

Ask your agent things like:

- "My Chrome bookmarks are a mess. Dedupe them and sort them into folders."
- "Find the dead links in my Firefox bookmarks."
- "Merge my Brave and Safari bookmarks into one clean set."
- "Which of my personal bookmarks are actually work stuff?"

The agent then follows this workflow:

1. **Discover** every browser profile on the machine and label it `work`, `personal` or `unknown`. The label comes from the signed-in account (a Google Workspace or company email domain means work; gmail, outlook, icloud and similar mean personal) and from the profile name. You confirm the labels once, and they're saved to `~/.config/browser-bookmarks/contexts.json`.
2. **Analyze** each profile: duplicates, empty and very deep folders, untitled bookmarks, top domains, and bookmarks that look like they belong to the other context (for example, the company Jira in a personal profile).
3. **Clean up**, using only the operations you ask for:
   - **Dedupe:** compares normalized URLs, so http/https, `www.`, trailing slashes and tracking parameters don't hide duplicates. It keeps the copy you filed most deliberately and strips `utm_*`, `fbclid` and similar parameters.
   - **Dead links (opt-in, network):** only clear failures (404, 410, DNS failure) are moved to a "Dead links (review)" folder, and nothing is deleted. Bot-blocks, login walls and work intranet hosts aren't treated as dead.
   - **Organize:** the agent proposes a short folder taxonomy from your domains and titles. You approve it, and a rules file applies it. The toolbar is left as you built it.
   - **Merge** across browsers or profiles.
4. **Write the result** in one of two ways:
   - **Export** (the default): a standard `bookmarks.html` you import yourself. The live profile is never touched. This is the right choice whenever browser sync is on.
   - **In place**, with a timestamped backup first and the browser closed:
     - Chrome, Chromium and Brave: the `Bookmarks` JSON is rewritten with a valid checksum, keeping ids and guids.
     - Firefox: its own import-on-startup mechanism replaces the bookmarks. There's no raw SQL against `places.sqlite`.
     - Safari: `Bookmarks.plist` is rewritten, leaving the Reading List untouched.

### Work vs personal

Work profiles are often managed by an employer and synced to an employer account. To avoid leaking personal browsing to your employer, or company links into a personal account:

- Merging or writing bookmarks across contexts is refused unless you explicitly ask for it.
- Cross-context bookmarks are reported, never moved silently.
- Cleanups run separately for each context.

### Safety

- Reading never needs the browser to be closed; the files are copied first.
- In-place writes refuse to run while that profile is open. They also refuse Chrome or Brave profiles signed in to sync, because the sync server would undo the change or duplicate bookmarks.
- Every write backs up the original files to `~/.config/browser-bookmarks/backups/` first.

## The CLI

The skill's engine is a standalone script that you can also run yourself:

```bash
BM=skills/browser-bookmarks/scripts/bookmarks.py
python3 $BM discover
python3 $BM read --browser firefox -o ff.json
python3 $BM analyze ff.json
python3 $BM dedupe ff.json -o ff.clean.json --prune-empty --log removed.json
python3 $BM organize ff.clean.json --rules rules.json -o ff.organized.json
python3 $BM render ff.organized.json -o bookmarks-cleaned.html
python3 $BM write ff.organized.json --browser firefox --dry-run
```

Run `python3 $BM <command> -h` for each command's options. The rules file format is documented in `SKILL.md`. Storage locations, formats and sync behavior for each browser are in `skills/browser-bookmarks/references/browsers.md`.

| Browser | Read | Export | In-place write |
|---|---|---|---|
| Chrome / Chromium / Brave | `Bookmarks` JSON (plus `AccountBookmarks`) | yes | yes, with checksum |
| Firefox | `places.sqlite` (read from a copy) | yes | yes, via Firefox's startup import |
| Safari (macOS) | `Bookmarks.plist` | yes | yes |
| Any browser | exported `bookmarks.html` | yes | — |

Edge, Vivaldi and Opera use the Chromium format, and they work through `--file`.

## Tests

`skills/browser-bookmarks/evals/` has three test cases in the [`claude plugin eval`](https://docs.claude.com/en/docs/claude-code) layout:

- a personal Chrome cleanup exported to a file
- a "merge Chrome and Brave" request where Brave is a work profile
- an in-place Firefox reorganize

Each case ships a fake home directory as fixtures. `evals/make_fixtures.py` rebuilds them. The writers were checked against real browsers:

- Chromium accepts the rewritten file, and its own checksum matches the script's.
- Firefox applies the staged import on startup and resets the pref.

## License

MIT
