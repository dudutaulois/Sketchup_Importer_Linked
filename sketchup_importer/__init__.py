__author__ = 'Martijn Berger'
__license__ = "GPL"

# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, see <http://www.gnu.org/licenses/>.

bl_info = {
    "name": "Sketchup importer",
    "author": "Martijn Berger",
    "version": (0, 0, 1, 'dev'),
    "blender": (2, 7, 2),
    "description": "import/export Sketchup skp files",
    "warning": "Very early preview",
    "wiki_url": "https://github.com/martijnberger/pyslapi",
    "tracker_url": "",
    "category": "Import-Export",
    "location": "File > Import"}

import bpy
import os
import time
from . import sketchup
import mathutils
import tempfile
from collections import OrderedDict, defaultdict
from mathutils import Matrix, Vector
from bpy.types import Operator, AddonPreferences
from bpy.props import StringProperty, IntProperty, BoolProperty
from bpy_extras.io_utils import ImportHelper, unpack_list, unpack_face_list
from extensions_framework import log


class keep_offset(defaultdict):
    def __init__(self):
        defaultdict.__init__(self, int)

    def __missing__(self, _):
        return defaultdict.__len__(self)

    def __getitem__(self, item):
        number = defaultdict.__getitem__(self, item)
        self[item] = number
        return number

class SketchupAddonPreferences(AddonPreferences):
    bl_idname = __name__

    camera_far_plane = IntProperty(name="Default Camera Distance", default=1250)
    draw_bounds = IntProperty(name="Draw object as bounds when over", default=5000)
    max_instance = IntProperty( name="Create DUPLI vert instance when count over", default=50)


    def draw(self, context):
        layout = self.layout
        layout.label(text="SKP import options:")
        layout.prop(self, "camera_far_plane")
        layout.prop(self, "draw_bounds")
        layout.prop(self, "max_instance")

def sketchupLog(*args):
    if len(args) > 0:
        log(' '.join(['%s'%a for a in args]), module_name='Sketchup')


