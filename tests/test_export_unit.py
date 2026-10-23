"""Quick tests of sources / exporter on a synthetic area source (no savestate, no Blender).

    python -m unittest discover -s tests -p "test_export*.py"
"""
import json, os, struct, sys, tempfile, unittest

import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import exporter, render_textured, sources      # noqa: E402


class FakeSource:
    """An area source made of two floors (a 400-unit square at y 0 and a ledge at y 500) and a wall."""

    def __init__(self, level=9, area=1, name='Test Area', hack='Test Hack'):
        self.name, self.hack, self.level, self.area, self.shift, self.mario = name, hack, level, area, 0, None
        sq = lambda y, x0, x1: [np.array([[x0, y, 0], [x0, y, x1], [x1, y, 0]], float),
                                np.array([[x1, y, 0], [x0, y, x1], [x1, y, x1]], float)]
        self.tris = []
        for v in sq(0, 0, 400) + sq(500, 100, 300):
            n = np.cross(v[1] - v[0], v[2] - v[0])
            self.tris.append(('floor', 0, v, n / np.linalg.norm(n) * (1 if n[1] > 0 else -1)))
        self.tris.append(('wall', 0, np.array([[0, 0, 0], [0, 100, 0], [0, 0, 400]], float), np.array([1.0, 0, 0])))
        self.notes = []

    def visual(self, objects=True, light=None):
        raise AssertionError('collision export must not need the display lists')


class NamesAndSignatures(unittest.TestCase):
    def test_clean_hack_name(self):
        c = sources.clean_hack_name
        self.assertEqual(c('SM64 Island 1.1'), 'SM64 Island')
        self.assertEqual(c('Super Mario 64 (USA)'), 'Super Mario 64')
        self.assertEqual(c('Super Mario The Lost Dreams v1.2h'), 'Super Mario The Lost Dreams')
        self.assertEqual(c('SUPER MARIO 64'), 'Super Mario 64')

    def test_hack_tag(self):
        self.assertEqual(exporter.hack_tag('SM64 Island'), 'Island')
        self.assertEqual(exporter.hack_tag('Super Mario The Lost Dreams'), 'TLD')
        self.assertEqual(exporter.hack_tag('Super Mario Star Road'), 'SR')
        self.assertEqual(exporter.hack_tag('Super Mario 64 Preservation Project'), 'PP')
        self.assertEqual(exporter.hack_tag('Super Mario 74 Ten Years After'), '74TYA')
        self.assertEqual(exporter.hack_tag('Test Hack'), 'TH')
        self.assertEqual(exporter.hack_tag('SM THE LOST DREAMS'), 'TLD')          # a ROM header, all capitals
        self.assertEqual(exporter.hack_tag('SS1Repainted'), 'SS1R')               # camelCase, a capitals run whole
        self.assertEqual(exporter.hack_tag('Super Mario 74 Ex V1.5'), '74E')      # version tail off
        self.assertEqual(exporter.hack_tag('Super Mario 64 (patched)'), 'Hack')   # nothing but generic words
        self.assertEqual(exporter.hack_tag('Super Mario The Lost Dreams', taken=['tld', 'TLD2']), 'TLD3')
        self.assertTrue(sources.is_generic_title('SUPER MARIO 64'))
        self.assertFalse(sources.is_generic_title('SM64 ISLAND'))

    def test_hack_slug_and_folder(self):
        self.assertEqual(exporter.hack_slug('SM64 Island'), 'island')
        self.assertEqual(exporter.hack_slug('Super Mario The Lost Dreams'), 'super_mario_the_lost_dreams')
        self.assertEqual(exporter.folder_name('A: b/c'), 'A_ b_c')

    def test_signature_from_words(self):
        tmp = tempfile.mkdtemp()
        rom = os.path.join(tmp, 'fake_vanilla.z64')
        base = bytes(range(256)) * 4096                                  # 1 MiB of known words
        with open(rom, 'wb') as f:
            f.write(b'\x80\x37\x12\x40' + base[4:])
        try:
            with open(rom, 'rb') as fh:
                data = fh.read()
            changed = {sources.TEXT_BASE + 0x1000 + 4 * i: 0xDEADBEEF for i in range(50)}

            def same(a):
                return struct.unpack_from('>I', data, a - sources.TEXT_BASE + sources.TEXT_ROM)[0]
            sig = sources.signature_from_words(lambda a: changed.get(a, same(a)), rom)
            self.assertEqual(len(sig), 8)
            self.assertTrue(all(v == 0xDEADBEEF and a in changed for a, v in sig))
            self.assertEqual(sources.signature_from_words(same, rom), [])
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


