"""Tests use synthetic ghosts only (no game files). Run from the repo folder:  python -m unittest discover -s tests"""
import base64, json, os, shutil, struct, sys, tempfile, threading, unittest, urllib.error, urllib.request, zlib

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import rr_ghost_importer as rr


def pack_time(m, s, ms):
    return ((m << 17) | (s << 10) | ms).to_bytes(3, 'big')


def make_rkg(m, s, ms, player='Tester', ctrl=3, char=22, veh=23, slot=2, laps=((0, 40, 0), (0, 41, 0), (0, 42, 0)), drift=0):
    """A minimal valid-looking .rkg: header fields the importer reads, some payload, and the trailing CRC32."""
    h = bytearray(0x88)
    h[0:4] = b'RKGD'
    h[4:7] = pack_time(m, s, ms)
    h[7] = slot << 2
    h[8:12] = ((veh << 26) | (char << 20) | ctrl).to_bytes(4, 'big')
    h[0xD] = drift << 1
    h[0x10] = len(laps)
    for i, lap in enumerate(laps):
        h[0x11 + 3 * i:0x14 + 3 * i] = pack_time(*lap)
    mii = bytearray(0x4A)
    mii[2:2 + 2 * len(player)] = player.encode('utf-16-be')
    h[0x3C:0x3C + 0x4A] = mii
    h[0x86:0x88] = b'\xab\xcd'
    body = bytes(h) + struct.pack('>I', 8) + b'payload!'
    return body + struct.pack('>I', zlib.crc32(body))


TID = '1db6dcea'


