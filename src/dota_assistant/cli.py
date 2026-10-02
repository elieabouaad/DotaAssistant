"""`dota-assistant` command dispatcher.

One console script, subcommand per tool. `serve` is the default, so a bare
`dota-assistant` starts the GSI receiver + dashboard. Each subcommand's own
flags (e.g. `serve --turbo --overlay`, `overlay --setup`, `stats --backfill`)
are passed through untouched.
"""

from __future__ import annotations

import sys

from . import config

USAGE = """\
dota-assistant [command] [options]

Commands:
  serve            Start the GSI receiver + web dashboard (default)
                   options: --turbo  --overlay  --no-enrich
  overlay          In-game HUD (own process; needs the server running)
                   options: --setup (drag to reposition)
  scrape           Pull your full OpenDota history + improvement report
                   options: <account_id>  --no-claude
  enrich           Backfill OpenDota parses for your match history
                   options: --max N  --status
  stats            Local match database tools
                   options: --backfill  --list
  build-talents    Rebuild the talent-tree data file from OpenDota + d2vpkr
  gsi-config       Print the GSI config file to install into Dota 2
  -h, --help       Show this help
"""

COMMANDS = {"serve", "overlay", "scrape", "enrich", "stats", "build-talents", "gsi-config"}


def gsi_config_text() -> str:
    """The packaged GSI config with the configured port substituted in."""
    cfg = config.section("server")
    text = config.read_resource_text("gamestate_integration_assistant.cfg")
    return text.replace("127.0.0.1:53100", f"{cfg['host']}:{cfg['port']}")


def main() -> None:
    argv = sys.argv[1:]
    if argv and argv[0] in ("-h", "--help"):
        print(USAGE)
        return
    if argv and argv[0] in COMMANDS:
        cmd, rest = argv[0], argv[1:]
    else:
        cmd, rest = "serve", argv  # bare flags go to serve

    # Modules read their flags from sys.argv (serve even at import time), so
    # hand the remaining args through as the process argv.
    sys.argv = [f"dota-assistant {cmd}", *rest]

    if cmd == "serve":
        from . import server
        server.main()
    elif cmd == "overlay":
        from . import overlay
        overlay.main()
    elif cmd == "scrape":
        from . import opendota_scraper
        opendota_scraper.main(rest)
    elif cmd == "enrich":
        from . import enrich
        enrich._main(rest)
    elif cmd == "stats":
        from . import stats
        stats._main(rest)
    elif cmd == "build-talents":
        from . import build_talents
        build_talents.main()
    elif cmd == "gsi-config":
        print(gsi_config_text())


if __name__ == "__main__":
    main()