class ExportCollision(unittest.TestCase):
    def test_export_writes_images_xml_manifest(self):
        with tempfile.TemporaryDirectory() as d:
            seen = []
            a, b = FakeSource(9, 1, 'Test Area'), FakeSource(12, 2, 'Test Area', 'Test Hack')
            res = exporter.export([a, b], kinds=('collision',), out_dir=d, options={'hack': 'Test Hack', 'signature': False},
                                  log=lambda *_: None, progress=lambda done, total: seen.append((done, total)))
            self.assertTrue(res.ok)
            self.assertEqual(seen[0][0], 0)
            self.assertEqual(seen[-1][0], seen[-1][1])
            self.assertEqual(len(res.images), seen[-1][1])
            self.assertTrue(all(os.path.isfile(p) for p in res.images))
            names = sorted(os.path.basename(p) for p in res.images)
            self.assertIn('test_area.png', ' '.join(names).replace('test_area_1', 'test_area').replace('test_area_2', 'test_area') + 'test_area.png')
            with open(os.path.join(d, 'export.json')) as fh:
                man = json.load(fh)
            self.assertEqual(man['sets'][0]['hack'], 'Test Hack')
            with open(os.path.join(d, 'MapAssociations_test_hack.xml'), encoding='utf-8') as fh:
                xml = fh.read()
            self.assertIn('hack="Test Hack" kind="collision"', xml)
            self.assertIn('<MapHack name="Test Hack" prefer="collision"', xml)
            self.assertIn(r'Test Hack\test_area', xml)
            # the same area source with a different level / area gets a distinct slug, and ids are unique
            ids = [x.split('"')[1] for x in xml.split('<Map ')[1:] for x in [x[x.index('id='):]]]
            self.assertEqual(len(ids), len(set(ids)))

    def test_cancel_stops_between_images(self):
        with tempfile.TemporaryDirectory() as d:
            calls = [0]

            def cancel():
                calls[0] += 1
                return calls[0] > 3
            res = exporter.export([FakeSource(9, 1), FakeSource(10, 1)], kinds=('collision',), out_dir=d,
                                  options={'hack': 'T'}, log=lambda *_: None, cancel=cancel)
            self.assertTrue(res.cancelled)
            self.assertFalse(os.path.exists(os.path.join(d, 'MapAssociations_t.xml')))

    def test_errors(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(exporter.ExportError):
                exporter.export([FakeSource(hack='')], out_dir=d, kinds=('collision',), log=None)
            with self.assertRaises(exporter.ExportError):
                exporter.export([FakeSource(), FakeSource()], out_dir=d, kinds=('collision',), log=None)
            with self.assertRaises(exporter.ExportError):
                exporter.export([FakeSource()], out_dir=d, kinds=('bogus',), log=None)

    def test_textured_without_blender(self):
        old = render_textured.BLENDER
        old_find = render_textured.find_blender
        render_textured.BLENDER = ''
        render_textured.find_blender = lambda: ''
        try:
            with tempfile.TemporaryDirectory() as d:
                with self.assertRaises(render_textured.BlenderMissing):
                    exporter.export([FakeSource()], kinds=('textured',), out_dir=d, log=None)
                res = exporter.export([FakeSource()], kinds=('collision', 'textured'), out_dir=d, log=lambda *_: None)
                self.assertFalse(res.ok)
                self.assertTrue(any('Blender' in e for e in res.errors))
                self.assertEqual([s.kind for s in res.sets], ['collision'])      # collision still exported
                ok, why = exporter.blender_status()
                self.assertFalse(ok)
        finally:
            render_textured.BLENDER = old
            render_textured.find_blender = old_find

    def test_resume_skips_rendered_areas(self):
        old_exe, old_render = render_textured.blender_exe, render_textured.render_source
        calls = []

        def fake_render(src, slug, out, frame, splits, work, **kw):
            names = [f'{slug}_{b + 1}.png' if splits else f'{slug}.png' for b in range(len(splits) + 1)]
            os.makedirs(out, exist_ok=True)
            for n in names:
                Image.new('RGB', (2, 2)).save(os.path.join(out, n))
            calls.append(slug)
            return {'files': names}
        render_textured.blender_exe = lambda: 'blender'
        render_textured.render_source = fake_render
        try:
            with tempfile.TemporaryDirectory() as d:
                srcs = [FakeSource(), FakeSource(area=2, name='Other Area')]
                first = exporter.export(srcs, kinds=('textured',), out_dir=d, log=None)
                self.assertEqual(len(calls), 2)
                with open(first.images[-1], 'r+b') as f:        # the second area was cut off mid-write
                    f.truncate(20)
                again = exporter.export(srcs, kinds=('textured',), out_dir=d, log=None, options={'resume': True})
                self.assertEqual(len(calls), 3)                 # only the incomplete area rendered again
                self.assertEqual(calls[-1], calls[1])
                self.assertEqual(again.sets[0].entries, first.sets[0].entries)
        finally:
            render_textured.blender_exe, render_textured.render_source = old_exe, old_render

    def test_one_map_per_area_by_default_and_textured_names(self):
        old_exe, old_render = render_textured.blender_exe, render_textured.render_source

        def fake_render(src, slug, out, frame, splits, work, **kw):
            names = [f'{slug}_{b + 1}.png' if splits else f'{slug}.png' for b in range(len(splits) + 1)]
            os.makedirs(out, exist_ok=True)
            for n in names:
                Image.new('RGB', (2, 2)).save(os.path.join(out, n))
            return {'files': names}
        render_textured.blender_exe = lambda: 'blender'
        render_textured.render_source = fake_render
        try:
            with tempfile.TemporaryDirectory() as d:
                # FakeSource has floors at y 0 and 500: no bands unless asked for
                res = exporter.export([FakeSource()], kinds=('textured',), out_dir=d, log=None)
                self.assertEqual(res.sets[0].entries, 1)
                with open(res.sets[0].xml, encoding='utf-8') as fh:
                    self.assertIn('name="TH Test Area"', fh.read())          # textured alone: no "(textured)"
            with tempfile.TemporaryDirectory() as d:
                res = exporter.export([FakeSource()], out_dir=d, log=None, options={'bands': '250'})
                self.assertEqual([s.entries for s in res.sets], [2, 2])
                with open(res.sets[0].xml, encoding='utf-8') as fh:
                    self.assertIn('name="TH Test Area (collision)"', fh.read())  # both kinds: each says which
                with open(res.sets[1].xml, encoding='utf-8') as fh:
                    self.assertIn('name="TH Test Area (textured)"', fh.read())
        finally:
            render_textured.blender_exe, render_textured.render_source = old_exe, old_render


if __name__ == '__main__':
    unittest.main()