class Base(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.gd = os.path.join(self.d, 'Ghosts')
        os.makedirs(self.gd)

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def table(self, ldb, cc=150, lap=False):
        off = rr.CLASS_OFFS[cc][1 if lap else 0]
        return sorted(rr.read_table(bytearray(ldb), off), key=rr.entry_time)


class TestGhostParsing(unittest.TestCase):
    def test_fields(self):
        i = rr.parse_rkg(make_rkg(1, 54, 866, player='Tester2', ctrl=2, laps=((0, 38, 1), (0, 37, 500), (0, 39, 0))))
        self.assertEqual(i['total'], (1, 54, 866))
        self.assertEqual(i['best_lap'], (0, 37, 500))
        self.assertEqual(rr.mii_name(i['mii']), 'Tester2')
        self.assertEqual(rr.CTRL_NAMES[i['ctrl']], 'Classic')
        self.assertEqual(rr.time_name(i['total']), '1m54s866')

    def test_rejects_bad_files(self):
        with self.assertRaises(rr.RRError): rr.parse_rkg(b'nope')
        bad = bytearray(make_rkg(1, 0, 0)); bad[-1] ^= 1
        with self.assertRaises(rr.RRError): rr.parse_rkg(bytes(bad))

    def test_private_use_glyphs_removed_from_names(self):
        self.assertEqual(rr.mii_name(b'\0\0' + 'ab\uf063'.encode('utf-16-be') + b'\0' * 30), 'ab')


class TestLeaderboard(Base):
    def test_blank_leaderboard_header(self):
        b = rr.new_ldb(TID, 'DS Airship Fortress')
        self.assertEqual((len(b), b[:4], b[4:8], b[8:12]), (rr.LDB_SIZE, b'PULL', b'\0\0\0\1', bytes.fromhex(TID)))
        self.assertTrue(b[12:].startswith(b'DS Airship Fortress'))

    def test_entry_layout(self):
        info = rr.parse_rkg(make_rkg(2, 6, 869, ctrl=3, char=22, veh=23, drift=1))
        e = rr.make_entry(info, info['total'], True)
        self.assertEqual(len(e), 100)
        self.assertEqual((e[:74], e[74:76], e[76:80]), (info['mii'], info['crc16'], info['crc32']))
        self.assertEqual((e[80:82], e[82], e[83:86], e[86], e[87]), (b'\0\2', 6, b'\0\x03\x65', 1, 1))
        self.assertEqual(struct.unpack('>III', e[88:100]), (22, 23, 3))

    def test_sorted_dedupe_and_top10(self):
        ldb = rr.new_ldb(TID, 'x')
        for s in range(12, 0, -1):                      # 12 ghosts, slowest first
            ldb, _ = rr.add_ghost(ldb, make_rkg(1, 30 + s, 0, player=f'P{s}'), TID)
        t = self.table(ldb)
        self.assertEqual(len(t), 10)
        self.assertEqual([rr.entry_time(e)[1] for e in t], list(range(31, 41)))
        with self.assertRaises(rr.RRError):             # same ghost twice
            rr.add_ghost(ldb, make_rkg(1, 31, 0, player='P1'), TID)
        with self.assertRaises(rr.RRError):             # slower than a full top 10
            rr.add_ghost(ldb, make_rkg(5, 0, 0), TID)

    def test_lap_table_filled(self):
        ldb, _ = rr.add_ghost(rr.new_ldb(TID, 'x'), make_rkg(1, 54, 0, laps=((0, 40, 0), (0, 36, 471), (0, 41, 0))), TID)
        self.assertEqual(rr.entry_time(self.table(ldb, lap=True)[0]), (0, 36, 471))

    def test_wrong_track_and_size_refused(self):
        with self.assertRaises(rr.RRError): rr.add_ghost(rr.new_ldb('aaaaaaaa', 'x'), make_rkg(1, 0, 0), TID)
        with self.assertRaises(rr.RRError): rr.add_ghost(b'\0' * 100, make_rkg(1, 0, 0), TID)

    def test_200cc_goes_in_its_own_block_and_is_guarded(self):
        ldb = rr.new_ldb(TID, 'x')
        new, _ = rr.add_ghost(ldb, make_rkg(1, 54, 0), TID, True, 200)
        self.assertEqual(new[:rr.CLASS_OFFS[200][0]], ldb[:rr.CLASS_OFFS[200][0]])      # 150cc part untouched
        self.assertEqual(len(self.table(new, 200)), 1)
        junk = bytearray(ldb); junk[rr.CLASS_OFFS[200][0] + 10] = 0x55; junk[rr.CLASS_OFFS[200][0] + 86] = 7
        with self.assertRaises(rr.RRError): rr.add_ghost(bytes(junk), make_rkg(1, 54, 0), TID, True, 200)


class TestImport(Base):
    def files(self, n=3):
        return [(f'g{i}.rkg', make_rkg(1, 40 + i, 0, player=f'P{i}')) for i in range(n)]

    def test_creates_files_backup_and_second_run_is_noop(self):
        rep, bk, created = rr.import_into_track(self.gd, TID, self.files(), 'Test Track')
        self.assertTrue(created and bk is None and all(r[1].startswith('OK') for r in rep))
        self.assertEqual(sorted(os.listdir(os.path.join(self.gd, TID, '150'))), ['1m40s000.rkg', '1m41s000.rkg', '1m42s000.rkg'])
        rep2, bk2, _ = rr.import_into_track(self.gd, TID, self.files(), 'Test Track')
        self.assertTrue(bk2 is None and all('already' in r[1] for r in rep2))

    def test_version_folder_is_separate(self):
        rr.import_into_track(self.gd, TID, self.files(1), 'Base')
        rr.import_into_track(self.gd, TID, self.files(2), 'Newer', version=1)
        self.assertTrue(os.path.isfile(os.path.join(self.gd, TID, '1', 'ldb.pul')))
        self.assertEqual(len(os.listdir(os.path.join(self.gd, TID, '150'))), 1)
        self.assertEqual(len(os.listdir(os.path.join(self.gd, TID, '1', '150'))), 2)

    def test_backup_keeps_the_original_when_150_and_200_run_back_to_back(self):
        rr.import_into_track(self.gd, TID, self.files(1), 'T')                 # leaderboard now exists
        path = os.path.join(self.gd, TID, 'ldb.pul'); original = open(path, 'rb').read()
        _, bk1, _ = rr.import_into_track(self.gd, TID, [('n.rkg', make_rkg(1, 10, 0, player='New'))], 'T', cc=150)
        _, bk2, _ = rr.import_into_track(self.gd, TID, [('n.rkg', make_rkg(1, 10, 0, player='New'))], 'T', cc=200)
        if bk1 == bk2:                                                          # same second -> must still be the pre-run state
            self.assertEqual(open(bk1, 'rb').read(), original)

    def test_nothing_written_if_every_ghost_is_rejected(self):
        rep, bk, created = rr.import_into_track(self.gd, TID, [('bad.rkg', b'nope')], 'T')
        self.assertTrue(bk is None and not created and not os.path.exists(os.path.join(self.gd, TID)))

    def test_preview_matches_import_and_writes_nothing(self):
        rr.import_into_track(self.gd, TID, self.files(2), 'T')
        before = open(os.path.join(self.gd, TID, 'ldb.pul'), 'rb').read()
        new = [('a.rkg', make_rkg(1, 20, 0, player='A')), ('b.rkg', make_rkg(1, 41, 0, player='P1'))]
        pv = rr.preview_import(self.gd, TID, new, 'T')
        self.assertEqual(open(os.path.join(self.gd, TID, 'ldb.pul'), 'rb').read(), before)
        self.assertEqual([r['ok'] for r in pv['rows']], [True, False])
        rr.import_into_track(self.gd, TID, new, 'T')
        real = [rr.time_name(rr.entry_time(e)) for e in self.table(open(os.path.join(self.gd, TID, 'ldb.pul'), 'rb').read())]
        self.assertEqual([a[0] for a in pv['after']], real)


class TestDiscovery(Base):
    def test_scan_known_tracks_reads_names_and_versions(self):
        for sub, name in (('', 'DS Airship Fortress'), ('1', 'SW2 Airship Fortress')):
            p = os.path.join(self.gd, TID, sub); os.makedirs(p)
            open(os.path.join(p, 'ldb.pul'), 'wb').write(rr.new_ldb(TID, name))
        self.assertEqual(rr.scan_known_tracks(self.gd), {TID: 'DS Airship Fortress', f'{TID}/1': 'SW2 Airship Fortress'})

    def test_versions_and_slot_shortlist(self):
        lookup = os.path.join(self.d, 'lookup'); experts = os.path.join(self.d, 'ExpertsRT'); os.makedirs(lookup); os.makedirs(experts)
        open(os.path.join(lookup, 'FolderToTrackNameRT.txt'), 'w').write('\\c{red}DS \\c{off}Track A = AAAAAAAA\nTrack B = BBBBBBBB\nTrack C = CCCCCCCC\n')
        for name, slot in (('0_150.rkg', 5), ('1_150.rkg', 5), ('2_150.rkg', 9), ('1_v1_150.rkg', 5)):
            open(os.path.join(experts, name), 'wb').write(make_rkg(1, 0, 0, slot=slot)[:0x90])
        self.assertEqual(rr.load_tracks([lookup])['aaaaaaaa'], 'DS Track A')
        self.assertEqual(rr.versioned_tracks([lookup], experts), {'bbbbbbbb/1': 'Track B - newer version 1'})
        idx = rr.slot_index(experts, rr.rt_order([lookup]))
        self.assertEqual(sorted(idx[5]), ['aaaaaaaa', 'bbbbbbbb', 'bbbbbbbb/1']); self.assertEqual(idx[9], ['cccccccc'])


class TestServer(Base):
    def setUp(self):
        super().setUp()
        os.makedirs(os.path.join(self.d, 'x'))
        rr.APP.update(dir=None, tracks={}, game=set(), slots={}, experts=None, lookup=False)
        rr.load_app_state(self.gd)
        self.srv = rr.ThreadingHTTPServer(('127.0.0.1', 0), rr.Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f'http://127.0.0.1:{self.srv.server_address[1]}'

    def tearDown(self):
        self.srv.shutdown(); self.srv.server_close(); super().tearDown()

    def call(self, path, body=None, tok=None):
        req = urllib.request.Request(self.base + path, data=None if body is None else json.dumps(body).encode(),
                                     headers={'X-Token': rr.TOKEN if tok is None else tok, 'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req) as r: return r.status, json.load(r)
        except urllib.error.HTTPError as e: return e.code, json.load(e)

    def test_auth(self):
        self.assertEqual(self.call('/api/state', tok='wrong')[0], 403)
        with self.assertRaises(urllib.error.HTTPError): urllib.request.urlopen(self.base + '/')
        self.assertEqual(urllib.request.urlopen(self.base + f'/?t={rr.TOKEN}').status, 200)

    def test_run_rejects_path_tricks_and_imports_good_ids(self):
        b64 = base64.b64encode(make_rkg(1, 30, 0)).decode()
        code, _ = self.call('/api/run', {'mode': 'import', 'classes': [150], 'items': [{'name': 'a.rkg', 'b64': b64, 'key': '../../evil'}]})
        self.assertEqual(code, 500)
        self.assertFalse(os.path.exists(os.path.join(self.d, 'evil')))
        code, r = self.call('/api/run', {'mode': 'import', 'classes': [150, 200], 'items': [{'name': 'a.rkg', 'b64': b64, 'key': TID}]})
        self.assertEqual(code, 200); self.assertEqual(len(r['results']), 2)
        self.assertTrue(all(x['report'][0]['status'].startswith('OK') for x in r['results']))


if __name__ == '__main__':
    unittest.main()
