# Command Center Session Sync (browser extension)

Keeps your course sessions fresh in the Command Center app automatically, so
it stops asking you to paste cookies every day. As long as you stay logged
into OAKS/VHL/Connect/Blended in this browser, the extension pushes the live
session to the local app whenever it changes.

## What it does and does not do

- Reads cookies for exactly these hosts, and nothing else:
  - your course sites: `lms.cofc.edu`, `vhlcentral.com`,
    `newconnect.mheducation.com`, `library.blended-teaching.com`
  - `docs.google.com` and `cofc-my.sharepoint.com`, because courses link
    readings that live there and the app has to fetch them as you
- It does NOT read your Gmail, your Drive, your personal Google account or
  your wider Microsoft 365 account. An earlier version asked Chrome for
  `*.google.com` and `*.office.com`, which quietly covered all of that; the
  scope is now exact hosts, and every cookie returned is re-checked against
  that list before it is sent.
- Sends them to exactly one place: `http://127.0.0.1:8177` (the app on your
  own machine). Nothing leaves your computer.
- No passwords, no reading files off disk, no third parties. It uses the
  browser's own cookie API - the supported, non-sketchy way.

## Install (one time, ~1 minute)

1. Open `chrome://extensions` (or `edge://extensions`).
2. Turn on **Developer mode** (top-right toggle).
3. Click **Load unpacked** and pick this `browser-extension` folder.
4. Make sure the Command Center app is running (`brain serve`).
5. Open OAKS once so you're logged in - the extension pushes on the next
   cookie change and every 30 minutes after.

Click the extension's icon anytime to see per-site sync status and a
**Sync now** button.

## If it says a site failed

- App not running: start it with `brain serve`.
- Not logged in: open that site and log in; the extension pushes automatically.
