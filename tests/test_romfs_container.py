import tempfile, unittest
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


if __name__ == '__main__':
    unittest.main()
