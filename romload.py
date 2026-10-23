"""The ROM side of the area sources: every level and area of a ROM, read from the ROM alone (no Mupen, no savestate).

    import romload
    for a in romload.list_areas(rom_path): print(a.level, a.area, a.name, a.notes)
    src = romload.RomAreaSource(rom_path, level, area)
    src.tris                        # the static collision, as stroopmap.Area.tris
    ram, scene = src.visual()       # geodecode.Ram-style memory and the batch dict render_textured.export takes

How it works (the approach of STROOP Next's Game/RomLevels.cs and Game/HackLevels.cs, by hugou74130, re-implemented
here in Python; that repository has no license, so no code was copied):
  * the boot level scripts are not run: the intro script's EXIT_AND_EXECUTE to segment 0x15 names the "scripts"
    segment in the ROM, whose first script (level_main_scripts_entry) loads the common segments (behaviours 0x13,
    common geo 0x16/0x17, MIO0 groups 0x03/0x04) and the models every level shares; the level table in that segment
    (JUMP_IF(level == N) -> EXECUTE(seg, rom start, rom end, entry)) gives each level's script;
  * a small interpreter (class VM) runs a level script as level_script.c does: LOAD_RAW / LOAD_MIO0 (with an MIO0
    decoder) place ROM data in synthetic memory at a segment, JUMP / JUMP_LINK / RETURN / EXECUTE / EXIT /
    JUMP_IF / LOOP_* / SET_REG / GET_OR_SET follow the script (a register set by CALL is unknown and takes no
    conditional jump), AREA ... END_AREA collect each area's geo layout, TERRAIN, ROOMS, MACRO_OBJECTS, OBJECTs,
    WARP_NODEs; LOAD_MODEL_FROM_GEO / FROM_DL / (0x23) fill the model table;
  * memory is a set of byte blocks at synthetic KSEG0 addresses with a 32-entry segment table (class RomRam has the
    geodecode.Ram interface: va, u32, s16, f32, raw); blocks are decompressed when first read;
  * collision: romcol.py (the area's TERRAIN, the static triangles with ROM Manager's ELB shift) plus the collision
    models of the area's objects whose behaviour script loads collision data, placed at their OBJECT position,
    rotation and scale;
  * visuals: geolayout.py (the area's geo layout walked into display lists, run by geodecode.Gfx) plus the objects the
    user's rule keeps: collision objects, and grabbable objects that don't move, drawn with their model at their
    OBJECT placement.
  * course names come from segment 2 (found as the MIO0 block holding the course-name table), through the level ->
    course table in the ROM's code; a level without a course name is called Castle / Castle Grounds / ... or "Level N".

What it does not do (listed in the notes of an area): special objects (doors, trees, the terrain's 0x43 section),
macro objects, objects whose behaviour it cannot read, MIO0 or segment loads it cannot satisfy.
"""
import bisect
import dataclasses
import os
import re
import struct
import sys
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import numpy as np                                                            # noqa: E402

import geodecode                                                              # noqa: E402
import geolayout                                                              # noqa: E402
import romcol                                                                 # noqa: E402
import sm64io                                                                 # noqa: E402

KSEG0 = 0x80000000


class MemError(LookupError):
    """A read of memory nothing was loaded into (an unloaded segment, or past the end of a block)."""


# ------------------------------------------------------------------ MIO0
def mio0_decode(data, off=0):
    """Decompress the MIO0 block at `off` (header: 'MIO0', length, compressed offset, uncompressed offset)."""
    if data[off:off + 4] != b'MIO0':
        raise ValueError(f'no MIO0 header at ROM {off:#x}')
    n, co, uo = struct.unpack('>III', data[off + 4:off + 16])
    out = bytearray()
    bp, cp, up = off + 16, off + co, off + uo
    flags, mask = 0, 0
    append, extend = out.append, out.extend
    ln_out = 0
    while ln_out < n:
        if mask == 0:
            flags = int.from_bytes(data[bp:bp + 4], 'big')
            bp += 4
            mask = 0x80000000
        if flags & mask:
            append(data[up])
            up += 1
            ln_out += 1
        else:
            v = (data[cp] << 8) | data[cp + 1]
            cp += 2
            ln = (v >> 12) + 3
            dist = (v & 0xFFF) + 1
            s = ln_out - dist
            if dist >= ln:
                extend(out[s:s + ln])
            else:
                for k in range(ln):
                    append(out[s + k])
            ln_out += ln
        mask >>= 1
    return bytes(out[:n])


def mio0_size(data, off):
    return struct.unpack('>I', data[off + 4:off + 8])[0] if data[off:off + 4] == b'MIO0' else 0


# ------------------------------------------------------------------ the ROM
_ROMS = {}
# The engine segment (decomp src/engine) of a US-layout ROM: RAM 0x80378800 is loaded from ROM 0xF5580. The ELB
# sites are the nops after the coordinate truncations in find_wall_collisions (two), find_ceil (two), find_floor (two).
ELB_ENGINE_RAM, ELB_ENGINE_ROM = 0x80378800, 0xF5580
ELB_SITES = (0x80380EB0, 0x80380EC8, 0x803812A0, 0x803812C8, 0x8038193C, 0x80381964)


