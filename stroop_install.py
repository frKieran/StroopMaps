"""Install exported map sets into a STROOP folder, and switch the "Recommended" patch.

    import stroop_install as si
    stroop = si.find_stroop()                              # or a folder chosen in the window
    si.install(result_or_dir, stroop, 'SM64 Island')       # images + entries; a timestamped backup of the XML first
    si.installed_sets(stroop)                              # [InstalledSet(hack, kinds, entries, legacy)]
    si.uninstall(stroop, 'SM64 Island')
    si.patch_status(stroop)                                # 'stock' | 'patched' | 'unknown'
    si.set_patch(stroop, True)                             # swap in the patched STROOP.dll (STROOP must be closed)
    si.set_hack_override(stroop, 'off')                    # 'auto' | 'off' | a hack's name (the patch's manual override)

What goes into Config\\MapAssociations.xml (text edits only: the file's BOM, CRLF and formatting stay):

    <!-- stroopmaps:begin hack="SM64 Island" kind="hack" -->
    <MapHack name="SM64 Island" prefer="textured"><Signature address=".." value=".." /> ...</MapHack>
    <!-- stroopmaps:end hack="SM64 Island" kind="hack" -->
    <!-- stroopmaps:begin hack="SM64 Island" kind="collision" -->
    <Map name=".." id=".." level=".." area=".." hack="SM64 Island" kind="collision"> ... </Map>
    <!-- stroopmaps:end hack="SM64 Island" kind="collision" -->      (and "textured")

The comments mark a set so that it can be found, replaced and removed; the hack/kind attributes and <MapHack> are what
the patched STROOP reads (stroop_patch/README.md). Stock STROOP reads neither (its parser looks at named attributes
and elements only). Sets from before this format, marked only by a comment "<!-- SM64 Island (... stroopmaps ...) -->"
(and "... Textured ...") before their <Map> entries, are recognised as legacy sets and replaced the same way.

Every function that changes a file takes a copy of it first (Config\\MapAssociations.backup-<timestamp>.xml; the
STROOP.dll swap keeps both builds in "STROOP Archive\\stroopmaps-patch"), and checks the result before writing: the XML
parses, every <Map> id is unique (a new id that clashes with another set's is renamed), levels and areas are numbers.
"""
import dataclasses, json, os, re, shutil, sys, time
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

ASSOC = os.path.join('Config', 'MapAssociations.xml')
PATCH_BIN = os.path.join(HERE, 'stroop_patch', 'bin')       # the patched build's STROOP.dll / .pdb (build.py)
PATCH_MARK = 'stroopmaps-hack-patch'                        # UTF-16 text in a patched STROOP.dll
ARCHIVE = os.path.join('STROOP Archive', 'stroopmaps-patch')
SWAP_FILES = ('STROOP.dll', 'STROOP.pdb')
KINDS = ('collision', 'textured')


class InstallError(RuntimeError):
    """A clear, user-facing reason the install / patch did not happen (nothing was changed)."""


@dataclasses.dataclass
class InstalledSet:
    """One hack map set found in a MapAssociations.xml."""
    hack: str
    kinds: list                # ['collision', 'textured'] (those installed)
    entries: int               # <Map> entries over all kinds
    legacy: bool = False       # put in by hand (comment only), not tagged
    images: list = dataclasses.field(default_factory=list)      # image paths (relative to the map image folder)
    tag: str = ''              # the short name before its map names (<MapHack tag>, else read off the names)

    def __str__(self):
        return f'{self.hack} ({", ".join(self.kinds)}; {self.entries} maps{"; hand-installed" if self.legacy else ""})'


# ------------------------------------------------------------------------------------------------ locating
def find_stroop():
    """The configured STROOP folder (sm64io.machine_path 'stroop'), or None; the window then asks for it. A folder
    counts when it holds Config\\MapAssociations.xml."""
    import sm64io
    cands = [sm64io.machine_path('stroop')]
    for c in cands:
        if c and os.path.isfile(os.path.join(c, ASSOC)):
            return os.path.abspath(c)
    return None


def _assoc_path(stroop_dir):
    p = os.path.join(stroop_dir, ASSOC)
    if not os.path.isfile(p):
        raise InstallError(f'{stroop_dir} is not a STROOP folder (Config\\MapAssociations.xml is missing).')
    return p


