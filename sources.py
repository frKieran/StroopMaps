"""Area sources: what the exporter (exporter.py) draws maps from.

An area source is any object with (GUI_PLAN.md, "Area sources"):
  name, hack          display name ("Castle") and the map set's name ("SM64 Island")
  level, area         the sWarpDest bytes STROOP keys maps on
  shift               the ELB vertex shift (0 vanilla, 2 = ROM Manager 4x)
  tris                stroopmap.Area.tris: [(kind, surface_type, verts 3x3 float game units, normal)]
  mario               Mario's position or None
  visual(objects=True, light=None) -> (ram, scene)    for render_textured.export(ram, scene, ...)
and, optionally (the exporter uses them when present):
  slug                file stem of the images ("castle"); else derived from the name
  background          STROOP background name for the entries ("Blue Background" by default)
  signature()         [(address, u32)] words of the running game that identify the hack (see below)
  rom_path            the ROM file (then the signature comes from it)
  notes               list of strings

StateSource is the savestate one: the collision comes from stroopmap.Area (sm64io.load_state_collision underneath)
and the display lists from geodecode.decode, both unchanged.

The hack signature is what lets the patched STROOP's "Recommended" know which hack is running (stroop_patch/README.md):
words of the game's code that differ from the vanilla US ROM's (the hack's patches to the game code, static while the
game runs), eight of them spread over the changed range. A hack that doesn't change the game code has none.
"""
import os, re, struct, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import stroopmap                                   # noqa: E402

TEXT_BASE = 0x80246000                             # where the boot code puts ROM 0x1000.. (US layout)
TEXT_ROM = 0x1000
SIGNATURE_SPAN = 0x60000                           # bytes from TEXT_BASE searched for patched words (all code, no .data)
SIGNATURE_WORDS = 8

# decomp include/levels.h: what the vanilla game calls its level ids
VANILLA_LEVELS = {4: 'Big Boo\'s Haunt', 5: 'Cool, Cool Mountain', 6: 'Castle', 7: 'Hazy Maze Cave',
                  8: 'Shifting Sand Land', 9: 'Bob-omb Battlefield', 10: 'Snowman\'s Land', 11: 'Wet-Dry World',
                  12: 'Jolly Roger Bay', 13: 'Tiny-Huge Island', 14: 'Tick Tock Clock', 15: 'Rainbow Ride',
                  16: 'Castle Grounds', 17: 'Bowser in the Dark World', 18: 'Vanish Cap under the Moat',
                  19: 'Bowser in the Fire Sea', 20: 'The Secret Aquarium', 21: 'Bowser in the Sky',
                  22: 'Lethal Lava Land', 23: 'Dire, Dire Docks', 24: 'Whomp\'s Fortress', 26: 'Castle Courtyard',
                  27: 'The Princess\'s Secret Slide', 28: 'Cavern of the Metal Cap', 29: 'Tower of the Wing Cap',
                  30: 'Bowser Fight 1', 31: 'Wing Mario over the Rainbow', 33: 'Bowser Fight 2',
                  34: 'Bowser Fight 3', 36: 'Tall, Tall Mountain'}


def clean_hack_name(title):
    """A map set's name from a ROM's title or file name: the version tail and any parenthesis left off ("SM64 Island
    1.1" -> "SM64 Island", "Super Mario 64 (USA)" -> "Super Mario 64"). An all-capitals ROM header is put in title case,
    words with a digit kept as they are, and a leading "SM" read as Super Mario ("SM64 ISLAND 1.1" -> "SM64 Island",
    "SM THE LOST DREAMS" -> "Super Mario The Lost Dreams")."""
    t = re.sub(r'\s*\([^)]*\)\s*', ' ', title or '').strip()
    t = re.sub(r'\s+v?\d+(\.\d+)+[a-z]?$', '', t, flags=re.I)
    t = re.sub(r'\s+v\d+[a-z]?$', '', t, flags=re.I)
    if t.isupper():
        t = ' '.join(w if any(c.isdigit() for c in w) else w.capitalize() for w in t.split())
        t = re.sub(r'^Sm\b', 'Super Mario', t)
    return t.strip()


GENERIC_TITLES = {'', 'super mario 64', 'sm64', 'mario 64', 'super mario'}


def is_generic_title(title):
    """True for a header name that doesn't name the hack (many hacks keep "SUPER MARIO 64"): use the file name."""
    return clean_hack_name(title).lower().strip(' :-_') in GENERIC_TITLES


def hack_name_for(path):
    """The hack's name from the ROM header of a ROM, or of the ROM a savestate (or movie) was saved on
    (sm64io.identify); None when that ROM can't be resolved (no ROM on disk matches)."""
    import sm64io
    p = sm64io.identify(path)
    if p is None:
        return None
    title = p.title or p.name
    if is_generic_title(title):                    # the header doesn't name the hack: the ROM's file name does
        rom = getattr(p, 'rom_path', None) or (path if path.lower().endswith(('.z64', '.n64', '.v64')) else None)
        title = os.path.splitext(os.path.basename(rom))[0] if rom else ''
    return clean_hack_name(title) or None