class Rom:
    """A ROM file in big-endian (z64) order. Use Rom.open(path): one instance per file."""

    def __init__(self, path):
        self.path = os.path.abspath(path)
        raw = open(path, 'rb').read()
        self.data = sm64io.z64(raw)
        if self.data[:4] != b'\x80\x37\x12\x40':
            raise ValueError(f'{path}: not an N64 ROM')
        self.name = self.data[0x20:0x34].decode('ascii', 'replace').rstrip(' \x00')
        self.crc1 = struct.unpack('>I', self.data[0x10:0x14])[0]
        self.mem = Memory(self)
        self._levels = {}
        self._table = None
        self._prelude = None
        self._names = False
        self._profile = None

    @classmethod
    def open(cls, path):
        """The Rom of `path`, read once and kept while the file is unchanged."""
        key = os.path.abspath(path)
        st = os.stat(key)
        hit = _ROMS.get(key)
        if hit is None or hit[0] != (st.st_mtime_ns, st.st_size):
            hit = ((st.st_mtime_ns, st.st_size), cls(key))
            _ROMS[key] = hit
        return hit[1]

    def u32(self, a):
        return struct.unpack('>I', self.data[a:a + 4])[0]

    def ucode(self):
        """'F3D' | 'F3DEX' | 'F3DEX2': the microcode this game's display lists use (geodecode.detect_ucode on the
        ROM's RSP microcode text; F3D when geodecode can't tell)."""
        if getattr(self, '_ucode', None) is None:
            try:
                self._ucode = geodecode.detect_ucode(self.data) if hasattr(geodecode, 'detect_ucode') else 'F3D'
            except Exception:                                                  # noqa: BLE001
                self._ucode = 'F3D'
        return self._ucode

    # ---- identification (ELB, a name for the map set)
    @property
    def profile(self):
        """What sm64io.identify knows of this ROM, or None."""
        if self._profile is None:
            self._profile = sm64io.identify(self.path) or False
        return self._profile or None

    def detect_elb(self):
        """(shift or None, how) from the collision code itself. ROM Manager's extended boundaries put `sra r, r, k`
        (the coordinate scaled down, k = 2 for 4x) where vanilla US has a nop, at six places in find_wall_collisions,
        find_ceil and find_floor (surface_collision.c; measured 2026-10-23 on SM64 Island 1.0 / 1.1, The Lost Dreams and
        Shining Stars Repainted, none on US, 74 Ex, Star Road). None when the ROM isn't laid out like US there."""
        if len(self.data) < ELB_ENGINE_ROM + 0x10000:
            return None, 'ROM too small for the US layout'
        ks = set()
        for a in ELB_SITES:
            off = a - ELB_ENGINE_RAM + ELB_ENGINE_ROM
            w = struct.unpack('>I', self.data[off:off + 4])[0]
            rt, rd, k, funct = (w >> 16) & 31, (w >> 11) & 31, (w >> 6) & 31, w & 0x3F
            if w == 0:
                ks.add(0)
            elif w >> 26 == 0 and funct == 3 and rt == rd and k and not (w >> 21) & 31:     # sra rd, rd, k
                ks.add(k)
            else:
                return None, 'the collision code is not laid out like US here'
        if len(ks) != 1:
            return None, 'the collision code shifts some coordinates and not others'
        k = ks.pop()
        return k, ('ROM Manager extended boundaries in the collision code (coordinates >> %d)' % k if k else
                   'vanilla collision code (no extended boundaries)')

    @property
    def elb_shift(self):
        """(shift, how): 0 vanilla, 2 = ROM Manager's 4x extended boundaries, read off the collision code
        (detect_elb); the workspace's ROM table only when the code can't tell (a ROM not laid out like US)."""
        k, how = self.detect_elb()
        if k is not None:
            return k, how
        p = self.profile
        if p is not None and p.elb is not None:
            return int(p.elb), f'{p.title or self.name}: {p.elb_source}'
        return 0, f'ELB unknown for this ROM ({how}), assuming normal level boundaries (pass shift= to override)'

    @property
    def hack_name(self):
        """A name for the hack's map set: the header's, else the file name's, cleaned."""
        p = self.profile
        title = p.title if p is not None and p.title else self.name
        import sources                                 # a header that doesn't name the hack: the file name does
        if sources.is_generic_title(title):
            title = os.path.splitext(os.path.basename(self.path))[0]
        return sources.clean_hack_name(title)

    # ---- the scripts segment and its level table
    def scripts_segment(self):
        """(rom start, rom end) of the segment-0x15 scripts, from the intro script's EXIT_AND_EXECUTE to it."""
        d = self.data
        i = 0
        best = None
        while True:
            i = d.find(b'\x01\x10\x00\x15', i)
            if i < 0:
                break
            s, e, ent = struct.unpack('>III', d[i + 4:i + 16])
            if ent >> 24 == 0x15 and s < e <= len(d) and (ent & 0xFFFFFF) < e - s:
                # level_main_scripts_entry begins with LOAD_MIO0(0x04, ...), LOAD_MIO0(0x03, ...)
                head = d[s + (ent & 0xFFFFFF):s + (ent & 0xFFFFFF) + 16]
                if head[0] == 0x18 and head[3] == 0x04:
                    return s, e, ent & 0xFFFFFF
                best = best or (s, e, ent & 0xFFFFFF)
            i += 1
        if best is None:
            raise ValueError(f'{self.path}: the segment-0x15 script (EXIT_AND_EXECUTE) was not found')
        return best

    def level_table(self):
        """{level: (segment, rom start, rom end, entry)} read from the scripts segment, as RomLevels.Scan does:
        JUMP_IF(OP_EQ, level) -> a script that holds EXECUTE(segment, start, end, entry)."""
        if self._table is None:
            s, e, _ent = self.scripts_segment()
            d = self.data
            out = {}
            for off in range(0, e - s - 12, 4):
                a = s + off
                if d[a] != 0x0C or d[a + 1] != 0x0C or d[a + 2] != 2:
                    continue
                lvl, tgt = struct.unpack('>II', d[a + 4:a + 12])
                if tgt >> 24 != 0x15 or not 1 <= lvl <= 63 or lvl in out:
                    continue
                ex = s + (tgt & 0xFFFFFF)
                if ex + 16 > len(d) or d[ex] != 0x00 or d[ex + 1] != 0x10:
                    continue
                es, ee, ent = struct.unpack('>III', d[ex + 4:ex + 16])
                out[lvl] = (struct.unpack('>h', d[ex + 2:ex + 4])[0] & 0x1F, es, ee, ent, tgt)
            self._table = out
        return self._table

    # ---- text: course names
    def segment2(self):
        """The decompressed segment 2 (fonts, texts, course names) the game loads. ROM Manager hacks keep their own
        segment 2 as an MIO0 block at 0x800000 (the start of the extended ROM) and leave vanilla's at 0x108A40 as an
        unused leftover of the same size; setup_game_memory's load (LUI/ADDIU pair at ROM 0x3AC0 in US) is patched to
        the new one. So of the MIO0 blocks that hold a course-name table, the one the code references wins; if several
        are referenced (0x108A40 is also the previous segment's end, at 0x3AA8), the highest ROM offset (the relocated
        copy); without any reference, the size closest to vanilla's. (Checked 2026-10-22: TLD, SS1R, 74E, Star Road,
        Island 1.1.)"""
        if getattr(self, '_seg2', None) is not None:
            return self._seg2 or None
        d = self.data
        found = []
        i = 0
        while True:
            i = d.find(b'MIO0', i)
            if i < 0:
                break
            n = mio0_size(d, i)
            if 0x8000 <= n <= 0x60000:
                try:
                    u = mio0_decode(d, i)
                    names = course_names(u)
                except Exception:                                          # noqa: BLE001
                    names = None
                if names:
                    found.append((i, n, u, names))
            i += 4
        self._seg2, self._names, self._seg2_rom = b'', None, None
        if found:
            refd = [f for f in found if code_loads(d, f[0])]
            pick = max(refd, key=lambda f: f[0]) if refd else min(found, key=lambda f: abs(f[1] - 0x18A0E))
            self._seg2_rom, _, self._seg2, self._names = pick
        return self._seg2 or None

    def course_names(self):
        """The hack's course names (from its segment 2), or None."""
        self.segment2()
        return self._names or None

    def names_untrusted(self):
        """True when the course names found are vanilla's but the ROM also holds other numbered course names (" 1 ..."
        text that isn't vanilla's): the table found is probably a leftover. A safety net since segment2() follows the
        code's reference (no ROM here trips it any more)."""
        if getattr(self, '_untrusted', None) is None:
            self._untrusted = False
            names = self.course_names()
            if names and clean_name(names[0]).lower() == 'bob-omb battlefield' and not self.is_official():
                for m in re.finditer(re.escape(bytes((0x9E, 0x01, 0x9E))), self.data):
                    t = decode_text(self.data, m.start())
                    if t and clean_name(t) and clean_name(t).lower() != 'bob-omb battlefield':
                        self._untrusted = True
                        break
        return self._untrusted

    def is_official(self):
        """True for the four retail builds."""
        p = self.profile
        return p is not None and p.kind == 'official'

    # ---- level -> course number
    def level_course_table(self):
        """The level -> course number table of the ROM's code (38 bytes, vanilla order), found by its vanilla bytes
        (at least 30 of 38 equal), else the vanilla table."""
        if getattr(self, '_l2c', None) is None:
            self._l2c = list(VANILLA_LEVEL_COURSE)
            d = self.data
            want = bytes(VANILLA_LEVEL_COURSE)
            lo, hi = 0x1000, min(len(d), 0x120000)
            i = d.find(want, lo, hi)
            if i >= 0:
                self._l2c = list(d[i:i + 38])
            else:
                # a hack that changed a few entries: look for a window with most entries equal
                arr = np.frombuffer(d[lo:hi], dtype=np.uint8)
                w = np.frombuffer(want, dtype=np.uint8)
                score = np.zeros(len(arr) - 38, dtype=np.int16)
                for k in range(38):
                    score += (arr[k:len(arr) - 38 + k] == w[k])
                j = int(score.argmax())
                if score[j] >= 30:
                    self._l2c = list(d[lo + j:lo + j + 38])
        return self._l2c


