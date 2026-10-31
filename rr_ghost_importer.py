#!/usr/bin/env python3
"""Retro Rewind Ghost Importer

Add downloaded .rkg ghost files to Retro Rewind v6 (Pulsar) time trials.
A fan-made tool - not affiliated with Retro Rewind, Wheel Wizard or Nintendo.

Run:     python rr_ghost_importer.py        (use the .pyw copy, or the exe, to avoid a console window)
Options: --ghosts-dir PATH  --no-window  --port N  --version
Needs only Python 3.8+. Optional: `pip install pywebview` for a native app window instead of an Edge/Chrome one.
File-format notes: see docs/ldb-pul-format.md in the repository.
"""
__version__ = "0.1.0"

# ---- core: Retro Rewind (Pulsar) leaderboard + ghost files --------------------------------------
# Verified against a game-made ghost + ldb.pul (Mushroom Gorge, 150cc):
#   <Ghosts>/<trackid lowercase>/ldb.pul   size 0x11C0, header 'PULL' + version + track id + name
#   <Ghosts>/<trackid>/150/<M>m<SS>s<mmm>.rkg   the ghost file, named by its finish time
#   ldb.pul: 10 'total time' entries at 0x50 and 10 'best lap' entries at 0x438, 100 bytes each:
#     Mii(0x4A, = rkg[0x3C:0x86]) | Mii CRC16 (rkg[0x86:0x88]) | file CRC32 (last 4 bytes of rkg,
#     zero for lap entries) | time: u16 min, u8 sec, u24 ms | 01 | drift byte | u32 char | u32 vehicle | u32 controller
import struct, zlib, os, re, shutil, time as _time

LDB_SIZE = 0x11C0
TOTAL_OFF = 0x50
LAP_OFF = 0x438
# (total-time table, best-lap table) per engine class. 150cc is verified against the game's own files.
# 200cc is a GUESS from the file size (two blocks of 0x8C0 bytes after the 0x40 header) - not yet verified.
CLASS_OFFS = {150: (0x50, 0x438), 200: (0x910, 0xCF8)}
ENTRY = 100
SLOTS = 10


class RRError(Exception):
    pass


def _read(path, n=-1):
    with open(path, 'rb') as f:
        return f.read(n)


def _write(path, data):
    with open(path, 'wb') as f:
        f.write(data)


def parse_rkg(g):
    if len(g) < 0x90 or g[:4] != b'RKGD':
        raise RRError('Not a Mario Kart Wii ghost (.rkg) file')
    t = int.from_bytes(g[4:7], 'big')
    total = ((t >> 17) & 0x7F, (t >> 10) & 0x7F, t & 0x3FF)
    w = int.from_bytes(g[8:12], 'big')
    laps = []
    for i in range(min(g[0x10], 9)):
        b = int.from_bytes(g[0x11 + 3 * i:0x14 + 3 * i], 'big')
        laps.append(((b >> 17) & 0x7F, (b >> 10) & 0x7F, b & 0x3FF))
    if not laps:
        raise RRError('Ghost has no lap data')
    if struct.pack('>I', zlib.crc32(g[:-4])) != g[-4:]:
        raise RRError('Ghost file checksum is wrong (file damaged?)')
    return {
        'total': total, 'best_lap': min(laps),
        'mii': g[0x3C:0x86], 'crc16': g[0x86:0x88], 'crc32': g[-4:],
        'veh': w >> 26, 'char': (w >> 20) & 0x3F, 'ctrl': w & 0xF,
        'drift': (g[0xD] >> 1) & 1,
    }


def time_name(t):
    return f'{t[0]}m{t[1]:02d}s{t[2]:03d}'


def make_entry(info, t, with_crc32):
    e = bytearray()
    e += info['mii'] + info['crc16']
    e += info['crc32'] if with_crc32 else b'\0\0\0\0'
    e += t[0].to_bytes(2, 'big') + bytes([t[1]]) + t[2].to_bytes(3, 'big')
    e += bytes([1, info['drift']])
    e += struct.pack('>III', info['char'], info['veh'], info['ctrl'])
    assert len(e) == ENTRY
    return bytes(e)


def entry_time(e):
    return (int.from_bytes(e[80:82], 'big'), e[82], int.from_bytes(e[83:86], 'big'))


def read_table(buf, off):
    out = []
    for i in range(SLOTS):
        e = bytes(buf[off + i * ENTRY: off + (i + 1) * ENTRY])
        if e[86] == 1:
            out.append(e)
    return out


def write_table(buf, off, entries):
    entries = sorted(entries, key=entry_time)[:SLOTS]
    for i in range(SLOTS):
        e = entries[i] if i < len(entries) else b'\0' * ENTRY
        buf[off + i * ENTRY: off + (i + 1) * ENTRY] = e


def new_ldb(track_id, name):
    """A blank leaderboard exactly like the one the game creates: 'PULL', version 1, track id, name, zeros."""
    b = bytearray(LDB_SIZE)
    b[0:4] = b'PULL'
    b[4:8] = (1).to_bytes(4, 'big')
    b[8:12] = bytes.fromhex(track_id)
    nm = name.encode('utf-8')[:0x2F]
    b[12:12 + len(nm)] = nm
    return bytes(b)


def check_ldb(ldb, track_id, strict=True):
    if len(ldb) != LDB_SIZE:
        raise RRError(f'Unexpected ldb.pul size ({len(ldb)}); refusing to edit')
    if ldb[:4] != b'PULL':
        raise RRError('ldb.pul header not recognised')
    if strict and ldb[8:12] != bytes.fromhex(track_id):
        raise RRError('ldb.pul belongs to a different track than the folder name says')


def check_tables(buf, cc):
    """Refuse to write into a table that doesn't look like entries (guards the unverified 200cc layout)."""
    for off in CLASS_OFFS[cc]:
        for i in range(SLOTS):
            e = bytes(buf[off + i * ENTRY: off + (i + 1) * ENTRY])
            if not any(e):
                continue
            mn, sc, ms = entry_time(e)
            if e[86] != 1 or mn > 59 or sc > 59 or ms > 999:
                raise RRError(f'The {cc}cc part of this leaderboard does not look like I expected - not touching it')


def add_ghost(ldb, rkg, track_id, strict=True, cc=150):
    """Returns (new_ldb, message). Raises RRError if it should not be added."""
    check_ldb(ldb, track_id, strict)
    info = parse_rkg(rkg)
    buf = bytearray(ldb)
    check_tables(buf, cc)
    t_off, l_off = CLASS_OFFS[cc]
    total = read_table(buf, t_off)
    for e in total:
        if e[76:80] == info['crc32'] and entry_time(e) == info['total']:
            raise RRError('already in the leaderboard')
    new_total = make_entry(info, info['total'], True)
    if len(total) >= SLOTS and entry_time(new_total) >= entry_time(sorted(total, key=entry_time)[-1]):
        raise RRError('slower than everything in the (full) top 10')
    write_table(buf, t_off, total + [new_total])
    laps = read_table(buf, l_off)
    new_lap = make_entry(info, info['best_lap'], False)
    if not any(entry_time(e) == info['best_lap'] and e[:74] == info['mii'] for e in laps):
        if len(laps) < SLOTS or entry_time(new_lap) < entry_time(sorted(laps, key=entry_time)[-1]):
            write_table(buf, l_off, laps + [new_lap])
    return bytes(buf), time_name(info['total'])


