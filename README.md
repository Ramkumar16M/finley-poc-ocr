# finley-poc-ocr

Local Finley intake + content screening over Gmail, with Tesseract / pdfplumber / openpyxl in place
of Azure Document Intelligence. Everything runs on the laptop; no outbound calls other than Gmail.

## What it does

- Polls your Gmail inbox for messages matching a search filter you set.
- For each new message: downloads the raw `.eml` and every attachment into `data/library/`.
- Extracts text and tables (pdfplumber for PDFs, openpyxl for Excel, Tesseract for scans/images).
- Checks for hidden text aimed at an AI (tiny, off-page, background-coloured via pixel sampling).
- Writes `screened.md` (clean) or `held.json` (blocked) beside the originals.
- Labels the processed message with `finley-screened` so it's skipped next pass.


## Gmail setup

### 1. Create the OAuth client

1. Open <https://console.cloud.google.com/apis/credentials>.
2. Create a project (or use an existing one).
3. **Enable APIs & Services → Library → Gmail API → Enable.**
4. **Credentials → Create Credentials → OAuth client ID.**
5. Application type: **Desktop app**. (Not "Web application" — the local server flow
   requires a Desktop client, otherwise you'll need to pin a redirect URI.)
6. Give it any name and click Create.
7. Download the JSON file and save it as `credentials.json` at the project root
   (same directory as `pyproject.toml`).

### 2. Configure the OAuth consent screen

1. **APIs & Services → OAuth consent screen.**
2. User type: **External** (unless the account you'll use is in a Google Workspace that owns
   this Cloud project — then you can keep "Internal").
3. Fill in the required fields (app name, support email).
4. Scroll to **Test users** and add the Gmail address you'll authenticate with.
5. Save.

### 3. Local configuration

```bash
cp .env.example .env
```

Defaults are fine for a first run. The optional `FINLEY_GMAIL_QUERY` narrows which messages get
screened:

```bash
# Last 24 hours, subject contains "Draw":
FINLEY_GMAIL_QUERY=newer_than:1d subject:Draw

# Only from a specific sender, with attachments:
FINLEY_GMAIL_QUERY=from:builder@example.com has:attachment

# Unset -> screens every inbox message (noisy; useful only once you've confirmed the flow).
FINLEY_GMAIL_QUERY=
```

The folder should look like this before you run:

```
finley-poc-ocr/
├── credentials.json     # from step 1
├── .env                 # from step 3
├── pyproject.toml
└── src/
```

`credentials.json`, `token.json`, `.env`, and `data/` are already in `.gitignore`.

## Install

```bash
cd finley-poc-ocr
uv sync
```

## Run

### One-shot (recommended for testing)

```bash
FINLEY_POLL_ONCE=1 uv run python src/main.py
```

On the **first run** a browser window opens for OAuth consent:

1. Pick the Google account you added under Test users.
2. You'll see "Google hasn't verified this app" → click **Advanced → Continue**.
3. Grant the requested scope (`gmail.modify`).
4. The browser tab shows "The authentication flow has completed. You may close this window."
5. `token.json` is written next to `credentials.json`. Subsequent runs use it silently and
   auto-refresh when it expires.

The `finley-screened` label is created automatically on the first pass — no need to make it
in Gmail by hand.

### Continuous polling

```bash
uv run python src/main.py
```

Polls every `FINLEY_POLL_SECONDS` seconds (default 30). `Ctrl+C` to stop; it finishes the
current pass first.

### Local-file mode (no Gmail)

For iterating on extraction / screening without touching Gmail:

```bash
uv run python src/local.py path/to/folder --out data/library/my-run
```

Takes every supported file under the folder and produces the same `screened.md` / `held.json`
output.

## Output layout

Each processed message gets its own folder:

```
data/library/
└── 2026-11-10-<gmail-message-id>/
    ├── message.eml                      # the raw email
    ├── <attachment-1>                   # every attachment, original filename preserved
    ├── <attachment-2>
    ├── screened.md                      # clean — content Claude would read
    └── report.json                      # what was checked (characters, pages, hidden findings)
    # OR, if the hidden-text check flagged something:
    └── held.json                        # reason + per-file report; no screened.md written
```

## Troubleshooting

**`Error 403: org_internal`** — Your OAuth consent screen is set to User type "Internal" but
you're signing in with an account outside that Workspace org. Switch the consent screen to
"External" and add yourself as a Test user.

**`redirect_uri_mismatch`** — You created a Web application OAuth client instead of a Desktop
app. Either recreate as Desktop, or add `http://localhost:8765/` as an authorized redirect
URI and pin that port in `src/gmail.py`.

**`Gmail label 'finley-screened' not found`** — Old builds required manual label creation.
The current version auto-creates it; pull latest and re-run.

**Message produced `held.json` instead of `screened.md`** — Expected when an attachment
contains hidden text aimed at an AI (invisible, tiny, or off-page). Open `held.json` to see
which file and what was found. The test set under `injection/` has a `0-clean-control`
variant that will pass cleanly.

**Nothing happens on a pass** — The `FINLEY_GMAIL_QUERY` filter in `.env` is matching zero
messages. Either send yourself a message that matches, or temporarily broaden the filter.

**Re-process a message** — Remove the `finley-screened` label from it in Gmail; the next
pass will pick it up again.