# ------------------------------------------------------------------------------------------------ the file
class _File:
    """MapAssociations.xml as text with '\\n' line ends, remembering the BOM and the original line end."""

    def __init__(self, path):
        with open(path, 'rb') as fh:
            raw = fh.read()
        self.path = path
        self.bom = raw.startswith(b'\xef\xbb\xbf')
        text = raw.decode('utf-8-sig')
        self.crlf = '\r\n' in text
        self.text = text.replace('\r\n', '\n')

    def tobytes(self, text=None):
        """The file's text as bytes, with its original BOM and line endings."""
        t = self.text if text is None else text
        if self.crlf:
            t = t.replace('\n', '\r\n')
        return (b'\xef\xbb\xbf' if self.bom else b'') + t.encode('utf-8')


_BEGIN = re.compile(r'^[ \t]*<!-- stroopmaps:begin hack="([^"]*)" kind="(\w+)" -->[ \t]*\n', re.M)
_LEGACY = re.compile(r'^[ \t]*<!--\s*(.+?)\s*\([^)]*\bstroopmaps\b[^)]*\)\s*-->[ \t]*\n', re.M)
_ID = re.compile(r'<Map\b[^>]*?\bid="([^"]*)"')


def _find_blocks(text):
    """[(start, end, hack, kind, legacy)] of every set in the file, in order."""
    blocks = []
    for m in _BEGIN.finditer(text):
        end_tag = f'<!-- stroopmaps:end hack="{m.group(1)}" kind="{m.group(2)}" -->'
        e = text.find(end_tag, m.end())
        if e < 0:
            raise InstallError(f'MapAssociations.xml has "{m.group(0).strip()}" but no matching end comment; '
                               'fix or remove it by hand')
        e = text.find('\n', e) + 1 or len(text)
        blocks.append((m.start(), e, html_unescape(m.group(1)), m.group(2), False))
    for m in _LEGACY.finditer(text):
        if any(b[0] <= m.start() < b[1] for b in blocks):
            continue
        name = m.group(1).strip()
        kind = 'collision'
        if name.lower().endswith(' textured'):
            name, kind = name[:-len(' textured')].strip(), 'textured'
        pos = m.end()
        while True:                                      # the <Map> elements that follow the comment
            mm = re.compile(r'[ \t]*<Map\b').match(text, pos)
            if not mm:
                break
            close = text.find('</Map>', mm.end())
            if close < 0:
                break
            pos = text.find('\n', close) + 1 or len(text)
            if text[mm.end():close].count('<Map ') :           # a nested/unclosed one: stop, leave it alone
                break
        if pos > m.end():
            blocks.append((m.start(), pos, name, kind, True))
    return sorted(blocks)


def html_unescape(s):
    """XML attribute text unescaped (the five entities the writer uses)."""
    return s.replace('&quot;', '"').replace('&lt;', '<').replace('&gt;', '>').replace('&amp;', '&')


def _attr(s):
    return s.replace('&', '&amp;').replace('"', '&quot;').replace('<', '&lt;')


def _elem_text(e):
    e = ET.fromstring(ET.tostring(e))
    ET.indent(e, '  ')
    return '\n'.join('  ' + ln for ln in ET.tostring(e, encoding='unicode').splitlines()) + '\n'


def _image_dir(text):
    m = re.search(r'<MapImageDirectory>(.*?)</MapImageDirectory>', text)
    return (m.group(1).strip() if m else 'Resources\\Maps\\Map Images\\').replace('\\', os.sep)


def _scan(text):
    """{hack: InstalledSet} for the file text."""
    out, names = {}, {}
    for s, e, hack, kind, legacy in _find_blocks(text):
        blk = text[s:e]
        if kind == 'hack':
            st = out.setdefault(hack, InstalledSet(hack, [], 0))
            m = re.search(r'<MapHack\b[^>]*\btag="([^"]*)"', blk)
            if m:
                st.tag = html_unescape(m.group(1))
            continue
        n = len(re.findall(r'<Map\b', blk))
        imgs = [m.replace('\\', os.sep) for m in re.findall(r'<Image\s+path="([^"]*)"', blk)]
        st = out.setdefault(hack, InstalledSet(hack, [], 0, legacy))
        if kind not in st.kinds:
            st.kinds.append(kind)
        st.entries += n
        st.images += imgs
        st.legacy = st.legacy and legacy
        names.setdefault(hack, []).extend(html_unescape(n) for n in re.findall(r'<Map\b[^>]*?\bname="([^"]*)"', blk))
    for hack, st in out.items():                         # no tag attribute (older installs): the names' first word
        ns = names.get(hack, [])
        firsts = {n.split()[0] for n in ns if n.split()}
        if not st.tag and len(firsts) == 1 and all(len(n.split()) > 1 for n in ns):
            st.tag = firsts.pop()
    return out