def import_into_track(ghosts_dir, track_id, rkgs, track_name=None, version=0, cc=150):
    """rkgs: list of (name, bytes). Works on <ghosts_dir>/<track_id>[/<version>]/ldb.pul. Versions >= 1 are the
    other tracks that share an ID (e.g. SW2 Airship Fortress lives in <DS Airship Fortress id>/1).
    A missing leaderboard (base or versioned) is created from the id and name, in the same format the game writes
    (verified against a game-made version folder: header is identical apart from the track name).
    Returns (report, backup_path_or_None, created_new_ldb)."""
    track_id = track_id.lower()
    tdir = os.path.join(ghosts_dir, track_id, str(version)) if version else os.path.join(ghosts_dir, track_id)
    ldb_path = os.path.join(tdir, 'ldb.pul')
    strict = not version
    created = False
    if os.path.isfile(ldb_path):
        ldb = _read(ldb_path)
    else:
        if not track_name:
            raise RRError('This track has no leaderboard yet and no track name was given to create one')
        ldb = new_ldb(track_id, track_name)
        created = True
    check_ldb(ldb, track_id, strict)
    report, current, wrote = [], ldb, []
    for name, rkg in rkgs:
        try:
            current, tname = add_ghost(current, rkg, track_id, strict, cc)
        except RRError as e:
            report.append((name, f'SKIPPED: {e}')); continue
        dest_dir = os.path.join(tdir, str(cc))
        dest = os.path.join(dest_dir, tname + '.rkg')
        wrote.append((dest_dir, dest, rkg))
        report.append((name, f'OK: {tname} added to the {cc}cc leaderboard'))
    if not wrote:
        return report, None, False
    stamp = _time.strftime('%Y%m%d-%H%M%S')
    backup = None
    os.makedirs(tdir, exist_ok=True)
    if not created:
        backup = f'{ldb_path}.backup-{stamp}'
        if not os.path.exists(backup):        # keep the earliest copy: it is the state from before this run
            shutil.copy2(ldb_path, backup)
    for dest_dir, dest, rkg in wrote:
        os.makedirs(dest_dir, exist_ok=True)
        if os.path.exists(dest) and _read(dest) != rkg and not os.path.exists(dest + f'.backup-{stamp}'):
            shutil.copy2(dest, dest + f'.backup-{stamp}')
        _write(dest, rkg)
    tmp = ldb_path + '.tmp'
    _write(tmp, current)
    os.replace(tmp, ldb_path)
    return report, backup, created


CTRL_NAMES = {0: 'Wii Wheel', 1: 'Nunchuk', 2: 'Classic', 3: 'GameCube'}


def mii_name(mii):
    s = mii[2:22].decode('utf-16-be', 'ignore').split('\0')[0]
    return ''.join(ch for ch in s if not ('\ue000' <= ch <= '\uf8ff'))  # drop Wii-only glyphs that don't render


def preview_import(ghosts_dir, track_id, rkgs, track_name=None, version=0, cc=150):
    """Dry run of import_into_track: nothing is written. Returns a dict with the target folder, whether a new
    leaderboard would be created, one row per ghost (what would happen) and the 150cc top 10 afterwards."""
    track_id = track_id.lower()
    tdir = os.path.join(ghosts_dir, track_id, str(version)) if version else os.path.join(ghosts_dir, track_id)
    ldb_path = os.path.join(tdir, 'ldb.pul')
    exists = os.path.isfile(ldb_path)
    if exists:
        ldb = _read(ldb_path)
    else:
        if not track_name:
            raise RRError('This track has no leaderboard yet and no track name was given to create one')
        ldb = new_ldb(track_id, track_name)
    strict = not version
    check_ldb(ldb, track_id, strict)
    t_off = CLASS_OFFS[cc][0]
    current, rows, new_ids = ldb, [], set()
    for name, rkg in rkgs:
        row = {'file': name, 'time': '?', 'player': '', 'controller': '', 'ok': False, 'note': ''}
        try:
            info = parse_rkg(rkg)
            row.update(time=time_name(info['total']), player=mii_name(info['mii']),
                       controller=CTRL_NAMES.get(info['ctrl'], str(info['ctrl'])))
            current, tname = add_ghost(current, rkg, track_id, strict, cc)
        except RRError as e:
            row['note'] = f'will be skipped: {e}'
            rows.append(row); continue
        dest = os.path.join(tdir, str(cc), tname + '.rkg')
        row.update(ok=True, key=(info['crc32'], info['total']),
                   replaces=' (replaces a different file with the same time name)' if os.path.exists(dest) and _read(dest) != rkg else '')
        new_ids.add((info['crc32'], info['total']))
        rows.append(row)
    final = sorted(read_table(bytearray(current), t_off), key=entry_time)
    for r in rows:
        if r['ok']:
            rank = next(i for i, e in enumerate(final, 1) if (e[76:80], entry_time(e)) == r['key'])
            r['note'] = f"will be added as #{rank} of {len(final)}" + r['replaces']
    after = []
    for e in sorted(read_table(bytearray(current), t_off), key=entry_time):
        after.append((time_name(entry_time(e)), mii_name(e[:74]), (e[76:80], entry_time(e)) in new_ids))
    return {'folder': tdir, 'new_leaderboard': not exists, 'rows': rows, 'after': after}


# ---- track list ------------------------------------------------------------------------------
def _strip(n):
    return re.sub(r'\\c\{[^}]*\}', '', n).strip()


LOOKUP_FILES = ('FolderToTrackNameRT.txt', 'FolderToTrackNameCT.txt', 'FolderToTrackName.txt')


def _parse_lookup(path):
    out = []
    try:
        with open(path, encoding='utf-8', errors='ignore') as f:
            for line in f:
                m = re.match(r'(.*)=\s*([0-9A-Fa-f]{8})\s*$', line.strip())
                if m:
                    out.append((m.group(2).lower(), _strip(m.group(1))))
    except OSError:
        pass
    return out


def lookup_dirs(ghosts_dir=None):
    """Folders that hold the pack's FolderToTrackName*.txt files (they ship with Retro Rewind)."""
    cands = []
    app = os.environ.get('APPDATA', '')
    cands.append(os.path.join(app, 'CT-MKWII', 'RetroRewind', 'RetroRewind6', 'Ghosts'))
    if ghosts_dir:
        cands += [ghosts_dir, os.path.dirname(ghosts_dir)]
    root = os.path.join(app, 'CT-MKWII')
    if app and os.path.isdir(root):
        base = root.rstrip(os.sep).count(os.sep)
        for dp, dn, fn in os.walk(root):
            if dp.count(os.sep) - base > 6:
                dn[:] = []
                continue
            if any(f in fn for f in LOOKUP_FILES):
                cands.append(dp)
    seen, out = set(), []
    for c in cands:
        if c and c not in seen and any(os.path.isfile(os.path.join(c, f)) for f in LOOKUP_FILES):
            seen.add(c); out.append(c)
    return out


def load_tracks(dirs=()):
    """track id (lowercase hex) -> name, read from the pack's lookup files."""
    tracks = {}
    for d in dirs:
        for fn in LOOKUP_FILES:
            for k, v in _parse_lookup(os.path.join(d, fn)):
                tracks[k] = v
    return tracks


def _read_name(path):
    try:
        h = _read(path, 0x40)
        if h[:4] == b'PULL':
            return h[12:0x3C].split(b'\0')[0].decode('utf-8', 'ignore').strip()
    except OSError:
        pass
    return ''


def scan_known_tracks(ghosts_dir):
    """Keys 'id' or 'id/version' -> name, read from the ldb.pul files the game created itself."""
    out = {}
    if not ghosts_dir or not os.path.isdir(ghosts_dir):
        return out
    for d in os.listdir(ghosts_dir):
        if not re.fullmatch(r'[0-9a-fA-F]{8}', d):
            continue
        base = os.path.join(ghosts_dir, d)
        nm = _read_name(os.path.join(base, 'ldb.pul'))
        if nm:
            out[d.lower()] = nm
        for v in os.listdir(base):
            if v.isdigit() and os.path.isdir(os.path.join(base, v)):
                nm = _read_name(os.path.join(base, v, 'ldb.pul'))
                if nm:
                    out[f'{d.lower()}/{v}'] = nm
    return out


