"""Tests of stroop_install on a synthetic STROOP folder (never a real one).

    python -m unittest discover -s tests -p "test_install*.py"
"""
import os, re, shutil, sys, tempfile, unittest
import xml.etree.ElementTree as ET
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
import exporter, stroop_install as si                  # noqa: E402
from test_export_unit import FakeSource                # noqa: E402

VANILLA = '''<?xml version="1.0" encoding="utf-8" ?>
<MapAssociations>
  <Config>
    <MapImageDirectory>Resources\\Maps\\Map Images\\</MapImageDirectory>
    <DefaultImage>default.png</DefaultImage>
  </Config>

  <Background name="Blue Background">
    <Image path="Blue Background.png"/>
  </Background>

  <Map name="BoB" id="009" level="9" area="1">
    <Image path="BoB.png"/>
    <Coordinates x1="-8191" x2="8192" z1="-8191" z2="8192"/>
    <BackgroundImage name="Blue Background"/>
  </Map>
LEGACY</MapAssociations>
'''
LEGACY = '''
  <!-- SM64 Island (stroopmaps, 2026-10-22) -->
  <Map name="Island Castle" id="I061A" level="6" area="1">
    <Image path="SM64 Island\\castle.png" sizeX="986" sizeY="1345" />
    <Coordinates x1="-1966" x2="1978" z1="-342" z2="5038" />
    <BackgroundImage name="Black Background" path="Black Background.png" />
  </Map>
  <!-- SM64 Island Textured (stroopmaps, 2026-10-22) -->
  <Map name="Island Castle (textured)" id="IT061A" level="6" area="1">
    <Image path="SM64 Island Textured\\castle.png" sizeX="986" sizeY="1345" />
    <Coordinates x1="-1966" x2="1978" z1="-342" z2="5038" />
    <BackgroundImage name="Black Background" path="Black Background.png" />
  </Map>
'''


def wb(path, data):
    with open(path, 'wb') as f:
        f.write(data)


def rb(path):
    with open(path, 'rb') as f:
        return f.read()


def make_stroop(root, legacy=True, bom=True, crlf=True):
    os.makedirs(os.path.join(root, 'Config'))
    img = os.path.join(root, 'Resources', 'Maps', 'Map Images')
    for sub in ('SM64 Island', 'SM64 Island Textured'):
        os.makedirs(os.path.join(img, sub))
        wb(os.path.join(img, sub, 'castle.png'), b'old')
    wb(os.path.join(img, 'BoB.png'), b'bob')
    text = VANILLA.replace('LEGACY', LEGACY if legacy else '')
    if crlf:
        text = text.replace('\n', '\r\n')
    wb(os.path.join(root, 'Config', 'MapAssociations.xml'), (b'\xef\xbb\xbf' if bom else b'') + text.encode('utf-8'))
    wb(os.path.join(root, 'STROOP.dll'), 'Refactor 0.9.2'.encode('utf-16-le'))


def read(root):
    return rb(os.path.join(root, 'Config', 'MapAssociations.xml'))