# content_signature() of each level of the US 1.0 ROM (collision triangles, drawn triangles with their colours and
# textures, per area): a level of a hack with the same signature is vanilla in content (a leftover, or a level the
# hack kept), even when its ROM bytes were recompressed or moved. Regenerate: python romload.py --vanilla-signatures
VANILLA_SIGNATURE = {4: 3689518140, 5: 1921060547, 6: 357628780, 7: 1493527937, 8: 4023450965, 9: 1911592061,
                     10: 2787089497, 11: 784601237, 12: 3548146405, 13: 1883339245, 14: 1842172159, 15: 4163590451,
                     16: 3588673259, 17: 2517654173, 18: 3335005057, 19: 75139727, 20: 3399135095, 21: 4125504317,
                     22: 3364974768, 23: 3406389805, 24: 117341327, 25: 1169454965, 26: 331549971, 27: 3330350387,
                     28: 1748086734, 29: 2475323322, 30: 435444973, 31: 1575153315, 33: 2236902531, 34: 341724754,
                     36: 3014299212}
VANILLA_LEVEL_COURSE = [0, 0, 0, 5, 4, 0, 6, 8, 1, 10, 11, 3, 13, 14, 15, 0, 16, 22, 17, 24, 18, 7, 9, 2, 25, 0, 19,
                        20, 21, 16, 23, 0, 17, 18, 0, 12, 0, 0]
LEVEL_NAMES = {4: 'Big Boo\'s Haunt', 5: 'Cool, Cool Mountain', 6: 'Castle', 7: 'Hazy Maze Cave',
               8: 'Shifting Sand Land', 9: 'Bob-omb Battlefield', 10: 'Snowman\'s Land', 11: 'Wet-Dry World',
               12: 'Jolly Roger Bay', 13: 'Tiny-Huge Island', 14: 'Tick Tock Clock', 15: 'Rainbow Ride',
               16: 'Castle Grounds', 17: 'Bowser in the Dark World', 18: 'Vanish Cap under the Moat',
               19: 'Bowser in the Fire Sea', 20: 'Secret Aquarium', 21: 'Bowser in the Sky', 22: 'Lethal Lava Land',
               23: 'Dire, Dire Docks', 24: 'Whomp\'s Fortress', 25: 'The End', 26: 'Castle Courtyard',
               27: 'Princess\'s Secret Slide', 28: 'Cavern of the Metal Cap', 29: 'Tower of the Wing Cap',
               30: 'Bowser 1 Arena', 31: 'Wing Mario over the Rainbow', 33: 'Bowser 2 Arena', 34: 'Bowser 3 Arena',
               36: 'Tall, Tall Mountain'}
CHARMAP = {0x9E: ' ', 0x9F: '-', 0x3E: "'", 0x3F: '.', 0x6F: ',', 0xE1: '(', 0xE3: ')', 0xE4: '+', 0xE5: '&',
           0xE6: ':', 0xF2: '!', 0xF4: '?', 0xD0: '/', 0x54: '"'}