def find_ghost_dirs():
    """Find <...>/Pulsar/RetroRewind6/Ghosts folders (where the game saves your ghosts)."""
    home = os.path.expanduser('~')
    roots = [os.path.join(os.environ.get('APPDATA', ''), 'CT-MKWII'),
             os.path.join(os.environ.get('APPDATA', ''), 'Dolphin Emulator'),
             os.path.join(home, 'Documents', 'Dolphin Emulator'),
             os.path.join(home, '.local', 'share', 'dolphin-emu')]
    found = []
    for r in roots:
        if not r or not os.path.isdir(r):
            continue
        base_depth = r.rstrip(os.sep).count(os.sep)
        for dp, dn, _ in os.walk(r):
            if dp.count(os.sep) - base_depth > 9:
                dn[:] = []
                continue
            if os.path.basename(dp) == 'Ghosts' and os.path.basename(os.path.dirname(dp)).startswith('RetroRewind') \
                    and os.path.basename(os.path.dirname(os.path.dirname(dp))) == 'Pulsar':
                found.append(dp)
                dn[:] = []
    return found


def rt_order(dirs=()):
    """[(id, name), ...] in the order of FolderToTrackNameRT.txt (the expert ghosts are numbered by this order)."""
    for d in dirs:
        out = _parse_lookup(os.path.join(d, 'FolderToTrackNameRT.txt'))
        if out:
            return out
    return []


def find_versions(experts_dir):
    """{track number: {versions}} from ExpertsRT file names like 83_v1_150.rkg (a second version of a track)."""
    out = {}
    if experts_dir and os.path.isdir(experts_dir):
        for f in os.listdir(experts_dir):
            m = re.fullmatch(r'(\d+)_v(\d+)_\d+\.rkg', f)
            if m:
                out.setdefault(int(m.group(1)), set()).add(int(m.group(2)))
    return out


def versioned_tracks(rt_dirs, experts_dir):
    order = rt_order(rt_dirs)
    out = {}
    for idx, vs in find_versions(experts_dir).items():
        if idx < len(order):
            tid, name = order[idx]
            for v in sorted(vs):
                out[f'{tid}/{v}'] = f'{name} - newer version {v}'
    return out



HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Retro Rewind Ghost Importer</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#10131a">
<meta name="color-scheme" content="dark">
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 256 256'%3E%3Cdefs%3E%3ClinearGradient id='bg' x1='0' y1='0' x2='1' y2='1'%3E%3Cstop offset='0' stop-color='#1b2030'/%3E%3Cstop offset='1' stop-color='#0b0d12'/%3E%3C/linearGradient%3E%3ClinearGradient id='g' x1='0.15' y1='0' x2='0.85' y2='1'%3E%3Cstop offset='0' stop-color='#ff7a3d'/%3E%3Cstop offset='1' stop-color='#ff3d78'/%3E%3C/linearGradient%3E%3C/defs%3E%3Crect width='256' height='256' rx='58' fill='url(#bg)'/%3E%3Cpath fill='url(#g)' d='M64 190 V118 C64 83 92 58 128 58 C164 58 192 83 192 118 V190 L170 170 L149 190 L128 170 L107 190 L86 170 Z'/%3E%3Cellipse cx='106' cy='116' rx='12' ry='16' fill='#10131a'/%3E%3Cellipse cx='150' cy='116' rx='12' ry='16' fill='#10131a'/%3E%3Ccircle cx='110' cy='110' r='4.5' fill='#fff'/%3E%3Ccircle cx='154' cy='110' r='4.5' fill='#fff'/%3E%3Cg stroke='#ff6a5a' stroke-width='9' stroke-linecap='round' opacity='.6'%3E%3Cpath d='M30 104 H50'/%3E%3Cpath d='M24 128 H50'/%3E%3Cpath d='M34 152 H50'/%3E%3C/g%3E%3C/svg%3E">
<style>
:root{color-scheme:dark;--bg:#10131a;--card:#1a1e28;--card2:#262c3b;--line:#323a4f;--tx:#eef0f6;--dim:#8d95aa;--acc:#ff5436;--acc2:#ff6e54;--good:#4cd18b;--warn:#f0b94a;--bad:#ff6b6b}
*{box-sizing:border-box}
*{scrollbar-width:thin;scrollbar-color:#3a4259 transparent}
::-webkit-scrollbar{width:10px;height:10px}::-webkit-scrollbar-thumb{background:#3a4259;border:2px solid transparent;background-clip:content-box;border-radius:8px}::-webkit-scrollbar-thumb:hover{background:#4b5575;background-clip:content-box}::-webkit-scrollbar-track,::-webkit-scrollbar-corner{background:transparent}
body{user-select:none;-webkit-user-select:none;cursor:default;overscroll-behavior:none}
input,.log,.box{user-select:text;-webkit-user-select:text}
img,a{-webkit-user-drag:none}
html{font-size:clamp(13px,calc(.62vw + 7.4px),19px)}
html,body{height:100%;margin:0;background:var(--bg);color:var(--tx);font-family:"Segoe UI","Segoe UI Variable",system-ui,-apple-system,Roboto,sans-serif;line-height:1.4;font-size:1rem}
.app{height:100vh;height:100dvh;display:flex;flex-direction:column;padding:1rem 1.2rem;gap:.7rem;min-width:0}
.top{display:flex;align-items:baseline;gap:1rem;flex:none}
h1{font-size:1.65rem;margin:0;font-weight:700;white-space:nowrap}
.sub{color:var(--dim);font-size:.9rem}
.cols{flex:1;min-height:0;display:grid;grid-template-columns:minmax(17rem,25rem) minmax(0,1fr);gap:1rem}
.left{display:flex;flex-direction:column;gap:.7rem;min-height:0;overflow:auto;padding-right:2px}
.right{display:flex;flex-direction:column;gap:.7rem;min-height:0;min-width:0}
.card{background:var(--card);border:1px solid var(--line);flex:none}
.card.grow{flex:1;min-height:0;display:flex;flex-direction:column}
.ch{display:flex;align-items:center;gap:.6rem;padding:.7rem .9rem .3rem;flex:none}
.num{background:var(--acc);color:#fff;font-weight:700;padding:.05rem .55rem;font-size:.85rem}
.ct{font-weight:700;font-size:.95rem}
.cr{margin-left:auto;display:flex;gap:1rem;align-items:center;font-size:.82rem;color:var(--dim)}
.cr .good{color:var(--good)}
.cb{padding:.25rem .9rem .85rem}
.card.grow .cb{flex:1;min-height:0;display:flex;flex-direction:column}
.flat{display:flex;gap:.5rem}
.box{flex:1;min-width:0;background:var(--card2);padding:.5rem .7rem;font-size:.78rem;line-height:1.3;word-break:break-all;max-height:3.6rem;overflow:auto}
.btn{background:var(--card2);color:var(--tx);border:0;padding:.6rem 1.1rem;font:600 .85rem "Segoe UI",system-ui,sans-serif;cursor:pointer}
.btn:hover{background:#303851}
.btn.pri{background:var(--acc);color:#fff}
.btn.pri:hover{background:var(--acc2)}
.btn.big{font-size:1rem;padding:.75rem 1.4rem;flex:1}
input[type=text]{width:100%;background:var(--card2);color:var(--tx);border:1px solid transparent;padding:.6rem .7rem;font:inherit;font-size:.9rem;outline:none}
input[type=text]:focus{border-color:var(--acc)}
.hint{color:var(--dim);font-size:.78rem;margin-top:.45rem}
.link{color:var(--dim);font-size:.8rem;cursor:pointer;margin-top:.5rem;display:inline-block}
.link:hover{color:var(--tx)}
.row{display:flex;gap:.6rem;align-items:center;flex-wrap:wrap}
.cc{display:flex;gap:.5rem;align-items:center;margin-top:.7rem;font-size:.8rem;color:var(--dim);flex-wrap:wrap}
.chip{background:var(--card2);color:var(--dim);border:1px solid var(--line);padding:.3rem .85rem;border-radius:999px;font:600 .8rem "Segoe UI",system-ui,sans-serif;cursor:pointer;user-select:none}
.chip:hover{color:var(--tx)}
.chip.on{background:rgba(255,84,54,.2);border-color:rgba(255,84,54,.7);color:#fff}
.exp{color:var(--warn);font-size:.62rem;text-transform:uppercase;letter-spacing:.04rem;margin-left:.4rem}
.drop{background:var(--card2);text-align:center;padding:1.1rem .6rem;cursor:pointer;border:2px dashed var(--line);transition:.15s;flex:none;display:flex;flex-direction:column;justify-content:center}
.drop:hover,.drop.over{border-color:var(--acc);background:#2a3042}
.drop b{display:block;font-size:1rem}
.drop span{color:var(--dim);font-size:.82rem}
.drop.empty{flex:1;min-height:6rem}
.drop.compact{padding:.5rem .6rem;flex-direction:row;gap:.6rem;align-items:center;justify-content:center}
.drop.compact b{font-size:.88rem}
.tools{display:flex;gap:.6rem;align-items:center;margin-top:.6rem;flex:none;flex-wrap:wrap}
.tools.hidden{display:none}
.tools .allwrap{width:min(13rem,100%)}
.tools .chip{white-space:nowrap}
.tools .allwrap input{padding:.4rem .6rem;font-size:.85rem}
.grow{flex:1}
.tablewrap{flex:1;min-height:0;overflow:auto;margin-top:.6rem}
.tablewrap.hidden{display:none}
table{width:100%;border-collapse:collapse;font-size:.88rem}
th{color:var(--dim);font-weight:400;font-size:.78rem;text-align:left;padding:.4rem .6rem;background:var(--card);position:sticky;top:0;z-index:2}
td{padding:.3rem .6rem;background:var(--card2);border-top:1px solid #1d2230;white-space:nowrap}
tr.sel td{background:#3a4259}
tr.bad td{color:var(--bad)}
td.tk{padding:.15rem .3rem;width:36%}
td.tk input{background:transparent;padding:.35rem .5rem;font-size:.85rem}
td.tk input:hover{background:#303851}
td.tk input.auto{color:#9ff0c9}
td.tk input.none{color:var(--warn)}
td.x{width:2rem;text-align:center;color:var(--dim);cursor:pointer;font-size:1.1rem;padding:0}
td.x:hover{color:var(--bad)}
.sm{font-size:.78rem;color:var(--dim)}
.sm a{color:var(--dim);cursor:pointer;margin-left:.9rem}
.sm a:hover{color:var(--tx)}
.btns{display:flex;gap:.7rem;margin-top:auto;flex:none;padding-top:.3rem}
.log{background:#0c0e13;border:1px solid var(--line);padding:.6rem .8rem;font:.78rem/1.5 "Cascadia Mono",Consolas,"DejaVu Sans Mono",monospace;flex:0 0 32%;min-height:6rem;overflow:auto;white-space:pre-wrap;word-break:break-word}
.log .ok{color:var(--good)}.log .warn{color:var(--warn)}.log .err{color:var(--bad)}.log .dim{color:var(--dim)}.log .acc{color:var(--acc)}.log .head{font-weight:700;color:var(--tx)}
.foot{color:var(--dim);font-size:.72rem;text-align:center;flex:none}
.banner{display:none;padding:.6rem .8rem;border:1px solid rgba(240,185,74,.4);background:rgba(240,185,74,.08);color:#ffdb8a;font-size:.85rem;flex:none}
.banner.show{display:block}
.banner input{margin-top:.4rem}
.menu{position:fixed;z-index:50;background:#151a26;border:1px solid #3a4566;box-shadow:0 18px 40px rgba(0,0,0,.55);max-height:300px;overflow:auto;padding:4px;display:none}
.menu .it{padding:.4rem .6rem;cursor:pointer;display:flex;gap:.5rem;align-items:center;font-size:.88rem}
.menu .it.act,.menu .it:hover{background:#2c3550}
.menu .sec{font-size:.64rem;text-transform:uppercase;letter-spacing:.06rem;color:var(--dim);padding:.45rem .6rem .1rem}
.tag{font-size:.62rem;font-weight:700;letter-spacing:.03rem;text-transform:uppercase;padding:.05rem .4rem;background:var(--card2);color:var(--dim);margin-left:auto;flex:none}
.tag.game{background:rgba(76,209,139,.16);color:var(--good)}
.tag.newer{background:rgba(240,185,74,.14);color:var(--warn)}
.toast{position:fixed;left:50%;bottom:1.4rem;transform:translateX(-50%) translateY(14px);background:#1b2236;border:1px solid #39446a;padding:.6rem 1.1rem;opacity:0;pointer-events:none;transition:.22s;z-index:80;font-weight:600}
.toast.show{opacity:1;transform:translateX(-50%)}
.sumcard{flex:1;min-height:5rem;display:flex;flex-direction:column}
.sumcard .cb{flex:1;min-height:0;overflow:auto}
.stats{display:flex;gap:.5rem;margin-bottom:.7rem}
.stat{flex:1;background:var(--card2);padding:.5rem .6rem;text-align:center}
.stat b{display:block;font-size:1.35rem;line-height:1.1}
.stat span{font-size:.7rem;color:var(--dim);text-transform:uppercase;letter-spacing:.04rem}
.grp{display:flex;gap:.5rem;align-items:baseline;padding:.3rem 0;border-top:1px solid #232a3b;font-size:.85rem}
.grp:first-of-type{border-top:0}
.grp .n{margin-left:auto;color:var(--dim);white-space:nowrap}
.grp.miss{color:var(--warn)}
.sumline{font-size:.8rem;margin-top:.6rem;color:var(--dim)}
.sumline.ok{color:var(--good)}.sumline.warn{color:var(--warn)}
.pillst{font-size:.72rem;font-weight:700;padding:.1rem .55rem;border-radius:999px}
.pillst.ok{background:rgba(76,209,139,.16);color:var(--good)}.pillst.warn{background:rgba(240,185,74,.14);color:var(--warn)}.pillst.idle{background:var(--card2);color:var(--dim)}
/* narrow windows / phones: stack the columns and let the page scroll */
@media (max-width:860px){
  html,body{height:auto}
  .app{height:auto;min-height:100vh}
  .cols{grid-template-columns:1fr}
  .left{overflow:visible}
  .right{min-height:auto}
  .card.grow{flex:none}
  .sumcard{flex:none}
  .tablewrap{max-height:18rem}
  .log{flex:none;height:13rem}
  h1{font-size:1.35rem;white-space:normal}
  .top{flex-direction:column;gap:.1rem}
}
/* short windows: tighten up */
@media (max-height:620px){.app{padding:.6rem .8rem;gap:.5rem}.sub{display:none}.cb{padding-bottom:.6rem}.hint{display:none}}
</style>
</head>
<body>
<div class="app">
  <div class="top"><h1>Retro Rewind Ghost Importer</h1><div class="sub">Drop downloaded ghosts into your time trials - with a preview first.</div></div>
  <div class="banner" id="banner"><b>Couldn't find your Ghosts folder.</b> Paste the full path to <i>Pulsar\RetroRewind6\Ghosts</i> and press Enter.
    <input type="text" id="dirInput" placeholder="C:\Users\...\Pulsar\RetroRewind6\Ghosts" spellcheck="false"></div>

  <div class="cols">
    <aside class="left">
      <div class="card"><div class="ch"><span class="num">1</span><span class="ct">Ghosts folder</span><span class="cr"><span class="good" id="trackCount"></span></span></div>
        <div class="cb"><div class="flat"><div class="box" id="dirText">Not found yet</div><button class="btn" id="changeDir">Browse</button></div><div class="hint">Drop your ghosts, then pick a track for each in the table. Ghosts that only fit one track are filled in for you.</div></div></div>

      <div class="card sumcard"><div class="ch"><span class="ct">Summary</span><span class="cr" id="sumState"></span></div><div class="cb" id="sum"></div></div>

      <div class="btns"><button class="btn big" id="btnPreview">Preview</button><button class="btn pri big" id="btnImport">Import ghosts</button></div>
      <div class="foot">A backup of the leaderboard is made before every import &middot; <span id="ver"></span> beta &middot; fan-made, not affiliated with Retro Rewind</div>
    </aside>

    <section class="right">
      <div class="card grow"><div class="ch"><span class="num">2</span><span class="ct">Ghosts to import</span><span class="cr sm"><a id="rmSel">Remove selected</a><a id="clearAll">Clear</a></span></div>
        <div class="cb"><div class="drop empty" id="drop"><b>Drop .rkg ghost files here</b><span>or click to browse</span></div><input type="file" id="file" accept=".rkg" multiple hidden>
          <div class="tools hidden" id="tools"><span class="sm">Same track for all</span><div class="allwrap"><input type="text" id="allPick" placeholder="Search a track..." autocomplete="off" spellcheck="false"></div><span class="grow"></span><span class="sm">Class</span><button class="chip on" id="cc150">150cc</button><button class="chip" id="cc200" title="Experimental: where the 200cc table sits is a guess">200cc<span class="exp">beta</span></button></div>
          <div class="tablewrap hidden" id="tw"><table><thead><tr><th>File</th><th>Time</th><th>Player</th><th>Controller</th><th>Track</th><th></th></tr></thead><tbody id="rows"></tbody></table></div></div></div>
      <div class="log" id="log"></div>
    </section>
  </div>
</div>
<div class="menu" id="menu"></div><div class="toast" id="toast"></div>

<script>
const TOKEN="__TOKEN__";
const $=s=>document.querySelector(s);
const esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let S={tracks:[],dir:null,ok:false,experts:false,ghosts:[],sel:new Set(),cc:{150:true,200:false},nextId:1};

async function api(path,body){
  const r=await fetch(path,{method:body?'POST':'GET',headers:{'X-Token':TOKEN,'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined});
  const j=await r.json().catch(()=>({}));
  if(!r.ok) throw new Error(j.error||('Request failed ('+r.status+')'));
  return j;
}
function toast(msg,kind){const t=$('#toast');t.textContent=msg;t.className='toast show '+(kind||'');clearTimeout(toast.h);toast.h=setTimeout(()=>t.className='toast',3000)}
const trackName=k=>(S.tracks.find(t=>t.key===k)||{}).name||k;
function say(msg,tag){const l=$('#log');const d=document.createElement('div');if(tag)d.className=tag;d.textContent=msg;l.appendChild(d);l.scrollTop=l.scrollHeight}

/* ---------- folder ---------- */
function applyState(st){
  S.tracks=st.tracks;S.dir=st.dir;S.ok=st.ok;S.experts=st.experts;
  $('#dirText').textContent=st.ok?st.dir:'Not found yet';
  $('#trackCount').textContent=st.ok?st.tracks.length+' tracks available':'';
  if(st.version)$('#ver').textContent='v'+st.version;
  $('#banner').classList.toggle('show',!st.ok);
  if(typeof renderSummary==='function')renderSummary();
}
$('#changeDir').onclick=()=>{$('#banner').classList.add('show');$('#dirInput').value=S.dir||'';$('#dirInput').focus();$('#dirInput').select()};
$('#dirInput').addEventListener('keydown',async e=>{
  if(e.key!=='Enter')return;
  try{applyState(await api('/api/set_dir',{path:e.target.value}));say('Ghosts folder set.','ok');if(S.ghosts.length)await reanalyze()}
  catch(err){say(err.message,'err')}
});
/* ---------- files ---------- */
const drop=$('#drop'),fileIn=$('#file');
drop.onclick=()=>fileIn.click();
fileIn.onchange=()=>{addFiles(fileIn.files);fileIn.value=''};
['dragenter','dragover'].forEach(ev=>window.addEventListener(ev,e=>{e.preventDefault();drop.classList.add('over')}));
['dragleave','drop'].forEach(ev=>window.addEventListener(ev,e=>{e.preventDefault();if(ev==='drop'||!e.relatedTarget)drop.classList.remove('over')}));
window.addEventListener('drop',e=>{if(e.dataTransfer&&e.dataTransfer.files.length)addFiles(e.dataTransfer.files)});
function b64(buf){let s='';const u=new Uint8Array(buf);for(let i=0;i<u.length;i+=0x8000)s+=String.fromCharCode.apply(null,u.subarray(i,i+0x8000));return btoa(s)}
async function addFiles(fl){
  const add=[];
  for(const f of fl){
    if(!/\.rkg$/i.test(f.name)){say(f.name+' is not a .rkg file','warn');continue}
    if(S.ghosts.some(g=>g.name===f.name&&g.size===f.size))continue;
    add.push({id:S.nextId++,name:f.name,size:f.size,b64:b64(await f.arrayBuffer())});
  }
  if(!add.length)return;
  try{
    const r=await api('/api/analyze',{files:add.map(a=>({name:a.name,b64:a.b64}))});
    add.forEach((a,i)=>{Object.assign(a,r.ghosts[i]);a.key=null;a.auto=false;
      if(a.valid&&a.candidates.length===1){a.key=a.candidates[0].key;a.auto=true;say(`Auto-detected ${a.time}: ${a.candidates[0].name}`,'ok')}
      else if(a.valid&&a.candidates.length>1)say(`${a.time}: ${a.candidates.length} possible tracks - pick one in the Track column`,'warn')
      else if(a.valid)say(`${a.time}: no automatic match - search for the track`,'dim')});
    S.ghosts.push(...add);render();
  }catch(err){say(err.message,'err')}
}
async function reanalyze(){
  const r=await api('/api/analyze',{files:S.ghosts.map(a=>({name:a.name,b64:a.b64}))});
  S.ghosts.forEach((g,i)=>{const prev=g.key;Object.assign(g,r.ghosts[i]);g.key=prev});render();
}
$('#clearAll').onclick=()=>{S.ghosts=[];S.sel.clear();render()};
$('#rmSel').onclick=()=>{S.ghosts=S.ghosts.filter(g=>!S.sel.has(g.id));S.sel.clear();render()};

/* ---------- table ---------- */
function renderSummary(){
  const el=$('#sum');if(!el)return;
  const valid=S.ghosts.filter(g=>g.valid);
  const keyOf=g=>g.key;
  const withKey=valid.filter(keyOf);
  const groups={};withKey.forEach(g=>{const k=keyOf(g);(groups[k]=groups[k]||[]).push(g)});
  const missing=valid.filter(g=>!keyOf(g));
  const classes=[150,200].filter(c=>S.cc[c]);
  const nTracks=Object.keys(groups).length;
  let state,cls;
  if(!S.ok){state='No folder';cls='warn'}
  else if(!valid.length){state='Waiting';cls='idle'}
  else if(missing.length){state=missing.length+' need a track';cls='warn'}
  else{state='Ready';cls='ok'}
  $('#sumState').innerHTML=`<span class="pillst ${cls}">${state}</span>`;
  let html=`<div class="stats"><div class="stat"><b>${valid.length}</b><span>Ghosts</span></div><div class="stat"><b>${nTracks}</b><span>Tracks</span></div><div class="stat"><b>${classes.length}</b><span>Class${classes.length>1?'es':''}</span></div></div>`;
  Object.entries(groups).forEach(([k,gs])=>{html+=`<div class="grp"><span>${esc(trackName(k))}</span><span class="n">${gs.length} ghost${gs.length>1?'s':''} &middot; ${classes.map(c=>c+'cc').join(' + ')}</span></div>`});
  if(missing.length)html+=`<div class="grp miss"><span>No track yet</span><span class="n">${missing.map(g=>g.time).join(', ')}</span></div>`;
  html+=!valid.length?'<div class="sumline">Add ghosts to get started.</div>':
        missing.length?'<div class="sumline warn">Pick a track for the ghosts above - they will be left out otherwise.</div>':
        '<div class="sumline ok">Press Preview to check, then Import.</div>';
  el.innerHTML=html;
}
function render(){
  if(pk)close();
  const has=S.ghosts.length>0;
  $('#drop').className='drop '+(has?'compact':'empty');
  $('#drop').innerHTML=has?'<b>+ Drop more ghosts</b><span>or click to browse</span>':'<b>Drop .rkg ghost files here</b><span>or click to browse</span>';
  $('#tw').classList.toggle('hidden',!has);
  $('#tools').classList.toggle('hidden',!has);
  const T=$('#rows');
  T.innerHTML=S.ghosts.map(g=>{
    if(!g.valid)return `<tr class="bad" data-id="${g.id}"><td>${esc(g.name)}</td><td colspan="4">invalid - ${esc(g.error||'')}</td><td class="x" data-rm="${g.id}">&times;</td></tr>`;
    const cls=g.key?(g.auto?'auto':''):'none';
    const ph=g.candidates.length>1?`${g.candidates.length} possible - choose...`:'Search a track or type its ID...';
    return `<tr data-id="${g.id}" class="${S.sel.has(g.id)?'sel':''}"><td>${esc(g.name)}</td><td>${esc(g.time)}</td><td>${esc(g.player||'')}</td><td>${esc(g.ctrl)}</td>
      <td class="tk"><input type="text" class="${cls}" data-pick="${g.id}" placeholder="${ph}" autocomplete="off" spellcheck="false" value="${g.key?esc(trackName(g.key)):''}"></td><td class="x" data-rm="${g.id}">&times;</td></tr>`;
  }).join('');
  T.querySelectorAll('tr').forEach(tr=>tr.addEventListener('click',e=>{
    if(e.target.closest('input')||e.target.closest('[data-rm]'))return;
    const id=+tr.dataset.id;S.sel.has(id)?S.sel.delete(id):S.sel.add(id);tr.classList.toggle('sel')}));
  T.querySelectorAll('[data-rm]').forEach(b=>b.onclick=()=>{S.ghosts=S.ghosts.filter(g=>g.id!=b.dataset.rm);render()});
  T.querySelectorAll('[data-pick]').forEach(inp=>attachPicker(inp,k=>setKey(+inp.dataset.pick,k),()=>candFor(+inp.dataset.pick)));
  renderSummary();
}
const candFor=id=>((S.ghosts.find(g=>g.id===id)||{}).candidates||[]);
function setKey(id,k){const g=S.ghosts.find(x=>x.id===id);if(!g)return;g.key=k;g.auto=false;render()}

/* ---------- searchable track picker ---------- */
const menu=$('#menu');let pk=null;
function attachPicker(inp,onPick,getCands){
  inp.addEventListener('focus',()=>{inp.select();pk={inp,onPick,getCands,act:0,items:[]};build()});
  inp.addEventListener('input',build);
  inp.addEventListener('keydown',e=>{
    if(!pk||pk.inp!==inp)return;
    if(e.key==='ArrowDown'){pk.act=Math.min(pk.act+1,pk.items.length-1);paint();e.preventDefault()}
    else if(e.key==='ArrowUp'){pk.act=Math.max(pk.act-1,0);paint();e.preventDefault()}
    else if(e.key==='Enter'){if(pk.items[pk.act])choose(pk.items[pk.act].key);e.preventDefault()}
    else if(e.key==='Escape'){close()}
  });
}
function build(){
  if(!pk)return;
  const q=pk.inp.value.trim().toLowerCase();
  const cands=(pk.getCands?pk.getCands():[]).filter(c=>!q||c.name.toLowerCase().includes(q)||q===trackName(c.key).toLowerCase());
  const ck=new Set(cands.map(c=>c.key));
  const rest=S.tracks.filter(t=>!ck.has(t.key)&&(!q||t.name.toLowerCase().includes(q)||t.key.includes(q))).slice(0,60);
  const idm=q.match(/^[0-9a-f]{8}(\/\d{1,3})?$/);
  const useId=idm&&!S.tracks.some(t=>t.key===q)?[{key:q}]:[];
  pk.items=[...useId,...cands.map(c=>({key:c.key})),...rest];pk.act=0;
  let h='',i=0;
  if(useId.length){h+=`<div class="it" data-i="${i++}">Use track ID <b>&nbsp;${esc(q)}</b><span class="tag newer">not in list</span></div>`}
  if(cands.length){h+='<div class="sec">Matches this ghost</div>';cands.forEach(c=>{h+=item(i++,c.name,'',true)})}
  if(rest.length){if(cands.length)h+='<div class="sec">All tracks</div>';rest.forEach(t=>{h+=item(i++,t.name,t.tag,false)})}
  if(!pk.items.length)h='<div class="sec">No tracks found</div>';
  menu.innerHTML=h;place();
  menu.querySelectorAll('.it').forEach(el=>{el.onmousedown=e=>{e.preventDefault();choose(pk.items[+el.dataset.i].key)}});
  paint();
}
function place(){
  if(!pk)return;
  if(!pk.inp.isConnected){close();return}
  const r=pk.inp.getBoundingClientRect();
  const below=window.innerHeight-r.bottom-16,above=r.top-16,up=below<220&&above>below;
  menu.style.left=r.left+'px';menu.style.width=Math.max(r.width,330)+'px';menu.style.display='block';
  menu.style.maxHeight=Math.max(150,Math.min(320,up?above:below))+'px';
  if(up){menu.style.top='';menu.style.bottom=(window.innerHeight-r.top+4)+'px'}else{menu.style.bottom='';menu.style.top=(r.bottom+4)+'px'}
}
window.addEventListener('scroll',place,true);window.addEventListener('resize',place);
const item=(i,name,tag,sug)=>`<div class="it" data-i="${i}">${esc(name)}${tag?`<span class="tag ${tag}">${tag==='game'?'in your game':'newer'}</span>`:(sug?'<span class="tag game">match</span>':'')}</div>`;
function paint(){menu.querySelectorAll('.it').forEach((el,i)=>el.classList.toggle('act',i===pk.act));const a=menu.querySelector('.it.act');if(a)a.scrollIntoView({block:'nearest'})}
function choose(k){const f=pk.onPick;close();f(k)}
function close(){menu.style.display='none';pk=null}
document.addEventListener('mousedown',e=>{if(pk&&!menu.contains(e.target)&&e.target!==pk.inp)close()});
attachPicker($('#allPick'),k=>{S.ghosts.forEach(g=>{if(g.valid){g.key=k;g.auto=false}});$('#allPick').value=trackName(k);render()},null);

/* ---------- classes ---------- */
function setCC(c){S.cc[c]=!S.cc[c];if(!S.cc[150]&&!S.cc[200])S.cc[c==150?200:150]=true;
  $('#cc150').classList.toggle('on',S.cc[150]);$('#cc200').classList.toggle('on',S.cc[200]);renderSummary()}
$('#cc150').onclick=()=>setCC(150);$('#cc200').onclick=()=>setCC(200);

/* ---------- run ---------- */
function collect(){
  if(!S.ok){toast('Pick your Ghosts folder first','bad');return null}
  const valid=S.ghosts.filter(g=>g.valid);
  if(!valid.length){toast('Add some .rkg files first','bad');return null}
  const items=valid.filter(g=>g.key).map(g=>({name:g.name,b64:g.b64,key:g.key}));
  const left=valid.length-items.length;
  if(!items.length){toast('Pick a track for your ghosts first','bad');return null}
  if(left)say(`${left} ghost(s) have no track and will be left out.`,'warn');
  return {items};
}
async function run(mode){
  const c=collect();if(!c)return;
  if(mode==='import'&&!confirm('Close the game (and any emulator) before importing. Continue?'))return;
  const classes=[150,200].filter(x=>S.cc[x]);
  try{
    const r=await api('/api/run',{mode,classes,items:c.items});
    if(mode==='preview')showPreview(r.results);else showImport(r.results);
  }catch(err){say('Failed: '+err.message,'err');toast(err.message,'bad')}
}
function showPreview(res){
  say('');say('PREVIEW - nothing has been changed','head');
  res.forEach(r=>{
    say(`Track   ${r.track}  [${r.key}]   ${r.cc}cc`,'dim');
    if(r.error){say('  '+r.error,'err');return}
    say('Folder  '+r.folder,'dim');
    if(r.new_leaderboard)say('A new leaderboard will be created for this track.','warn');
    r.rows.forEach(x=>say(`  ${x.file}: ${x.time}${x.player?' by '+x.player:''} (${x.controller}) - ${x.note}`,x.ok?'ok':'warn'));
    say(`${r.cc}cc top 10 after import  (* = new)`,'head');
    r.after.forEach((a,i)=>say(`  ${String(i+1).padStart(2)}. ${a.time.padEnd(10)} ${a.player}${a.new?'   *':''}`,a.new?'acc':''));
  });
  say('Happy with that? Press Import ghosts.','dim');
}
function showImport(res){
  say('');let done=0;
  res.forEach(r=>{
    say(`${r.track}  (${r.cc}cc)`,'head');
    if(r.error){say('  '+r.error,'err');return}
    r.report.forEach(x=>{const ok=x.status.startsWith('OK');if(ok)done++;say(`  ${x.file}: ${x.status}`,ok?'ok':'warn')});
    if(r.created)say('  The track had no leaderboard yet, so a new one was created.','dim');
    if(r.backup)say('  Backup: '+r.backup,'dim');
  });
  if(done){say('Done. Launch the game and check Time Trials on that track.','ok');toast('Done - launch the game','good');
    S.ghosts=S.ghosts.filter(g=>!(g.valid&&g.key));S.sel.clear();render()}
  else say('Nothing changed.','warn');
}
$('#btnPreview').onclick=()=>run('preview');
$('#btnImport').onclick=()=>run('import');

/* ---------- app feel ---------- */
document.addEventListener('contextmenu',e=>{if(!e.target.closest('input,.log,.box'))e.preventDefault()});
document.addEventListener('keydown',e=>{
  if((e.ctrlKey||e.metaKey)&&['+','-','=','0','p','s','u','r','f','g'].includes(e.key.toLowerCase()))e.preventDefault();
  if(e.key==='F5'||e.key==='F12'&&false)e.preventDefault();
  if(e.altKey&&(e.key==='ArrowLeft'||e.key==='ArrowRight'))e.preventDefault();
});
window.addEventListener('wheel',e=>{if(e.ctrlKey)e.preventDefault()},{passive:false});
/* ---------- boot ---------- */
(async()=>{
  try{const st0=await api('/api/state');applyState(st0);say(S.ok?'Ghosts folder found automatically.':'Could not find the Ghosts folder automatically.',S.ok?'ok':'warn');
    if(S.ok&&!st0.lookup)say("Couldn't find the pack's track list (FolderToTrackNameRT.txt) - only tracks your game has already created are listed. You can still type a track ID.",'warn')}catch(e){say(e.message,'err')}
  say('Backups are made before every import.','dim');
  renderSummary();
  setInterval(()=>api('/api/ping').catch(()=>{}),2500);api('/api/ping').catch(()=>{});
  window.__app={S,addFiles,render,run,applyState,setKey,say};
})();
</script>
</body>
</html>
"""


# ---- auto-detect -------------------------------------------------------------------------------
def find_experts_dir(ghosts_dir):
    """The pack's ExpertsRT folder (expert ghosts named <track number>_150.rkg), if we can find it."""
    cands = []
    app = os.environ.get('APPDATA', '')
    cands.append(os.path.join(app, 'CT-MKWII', 'RetroRewind', 'RetroRewind6', 'Ghosts', 'ExpertsRT'))
    if ghosts_dir:
        cands.append(os.path.join(ghosts_dir, 'ExpertsRT'))
    for r in (os.path.join(app, 'CT-MKWII'),):
        if os.path.isdir(r):
            base = r.rstrip(os.sep).count(os.sep)
            for dp, dn, _ in os.walk(r):
                if dp.count(os.sep) - base > 6:
                    dn[:] = []; continue
                if os.path.basename(dp) == 'ExpertsRT':
                    cands.append(dp); dn[:] = []
    for c in cands:
        if os.path.isdir(c):
            return c
    return None


def slot_index(experts_dir, order):
    """{stock-game slot number: [track keys]} built from the expert ghosts' own headers.
    A ghost only records which stock slot its track is built on, so this gives a shortlist, never a certainty."""
    out = {}
    if not experts_dir or not os.path.isdir(experts_dir):
        return out
    for f in os.listdir(experts_dir):
        m = re.fullmatch(r'(\d+)(?:_v(\d+))?_150\.rkg', f)
        if not m:
            continue
        idx, v = int(m.group(1)), int(m.group(2) or 0)
        if idx >= len(order):
            continue
        try:
            head = _read(os.path.join(experts_dir, f), 8)
        except OSError:
            continue
        if head[:4] != b'RKGD':
            continue
        key = order[idx][0] + (f'/{v}' if v else '')
        out.setdefault(head[7] >> 2, []).append(key)
    return out


# ---- local web app -----------------------------------------------------------------------------
import base64, json, secrets, subprocess, threading, time, webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

APP = {'dir': None, 'tracks': {}, 'game': set(), 'slots': {}, 'experts': None, 'lookup': False,
       'last_ping': 0.0, 'pinged': False}
TOKEN = secrets.token_urlsafe(16)


def load_app_state(path):
    APP['dir'] = path
    dirs = lookup_dirs(path)
    tracks = load_tracks(dirs)
    exp = find_experts_dir(path)
    predicted = versioned_tracks(dirs, exp)
    tracks.update(predicted)
    game = set()
    for k, v in scan_known_tracks(path).items():
        if k not in tracks or '/' in k:
            tracks[k] = v
            game.add(k)
        else:
            game.add(k)
    APP['tracks'], APP['game'], APP['experts'] = tracks, game, exp
    APP['slots'] = slot_index(exp, rt_order(dirs))
    APP['lookup'] = bool(dirs)


def state_json():
    tr = []
    for k, v in APP['tracks'].items():
        tag = 'game' if k in APP['game'] and '/' in k else ('newer' if '/' in k else '')
        name = f'{v} (newer version)' if (tag == 'newer') else v
        tr.append({'key': k, 'name': name, 'tag': tag})
    tr.sort(key=lambda t: t['name'].lower())
    return {'dir': APP['dir'], 'ok': bool(APP['dir'] and os.path.isdir(APP['dir'])), 'tracks': tr,
            'experts': bool(APP['experts']), 'lookup': APP['lookup'], 'slots_known': len(APP['slots']),
            'version': __version__}


def analyze(files):
    out = []
    for f in files:
        name, raw = f['name'], base64.b64decode(f['b64'])
        g = {'name': name, 'valid': False, 'candidates': []}
        try:
            info = parse_rkg(raw)
            slot = raw[7] >> 2
            keys = APP['slots'].get(slot, [])
            keys = sorted(set(keys), key=lambda k: (k not in APP['game'], APP['tracks'].get(k, k).lower()))
            g.update(valid=True, time=time_name(info['total']), player=mii_name(info['mii']),
                     ctrl=CTRL_NAMES.get(info['ctrl'], str(info['ctrl'])), slot=slot,
                     candidates=[{'key': k, 'name': APP['tracks'].get(k, k)} for k in keys if k in APP['tracks']])
        except RRError as e:
            g['error'] = str(e)
        out.append(g)
    return out


def run_import(mode, classes, items, manual_name=''):
    """Group ghosts by (track, class) and preview or import each group."""
    groups = {}
    for it in items:
        if not re.fullmatch(r'[0-9a-f]{8}(/\d{1,3})?', str(it.get('key', ''))):
            raise RRError('Invalid track ID')
        groups.setdefault(it['key'], []).append((it['name'], base64.b64decode(it['b64'])))
    results = []
    for key, rkgs in groups.items():
        tid, _, ver = key.partition('/')
        ver = int(ver) if ver else 0
        tname = manual_name or APP['tracks'].get(key) or APP['tracks'].get(tid) or tid
        for cc in classes:
            res = {'key': key, 'track': tname, 'cc': cc}
            try:
                if mode == 'preview':
                    pv = preview_import(APP['dir'], tid, rkgs, track_name=tname, version=ver, cc=cc)
                    res.update(folder=pv['folder'], new_leaderboard=pv['new_leaderboard'], rows=pv['rows'],
                               after=[{'time': a, 'player': b, 'new': c} for a, b, c in pv['after']])
                    for r in res['rows']:
                        r.pop('key', None)
                else:
                    rep, bk, created = import_into_track(APP['dir'], tid, rkgs, track_name=tname, version=ver, cc=cc)
                    res.update(report=[{'file': n, 'status': s} for n, s in rep], backup=bk, created=created)
            except Exception as e:
                res['error'] = str(e)
            results.append(res)
    return results


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype='application/json'):
        data = body if isinstance(body, bytes) else (json.dumps(body) if ctype == 'application/json' else body).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', ctype + ('; charset=utf-8' if ctype != 'application/json' else ''))
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(data)

    def _authed(self):
        return self.headers.get('X-Token') == TOKEN

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == '/':
            if parse_qs(u.query).get('t', [''])[0] != TOKEN:
                return self._send(403, 'Open this app from the program window.', 'text/plain')
            return self._send(200, HTML.replace('__TOKEN__', TOKEN), 'text/html')
        if not self._authed():
            return self._send(403, {'error': 'forbidden'})
        if u.path == '/api/state':
            return self._send(200, state_json())
        if u.path == '/api/ping':
            APP['last_ping'] = time.time(); APP['pinged'] = True
            return self._send(200, {'ok': True})
        self._send(404, {'error': 'not found'})

    def do_POST(self):
        if not self._authed():
            return self._send(403, {'error': 'forbidden'})
        try:
            n = int(self.headers.get('Content-Length', 0))
            if n > 64 * 1024 * 1024:
                return self._send(413, {'error': 'Request too large'})
            body = json.loads(self.rfile.read(n) or b'{}')
            p = urlparse(self.path).path
            if p == '/api/set_dir':
                path = body.get('path', '').strip().strip('"')
                if not os.path.isdir(path):
                    return self._send(400, {'error': 'That folder does not exist.'})
                load_app_state(path)
                return self._send(200, state_json())
            if p == '/api/analyze':
                return self._send(200, {'ghosts': analyze(body.get('files', []))})
            if p == '/api/run':
                if not APP['dir']:
                    return self._send(400, {'error': 'Pick your Ghosts folder first.'})
                classes = [c for c in body.get('classes', [150]) if c in CLASS_OFFS] or [150]
                return self._send(200, {'results': run_import(body.get('mode', 'preview'), classes, body.get('items', []), body.get('manual_name', ''))})
            self._send(404, {'error': 'not found'})
        except Exception as e:
            self._send(500, {'error': str(e)})


TITLE = 'Retro Rewind Ghost Importer'


def darken_titlebar(title=TITLE):
    """Windows only: make the native title bar dark so it matches the app (no white bar)."""
    if os.name != 'nt':
        return
    try:
        import ctypes
        user32, dwm = ctypes.windll.user32, ctypes.windll.dwmapi
        hwnd = 0
        for _ in range(60):
            hwnd = user32.FindWindowW(None, title)
            if hwnd:
                break
            time.sleep(0.25)
        if not hwnd:
            return
        on = ctypes.c_int(1)
        for attr in (20, 19):                                   # immersive dark mode (Win10 1809+ / Win11)
            dwm.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(on), 4)
        dwm.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(ctypes.c_int(0x001A1310)), 4)   # caption colour #10131a (Win11)
        dwm.DwmSetWindowAttribute(hwnd, 36, ctypes.byref(ctypes.c_int(0x00F6F0EE)), 4)   # caption text colour
        user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x0027)        # redraw the frame
    except Exception:
        pass


def open_window(url):
    """Open the UI in an app-style window (Edge/Chrome --app), else the default browser."""
    pf = [os.environ.get('PROGRAMFILES(X86)', ''), os.environ.get('PROGRAMFILES', ''), os.environ.get('LOCALAPPDATA', '')]
    rels = [r'Microsoft\Edge\Application\msedge.exe', r'Google\Chrome\Application\chrome.exe',
            r'BraveSoftware\Brave-Browser\Application\brave.exe']
    for base in pf:
        for r in rels:
            exe = os.path.join(base, r) if base else ''
            if exe and os.path.isfile(exe):
                try:
                    subprocess.Popen([exe, f'--app={url}', '--window-size=1120,700', '--disable-features=Translate'])
                    return True
                except OSError:
                    pass
    return webbrowser.open(url)


def hide_console():
    """Hide the black console window on Windows when this program owns it (double-clicked .py or the exe)."""
    if os.name != 'nt':
        return
    try:
        import ctypes
        k = ctypes.windll.kernel32
        procs = (ctypes.c_uint * 4)()
        hwnd = k.GetConsoleWindow()
        if hwnd and k.GetConsoleProcessList(procs, 4) <= 1:
            ctypes.windll.user32.ShowWindow(hwnd, 0)
    except Exception:
        pass


def main(open_browser=True, port=0, ghosts_dir=None):
    import time as _t
    hide_console()
    srv = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    port = srv.server_address[1]
    found = [ghosts_dir] if ghosts_dir else find_ghost_dirs()
    load_app_state(found[0] if found else None)
    url = f'http://127.0.0.1:{port}/?t={TOKEN}'
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    if open_browser:
        try:
            import webview                                    # optional: pip install pywebview -> a real app window
        except Exception:
            webview = None
        if webview is not None:
            webview.create_window(TITLE, url, width=1120, height=700, min_size=(760, 560), background_color='#10131a',
                                  text_select=True)
            threading.Thread(target=darken_titlebar, daemon=True).start()
            webview.start()                                   # blocks until the window is closed
            srv.shutdown()
            return
        threading.Thread(target=darken_titlebar, daemon=True).start()
        open_window(url)
    print('Retro Rewind Ghost Importer running at', url)
    started = _t.time()
    try:
        while True:                      # quit when the window has been closed
            _t.sleep(1)
            if APP['pinged'] and _t.time() - APP['last_ping'] > 20:
                break
            if not APP['pinged'] and _t.time() - started > 120:
                break
    except KeyboardInterrupt:
        pass
    srv.shutdown()


def cli():
    import argparse
    p = argparse.ArgumentParser(description='Add downloaded .rkg ghosts to Retro Rewind (Pulsar) time trials.')
    p.add_argument('--ghosts-dir', help='path to Pulsar/RetroRewind6/Ghosts (default: find it automatically)')
    p.add_argument('--no-window', action='store_true', help="don't open a window; just print the local URL")
    p.add_argument('--port', type=int, default=0, help='local port to use (default: any free port)')
    p.add_argument('--version', action='version', version=f'rr-ghost-importer {__version__}')
    a = p.parse_args()
    main(open_browser=not a.no_window, port=a.port, ghosts_dir=a.ghosts_dir)


if __name__ == '__main__':
    try:
        cli()
    except Exception as exc:                                  # show failures even in the windowed exe
        import traceback
        msg = traceback.format_exc()
        try:
            with open(os.path.join(os.path.expanduser('~'), 'RRGhostImporter-error.txt'), 'w') as f:
                f.write(msg)
            if os.name == 'nt':
                import ctypes
                ctypes.windll.user32.MessageBoxW(0, f'{exc}\n\nDetails saved to RRGhostImporter-error.txt in your user folder.',
                                                 'Retro Rewind Ghost Importer', 0x10)
        except Exception:
            pass
        raise
