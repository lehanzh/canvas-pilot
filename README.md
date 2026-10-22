<!--
  Maintainer note (invisible on GitHub): the install links assume the default branch is "main";
  if it's "master", change "refs/heads/main.zip" to "refs/heads/master.zip".
-->

# Canvas Pilot

**Let an AI assistant like Claude read your Canvas account, so you can just ask:**

> 💬 *"What should I work on today?"*
> 💬 *"What's due this week, and is anything overdue?"*
> 💬 *"Summarize the new announcements. Did any deadline change?"*
> 💬 *"What do I need on the rest of my classes to keep an A?"*

You don't need any coding experience. Setup takes about **15 minutes**, and you only do it once.

---

## Contents

1. [What is this?](#what-is-this)
2. [What you need](#what-you-need)
3. [Step-by-step setup](#step-by-step-setup)
4. [How to use it](#how-to-use-it)
5. [Shortcuts](#shortcuts)
6. [Is it safe?](#is-it-safe)
7. [Updating, changing, or removing it](#updating-changing-or-removing-it)
8. [Troubleshooting](#troubleshooting)
9. [For the technically curious](#for-the-technically-curious)

---

## What is this?

AI assistants are good at planning and explaining, but they can't see your Canvas. This small
program is a **bridge**: it runs quietly on your own computer and lets your AI app look things up in
Canvas when you ask. That covers assignments, due dates, announcements, grades, course pages, and
more.

**It can:**
- Build a to-do list for today or the week, ranked by what's due soonest and what counts most toward
  your grade.
- List every deadline, including ones your instructor only posted on the course calendar.
- Explain assignment instructions and rubrics, and help you plan the work.
- Summarize announcements and point out changes, such as a moved due date or a new requirement.
- Show your grades and work out the average you need on the remaining work for each letter grade.
- Find course pages and files, and quiz you on course material.

**It will never:**
- Submit assignments, post in discussions, or message anyone.
- Change anything in your courses. The one exception: when you ask, it can tick items off in your
  *personal* Canvas to-do list, or add a reminder note there. Only you can see those.

It works with any school that uses Canvas, on Mac and Windows.

---

## What you need

- ✅ A **Mac or Windows** computer. Linux works too; see the [advanced guide](docs/advanced.md).
- ✅ Your school's **Canvas** login.
- ✅ An AI app that supports this kind of add-on (called "MCP"). These instructions use
  **Claude Desktop**, which is free to download. Claude Code and Cursor work too; see
  [Other AI apps](#other-ai-apps).
- ✅ About 15 minutes and an internet connection.

You'll type (or paste) a few commands into a window called the **Terminal**. Every command is
written out below; just copy, paste, and press **Enter**.

---

## Step-by-step setup

### Step 1: Install Claude Desktop

If you already have Claude Desktop, skip to Step 2.

1. Go to **[claude.ai/download](https://claude.ai/download)** and download the app for your computer.
2. Install it, open it, and sign in.

### Step 2: Open the Terminal

The Terminal is a plain window where you type commands. You only need it during setup.

- **Mac:** press **⌘ Command + Space**, type **Terminal**, and press **Enter**.
- **Windows:** click **Start**, type **PowerShell**, and click **Windows PowerShell**.

A window with a blinking cursor appears. That's it.

> **Tips for the Terminal**
> - To **paste**: **⌘ Command + V** on Mac. On Windows, **Ctrl + V**, or **right-click** inside
>   the window.
> - Press **Enter** after each command to run it.
> - Some commands take a few seconds. Wait until the blinking cursor comes back before typing the
>   next one.

### Step 3: Install "uv" (a free helper that installs the assistant)

Copy the command for your computer, paste it into the Terminal, and press **Enter**.

**Mac:**
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

**Windows (PowerShell):**
```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

You'll see a few lines of text ending with something like *"everything's installed!"*

👉 **Now close the Terminal window completely and open a new one** (repeat Step 2). This lets the
new window find uv.

### Step 4: Install Canvas Pilot

In the new Terminal window, paste this and press **Enter**:

```bash
uv tool install https://github.com/lehanzh/canvas-pilot/archive/refs/heads/main.zip
```

After a few seconds it should say **"Installed 1 executable: canvas-pilot"**.

> **Did you see a warning that says *"is not on your PATH"*?** Run this, then close and reopen the
> Terminal:
> ```bash
> uv tool update-shell
> ```

### Step 5: Get your Canvas access token

An **access token** is like a special password that lets the assistant read your Canvas account.
You create it in Canvas:

1. Open Canvas in your web browser and sign in.
2. Click **Account** (your profile picture, top-left), then **Settings**.
3. Scroll down to **Approved Integrations** and click **+ New Access Token**.
4. **Purpose:** type something like `AI assistant`.
   **Expires:** pick a date, such as the last day of the semester. You can always make a new token
   later.
5. Click **Generate Token**.
6. **Copy the long token right away.** Canvas only shows it once. Don't share it with anyone.

> **No "+ New Access Token" button?** Some schools turn this off for students. Ask your school's
> IT help desk whether students can create Canvas access tokens.

### Step 6: Run the setup

In the Terminal, paste this and press **Enter**:

```bash
canvas-pilot setup
```

It asks three questions:

1. **"Canvas address"**: copy the address from your browser while you're on Canvas. Only the first
   part counts, for example `https://myschool.instructure.com`. Paste it and press **Enter**.
2. **"Paste your Canvas access token"**: paste the token from Step 5 and press **Enter**.
   ⚠️ **Nothing appears on screen while you paste.** That's normal, and it keeps the token hidden.
   Paste once and press Enter.
3. **"Timezone"**: just press **Enter** to use your computer's timezone.

If everything worked, you'll see your name and a list of your courses:

```
OK: authenticated to https://myschool.instructure.com as Your Name
...
4 active courses:
- Algorithms and Data ...
```

Your token is now stored in your computer's secure password storage: **Keychain** on Mac,
**Credential Manager** on Windows. It isn't saved in any file.

### Step 7: Connect it to Claude Desktop

Paste this and press **Enter**:

```bash
canvas-pilot connect claude-desktop
```

It says **"Connected to Claude Desktop."** Your existing Claude settings are backed up
automatically first.

Now **fully quit Claude Desktop and open it again.** Closing the window isn't enough:
- **Mac:** click Claude in the Dock, then press **⌘ Command + Q**.
- **Windows:** right-click the Claude icon near the clock (bottom-right) and choose **Quit**.

### Step 8: Try it! 🎉

Open Claude Desktop and ask:

> 💬 *What courses am I in?*

The first time, Claude asks for permission to use a Canvas tool. Choose **Allow always** (or
**Always allow**) so it won't ask again for that tool.

You're done, and you can close the Terminal. From now on, just talk to Claude.

### Other AI apps

Use the same Steps 2–6, then connect the app you use instead of Step 7:

| App | Command to run in the Terminal | Then |
|---|---|---|
| **Claude Code** | `canvas-pilot connect claude-code` | Start a new Claude Code session |
| **Cursor** | `canvas-pilot connect cursor` | Quit and reopen Cursor |
| **Others** (Codex, VS Code, Windsurf, Gemini CLI…) | `canvas-pilot client-config` | Follow the [advanced guide](docs/advanced.md#connecting-any-ai-app-by-hand) |

---

## How to use it

Just ask in your own words. You don't need special phrasing. Some ideas:

**📅 Planning**
- "What should I work on today?"
- "Plan my week so nothing piles up."

**⏰ Deadlines**
- "What's due in the next two weeks?"
- "Is anything overdue?"
- "When's my next exam?"

**📝 Assignments**
- "Explain the instructions for Assignment 4 and show me the rubric."
- "Break the HW 5 assignment into steps with mini-deadlines."

**📢 Announcements**
- "Any new announcements this week?"
- "Did any of my professors change a due date?"

**📊 Grades**
- "What are my grades right now?"
- "What do I need on the rest of Algorithms to get an A?"
- "If I get 75% on the midterms, what will my final grade be?"

**📚 Studying**
- "What's in this week's module for CS3000?"
- "Read the 'Code Readability' page and quiz me on it."
- "Find the lecture slides about greedy algorithms."
- "What's the late policy in the syllabus?"

**✅ Keeping track**
- "I turned in Lab 5 on Gradescope. Mark it done."
- "Remind me on Friday to start the lab report."
- "Do I have any unread Canvas messages?"

**Good to know**
- **Naming courses:** say it however you like, e.g. "CS3000", "3000", or "algorithms".
- **Work you hand in outside Canvas** (Gradescope, paper, in class): Canvas can't see those
  submissions, so they may still show up as to-dos. Tell Claude *"I submitted it, mark it done"* to
  clear them.
- **Grade predictions are estimates.** They use your course's real grade weights, but can't know
  about curves, extra credit, or dropped-lowest-score rules.

---

## Shortcuts

For things you ask often, there are built-in shortcuts that give a consistent answer every time:

| Shortcut | What you get |
|---|---|
| **overview** | A snapshot of all your courses: grade, next deadline, and new announcements for each, plus your top 3 priorities and your next exam. Add a course name for a deep dive into one course. |
| **today** | Up to 5 things to do today, in order, plus today's classes and office hours |
| **week** | A day-by-day plan for the next 7 days, with the heaviest day pointed out |
| **news** | Only announcements, sorted into **Changes**, **Action needed**, and **FYI** |
| **due** | A table of everything due in the next 14 days |
| **plan_assignment** | A step-by-step plan for one assignment, from its instructions and rubric |

**Where to find them:**
- **Claude Desktop:** click the **+** button in the message box, choose **canvas**, and pick a
  shortcut.
- **Claude Code:** type `/canvas` and choose from the list (e.g. `/mcp__canvas__today`).
- **Other apps** may not show shortcuts. Just ask in words, e.g. *"what's due in the next 14 days"*.

---

## Is it safe?

**Where is my token kept?**
Only in your computer's secure password storage: Keychain on Mac, Credential Manager on Windows. It
isn't saved in any file, and it's only ever sent to your school's Canvas.

**Can it submit homework or post anything?**
No. It can't submit, post, or message anyone. It can only tick items off in your personal Canvas
to-do list, or add a reminder note there, and only when you ask.

**What does the AI see?**
Only what it looks up to answer your question, such as the assignment you asked about. That
information goes to the AI app you use (e.g. Claude), and that app's own privacy policy applies.

**Will my instructors know?**
The assistant reads Canvas the same way you do when you browse it. Canvas may log that activity like
any other visit. It doesn't post or send anything on your behalf.

**Is using it allowed for coursework?**
Reading your own course information is just like reading Canvas yourself. How you use AI for the
actual work is up to your course's academic integrity rules, so check your syllabus.

**Does it cost anything?**
Canvas Pilot is free. Your AI app may have its own free and paid plans.

**What if my token leaks?**
Delete it in Canvas (**Account → Settings → Approved Integrations**, then the trash icon next to
it). It stops working immediately. Then make a new one and run `canvas-pilot set-token`.

---

## Updating, changing, or removing it

All of these are typed in the Terminal.

**Get the latest version.** Run the install command again with `--reinstall`:
```bash
uv tool install --reinstall https://github.com/lehanzh/canvas-pilot/archive/refs/heads/main.zip
```
Then fully quit and reopen your AI app.

**Your token expired, or you made a new one:**
```bash
canvas-pilot set-token
```

**Change your Canvas address or timezone.** Run the setup again; it's safe to repeat:
```bash
canvas-pilot setup
```
When it asks *"A token is already in your keychain. Replace it?"*, just press **Enter** to keep
your current token.

**Check that everything still works:**
```bash
canvas-pilot check
```

**Hide courses you don't care about** (orientation, advising, clubs):
1. Ask Claude *"List my courses with their IDs"* and note the ID numbers you want to hide.
2. Open the settings file:
   - Mac: `open -e ~/.config/canvas-pilot/config.env`
   - Windows: `notepad $env:APPDATA\canvas-pilot\config.env`
3. Add a line like this (your ID numbers, separated by commas), then save:
   `CANVAS_EXCLUDE_COURSE_IDS=12345,67890`
4. Quit and reopen your AI app.

**Remove it completely:**
```bash
canvas-pilot delete-token
```
```bash
uv tool uninstall canvas-pilot
```
Then:
- Delete the access token in Canvas (**Account → Settings → Approved Integrations**).
- Optionally, remove "canvas" from Claude Desktop: **Settings → Developer → Edit Config**, delete the
  `"canvas"` entry, save, and restart Claude.

---

## Troubleshooting

| What you see | What to do |
|---|---|
| `command not found: uv` (Mac) or `uv is not recognized` (Windows) | Close the Terminal and open a new one. If it still happens, repeat Step 3. |
| `command not found: canvas-pilot` or `is not recognized` | Run `uv tool update-shell`, then close and reopen the Terminal. |
| Nothing appears when you paste the token | That's normal; it's hidden on purpose. Paste once and press **Enter**. |
| Pasting does nothing on Windows | **Right-click** inside the PowerShell window to paste. |
| *"Canvas rejected the access token (401)"* | The token was copied incompletely or has expired. Make a new one (Step 5), then run `canvas-pilot set-token`. |
| *"Canvas returned a web page instead of API data"* | The Canvas address is wrong. Run `canvas-pilot setup` again and paste the address shown in your browser **while you're inside Canvas**, e.g. `https://myschool.instructure.com`. |
| Claude doesn't seem to know about Canvas | Make sure you **fully quit** and reopened Claude (Step 7). Run `canvas-pilot check` to confirm the setup works, then run `canvas-pilot connect claude-desktop` again. In Claude Desktop, **Settings → Developer** shows whether "canvas" is running. |
| One question fails with *"denied access (403)"* | Your instructor or school has hidden that part of Canvas from students. Everything else still works. |
| Times are off by an hour or more | Run `canvas-pilot setup` again and type your timezone, e.g. `America/New_York` or `Europe/London`. |
| Windows: *"running scripts is disabled"* | Make sure you copied the **whole** uv command from Step 3, including `-ExecutionPolicy ByPass`. |

**Still stuck?** Run `canvas-pilot check` and copy what it prints. Open an issue on this project's
GitHub page and include that output. Remove your name and course list first if you prefer.

---

## For the technically curious

The [advanced guide](docs/advanced.md) covers:
- how it works
- every command and setting
- connecting other apps by hand
- the math behind the to-do ranking and grade predictions
- security details
- how to develop and test it

In short, this is a local [Model Context Protocol](https://modelcontextprotocol.io) server that uses
the official [Canvas REST API](https://canvas.instructure.com/doc/api/).
