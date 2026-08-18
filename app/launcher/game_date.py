"""
game_date.py — move the game's calendar off 2009.

Found 2026-08-17, chasing "the game says Gavin McKenna is 1 year old".  He is: his birth date in
Roster.ROS is stored correctly as 2007-12-20, and the game's idea of *today* is hardcoded to
1 October 2009, so it subtracts and gets 1.  Nothing in Roster.ROS carries a current date — the
clock is three `li` immediates in the XEX.

    Date_Pack (0x83D509D0)  packs (year, month, day, hour, minute) into one u32:

        bits 31..25   year - 2000      7 bits, so 2000..2127
        bits 24..21   month            4 bits, ZERO-BASED (the packer rejects month > 0xB)
        bits 20..16   day              5 bits, 1-based, validated against days-in-month
        bits 15.. 5   minute-of-day    11 bits (60*hour + minute)

    Proof the year field is year-2000 and not a raw year: Date_Now (0x83D50A58) reads the real
    system clock and calls the packer with `addi r3, r9, -0x7d0`.  The packer accepts either form —
    anything >= 0x80 gets +0x30 and is truncated to 7 bits, which is the same thing — so these
    sites pass the literal year and we keep that convention.

    GetCurrentDate (0x83FF9F58), 83 callers, is the one that matters:

        83ff9f64  bl   0x83bb1e48       <- ask the franchise/season subsystem for a live date
        83ff9f70  beq  cr6,0x83ff9f88   <- ... nothing there?  fall through to the DEFAULT
        83ff9f88  li   r7,0             <- minute
        83ff9f8c  li   r6,0             <- hour
        83ff9f90  li   r5,1             <- day   1
        83ff9f94  li   r4,9             <- month 9 = OCTOBER (zero-based)
        83ff9f98  li   r3,0x7d9         <- year  2009
        83ff9f9c  bl   0x83d509d0       <- Date_Pack
        83ff9fb0  ...                   <- the live path, reads 0x84B9B148+0x10

    Outside a franchise there is no live date, so every date the front end shows — ages included —
    comes from that default.  Two more sites carry the same calendar:

        0x83FFA000  GetSeasonEndDate, same shape, defaults to 2010-04-13 (the 2009-10 regular
                    season ended 11 April 2010)
        0x83FFCC10  the subsystem's init, which packs 2009-10-01 again and hands it to 0x83FF9FD0

    So the whole feature is three `li r3,<year>` (plus the month/day pair next to each) and the
    patch is 4 bytes per field.

Version safety: Title Update #1 is a full relink, so these v1.0 VAs mean nothing on a TU build.
read() and write() verify the `bl Date_Pack` that follows each site and refuse to touch a file
whose fingerprint is off — see project_title_update_analysis.
"""
from __future__ import annotations
import struct

from . import xex_patch

# Each site is the `li r3,<year>` instruction.  month is at -4, day at -8, hour -12, minute -16.
CURRENT_VA = 0x83FF9F98        # GetCurrentDate's default  -> "today"
SEASON_END_VA = 0x83FFA040     # GetSeasonEndDate's default
INIT_VA = 0x83FFCC7C           # the init that seeds the current date

SITES = {"current": CURRENT_VA, "season_end": SEASON_END_VA, "init": INIT_VA}

# The `bl 0x83D509D0` (Date_Pack) immediately after each `li r3` — our version fingerprint.
_GUARD = {CURRENT_VA + 4: 0x4BD56A35,
          SEASON_END_VA + 4: 0x4BD5698D,
          INIT_VA + 4: 0x4BD53D51}

STOCK = {"current": (2009, 10, 1), "season_end": (2010, 4, 13), "init": (2009, 10, 1)}

_LI_R3, _LI_R4, _LI_R5 = 0x38600000, 0x38800000, 0x38A00000   # addi rD,0,imm
_LI_MASK = 0xFFFF0000

DAYS = (31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)       # 29: leap years are the game's problem


class DateError(RuntimeError):
    pass


def _read_word(xex, va):
    w = xex_patch.read_u32(xex, va)
    if w is None:
        raise DateError(f"VA 0x{va:X} is not in the XEX image — wrong file, or a Title Update build")
    return w


