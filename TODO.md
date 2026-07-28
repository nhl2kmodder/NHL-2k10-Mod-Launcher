# NHL 2K10 Mod Launcher — TODO / roadmap

Open work and ideas, grouped by system. This is the living wish-list — good places for new
contributors to jump in. For *how* each system currently works, see `findings/`.

## Portraits
- Conflicts could be resolved by player's number?
- When I search for Elias Pettersson in the Portraits "filter players", only one shows up,
  but there are two — so I can't even see duplicate players!
- Jersey swap is still not seamless. More effort needs to go into the ML model to better
  wrap the jersey around the neck with no gaps. Maybe let users adjust the head/jersey
  before the model splices them together.

## IFF Textures
- Can we split up `global.iff` (or all IFF files)? Applying changes while looking at 446
  textures takes a long time.
- (In Progress) Discover the remaining IFF files that aren't found (multi-texture packs that don't show up,
  or any in the game files we haven't discovered yet).
  - `led_{team}.iff` is pretty bare — I wonder if there's more to discover there.
  - Not textures specifically, but can we identify the remaining data in these IFF files to
    see what else we can modify? Probably scale, positioning, etc.

## Overlays / Scorebug
- Can we modify more overlays like we did the scorebug?
- I've gotten close to adding "shots" on the scorebug. We need a way to safely **add elements**
  to the widget and bind the tracked shots to a text element so they update.
- When the scorebug is moved to the top, the in-game team-change elements (lines, strategy,
  picture-in-picture) crash — they need to auto-switch to the bottom anchor. Leave the
  top-center anchor alone for now.
- Can we move the Powerplay / Offside / Icing warnings? Would love to attach them to the
  scorebug.

## Audio
- (In Progress) We need a way to **name these files** — at minimum categorize them better. Right now 80,000+
  files are hand-named/categorized, which takes too long. Identify a pattern in the offsets,
  or read the TOC while the game is live for authored names / audio banks they pull from.
- Still possibly some undiscovered assets — ones I haven't noticed yet: Whistles, Posts,
  Referee Voice, Crowd Team Chants.

## Commentary (Play-by-Play, Color, PA)
With new rosters, player names don't match (e.g. DeBrusk overwrote a player named Adams, so
he's always called "Adams"). Fix this in bulk, like we did portraits. Multi-part:
- (In Progress) Identify the commentary name bank / how it's assigned.
- Replace all variations of the name (there are multiple).
- Can we use AI/ML to generate the name? We have full access to the Play-by-Play AND Color
  commentators (two different voices), plus the PA announcer. Could we build 3 voice models
  to synthesize names? (e.g. no "DeBrusk" line exists → find the pronunciation, run it through
  the model, replace the lines with the new name.)

## Mod Packs
- Test and verify that it all works.

## Models
- I extracted a goalie mask into Substance Painter. We need to do this with **all** models
  (uniforms, jumbotrons, boards, heads, crowd… everything!).
- Can we modify and replace models without breaking the game?

## Rosters
- Lots left to discover in the roster editor.
- Can we save the roster through the tool when we make changes?
- (In Progress) Find any audio ID / binding for teams and players. I know for custom players, there is a list of first and last names to choose from, if you choose one of those, it is associated with an audio name. (If you type in one that isnt on the list, the game simply does not play audio for you). Real players will have extra audio for them, example, the color commentary will talk about how [x] player has some skill / ability, and how they use it in game. Example, offset 37171000 - "Teams just hate playing against Jordan Tootoo. He's always getting under everyones skin because of his abrasive style.". This is depth that we would like to support eventually (finding tags for star players), but for now we just want to use the created skater flow for all roster players. (Note - there are generic tags like this call for custom players as well, that just say things like "this guy..." instead of specific player names)
- Identify more team colors (fonts, helmet colors, arena dashers) — only primary and
  secondary discovered so far.
- Allow us to re-name strings with longer names (ex - "Atlanta" (7 characters) -> "Winnipeg" (8 characters). This was attempted before in a quick/dirty manner, and did not work (the entire file got read incorrectly after - everything shifted with the change). We either have to investigate how to replace in-place or we have to figure out how we can re-direct like we do with textures.

## Unanswered questions
- The **boot screen** isn't solved. There are NHL logos that aren't the ones we apply — find
  where they live and update them alongside our team logos.
- We haven't found **all** the rink textures. There are jumbotron assets, seats, and specific
  rink textures still undiscovered — we see boards/glass displays/etc., but are missing some
  important pieces.
- Find a way to change the **dasher color** per arena/team. This seems to be a recolor value,
  not a texture asset (most teams red; some black, blue, or yellow).
