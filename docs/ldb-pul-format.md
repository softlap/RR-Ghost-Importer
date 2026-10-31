# Retro Rewind (Pulsar) ghost storage - format notes

Everything here was worked out by looking at files the game itself wrote (and checking that code can rebuild them
byte for byte). It is **not** official documentation. Each item is tagged:

- **Verified** - rebuilt a game-written file exactly from a ghost file using this tool's code
- **Likely** - fits every file seen, but not proven
- **Guess** - inferred from sizes only, never seen filled in

Tested on Retro Rewind v6 (Pulsar) run through Wheel Wizard on Windows. Other versions may differ.

## Folder layout (Verified)

```
Pulsar/RetroRewind6/Ghosts/
  <trackid>/            8 lowercase hex digits - the track's folder name
    ldb.pul             the track's leaderboard (see below)
    150/                ghost files for 150cc, named by finish time:  2m06s869.rkg
    200/                ghost files for 200cc
    1/                  a second *version* of the track (same id): its own ldb.pul + 150/ + 200/
```

The pack ships `FolderToTrackNameRT.txt` / `FolderToTrackNameCT.txt` (`Track name = ID` lines) which map folder
names to track names. A second version of a track is not listed there; the game writes the real name into that
version's `ldb.pul` header.

A bare `.rkg` placed in a track folder is **not** shown by the game - it is listed from `ldb.pul`, so a leaderboard
entry is needed as well as the file. (Verified by trial: dropping the file alone did nothing.)

## ldb.pul (always 4544 bytes)

| Offset | Size | Contents | Status |
|---|---|---|---|
| 0x00 | 4 | `PULL` | Verified |
| 0x04 | 4 | `00 00 00 01` (format version) | Verified |
| 0x08 | 4 | track id (same as the folder name; for a version folder it is the parent's id) | Verified |
| 0x0C | to 0x3B | track name, UTF-8, NUL padded | Verified |
| 0x3C | 4 | trophy flags (a community trophy editor seeks here) - all zero in every file seen | Likely |
| 0x40 | 16 | zeros | Verified |
| 0x50 | 1000 | 150cc **total time** table: 10 entries x 100 bytes, fastest first | Verified |
| 0x438 | 1000 | 150cc **best lap** table: 10 entries x 100 bytes | Verified |
| 0x820-0x11C0 | | not seen filled in | - |
| 0x910 / 0xCF8 | 1000 each | 200cc total / lap tables | **Guess** (the file is `0x40 + 2 x 0x8C0` bytes) |

### Entry (100 bytes)

| Offset | Size | Contents | Status |
|---|---|---|---|
| 0 | 74 | the player's Mii, copied from the ghost file (`rkg[0x3C:0x86]`); UTF-16BE name starts at byte 2 | Verified |
| 74 | 2 | Mii CRC16 (`rkg[0x86:0x88]`) | Verified |
| 76 | 4 | CRC32 of the ghost file (its last 4 bytes); zero in the lap table | Verified |
| 80 | 2 | minutes (big-endian) | Verified |
| 82 | 1 | seconds | Verified |
| 83 | 3 | milliseconds (big-endian) | Verified |
| 86 | 1 | `01` = slot in use | Verified |
| 87 | 1 | drift type? (0 in the only sample; tool copies bit 1 of `rkg[0x0D]`) | Guess |
| 88 | 4 x 3 | character, vehicle, controller as big-endian uint32 | Verified |

The lap table uses the same layout with the ghost's best single lap as the time and a zero CRC32.

## Ghost files (.rkg) - the fields this tool reads

| Offset | Contents |
|---|---|
| 0x00 | `RKGD` |
| 0x04 | finish time, 3 bytes: 7 bits minutes, 7 bits seconds, 10 bits milliseconds |
| 0x07 | top 6 bits: the **stock-game slot** the track is built on (0-31) - *not* a unique track id |
| 0x08 | vehicle (6 bits), character (6 bits), date, controller (low 4 bits) |
| 0x10 | lap count; then 3-byte lap times from 0x11 in the same time format |
| 0x3C | Mii (74 bytes) |
| 0x86 | Mii CRC16 |
| end-4 | CRC32 of everything before it |

Retro Rewind ghosts carry **no track id**, which is why the track has to be chosen by hand (or narrowed down).

## Auto-detect (Likely, not exact)

Because a ghost only records the stock slot, the tool lists tracks whose *expert ghost* (in the pack's `ExpertsRT`
folder, named `<n>_150.rkg`) has the same slot. `<n>` is the track's 0-based position in
`FolderToTrackNameRT.txt` (checked on two tracks). Files named `<n>_v1_150.rkg` mark tracks that have a second
version. This only produces a shortlist; many tracks share a slot.

## How these were checked

Take the game's own `ldb.pul`, blank both tables, rebuild them from the matching `.rkg` with `add_ghost()`, and
compare byte for byte. The tool's tests do the equivalent with synthetic ghosts; the real-file comparison was done
by hand during development and is not part of the repository (it needs game files).

## Open questions

- The real 200cc layout, and what lives in 0x820-0x8FF and 0x10E0-0x11BF.
- What the `1` folder means in every case (version of a track vs something else).
- Whether the game checks anything else when loading (it accepted every file the tool produced in testing).
