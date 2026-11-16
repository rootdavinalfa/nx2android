# A single-file RomFS container (a profile's android.tsv romfs container): the whole RomFS in one file, so the installer pushes one resumable file instead of
# 200,000 small ones. The host reads it back (dm/host/hle_fs.c, cf_*), and the legacy folder RomFS stays the default.
#   header, 36 bytes, little-endian: 'NXZC', u32 version 1, u32 entry count, u64 index offset, u64 data offset, u64 0
#   data: per file, its block table (u64 offsets[n + 1]) and its blocks, as porter/romfs_pack.py stores them (a block as long as its raw bytes is stored, else one zstd frame)
#   index: per file, u16 path length, the path, u64 raw size, u32 block size, u32 block count, u64 block table offset
# The index is last so the data can be written in one pass; the header is rewritten at the end with the index offset.
import concurrent.futures as cf, json, os, struct
from pathlib import Path

from porter import romfs_pack

MAGIC, VERSION = b'NXZC', 1
HDR = struct.Struct('<4sIIQQQ')
ENT = struct.Struct('<H')
BLOCK, LEVEL = romfs_pack.BLOCK, romfs_pack.LEVEL


def is_container(path):
    """True when path is a container this reader knows (a short or foreign file is not)"""
    try:
        with open(path, 'rb') as f: h = f.read(HDR.size)
    except OSError:
        return False
    if len(h) != HDR.size: return False
    magic, ver, n, idx, data, _ = HDR.unpack(h)
    return magic == MAGIC and ver == VERSION and idx >= HDR.size and data >= HDR.size


def _pack_one(src, out, zstd):
    """src's block table and blocks at out's current position → (raw size, block size, block count, block table offset)"""
    size = src.stat().st_size; n = (size + BLOCK - 1) // BLOCK   # 0 for an empty file: an empty block table, no blocks
    table = out.tell(); out.write(b'\0' * (8 * (n + 1)))
    offs = [table + 8 * (n + 1)]
    with open(src, 'rb') as f:
        for _ in range(n):
            raw = f.read(BLOCK)
            if not raw: raise OSError(f'{src}: shorter than its size')
            z = zstd.compress(raw, LEVEL)
            out.write(z if len(z) < len(raw) else raw); offs.append(offs[-1] + min(len(z), len(raw)))
    end = out.tell(); out.seek(table); out.write(struct.pack(f'<{n + 1}Q', *offs)); out.seek(end)
    return size, BLOCK, n, table


def pack(src, dst, jobs=8, progress=None):
    """src: an extracted RomFS; dst: the container (replaced). progress(done bytes, total bytes). Returns (files, container bytes).
    A state file beside dst (dst.json) lets a rebuild copy the blocks of unchanged files from the previous container instead of compressing them again."""
    zstd = romfs_pack.zstd_mod()
    if not zstd: raise RuntimeError(f'Python {__import__("sys").version_info[0]}.{__import__("sys").version_info[1]} has no compression.zstd (3.14+): pip install backports.zstd')
    src, dst = Path(src), Path(dst)
    sf = dst.with_name(dst.name + '.json')
    try: state = json.loads(sf.read_text(encoding='utf-8'))
    except (OSError, ValueError): state = {}
    old = None
    if state and dst.is_file():
        try: old = open(dst, 'rb')
        except OSError: old = None
    files = sorted(p for p in src.rglob('*') if p.is_file())
    total = sum(p.stat().st_size for p in files); done = 0; entries = []
    tmp = dst.with_name(dst.name + '.partial')   # a failed pack leaves no half container at dst
    with open(tmp, 'wb') as out:
        out.write(b'\0' * HDR.size); data = HDR.size
        for p in files:
            rel = p.relative_to(src).as_posix(); st = p.stat(); key = [st.st_size, st.st_mtime_ns]
            o = state.get(rel)
            if old and o and o.get('src') == key and o.get('n') is not None:   # unchanged: copy the blocks from the previous container
                n = o['n']; raw = o['raw']; bs = o['bs']; table = out.tell()
                old.seek(o['table']); old_off = struct.unpack(f'<{n + 1}Q', old.read(8 * (n + 1)))
                out.write(b'\0' * (8 * (n + 1))); new_data = table + 8 * (n + 1)
                old.seek(o['data']); left = o['len']
                while left:
                    chunk = old.read(min(left, 1 << 20))
                    if not chunk: raise OSError(f'{dst}: the previous container is shorter than its state')
                    out.write(chunk); left -= len(chunk)
                end = out.tell(); out.seek(table); out.write(struct.pack(f'<{n + 1}Q', *(new_data + (x - o['data']) for x in old_off))); out.seek(end)
                length = o['len']
            else:
                raw, bs, n, table = _pack_one(p, out, zstd)
                length = out.tell() - (table + 8 * (n + 1))
            entries.append((rel, raw, bs, n, table, length, key)); done += raw
            if progress: progress(done, total)
        index = out.tell()
        for rel, raw, bs, n, table, length, key in entries:
            b = rel.encode('utf-8'); out.write(ENT.pack(len(b))); out.write(b); out.write(struct.pack('<QIIQ', raw, bs, n, table))
        out.seek(0); out.write(HDR.pack(MAGIC, VERSION, len(entries), index, data, 0))
    if old: old.close()
    tmp.replace(dst)
    sf.write_text(json.dumps({rel: {'src': key, 'raw': raw, 'bs': bs, 'n': n, 'table': table, 'data': table + 8 * (n + 1), 'len': length}
                              for rel, raw, bs, n, table, length, key in entries}), encoding='utf-8')
    return len(entries), dst.stat().st_size


def read_index(container):
    """{path: (raw size, block size, block count, block table offset)}"""
    with open(container, 'rb') as f:
        h = f.read(HDR.size)
        if len(h) != HDR.size: raise ValueError('not a RomFS container')
        magic, ver, n, idx, data, _ = HDR.unpack(h)
        if magic != MAGIC or ver != VERSION: raise ValueError('not a RomFS container')
        f.seek(idx); out = {}
        for _ in range(n):
            (L,) = ENT.unpack(f.read(ENT.size)); rel = f.read(L).decode('utf-8')
            raw, bs, nb, table = struct.unpack('<QIIQ', f.read(24)); out[rel] = (raw, bs, nb, table)
        return out


def unpack(container, dst):
    """The container's files into dst (tests; the host reads them block by block). Returns the file count"""
    zstd = romfs_pack.zstd_mod(); dst = Path(dst); dst.mkdir(parents=True, exist_ok=True)
    idx = read_index(container)
    with open(container, 'rb') as f:
        for rel, (raw, bs, n, table) in idx.items():
            f.seek(table); offs = struct.unpack(f'<{n + 1}Q', f.read(8 * (n + 1)))
            out = dst / rel; out.parent.mkdir(parents=True, exist_ok=True)
            with open(out, 'wb') as o:
                for i in range(n):
                    f.seek(offs[i]); b = f.read(offs[i + 1] - offs[i]); want = min(bs, raw - i * bs)
                    if len(b) == want: o.write(b)
                    elif zstd: o.write(zstd.decompress(b))
                    else: raise RuntimeError('a compressed block needs compression.zstd (Python 3.14+) or backports.zstd')
    return len(idx)
