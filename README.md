# GitHub Star Organizer — Jev

Use [TypeSafe Jev](https://docs.typesafe.ai/) to classify GitHub stars against
your existing GitHub Lists. Generate a JSON classification report and optionally
apply its assignments to GitHub.

## Setup

1. Install Python 3.11 or later and [uv](https://docs.astral.sh/uv/).
2. Create at least one List at <https://github.com/stars>.
3. Set `STAR_LISTS_TOKEN` to a personal access token owned by the account whose
   stars and Lists you want to classify. A classic PAT with the `user` scope can
   be used for Lists; private repositories also require repository access
   (`repo` for a classic PAT).
4. Set the `JEV_API_KEY` environment variable to an API key from the
   [TypeSafe console](https://console.typesafe.ai/).

The program calls GitHub's GraphQL and REST APIs directly with `STAR_LISTS_TOKEN`.
GitHub CLI and interactive login are not required.

## Run

From this repository's root:

```bash
uv run --frozen github-star-organizer-jev --output output/classifications.json
```

By default, the script fetches and classifies all accessible stars, including
already listed ones. To try a small sample:

```bash
uv run --frozen github-star-organizer-jev --limit 10 --output output/classifications.json
```

With a positive `--limit`, fetching stops as soon as that many repositories are
available. `--limit 0` means all stars. `--model` defaults to `jev-latest`.
Omit `--output` to print the JSON report to stdout.

Use `--endpoint URL` or the `JEV_ENDPOINT` environment variable to select another
Jev-compatible provider. The command-line option takes precedence over the
environment variable; the default is `https://api.typesafe.ai/v1/systemone`.
The provider must accept the same typed-question request and response format
and Bearer authentication. Set `JEV_API_KEY` to that provider's API key and
use `--model` to choose its model.

Each completed classification is printed once to stderr with its destination
List, confidence, and request time. JSON on stdout stays suitable for piping.

## GitHub Actions

The workflow is `.github/workflows/classify-stars.yml`, named
`Classify starred repositories`.

1. Commit and push the workflow to the repository's default branch.
2. Add these repository secrets under **Settings > Secrets and variables > Actions**:

   | Secret | Purpose |
   | --- | --- |
   | `STAR_LISTS_TOKEN` | The user's GitHub PAT described above |
   | `JEV_API_KEY` | The TypeSafe API key |

3. Open **Actions > Classify starred repositories > Run workflow**.
4. Choose the limit, model, and optional endpoint. Both the workflow and CLI
   default to all stars (`0`) and `jev-latest`; use a positive limit for a smaller batch.
5. Download the `star-classifications` artifact to inspect `classifications.json`.

Runs are serialized, with a six-hour timeout and 30-day artifact retention.
The automatic `GITHUB_TOKEN` is used for checkout; GitHub API requests use
`STAR_LISTS_TOKEN`. The separate CI workflow runs tests and Ruff checks without
API credentials.

The weekly schedule is present but commented out. Uncomment the `schedule`
block to run on Mondays at 00:00 UTC. Scheduled runs use the same default limit
and model without requiring dispatch inputs. Set the repository variable
`JEV_ENDPOINT` to select a provider for scheduled runs or when the endpoint input
is blank. An explicit workflow input overrides the repository variable.

The `Apply List assignments` step is also commented out. Uncomment that step
to apply the generated report after it has been uploaded as an artifact.
Until then, workflow runs only generate classification reports.

## Apply classifications

Applying requires a classic PAT with the `user` scope in `STAR_LISTS_TOKEN`.
Only the token owner's existing Lists can be used. Jev credentials are not
needed by the apply command.

To validate a report against current GitHub data without making changes:

```bash
uv run --frozen github-star-organizer-jev-apply \
  --report output/classifications.json --dry-run
```

To apply it:

```bash
uv run --frozen github-star-organizer-jev-apply \
  --report output/classifications.json
```

The command validates the report, checks that the token belongs to its owner,
and confirms that destination Lists still exist before changing memberships.
It keeps the memberships read at application time, adds the selected List, and
skips repositories already in that List. `No matching category` results are
skipped. Lists are never created or deleted.

Membership reads must be complete; a failed or partial read stops application.
Avoid concurrent membership edits while applying: GitHub's update operation
replaces an item's entire List membership set. A rerun skips assignments that
have already succeeded.

## Classification

The script reads Lists and stars through GitHub's GraphQL API. For each selected
repository, it fetches the default branch's README through the REST API and strips
common image/badge markup, HTML comments/tags, and empty lines. It sends the first
2,000 characters to Jev alongside the repository's name, description, topics,
language, other metadata, and the List names/descriptions.

If a README is absent or cannot be fetched, classification uses metadata alone.
READMEs are not cached. Jev can choose `No matching category` when no List fits
or there is not enough information. Up to 254 existing Lists are supported,
leaving one choice for this result.

The report contains each repository and its README excerpt, suggested List,
confidence, probabilities, model, token usage, and Jev request time including
retries. `No matching category` is represented by `list_id: null`,
`list_name: "No matching category"`, and the probability key
`no_matching_category`. `starred_total` is GitHub's reported total even when only
a sample is fetched. Confidence describes the probability distribution; it is
not a measured classification accuracy.
