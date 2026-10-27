---
tags: [skill-creator]
max_turns: 40
timeout_seconds: 900
allowed_tools: [Read, Glob, Grep, Skill, Write, Edit, Bash]
---

my chrome bookmarks are a total mess, tons of dupes and one giant Other Bookmarks pile. can you clean them up and sort them into sensible folders? dont touch chrome itself, just give me a file I can import. save it as bookmarks-cleaned.html

(Test setup: this machine's home directory is the `home/` folder in the working directory. Before any command, run `export HOME="$PWD/home" XDG_CONFIG_HOME="$PWD/home/.config"` and never touch the real home directory. Treat all browsers as fully closed. You cannot ask me questions mid-task: if you would normally ask, say what you would ask and what you did instead.)

Input files: home/
