"""Geo layouts from ROM data: the scene graph the geo-layout commands build, and the draw calls it makes.

Part of the ROM reader (romload.py). Ports the command interpreter of decomp/src/engine/geo_layout.c (the commands
that make graph nodes, OPEN/CLOSE_NODE, BRANCH, BRANCH_AND_LINK, RETURN, END, UPDATE_NODE_FLAGS) into a small node
tree, and walks it the way geodecode.Walker walks the graph in a savestate's RAM: translation / rotation / scale /
display-list / animated-part / billboard nodes apply their matrix and draw their display list, level-of-detail groups
keep the ranges that start at distance <= 0 (the closest), switch nodes take one case, and background, object-parent,
generated (asm), shadow and held-object nodes are skipped.

Every display list is run through geodecode.Gfx(...).run(dl, matrix, layer), so a ROM-built scene is the same batch
dict (`geodecode.Walker.scene`) the savestate path makes and `render_textured.export` takes it unchanged.

`ram` is anything with `raw`, `u32`, `s16`, `va` (romload.RomRam); geo-layout commands are read through the segment
table it holds, so give the parser a RomRam made with the segment table of the moment the command ran.
"""
import os
import struct
import sys


HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import geodecode                                                              # noqa: E402

T_ROOT, T_ORTHO, T_PERSP, T_MASTER, T_START, T_LOD, T_SWITCH, T_CAMERA = (
    geodecode.T_ROOT, geodecode.T_ORTHO, geodecode.T_PERSP, geodecode.T_MASTER, geodecode.T_START, geodecode.T_LOD,
    geodecode.T_SWITCH, geodecode.T_CAMERA)
T_TRANSROT, T_TRANS, T_ROT, T_OBJECT, T_ANIM, T_BILL, T_DL, T_SCALE = (
    geodecode.T_TRANSROT, geodecode.T_TRANS, geodecode.T_ROT, geodecode.T_OBJECT, geodecode.T_ANIM, geodecode.T_BILL,
    geodecode.T_DL, geodecode.T_SCALE)
T_SHADOW, T_OBJPARENT, T_GENERATED, T_BACKGROUND, T_HELD, T_CULL = (
    geodecode.T_SHADOW, geodecode.T_OBJPARENT, geodecode.T_GENERATED, geodecode.T_BACKGROUND, geodecode.T_HELD,
    geodecode.T_CULL)

GRAPH_RENDER_ACTIVE = 1
SKIPPED_STATIC = {T_BACKGROUND, T_OBJPARENT, T_HELD, T_SHADOW, T_OBJECT, T_GENERATED}


class GNode:
    """One node of a geo layout read from the ROM: type, flags, display list, transform, children."""
    __slots__ = ('typ', 'flags', 'dl', 't', 'r', 'scale', 'kids', 'case', 'lod_min', 'func', 'param')

    def __init__(self, typ, layer=0, dl=0, t=(0, 0, 0), r=(0, 0, 0), scale=1.0):
        self.typ, self.dl, self.t, self.r, self.scale = typ, dl, t, r, scale
        self.flags = GRAPH_RENDER_ACTIVE | (layer << 8)
        self.kids = []
        self.case = 0
        self.lod_min = 0
        self.func = 0
        self.param = 0

    @property
    def layer(self):
        """The render layer (the high byte of the flags)."""
        return self.flags >> 8


def _angle(v):
    """read_vec3s_angle: degrees in the script to an s16 angle ((v << 15) / 180, C integer division)."""
    x = (v << 15)
    q = abs(x) // 180
    q = -q if x < 0 else q
    return ((q + 0x8000) & 0xFFFF) - 0x8000