def installed_sets(stroop_dir):
    """[InstalledSet] of the map sets in the STROOP folder's MapAssociations.xml (tagged and hand-installed)."""
    return list(_scan(_File(_assoc_path(stroop_dir)).text).values())


def suggest_tag(hack, stroop_dir=None, remembered=None):
    """The short name for `hack`'s maps: `remembered` (the GUI's last one for this hack), else the tag this hack has
    installed in the STROOP folder, else exporter.hack_tag of the name, unique against the other installed hacks'."""
    if remembered:
        return remembered
    sets = []
    if stroop_dir:
        try:
            sets = installed_sets(stroop_dir)
        except Exception:                                  # noqa: BLE001 - no or a broken STROOP folder
            sets = []
    for st in sets:
        if st.hack == hack and st.tag:
            return st.tag
    import exporter
    return exporter.hack_tag(hack, taken=[st.tag for st in sets if st.hack != hack and st.tag])


# ------------------------------------------------------------------------------------------------ checks
def _check(text, stroop_dir, log):
    """The result of an edit: raises InstallError when it can't be right. Returns the ET root."""
    try:
        root = ET.fromstring(text.encode('utf-8'))
    except ET.ParseError as e:
        raise InstallError(f'the edited MapAssociations.xml would not parse ({e}); nothing was changed') from e
    seen = {}
    for m in root.iter('Map'):
        i = m.get('id')
        if not i:
            raise InstallError('a <Map> has no id')
        if i in seen:
            raise InstallError(f'duplicate map id {i!r}; nothing was changed')
        seen[i] = m
        for a in ('level', 'area'):
            if not (m.get(a) or '').isdigit():
                raise InstallError(f'map {i} has a non-numeric {a}={m.get(a)!r}')
        if m.find('Image') is None or m.find('Coordinates') is None:
            raise InstallError(f'map {i} has no <Image> or <Coordinates>')
    imgdir = os.path.join(stroop_dir, _image_dir(text))
    for i, m in seen.items():
        if m.get('hack') and not os.path.isfile(os.path.join(imgdir, m.find('Image').get('path').replace('\\', os.sep))):
            log(f'warning: map {i} ({m.get("name")}) points at a missing image {m.find("Image").get("path")}')
    return root


def _backup(stroop_dir, f, log):
    stamp = time.strftime('%Y%m%d-%H%M%S')
    dst = os.path.join(stroop_dir, 'Config', f'MapAssociations.backup-{stamp}.xml')
    n = 2
    while os.path.exists(dst):
        dst = os.path.join(stroop_dir, 'Config', f'MapAssociations.backup-{stamp}-{n}.xml')
        n += 1
    shutil.copy2(f.path, dst)
    log(f'backup: {dst}')
    return dst


def _write(f, text):
    tmp = f.path + '.tmp'
    with open(tmp, 'wb') as fh:
        fh.write(f.tobytes(text))
    os.replace(tmp, f.path)