class SceneImporter():
    def __init__(self):
        self.filepath = '/tmp/untitled.skp'
        self.name_mapping = {}
        self.component_meshes = {}

    def set_filename(self, filename):
        self.filepath = filename
        self.basepath, self.skp_filename = os.path.split(self.filepath)
        return self # allow chaining

    def load(self, context, **options):
        """load a sketchup file"""
        self.context = context
        self.reuse_material = options['reuse_material']

        sketchupLog('importing skp %r' % self.filepath)

        addon_name = __name__.split('.')[0]
        self.prefs = addon_prefs = context.user_preferences.addons[addon_name].preferences

        time_main = time.time()

        try:
            skp_model = sketchup.Model.from_file(self.filepath)
        except Exception as e:
            sketchupLog('Error reading input file: %s' % self.filepath)
            sketchupLog(e)
            return {'FINISHED'}
        self.skp_model = skp_model

        sketchupLog('parsed skp %r in %.4f sec.' % (self.filepath, (time.time() - time_main)))

        if options['import_camera']:
            active_cam = self.write_camera(skp_model.camera)
            context.scene.camera = active_cam

        t1 = time.time()
        self.write_materials(skp_model.materials)
        sketchupLog('imported materials in %.4f sec' % (time.time() - t1))

        t1 = time.time()
        self.write_entities(skp_model.Entities, "Sketchup", Matrix.Identity(4))
        sketchupLog('imported entities in %.4f sec' % (time.time() - t1))

        sketchupLog('finished importing %s in %.4f sec '% (self.filepath, time.time() - time_main))
        return {'FINISHED'}


    def write_materials(self,materials):
        if self.context.scene.render.engine != 'CYCLES':
            self.context.scene.render.engine = 'CYCLES'

        self.materials = {}
        if self.reuse_material and 'Material' in bpy.data.materials:
            self.materials['Material'] = bpy.data.materials['Material']
        else:
            bmat = bpy.data.materials.new('Material')
            bmat.diffuse_color = (.8, .8, .8)
            bmat.use_nodes = True
            self.materials['Material'] = bmat


        for mat in materials:

            name = mat.name

            if self.reuse_material and not name in bpy.data.materials:
                bmat = bpy.data.materials.new(name)
                r, g, b, a = mat.color
                tex = mat.texture
                if a < 255:
                    bmat.alpha = a / 256.0
                bmat.diffuse_color = (r / 256.0, g / 256.0, b / 256.0)
                bmat.use_nodes = True
                if tex:
                    tex_name = tex.name.split("\\")[-1]
                    tmp_name = tempfile.gettempdir() + os.pathsep + tex_name
                    #sketchupLog("Texture saved temporarily as {}".format(tmp_name))
                    tex.write(tmp_name)
                    img = bpy.data.images.load(tmp_name)
                    img.pack()
                    os.remove(tmp_name)
                    n = bmat.node_tree.nodes.new('ShaderNodeTexImage')
                    n.image = img
                    bmat.node_tree.links.new(n.outputs['Color'], bmat.node_tree.nodes['Diffuse BSDF'].inputs['Color'] )

                self.materials[name] = bmat
            else:
                self.materials[name] = bpy.data.materials[name]



    def write_mesh_data(self, fs, name, default_material='Material'):
        verts = []
        faces = []
        mat_index = []
        mats = keep_offset()
        seen = keep_offset()
        uv_list = []
        alpha = False # We assume object does not need alpha flag
        uvs_used = False # We assume no uvs need to be added


        for f in fs:
            vs, tri, uvs = f.triangles

            if f.material:
                mat_number = mats[f.material.name]
            else:
                mat_number = mats[default_material]


            mapping = {}
            for i, (v, uv) in enumerate(zip(vs, uvs)):
                l = len(seen)
                mapping[i] = seen[v]
                if len(seen) > l:
                    verts.append(v)
                uvs.append(uv)


            for face in tri:
                f0, f1, f2 = face[0], face[1], face[2]
                if f2 == 0: ## eeekadoodle dance
                    faces.append( ( mapping[f1], mapping[f2], mapping[f0] ) )
                    uv_list.append(( uvs[f2][0], uvs[f2][1],
                                     uvs[f1][0], uvs[f1][1],
                                     uvs[f0][0], uvs[f0][1],
                                     0, 0 ) )
                else:
                    faces.append( ( mapping[f0], mapping[f1], mapping[f2] ) )
                    uv_list.append(( uvs[f0][0], uvs[f0][1],
                                     uvs[f1][0], uvs[f1][1],
                                     uvs[f2][0], uvs[f2][1],
                                     0, 0 ) )
                mat_index.append(mat_number)


        if len(verts) == 0:
            return None, False

        me = bpy.data.meshes.new(name)

        me.vertices.add(len(verts))
        me.tessfaces.add(len(faces))

        if len(mats) >= 1:
            mats_sorted = OrderedDict(sorted(mats.items(), key=lambda x: x[1]))
            for k in mats_sorted.keys():
                bmat = self.materials[k]
                me.materials.append(bmat)
                if bmat.alpha < 1.0:
                    alpha = True
                if 'Image Texture' in bmat.node_tree.nodes.keys():
                    uvs_used = True
        else:
            sketchupLog("WARNING OBJECT {} HAS NO MATERIAL".format(name))

        me.vertices.foreach_set("co", unpack_list(verts))
        me.tessfaces.foreach_set("vertices_raw", unpack_face_list(faces))
        me.tessfaces.foreach_set("material_index", mat_index)

        if uvs_used:
            me.tessface_uv_textures.new()
            for i in range(len(faces)):
                me.tessface_uv_textures[0].data[i].uv_raw = uv_list[i]

        me.update(calc_edges=True)
        me.validate()
        return me, alpha

    def write_entities(self, entities, name, parent_tranform, default_material="Material", type=None):
        #sketchupLog("Creating object -> {} with default mat {}".format(name, default_material))
        if type=="Component":
            if (name,default_material) in self.component_meshes:
                me, alpha = self.component_meshes[(name,default_material)]
            else:
                me, alpha = self.write_mesh_data(entities.faces, name, default_material=default_material)
                self.component_meshes[(name,default_material)] = (me, alpha)
        else:
            me, alpha = self.write_mesh_data(entities.faces, name, default_material=default_material)

        if me:
            ob = bpy.data.objects.new(name, me)
            ob.matrix_world = parent_tranform
            if alpha:
                ob.show_transparent = True
            me.update(calc_edges=True)
            self.context.scene.objects.link(ob)

        for group in entities.groups:
            trans = Matrix(group.transform)
            mat = group.material
            mat_name = mat.name if mat else default_material
            if mat_name == "Material" and default_material != "Material":
                mat_name = default_material
            self.write_entities(group.entities, "G-" + group.name, parent_tranform * trans, default_material=mat_name)

        for instance in entities.instances:
            trans = Matrix(instance.transform)
            mat = instance.material
            mat_name = mat.name if mat else default_material
            if mat_name == "Material" and default_material != "Material":
                mat_name = default_material
            self.write_entities(instance.definition.entities, instance.definition.name ,parent_tranform * trans, default_material=mat_name, type="Component")

        return

    def write_camera(self, camera):
        pos, target, up = camera.GetOrientation()
        bpy.ops.object.add(type='CAMERA', location=pos)
        ob = self.context.object

        z = (mathutils.Vector(pos) - mathutils.Vector(target)).normalized()
        x = z.cross(mathutils.Vector(up)).normalized()
        y = z.cross(x)

        ob.matrix_world.col[0] = x.resized(4)
        ob.matrix_world.col[1] = y.resized(4)
        ob.matrix_world.col[2] = z.resized(4)

        cam = ob.data
        cam.lens = camera.fov
        cam.clip_end = self.prefs.camera_far_plane
        cam.name = "Active Camera"



class ImportSKP(bpy.types.Operator, ImportHelper):
    """load a Trimble Sketchup SKP file"""
    bl_idname = "import_scene.skp"
    bl_label = "Import SKP"
    bl_options = {'PRESET', 'UNDO'}

    filename_ext = ".skp"

    filter_glob = StringProperty(
        default="*.SKP",
        options={'HIDDEN'},
    )

    import_camera = BoolProperty(name="Cameras", description="Import camera's", default=True)
    reuse_material = BoolProperty(name="Use Existing Materials", description="Reuse scene materials", default=True)


    def execute(self, context):
        keywords = self.as_keywords(ignore=("axis_forward",
                                            "axis_up",
                                            "filter_glob",
                                            "split_mode"))
        return SceneImporter().set_filename(keywords['filepath']).load(context, **keywords)

    def draw(self, context):
        layout = self.layout

        row = layout.row(align=True)
        row.prop(self, "import_camera")
        row.prop(self, "reuse_material")


def menu_func_import(self, context):
    self.layout.operator(ImportSKP.bl_idname, text="Import Sketchup Scene(.skp)")


# Registration
def register():
    bpy.utils.register_class(SketchupAddonPreferences)
    bpy.utils.register_class(ImportSKP)
    bpy.types.INFO_MT_file_import.append(menu_func_import)


def unregister():
    bpy.utils.unregister_class(ImportSKP)
    bpy.types.INFO_MT_file_import.remove(menu_func_import)
    bpy.utils.unregister_class(SketchupAddonPreferences)