class GeoParser:
    """process_geo_layout: runs the commands at `addr` (segmented or KSEG0), returns the root GNode (or None)."""

    def __init__(self, ram, max_cmds=20000):
        self.ram, self.max_cmds = ram, max_cmds
        self.counts = {}
        self.notes = []

    def parse(self, addr):
        """Run the geo layout commands at `addr` (geo_layout.c) and build its node tree."""
        ram = self.ram
        stack = [0, 0]                                 # gGeoLayoutStack, indices from 0
        ret_idx = 2
        cur = [None]                                   # gCurGraphNodeList
        idx = 0
        root = None
        pc = ram.va(addr)
        n_cmds = 0

        def register(node):
            nonlocal root
            while len(cur) <= idx:
                cur.append(None)
            cur[idx] = node
            if idx == 0:
                if root is None:
                    root = node
            else:
                parent = cur[idx - 1]
                if parent.typ == T_OBJPARENT:
                    parent.kids = [node]
                else:
                    parent.kids.append(node)

        while pc is not None and n_cmds < self.max_cmds:
            n_cmds += 1
            try:
                op = ram.raw(pc, 1)[0]
                c = ram.raw(pc, 0x14)
            except LookupError as e:
                self.notes.append(f'geo layout: {e}')
                break
            self.counts[op] = self.counts.get(op, 0) + 1
            u8 = lambda o: c[o]                                      # noqa: E731
            s16 = lambda o: struct.unpack('>h', c[o:o + 2])[0]       # noqa: E731
            u32 = lambda o: struct.unpack('>I', c[o:o + 4])[0]       # noqa: E731
            if op == 0x00:                                           # BRANCH_AND_LINK
                stack.append(pc + 8)
                stack.append((idx << 16) + ret_idx)
                ret_idx = len(stack)
                pc = ram.va(u32(4))
            elif op == 0x01:                                         # END
                del stack[ret_idx:]
                top = stack.pop()
                ret_idx = top & 0xFFFF
                idx = top >> 16
                pc = stack.pop()
                if pc == 0:
                    pc = None
            elif op == 0x02:                                         # BRANCH
                if u8(1) == 1:
                    stack.append(pc + 8)
                pc = ram.va(u32(4))
            elif op == 0x03:                                         # RETURN
                pc = stack.pop()
                if pc == 0:
                    pc = None
            elif op == 0x04:                                         # OPEN_NODE
                if idx + 1 >= len(cur):
                    cur.extend([None] * (idx + 2 - len(cur)))
                cur[idx + 1] = cur[idx]
                idx += 1
                pc += 4
            elif op == 0x05:                                         # CLOSE_NODE
                idx -= 1
                pc += 4
            elif op == 0x06:                                         # ASSIGN_AS_VIEW
                pc += 4
            elif op == 0x07:                                         # UPDATE_NODE_FLAGS
                node = cur[idx] if idx < len(cur) else None
                if node is not None:
                    bits = s16(2) & 0xFFFF
                    if u8(1) == 0:
                        node.flags = bits
                    elif u8(1) == 1:
                        node.flags |= bits
                    elif u8(1) == 2:
                        node.flags &= ~bits
                pc += 4
            elif op == 0x08:                                         # NODE_ROOT
                register(GNode(T_ROOT))
                pc += 0x0C
            elif op == 0x09:                                         # ORTHO
                register(GNode(T_ORTHO))
                pc += 4
            elif op == 0x0A:                                         # PERSPECTIVE
                register(GNode(T_PERSP))
                pc += 12 if u8(1) else 8
            elif op == 0x0B:                                         # START
                register(GNode(T_START))
                pc += 4
            elif op == 0x0C:                                         # MASTER_LIST
                register(GNode(T_MASTER))
                pc += 4
            elif op == 0x0D:                                         # RENDER_RANGE (level of detail)
                n = GNode(T_LOD)
                n.lod_min = s16(4)
                register(n)
                pc += 8
            elif op == 0x0E:                                         # SWITCH_CASE
                n = GNode(T_SWITCH)
                n.case, n.func = s16(2), u32(4)
                register(n)
                pc += 8
            elif op == 0x0F:                                         # CAMERA
                register(GNode(T_CAMERA))
                pc += 0x14
            elif op == 0x10:                                         # TRANSLATION_ROTATION
                params = u8(1)
                lay = (params & 0x70) >> 4
                if lay == 0:
                    t = struct.unpack('>hhh', c[4:10])
                    r = tuple(_angle(v) for v in struct.unpack('>hhh', c[10:16]))
                    p = 16
                elif lay == 1:
                    t, r, p = struct.unpack('>hhh', c[2:8]), (0, 0, 0), 8
                elif lay == 2:
                    t, r, p = (0, 0, 0), tuple(_angle(v) for v in struct.unpack('>hhh', c[2:8])), 8
                else:
                    t, r, p = (0, 0, 0), (0, _angle(s16(2)), 0), 4
                dl, layer = 0, 0
                if params & 0x80:
                    dl, layer = struct.unpack('>I', ram.raw(pc + p, 4))[0], params & 0x0F
                    p += 4
                register(GNode(T_TRANSROT, layer, dl, t, r))
                pc += p
            elif op in (0x11, 0x14):                                 # TRANSLATION, BILLBOARD
                params = u8(1)
                t = struct.unpack('>hhh', c[2:8])
                dl, layer, p = 0, 0, 8
                if params & 0x80:
                    dl, layer = struct.unpack('>I', ram.raw(pc + 8, 4))[0], params & 0x0F
                    p = 12
                register(GNode(T_TRANS if op == 0x11 else T_BILL, layer, dl, t))
                pc += p
            elif op == 0x12:                                         # ROTATION
                params = u8(1)
                r = tuple(_angle(v) for v in struct.unpack('>hhh', c[2:8]))
                dl, layer, p = 0, 0, 8
                if params & 0x80:
                    dl, layer = struct.unpack('>I', ram.raw(pc + 8, 4))[0], params & 0x0F
                    p = 12
                register(GNode(T_ROT, layer, dl, (0, 0, 0), r))
                pc += p
            elif op == 0x13:                                         # ANIMATED_PART
                t = struct.unpack('>hhh', c[2:8])
                register(GNode(T_ANIM, u8(1), u32(8), t))
                pc += 12
            elif op == 0x15:                                         # DISPLAY_LIST
                register(GNode(T_DL, u8(1), u32(4)))
                pc += 8
            elif op == 0x16:                                         # SHADOW
                register(GNode(T_SHADOW))
                pc += 8
            elif op == 0x17:                                         # OBJECT_PARENT
                register(GNode(T_OBJPARENT))
                pc += 4
            elif op == 0x18:                                         # GENERATED (asm)
                n = GNode(T_GENERATED)
                n.func, n.param = u32(4), s16(2)
                register(n)
                pc += 8
            elif op == 0x19:                                         # BACKGROUND
                register(GNode(T_BACKGROUND))
                pc += 8
            elif op == 0x1A:                                         # NOP
                pc += 8
            elif op == 0x1B:                                         # COPY_VIEW
                register(GNode(T_OBJPARENT))
                pc += 4
            elif op == 0x1C:                                         # HELD_OBJECT
                register(GNode(T_HELD))
                pc += 0x0C
            elif op == 0x1D:                                         # SCALE
                params = u8(1)
                scale = u32(4) / 65536.0
                dl, layer, p = 0, 0, 8
                if params & 0x80:
                    dl, layer = u32(8), params & 0x0F
                    p = 12
                register(GNode(T_SCALE, layer, dl, scale=scale))
                pc += p
            elif op == 0x1E:
                pc += 8
            elif op == 0x1F:
                pc += 0x10
            elif op == 0x20:                                         # CULLING_RADIUS
                register(GNode(T_CULL))
                pc += 4
            else:
                self.notes.append(f'geo layout: unknown command {op:#x} at {pc:#x}')
                break
        if n_cmds >= self.max_cmds:
            self.notes.append('geo layout: command limit reached')
        return root