# ------------------------------------------------------------------------------------------------ reading an export
def _read_export(result_or_dir, hack):
    """({kind: [<Map> elements]}, <MapHack> element or None, {kind: image folder}, export dir, hack) of one hack."""
    d = getattr(result_or_dir, 'out_dir', None) or result_or_dir
    d = str(d)
    if not os.path.isdir(d):
        raise InstallError(f'{d} is not a folder (make the maps first)')
    man = os.path.join(d, 'export.json')
    sets = []
    if os.path.isfile(man):
        with open(man, encoding='utf-8') as fh:
            sets = json.load(fh)['sets']
    else:
        for fn in sorted(os.listdir(d)):
            m = re.match(r'MapAssociations_(.+?)(_textured)?\.xml$', fn)
            if m:
                sets.append({'hack': None, 'kind': 'textured' if m.group(2) else 'collision', 'xml': fn,
                             'folder': None})
    if not sets:
        raise InstallError(f'{d} holds no exported maps (no export.json or MapAssociations_*.xml)')
    parsed = []
    for s in sets:
        xml = os.path.join(d, s['xml'])
        if not os.path.isfile(xml):
            continue
        with open(xml, encoding='utf-8') as fh:
            root = ET.fromstring(('<r>' + fh.read() + '</r>').encode('utf-8'))
        maps = list(root.iter('Map'))
        mh = root.find('MapHack')
        h = s['hack'] or (mh.get('name') if mh is not None else None) or (maps[0].get('hack') if maps else None)
        parsed.append((h, s['kind'], maps, mh, s.get('folder')))
    hacks = sorted({p[0] for p in parsed if p[0]})
    if hack is None:
        if len(hacks) != 1:
            raise InstallError(f'{d} holds several hacks ({hacks}); name the one to install')
        hack = hacks[0]
    mine = [p for p in parsed if p[0] == hack]
    if not mine:
        raise InstallError(f'{d} holds no maps for {hack!r} (it holds: {hacks})')
    maps = {k: [] for k in KINDS}
    folders, mh = {}, None
    for h, kind, ms, hel, folder in mine:
        maps[kind] += ms
        mh = mh if mh is not None else hel
        if folder:
            folders[kind] = folder
        elif ms:
            folders[kind] = ms[0].find('Image').get('path').split('\\')[0]
    return {k: v for k, v in maps.items() if v}, mh, folders, d, hack


# ------------------------------------------------------------------------------------------------ install / uninstall
def _remove_blocks(text, hack, kinds=None):
    """text without the hack's blocks (all kinds, or `kinds` + the hack block when none remain)."""
    blocks = [b for b in _find_blocks(text) if b[2] == hack]
    if kinds == ['hack']:                                  # only the <MapHack> block
        blocks = [b for b in blocks if b[3] == 'hack']
    elif kinds is not None:
        left = {b[3] for b in blocks if b[3] != 'hack' and b[3] not in kinds}
        blocks = [b for b in blocks if b[3] in kinds or (b[3] == 'hack' and not left)]
    for s, e, *_ in sorted(blocks, reverse=True):
        # take the blank line before the block with it, so repeated installs don't leave gaps
        while s > 1 and text[s - 1] == '\n' and text[s - 2] == '\n':
            s -= 1
        text = text[:s] + text[e:]
    return text, blocks


def _retag_legacy(text, hack, keep_kinds):
    """The hack's hand-installed (comment-only) sets of `keep_kinds`, tagged like our own: begin/end comments and
    hack/kind attributes on each <Map>, so the patched STROOP can see them. Everything else stays as it is."""
    for s, e, h, kind, legacy in sorted(_find_blocks(text), reverse=True):
        if not legacy or h != hack or kind not in keep_kinds:
            continue
        blk = text[s:e]
        first = blk.index('\n') + 1
        body = re.sub(r'<Map\b(?![A-Za-z])', f'<Map hack="{_attr(hack)}" kind="{kind}"', blk[first:])
        text = (text[:s] + f'  <!-- stroopmaps:begin hack="{_attr(hack)}" kind="{kind}" -->\n' + body +
                f'  <!-- stroopmaps:end hack="{_attr(hack)}" kind="{kind}" -->\n' + text[e:])
    return text


_KIND_SUFFIX = re.compile(r' \((?:collision|textured)\)$')
_MAP_HEAD = re.compile(r'<Map\b[^>]*>')


def _area_key(head):
    """An entry's area as STROOP keys maps: level, area, loading point, mission layout."""
    return tuple((re.search(r'\b' + a + r'="([^"]*)"', head) or [None, None])[1]
                 for a in ('level', 'area', 'loadingPoint', 'missionLayout'))


