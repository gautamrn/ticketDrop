# Screenshots

Shots for the portfolio site (gautamn.xyz). Stage them against the local mock
pipeline from the main README's "Running the full pipeline locally" section so
every panel is populated, then drop the PNGs in this folder with the names
below — the main README's Screenshots section already links these filenames.

| File | What to capture | How to stage it |
| --- | --- | --- |
| `panel-feed.png` | Admin panel live drop feed with several drops visible | Run mocks + ingest + harness + panel, then provoke drops: `POST http://localhost:8098/admin/presale?id=90001` and `POST http://localhost:8098/admin/ticketed?id=90002`, plus `POST http://localhost:8099/admin/add` |
| `panel-detail.png` | Drop detail view: FSM transitions, hype score breakdown, healed-selector tag if present | Click a drop in the feed after it has moved through several states |
| `panel-health.png` | Health strip + control panels (watchlist, preferences, overrides) | Same session as the feed shot; scroll so the health strip and one control panel share the frame |

Tips:

- Seed at least one preference first (`python -m harness prefs add --kind artist --label "Prospa"`) or every drop scores zero and the feed looks empty.
- Capture at 100% browser zoom, light terminal/panel contrast reads better on the portfolio's dark sections — take one of each if unsure.
- Crop to the panel, not the whole desktop. ~1600px wide is plenty.
