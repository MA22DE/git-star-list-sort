# git-star-list-sort

Organize your GitHub stars into your existing [GitHub Lists](https://github.com/stars)
using [TypeSafe Jev](https://docs.typesafe.ai/).

A fork of [`yutkat/github-star-organizer-jev`](https://github.com/yutkat/github-star-organizer-jev)
turned into a globally installable CLI, with one significant addition: it can
generate descriptions for your Lists, because a bare List title gives the model
almost nothing to sort by.

## Install

```bash
git clone git@github.com:MA22DE/git-star-list-sort.git
cd git-star-list-sort
uv tool install --editable .
```

This installs three commands onto your `PATH` (via `~/.local/bin`):

| Command | Purpose |
| --- | --- |
| `git-star-list-sort` | Classify stars into Lists and write a JSON report |
| `git-star-list-sort-apply` | Apply a report to GitHub Lists |
| `git-star-list-sort-describe` | Generate List descriptions with an LLM |

Reinstalling is not needed after editing the source: the install is editable.

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

No credential files are read unless you ask for them. Resolution order:

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

If `gh` is logged in to more than one account the tool refuses to guess and asks
you to set `STAR_LISTS_TOKEN` explicitly.

## Usage

Generate descriptions once, whenever your List taxonomy changes:

```bash
# reads OPENROUTER_API_KEY from .env (or the environment)
git-star-list-sort-describe --describe-lists-output lists.json

# re-generate every description instead of keeping existing ones
git-star-list-sort-describe --describe-lists-output lists.json --force
```

Classify. **By default only stars that are already in at least one List are
classified**, so a mistake can only change *which* List a repository is in — it
can never file a previously unsorted star:

```bash
git-star-list-sort --output output/classifications.json

# include the stars that are not in any List yet
git-star-list-sort --include-unlisted --output output/classifications.json

# try a small batch first
git-star-list-sort --limit 10 --include-unlisted --output output/classifications.json
```

When repositories are skipped, the tool says so on stderr:

```
Report-only: 1202 unlisted stars skipped because they are not in any GitHub List;
pass --include-unlisted to classify them too
```

The report records `unlisted_skipped` so a scripted run can detect it too.

Apply. `apply` is never run automatically; it needs an explicit report and
validates first with `--dry-run`:

```bash
git-star-list-sort-apply --report output/classifications.json --dry-run
git-star-list-sort-apply --report output/classifications.json
```

Applying is **additive**: it reads current memberships, adds the assigned List,
and never removes a repository from a List. Lists are never created or deleted.
`No matching category` results are skipped.

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
uv run python -m unittest        # 81 tests, no network access
uv run --with ruff ruff check .
uv run --with ruff ruff format --check .
```

Tests never touch the network and never depend on an ambient `gh` session.

## License

See `LICENSE`.