def kind_names(text, hack):
    """The hack's map names say their kind, " (collision)" / " (textured)", only for an area that has both kinds
    installed; otherwise the name is the area's alone. Run after every install / uninstall."""
    blocks = [b for b in _find_blocks(text) if b[2] == hack and b[3] in KINDS]
    kinds = {}
    for s, e, h, kind, legacy in blocks:
        for m in _MAP_HEAD.finditer(text, s, e):
            kinds.setdefault(_area_key(m.group(0)), set()).add(kind)

    def rename(m, kind):
        head = m.group(0)
        both = len(kinds.get(_area_key(head), ())) > 1
        nm = re.search(r'\bname="([^"]*)"', head)
        if not nm:
            return head
        base = _KIND_SUFFIX.sub('', nm.group(1))
        new = f'{base} ({kind})' if both else base
        return head[:nm.start(1)] + new + head[nm.end(1):]
    for s, e, h, kind, legacy in sorted(blocks, reverse=True):
        blk = _MAP_HEAD.sub(lambda m: rename(m, kind), text[s:e])
        text = text[:s] + blk + text[e:]
    return text


def install(result_or_dir, stroop_dir, hack=None, prefer=None, log=print):
    """Put the exported maps of `hack` into the STROOP folder: copy the images to the map image folder
    (Resources\\Maps\\Map Images\\<folder>\\), replace that hack's entries (and its legacy hand-installed ones) in
    Config\\MapAssociations.xml, after a timestamped backup of it. `result_or_dir` is the exporter.ExportResult or its
    out_dir; `hack` may be None when the folder holds one hack. Only the kinds the export holds are replaced.
    Returns {'hack', 'kinds', 'entries', 'backup', 'renamed_ids'}."""
    log = log or (lambda *_: None)
    maps, mh, folders, src_dir, hack = _read_export(result_or_dir, hack)
    path = _assoc_path(stroop_dir)
    f = _File(path)
    base = _retag_legacy(f.text, hack, [k for k in KINDS if k not in maps])
    text, old = _remove_blocks(base, hack, kinds=list(maps))
    old_imgs = []
    for s, e, h, kind, legacy in old:
        old_imgs += re.findall(r'<Image\s+path="([^"]*)"', base[s:e])

    # ids: unique against everything that stays; a clash is renamed
    taken = set(_ID.findall(text))
    renamed = {}
    for ms in maps.values():
        for m in ms:
            i = m.get('id')
            if i in taken:
                k = 2
                while f'{i[0]}{k}{i[1:]}' in taken:
                    k += 1
                renamed[i] = f'{i[0]}{k}{i[1:]}'
                m.set('id', renamed[i])
                i = renamed[i]
            taken.add(i)
    if renamed:
        log(f'ids renamed because other sets use them: {renamed}')

    if mh is not None:
        after = set(maps) | {b[3] for b in _find_blocks(text) if b[2] == hack and b[3] != 'hack'}
        mh.set('prefer', prefer if prefer in after else ('textured' if 'textured' in after else 'collision'))
    ind = '  '
    out = ''
    if mh is not None:
        text, _ = _remove_blocks(text, hack, kinds=['hack'])          # an older <MapHack> block of the hack
        out += f'{ind}<!-- stroopmaps:begin hack="{_attr(hack)}" kind="hack" -->\n{_elem_text(mh)}' \
               f'{ind}<!-- stroopmaps:end hack="{_attr(hack)}" kind="hack" -->\n'
    for kind in KINDS:
        if kind in maps:
            body = ''.join(_elem_text(m) for m in maps[kind])
            out += f'{ind}<!-- stroopmaps:begin hack="{_attr(hack)}" kind="{kind}" -->\n{body}' \
                   f'{ind}<!-- stroopmaps:end hack="{_attr(hack)}" kind="{kind}" -->\n'
    close = text.rfind('</MapAssociations>')
    if close < 0:
        raise InstallError('MapAssociations.xml has no </MapAssociations>')
    ls = text.rfind('\n', 0, close) + 1
    new_text = text[:ls] + out + text[ls:]
    new_text = kind_names(new_text, hack)
    _check(new_text, stroop_dir, lambda m: None)         # raises before anything is written

    img_root = os.path.join(stroop_dir, _image_dir(new_text))
    backup = _backup(stroop_dir, f, log)
    for rel in old_imgs:                                  # the replaced set's own images go; the new ones come
        p = os.path.join(img_root, rel.replace('\\', os.sep))
        if os.path.isfile(p):
            os.remove(p)
    copied = 0
    for kind, folder in folders.items():
        sdir = os.path.join(src_dir, 'Map Images', folder)
        ddir = os.path.join(img_root, folder)
        os.makedirs(ddir, exist_ok=True)
        wanted = {m.find('Image').get('path').split('\\')[-1] for m in maps[kind]}
        for fn in sorted(wanted):
            shutil.copy2(os.path.join(sdir, fn), os.path.join(ddir, fn))
            copied += 1
    _write(f, new_text)
    _check(new_text, stroop_dir, log)                      # warns about entries whose images are missing
    n = sum(len(v) for v in maps.values())
    log(f'installed {hack}: {n} map entries ({", ".join(maps)}), {copied} images -> {img_root}')
    return {'hack': hack, 'kinds': list(maps), 'entries': n, 'backup': backup, 'renamed_ids': renamed}


