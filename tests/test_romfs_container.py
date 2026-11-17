import struct, tempfile, unittest
from pathlib import Path
from porter import romfs_container as rc


class ContainerTest(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as t:
            src, dst, out = Path(t) / 'src', Path(t) / 'romfs.nxz', Path(t) / 'out'
            (src / 'a').mkdir(parents=True); (src / 'a' / 'b.bin').write_bytes(b'x' * 300_000)
            (src / 'c.txt').write_bytes(b'hello')
            files, size = rc.pack(src, dst)
            self.assertEqual(files, 2); self.assertTrue(rc.is_container(dst))
            self.assertFalse((dst.parent / (dst.name + '.partial')).exists())
            self.assertEqual(rc.unpack(dst, out), 2)
            self.assertEqual((out / 'a' / 'b.bin').read_bytes(), b'x' * 300_000)
            self.assertEqual((out / 'c.txt').read_bytes(), b'hello')

    def test_corrupt_header_is_not_a_container(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / 'bad.nxz'; p.write_bytes(b'NXZC' + b'\0' * 8)
            self.assertFalse(rc.is_container(p))

    def test_empty_file_round_trips(self):
        with tempfile.TemporaryDirectory() as t:
            src, dst, out = Path(t) / 'src', Path(t) / 'romfs.nxz', Path(t) / 'out'
            src.mkdir(); (src / 'empty.bin').write_bytes(b'')
            files, size = rc.pack(src, dst)
            self.assertEqual(files, 1)
            self.assertEqual(rc.unpack(dst, out), 1)
            self.assertEqual((out / 'empty.bin').read_bytes(), b'')

    def test_index_entry_order(self):
        with tempfile.TemporaryDirectory() as t:
            src, dst = Path(t) / 'src', Path(t) / 'romfs.nxz'
            src.mkdir(); (src / 'a.bin').write_bytes(b'x' * 10)
            rc.pack(src, dst)
            data = dst.read_bytes()
            magic, ver, n, idx, data_off, _ = rc.HDR.unpack_from(data)
            pos = idx
            (L,) = rc.ENT.unpack_from(data, pos); pos += rc.ENT.size
            self.assertEqual(data[pos:pos + L], b'a.bin')   # the path comes first, then raw/bs/n/table (hle_fs.c cf_open)
            pos += L
            raw, bs, nb, table = struct.unpack_from('<QIIQ', data, pos)
            self.assertEqual(raw, 10)

    def test_rebuild_reuses_unchanged_blocks(self):
        with tempfile.TemporaryDirectory() as t:
            src, dst = Path(t) / 'src', Path(t) / 'romfs.nxz'
            src.mkdir(); (src / 'a.bin').write_bytes(b'x' * 300_000)
            rc.pack(src, dst); first = dst.read_bytes()
            rc.pack(src, dst); second = dst.read_bytes()
            self.assertEqual(first, second)   # the cache copied the blocks, the container is the same
            self.assertTrue((dst.parent / (dst.name + '.json')).is_file())

    def test_changed_file_is_repacked(self):
        with tempfile.TemporaryDirectory() as t:
            src, dst, out = Path(t) / 'src', Path(t) / 'romfs.nxz', Path(t) / 'out'
            src.mkdir(); p = src / 'a.bin'; p.write_bytes(b'x' * 300_000)
            rc.pack(src, dst); first = dst.read_bytes()
            p.write_bytes(b'y' * 300_000)   # same size, new content and mtime
            rc.pack(src, dst); second = dst.read_bytes()
            self.assertNotEqual(first, second)
            rc.unpack(dst, out)
            self.assertEqual((out / 'a.bin').read_bytes(), b'y' * 300_000)


if __name__ == '__main__':
    unittest.main()
