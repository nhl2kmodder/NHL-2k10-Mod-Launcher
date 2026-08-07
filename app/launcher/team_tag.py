"""Work out which NHL team an audio stream belongs to.

Four sources, in order of trust:

0. The stream's SLOT in its wave bank -- ``audio_team_slots.json``. Several banks hand
   the engine a team's audio by position alone: there is no lookup table, the team id
   *is* the index. Team ids are 0..29 in city-alphabetical order, which is exactly the
   order of ``TEAMS`` below, so the id doubles as an index into it.

     pamusic.bin              cue 43 + id   -> that team's goal song
     teams.bin                slot i        -> id i//2 (two PxP takes per team)
     loadingaudio_teams.bin   slot i        -> id i//2 (two pre-game takes per team)
     horns.bin                run slot i    -> the i'th of ids 0..29 minus {1,8,9};
                                               Atlanta, Columbus and Dallas sit in the
                                               tail block and are remapped by the bank
                                               trailer's 29-entry array

   This is the strongest source there is -- it is how the game itself picks the file --
   and it is the only one that can tag a stream whose name says nothing (a goal song is
   just a music track; nothing in it mentions the team). Generated offline; see the
   project_goal_songs note for the evidence.
1. The arena sound banks. A stream referenced by exactly one ``arena_<code>`` bank is
   that team's by construction -- this is an archive fact, not a guess, but it only
   covers ~7% of the catalogue.
2. The stream's name -- but only when it spells the team out in full. A city
   (``Crowd_Chant_Vancouver``, ``Chatter_Str_OnMinnesota``) or a full nickname
   (``Goal_Horn_Canucks``) is trusted; an abbreviation is not.
3. What the stream actually SAYS -- ``audio_team_from_text.json``, built offline from the
   full ASR sweep of all 54,268 speech takes. Filenames are transcripts truncated to
   ~25 chars, so a line that names its team late ("Save made, oh it doesn't get any
   bigger. The *Buffalo* fans...") has the team in the audio and nothing in the stem.
   This source only fills in where 1 and 2 are silent, and it is a lookup rather than a
   rule: every entry was decided offline under the same "spelled out in full, exactly
   one team" test, so nothing here can be looser than what ``team_from_name`` allows.

   Two things the offline pass had to get right, both learned the hard way:
   *ambiguity must count the weak nicknames too* -- "The **Ducks** want to finish strong
   ... but the **Thrashers**..." reads as single-team if only ``_STRONG`` votes, and
   tagged 9 two-team lines with the wrong one until weak tokens were allowed to veto;
   and *composite takes* (one file holding two alternate readings) name two teams and
   must stay blank. Validation over the takes where the name ALSO has an opinion:
   3,680 agree, 2 conflict -- and both conflicts are cases like these, where the name
   wins anyway because this source never overrides it.

Everything else stays blank. A wrong team is worse than no team here: the Team filter
is used to find a specific team's audio, and a false positive means the user edits the
wrong file, so the shipped tag is deliberately sparse and the blanks are meant to be
filled in by hand.

Explicitly NOT matched from a name: three-letter codes (``VAN``, ``COL``, ``TB``) and
short forms (``Habs``, ``Leafs``, ``Sens``, ``Pens``, ``Caps``, ``Wings``, ``Hawks``...).
They are real names for the team but they are also substrings and slang that turn up in
18k lines of play-by-play, and there is no way to tell the two apart from the stem alone.
Codes survive only via source 1, where the arena bank -- not the name -- is the evidence.
"""
from __future__ import annotations

import re

# Full names as shown in the UI -- must stay identical to TEAMS in nhl2k10_launcher.py
# or the Team filter combobox will offer values that never match a row.
TEAMS = [
    "Anaheim Ducks", "Atlanta Thrashers", "Boston Bruins", "Buffalo Sabres",
    "Calgary Flames", "Carolina Hurricanes", "Chicago Blackhawks",
    "Colorado Avalanche", "Columbus Blue Jackets", "Dallas Stars",
    "Detroit Red Wings", "Edmonton Oilers", "Florida Panthers",
    "Los Angeles Kings", "Minnesota Wild", "Montreal Canadiens",
    "Nashville Predators", "New Jersey Devils", "NY Islanders", "NY Rangers",
    "Ottawa Senators", "Philadelphia Flyers", "Phoenix Coyotes",
    "Pittsburgh Penguins", "San Jose Sharks", "St. Louis Blues",
    "Tampa Bay Lightning", "Toronto Maple Leafs", "Vancouver Canucks",
    "Washington Capitals",
]

