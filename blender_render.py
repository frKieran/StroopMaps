"""Run inside Blender: blender -b -P blender_render.py -- JOB.json

Builds one mesh per batch from the job's npz (positions in SM64 units, per-corner colours and UVs), gives each an
emission material (texture x vertex colour x constant, in the N64's gamma space: textures Non-Color, view transform
Raw, so the product is the combiner's), and renders an orthographic top-down camera per band. SM64 (x, y, z) maps to
Blender (x, -z, y): north (-Z) is the image's top. Each band's top clips with the camera's clip_start.
mode 'height' writes the height of the topmost surface per pixel centre (float, NaN where empty) to an .npy instead.
"""
import json, sys
import bpy
import numpy as np

job = json.load(open(sys.argv[sys.argv.index('--') + 1]))
data = np.load(job['npz'])
mode = job.get('mode', 'color')

bpy.ops.wm.read_factory_settings(use_empty=True)
sc = bpy.context.scene
sc.render.engine = 'CYCLES'
cy = sc.cycles
cy.device = 'CPU'
cy.samples = job.get('samples', 16) if mode not in ('height', 'normal') else 1
cy.use_denoising = False
cy.use_adaptive_sampling = False
cy.max_bounces = 0
cy.transparent_max_bounces = 64
cy.diffuse_bounces = cy.glossy_bounces = cy.transmission_bounces = cy.volume_bounces = 0
cy.pixel_filter_type = 'BOX'
cy.filter_width = 1.0 if mode not in ('height', 'normal') else 0.01
sc.render.film_transparent = True
sc.view_settings.view_transform = 'Raw'
sc.view_settings.look = 'None'
sc.display_settings.display_device = 'sRGB'
sc.sequencer_colorspace_settings.name = 'Non-Color'
sc.render.threads_mode = 'FIXED'
sc.render.threads = job.get('threads', 8)
sc.render.use_persistent_data = True

fr = job['frame']        # x1, x2, z1, z2, w, h, scale
x1, x2, z1, z2, W, H = fr['x1'], fr['x2'], fr['z1'], fr['z2'], fr['w'], fr['h']
sc.render.resolution_x, sc.render.resolution_y = W, H
sc.render.resolution_percentage = 100
sc.render.pixel_aspect_x = sc.render.pixel_aspect_y = 1

images = {}


def material(i, m):
    """A Blender material for scene material `m`: its texture, combiner colour and alpha."""
    mat = bpy.data.materials.new(f'm{i}')
    mat.use_nodes = True
    nt = mat.node_tree
    for n in list(nt.nodes):
        nt.nodes.remove(n)
    out = nt.nodes.new('ShaderNodeOutputMaterial')
    if mode == 'normal':
        geo = nt.nodes.new('ShaderNodeNewGeometry')
        em = nt.nodes.new('ShaderNodeEmission')
        nt.links.new(geo.outputs['True Normal'], em.inputs['Color'])
        alpha = None
    elif mode == 'height':
        geo = nt.nodes.new('ShaderNodeNewGeometry')
        sep = nt.nodes.new('ShaderNodeSeparateXYZ')
        nt.links.new(geo.outputs['Position'], sep.inputs[0])
        em = nt.nodes.new('ShaderNodeEmission')
        nt.links.new(sep.outputs['Z'], em.inputs['Color'])
        alpha = None
    else:
        col = nt.nodes.new('ShaderNodeVertexColor')
        col.layer_name = 'Col'
        rgb, alpha = col.outputs['Color'], col.outputs['Alpha']
        if not m['shade']:
            rgb, alpha = None, None
        if m.get('const'):
            c = m['const']
            k = nt.nodes.new('ShaderNodeRGB')
            k.outputs[0].default_value = (c[0] / 255, c[1] / 255, c[2] / 255, 1)
            if rgb is None:
                rgb = k.outputs[0]
            else:
                mul = nt.nodes.new('ShaderNodeMix')
                mul.data_type, mul.blend_type = 'RGBA', 'MULTIPLY'
                mul.inputs['Factor'].default_value = 1
                nt.links.new(rgb, mul.inputs[6])
                nt.links.new(k.outputs[0], mul.inputs[7])
                rgb = mul.outputs[2]
        if m.get('tex') and job.get('textures', True):
            p = m['tex']
            if p not in images:
                im = bpy.data.images.load(p)
                im.colorspace_settings.name = 'Non-Color'
                im.alpha_mode = 'STRAIGHT'
                images[p] = im
            tn = nt.nodes.new('ShaderNodeTexImage')
            tn.image = images[p]
            tn.interpolation = 'Linear'
            tn.extension = 'EXTEND' if m.get('clamp') else 'REPEAT'
            uv = nt.nodes.new('ShaderNodeUVMap')
            uv.uv_map = 'UV'
            nt.links.new(uv.outputs[0], tn.inputs[0])
            if rgb is None:
                rgb, alpha = tn.outputs['Color'], tn.outputs['Alpha']
            else:
                mul = nt.nodes.new('ShaderNodeMix')
                mul.data_type, mul.blend_type = 'RGBA', 'MULTIPLY'
                mul.inputs['Factor'].default_value = 1
                nt.links.new(rgb, mul.inputs[6])
                nt.links.new(tn.outputs['Color'], mul.inputs[7])
                rgb = mul.outputs[2]
                am = nt.nodes.new('ShaderNodeMath')
                am.operation = 'MULTIPLY'
                nt.links.new(alpha, am.inputs[0])
                nt.links.new(tn.outputs['Alpha'], am.inputs[1])
                alpha = am.outputs[0]
        if rgb is None:
            k = nt.nodes.new('ShaderNodeRGB')
            k.outputs[0].default_value = (0.7, 0.7, 0.7, 1)
            rgb = k.outputs[0]
        em = nt.nodes.new('ShaderNodeEmission')
        nt.links.new(rgb, em.inputs['Color'])
    layer = m['layer']
    if layer in (0, 1, 2, 3) or alpha is None:           # opaque layers ignore alpha
        nt.links.new(em.outputs[0], out.inputs[0])
        return mat
    if layer == 4:                                       # alpha test (cutout)
        gt = nt.nodes.new('ShaderNodeMath')
        gt.operation = 'GREATER_THAN'
        gt.inputs[1].default_value = 0.5
        nt.links.new(alpha, gt.inputs[0])
        alpha = gt.outputs[0]
    tr = nt.nodes.new('ShaderNodeBsdfTransparent')
    mix = nt.nodes.new('ShaderNodeMixShader')
    nt.links.new(alpha, mix.inputs[0])
    nt.links.new(tr.outputs[0], mix.inputs[1])
    nt.links.new(em.outputs[0], mix.inputs[2])
    nt.links.new(mix.outputs[0], out.inputs[0])
    return mat


