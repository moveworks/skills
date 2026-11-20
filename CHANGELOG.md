# Changelog

Versions are per plugin. Tags look like `evals-v0.1.0`.

## evals 0.2.0

- New skill `rsi-loop`: a daily improvement loop for one assistant. It pulls the assistant's traffic with
  the Data API, draws a stratified sample (capability, experience, audience), rewrites each sampled
  conversation as an anonymized task or drops it at a sensitivity gate, builds an Eval API task suite with
  tagged judges, runs it, and writes a daily report: the negative experiences seen, the pass rate per tag
  over time, and the changes proposed to fix them. It proposes changes and never applies them.
- `.gitignore` now covers the loop's work folder (`rsi-loop/`).

## evals 0.1.0 (renamed)

- The plugin is now named `evals` (was `eval-api`), so future eval skills can ship in the same plugin.
  Install with `/plugin install evals@moveworks-skills`.

## Marketplace rename

- The marketplace is now named `moveworks-skills` (was `moveworks`), so it no longer clashes with other
  marketplaces named `moveworks`. Install with `/plugin install evals@moveworks-skills`.

## eval-api 0.1.0 (first release)

- First release: guided Eval API walkthrough (prerequisites, eval config, test cases, dry run, run, report)
  for Claude Code and Codex.
