# Moveworks developer plugins

Plugins for Claude Code and Codex that help you build on the Moveworks APIs. Your coding agent walks you
through the API, step by step, in your own environment.

| Plugin | What it does |
|---|---|
| [`eval-api`](plugins/eval-api/) | Evaluate your Moveworks agents with the [Eval API](https://docs.moveworks.com/api-reference/alpha-eval-api/overview): config, test cases, dry run, run, and report |

## Install

### Claude Code

```
/plugin install eval-api --marketplace moveworks/skills
```

On Claude Code older than 2.1.275, add the marketplace first:

```
/plugin marketplace add moveworks/skills
/plugin install eval-api@moveworks
```

### Codex

```
codex plugin marketplace add moveworks/skills
```

Then open the plugin browser in Codex, choose **Moveworks**, and install **eval-api**.

### Use it

Ask your agent: "Run a Moveworks eval on my agent."

### Updates

Fixes reach you when you refresh the marketplace:

- Claude Code: `/plugin marketplace update moveworks`
- Codex: `codex plugin marketplace upgrade moveworks`

## Requirements

`bash`, `curl` and `jq`, plus access to the Moveworks API you're using. Each plugin's README lists what it needs.

## Support

See [SUPPORT.md](SUPPORT.md). Report security issues privately as described in [SECURITY.md](SECURITY.md).

## License

[MIT](LICENSE.txt)
