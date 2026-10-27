# Browser storage, sync and import/export

## Contents
- Where bookmarks live
- Formats and quirks
- Sync behavior
- Clearing and importing (the export route)
- Troubleshooting

## Where bookmarks live

| Browser | Linux | macOS | Windows |
|---|---|---|---|
| Chrome | `~/.config/google-chrome/<Profile>/Bookmarks` (Flatpak: `~/.var/app/com.google.Chrome/config/google-chrome`) | `~/Library/Application Support/Google/Chrome/<Profile>/Bookmarks` | `%LOCALAPPDATA%\Google\Chrome\User Data\<Profile>\Bookmarks` |
| Chromium | `~/.config/chromium/...` (snap: `~/snap/chromium/common/chromium`) | `~/Library/Application Support/Chromium/...` | `%LOCALAPPDATA%\Chromium\User Data\...` |
| Brave | `~/.config/BraveSoftware/Brave-Browser/<Profile>/Bookmarks` | `~/Library/Application Support/BraveSoftware/Brave-Browser/...` | `%LOCALAPPDATA%\BraveSoftware\Brave-Browser\User Data\...` |
| Firefox | `~/.mozilla/firefox/<id>/places.sqlite`; newer builds use `~/.config/mozilla/firefox`; snap and Flatpak have their own dirs | `~/Library/Application Support/Firefox/Profiles/<id>/places.sqlite` | `%APPDATA%\Mozilla\Firefox\Profiles\<id>\places.sqlite` |
| Safari | — | `~/Library/Safari/Bookmarks.plist` (needs Full Disk Access) | — |

Chromium profile dirs are `Default`, `Profile 1`, `Profile 2`, and so on. The display name, signed-in email and Workspace `hosted_domain` are in `<user data dir>/Local State` under `profile.info_cache`. Firefox profiles are listed in `profiles.ini` next to the profile dirs, and the Firefox Account email is in `<profile>/signedInUser.json`.

Edge, Vivaldi and Opera are Chromium-based too. They use the same `Bookmarks` JSON, so `read --file <path>/Bookmarks --format chromium` and `write --browser chromium --file ...` work with them. Discovery just doesn't look for them.

## Formats and quirks

- **Chromium `Bookmarks`**: JSON with the roots `bookmark_bar`, `other` and `synced` (shown as "Mobile bookmarks"). Times are microseconds since 1601. A `checksum` (MD5 over ids, UTF-16 titles and urls) is written by `write`; current Chromium no longer rejects a mismatch, but older builds do. Chromium has no "menu" root, so the menu root is written as an `other/Bookmarks Menu` folder. Since 2024, a profile signed in without full sync can also have `AccountBookmarks` (bookmarks saved to the Google account): read them with `read --account`. `write` leaves that file alone, and the sync guard covers it.
- **Firefox `places.sqlite`**: `moz_bookmarks` joined to `moz_places`. The roots are guids `toolbar_____`, `menu________`, `unfiled_____` (shown as "Other Bookmarks") and `mobile______`. Tags live under `tags________` and are skipped. Never edit the database with SQL; Firefox keeps url hashes, frecency and sync state consistent itself. `write` uses Firefox's own startup import (`browser.places.importBookmarksHTML` + `<profile>/bookmarks.html`, with `replace: true`) instead.
- **Safari `Bookmarks.plist`**: binary plist. The root's children are a `History` proxy, `BookmarksBar`, `BookmarksMenu`, optional extra top-level folders (read as `other`), and `com.apple.ReadingList`, which the tool never touches. Safari stores no dates.
- **`bookmarks.html`**: the Netscape format every browser imports and exports. `PERSONAL_TOOLBAR_FOLDER="true"` marks the toolbar and `UNFILED_BOOKMARKS_FOLDER="true"` marks Firefox's Other Bookmarks.

## Sync behavior

| Sync | Effect of an in-place file edit | Recommendation |
|---|---|---|
| Chrome/Brave sync, or Google account bookmarks | The server copy can restore deleted items or merge in duplicates on the next sync | Export route; or turn sync off, write, and when turning sync back on expect a merge |
| Firefox Sync | The startup import goes through the Places API, so the replace syncs out as a normal change | In place is fine. Warn that other devices get the new structure too |
| iCloud (Safari) | iCloud can overwrite the local plist | Export route |
| Brave Sync chain | Like Chrome | Export route |

## Clearing and importing (the export route)

Importing *adds* to existing bookmarks in every browser. For a clean result, the user deletes the old ones first (after `read` has captured them, which doubles as a backup) and then imports. Give these steps:

- **Chrome / Chromium / Brave**: open `chrome://bookmarks` (`brave://bookmarks`). In "Bookmarks bar", press Ctrl/Cmd+A, then Delete; repeat in "Other bookmarks". Then go to `chrome://settings/importData` (`brave://settings/importData`), choose "Bookmarks HTML File", and pick the file. If the bookmarks bar was empty, the toolbar items land on it; otherwise everything lands in an "Imported" folder you drag out.
- **Firefox**: Ctrl/Cmd+Shift+O opens the Library. Select all in Bookmarks Toolbar, Bookmarks Menu and Other Bookmarks, and delete. Then use Import and Backup → Import Bookmarks from HTML. (In-place `write` does both steps for you on next start.)
- **Safari**: File → Import From → Bookmarks HTML File. Everything goes into an "Imported <date>" folder; drag the contents into Favorites and the sidebar. Delete old bookmarks first from the Edit Bookmarks view (Opt+Cmd+B).

Manual exports for a backup the user can see: Chrome/Brave bookmark manager ⋮ → Export bookmarks; Firefox Library → Import and Backup → Export Bookmarks to HTML; Safari File → Export → Bookmarks.

## Troubleshooting

- **A profile is missing from `discover`**: check for snap or Flatpak installs, and for a custom `--user-data-dir` or `-profile`. Point `read --file` at the store directly.
- **"is running" from `write`**: on Linux and macOS, Chromium checks the profile's `SingletonLock`, and on Linux Firefox checks its `lock` symlink, so other open profiles don't block. A stale lock after a crash: confirm no browser process uses that profile (`pgrep -a <browser>`), then remove the lock.
- **Safari "permission denied"**: System Settings → Privacy & Security → Full Disk Access → enable the terminal app.
- **Firefox didn't change after `write`**: Firefox has to be started once. If `prefs.js` still says `importBookmarksHTML true`, it hasn't started yet.