# team -> (arena/roster codes, city spellings, nickname spellings)
#
# Codes are kept only so ``team_for`` can read an arena bank's "VAN only" reference; they
# are never matched against a stream name. Cities and nicknames are matched
# case-insensitively but only as whole tokens.
_ALIASES: dict[str, tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]] = {
    "Anaheim Ducks":        (("ANA",),        ("Anaheim",),                  ("Ducks", "MightyDucks")),
    "Atlanta Thrashers":    (("ATL",),        ("Atlanta",),                  ("Thrashers",)),
    "Boston Bruins":        (("BOS",),        ("Boston",),                   ("Bruins",)),
    "Buffalo Sabres":       (("BUF",),        ("Buffalo",),                  ("Sabres",)),
    "Calgary Flames":       (("CGY", "CAL"),  ("Calgary",),                  ("Flames",)),
    "Carolina Hurricanes":  (("CAR",),        ("Carolina",),                 ("Hurricanes",)),
    "Chicago Blackhawks":   (("CHI",),        ("Chicago",),                  ("Blackhawks", "BlackHawks")),
    "Colorado Avalanche":   (("COL",),        ("Colorado",),                 ("Avalanche",)),
    "Columbus Blue Jackets":(("CBJ",),        ("Columbus",),                 ("BlueJackets",)),
    "Dallas Stars":         (("DAL",),        ("Dallas",),                   ("Stars",)),
    "Detroit Red Wings":    (("DET",),        ("Detroit",),                  ("RedWings",)),
    "Edmonton Oilers":      (("EDM",),        ("Edmonton",),                 ("Oilers",)),
    "Florida Panthers":     (("FLA", "FLO"),  ("Florida",),                  ("Panthers",)),
    "Los Angeles Kings":    (("LAK", "LA"),   ("LosAngeles", "Los Angeles"), ("Kings",)),
    "Minnesota Wild":       (("MIN",),        ("Minnesota",),                ("Wild",)),
    "Montreal Canadiens":   (("MTL", "MON"),  ("Montreal",),                 ("Canadiens",)),
    "Nashville Predators":  (("NSH", "NAS"),  ("Nashville",),                ("Predators",)),
    "New Jersey Devils":    (("NJD", "NJ"),   ("NewJersey", "New Jersey"),   ("Devils",)),
    "NY Islanders":         (("NYI",),        ("NYIslanders",),              ("Islanders",)),
    "NY Rangers":           (("NYR",),        ("NYRangers",),                ("Rangers",)),
    "Ottawa Senators":      (("OTT",),        ("Ottawa",),                   ("Senators",)),
    "Philadelphia Flyers":  (("PHI",),        ("Philadelphia",),             ("Flyers",)),
    "Phoenix Coyotes":      (("PHX", "PHO"),  ("Phoenix",),                  ("Coyotes",)),
    "Pittsburgh Penguins":  (("PIT",),        ("Pittsburgh",),               ("Penguins",)),
    "San Jose Sharks":      (("SJS", "SJ"),   ("SanJose", "San Jose"),       ("Sharks",)),
    "St. Louis Blues":      (("STL",),        ("StLouis", "St Louis"),       ("Blues",)),
    "Tampa Bay Lightning":  (("TBL", "TB"),   ("TampaBay", "Tampa Bay"),     ("Lightning",)),
    "Toronto Maple Leafs":  (("TOR",),        ("Toronto",),                  ("MapleLeafs",)),
    "Vancouver Canucks":    (("VAN",),        ("Vancouver",),                ("Canucks",)),
    "Washington Capitals":  (("WSH", "WAS"),  ("Washington",),               ("Capitals",)),
}

# Nicknames that are also ordinary English and so cannot be trusted on their own.
# "Wild shot", "the kings of the crease", "blues" and "stars" all turn up in 18k lines
# of play-by-play. These only count when the name has no other team evidence AND the
# token stands alone between delimiters in the stem -- ``PA_Wild_Goal`` yes,
# ``PxP_TheyGoWildInTheCrowd`` no.
_WEAK = {"Wild", "Stars", "Kings", "Blues", "Devils", "Capitals", "Lightning",
         "Sharks", "Flames", "Rangers", "Islanders", "Panthers", "Ducks",
         "Senators", "Avalanche"}

# Build lookup tables once. Multi-word spellings ("Los Angeles") are folded to their
# squashed form so a single token match covers both.
_STRONG: dict[str, str] = {}      # lowercase token -> team
_WEAKMAP: dict[str, str] = {}     # lowercase token -> team (capitalisation required)
_CODES: dict[str, str] = {}       # UPPERCASE token -> team

for _team, (_codes, _cities, _nicks) in _ALIASES.items():
    for _c in _codes:
        _CODES[_c] = _team
    for _s in _cities + _nicks:
        _key = _s.replace(" ", "").lower()
        if _s in _WEAK:
            _WEAKMAP[_key] = _team
        else:
            _STRONG[_key] = _team

# Split a stem into candidate tokens: underscores, digits and CamelCase humps all break.
# "Chatter_Str_OnMinnesota_Var4" -> On, Minnesota, Var
# Multi-hump names survive too, because the squashed spellings ("LosAngeles") are kept
# alongside the split ones.
_SPLIT = re.compile(r"[^A-Za-z]+")
_HUMP = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z]*|[a-z]+")


