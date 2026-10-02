# Third-party data & services

**Valve / Dota 2.** This project is a community tool. It is not affiliated
with, endorsed by, or sponsored by Valve Corporation. Dota 2 and all hero,
item, and game names are trademarks or registered trademarks of Valve
Corporation. The assistant reads game state exclusively through Valve's
official Game State Integration (GSI) API — the same mechanism broadcast
tools use — and never touches the game process or memory.

**OpenDota.** Draft matchups, hero benchmarks, item popularity, match
details, and player histories come from the free
[OpenDota API](https://docs.opendota.com/). Responses are cached on disk to
stay far below the free-tier rate limits; please keep it that way when
modifying API code. Data is used under OpenDota's terms.

**dotabuff/d2vpkr.** The baked talent-tree file
(`src/dota_assistant/data/talents.json`, rebuilt with
`dota-assistant build-talents`) resolves numeric talent values from the
[dotabuff/d2vpkr](https://github.com/dotabuff/d2vpkr) mirror of Dota 2's
game files, combined with OpenDota's constants.

**Map coordinates.** `src/dota_assistant/data/map_coords.json` contains
building/Roshan world coordinates baked from Dota 2 map data, since GSI does
not send building positions.

**Microsoft edge-tts.** Discord voice announcements synthesize speech with
Microsoft's neural voices via the `edge-tts` package (requires internet).

**Anthropic Claude.** Optional AI coaching features call Anthropic's Claude
via your own Claude Code login or `ANTHROPIC_API_KEY`. No game data leaves
your machine unless you enable these features.