def _check(xex):
    """Refuse to read or write an image whose Date_Pack calls are not where v1.0 puts them."""
    for va, want in _GUARD.items():
        got = _read_word(xex, va)
        if got != want:
            raise DateError(f"XEX fingerprint mismatch at 0x{va:X}: expected bl Date_Pack "
                            f"({want:08x}), found {got:08x}. This is not a stock v1.0 default.xex.")


def read(xex) -> dict[str, tuple[int, int, int]]:
    """{site: (year, month, day)} with month 1-12, i.e. how a human writes it."""
    xex = str(xex)
    _check(xex)
    out = {}
    for name, va in SITES.items():
        year = _read_word(xex, va) & 0xFFFF
        month = _read_word(xex, va - 4) & 0xFFFF
        day = _read_word(xex, va - 8) & 0xFFFF
        if year < 0x80:                       # the packer's other accepted form
            year += 2000
        out[name] = (year, month + 1, day)    # month is zero-based in the instruction
    return out


def _validate(name, ymd):
    year, month, day = ymd
    if not 2000 <= year <= 2127:
        raise DateError(f"{name}: year {year} is outside the 7-bit field's range (2000-2127)")
    if not 1 <= month <= 12:
        raise DateError(f"{name}: month {month} is not 1-12")
    if not 1 <= day <= DAYS[month - 1]:
        raise DateError(f"{name}: day {day} is not valid for month {month}")


def write(xex, dates: dict[str, tuple[int, int, int]], log=print) -> dict:
    """Patch one or more sites. `dates` maps a name from SITES to (year, month, day), month 1-12.

    Writes the year, month and day words for each named site; hour and minute are left at zero,
    which is what every site ships with."""
    xex = str(xex)
    _check(xex)
    unknown = set(dates) - set(SITES)
    if unknown:
        raise DateError(f"unknown site(s): {', '.join(sorted(unknown))}")
    for name, ymd in dates.items():
        _validate(name, ymd)
    done = {}
    for name, (year, month, day) in dates.items():
        va = SITES[name]
        for off, base, val in ((0, _LI_R3, year), (-4, _LI_R4, month - 1), (-8, _LI_R5, day)):
            cur = _read_word(xex, va + off)
            if cur & _LI_MASK != base:
                raise DateError(f"{name}: 0x{va + off:X} is not the `li` we expect ({cur:08x})")
            xex_patch.patch_va(xex, va + off, struct.pack(">I", base | (val & 0xFFFF)),
                               expect=struct.pack(">I", cur), log=lambda *_: None)
        done[name] = (year, month, day)
        log(f"  {name}: {year}-{month:02d}-{day:02d}")
    return done


def set_season(xex, start_year: int, log=print) -> dict:
    """Move the whole calendar to a season starting in `start_year`, keeping the shipped
    October 1 / April 13 shape: today = start_year-10-01, season end = start_year+1-04-13."""
    return write(xex, season_dates(start_year), log=log)


def season_dates(start_year: int) -> dict[str, tuple[int, int, int]]:
    """What set_season(start_year) would write — the shipped Oct 1 / Apr 13 shape. Split out so
    a caller can compare read() against the wanted state without writing anything."""
    return {"current": (start_year, 10, 1),
            "init": (start_year, 10, 1),
            "season_end": (start_year + 1, 4, 13)}


def season_year(xex) -> int | None:
    """The season start year the XEX currently encodes, or None if the three sites disagree
    (hand-edited to something that isn't a plain season)."""
    cur = read(xex)
    for y in {cur["current"][0], cur["init"][0]}:
        if cur == season_dates(y):
            return y
    return None


def revert(xex, log=print) -> dict:
    """Put the shipped 2009-10 calendar back (today 2009-10-01, season end 2010-04-13)."""
    return write(xex, dict(STOCK), log=log)


if __name__ == "__main__":
    XEX = r"C:\Users\cloug\Documents\NHL 2k10 Extracted\default.xex"
    for name, ymd in read(XEX).items():
        print(f"  {name:11s} {ymd[0]}-{ymd[1]:02d}-{ymd[2]:02d}")