def _tokens(name: str) -> tuple[set[str], set[str]]:
    """(lowercase tokens, lowercase adjacent-pair joins)"""
    low: set[str] = set()
    pairs: set[str] = set()
    for chunk in _SPLIT.split(name):
        if not chunk:
            continue
        humps = _HUMP.findall(chunk)
        # the chunk as a whole matters for squashed spellings like "TampaBay"
        humps.append(chunk)
        for h in humps:
            low.add(h.lower())
        # "New Jersey" / "Los Angeles" arrive as two humps -- join neighbours so the
        # squashed alias table still matches without needing every word order.
        parts = _HUMP.findall(chunk)
        for i in range(len(parts) - 1):
            pairs.add((parts[i] + parts[i + 1]).lower())
    return low, pairs


def team_from_name(name: str) -> str:
    """Team implied by a stream name, or '' when nothing matches unambiguously.

    Only full city / nickname spellings count. Abbreviations are never matched here --
    see the module docstring."""
    if not name:
        return ""
    low, pairs = _tokens(name)
    hits = {_STRONG[t] for t in low | pairs if t in _STRONG}
    if len(hits) == 1:
        return hits.pop()
    if hits:
        return ""                       # two teams named -- e.g. a matchup line
    # nothing strong; fall back to the ambiguous nicknames, standalone token required
    weak = {_WEAKMAP[t] for t in low if t in _WEAKMAP and _standalone(name, t)}
    return weak.pop() if len(weak) == 1 else ""


def _standalone(name: str, token: str) -> bool:
    """Is this token its own word in the stem rather than a syllable of a longer one?

    ``PA_Wild_Goal`` and ``PxP_TS_WildWillTryAndBring`` both start "Wild" at a boundary;
    ``PxP_TheyGoWildInTheCrowd`` buries it mid-word, where it is English, not a team."""
    return bool(re.search(r"(?<![A-Za-z])" + re.escape(token.capitalize()) + r"(?![a-z])", name))


def canon(value: str) -> str:
    """Fold any spelling of a team onto the exact string the Team filter offers.

    The filter combobox is built from ``TEAMS``, and it matches by equality, so a tag
    reading "ANA", "Anaheim" or "Ducks" is invisible to it however correct it is -- and
    the shipped data had accumulated 102 distinct spellings of 30 teams. Anything that
    does not resolve is handed back untouched rather than blanked: it may be a team this
    build has never heard of (an expansion club) or a note the user typed, and losing it
    would be worse than leaving it unfilterable."""
    v = (value or "").strip()
    if not v or v in _ALIASES:
        return v
    if v.upper() in _CODES:
        return _CODES[v.upper()]
    return team_from_name(v) or v


_SLOTS: dict[str, str] | None = None


def _from_slot(akey: str) -> str:
    """Team that owns this stream's slot in its wave bank. '' when it owns none.

    Cached, and a missing data file degrades to the other three sources -- see _from_text
    for why that is deliberate."""
    global _SLOTS
    if _SLOTS is None:
        try:
            try:
                from . import resources
            except ImportError:
                import resources
            import json
            p = resources.data_path("audio_team_slots.json")
            _SLOTS = (json.loads(p.read_text(encoding="utf-8")).get("slots") or {}) \
                if p.exists() else {}
        except Exception:
            _SLOTS = {}
    return _SLOTS.get(akey, "")


_FROM_TEXT: dict[str, str] | None = None


def _from_text(akey: str) -> str:
    """Team the stream SPELLS OUT in its audio, per the offline sweep. '' when unknown.

    Loaded once and cached. A missing data file is not an error -- the column simply
    falls back to what the name and banks say, which is what it did before this source
    existed, so an incomplete install degrades instead of raising."""
    global _FROM_TEXT
    if _FROM_TEXT is None:
        try:
            try:
                from . import resources
            except ImportError:
                import resources                       # running from launcher/ directly
            import json
            p = resources.data_path("audio_team_from_text.json")
            _FROM_TEXT = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        except Exception:
            _FROM_TEXT = {}
    return _FROM_TEXT.get(akey, "")


def team_for(name: str, bank_display: str = "", akey: str = "") -> str:
    """Team for one stream. ``bank_display`` is audio_names.bank_info()'s first value;
    a single-arena reference ('VAN only') outranks anything the name says. ``akey`` is the
    manifest key ('1B:0x0933A000') and is what the transcript table is keyed on -- omit it
    and the transcript source is simply skipped."""
    if akey:
        slot = _from_slot(akey)
        if slot:
            return slot
    if bank_display:
        m = re.match(r"^([A-Z]{2,3}) only$", bank_display.strip())
        if m and m.group(1) in _CODES:
            return _CODES[m.group(1)]
    return team_from_name(name) or (_from_text(akey) if akey else "")
