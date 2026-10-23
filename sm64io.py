"""Reading Super Mario 64 data for StroopMaps: Mupen64 savestates (Savestate), the loaded area's static collision
(load_state_collision), ROM byte order (z64), which ROM a file is or a savestate was saved on (identify,
vanilla_us_rom) and tool paths from the environment (machine_path).

Savestates: gzip (Mupen64 up to 1.4.x) or an LZ4 frame (1.5.0 on); inside, RDRAM starts at 0x1B0 as little-endian
32-bit words. The collision format is the decomp's (levels/*/collision.inc.c, read by surface_load.c), and the action
names are the decomp's include/sm64.h.
"""
import gzip
import hashlib
import json
import math
import os
import struct

RDRAM_OFFSET = 0x1B0
MARIO_STATE = 0x8033B170  # gMarioStates (US)
KNOWN_MARIO_STATES = (0x8033B170, 0x80339E00, 0x8031D9C0, 0x80309430)     # US, JP, SH, EU
FORCE_TYPES = {0x0004, 0x000E, 0x0024, 0x0025, 0x0027, 0x002C, 0x002D}  # surface_has_force()


# Mario's actions (decomp include/sm64.h): value -> name
ACTION_NAMES = {
    0x00000000: 'uninitialized',
    0x00000050: 'begin sliding',
    0x00000051: 'hold begin sliding',
    0x00000383: 'picking up',
    0x00000385: 'dive picking up',
    0x00000386: 'stomach slide stop',
    0x00000387: 'placing down',
    0x00000390: 'picking up bowser',
    0x00000391: 'holding bowser',
    0x00000392: 'releasing bowser',
    0x00000442: 'hold walking',
    0x00000443: 'turning around',
    0x00000444: 'finish turning around',
    0x00000447: 'hold heavy walking',
    0x0000044b: 'hold decelerating',
    0x00000474: 'hold jump land',
    0x00000475: 'hold freefall land',
    0x00000476: 'quicksand jump land',
    0x00000477: 'hold quicksand jump land',
    0x00000479: 'long jump land',
    0x000004a8: 'riding hoot',
    0x0000054c: 'ledge climb slow 1',
    0x0000054d: 'ledge climb slow 2',
    0x0000054e: 'ledge climb down',
    0x0000054f: 'ledge climb fast',
    0x000008a7: 'air hit wall',
    0x000008ae: 'crazy box bounce',
    0x00001300: 'disappeared',
    0x00001302: 'star dance exit',
    0x00001303: 'star dance water',
    0x00001307: 'star dance no exit',
    0x00001308: 'reading sign',
    0x0000130a: 'waiting for dialog',
    0x0000130f: 'debug free move',
    0x00001319: 'credits cutscene',
    0x0000131a: 'end waving cutscene',
    0x00001320: 'pulling door',
    0x00001321: 'pushing door',
    0x00001322: 'warp door spawn',
    0x00001325: 'spawn spin landing',
    0x00001327: 'exit land save dialog',
    0x0000132e: 'unlocking key door',
    0x0000132f: 'unlocking star door',
    0x00001331: 'entering star door',
    0x00001333: 'spawn no spin landing',
    0x00001336: 'teleport fade out',
    0x00001337: 'teleport fade in',
    0x0000133d: 'putting on cap',
    0x00001371: 'in cannon',
    0x00001535: 'bbh enter spin',
    0x00001904: 'fall after star grab',
    0x00001909: 'jumbo star cutscene',
    0x00001918: 'end peach cutscene',
    0x00001923: 'emerge from pipe',
    0x00001924: 'spawn spin airborne',
    0x00001926: 'exit airborne',
    0x00001928: 'death exit',
    0x00001929: 'unused death exit',
    0x0000192a: 'falling death exit',
    0x0000192b: 'special exit airborne',
    0x0000192c: 'special death exit',
    0x0000192d: 'falling exit airborne',
    0x00001932: 'spawn no spin airborne',
    0x00001934: 'bbh enter jump',
    0x000042f4: 'metal water falling',
    0x000042f5: 'hold metal water falling',
    0x000042f6: 'metal water fall land',
    0x000042f7: 'hold metal water fall land',
    0x000044f2: 'metal water walking',
    0x000044f3: 'hold metal water walking',
    0x000044f8: 'metal water jump',
    0x000044f9: 'hold metal water jump',
    0x000044fa: 'metal water jump land',
    0x000044fb: 'hold metal water jump land',
    0x0002020d: 'in quicksand',
    0x0002020e: 'unknown 0002020e',
    0x00020226: 'shockwave bounce',
    0x00020338: 'shocked',
    0x00020339: 'squished',
    0x0002033a: 'head stuck in ground',
    0x0002033b: 'butt stuck in ground',
    0x0002033c: 'feet stuck in ground',
    0x00020370: 'grabbed',
    0x00020449: 'burning ground',
    0x00020460: 'hard backward ground kb',
    0x00020461: 'hard forward ground kb',
    0x00020462: 'backward ground kb',
    0x00020463: 'forward ground kb',
    0x00020464: 'soft backward ground kb',
    0x00020465: 'soft forward ground kb',
    0x00020466: 'ground bonk',
    0x00020467: 'death exit land',
    0x00021311: 'standing death',
    0x00021312: 'quicksand death',
    0x00021313: 'electrocution',
    0x00021314: 'suffocation',
    0x00021315: 'death on stomach',
    0x00021316: 'death on back',
    0x00021317: 'eaten by bubba',
    0x00100341: 'grab pole slow',
    0x00100342: 'grab pole fast',
    0x00100343: 'climbing pole',
    0x00100344: 'top of pole transition',
    0x00100345: 'top of pole',
    0x00200349: 'hanging',
    0x0020054a: 'hang moving',
    0x0080023c: 'ground pound land',
    0x00800380: 'punching',
    0x00800457: 'move punching',
    0x0080045a: 'slide kick slide',
    0x008008a9: 'ground pound',
    0x0081089b: 'riding shell fall',
    0x00840452: 'butt slide',
    0x00840454: 'hold butt slide',
    0x00880456: 'dive slide',
    0x00880898: 'shot from cannon',
    0x008c0453: 'stomach slide',
    0x008c0455: 'hold stomach slide',
    0x01000882: 'triple jump',
    0x01000883: 'backflip',
    0x01000887: 'side flip',
    0x01000889: 'water jump',
    0x0100088c: 'freefall',
    0x010008a1: 'hold freefall',
    0x010008a2: 'hold butt slide air',
    0x010008a3: 'hold water jump',
    0x010008a6: 'forward rollout',
    0x010008ad: 'backward rollout',
    0x010208b0: 'backward air kb',
    0x010208b1: 'forward air kb',
    0x010208b2: 'hard forward air kb',
    0x010208b3: 'hard backward air kb',
    0x010208b4: 'burning jump',
    0x010208b5: 'burning fall',
    0x010208b6: 'soft bonk',
    0x010208b7: 'lava boost',
    0x010208b8: 'getting blown',
    0x010208bd: 'thrown forward',
    0x010208be: 'thrown backward',
    0x018008aa: 'slide kick',
    0x018008ac: 'jump kick',
    0x0188088a: 'dive',
    0x0281089a: 'riding shell jump',
    0x03000880: 'jump',
    0x03000881: 'double jump',
    0x03000885: 'steep jump',
    0x03000886: 'wall kick air',
    0x03000888: 'long jump',
    0x0300088d: 'top of pole jump',
    0x0300088e: 'butt slide air',
    0x03000894: 'flying triple jump',
    0x030008a0: 'hold jump',
    0x030008af: 'special triple jump',
    0x04000440: 'walking',
    0x04000445: 'braking',
    0x0400044a: 'decelerating',
    0x04000470: 'jump land',
    0x04000471: 'freefall land',
    0x04000472: 'double jump land',
    0x04000473: 'side flip land',
    0x04000478: 'triple jump land',
    0x0400047a: 'backflip land',
    0x04001301: 'intro cutscene',
    0x04008448: 'crawling',
    0x04808459: 'crouch slide',
    0x08000206: 'hold panting unused',
    0x08000207: 'hold idle',
    0x08000208: 'hold heavy idle',
    0x08000225: 'slide kick slide stop',
    0x0800022f: 'backflip land stop',
    0x08000234: 'hold jump land stop',
    0x08000235: 'hold freefall land stop',
    0x08000239: 'lava boost land',
    0x0800023a: 'triple jump land stop',
    0x0800023b: 'long jump land stop',
    0x0800034b: 'ledge grab',
    0x0800043f: 'hold butt slide stop',
    0x080042f0: 'metal water standing',
    0x080042f1: 'hold metal water standing',
    0x08100340: 'holding pole',
    0x08200348: 'start hanging',
    0x0c000203: 'sleeping',
    0x0c000204: 'waking up',
    0x0c000227: 'first person',
    0x0c000230: 'jump land stop',
    0x0c000231: 'double jump land stop',
    0x0c000232: 'freefall land stop',
    0x0c000233: 'side flip land stop',
    0x0c00023d: 'braking stop',
    0x0c00023e: 'butt slide stop',
    0x0c008220: 'crouching',
    0x0c008221: 'start crouching',
    0x0c008222: 'stop crouching',
    0x0c008223: 'start crawling',
    0x0c008224: 'stop crawling',
    0x0c400201: 'idle',
    0x0c400202: 'start sleeping',
    0x0c400205: 'panting',
    0x0c400209: 'standing against wall',
    0x0c40020a: 'coughing',
    0x0c40020b: 'shivering',
    0x10020372: 'tornado twirling',
    0x1008089c: 'vertical wind',
    0x108008a4: 'twirling',
    0x10880899: 'flying',
    0x18800238: 'twirl land',
    0x20001305: 'reading automatic dialog',
    0x20001306: 'reading npc dialog',
    0x20810446: 'riding shell ground',
    0x300022c2: 'water action end',
    0x300022c3: 'hold water action end',
    0x300022e2: 'water plunge',
    0x300024d0: 'breaststroke',
    0x300024d1: 'swimming end',
    0x300024d2: 'flutter kick',
    0x300024d3: 'hold breaststroke',
    0x300024d4: 'hold swimming end',
    0x300024d5: 'hold flutter kick',
    0x300024d6: 'water shell swimming',
    0x300024e0: 'water throw',
    0x300024e1: 'water punch',
    0x300032c4: 'drowning',
    0x300032c7: 'water death',
    0x300222c5: 'backward water kb',
    0x300222c6: 'forward water kb',
    0x300222c8: 'water shocked',
    0x300222e3: 'caught in whirlpool',
    0x380022c0: 'water idle',
    0x380022c1: 'hold water idle',
    0x80000588: 'throwing',
    0x80000589: 'heavy throw',
    0x80000a36: 'air throw land',
    0x830008ab: 'air throw',
}