def vanilla_us_rom():
    """The path of the vanilla US ROM (for the signature), or None."""
    import sm64io
    return sm64io.vanilla_us_rom()


def _z64_bytes(path, lo, hi):
    """Bytes lo..hi of a ROM file in big-endian (.z64) order."""
    with open(path, 'rb') as f:
        head = f.read(4)
        f.seek(lo)
        raw = bytearray(f.read(hi - lo))
    src = bytes(raw)
    if head == b'\x37\x80\x40\x12':                # .v64: bytes swapped in pairs
        raw[0::2], raw[1::2] = src[1::2], src[0::2]
    elif head == b'\x40\x12\x37\x80':              # .n64: little-endian words
        for k in range(4):
            raw[k::4] = src[3 - k::4]
    return bytes(raw)


def _pick_spread(changed, n):
    """`n` entries spread evenly over a sorted list."""
    if len(changed) <= n:
        return list(changed)
    return [changed[(2 * i + 1) * len(changed) // (2 * n)] for i in range(n)]


def signature_from_words(words, vanilla_rom=None, n=SIGNATURE_WORDS):
    """[(address, value)]: `n` words of the game code that differ from the vanilla US ROM, spread over the changed
    range. `words(address)` reads the hack's u32 at that address (RAM, or its ROM through _rom_reader). [] when
    there is no vanilla ROM or nothing differs."""
    vanilla_rom = vanilla_rom or vanilla_us_rom()
    if not vanilla_rom:
        return []
    ref = _z64_bytes(vanilla_rom, TEXT_ROM, TEXT_ROM + SIGNATURE_SPAN)
    changed = []
    for off in range(0, len(ref), 4):
        a = TEXT_BASE + off
        if words(a) != struct.unpack_from('>I', ref, off)[0]:
            changed.append(a)
    return [(a, words(a)) for a in _pick_spread(changed, n)]


def signature_from_rom(rom_path, vanilla_rom=None, n=SIGNATURE_WORDS):
    """The same signature read from a hack's ROM file (its code is DMAed to TEXT_BASE as it stands)."""
    data = _z64_bytes(rom_path, TEXT_ROM, TEXT_ROM + SIGNATURE_SPAN)

    def words(a):
        off = a - TEXT_BASE
        return struct.unpack_from('>I', data, off)[0] if 0 <= off <= len(data) - 4 else 0
    return signature_from_words(words, vanilla_rom, n)


def slug_of(text):
    """A file-name stem: lower case letters, digits and underscores."""
    return re.sub(r'[^a-z0-9]+', '_', (text or '').lower()).strip('_') or 'area'


class StateSource:
    """The area a savestate holds (the level Mario is in, taken after it loaded). Cheap to build once: the collision is
    read at construction; visual() decodes the display lists when asked.

        StateSource(path, hack=None, name=None, slug=None, background=None)

    hack: the map set's name. Default: from the ROM header (sm64io.identify) when the ROM the state was saved on can
    be found on disk, else '' (the exporter then needs options['hack'] or `src.hack = ...`)."""

    def __init__(self, path, hack=None, name=None, slug=None, background=None):
        self.path = os.path.abspath(path)
        self._area = stroopmap.Area(self.path)
        a = self._area
        self.level, self.area, self.shift, self.mario = a.level, a.area, a.shift, a.mario
        self.tris = a.tris
        self.notes = list(a.notes or [])
        self.hack = hack if hack is not None else (hack_name_for(self.path) or '')
        self.official = False
        rom_name = None
        import sm64io
        prof = sm64io.identify(self.path)
        if prof is not None:
            self.official = prof.kind == 'official'
            if not name and prof.rom_path and not self.official:
                import romload                     # the hack's own course name, read from its ROM's segment 2
                try:
                    rom_name = romload.level_display_name(romload.Rom.open(prof.rom_path), self.level)[0]
                except (OSError, ValueError, romload.MemError) as e:
                    self.notes.append(f'course name not read from {os.path.basename(prof.rom_path)}: {e}')
        self.name = (name or (VANILLA_LEVELS.get(self.level) if self.official else None) or rom_name
                     or f'Level {self.level}')
        if self.area != 1 and not name:
            self.name += f' area {self.area}'
        self.slug = slug
        self.background = background
        self.last_walker = None                    # the geodecode.Walker of the last visual() call (statistics)

    def of(self, kind, death=None):
        """The area's triangles of one kind (stroopmap.tris_of)."""
        return stroopmap.tris_of(self, kind, death)

    def visual(self, objects=True, light=None):
        """(ram, scene) of the loaded area's display lists (geodecode.decode); `light` is geodecode's light model
        (None = the ROM's own lights)."""
        import geodecode
        ram, walker = geodecode.decode(self.path, objects=objects, light=light)
        self.last_walker = walker
        return ram, walker.scene

    def signature(self):
        """[(address, u32)] identifying the hack from this state's RAM (see the module doc), [] when there is none."""
        import sm64io
        return signature_from_words(sm64io.Savestate(self.path).u32)

    def __repr__(self):
        return f'StateSource({os.path.basename(self.path)!r}, hack={self.hack!r}, level={self.level}, area={self.area})'