def parse_geo(ram, addr):
    """(root GNode or None, parser) for the geo layout at `addr`."""
    p = GeoParser(ram)
    return p.parse(addr), p


def make_gfx(ram, scene, ucode=None):
    """geodecode.Gfx for `ram`, with the microcode geodecode.detect_ucode finds, else F3D."""
    if ucode is None and hasattr(geodecode, 'detect_ucode'):
        try:
            ucode = geodecode.detect_ucode(ram)
        except Exception:                                    # noqa: BLE001  (detection is a convenience)
            ucode = None
    try:
        return geodecode.Gfx(ram, scene, ucode=ucode or 'F3D')
    except TypeError:                                        # Gfx(ram, scene) only, before D's change
        return geodecode.Gfx(ram, scene)


class Drawer:
    """Walks GNode trees into one scene (the batch dict geodecode.Walker.scene is)."""

    def __init__(self, ram, ucode=None, light=None):
        self.ram = ram
        self.scene = {}
        self.gfx = make_gfx(ram, self.scene, ucode)
        self.gfx.light_model = light
        self.errors = []
        self.drawn = 0
        self.skipped = {}

    def draw_list(self, nodes, mtx, objects=False, switch='first'):
        """Draw each node of `nodes` and its subtree."""
        for n in nodes:
            self.draw(n, mtx, objects, switch)

    def draw(self, n, mtx, objects=False, switch='first'):
        """Draw node `n` and its subtree under `mtx`, as geo_process_* would."""
        typ = n.typ
        if not n.flags & GRAPH_RENDER_ACTIVE:
            return
        layer = n.layer
        m, dl = mtx, None
        if typ == T_TRANSROT:
            m, dl = geodecode.mat_trs(n.t, n.r) @ mtx, n.dl
        elif typ == T_BILL and objects:
            m, dl = geodecode.billboard_matrix(n.t, mtx), n.dl          # sprites lie flat, facing up (as from RAM)
        elif typ in (T_TRANS, T_ANIM, T_BILL):
            m, dl = geodecode.mat_trs(n.t) @ mtx, n.dl
        elif typ == T_ROT:
            m, dl = geodecode.mat_trs((0, 0, 0), n.r) @ mtx, n.dl
        elif typ == T_SCALE:
            m, dl = geodecode.mat_trs((0, 0, 0), None, n.scale) @ mtx, n.dl
        elif typ == T_DL:
            dl = n.dl
        elif typ == T_LOD:
            if n.lod_min > 0:                                # only the range that starts at distance <= 0
                return
        elif typ == T_SWITCH:
            if switch == 'all':
                self.draw_list(n.kids, m, objects, switch)
            elif n.kids:
                sel = n.case if switch == 'initial' else 0
                if 0 <= sel < len(n.kids):
                    self.draw(n.kids[sel], m, objects, switch)
            return
        elif objects and typ in (geodecode.T_SHADOW, geodecode.T_GENERATED, geodecode.T_HELD):
            pass                     # an object's model hangs below its shadow node: draw the children (as from RAM)
        elif typ in SKIPPED_STATIC:
            self.skipped[typ] = self.skipped.get(typ, 0) + 1
            return
        if dl:
            self.gfx.geom = geodecode.GEOM_INIT
            try:
                self.gfx.run(dl, m, layer)
                self.drawn += 1
            except (LookupError, struct.error) as e:
                self.errors.append(f'display list {dl:#010x}: {e}')
        if n.kids:
            self.draw_list(n.kids, m, objects, switch)

    def draw_dl(self, dl, mtx, layer):
        """Interpret one display list, noting (not raising) a list that cannot be read."""
        self.gfx.geom = geodecode.GEOM_INIT
        try:
            self.gfx.run(dl, mtx, layer)
            self.drawn += 1
        except (LookupError, struct.error) as e:
            self.errors.append(f'display list {dl:#010x}: {e}')
