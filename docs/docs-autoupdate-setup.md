# Auto-updating the product guide

The product guide ([`src/newslet/docs/product.md`](../src/newslet/docs/product.md))
is meant to stay fact-based and in step with the code. A GitHub Actions workflow
regenerates it with an AI step whenever code is pushed to `main`, so the guide
doesn't drift as features change.

## How it stays in sync

There are two layers, and only one of them is a "sync" job:

1. **Markdown → HTML is always live.** The HTML viewer (`index.html`) fetches
   `product.md` at runtime (served at `/docs/content.md`) and renders it in the
   browser. The HTML never stores its own copy of the prose, so the rendered
   guide can't fall out of sync with the markdown. There is nothing to keep in
   sync here, by construction.

2. **Code → markdown is refreshed by AI.** This is the workflow below. On a push
   to `main` that touches the code, it asks Claude to update `product.md` to
   match, then commits the result if anything changed.

## The workflow

> **Why this file isn't already committed:** GitHub rejects pushes that add or
> change files under `.github/workflows/` unless the pushing token carries the
> `workflow` scope. The automation token used to open this change doesn't have
> it, so the workflow is documented here for you to add. Save it as
> `.github/workflows/docs-autoupdate.yml`, commit, and push (the same one-time
> step the [auto-deploy setup](github-actions-setup.md) calls out).

```yaml
name: docs-autoupdate

# Refresh the product guide whenever code that it describes lands on main.
# paths-ignore (not paths) so the guide's own output can't retrigger the job —
# a commit that only touches the generated doc is skipped, which breaks the loop.
on:
  push:
    branches: [main]
    paths-ignore:
      - 'src/newslet/docs/**'
      - 'docs/**'
      - '*.md'
      - '.github/**'

permissions:
  contents: write          # commit the refreshed guide back to main

concurrency:
  group: docs-autoupdate
  cancel-in-progress: false

jobs:
  refresh:
    runs-on: ubuntu-latest
    # Belt-and-suspenders against loops: never run on the bot's own commit.
    if: ${{ !contains(github.event.head_commit.message, '[docs-bot]') }}
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - name: Update the product guide from the code
        uses: anthropics/claude-code-action@v1
        continue-on-error: true   # documentation must never block the pipeline
        with:
          # Use whichever credential you have set as a repo secret. An OAuth
          # token (from `claude setup-token`) or a raw Anthropic API key both
          # work; leave the other blank.
          claude_code_oauth_token: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}
          anthropic_api_key: ${{ secrets.ANTHROPIC_API_KEY }}
          claude_args: '--model claude-sonnet-4-6'
          prompt: |
            You maintain newslet's product guide at src/newslet/docs/product.md.

            Read the application code under src/newslet/ (handlers, the feature
            modules, contracts.py, infra/template.yaml) and update
            src/newslet/docs/product.md so it stays accurate and fact-based.

            Hard rules:
            - Edit ONLY src/newslet/docs/product.md. Touch no other file.
            - Preserve the document's structure: the three-level model, the
              per-feature sections, and the `:::tier little` / `:::tier medium`
              fences. Keep every fence balanced (each `:::tier ...` has a
              closing `:::`).
            - Keep the plain-language sections free of technology; put light
              detail in `:::tier little` blocks and concrete detail (module
              names, AWS services, data shapes, schedules, numbers) in
              `:::tier medium` blocks.
            - Write like a sharp human editor: plain, direct, specific. No
              significance inflation, no AI vocabulary (delve, landscape, realm,
              tapestry), no "not just X but Y", no em-dash overuse, no
              rule-of-three filler.
            - Only change what the code actually requires. If the guide is
              already accurate, make no edits at all.

      - name: Commit the refreshed guide
        run: |
          if [ -n "$(git status --porcelain src/newslet/docs/product.md)" ]; then
            git config user.name  "newslet-docs-bot"
            git config user.email "actions@github.com"
            git add src/newslet/docs/product.md
            git commit -m "[docs-bot] refresh product guide from code"
            git push
          else
            echo "Product guide already up to date."
          fi
```

## One-time setup

1. **Add a credential secret.** On the repo:
   **Settings → Secrets and variables → Actions → New repository secret**, and
   add either:
   - `CLAUDE_CODE_OAUTH_TOKEN` — from running `claude setup-token` locally, or
   - `ANTHROPIC_API_KEY` — a key from the
     [Anthropic console](https://console.anthropic.com).

   (The repo's existing `Pullfrog` workflow already references both names, so
   you may have one set up.)

2. **Add the workflow file** above, commit, and push it to `main`.

3. **Done.** The next push that changes code under `src/` triggers a run; watch
   it in the **Actions** tab. A run either commits a `[docs-bot] refresh product
   guide` change or logs "already up to date".

## Notes and trade-offs

- **Loop safety** is handled two ways: `paths-ignore` skips commits that only
  touch the generated guide, and the `if:` guard skips any commit whose message
  carries `[docs-bot]`.
- The step is `continue-on-error`, so a flaky model call or a missing secret
  never blocks the rest of CI (tests and deploy live in `ci.yml` and run
  independently of this workflow).
- It commits straight to `main`. If you'd rather review the AI's edits, swap the
  final step for a `peter-evans/create-pull-request` step and merge the guide
  updates yourself.
- The guide is generated, but it is not throwaway: edit `product.md` by hand any
  time. The next run treats your text as the current state and only adjusts what
  the code requires.