def code_loads(d, addr, lo_=0x1000, hi_=0x100000):
    """True when the game code (ROM lo_..hi_) builds the address `addr` with a LUI + ADDIU (or ORI) pair on one
    register within 8 instructions, as setup_game_memory does for a segment's ROM start."""
    lo = addr & 0xFFFF
    hi_addiu = ((addr >> 16) + (1 if lo & 0x8000 else 0)) & 0xFFFF       # ADDIU sign-extends its immediate
    hi_ori = (addr >> 16) & 0xFFFF
    words = np.frombuffer(d[lo_:min(hi_, len(d)) // 4 * 4], dtype='>u4')
    lui = np.flatnonzero(((words >> 26) == 0x0F) & (((words & 0xFFFF) == hi_addiu) | ((words & 0xFFFF) == hi_ori)))
    for k in lui:
        w = int(words[k])
        rt = (w >> 16) & 31
        for v in words[k + 1:k + 9]:
            v = int(v)
            op, rs, imm = v >> 26, (v >> 21) & 31, v & 0xFFFF
            if rs == rt and imm == lo and ((op == 0x09 and (w & 0xFFFF) == hi_addiu) or
                                           (op == 0x0D and (w & 0xFFFF) == hi_ori)):
                return True
    return False


def decode_text(buf, off):
    """The game's text at `off` in `buf` (its own character set), or None."""
    out = []
    for k in range(40):
        if off + k >= len(buf):
            return None
        b = buf[off + k]
        if b == 0xFF:
            return ''.join(out)
        if b <= 0x09:
            out.append(chr(48 + b))
        elif 0x0A <= b <= 0x23:
            out.append(chr(65 + b - 0x0A))
        elif 0x24 <= b <= 0x3D:
            out.append(chr(97 + b - 0x24))
        else:
            out.append(CHARMAP.get(b, '·'))
    return None


def course_names(seg2, at=None):
    """The course-name table of segment 2 (25+ segmented pointers to texts, the first starting with "1"), as HackLevels.CourseNames finds it.
    `at`: check only the table at that offset."""
    n = len(seg2)
    for off in (range(0, min(n - 100, 0x40000), 4) if at is None else [at]):
        if off < 0 or off + 100 > n:
            continue
        if seg2[off] != 2:
            continue
        names = []
        for i in range(30):
            if off + 4 * i + 4 > n:
                break
            p = struct.unpack('>I', seg2[off + 4 * i:off + 4 * i + 4])[0]
            if p >> 24 != 2:
                break
            t = decode_text(seg2, p & 0xFFFFFF)
            if t is None:
                break
            names.append(t)
        if len(names) >= 25 and names[0].lstrip().startswith('1') and sum('·' in x for x in names) <= 2:
            return names
    return None


def clean_name(s):
    """A course name as the game stores it, made readable (number off, title case)."""
    s = s.strip().lstrip('0123456789').strip()
    s = re.sub(r'\s+', ' ', s)
    if s.isupper():
        small = {'of', 'the', 'in', 'under', 'over', 'and', 'a', 'to'}
        words = s.title().replace("'S", "'s").split(' ')
        s = ' '.join(w if i == 0 or w.lower() not in small else w.lower() for i, w in enumerate(words))
        s = re.sub(r'-([A-Z])', lambda m: '-' + m.group(1).lower() if s.startswith('Bob-') else m.group(0), s)
    return s


# ------------------------------------------------------------------ memory
class Block:
    """A stretch of ROM loaded into a segment (raw or MIO0), decoded when first read."""
    __slots__ = ('base', 'size', 'key', '_data', 'rom')

    def __init__(self, base, size, key, rom):
        self.base, self.size, self.key, self.rom, self._data = base, size, key, rom, None

    @property
    def data(self):
        """The block's bytes, decompressed on first use."""
        if self._data is None:
            start, end, mio0 = self.key
            d = self.rom.data
            self._data = mio0_decode(d, start) if mio0 else d[start:end]
        return self._data


class Memory:
    """ROM ranges placed at synthetic KSEG0 addresses; one block per (rom start, rom end, compressed) key."""

    def __init__(self, rom):
        self.rom = rom
        self._next = 0x80100000
        self.blocks = []
        self.bases = []
        self._by_key = {}

    def map(self, start, end, mio0):
        """The memory base of the ROM range, loading it once."""
        key = (start, end, bool(mio0))
        base = self._by_key.get(key)
        if base is not None:
            return base
        d = self.rom.data
        if mio0:
            size = mio0_size(d, start)
            if not size:
                raise MemError(f'no MIO0 block at ROM {start:#x}')
        else:
            end = min(end, len(d))
            size = max(end - start, 0)
            key = (start, end, False)
        base = self._next
        self._next += ((size + 0xFF) & ~0xFF) + 0x100
        self.blocks.append(Block(base, size, key, self.rom))
        self.bases.append(base)
        self._by_key[key] = base
        self._by_key[(start, end, bool(mio0))] = base
        return base

    def block_at(self, a):
        """The block that holds memory address `a`."""
        i = bisect.bisect_right(self.bases, a) - 1
        if i < 0:
            raise MemError(f'address {a:#x} is not mapped')
        b = self.blocks[i]
        if a - b.base > b.size:
            raise MemError(f'address {a:#x} is past the end of its block ({b.base:#x}+{b.size:#x})')
        return b

    def raw(self, a, n):
        b = self.block_at(a)
        off = a - b.base
        d = b.data
        out = d[off:off + n]
        if len(out) < n:
            out = out + bytes(n - len(out))              # zero beyond the data (a texture ending a block)
        return out


class RomRam:
    """The geodecode.Ram interface over a Memory and a segment table (KSEG0 base address or None per segment)."""

    def __init__(self, mem, segs):
        self.mem, self.seg = mem, segs

    def snapshot(self):
        """A copy of the segment table, so a later load does not move earlier pointers."""
        return RomRam(self.mem, list(self.seg))

    def va(self, a):
        """Segmented address `a` as a memory address."""
        if a >= KSEG0:
            return a
        s = (a >> 24) & 0x1F
        base = self.seg[s]
        if base is None:
            raise MemError(f'segment {s:#x} is not loaded (address {a:#010x})')
        return base + (a & 0xFFFFFF)

    @property
    def rom_bytes(self):
        """The ROM (z64 order): geodecode.detect_ucode reads the RSP microcode text from it."""
        return self.mem.rom.data

    def raw(self, a, n): return self.mem.raw(a, n)
    def u8(self, a): return self.mem.raw(a, 1)[0]
    def u16(self, a): return struct.unpack('>H', self.mem.raw(a, 2))[0]
    def u32(self, a): return struct.unpack('>I', self.mem.raw(a, 4))[0]
    def s16(self, a): return struct.unpack('>h', self.mem.raw(a, 2))[0]
    def f32(self, a): return struct.unpack('>f', self.mem.raw(a, 4))[0]


# ------------------------------------------------------------------ level scripts
@dataclasses.dataclass
class ObjDef:
    """An OBJECT command of a level script: model, position, angle, behaviour."""
    acts: int
    model: int
    pos: tuple
    angle: tuple          # degrees as stored in the script (s16)
    bparam: int
    bhv: int              # segmented behaviour script address


@dataclasses.dataclass
class ModelDef:
    """A LOAD_MODEL_FROM_GEO / _FROM_DL command: the model id and its pointer."""
    kind: str             # 'geo' | 'dl'
    ptr: int
    layer: int = 0
    scale: float = 1.0
    segs: list = None     # the segment table when the model was loaded


@dataclasses.dataclass
class AreaDef:
    """An AREA of a level script: its geo layout, collision, objects and segment table."""
    index: int
    geo: int
    segs: list
    terrain: int = 0      # segmented address of the terrain data (0 = none)
    rooms: int = 0
    macro: int = 0
    segs_final: list = None   # the segment table when the area is drawn / loaded for play (see RomLevel)
    seg_e_note: str = ''
    objects: list = dataclasses.field(default_factory=list)
    warps: list = dataclasses.field(default_factory=list)       # (kind, id, dest level, dest area, dest node)


class State:
    """The level script interpreter state: segments, models, the current area and register."""
    def __init__(self, segs=None, models=None):
        self.segs = list(segs) if segs else [None] * 32
        self.models = dict(models) if models else {}
        self.areas = {}
        self.cur = None
        self.reg = None
        self.stack = []
        self.frames = []
        self.level = 0
        self.act = 1
        self.notes = []
        self.mario = None
        self.done = False
        self.loaded = []          # (rom start, rom end, compressed) of every segment load, in order


def _cmd_len(head, pc):
    """A level script command's length (its second byte); a zero length would stop the skip loops advancing."""
    if head[1] == 0:
        raise MemError(f'level script command {head[0]:#04x} at {pc:#010x} has length 0')
    return head[1]


class VM:
    """Runs level scripts (decomp/src/engine/level_script.c) over a Rom's memory; see the module docstring."""

    def __init__(self, rom):
        self.rom = rom
        self.mem = rom.mem

    def load(self, st, seg, start, end, mio0):
        """LOAD_RAW / LOAD_MIO0: map ROM start..end into segment `seg`."""
        if not 0 <= seg < 32:
            return
        try:
            if end < start or start >= len(self.rom.data):
                raise MemError(f'segment {seg:#x}: ROM range {start:#x}-{end:#x} is outside the ROM')
            st.segs[seg] = self.mem.map(start, end, mio0)
            st.loaded.append((start, end, bool(mio0)))
        except (MemError, ValueError) as e:
            st.segs[seg] = None
            st.notes.append(str(e))

    def cond(self, st, op, arg):
        """A JUMP_IF / SKIP_IF condition on the script's register."""
        r = st.reg
        if r is None:
            return None
        return {0: bool(r & arg), 1: not (r & arg), 2: r == arg, 3: r != arg, 4: r < arg, 5: r <= arg, 6: r > arg,
                7: r >= arg}.get(op, False)

    def run(self, st, addr, max_cmds=60000, stop_ops=()):
        """Run from `addr` (segmented) until the script exits; returns the address of a stop_ops command or None."""
        ram = RomRam(self.mem, st.segs)
        try:
            pc = ram.va(addr)
        except MemError as e:
            st.notes.append(str(e))
            return None
        seen = {}
        steps = 0
        while steps < max_cmds and not st.done:
            steps += 1
            try:
                hdr = ram.raw(pc, 2)
                op, ln = hdr
                if ln == 0 or ln & 3 or ln > 0x30:
                    st.notes.append(f'bad level command length at {pc:#x}')
                    break
                c = ram.raw(pc, ln)
            except MemError as e:
                st.notes.append(str(e))
                break
            if op in stop_ops:
                return pc
            key = (pc, len(st.stack), len(st.frames))
            seen[key] = seen.get(key, 0) + 1
            if seen[key] > 40:
                st.notes.append(f'script loop at {pc:#x} left')
                break
            nxt = pc + ln
            s16 = lambda o: struct.unpack('>h', c[o:o + 2])[0]                    # noqa: E731
            u32 = lambda o: struct.unpack('>I', c[o:o + 4])[0]                    # noqa: E731
            try:
                if op in (0x00, 0x01):                                           # LOAD_AND_EXECUTE / EXIT_AND_EXECUTE
                    self.load(st, s16(2) & 0x1F, u32(4), u32(8), False)
                    if op == 0x00:
                        st.frames.append((nxt, st.stack))
                    st.stack = []
                    pc = ram.va(u32(12))
                elif op == 0x02:                                                 # EXIT
                    if not st.frames:
                        st.done = True
                        break
                    pc, st.stack = st.frames.pop()
                elif op in (0x03, 0x04):                                         # SLEEP
                    pc = nxt
                elif op == 0x05:                                                 # JUMP
                    pc = ram.va(u32(4))
                elif op == 0x06:                                                 # JUMP_LINK
                    st.stack.append(nxt)
                    pc = ram.va(u32(4))
                elif op == 0x07:                                                 # RETURN
                    if not st.stack:
                        st.done = True
                        break
                    pc = st.stack.pop()
                elif op == 0x08:                                                 # JUMP_LINK_PUSH_ARG
                    st.stack += [nxt, s16(2)]
                    pc = nxt
                elif op == 0x09:                                                 # JUMP_REPEAT
                    if len(st.stack) < 2:
                        pc = nxt
                    else:
                        val = st.stack[-1]
                        if val == 0:
                            pc = st.stack[-2]
                        elif val - 1 != 0:
                            st.stack[-1] = val - 1
                            pc = st.stack[-2]
                        else:
                            del st.stack[-2:]
                            pc = nxt
                elif op == 0x0A:                                                 # LOOP_BEGIN
                    st.stack += [nxt, 0]
                    pc = nxt
                elif op == 0x0B:                                                 # LOOP_UNTIL (unknown: satisfied)
                    r = self.cond(st, c[2], struct.unpack('>i', c[4:8])[0])
                    if r is None or r:
                        del st.stack[-2:]
                        pc = nxt
                    else:
                        pc = st.stack[-2] if len(st.stack) >= 2 else nxt
                elif op in (0x0C, 0x0D):                                         # JUMP_IF / JUMP_AND_LINK_IF
                    r = self.cond(st, c[2], struct.unpack('>i', c[4:8])[0])
                    if r:
                        if op == 0x0D:
                            st.stack.append(nxt)
                        pc = ram.va(u32(8))
                    else:
                        pc = nxt
                elif op == 0x0E:                                                 # SKIP_IF
                    r = self.cond(st, c[2], struct.unpack('>i', c[4:8])[0])
                    pc = nxt
                    if not r:
                        while True:
                            t = ram.raw(pc, 2)
                            if t[0] not in (0x0F, 0x10):
                                break
                            pc += _cmd_len(t, pc)
                        pc += _cmd_len(ram.raw(pc, 2), pc)
                elif op == 0x0F:                                                 # SKIP
                    pc = nxt
                    while ram.raw(pc, 1)[0] == 0x10:
                        pc += _cmd_len(ram.raw(pc, 2), pc)
                    pc += _cmd_len(ram.raw(pc, 2), pc)
                elif op in (0x11, 0x12):                                         # CALL / CALL_LOOP: a native function
                    st.reg = None
                    pc = nxt
                elif op == 0x13:                                                 # SET_REG
                    st.reg = s16(2)
                    pc = nxt
                elif op in (0x17, 0x18, 0x1A) or (op == 0x10 and ln == 0x0C):
                    # LOAD_RAW / LOAD_MIO0 / MIO0_TEXTURE. ROM Manager's hack (SM64 Island) also sends command 0x10
                    # to its LOAD_RAW handler; with 1 in byte 2 it DMAs the range to the segment's fixed address.
                    self.load(st, s16(2) & 0x1F, u32(4), u32(8), op == 0x18 or op == 0x1A)
                    pc = nxt
                elif op == 0x1F:                                                 # AREA
                    idx = c[2]
                    if idx < 8:
                        st.cur = idx
                        st.areas[idx] = AreaDef(idx, u32(4), list(st.segs))
                    pc = nxt
                elif op == 0x20:                                                 # END_AREA
                    st.cur = None
                    pc = nxt
                elif op in (0x21, 0x22, 0x23):                                   # LOAD_MODEL_FROM_DL / GEO / (scaled DL)
                    v = struct.unpack('>H', c[2:4])[0]
                    model = v & 0xFFF
                    if model < 256:
                        if op == 0x22:
                            st.models[model] = ModelDef('geo', u32(4), 0, 1.0, list(st.segs))
                        else:
                            sc = struct.unpack('>f', c[8:12])[0] if op == 0x23 else 1.0
                            st.models[model] = ModelDef('dl', u32(4), v >> 12, sc, list(st.segs))
                    pc = nxt
                elif op == 0x24:                                                 # OBJECT
                    if st.cur is not None and ln >= 0x18:
                        ang = struct.unpack('>hhh', c[10:16])
                        st.areas[st.cur].objects.append(ObjDef(c[2], c[3], struct.unpack('>hhh', c[4:10]), ang,
                                                               u32(16), u32(20)))
                    pc = nxt
                elif op in (0x26, 0x27, 0x28):                                   # WARP_NODE / PAINTING / INSTANT
                    if st.cur is not None and op != 0x28:
                        st.areas[st.cur].warps.append((op, c[2], c[3], c[4], c[5]))
                    pc = nxt
                elif op == 0x2B:                                                 # MARIO_POS
                    st.mario = (c[2], s16(4), struct.unpack('>hhh', c[6:12]))
                    pc = nxt
                elif op in (0x2E, 0x2F, 0x39):                                   # TERRAIN / ROOMS / MACRO_OBJECTS
                    if st.cur is not None:
                        a = st.areas[st.cur]
                        if op == 0x2E:
                            a.terrain = u32(4)
                        elif op == 0x2F:
                            a.rooms = u32(4)
                        else:
                            a.macro = u32(4)
                    pc = nxt
                elif op == 0x3C:                                                 # GET_OR_SET
                    if c[2] != 0:
                        st.reg = {2: st.act, 3: st.level, 4: None}.get(c[3])
                    pc = nxt
                else:
                    pc = nxt
            except MemError as e:
                st.notes.append(str(e))
                break
        if steps >= max_cmds:
            st.notes.append('level script step limit reached')
        return None


# ------------------------------------------------------------------ levels
class RomLevel:
    """One level run to its end: segments, models and areas."""

    def __init__(self, rom, level):
        self.rom, self.level = rom, level
        tbl = rom.level_table()
        if level not in tbl:
            raise KeyError(f'level {level} is not in the ROM\'s level table')
        self.seg, self.rom_start, self.rom_end, self.entry, self.exec_ptr = tbl[level]
        vm = VM(rom)
        pre = rom.prelude()
        st = State(pre.segs, pre.models)
        st.level = level
        vm.run(st, self.exec_ptr)
        self.state = st
        self.areas = st.areas
        self.models = st.models
        self.notes = list(st.notes)
        self.ram = RomRam(rom.mem, st.segs)              # the segment table at the end of the script
        for a in st.areas.values():
            a.segs_final = list(st.segs)
            self._area_segment_e(a)

    def _area_segment_e(self, a):
        """SM64 Island's (ROM Manager extended) hack loads each area's own data into segment 0x0E when the area
        is entered: the custom function at 0x80403700 DMAs ROM[start, end) read from the 16-byte record
        `a.index` at segment 0x19 + 0x5F00 to 0x80420000, if the word at segment 0x19 + 0x5FFC is 0x4BC9189A. The
        level script's own segment-0x0E load is only the objects chunk. Applied here, so the area's terrain,
        display lists and behaviours read the data the game has when it runs the area."""
        base = a.segs_final[0x19]
        if base is None:
            return
        ram = RomRam(self.rom.mem, a.segs_final)
        try:
            if ram.u32(base + 0x5FFC) != 0x4BC9189A:
                return
            start, end = ram.u32(base + 0x5F00 + 16 * a.index), ram.u32(base + 0x5F04 + 16 * a.index)
        except MemError:
            return
        if 0 < start < end <= len(self.rom.data):
            a.segs_final[0x0E] = self.rom.mem.map(start, end, False)
            a.seg_e_note = f'segment 0x0E = ROM {start:#x}-{end:#x} (area record)'


def _prelude(self):
    """Segments and models set by level_main_scripts_entry (before its level loop)."""
    if self._prelude is None:
        s, e, ent = self.scripts_segment()
        st = State()
        vm = VM(self)
        vm.load(st, 0x15, s, e, False)
        vm.run(st, 0x15000000 + ent, stop_ops=(0x0A, 0x00))
        self._prelude = st
    return self._prelude


Rom.prelude = _prelude


def _level(self, level):
    lv = self._levels.get(level)
    if lv is None:
        lv = self._levels[level] = RomLevel(self, level)
    return lv


Rom.level = _level


# ------------------------------------------------------------------ behaviours (collision objects)
_W = {1: {0x00, 0x01, 0x03, 0x05, 0x06, 0x07, 0x08, 0x09, 0x0A, 0x0B, 0x0D, 0x0E, 0x0F, 0x10, 0x11, 0x12, 0x18, 0x19,
          0x1A, 0x1B, 0x1D, 0x1E, 0x1F, 0x20, 0x21, 0x22, 0x24, 0x25, 0x26, 0x28, 0x2D, 0x32, 0x34, 0x35},
      2: {0x02, 0x04, 0x0C, 0x13, 0x14, 0x15, 0x16, 0x17, 0x23, 0x27, 0x2A, 0x2E, 0x2F, 0x31, 0x33, 0x36, 0x37},
      3: {0x1C, 0x29, 0x2B, 0x2C}, 5: {0x30}}
BHV_WORDS = {op: n for n, ops in _W.items() for op in ops}
INTERACT_GRABBABLE = 0x00000002


@dataclasses.dataclass
class BhvInfo:
    """What a behaviour script says about an object: collision, scale, flags, interaction."""
    collision: int = 0          # segmented address of the collision data (0: none)
    scale: float = 1.0
    grabbable: bool = False
    interact: int = 0           # SET_INTERACT_TYPE
    flags: int = 0              # oFlags from SET_INT / OR_INT / BIT_CLEAR (16-bit immediates)
    animated: bool = False      # LOAD_ANIMATIONS / ANIMATE
    physics: bool = False       # SET_OBJ_PHYSICS (moves under gravity)
    ok: bool = True
    note: str = ''


def read_behavior(ram, bhv, limit=400):
    """Walk a behaviour script from its start (following GOTO and CALL, to BEGIN_LOOP) for what the map needs:
    LOAD_COLLISION_DATA, SCALE, SET_INTERACT_TYPE, animations, physics."""
    info = BhvInfo()
    try:
        pc = ram.va(bhv)
    except MemError as e:
        info.ok, info.note = False, str(e)
        return info
    calls = []
    for _ in range(limit):
        try:
            w0 = ram.u32(pc)
        except MemError as e:
            info.ok, info.note = False, str(e)
            return info
        op = w0 >> 24
        words = BHV_WORDS.get(op)
        if words is None:
            info.ok, info.note = False, f'unknown behaviour command {op:#x}'
            return info
        if op == 0x2A:                                                       # LOAD_COLLISION_DATA
            info.collision = ram.u32(pc + 4)
        elif op == 0x32:                                                     # SCALE(unused, percent)
            info.scale = (w0 & 0xFFFF) / 100.0
        elif op == 0x2F:                                                     # SET_INTERACT_TYPE
            info.interact |= ram.u32(pc + 4)
            if info.interact & INTERACT_GRABBABLE:
                info.grabbable = True
        elif op in (0x10, 0x11, 0x12) and (w0 >> 16) & 0xFF == 1:           # SET_INT / OR_INT / BIT_CLEAR oFlags
            v = w0 & 0xFFFF
            info.flags = v if op == 0x10 else (info.flags | v) if op == 0x11 else (info.flags & ~v)
        elif op in (0x27, 0x28):                                             # LOAD_ANIMATIONS / ANIMATE
            info.animated = True
        elif op == 0x30:                                                     # SET_OBJ_PHYSICS
            info.physics = True
        elif op == 0x04:                                                     # GOTO
            pc = ram.va(ram.u32(pc + 4))
            continue
        elif op == 0x02:                                                     # CALL
            calls.append(pc + 8)
            pc = ram.va(ram.u32(pc + 4))
            continue
        elif op == 0x03:                                                     # RETURN
            if not calls:
                break
            pc = calls.pop()
            continue
        elif op in (0x08, 0x1D, 0x0A, 0x0B):                                 # BEGIN_LOOP / DEACTIVATE / BREAK
            break
        pc += 4 * words
    return info


def keep_rom_object(info, mode='static'):
    """The object rule for a ROM object, decided by geodecode.object_rule from what the
    behaviour script says: collision objects (not hostile ones: Whomp, Thwomp, ...) and grabbable objects that don't
    move. Speed is unknown in the ROM (0)."""
    if not info.ok:
        return False
    if hasattr(geodecode, 'object_rule'):
        why = geodecode.object_rule(collision=bool(info.collision), interact_type=info.interact, flags=info.flags,
                                    animated=info.animated, mode=mode)
        if why in ('collision', 'any'):
            return True
        # without collision: drawn unless it moves; SET_OBJ_PHYSICS (gravity) means it does
        return why in geodecode.OBJECT_KEEPS and not info.physics
    if info.collision:
        return True
    return info.grabbable and not info.animated and not info.physics


# ------------------------------------------------------------------ the public API
@dataclasses.dataclass
class AreaInfo:
    """One area list_areas() found: level, area, name, notes, unchanged."""
    level: int
    area: int
    name: str
    notes: list
    level_name: str = ''
    course: int = 0
    objects: int = 0
    collision_objects: int = 0
    warps: int = 0
    has_terrain: bool = True
    has_geo: bool = True
    unchanged: bool = False       # the level is byte-identical to vanilla US (STROOP ships those maps already)

    def __str__(self):
        return f'level {self.level} area {self.area}: {self.name}' + (f' ({"; ".join(self.notes)})' if self.notes else '')


def level_display_name(rom, level):
    """The hack's own name for a level: its course name (segment 2) through the ROM's level -> course table, else
    the vanilla name, else "Level N"."""
    l2c = rom.level_course_table()
    course = l2c[level - 1] if 1 <= level <= len(l2c) else 0
    names = rom.course_names()
    if names and rom.names_untrusted():
        alt = LEVEL_NAMES.get(level)
        return (f'Level {level} ({alt} slot)' if alt else f'Level {level}'), course
    if names and 1 <= course <= len(names):
        n = clean_name(names[course - 1])
        if n:
            return n, course
        if not rom.is_official():
            return f'Level {level}', course        # the hack left this course unnamed: not vanilla's name
    return LEVEL_NAMES.get(level) or f'Level {level}', course


def level_names(rom, levels):
    """{level: name}: the course name of the level (segment 2 through the ROM's level -> course table), made unique
    (levels of one course get the vanilla level name, or "[level N]", appended)."""
    raw = {lv: level_display_name(rom, lv)[0] for lv in levels}
    out = dict(raw)
    groups = {}
    for lv, n in raw.items():
        groups.setdefault(n.lower(), []).append(lv)
    for lvs in groups.values():
        if len(lvs) > 1:
            for lv in lvs:
                alt = LEVEL_NAMES.get(lv)
                out[lv] = f'{raw[lv]} ({alt})' if alt and alt.lower() != raw[lv].lower() else f'{raw[lv]} [level {lv}]'
    return out


def list_areas(rom_path, levels=None, include_unchanged=None):
    """Every level and area the ROM defines, as [AreaInfo]: the level table is read, each level's script run, and an
    area is listed when its script gave it a geo layout or terrain. Nothing is decompressed beyond the scripts.

    A hack's levels that are byte-identical to the vanilla US ones (the data their scripts load; Island keeps 34 such
    leftovers) are marked `unchanged` and left out unless include_unchanged is True; STROOP ships those maps. The
    default (None) is: leave them out for a hack, list everything for an official ROM."""
    rom = Rom.open(rom_path)
    p = rom.profile
    official = p is not None and p.kind == 'official'
    if include_unchanged is None:
        include_unchanged = official
    out = []
    tbl = rom.level_table()
    want = [lv for lv in sorted(tbl) if levels is None or lv in levels]
    names = level_names(rom, want)
    l2c = rom.level_course_table()
    for lv in want:
        try:
            rl = rom.level(lv)
        except Exception as e:                                               # noqa: BLE001
            out.append(AreaInfo(lv, 0, names[lv], [f'level script failed: {e}'], names[lv], has_terrain=False,
                                has_geo=False))
            continue
        if not rl.areas:
            continue
        same = (not official) and lv in VANILLA_SIGNATURE and VANILLA_SIGNATURE[lv] == content_signature(rom_path, lv)
        if same and not include_unchanged:
            continue
        multi = len(rl.areas) > 1
        for idx in sorted(rl.areas):
            a = rl.areas[idx]
            notes = list(rl.notes)
            if same:
                notes.append('identical to the vanilla US level (collision, geometry, textures)')
            if not a.terrain:
                notes.append('no TERRAIN command: no collision')
            if a.seg_e_note:
                notes.append(a.seg_e_note)
            if a.macro:
                notes.append('macro objects are not placed')
            out.append(AreaInfo(lv, idx, f'{names[lv]}' + (f' area {idx}' if multi else ''), notes, names[lv],
                                l2c[lv - 1] if lv - 1 < len(l2c) else 0, len(a.objects), 0,
                                len(a.warps), bool(a.terrain), bool(a.geo), same))
    return out


def content_signature(rom_path, level):
    """crc32 of what a level draws and collides with, independent of where its data sits in the ROM: per area the
    static collision triangles (type and vertices as stored) and the display-list triangles of the area's geo layout
    (position, colour, uv, material) with the texture pixels."""
    rom = Rom.open(rom_path)
    cache = rom.__dict__.setdefault('_sigs', {})
    if level in cache:
        return cache[level]
    rl = rom.level(level)
    crc = 0
    for idx in sorted(rl.areas):
        src = RomAreaSource(rom_path, level, idx, shift=0)
        tris = src.tris
        crc = zlib.crc32(struct.pack('>HH', level, idx), crc)
        for kind, ttype, v, _n in tris:
            crc = zlib.crc32(struct.pack('>H9i', ttype, *[int(c) for c in v.reshape(-1)]), crc)
        try:
            ram, scene = src.visual(objects=False)
            texs = geodecode.textures(ram, scene)
        except Exception as e:                                               # noqa: BLE001
            crc = zlib.crc32(b'visual:' + str(type(e).__name__).encode(), crc)
            continue
        for (layer, tex, mode, const), b in sorted(scene.items(), key=lambda kv: (kv[0][0], str(kv[0][2]), str(kv[0][3]))):
            crc = zlib.crc32(repr((layer, mode, const, None if tex is None else tex[1:5] + tex[6:])).encode(), crc)
            crc = zlib.crc32(np.round(np.array(b['pos'], dtype=np.float64), 2).tobytes(), crc)
            crc = zlib.crc32(np.array(b['col'], dtype=np.float64).round().astype(np.int16).tobytes(), crc)
            crc = zlib.crc32(np.array(b['st'], dtype=np.int32).tobytes(), crc)
            if tex is not None:
                crc = zlib.crc32(np.ascontiguousarray(texs[tex]).tobytes(), crc)
    cache[level] = crc
    return crc


class RomAreaSource:
    """The area-source interface (stroopmaps GUI_PLAN.md) for one level/area of a ROM.

    name, hack, level, area, shift, tris, mario (None), visual(objects=True, light=None) -> (ram, scene), notes.
    `act` is the star act whose objects are placed (OBJECT commands carry an act mask); `switch` says which case of
    a geo switch node is drawn: 'first' (case 0, default), 'initial' (the case the script selects) or 'all'."""

    def __init__(self, rom_path, level, area, shift=None, act=1, switch='first', with_object_collision=False):
        self.rom = Rom.open(rom_path)
        self.rom_path = os.path.abspath(rom_path)     # exporter reads it for the hack's code signature
        self.level, self.area = level, area
        self.rl = self.rom.level(level)
        if area not in self.rl.areas:
            raise KeyError(f'level {level} has no area {area} (areas: {sorted(self.rl.areas)})')
        self.adef = self.rl.areas[area]
        if shift is None:
            shift, how = self.rom.elb_shift
        else:
            how = 'given'
        self.shift, self.shift_how = int(shift), how
        self.ram = RomRam(self.rom.mem, self.adef.segs_final)
        self.act, self.switch = act, switch
        self.hack = self.rom.hack_name
        nm = level_names(self.rom, sorted(self.rom.level_table()))
        self.name = nm.get(level, f'Level {level}') + (f' area {area}' if len(self.rl.areas) > 1 else '')
        self.mario = None
        self.notes = list(self.rl.notes)
        self._tris = None
        self._objtris = None
        self._objs = None
        self.static_tris = 0
        self.object_tris = 0
        self.with_object_collision = with_object_collision

    def of(self, kind, death=None):
        """The triangles of `kind` ('floor' | 'wall' | 'ceil'), as stroopmap.tris_of."""
        import stroopmap
        return stroopmap.tris_of(self, kind, death)

    # ---- collision
    def _placed_objects(self):
        """[(ObjDef, BhvInfo)] for the area's objects of the chosen act."""
        if self._objs is None:
            ram = self.ram
            out = []
            bit = 1 << (self.act - 1)
            for o in self.adef.objects:
                if not (o.acts & bit or o.acts == 0x1F):
                    continue
                out.append((o, read_behavior(ram, o.bhv)))
            self._objs = out
        return self._objs

    def _collect(self):
        ram = self.ram
        tris, objtris = [], []
        if self.adef.terrain:
            tris, info = romcol.parse_terrain(ram, ram.va(self.adef.terrain), self.shift)
            if info.special:
                self.notes.append(f'{len(info.special)} special objects (doors, trees, ...) are not placed')
            if info.stopped:
                self.notes.append(f'terrain data stopped early: {info.stopped}')
        else:
            self.notes.append('no TERRAIN command: no collision')
        self.static_tris = len(tris)
        skipped = 0
        for o, b in self._placed_objects():
            if not b.collision:
                continue
            try:
                ang = [(v * 0x8000 // 180 if v >= 0 else -(-v * 0x8000 // 180)) for v in o.angle]
                ang = [((v + 0x8000) & 0xFFFF) - 0x8000 for v in ang]
                objtris += romcol.object_tris(ram, ram.va(b.collision), o.pos, ang, (b.scale,) * 3, self.shift)
            except LookupError as e:
                self.notes.append(f'object collision at {b.collision:#010x}: {e}')
                skipped += 1
        self.object_tris = len(objtris)
        if skipped:
            self.notes.append(f'{skipped} collision objects could not be read')
        if self.adef.macro:
            self.notes.append('macro objects are not placed')
        self._tris, self._objtris = tris, objtris

    @property
    def tris(self):
        """The static collision (the area's TERRAIN), as stroopmap.Area.tris: what the savestate path holds. With
        with_object_collision=True the collision objects' triangles are appended (static_tris marks the boundary)."""
        if self._tris is None:
            self._collect()
        return self._tris + self._objtris if self.with_object_collision else self._tris

    @property
    def object_collision(self):
        """The triangles of the area's collision objects (those whose behaviour script loads collision data, of the
        chosen act) at their OBJECT placement, in the same format."""
        if self._tris is None:
            self._collect()
        return self._objtris

    # ---- visual
    def visual(self, objects=True, light=None, ucode=None):
        """(ram, scene): the area's geo layout drawn, plus the objects `objects` asks for (True / False or a
        geodecode.OBJECT_MODES key)."""
        ram = self.ram
        geo_ram = RomRam(ram.mem, self.adef.segs)
        root, parser = geolayout.parse_geo(geo_ram, self.adef.geo)
        self.notes += [n for n in parser.notes if n not in self.notes]
        dr = geolayout.Drawer(ram, ucode or self.rom.ucode(), light)
        if root is not None:
            dr.draw(root, np.eye(4), switch=self.switch)
        n_obj = 0
        mode = geodecode.object_mode(objects)
        if mode != 'none':
            n_obj = self._draw_objects(dr, ram, mode)
        self.visual_info = dict(objects=n_obj, display_lists=dr.drawn, errors=dr.errors, skipped=dr.skipped,
                                culled=dr.gfx.culled, unknown_ops=dict(dr.gfx.unknown),
                                triangles=sum(len(b['pos']) // 3 for b in dr.scene.values()))
        for e in dr.errors[:5]:
            self.notes.append(e)
        return ram, dr.scene

    def _draw_objects(self, dr, ram, mode='static'):
        n = 0
        for o, b in self._placed_objects():
            if not keep_rom_object(b, mode):
                continue
            model = self.rl.models.get(o.model)
            if model is None:
                continue
            ang = [(v * 0x8000 // 180 if v >= 0 else -(-v * 0x8000 // 180)) for v in o.angle]
            ang = [((v + 0x8000) & 0xFFFF) - 0x8000 for v in ang]
            m = geodecode.mat_trs(list(o.pos), ang)
            m = np.diag([b.scale, b.scale, b.scale, 1.0]) @ m
            try:
                if model.kind == 'geo':
                    root, parser = geolayout.parse_geo(RomRam(ram.mem, model.segs), model.ptr)
                    if root is not None:
                        dr.draw(root, m, objects=True, switch=self.switch)
                else:
                    if model.scale != 1.0:
                        m = np.diag([model.scale] * 3 + [1.0]) @ m
                    dr.draw_dl(model.ptr, m, model.layer)
                n += 1
            except LookupError as e:
                dr.errors.append(f'object model {o.model}: {e}')
        return n


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser(description='ROM reader: list areas, or regenerate the vanilla signature table')
    ap.add_argument('rom')
    ap.add_argument('--vanilla-signatures', action='store_true', help='print VANILLA_SIGNATURE for this (US) ROM')
    ap.add_argument('--all', action='store_true', help='include levels identical to vanilla')
    a = ap.parse_args()
    if a.vanilla_signatures:
        r = Rom.open(a.rom)
        print({lv: content_signature(a.rom, lv) for lv in sorted(r.level_table()) if lv < 60})
    else:
        for x in list_areas(a.rom, include_unchanged=a.all or None):
            print(x)
