# Security

- The assistant's server binds to `127.0.0.1` by default and should stay
  local-only — it has no authentication. Don't expose it to a network.
- Never commit `config.toml`: it can contain your Discord bot token and
  account id. It is gitignored; prefer the `DISCORD_BOT_TOKEN` env var for
  the token.
- Raw session logs (`logs/session_*.jsonl`) contain your Steam ID and live
  match data. They stay on your machine; be mindful before sharing them.

To report a vulnerability, open a GitHub issue (or a private security
advisory if the repository has them enabled).