def uninstall(stroop_dir, hack, kinds=None, log=print):
    """Remove the hack's entries (tagged or hand-installed; only `kinds` when given) from MapAssociations.xml, after a
    timestamped backup, and delete the images they pointed at (and the folder when it is then empty)."""
    log = log or (lambda *_: None)
    f = _File(_assoc_path(stroop_dir))
    text, blocks = _remove_blocks(f.text, hack, kinds=list(kinds) if kinds else None)
    if not any(b[3] != 'hack' for b in blocks):
        raise InstallError(f'{hack!r} is not installed (installed: {[s.hack for s in installed_sets(stroop_dir)]})')
    text = kind_names(text, hack)                          # the kind that stays loses its " (kind)"
    _check(text, stroop_dir, lambda m: log(m))
    imgs = []
    for s, e, h, kind, legacy in blocks:
        imgs += re.findall(r'<Image\s+path="([^"]*)"', f.text[s:e])
    img_root = os.path.join(stroop_dir, _image_dir(f.text))
    _backup(stroop_dir, f, log)
    _write(f, text)
    n, busy = 0, []
    for rel in imgs:
        p = os.path.join(img_root, rel.replace('\\', os.sep))
        if os.path.isfile(p):
            try:
                os.remove(p)
                n += 1
            except PermissionError:                         # a running STROOP holds the images it has shown
                busy.append(rel)
        d = os.path.dirname(p)
        if os.path.isdir(d) and d != img_root and not os.listdir(d):
            os.rmdir(d)
    log(f'uninstalled {hack}: removed {len(imgs)} entries and {n} images')
    if busy:
        log(f'{len(busy)} images are in use (STROOP is running) and stay until deleted by hand: {busy}')


# ------------------------------------------------------------------------------------------------ the patch
def _dll_has(path, text):
    with open(path, 'rb') as fh:
        return text.encode('utf-16-le') in fh.read()


def patch_status(stroop_dir):
    """'patched' when STROOP.dll is the hack-maps build, 'stock' when it is a STROOP build without it, 'unknown' when
    there is no readable STROOP.dll."""
    dll = os.path.join(stroop_dir, 'STROOP.dll')
    try:
        if _dll_has(dll, PATCH_MARK):
            return 'patched'
        return 'stock' if _dll_has(dll, 'Refactor ') else 'unknown'
    except OSError:
        return 'unknown'


def patch_info():
    """The patched build's description (stroop_patch/bin/patched.json: base version text, built date), or None when the
    build isn't there (run stroop_patch/build.py)."""
    p = os.path.join(PATCH_BIN, 'patched.json')
    if not (os.path.isfile(p) and os.path.isfile(os.path.join(PATCH_BIN, 'STROOP.dll'))):
        return None
    with open(p) as fh:
        return json.load(fh)


def _same(a, b):
    with open(a, 'rb') as fa, open(b, 'rb') as fb:
        return fa.read() == fb.read()


def _copy_if_different(src, dst):
    if os.path.isfile(dst):
        with open(src, 'rb') as a, open(dst, 'rb') as b:
            if a.read() == b.read():
                return False
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(src, dst)
    return True