class Install(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.st = os.path.join(self.tmp, 'stroop')
        make_stroop(self.st)
        self.out = os.path.join(self.tmp, 'out')
        a = FakeSource(6, 1, 'Castle', 'SM64 Island')
        self.res = exporter.export([a], kinds=('collision',), out_dir=self.out, log=None,
                                   options={'signature': False})

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_recognises_the_hand_installed_set(self):
        sets = si.installed_sets(self.st)
        self.assertEqual([(s.hack, sorted(s.kinds), s.entries, s.legacy) for s in sets],
                         [('SM64 Island', ['collision', 'textured'], 2, True)])

    def test_install_replaces_keeps_format_and_backs_up(self):
        before = read(self.st)
        r = si.install(self.res, self.st, 'SM64 Island', log=None)
        after = read(self.st)
        self.assertTrue(after.startswith(b'\xef\xbb\xbf'))
        self.assertEqual(after.count(b'\r\n'), after.count(b'\n'))             # CRLF all through
        root = ET.fromstring(after.decode('utf-8-sig').encode())
        maps = {m.get('id'): m for m in root.iter('Map')}
        self.assertEqual(len(maps), len(list(root.iter('Map'))))
        self.assertIn('009', maps)                                             # the vanilla map is untouched
        self.assertNotIn('I061A_', maps)
        tagged = [m for m in maps.values() if m.get('hack')]
        self.assertEqual({(m.get('hack'), m.get('kind')) for m in tagged},
                         {('SM64 Island', 'collision'), ('SM64 Island', 'textured')})   # the legacy textured set was tagged
        self.assertEqual(len(root.findall('MapHack')), 1)
        self.assertTrue(os.path.isfile(r['backup']))
        self.assertEqual(rb(r['backup']), before)
        img = os.path.join(self.st, 'Resources', 'Maps', 'Map Images', 'SM64 Island', 'castle.png')
        self.assertNotEqual(rb(img), b'old')                    # replaced by the new image
        # installing again replaces rather than adds
        n = len(tagged)
        si.install(self.res, self.st, 'SM64 Island', log=None)
        root = ET.fromstring(read(self.st).decode('utf-8-sig').encode())
        self.assertEqual(len([m for m in root.iter('Map') if m.get('hack')]), n)
        self.assertEqual(len(root.findall('MapHack')), 1)

    def test_kind_in_names_only_when_an_area_has_both(self):
        # single-kind exports name maps by area alone; installed side by side, each says its kind; uninstall one and
        # the other's name is the area's again
        out = os.path.join(self.tmp, 'tex')
        os.makedirs(os.path.join(out, 'Map Images', 'SM64 Island Textured'))
        Image.new('RGB', (2, 2)).save(os.path.join(out, 'Map Images', 'SM64 Island Textured', 'castle.png'))
        with open(os.path.join(self.out, 'MapAssociations_island.xml'), encoding='utf-8') as fh:
            xml = fh.read()
        xml = xml.replace('kind="collision"', 'kind="textured"').replace('SM64 Island\\', 'SM64 Island Textured\\')
        xml = re.sub(r'id="I', 'id="IT', xml)
        with open(os.path.join(out, 'MapAssociations_island_textured.xml'), 'w', encoding='utf-8') as fh:
            fh.write(xml)
        shutil.rmtree(self.st)
        make_stroop(self.st, legacy=False)                                   # no hand-installed textured set
        def names():
            root = ET.fromstring(read(self.st).decode('utf-8-sig').encode())
            return {(m.get('kind'), m.get('name')) for m in root.iter('Map') if m.get('hack')}
        si.install(self.res, self.st, 'SM64 Island', log=None)
        base = {n for k, n in names()}
        self.assertEqual(len(base), 1)
        base = base.pop()
        self.assertFalse(base.endswith(')'))
        si.install(out, self.st, 'SM64 Island', log=None)
        both = {('collision', base + ' (collision)'), ('textured', base + ' (textured)')}
        self.assertEqual(names(), both)
        si.install(out, self.st, 'SM64 Island', log=None)                      # not marked twice
        self.assertEqual(names(), both)
        si.uninstall(self.st, 'SM64 Island', kinds=['collision'], log=None)
        self.assertEqual(names(), {('textured', base)})

    def test_tags_read_back_and_suggested(self):
        si.install(self.res, self.st, 'SM64 Island', log=None)               # MapHack tag="Island" (hack_tag)
        tags = {s.hack: s.tag for s in si.installed_sets(self.st)}
        self.assertEqual(tags['SM64 Island'], 'Island')
        self.assertEqual(si.suggest_tag('SM64 Island', self.st), 'Island')       # installed: kept
        self.assertEqual(si.suggest_tag('SM64 Island', self.st, 'Isl'), 'Isl')   # remembered wins
        self.assertEqual(si.suggest_tag('Super Mario Island', self.st), 'Island2')   # unique against the others
        f = os.path.join(self.st, 'Config', 'MapAssociations.xml')               # an older install: no tag attribute
        wb(f, rb(f).replace(b' tag="Island"', b''))
        self.assertEqual({s.hack: s.tag for s in si.installed_sets(self.st)}['SM64 Island'], 'Island')   # names

    def test_uninstall_restores_the_vanilla_file(self):
        si.install(self.res, self.st, 'SM64 Island', log=None)
        si.uninstall(self.st, 'SM64 Island', log=None)
        root = ET.fromstring(read(self.st).decode('utf-8-sig').encode())
        self.assertEqual([m.get('id') for m in root.iter('Map')], ['009'])
        self.assertEqual(si.installed_sets(self.st), [])
        self.assertTrue(os.path.isfile(os.path.join(self.st, 'Resources', 'Maps', 'Map Images', 'BoB.png')))
        with self.assertRaises(si.InstallError):
            si.uninstall(self.st, 'SM64 Island', log=None)

    def test_id_clash_with_another_set_is_renamed(self):
        make = lambda hack, d: exporter.export([FakeSource(9, 1, 'Test', hack)], kinds=('collision',),
                                               out_dir=os.path.join(self.tmp, d), log=None,
                                               options={'signature': False, 'id_prefix': 'X'})
        si.install(make('Hack One', 'o1'), self.st, 'Hack One', log=None)
        r = si.install(make('Hack Two', 'o2'), self.st, 'Hack Two', log=None)
        self.assertTrue(r['renamed_ids'])
        root = ET.fromstring(read(self.st).decode('utf-8-sig').encode())
        ids = [m.get('id') for m in root.iter('Map')]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(sorted(s.hack for s in si.installed_sets(self.st)), ['Hack One', 'Hack Two', 'SM64 Island'])

    def test_hack_override(self):
        si.set_hack_override(self.st, 'off', log=None)
        self.assertEqual(si.get_hack_override(self.st), 'off')
        si.set_hack_override(self.st, 'SM64 Island', log=None)
        self.assertEqual(si.get_hack_override(self.st), 'SM64 Island')
        si.set_hack_override(self.st, 'auto', log=None)
        self.assertEqual(si.get_hack_override(self.st), 'auto')
        ET.fromstring(read(self.st).decode('utf-8-sig').encode())

    def test_not_a_stroop_folder(self):
        with self.assertRaises(si.InstallError):
            si.installed_sets(self.tmp)

    def test_patch_status_and_swap(self):
        self.assertEqual(si.patch_status(self.st), 'stock')
        if not si.patch_info():
            self.skipTest('the patched build is not built (stroop_patch/build.py)')
        si.set_patch(self.st, True, log=None)
        # the fake dll only holds the version text, so the stock one is archived and swapped for the real patched one
        self.assertEqual(si.patch_status(self.st), 'patched')
        si.set_patch(self.st, False, log=None)
        self.assertEqual(si.patch_status(self.st), 'stock')
        self.assertEqual(rb(os.path.join(self.st, 'STROOP.dll')), 'Refactor 0.9.2'.encode('utf-16-le'))

    def test_patch_replaces_an_older_patched_build(self):
        if not si.patch_info():
            self.skipTest('the patched build is not built (stroop_patch/build.py)')
        wb(os.path.join(self.st, 'STROOP.dll'), 'Refactor 0.9.2 + hack maps stroopmaps-hack-patch'.encode('utf-16-le'))
        self.assertEqual(si.patch_status(self.st), 'patched')
        logs = []
        si.set_patch(self.st, True, log=logs.append)
        self.assertEqual(rb(os.path.join(self.st, 'STROOP.dll')), rb(os.path.join(si.PATCH_BIN, 'STROOP.dll')))
        # the old patched build isn't archived as "stock"
        self.assertFalse(os.path.exists(os.path.join(self.st, si.ARCHIVE, 'stock')))
        si.set_patch(self.st, True, log=logs.append)
        self.assertEqual(logs[-1], 'already patched')

    def test_patch_refuses_other_versions(self):
        wb(os.path.join(self.st, 'STROOP.dll'), 'Refactor 0.10.0'.encode('utf-16-le'))
        if si.patch_info():
            with self.assertRaises(si.InstallError):
                si.set_patch(self.st, True, log=None)


if __name__ == '__main__':
    unittest.main()
