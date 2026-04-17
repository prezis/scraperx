# PyPI Trusted Publisher Setup — scraperx

One-time setup so that pushing a `v*` git tag triggers `.github/workflows/publish.yml` and publishes `scraperx` to PyPI via OIDC (no API tokens, no secrets to rotate).

**Scope:** first publish of `scraperx==1.3.0`. Do Steps 1–2 once in the browser, then Steps 3–4 from the terminal.

---

## Prerequisites

- [ ] **PyPI account.** Register at <https://pypi.org/account/register/> if you don't have one.
- [ ] **Primary email verified.** PyPI disables the pending-publisher form until a primary email is verified. Check at <https://pypi.org/manage/account/>.
- [ ] **2FA enabled on PyPI.** Required for publishing in general; enable at <https://pypi.org/manage/account/two-factor/>.
- [ ] **TestPyPI account.** Separate registration at <https://test.pypi.org/account/register/>. Email verification + 2FA likewise required.
- [ ] **Write access to the `prezis/scraperx` repo** (you, the owner).

> You do **not** need to pre-create GitHub Environments named `pypi` / `test-pypi`. The workflow's `environment:` block will create them on first run. If you later want approval gates, configure them under **Settings → Environments** on GitHub.

---

## Step 1 — Add a "pending" Trusted Publisher on PyPI

The project `scraperx` does not exist on PyPI yet. PyPI supports **pending publishers** (PEP 740 / Warehouse feature): you can authorize the workflow *before* the first upload, and the project is auto-created on first successful publish.

1. Go to <https://pypi.org/manage/account/publishing/> (the "Publishing" tab under your **account** sidebar — not under a project, since the project doesn't exist yet).
2. Scroll to **"Add a new pending publisher"** and select the **GitHub** tab.
3. Fill in exactly:

   | Field              | Value              |
   |--------------------|--------------------|
   | PyPI Project Name  | `scraperx`         |
   | Owner              | `prezis`           |
   | Repository name    | `scraperx`         |
   | Workflow name      | `publish.yml`      |
   | Environment name   | `pypi`             |

   Notes on the fields (verbatim from the form):
   - **PyPI Project Name** — "The project that will be created on PyPI when this publisher is used."
   - **Owner** — "The GitHub organization name or GitHub username that owns the repository." For this repo: `prezis`.
   - **Repository name** — "The name of the GitHub repository that contains the publishing workflow." Just `scraperx`, not the full URL.
   - **Workflow name** — "The filename of the publishing workflow. This file should exist in the `.github/workflows/` directory in the repository configured above." Use `publish.yml`, not a path.
   - **Environment name** — optional on the form, but **our workflow requires it** (`environment: name: pypi`). Must match exactly: `pypi`.

4. Click **Add**.

The pending publisher now appears at the top of the page.

> **Important caveat from PyPI docs:** a pending publisher does **not** reserve the name. If someone else registers `scraperx` on PyPI before you first publish, your pending publisher is invalidated. (Low risk for an obscure name, but do Step 3 soon.)

---

## Step 2 — Add a pending Trusted Publisher on TestPyPI

TestPyPI is a separate index with its own accounts and its own publisher config. Do the same thing there so the `-rc` pre-release tags in Step 3 work.