for i, m in enumerate(job['materials']):
    pos = data[f'p{i}'].astype(np.float32)               # (n*3, 3) SM64
    n = len(pos) // 3
    if not n:
        continue
    bp = np.stack([pos[:, 0], -pos[:, 2], pos[:, 1]], 1)
    me = bpy.data.meshes.new(f'b{i}')
    me.vertices.add(3 * n)
    me.vertices.foreach_set('co', bp.reshape(-1))
    me.loops.add(3 * n)
    me.loops.foreach_set('vertex_index', np.arange(3 * n, dtype=np.int32))
    me.polygons.add(n)
    me.polygons.foreach_set('loop_start', np.arange(0, 3 * n, 3, dtype=np.int32))
    me.polygons.foreach_set('loop_total', np.full(n, 3, dtype=np.int32))
    me.update(calc_edges=True)
    if mode not in ('height', 'normal'):
        ca = me.color_attributes.new('Col', 'FLOAT_COLOR', 'CORNER')
        ca.data.foreach_set('color', (data[f'c{i}'].astype(np.float32) / 255.0).reshape(-1))
        uvl = me.uv_layers.new(name='UV')
        uvl.data.foreach_set('uv', data[f'u{i}'].astype(np.float32).reshape(-1))
    me.materials.append(material(i, m))
    ob = bpy.data.objects.new(f'b{i}', me)
    sc.collection.objects.link(ob)

top = job['ymax'] + 200.0
cam = bpy.data.cameras.new('cam')
cam.type = 'ORTHO'
cam.ortho_scale = max(W, H) * fr['scale']
cam.sensor_fit = 'AUTO'
co = bpy.data.objects.new('cam', cam)
co.location = ((x1 + x2) / 2, -(z1 + z2) / 2, top)
co.rotation_euler = (0, 0, 0)
sc.collection.objects.link(co)
sc.camera = co

for band in job['bands']:
    cam.clip_start = max(top - band['top'], 0.001) if band.get('top') is not None else 1.0
    cam.clip_end = top - job['ymin'] + 200.0
    if mode == 'normal':
        sc.render.image_settings.file_format = 'OPEN_EXR'
        sc.render.image_settings.color_depth = '32'
        sc.render.filepath = band['out'] + '.exr'
        bpy.ops.render.render(write_still=True)
        im = bpy.data.images.load(band['out'] + '.exr')
        px = np.array(im.pixels[:], dtype=np.float32).reshape(H, W, 4)[::-1]
        nb = np.where(px[:, :, 3:4] > 0.5, px[:, :, :3], np.nan)
        np.save(band['out'] + '.npy', np.stack([nb[:, :, 0], nb[:, :, 2], -nb[:, :, 1]], 2))   # SM64 axes
    elif mode == 'height':
        sc.render.image_settings.file_format = 'OPEN_EXR'
        sc.render.image_settings.color_depth = '32'
        sc.render.filepath = band['out'] + '.exr'
        bpy.ops.render.render(write_still=True)
        im = bpy.data.images.load(band['out'] + '.exr')
        px = np.array(im.pixels[:], dtype=np.float32).reshape(H, W, 4)[::-1]
        hgt = np.where(px[:, :, 3] > 0.5, px[:, :, 0], np.nan)
        np.save(band['out'] + '.npy', hgt)
    else:
        sc.render.image_settings.file_format = 'PNG'
        sc.render.image_settings.color_mode = 'RGBA'
        sc.render.image_settings.color_depth = '8'
        sc.render.image_settings.compression = 90
        sc.render.filepath = band['out']
        bpy.ops.render.render(write_still=True)
    print('BAND DONE', band['out'], flush=True)
