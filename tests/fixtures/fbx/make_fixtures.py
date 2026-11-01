import bpy, bmesh, json, os, sys
from pathlib import Path
OUT = Path(sys.argv[-1]); OUT.mkdir(parents=True, exist_ok=True)
truth = {}
def reset():
    bpy.ops.wm.read_factory_settings(use_empty=True)
def tri_count(objs):
    dg = bpy.context.evaluated_depsgraph_get(); seen=set(); n=0
    for o in objs:
        if o.type != "MESH": continue
        ev = o.evaluated_get(dg); m = ev.to_mesh()
        if o.data.name in seen: ev.to_mesh_clear(); continue
        seen.add(o.data.name); m.calc_loop_triangles(); n += len(m.loop_triangles); ev.to_mesh_clear()
    return n
def export(name, **kw):
    p = OUT / f"{name}.fbx"
    bpy.ops.export_scene.fbx(filepath=str(p), use_selection=False, **kw)
    return p
def record(name, objs, **extra):
    truth[name] = {"triangles": tri_count(objs), **extra}
# 1 subdivided cube (modifier applied on export)
reset(); bpy.ops.mesh.primitive_cube_add(); o=bpy.context.active_object; m=o.modifiers.new("s","SUBSURF"); m.levels=m.render_levels=3
record("cube_subd3",[o], materials=1, uv=1, influences=0, degenerate=0); export("cube_subd3", use_mesh_modifiers=True)
# 2 ngon + quad
reset(); me=bpy.data.meshes.new("ng"); bm=bmesh.new()
v=[bm.verts.new(c) for c in [(0,0,0),(1,0,0),(1.5,1,0),(0.5,1.8,0),(-0.5,1,0),(3,0,0),(4,0,0),(4,1,0),(3,1,0)]]
bm.faces.new(v[0:5]); bm.faces.new(v[5:9]); bm.to_mesh(me); o=bpy.data.objects.new("ng",me); bpy.context.scene.collection.objects.link(o)
record("ngon_quad",[o], materials=1, uv=0, influences=0, degenerate=0); export("ngon_quad")
# 3 five materials
reset(); bpy.ops.mesh.primitive_cube_add(); o=bpy.context.active_object
for i in range(5): o.data.materials.append(bpy.data.materials.new(f"M{i}"))
for i,p in enumerate(o.data.polygons): p.material_index=i%5
record("mats5",[o], materials=5, uv=1, influences=0, degenerate=0); export("mats5")
# 4 three UV maps
reset(); bpy.ops.mesh.primitive_plane_add(); o=bpy.context.active_object
o.data.uv_layers.new(name="UV2"); o.data.uv_layers.new(name="UV3")
record("uv3",[o], materials=1, uv=3, influences=0, degenerate=0); export("uv3")
# 5/6 skinned: one vertex bound to N bones
def skinned(name, nbones):
    reset(); bpy.ops.mesh.primitive_plane_add(); o=bpy.context.active_object
    arm=bpy.data.armatures.new("A"); ao=bpy.data.objects.new("Arm",arm); bpy.context.scene.collection.objects.link(ao)
    bpy.context.view_layer.objects.active=ao; bpy.ops.object.mode_set(mode="EDIT")
    for i in range(nbones):
        b=arm.edit_bones.new(f"b{i}"); b.head=(i,0,0); b.tail=(i,0,1)
    bpy.ops.object.mode_set(mode="OBJECT")
    for i in range(nbones):
        g=o.vertex_groups.new(name=f"b{i}"); g.add([0],1.0/nbones,"REPLACE")
        g.add([1],1.0 if i==0 else 0.0,"REPLACE") if i==0 else None
    o.parent=ao; mod=o.modifiers.new("arm","ARMATURE"); mod.object=ao
    record(name,[o], materials=1, uv=1, influences=nbones, degenerate=0); export(name, add_leaf_bones=False, use_armature_deform_only=False)
skinned("skin6",6); skinned("skin4",4)
# 7 linked duplicates: 3 objects share one mesh
reset(); bpy.ops.mesh.primitive_ico_sphere_add(subdivisions=3); o=bpy.context.active_object
objs=[o]
for i in range(2):
    d=o.copy(); d.location.x=3*(i+1); bpy.context.scene.collection.objects.link(d); objs.append(d)
record("linked3",objs, materials=1, uv=1, influences=0, degenerate=0, note="3 objects share one mesh"); export("linked3")
# 8 shape key (blendshape geometry must not add triangles)
reset(); bpy.ops.mesh.primitive_cube_add(); o=bpy.context.active_object; o.shape_key_add(name="Basis"); k=o.shape_key_add(name="Smile"); k.data[0].co.x+=0.5
record("shapekey",[o], materials=1, uv=1, influences=0, degenerate=0); export("shapekey", use_mesh_modifiers=False)
# 9 degenerate face (three collinear verts) + a good triangle
reset(); me=bpy.data.meshes.new("dg"); bm=bmesh.new()
a=[bm.verts.new(c) for c in [(0,0,0),(1,0,0),(2,0,0)]]; b=[bm.verts.new(c) for c in [(0,1,0),(1,1,0),(0,2,0)]]
bm.faces.new(a); bm.faces.new(b); bm.to_mesh(me); o=bpy.data.objects.new("dg",me); bpy.context.scene.collection.objects.link(o)
record("degenerate",[o], materials=1, uv=0, influences=0, degenerate=1); export("degenerate")
# 10-12 textures
def textured(name, mode, embed):
    reset(); bpy.ops.mesh.primitive_cube_add(); o=bpy.context.active_object
    img_path=OUT/f"{name}_wood_Albedo.png"; img=bpy.data.images.new(f"{name}_wood_Albedo",64,64); img.filepath_raw=str(img_path); img.file_format="PNG"; img.pixels=[0.5]*(64*64*4); img.save()
    mat=bpy.data.materials.new("Wood"); mat.use_nodes=True; n=mat.node_tree.nodes.new("ShaderNodeTexImage"); n.image=img
    mat.node_tree.links.new(n.outputs[0], mat.node_tree.nodes["Principled BSDF"].inputs[0]); o.data.materials.append(mat)
    record(name,[o], materials=1, uv=1, influences=0, degenerate=0, texture=img_path.name)
    export(name, path_mode=mode, embed_textures=embed)
    return img_path
textured("tex_relative","RELATIVE",False)
p=textured("tex_missing","RELATIVE",False); os.remove(p); truth["tex_missing"]["texture_missing"]=True
p=textured("tex_embedded","COPY",True); os.remove(p); truth["tex_embedded"]["embedded"]=True
# 13 big
reset(); bpy.ops.mesh.primitive_grid_add(x_subdivisions=400, y_subdivisions=300); o=bpy.context.active_object
record("big_grid",[o], materials=1, uv=1, influences=0, degenerate=0); export("big_grid")
# 14 empty scene
reset(); bpy.ops.object.empty_add(); truth["empty"]={"triangles":0}; export("empty")
json.dump(truth, open(OUT/"truth.json","w"), indent=1)
print("FIXTURES", len(truth))