def _lz4_block(src, i, end, out):
    """Decode one LZ4 block (src[i:end]) onto the end of out. Matches may reach back into earlier blocks,
    which is what linked blocks in a frame rely on."""
    while i < end:
        token = src[i]
        i += 1
        lit = token >> 4
        if lit == 15:
            while True:
                b = src[i]
                i += 1
                lit += b
                if b != 255:
                    break
        out += src[i:i + lit]
        i += lit
        if i >= end:                     # the last sequence is literals only
            break
        offset = src[i] | (src[i + 1] << 8)
        i += 2
        mlen = token & 15
        if mlen == 15:
            while True:
                b = src[i]
                i += 1
                mlen += b
                if b != 255:
                    break
        mlen += 4
        start = len(out) - offset
        if offset >= mlen:
            out += out[start:start + mlen]
        else:                            # overlapping copy: the last `offset` bytes repeat
            pattern = bytes(out[start:])
            out += (pattern * (mlen // offset + 1))[:mlen]


def lz4_frame_decompress(data):
    """The LZ4 frame format (as Mupen64 1.5.0 writes savestates), in plain Python so that no extra
    library is needed."""
    try:
        import lz4.frame
        return lz4.frame.decompress(data)
    except ImportError:
        pass
    out, i = bytearray(), 0
    while i + 4 <= len(data):
        magic = struct.unpack_from('<I', data, i)[0]
        if 0x184D2A50 <= magic <= 0x184D2A5F:            # skippable frame
            i += 8 + struct.unpack_from('<I', data, i + 4)[0]
            continue
        if magic != 0x184D2204:
            raise ValueError('not an LZ4 frame')
        flg = data[i + 4]
        i += 6 + (8 if flg & 0x08 else 0) + (4 if flg & 0x01 else 0) + 1   # descriptor, sizes, header checksum
        block_sum = 4 if flg & 0x10 else 0
        while True:
            size = struct.unpack_from('<I', data, i)[0]
            i += 4
            if size == 0:                                # end mark
                break
            n = size & 0x7FFFFFFF
            if size & 0x80000000:                        # stored uncompressed
                out += data[i:i + n]
            else:
                _lz4_block(data, i, i + n, out)
            i += n + block_sum
        if flg & 0x04:                                   # content checksum
            i += 4
    return bytes(out)


def read_savestate_file(path):
    """A Mupen64 savestate's contents: gzip up to 1.4.x, an LZ4 frame from 1.5.0, or already plain."""
    raw = open(path, 'rb').read()
    if raw[:2] == b'\x1f\x8b':
        return gzip.decompress(raw)
    if raw[:4] == b'\x04\x22\x4d\x18':
        return lz4_frame_decompress(raw)
    return raw


def read_savestate_head(path, n=64):
    """The first n bytes of a savestate's contents without decompressing the rest: gzip (a streaming prefix), an LZ4
    frame (only its first block is decoded; the pure-Python decoder takes seconds on a whole 8 MB state), or plain."""
    with open(path, 'rb') as f:
        raw = f.read(4 << 20)                                    # an LZ4 block is at most 4 MB
    if raw[:2] == b'\x1f\x8b':
        import zlib
        return zlib.decompressobj(31).decompress(raw, n)[:n]
    if raw[:4] == b'\x04\x22\x4d\x18':
        flg = raw[4]
        i = 6 + (8 if flg & 0x08 else 0) + (4 if flg & 0x01 else 0) + 1
        out = bytearray()
        while len(out) < n:
            size = struct.unpack_from('<I', raw, i)[0]
            i += 4
            if size == 0:
                break
            m = size & 0x7FFFFFFF
            if size & 0x80000000:
                out += raw[i:i + m]
            else:
                _lz4_block(raw, i, i + m, out)
            i += m + (4 if flg & 0x10 else 0)
        return bytes(out[:n])
    return raw[:n]


class Savestate:
    def __init__(self, path):
        self.data = read_savestate_file(path)
        self.mario_state = self.find_mario()

    def bytes(self, addr, n):
        """Big-endian N64 bytes at virtual address addr."""
        out = bytearray()
        for a in range(addr, addr + n):
            phys = (a & 0x1FFFFFFF)
            word = struct.unpack_from('<I', self.data, RDRAM_OFFSET + (phys & ~3))[0]
            out.append((word >> (24 - 8 * (phys & 3))) & 0xFF)
        return bytes(out)

    def u32(self, addr): return struct.unpack('>I', self.bytes(addr, 4))[0]
    def s16(self, addr): return struct.unpack('>h', self.bytes(addr, 2))[0]
    def u16(self, addr): return struct.unpack('>H', self.bytes(addr, 2))[0]
    def f32(self, addr): return struct.unpack('>f', self.bytes(addr, 4))[0]

    def surface(self, ptr):
        if not ptr:
            return None
        b = self.bytes(ptr, 0x30)
        typ, force, flags, room, lowerY, upperY = struct.unpack_from('>hhbbhh', b, 0)
        v = struct.unpack_from('>9h', b, 0x0A)
        normal = struct.unpack_from('>3f', b, 0x1C)
        oo = struct.unpack_from('>f', b, 0x28)[0]
        return {'type': typ, 'vertices': [v[0:3], v[3:6], v[6:9]], 'normal': normal, 'originOffset': oo,
                'lowerY': lowerY, 'upperY': upperY}

    def plausible_mario(self, addr):
        """Does a MarioState really live here? Every version lays the struct out the same way, so the
        struct itself identifies it: a known action, a position the floor agrees with, and pointers that
        point into RAM. Returns a score (higher = more of the optional checks passed) or None."""
        try:
            if self.u32(addr + 0x0C) not in ACTION_NAMES:
                return None
            pos = [self.f32(addr + 0x3C + 4 * i) for i in range(3)]
            floorHeight, fVel = self.f32(addr + 0x70), self.f32(addr + 0x54)
            floor, area, obj = self.u32(addr + 0x68), self.u32(addr + 0x90), self.u32(addr + 0x88)
        except (struct.error, IndexError):
            return None

        def ram(p, allow_null=True):
            return (p == 0 and allow_null) or 0x80000000 <= p < 0x80800000
        if not all(abs(v) < 40000 and v == v for v in pos):
            return None
        if not (abs(fVel) < 300 and fVel == fVel):
            return None
        if not (ram(floor) and ram(area, False) and ram(obj, False)):
            return None
        score = 0
        if abs(pos[1] - floorHeight) < 5000 and floorHeight == floorHeight:
            score += 2
        if floor:
            score += 1
        camera = self.u32(area + 0x24)
        if ram(camera, False):
            score += 1
        if self.u32(area + 0x08):            # terrain data for the collision the level loaded
            score += 1
        return score

    def find_mario(self):
        """Address of gMarioStates. The US one is tried first; anything else (JP, EU, Shindou, or a hack
        that moved it) is found by scanning RAM for the struct, so no table of addresses is needed. The
        scan reads whole words straight out of the file, because going through bytes() takes a minute."""
        if self.plausible_mario(MARIO_STATE) is not None:
            return MARIO_STATE
        # The other three official builds: the struct test at their known addresses is enough, no scan needed. A
        # JP power-on state saved mid-spin has a stale floor pointer that the scan's on_this_floor
        # test rejects for every candidate, while plausible_mario(JP) passes (score 3); 3 is also the bar here.
        for addr in KNOWN_MARIO_STATES[1:]:
            score = self.plausible_mario(addr)
            if score is not None and score >= 3:
                return addr
        import array
        size = (len(self.data) - RDRAM_OFFSET) & ~3
        raw = self.data[RDRAM_OFFSET:RDRAM_OFFSET + size]
        words, floats = array.array('I'), array.array('f')
        words.frombytes(raw)
        floats.frombytes(raw)
        if struct.pack('<I', 1) != struct.pack('=I', 1):   # the file is little-endian, this machine is not
            words.byteswap()
            floats.byteswap()
        actions = frozenset(a for a in ACTION_NAMES if a)  # 0 is "uninitialized": every blank word matches
        n = len(words)

        def ram(p, allow_null=True):
            return (p == 0 and allow_null) or 0x80000000 <= p < 0x80800000

        def on_this_floor(ptr, x, y, z, floorHeight):
            """The floor pointer has to describe the plane Mario is standing on: a unit normal, pointing
            up, and a height at his position that matches floorHeight. Stray memory does not do that."""
            s = (ptr & 0x1FFFFFFF) // 4
            if s + 10 >= n:
                return False
            nx, ny, nz, oo = floats[s + 7], floats[s + 8], floats[s + 9], floats[s + 10]
            if not (ny > 0.01 and abs(nx * nx + ny * ny + nz * nz - 1.0) < 0.01):
                return False
            return abs(-(nx * x + nz * z + oo) / ny - floorHeight) < 0.1
        best, best_score = None, -1
        for i, w in enumerate(words):
            if w not in actions:
                continue
            b = i - 3                                      # action sits at +0x0C
            if b < 0 or b + 40 >= n:
                continue
            x, y, z = floats[b + 15], floats[b + 16], floats[b + 17]
            fVel, floorHeight = floats[b + 21], floats[b + 28]
            floor, area, obj = words[b + 26], words[b + 36], words[b + 34]   # floor, area, marioObj
            if not (abs(x) < 40000 and abs(y) < 40000 and abs(z) < 40000):
                continue                                   # NaN compares false, which is what we want
            if not abs(fVel) < 300:
                continue
            if not (ram(floor) and ram(area, False) and ram(obj, False)):
                continue
            if floor and not on_this_floor(floor, x, y, z, floorHeight):
                continue
            score = (2 if floor else 0) + (1 if abs(y - floorHeight) < 5000 else 0)
            a = (area & 0x1FFFFFFF) // 4
            if a + 9 < n:
                score += 1 if ram(words[a + 9], False) else 0      # area->camera
                score += 1 if ram(words[a + 2], False) else 0      # area->terrainData
            if score > best_score:
                best, best_score = 0x80000000 + 4 * b, score
        if best is None:
            raise ValueError('no Mario state found in this savestate - is it a Super Mario 64 state?')
        return best

    def mario(self):
        m = self.mario_state
        area = self.u32(m + 0x90)
        camera = self.u32(area + 0x24) if area else 0
        return {
            'action': self.u32(m + 0x0C),
            'intendedMag': self.f32(m + 0x20), 'intendedYaw': self.s16(m + 0x24) & 0xFFFF,
            'faceYaw': self.s16(m + 0x2E) & 0xFFFF, 'slideYaw': self.s16(m + 0x38) & 0xFFFF,
            'pos': tuple(self.f32(m + 0x3C + 4 * i) for i in range(3)),
            'vel': tuple(self.f32(m + 0x48 + 4 * i) for i in range(3)),
            'forwardVel': self.f32(m + 0x54),
            'slideVel': (self.f32(m + 0x58), self.f32(m + 0x5C)),
            'peakHeight': self.f32(m + 0xBC),
            # B held on the last frame polled: the next frame's B would not count as a press
            'bHeld': bool(self.u16(self.u32(m + 0x9C) + 0x10) & 0x4000) if self.u32(m + 0x9C) else False,
            'floor': self.surface(self.u32(m + 0x68)),
            'floorHeight': self.f32(m + 0x70),
            'terrainType': self.u16(area + 0x02) if area else None,
            'cameraYaw': (self.s16(camera + 0x02) & 0xFFFF) if camera else None,
            'cameraMode': self.bytes(camera, 1)[0] if camera else None,
            # struct Camera (camera.h): nextYaw is where the yaw is heading - it steps 0x400 a frame toward it
            'cameraNextYaw': (self.s16(camera + 0x3A) & 0xFFFF) if camera else None,
            'cameraCutscene': self.bytes(camera + 0x30, 1)[0] if camera else None,
            'cameraAreaCen': (self.f32(camera + 0x28), self.f32(camera + 0x2C)) if camera else None,
            'actionName': ACTION_NAMES.get(self.u32(m + 0x0C), 'unknown'),
        }


def collision_triangles(st):
    """Static collision triangles in load order, read from the area's terrain data (same format as
    levels/*/collision.inc.c). Returns [(type, v1, v2, v3), ...].

    Vanilla stores 3 vertex indices per triangle (6 bytes) and a 4th word, the force, only for the
    surface_has_force() types (8 bytes). Hacks built with HackerSM64's ALL_SURFACES_HAVE_FORCE (The Hidden Stars)
    give every triangle the force word: when the vanilla reading fails (an index past the vertex list, or no end
    command) and the all-8-bytes one doesn't, that one is used (2026-10-22)."""
    area = st.u32(st.mario_state + 0x90)
    ptr = st.u32(area + 0x08)
    if not ptr:
        return []

    def s16at(p):
        return struct.unpack('>h', st.bytes(p, 2))[0]
    tris, ok = _collision_parse(s16at, ptr, all_force=False)
    if not ok:
        tris8, ok8 = _collision_parse(s16at, ptr, all_force=True)
        if ok8:
            return tris8
    return tris


def _collision_parse(s16at, ptr, all_force):
    """(triangles, ok) of one reading of the terrain data; ok is False when an index falls outside the vertex list or
    the data runs past 200000 commands without its end."""
    verts, tris = [], []
    for _ in range(200000):
        cmd = s16at(ptr) & 0xFFFF
        ptr += 2
        if cmd == 0x0040:                       # vertices
            n = s16at(ptr); ptr += 2
            verts = [tuple(s16at(ptr + 6 * i + 2 * k) for k in range(3)) for i in range(n)]
            ptr += 6 * n
        elif cmd < 0x0040 or cmd >= 0x0065:     # surface type: triangles follow
            n = s16at(ptr); ptr += 2
            step = 8 if all_force or cmd in FORCE_TYPES else 6
            for i in range(n):
                a, b, c = (s16at(ptr + 2 * k) for k in range(3))
                if not (0 <= a < len(verts) and 0 <= b < len(verts) and 0 <= c < len(verts)):
                    return tris, False
                tris.append((cmd, verts[a], verts[b], verts[c]))
                ptr += step
        else:                                   # 0x41 / 0x42 end; objects / water boxes come after the triangles
            return tris, True
    return tris, False


def elb_shift(st, tris=None):
    """How far the ROM's collision loader shifts vertices: 0 for vanilla, 2 for ROM Manager's 4x extended
    level boundaries (it stores the level at a quarter scale and scales Mario's position to match).
    Mario's floor in RAM is the loaded surface, so the shift is whichever one turns one of the level's own
    triangles into exactly that surface. Returns (shift, how it was found)."""
    m = st.mario()
    floor = m['floor']
    if not floor:
        return 0, 'no floor under Mario, assuming normal level boundaries'
    want = sorted(tuple(v) for v in floor['vertices'])
    tris = tris if tris is not None else collision_triangles(st)
    for shift in range(5):
        for _, *verts in tris:
            if sorted(tuple(c >> shift for c in v) for v in verts) == want:
                return shift, 'matched the floor under Mario'
    # Not a level triangle (an object's platform, say): fall back on the height it gives him.
    nx, ny, nz = floor['normal']
    for shift in range(5):
        x, z = int(m['pos'][0]) >> shift, int(m['pos'][2]) >> shift
        h = -(x * nx + nz * z + floor['originOffset']) / ny * (1 << shift)
        if ny and abs(h - m['floorHeight']) < 0.01:
            return shift, 'from the height of the floor under Mario (not a level triangle)'
    return 0, 'could not tell, assuming normal level boundaries'


def _z64(data):
    """ROM bytes in the big-endian (.z64) order Mupen hashes: .v64 is byte-swapped, .n64 word-swapped."""
    magic = data[:4]
    if magic == b'\x37\x80\x40\x12':
        b = bytearray(data)
        b[0::2], b[1::2] = data[1::2], data[0::2]
        return bytes(b)
    if magic == b'\x40\x12\x37\x80':
        b = bytearray(data)
        for k in range(4):
            b[k::4] = data[3 - k::4]
        return bytes(b)
    return data


# ---------------------------------------------------------------------------------------------------- the area
class Level:
    """A savestate area's static collision: tris [(kind, type, (v1, v2, v3), normal, index)], kind 'floor' / 'wall' /
    'ceil' from the normal as surface_load.c sorts surfaces; elb, the loader's vertex shift; mario; notes."""

    def __init__(self, title):
        self.title = title
        self.tris = []
        self.mario = None
        self.elb = 0
        self.notes = []

    def add(self, ttype, v1, v2, v3, index, shift=0):
        # surface_load.c computes the normal on the loaded (shifted) vertices; the full-scale ones are kept for drawing
        a, b, c = ([q >> shift for q in v] for v in (v1, v2, v3))
        nx = (b[1] - a[1]) * (c[2] - b[2]) - (b[2] - a[2]) * (c[1] - b[1])
        ny = (b[2] - a[2]) * (c[0] - b[0]) - (b[0] - a[0]) * (c[2] - b[2])
        nz = (b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0])
        mag = math.sqrt(nx * nx + ny * ny + nz * nz)
        if mag < 0.0001:
            return                                     # degenerate: the game skips it too
        n = (nx / mag, ny / mag, nz / mag)
        kind = 'floor' if n[1] > 0.01 else 'ceil' if n[1] < -0.01 else 'wall'
        self.tris.append((kind, ttype, (tuple(v1), tuple(v2), tuple(v3)), n, index))


def load_state_collision(path):
    """The loaded area's static collision in a savestate (a Level), with the collision loader's vertex shift (2 for
    ROM Manager's 4x extended boundaries) found from the floor under Mario (elb_shift)."""
    st = Savestate(path)
    lv = Level(os.path.basename(path))
    tris = collision_triangles(st)
    lv.elb, how = elb_shift(st, tris)
    lv.notes.append(f'vertex shift {lv.elb} ({how})')
    for i, (ttype, v1, v2, v3) in enumerate(tris):
        lv.add(ttype, v1, v2, v3, i, lv.elb)
    m = st.mario()
    lv.mario = {'pos': [round(c, 3) for c in m['pos']], 'action': m.get('actionName', ''),
                'faceYaw': m['faceYaw'], 'fv': round(m['forwardVel'], 3)}
    return lv


# ---------------------------------------------------------------------------------------------------- ROM files
ROM_EXT = ('.z64', '.n64', '.v64')
OFFICIAL = {'20b854b239203baf6c961b850a4a51a2': 'US', '85d61f5525af708c9f1e84dce6dc10e9': 'JP',
            '45676429ef6b90e65b517129b700308e': 'EU', '2d727c3278aa232d94f2fb45aec4d303': 'SH'}   # .z64 MD5s
US_MD5 = '20b854b239203baf6c961b850a4a51a2'
_md5_cache = {}                                        # (path, size, mtime) -> md5 of the .z64-order bytes


def z64(raw):
    """ROM bytes in the big-endian (.z64) order: .v64 is byte-swapped, .n64 word-swapped."""
    return _z64(raw)


def _settings_dir():
    base = os.environ.get('APPDATA') or os.path.expanduser('~')
    return os.path.join(base, 'StroopMaps')


def _rom_dirs():
    """Folders searched for ROM files: STROOPMAPS_ROMS (folders separated by os.pathsep), then the folders of ROMs
    and savestates opened before (remember_rom_dir)."""
    dirs = [d for d in os.environ.get('STROOPMAPS_ROMS', '').split(os.pathsep) if d]
    try:
        with open(os.path.join(_settings_dir(), 'rom_dirs.json'), encoding='utf-8') as f:
            dirs += [d for d in json.load(f) if isinstance(d, str)]
    except (OSError, ValueError):
        pass
    seen, out = set(), []
    for d in dirs:
        k = os.path.normcase(os.path.abspath(d))
        if k not in seen and os.path.isdir(d):
            seen.add(k)
            out.append(d)
    return out


def remember_rom_dir(folder):
    """Remember a folder to search for ROMs (the one a ROM or savestate was opened from); kept between runs."""
    if not folder or not os.path.isdir(folder):
        return
    path = os.path.join(_settings_dir(), 'rom_dirs.json')
    try:
        with open(path, encoding='utf-8') as f:
            dirs = [d for d in json.load(f) if isinstance(d, str)]
    except (OSError, ValueError):
        dirs = []
    folder = os.path.abspath(folder)
    if os.path.normcase(folder) in {os.path.normcase(d) for d in dirs}:
        return
    dirs = (dirs + [folder])[-20:]
    try:
        os.makedirs(_settings_dir(), exist_ok=True)
        with open(path + '.tmp', 'w', encoding='utf-8') as f:
            json.dump(dirs, f, indent=1)
        os.replace(path + '.tmp', path)
    except OSError:
        pass                                           # a read-only profile: the search just has fewer folders


def rom_md5(path):
    """MD5 of a ROM file's bytes in .z64 order (what Mupen stamps on its savestates), or None if it isn't a ROM."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = (os.path.abspath(path), st.st_size, int(st.st_mtime))
    if key not in _md5_cache:
        try:
            with open(path, 'rb') as f:
                data = _z64(f.read())
        except OSError:
            return None
        _md5_cache[key] = hashlib.md5(data).hexdigest() if data[:4] == b'\x80\x37\x12\x40' else None
    return _md5_cache[key]


def find_rom(md5):
    """The path of a ROM file whose MD5 is `md5` in the searched folders (_rom_dirs), or None."""
    for d in _rom_dirs():
        try:
            names = sorted(os.listdir(d))
        except OSError:
            continue
        for n in names:
            p = os.path.join(d, n)
            if n.lower().endswith(ROM_EXT) and os.path.isfile(p) and rom_md5(p) == md5:
                return p
    return None


class Profile:
    """What identify() knows of a ROM: title (None: no name besides the header's), name (the header's), kind
    ('official' for the four retail builds, else 'hack'), elb (None: read it from the ROM's code or a savestate),
    elb_source, rom_path (None when the file isn't found), md5."""

    def __init__(self, name, md5, rom_path):
        self.title, self.name, self.md5, self.rom_path = None, name, md5, rom_path
        self.kind = 'official' if md5 in OFFICIAL else 'hack'
        self.elb, self.elb_source = (0, 'retail build') if self.kind == 'official' else (None, 'unknown')


def _header_name(path):
    try:
        with open(path, 'rb') as f:
            head = _z64(f.read(0x40))
    except OSError:
        return ''
    return head[0x20:0x34].decode('ascii', 'replace').rstrip(' \x00') if head[:4] == b'\x80\x37\x12\x40' else ''


def identify(path):
    """What ROM a ROM file is, or a savestate was saved on (its ROM MD5 looked up in the searched folders): a Profile,
    or None when it can't be told."""
    if path.lower().endswith(ROM_EXT):
        md5 = rom_md5(path)
        return Profile(_header_name(path), md5, os.path.abspath(path)) if md5 else None
    md5 = state_rom_md5(path)
    if not md5:
        return None
    rom = find_rom(md5)
    return Profile(_header_name(rom) if rom else '', md5, rom)


def state_rom_md5(path):
    """The ROM MD5 a Mupen savestate is stamped with (32 hex characters at the start), or None."""
    try:
        head = read_savestate_head(path, 32)
        text = head.decode('ascii').lower()
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    return text if len(text) == 32 and all(c in '0123456789abcdef' for c in text) else None


def vanilla_us_rom():
    """The path of a Super Mario 64 (USA) ROM in the searched folders, or None."""
    return find_rom(US_MD5)


def machine_path(key):
    """A path set in the environment: 'blender' (STROOPMAPS_BLENDER) or 'stroop' (STROOPMAPS_STROOP); '' if unset."""
    return os.environ.get({'blender': 'STROOPMAPS_BLENDER', 'stroop': 'STROOPMAPS_STROOP'}.get(key, ''), '')


def bundle():
    """What a packaged build needs besides StroopMaps' own modules: nothing (this module is self-contained)."""
    return dict(paths=[], modules=[], datas=[], beside=[])
