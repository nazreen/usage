# ai-usage

A small CLI that prints how much of your Claude Code and Codex plan usage is left.

![ai-usage output](docs/screenshot.png)

```
Claude Code wk [███│█░░░░░░]  38% used (5d 1h)   5h [████████░░]  81% used (1h 43m)
Codex       wk [███│████░░░]  72% used (4d 19h)   resets 3 (next expires in 13d 20h)
Use         codex (codex weekly usage is 40.1 pts ahead of week elapsed; 3 banked resets, selection pace -259.9 pts; claude-code selection pace is +9.9 pts)
```

- Filled blocks show usage spent, matching the `% used` number. Light blocks show usage left.
- `│` on a weekly bar marks how much of the week has passed, counted from the left. Filled blocks reaching past the marker mean you are using quota faster than an even pace.
- Each tool shows its weekly window, and its 5-hour window when the plan has one.
- Codex also shows its available banked reset count and time until the next expiration. Only unexpired, available full resets supported by the current plan count. If the lookup fails, it shows `resets unknown` and ordinary usage remains available.
- Output is coloured on a terminal. Set `NO_COLOR=1` to turn colour off. Piped output and `--agent` are never coloured.
- `Use` names the recommended harness, picked by the same rule as `--agent` (see below).

## Requirements

- macOS (Claude Code stores its login in the macOS keychain).
- Python 3.9 or newer. No other dependencies.
- You are logged in to Claude Code (`claude`) and Codex (`codex login`) with a subscription plan.

## Install

```sh
curl -o ~/.local/bin/ai-usage https://raw.githubusercontent.com/nazreen/ai-usage/main/ai-usage
chmod +x ~/.local/bin/ai-usage
```

Make sure `~/.local/bin` is on your `PATH`.

## Usage

```sh
ai-usage           # one line per tool, with bars
ai-usage --agent   # JSON for agents choosing a harness
ai-usage --pick    # only the recommended harness name
ai-usage --help    # what the tool does
```

### `--agent`

Prints JSON with `recommended_harness`, a `reason`, the `rule` used, and the raw numbers for each window. Codex includes `resets.available_count` and `resets.expires_at` (sorted UTC timestamps). An unknown count is `null`, with an `error` explaining the lookup failure.

The recommended harness is the one with the lowest `selection_pace_pct`: weekly `pace_delta_pct` (weekly % used minus % of the week elapsed), minus 100 points per available banked reset. For example, Codex at +40 points with one reset scores -60 and is preferred over Claude Code at -20. This ranking heuristic gives Codex credit for its reserve weekly allowances. Unknown reset counts give no bonus. The usage bars and window percentages continue to show actual current usage.

A harness is skipped if it has 0% weekly quota left, under 10% of its 5-hour quota left, or no weekly window. Banked resets do not bypass those checks: this CLI never redeems them. When Codex is skipped with resets available, `reason` suggests redeeming one. `reason` names each skipped harness and why. A harness whose usage could not be fetched is listed in `unavailable_harnesses` and named in `reason`. If no harness has usable data, `recommended_harness` is `null`.

`wait_option` is set when a harness was skipped only for its 5-hour window, that window resets within 60 minutes, and its selection pace is better than the recommended harness (or there is none). It gives `harness`, `resets_in_minutes`, the actual weekly `pace_delta_pct`, and `selection_pace_pct`. Otherwise it is `null`. The default output adds the same advice to `Use`, for example `or wait 0h 53m for claude-code (selection pace +5.4 pts)`.

An agent rule might say: "Before picking a harness, run `ai-usage --agent` and use `recommended_harness`."

### `--pick`

Prints only the recommended harness name, `claude-code` or `codex`, picked by the same rule as `--agent`. If no harness is usable, it prints `none` and exits with status 1. It is never coloured.

Use it when a script or coordinator only needs to know which harness to start:

```sh
h=$(ai-usage --pick) || echo "no harness has quota"
```

## How it works

It calls the same endpoints the official clients use:

- Claude Code: `GET https://api.anthropic.com/api/oauth/usage`, with the OAuth token read from the keychain item `Claude Code-credentials`.
- Codex: `GET https://chatgpt.com/backend-api/wham/usage`, with the token read from `~/.codex/auth.json`.
- Codex resets: `GET https://chatgpt.com/backend-api/wham/rate-limit-reset-credits`, using the same Codex token and account header. Reset data has its own cache entry.

Tokens are read at run time and sent only to those two hosts. They are never written to disk. All requests are read-only; no resets are redeemed.

Each API response is cached in `~/.cache/ai-usage/` (or `$XDG_CACHE_HOME/ai-usage/`), with file mode `600`: Codex for 60 seconds, Claude for 5 minutes. Failed requests are not cached.

### Claude rate limits (HTTP 429)

Anthropic's `/api/oauth/usage` endpoint allows only a few requests per access token, then returns HTTP 429 for up to an hour ([anthropics/claude-code#30930](https://github.com/anthropics/claude-code/issues/30930)). Claude Code's own sessions use the same token, so the limit can be spent before `ai-usage` runs.

- **Statusline snapshot.** Claude Code 2.1.80+ passes `rate_limits` to its statusline script on stdin. If your statusline saves them, `ai-usage` reads the snapshot instead of calling the endpoint. A snapshot is used when it is under 10 minutes old and none of its windows has reset since it was written. Add this to your statusline script, where `$input` holds the JSON from stdin:

  ```sh
  # Save Claude rate limits for ai-usage, so it need not call the rate-limited usage endpoint.
  if printf '%s' "$input" | jq -e '.rate_limits.seven_day' >/dev/null 2>&1; then
    (d="${XDG_CACHE_HOME:-$HOME/.cache}/ai-usage"; umask 077; mkdir -p "$d" &&
     printf '%s' "$input" | jq -c '{written_at: now, rate_limits}' > "$d/claude-statusline.json.$$" &&
     mv -f "$d/claude-statusline.json.$$" "$d/claude-statusline.json")
  fi
  ```

  The snapshot does not name the account. If you run Claude Code under more than one account (for example with `CLAUDE_CONFIG_DIR`), the last session to refresh its statusline wins.
- **Back-off.** On a 429, `ai-usage` sends no request to that endpoint until `Retry-After` passes (5 minutes when it is missing or `0`). Meanwhile it shows the last good response up to 3 hours old, marked `(stale Nm, rate limited)`; `--agent` adds `stale_minutes`. With no cached response, the tool shows an error with the wait time, and the other harness is still recommended.

`--agent` reports Claude's `source` as `statusline` or `api`.

These endpoints are not documented public APIs. They can change or break without notice.

## Tests

```sh
python3 -m unittest discover -s tests -v
```

Tests use synthetic accounts, mocked network responses and a temporary cache; they do not access real credentials or redeem resets.

## License

MIT
