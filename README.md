# git-star-list-sort

[![CI](https://github.com/MA22DE/git-star-list-sort/actions/workflows/ci.yml/badge.svg)](https://github.com/MA22DE/git-star-list-sort/actions/workflows/ci.yml)

Organize your GitHub stars into your existing [GitHub Lists](https://github.com/stars)
using [TypeSafe Jev](https://docs.typesafe.ai/).

A fork of [`yutkat/github-star-organizer-jev`](https://github.com/yutkat/github-star-organizer-jev)
turned into a globally installable CLI, with one significant addition: it can
generate descriptions for your Lists, because a bare List title gives the model
almost nothing to sort by.

## Install

```bash
uv tool install git+https://github.com/MA22DE/git-star-list-sort
```

Working on the tool itself? Install from a clone with `--editable` so source edits
take effect without reinstalling:

```bash
git clone https://github.com/MA22DE/git-star-list-sort.git
cd git-star-list-sort
uv tool install --editable .
```

This installs one command onto your `PATH` (via `~/.local/bin`) that does the
whole job — refresh List descriptions, classify stars, and optionally apply the
assignments to GitHub:

| Command | Purpose |
| --- | --- |
| `git-star-list-sort` | Everything: refresh descriptions, classify, and `--apply` to write |
| `git-star-list-sort-apply` | Deprecated shim: apply a saved report |
| `git-star-list-sort-describe` | Deprecated shim: regenerate descriptions (`--force`, `--env-file`) |

The two shims still work for scripting and for re-driving a saved report, but the
one command covers normal use.

## How it uses Jev

One Jev request per repository, answered as a single `choice` question. This is
the entire classification step — there is no prompt to tune and no agent loop:

```jsonc
POST https://api.typesafe.ai/v1/systemone
{
  "model": "jev-latest",
  "state": {                              // the repository, as evidence
    "name_with_owner": "typesafe-ai/jev-examples",
    "description": "…", "language": "TypeScript",
    "topics": ["…"], "archived": false, "fork": false, "private": false,
    "starred_at": "…",
    "readme_excerpt": "… first 2000 chars, badges/images/comments stripped …"
  },
  "questions": {
    "list": {                            // one question, one choice
      "type": "choice",
      "instructions": "Choose the existing GitHub List that best matches …",
      "criteria": {                       // the options ARE your Lists
        "UL_kwDOExampleA": "Automation (AI Powered): AI-powered automation tools …",
        "UL_kwDOExampleB": "SQLite: SQLite tools and extensions that add …",
        "no_matching_category": "None of the existing Lists fits the repository's purpose …"
      }
    }
  }
}
```

The answer is a choice with a calibrated distribution over **every** option, and
the tool records it verbatim — including the options it did not pick:

```jsonc
{
  "answers": {"list": {
    "type": "choice", "choice": "UL_kwDOExampleA", "confidence": 0.79,
    "probabilities": {"UL_kwDOExampleA": 0.81, "no_matching_category": 0.0, "…": 0.18}
  }},
  "model": "jev-1.13.0",
  "usage": {"input_tokens": 3474, "output_tokens": 487}
}
```

Design points worth knowing:

- **The classifier is not an LLM.** Jev returns a decision in well under a second
  (measured 0.4–0.8 s), so a 100-star batch is a ~90-second job instead of a
  prompt-engineering project. The only LLM in the tool is the optional List
  description generator, and it runs once per *List*, never once per star.
- **The criteria are the options**, keyed by List id, each rendered as
  `"<List name>: <description>"`. That is why the descriptions matter so much
  (next section) — they are literally the model's answer space.
- **`no_matching_category` is a first-class option** with its own criterion, and
  the instructions forbid forcing a match to a broad List. Those results are
  recorded and skipped by `apply`.
- **254 Lists is a hard ceiling.** A Jev choice question accepts at most 255
  options and the 255th is `no_matching_category`, so more than 254 Lists is
  refused up front with a clear error instead of a silently truncated question.
- **Answers are validated before they are used.** The response must be a `choice`
  whose value is one of the criteria, with a numeric confidence in `[0, 1]` and a
  probability for every option; anything else raises instead of being
  half-recorded. 401s name the likely cause (`JEV_API_KEY`), and
  `429`/`502`/`503`/`504`/`529` are retried with exponential backoff.
- **Repository text is untrusted input.** The instructions tell the model that
  metadata, README text and List descriptions are evidence only and never
  instructions, so a README cannot steer the classifier.
- **Classification never writes to GitHub.** It produces a report; assigning is
  the separate, gated `--apply` step.
- **Any Jev-compatible endpoint works**: `--endpoint` or `JEV_ENDPOINT`, with
  `--model` defaulting to `jev-latest`. The report keeps both `requested_model`
  and the model that actually answered.

## Why List descriptions matter

Each List is presented to the model as the text `"<List name>: <description>"`.
With no description, a List becomes `"SQLite: "` — and the model is explicitly
told not to force a match, so it tends to answer `No matching category` instead.
Descriptions give it the *boundaries* between neighbouring Lists, which is what
actually disambiguates cases like `SQLite` versus `Database tools`.

Three sources are layered, highest priority first:

1. A committed `lists.json` entry (what `git-star-list-sort-describe` writes)
2. The List's description on GitHub
3. The bare List name

Because a committed entry wins, hand-editing `lists.json` is a supported way to
correct a generated description.

## Credentials

**Nothing to export.** Put credentials in a `.env` and the tool finds them:

```bash
~/.config/git-star-list-sort/.env     # works from any directory
# or a .env in your project (searched upward from the working directory)
```

```ini
STAR_LISTS_TOKEN=ghp_...        # classic PAT with the `user` scope, for apply
JEV_API_KEY=...                  # or TYPESAFE_API_KEY
OPENROUTER_API_KEY=sk-or-v1-...  # only for -describe
```

A project-local `.env` wins over the user config, and a real environment variable
wins over both, so `STAR_LISTS_TOKEN=... git-star-list-sort` still overrides
without editing any file. The `.env` is read by the tool itself because an
installed console script cannot source your shell profile. Set
`GIT_STAR_LIST_SORT_NO_DOTENV=1` to disable file loading entirely (the test suite
does this to stay hermetic).

Resolution order:

**Jev** — `JEV_API_KEY`, then `TYPESAFE_API_KEY`.

**GitHub** — `STAR_LISTS_TOKEN`, then `GH_TOKEN`, then `GITHUB_TOKEN`, then
`gh auth token --hostname github.com` as a fallback. All three commands share
this chain, so a report you classified is one you can apply.

The tool prints which *source* it used (never the value). If `gh` is logged in to
more than one account it refuses to guess and asks you to set `STAR_LISTS_TOKEN`
explicitly.

A `gh` OAuth token is **enough to classify, but not to apply.** This was tested,
not assumed:

| Operation | `gh` OAuth token (scopes `gist`, `read:org`, `repo`, `workflow`) |
| --- | --- |
| Read stars and Lists | works |
| Add a repository to a List | **fails:** `updateUserListsForItem` requires the `user` scope |

So classification runs on `gh auth token` out of the box, and the upstream
README's advice holds for `apply`: create a classic PAT with the `user` scope
(<https://github.com/settings/tokens>), export it as `STAR_LISTS_TOKEN`, and
`apply` will prefer it over the `gh` fallback.

## Usage

Generate descriptions once, whenever your List taxonomy changes:

```bash
# the usual command: report which Lists lack a description and generate only those
git-star-list-sort-describe --check --describe-lists-output lists.json

# re-generate every description instead of keeping existing ones
git-star-list-sort-describe --describe-lists-output lists.json --force
```

**Run `--check` whenever you add or rename a List.** New Lists have no committed
description, so without it they fall back to a bare `"Name: "` criterion and
classify poorly. `--check` needs no API key to report, so it is safe to run just
to see drift:

```
needs description: Small Language Models
needs description: Jev Model Alternatives
28 Lists, 25 described, 3 missing
```

It generates only what is missing, keeps your hand edits, and reports a List that
no longer exists on GitHub rather than silently dropping it. Without `--check`,
`-describe` also skips every List that already has a description.

The generator does not guess from the List title alone: it reads each List's
actual members, with their descriptions, topics, and languages, and also names the
sibling Lists most likely to be confused with it. This matters — `Typesafe-Jev`
was once described as generic "type-safe programming" from its title, and only came
out right once the real member (`reachjalil/jev-tree`) was supplied as evidence.

The default endpoint reads `OPENROUTER_API_KEY` from `--env-file` (default `.env`)
or the environment, and `OPENROUTER_MODEL` for the model, defaulting to
`deepseek/deepseek-v4.1-flash`. That model reasons before answering, so the request
budget (2000 tokens, retried once at 4000) covers reasoning plus the visible
description; a budget sized for the two sentences alone is spent entirely on
reasoning and returns no text. A full pass over ~30 Lists takes several minutes:
each description is one reasoning-model call, and a slow List can take a minute
on its own.

## The one command

`git-star-list-sort` does everything. Descriptions are refreshed automatically
before every sort, new Lists included, so the taxonomy never goes stale. Without
`--apply` nothing is ever written to GitHub; with `--apply` the tool previews the
net-new memberships and asks a `y/N` question (default No). Scripts pass `--yes`.

One nuance: generating descriptions writes them to `lists.json` (the local
criteria file) — that happens whenever Lists are missing descriptions, in every
mode. GitHub itself is only ever touched by `--apply`.

```bash
# sort the newest 100 and write them to GitHub
git-star-list-sort --limit 100 --include-unlisted --apply

# report only (never changes GitHub)
git-star-list-sort --limit 100 --include-unlisted

# preview what --apply would change, without writing
git-star-list-sort --limit 100 --include-unlisted --apply --dry-run

# rebuild List descriptions after adding or renaming Lists
git-star-list-sort --refresh-descriptions --limit 1
```

Running the command with no arguments prints this guide instead of starting a
long run. `-h`, `-help` and `--help` all show the option reference.

**By default only stars that are already in at least one List are classified**,
so a mistake can only change *which* List a repository is in — it can never file
a previously unsorted star:

```bash
git-star-list-sort --output output/classifications.json

# include the stars that are not in any List yet
git-star-list-sort --include-unlisted --output output/classifications.json

# try a small batch first
git-star-list-sort --limit 10 --include-unlisted --output output/classifications.json
```

### Bounding a batch

`--limit N` takes the **newest N stars, most recently starred first**, so a huge
star count cannot turn into a huge batch:

```bash
git-star-list-sort --limit 100 --include-unlisted --output output/classifications.json
```

That is the usual way to sort a bounded slice of recent stars — 100 repositories
costs 100 Jev requests (roughly 1.5 minutes at the measured ~0.4s each), not 1203.
Incremental use is simply raising the number later: results are per-run and
assignment is additive, so `--limit 100` then `--limit 300` continues where you
left off rather than starting over.

Two things to know about combining flags:

- `--limit` bounds what is *fetched*; the default report-only mode still skips
  unlisted stars. `--limit 100` alone on a fresh account classifies very little, so
  use `--include-unlisted` when you actually want the batch sorted.
- `--limit 0` means all stars. The notice tells you how many were classified, and
  says `Nothing to classify` outright when everything fetched was skipped.

When repositories are skipped, the tool says so on stderr:

```
Report-only: 1202 unlisted stars skipped because they are not in any GitHub List;
pass --include-unlisted to classify them too
```

The report records `unlisted_skipped` so a scripted run can detect it too.

Apply. With `--apply`, the tool previews the net-new memberships per List, then
asks `y/N` (default No) unless `--yes` is given. A non-interactive run without
`--yes` refuses to write, so a script that forgot the flag cannot mutate anything.
`--dry-run` shows the preview and stops.

```bash
git-star-list-sort --limit 100 --include-unlisted --apply --dry-run
git-star-list-sort --limit 100 --include-unlisted --apply
git-star-list-sort --limit 100 --include-unlisted --apply --yes   # scripts
```

The report is always written (default `classifications.json` when `--output` is
absent), so a half-finished apply can be re-driven with the deprecated
`git-star-list-sort-apply --report` shim.

Applying is **additive**: it reads current memberships, adds the assigned List,
and never removes a repository from a List. Lists are never created or deleted.
`No matching category` results are skipped. A rerun is a no-op.

## Validation status

What has actually been exercised against live services:

| Path | Status |
| --- | --- |
| Classify (`git-star-list-sort`) | **Live-tested**: ~1200 stars / 28 Lists, results with list, confidence, model, usage |
| Apply — read and validate (`--dry-run`) | **Live-tested**; confirmed side-effect free |
| Apply — write | **Live-tested**: 6 repositories assigned, rerun reported `already_assigned: 6` |
| `--describe-lists` generation | **Live-tested**: 28 of 28 Lists described |
| Committed descriptions reach Jev | **Live-tested**: empty criteria went 28 → 0 |

The committed `lists.json` was generated by `-describe`, then reviewed. Descriptions
are worth reading before trusting them: `Typesafe-Jev` was described as generic
type-safe programming rather than this repository's own Jev/TypeSafe list, because
the title alone does not say which is meant. Edit `lists.json` by hand and your
text wins.

Confidence is the model's certainty given the criteria, not measured accuracy. It
does move with description quality — the same repository scored 0.66 against empty
criteria and 0.87 once descriptions existed — but treat low values as "worth a
look", not as an error.

## Suggested order

```bash
# 1. generate the List descriptions, then read and edit them in lists.json
#    (needs OPENROUTER_API_KEY; the sort itself does not)
git-star-list-sort --refresh-descriptions --limit 1

# 2. classify the newest 100 stars - nothing is written to GitHub
#    (needs JEV_API_KEY; gh auth token is enough)
git-star-list-sort --limit 100 --include-unlisted

# 3. see exactly which memberships --apply would add
git-star-list-sort --limit 100 --include-unlisted --apply --dry-run

# 4. apply it; asks first, needs a classic PAT with the `user` scope
git-star-list-sort --limit 100 --include-unlisted --apply
```

`apply` is additive: it can add a repository to a List but has no removal path, so
undoing an assignment is a manual step on GitHub's stars page.

## Running it on GitHub Actions

`.github/workflows/classify-stars.yml` runs the classifier on demand, with no
local setup: open the Actions tab, pick *Classify starred repositories*, and give
it a `limit`. It needs two repository secrets — `STAR_LISTS_TOKEN` and
`JEV_API_KEY` — and uploads `classifications.json` as a build artifact. The apply
step is there but commented out, so the workflow cannot write to your Lists until
you enable it on purpose.

## Output

JSON on stdout (or `--output`). Each result carries the repository, its README
excerpt, the chosen List, confidence, probabilities, model, token usage, and
request time. `starred_total` is GitHub's total even for a sampled run, and
`unlisted_skipped` counts repositories the default mode left alone.

Confidence describes the probability distribution; it is not measured accuracy.

## Classification notes

Lists and stars are read through GitHub's GraphQL API. For each repository the
default branch's README is fetched over REST and stripped of badges, images,
HTML comments and blank lines; the first 2000 characters go to Jev along with the
name, description, topics, language and the List criteria. READMEs are not
cached. Up to 254 Lists are supported.

## Development

```bash
uv sync
uv run python -m unittest                  # 131 tests, no network access
uv run --with ruff==0.16.5 ruff check .
uv run --with ruff==0.16.5 ruff format --check .   # CI pins 0.16.5, so do you
```

Tests never touch the network and never depend on an ambient `gh` session.

## License

MIT — see [`LICENSE`](LICENSE). This is a fork of
[`yutkat/github-star-organizer-jev`](https://github.com/yutkat/github-star-organizer-jev),
and the upstream copyright notice is preserved.