def set_patch(stroop_dir, on, log=print):
    """Install (on=True) or remove (on=False) the patch that makes "Recommended" pick a romhack's maps, by swapping
    STROOP.dll and STROOP.pdb for the patched or the stock build. Both are kept in
    "STROOP Archive\\stroopmaps-patch\\stock" and "...\\patched"; the stock files are archived before the first swap.
    STROOP must be closed (its dll is locked while it runs). Needs the patched build in stroop_patch\\bin
    (stroop_patch/build.py) and a STROOP of the version the patch was made for."""
    log = log or (lambda *_: None)
    _assoc_path(stroop_dir)
    status = patch_status(stroop_dir)
    arch = os.path.join(stroop_dir, ARCHIVE)
    if on:
        info = patch_info()
        if status == 'patched':
            # an older patched build (the patch was rebuilt since) is replaced by the new one
            new = os.path.join(PATCH_BIN, 'STROOP.dll')
            if not info or _same(new, os.path.join(stroop_dir, 'STROOP.dll')):
                log('already patched')
                return
        elif not info:
            raise InstallError('the patched build is not here (stroop_patch/bin/STROOP.dll): run '
                               'stroop_patch/build.py first, or put the STROOP.dll of a StroopMaps release there')
        if status not in ('stock', 'patched') or \
                not _dll_has(os.path.join(stroop_dir, 'STROOP.dll'), info['base_text']):
            raise InstallError(f'this patch is for STROOP "{info["base_text"]}"; the STROOP.dll here is a different '
                               'version (or not STROOP). Nothing was changed.')
        for fn in SWAP_FILES:                              # archive the stock files (never overwrite an older copy)
            cur = os.path.join(stroop_dir, fn)
            if status == 'stock' and os.path.isfile(cur):
                kept = os.path.join(arch, 'stock', fn)
                if not os.path.isfile(kept):
                    _copy_if_different(cur, kept)
                elif not _same(cur, kept):                               # another stock build: keep it too
                    _copy_if_different(cur, kept + time.strftime('.%Y%m%d-%H%M%S'))
        for fn in SWAP_FILES:
            src = os.path.join(PATCH_BIN, fn)
            if os.path.isfile(src):
                _copy_if_different(src, os.path.join(arch, 'patched', fn))
        try:
            for fn in SWAP_FILES:
                src = os.path.join(PATCH_BIN, fn)
                if os.path.isfile(src):
                    shutil.copy2(src, os.path.join(stroop_dir, fn))
        except PermissionError as e:
            raise InstallError('STROOP.dll is in use: close STROOP and try again') from e
        log(f'patched build installed ({info["base_text"]} with the hack-maps patch); stock files kept in {arch}\\stock')
    else:
        if status == 'stock':
            log('already stock')
            return
        stock = os.path.join(arch, 'stock', 'STROOP.dll')
        if not os.path.isfile(stock):
            raise InstallError(f'the stock STROOP.dll was not archived (expected at {stock}); restore STROOP by hand')
        try:
            for fn in SWAP_FILES:
                src = os.path.join(arch, 'stock', fn)
                if os.path.isfile(src):
                    shutil.copy2(src, os.path.join(stroop_dir, fn))
        except PermissionError as e:
            raise InstallError('STROOP.dll is in use: close STROOP and try again') from e
        log('stock build restored')


def set_hack_override(stroop_dir, value, log=print):
    """The patched STROOP's manual override, `<Config><HackOverride>`: 'auto' (None; recognise the running hack by its
    signature), 'off' (never use a hack's maps: today's behaviour) or a hack's name (always use that hack's maps).
    Takes effect when STROOP starts. Stock STROOP ignores it."""
    f = _File(_assoc_path(stroop_dir))
    text = re.sub(r'[ \t]*<HackOverride>.*?</HackOverride>[ \t]*\n', '', f.text)
    if value and value != 'auto':
        close = text.find('</Config>')
        if close < 0:
            raise InstallError('MapAssociations.xml has no <Config> element')
        ls = text.rfind('\n', 0, close) + 1
        esc = value.replace('&', '&amp;').replace('<', '&lt;')
        text = text[:ls] + f'    <HackOverride>{esc}</HackOverride>\n' + text[ls:]
    if text != f.text:
        _check(text, stroop_dir, lambda m: None)
        _backup(stroop_dir, f, log or (lambda *_: None))
        _write(f, text)


def get_hack_override(stroop_dir):
    """The patched STROOP's HackOverride: 'auto', 'off' or a hack's name."""
    m = re.search(r'<HackOverride>(.*?)</HackOverride>', _File(_assoc_path(stroop_dir)).text)
    return m.group(1).strip() if m else 'auto'