1. Go to <https://test.pypi.org/manage/account/publishing/>.
2. Under **"Add a new pending publisher"** → **GitHub** tab. Fill in:

   | Field              | Value              |
   |--------------------|--------------------|
   | PyPI Project Name  | `scraperx`         |
   | Owner              | `prezis`           |
   | Repository name    | `scraperx`         |
   | Workflow name      | `publish.yml`      |
   | Environment name   | `test-pypi`        |

   (Only **Environment name** differs from Step 1 — it must match the `environment: name: test-pypi` in the workflow's `publish-test-pypi` job.)

3. Click **Add**.

---

## Step 3 — Dry-run via a `-rc` tag to TestPyPI

Our `publish.yml` routes tags containing `-rc` to TestPyPI (`if: contains(github.ref, '-rc')`) and everything else to PyPI. Validate end-to-end on TestPyPI first.

```bash
cd ~/ai/scraperx

# sanity — no dirty tree, on main, build works locally
git status
python -m build    # optional; the workflow builds too

git tag v1.3.0-rc1
git push origin v1.3.0-rc1
```

Then:

1. Watch the run: <https://github.com/prezis/scraperx/actions/workflows/publish.yml>
2. Jobs sequence: `build` → `publish-test-pypi`. The PyPI job is skipped on `-rc`.
3. On success the project page appears at <https://test.pypi.org/project/scraperx/>.

Verify install (in a throwaway venv so it doesn't pollute your system Python):

```bash
python -m venv /tmp/scraperx-testpypi && source /tmp/scraperx-testpypi/bin/activate
pip install \
  --index-url https://test.pypi.org/simple/ \
  --extra-index-url https://pypi.org/simple/ \
  scraperx==1.3.0rc1
python -c "import scraperx; print(scraperx.__file__)"
scraperx --help
deactivate && rm -rf /tmp/scraperx-testpypi
```

> The `--extra-index-url` to real PyPI is needed so TestPyPI's sparse mirror can resolve `scraperx`'s runtime deps (e.g. `beautifulsoup4`, `faster-whisper`).

> Version normalization: git tag `v1.3.0-rc1` → PEP 440 version `1.3.0rc1` (the hyphen is dropped on the PyPI side). That's what you'll `pip install`.

---

## Step 4 — Publish to real PyPI

Once the TestPyPI dry run worked:

```bash
cd ~/ai/scraperx
git tag v1.3.0
git push origin v1.3.0
```

Watch <https://github.com/prezis/scraperx/actions/workflows/publish.yml>. Jobs sequence: `build` → `publish-pypi` (TestPyPI job skipped).

Within ~1–3 minutes of the job succeeding the project is live at <https://pypi.org/project/scraperx/>. On first upload, the pending publisher is **promoted** to a normal publisher automatically — no further PyPI-side action needed for subsequent `v*` tags.

Smoke-test the real install:

```bash
python -m venv /tmp/scraperx-pypi && source /tmp/scraperx-pypi/bin/activate
pip install scraperx==1.3.0
scraperx --help
deactivate && rm -rf /tmp/scraperx-pypi
```

---

## Troubleshooting

**`invalid-publisher` / `invalid-pending-publisher` from the publish step.**
The OIDC token from GitHub doesn't match the publisher config on PyPI. Check every field for typos:
- `repository_owner` = `prezis` (case-insensitive but spell it right)
- `repository` = `scraperx`
- workflow filename = `publish.yml` (just the filename, no path)
- environment = `pypi` (or `test-pypi` for TestPyPI) — must match `environment: name:` in the job exactly

**`Non-user identities cannot create new projects. … project name incorrectly …`**
The OIDC token was valid, but the `name` in `pyproject.toml` (`scraperx`) doesn't match the "PyPI Project Name" you entered in Step 1. Our `pyproject.toml` already says `name = "scraperx"` — if you see this error, you made a typo on the PyPI form. Delete the pending publisher and re-add it.

**"Pending publisher form is disabled."**
Your PyPI (or TestPyPI) account's primary email isn't verified. Go to <https://pypi.org/manage/account/> and verify it, then reload the publishing page.

**`id-token: write` / "OIDC identity token error".**
Already handled in our workflow — `permissions: id-token: write` is at the top-level and each job runs in an `environment:`. No action needed.

**Reusable workflows.**
PyPI explicitly does not support reusable (`workflow_call`) workflows as the trusted workflow. Our `publish.yml` is a normal `on: push` workflow, so this is fine — just don't refactor it into a reusable workflow later.

**Rate limits.**
PyPI caps publisher registrations at 100 per user or IP per 24h. Not a concern for us (we're registering 2), but mentioned for completeness.

**GitHub Environment approval prompt (future).**
If you later add **Settings → Environments → pypi → Required reviewers** on GitHub, the `publish-pypi` job will pause until a reviewer approves. Nothing to do now; just know the hook exists.

**Something went really wrong, back out.**
Delete the pending publisher at <https://pypi.org/manage/account/publishing/> (and on TestPyPI). The next tag push will fail at the OIDC exchange step and nothing gets published. Re-do Step 1 with corrected values.

---

## What's already wired up (for reference)

`.github/workflows/publish.yml` (already committed) has:

- `on: push: tags: [v*]` — triggers on any tag starting with `v`
- `permissions: id-token: write` (workflow-level, inherited by jobs)
- Split jobs:
  - `publish-pypi` — `if: !contains(github.ref, '-rc')`, `environment: name: pypi, url: https://pypi.org/p/scraperx`
  - `publish-test-pypi` — `if: contains(github.ref, '-rc')`, `environment: name: test-pypi, url: https://test.pypi.org/p/scraperx`, `repository-url: https://test.pypi.org/legacy/`
- Uses `pypa/gh-action-pypi-publish@release/v1` — the PyPA's official action, which handles the OIDC token exchange automatically.

`pyproject.toml` declares `name = "scraperx"` and `version = "1.3.0"`, matching the pending publisher's project name and the tag you're about to push.

**No code changes needed. Just execute Steps 1–4.**
