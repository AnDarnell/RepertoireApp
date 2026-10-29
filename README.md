# Repertoire App

A Python-first application for importing Lichess games, building a player's
repertoire tree, and enriching selected positions with master-game statistics.

## Planned data flow

1. Import a player's games from Lichess as PGN.
2. Parse and store the games.
3. Build the player's repertoire tree from the imported games.
4. Enrich unique positions in that tree with cached master-database statistics.

The initial theory threshold is 20 master games reaching a position. This is a
working definition of established theory, not an evaluation of move quality.

## Project structure

- `src/repertoire_app/lichess/` — Lichess game import
- `src/repertoire_app/pgn/` — PGN parsing
- `src/repertoire_app/repertoire/` — repertoire-tree construction
- `src/repertoire_app/master_data/` — master-database queries and caching
- `src/repertoire_app/storage/` — persistence
- `tests/` — automated tests, organized by application area

## Import games

Install the project and run the importer:

```powershell
python -m pip install -e .
repertoire-import LichessUsername --max-games 100
```

The importer reads PGN from Lichess and stores each game's metadata and main
line in a local SQLite database. Repeated imports do not duplicate games.
Each completed import is saved atomically; if downloading or parsing fails, no
part of that import is added. The cache keeps up to three players. Importing a
fourth removes the games for the least recently imported player; the shared
Masters-position cache is not removed. The default database is
`repertoire.sqlite3` in the current directory. Use `--database`, `--since`, and
`--until` to select a different database or date range. Date arguments are
Unix timestamps in milliseconds.

Run the test suite with:

```powershell
$env:PYTHONPATH = "src"
python -m unittest discover -s tests -v
```

## Build a repertoire tree

Build a separate tree for the games played as White or Black:

```powershell
repertoire-tree LichessUsername --color white
repertoire-tree LichessUsername --color black
```

The command builds the repertoire locally from imported games first, without
Masters API calls. By default, it trims branches reached in fewer than 2 games
and summarizes the remaining lines grouped by their first 4 half-moves. Change
the cutoff with `--min-repertoire-games` or the prefix length with
`--opening-plies`. Use `--tree` to print the detailed move tree, or `--json` to
print the tree as JSON. `--show-all-branches` keeps infrequent branches in the
local repertoire. Each tree node includes its FEN, the number and fraction of
imported games reaching that position, and whether the incoming move was
played by the selected player or their opponent. Position counts ignore FEN
move counters, so transpositions share the same position count in the summary
while their distinct move sequences remain separate tree branches. Only
Standard games from the normal starting position are included; games of other
variants are excluded and counted in the summary.

## Explore in your browser

Start the local repertoire explorer:

```powershell
repertoire-web
```

The app opens `http://127.0.0.1:8765` in your browser. Enter a Lichess username,
then click **Hämta från Lichess** to import up to 100 recent games (adjustable
up to 1,000). Bullet, blitz, and rapid are selected by default; uncheck a
format to exclude it from that import. Re-imports only add games and never
remove previously cached games just because their format was not selected.
You can also search games already in the local database. Choose a color and
frequency cutoff, then click one of the common moves from the starting
position. The same move list
updates at each position so you can continue along any branch while the board
follows. Each move and selected line shows White's score percentage among
decided games, regardless of the selected repertoire color. White wins count
as 1 point, draws as 0.5, and White losses as 0; games without a final result
are excluded. The score colors indicate whether the result favors the selected
repertoire color: 45–55% is neutral, while higher or lower values are colored
positive or negative accordingly. Use the board-flip button to change sides.
Known opening positions show their ECO code and name below the board and in
the review and strong-result lists.
Use `Ctrl+C` in the terminal to stop the server. It runs locally and does not
make Masters API calls.

To restart the server automatically when Python, HTML, or opening-data files
change during development, use:

```powershell
repertoire-web --reload
```

ECO codes and opening names are looked up locally using the
[Lichess chess-openings dataset](https://github.com/lichess-org/chess-openings),
released under CC0. Its license is included with the bundled data.

## Enrich with master-game results

Add `--masters` to look up positions in Lichess's Masters opening database:

```powershell
$env:LICHESS_API_TOKEN = "your-lichess-oauth-token"
repertoire-tree LichessUsername --color white --masters
```

The Masters endpoint requires OAuth2 authentication. Create an API token in
your Lichess account and provide it through `LICHESS_API_TOKEN`; do not commit
the token to the project or put it directly in command-line arguments.

When `--masters` is supplied, the app then cross-checks the retained repertoire
positions against Lichess's Masters opening database. It does not query each
game separately: games reaching the same tree position are counted together.
The lookup follows each branch through positions reached by at least 20
master games. The first position with fewer than 20 games is included and
marked below the theory threshold; its descendants are not queried. Change
the cutoff with `--theory-threshold`. Results are from White's perspective
(White wins, draws, Black wins), and are cached in the same SQLite database by
position, including across transpositions and later runs. Without `--masters`,
tree generation remains offline. Each lookup also supplies results for
candidate next moves, so those counts are reused instead of requesting the
next position solely to find its total.

The same `--min-repertoire-games` cutoff determines which branches are
available to enrich. If `--show-all-branches` is used with `--masters`, less
frequent branches remain visible but are still excluded from Masters lookups.
If Lichess returns HTTP 429, the client honors `Retry-After` and retries with
increasing delays. Successful lookups remain cached, so rerunning after the
rate limit clears resumes without repeating completed requests. Progress and
rate-limit wait messages are written to the terminal's standard error stream.

## Request a repertoire review

In the web app, use **Begär granskning** next to a position in **Ställningar att
granska**. Set `LICHESS_API_TOKEN` before starting `repertoire-web`. A review
queries only the requested line, reuses cached Masters results, and considers
candidate Masters moves with at least 100 games. A move played in at least 75%
of the player's games is treated as the repertoire choice; if the player's
games split across moves, the review shows the player's best-scoring move and
the strongest eligible Masters suggestion. Review steps appear below the board
one at a time; **Föregående** and **Nästa** navigate between positions. At a
branch, choose one of the player's moves to continue the review along that line.

Review outliers are measured against the selected player's overall score
percentage for the chosen color: a position is flagged when its score is more
than 10 percentage points above or below that baseline, with at least 10
decided games.
