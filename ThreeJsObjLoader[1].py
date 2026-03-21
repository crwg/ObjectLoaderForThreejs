# SPDX-License-Identifier: GPL-2.0-or-later
# io_mesh_threejs_objectloader.py
"""
Blender ↔ three.js ObjectLoader (Format 4+) Import/Export Add-on

Supports:
- Export selected/visible meshes to three.js ObjectLoader JSON format 4.3+
- Import ObjectLoader JSON files into Blender
- Legacy format compatibility: different key order, alternative root keys
  (object/scene/root), geometries/materials as array or dict, index-based refs
- Precision control for float data (decimal places)
- BufferGeometry with indexed attributes (position, normal, uv, uv2, color)
- Core materials: MeshBasicMaterial, MeshLambertMaterial, MeshPhongMaterial
- Scene hierarchy preservation (on import)
"""

bl_info = {
    "name": "three.js ObjectLoader",
    "author": "Aleksei Vlasov crwde",
    "version": (1, 0, 0),
    "blender": (4, 0, 0),
    "location": "File > Import-Export",
    "description": "Import/Export three.js ObjectLoader format 4+ (JSON)",
    "warning": "",
    "doc_url": "https://github.com/mrdoob/three.js/wiki/JSON-Object-Scene-format-4",
    "category": "Import-Export",
}

import bpy
import json
import math
import os
import re
import traceback
import uuid
from pathlib import Path
from bpy_extras.io_utils import ImportHelper, ExportHelper
from bpy.props import StringProperty, BoolProperty, IntProperty, CollectionProperty, EnumProperty
from bpy.types import Context, Operator, Panel, OperatorFileListElement
from mathutils import Matrix, Vector
from typing import Any, Optional


# =============================================================================
# CONSTANTS
# =============================================================================

DEFAULT_ASPECT_RATIO = 16 / 9
DEFAULT_COLOR_WHITE = 0xFFFFFF
MATRIX_ELEMENT_COUNT = 16

# ObjectLoader format compatibility: alternative root keys (older exporters)
ROOT_OBJECT_KEYS = ("object", "scene", "root")
GEOMETRIES_KEYS = ("geometries",)
MATERIALS_KEYS = ("materials",)

# =============================================================================
# UTILS
# =============================================================================

def _get_first_key(data: dict, keys: tuple[str, ...]) -> Any:
    """Get value by first existing key (format compatibility: different key order/names)."""
    for key in keys:
        if key in data:
            return data[key]
    return None


def _normalize_to_list(value: Any) -> list:
    """Normalize geometries/materials to list. Older format may use dict keyed by uuid."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return list(value.values())
    return []

def round_floats(obj: Any, precision: int) -> Any:
    """Recursively round float values to specified precision."""
    if isinstance(obj, float):
        return round(obj, precision)
    elif isinstance(obj, dict):
        return {k: round_floats(v, precision) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [round_floats(item, precision) for item in obj]
    return obj


def color_to_hex(color: tuple[float, float, float]) -> int:
    """Convert Blender color (0-1 RGB) to three.js hex integer."""
    r, g, b = [int(c * 255) for c in color]
    return (r << 16) | (g << 8) | b


def hex_to_color(hex_val: int) -> tuple[float, float, float]:
    """Convert three.js hex integer to Blender color (0-1 RGB)."""
    r = ((hex_val >> 16) & 0xFF) / 255.0
    g = ((hex_val >> 8) & 0xFF) / 255.0
    b = (hex_val & 0xFF) / 255.0
    return (r, g, b)


def matrix_to_list(mat: Matrix) -> list[float]:
    """Convert mathutils.Matrix to flat 16-element list (column-major for three.js)."""
    # three.js uses column-major order, Blender uses row-major
    # Transpose for compatibility
    transposed = mat.transposed()
    return [round(val, 6) for row in transposed for val in row]


def list_to_matrix(lst: list[float]) -> Matrix:
    """Convert flat 16-element list to mathutils.Matrix (column-major from three.js)."""
    if len(lst) < 16:
        raise ValueError(f"Expected 16 elements for matrix, got {len(lst)}")
    # three.js column-major → Blender row-major (transpose)
    mat = Matrix((
        lst[0:4],
        lst[4:8],
        lst[8:12],
        lst[12:16]
    ))
    return mat.transposed()


def generate_uuid() -> str:
    """Generate uppercase UUID string for three.js compatibility."""
    return str(uuid.uuid4()).upper()


def _world_bbox(objects: list) -> tuple[Vector, Vector]:
    """Return (min, max) world-space bounding box for a list of objects."""
    min_v = Vector((float("inf"), float("inf"), float("inf")))
    max_v = Vector((float("-inf"), float("-inf"), float("-inf")))
    for obj in objects:
        for i in range(8):
            corner = obj.matrix_world @ Vector(obj.bound_box[i])
            min_v.x = min(min_v.x, corner.x)
            min_v.y = min(min_v.y, corner.y)
            min_v.z = min(min_v.z, corner.z)
            max_v.x = max(max_v.x, corner.x)
            max_v.y = max(max_v.y, corner.y)
            max_v.z = max(max_v.z, corner.z)
    return min_v, max_v


def arrange_objects_along_x(groups: list[list], gap_scale: float = 1.0) -> None:
    """Place each group of objects along X so they don't overlap. Gap = gap_scale * average(size_x)."""
    if not groups:
        return
    bboxes = []
    for objs in groups:
        if not objs:
            bboxes.append((0.0, 0.0, []))
            continue
        min_v, max_v = _world_bbox(objs)
        size_x = max_v.x - min_v.x
        roots = [o for o in objs if o.parent is None or o.parent not in objs]
        bboxes.append((min_v.x, size_x, roots))
    valid = [(mn, sz, roots) for mn, sz, roots in bboxes if roots and sz >= 0]
    if not valid:
        return
    avg_size = sum(sz for _, sz, _ in valid) / len(valid)
    gap = avg_size * gap_scale
    current_x = valid[0][0]
    for min_x, size_x, roots in valid:
        delta = current_x - min_x
        for obj in roots:
            obj.location.x += delta
        current_x += size_x + gap


# =============================================================================
# EXPORTER
# =============================================================================

class ThreeJSObjectExporter:
    """Exports Blender objects to three.js ObjectLoader format 4+."""
    
    def __init__(
        self,
        precision: int,
        split_by_material: bool,
        selected_only: bool,
        export_mesh: bool = True,
        export_lights: bool = True,
        export_cameras: bool = True,
        export_normals: bool = True,
        export_uvs: bool = True,
        export_vertex_colors: bool = True,
        format_version: str = "4",
    ):
        self.precision = precision
        self.split_by_material = split_by_material
        self.selected_only = selected_only
        self.export_mesh = export_mesh
        self.export_lights = export_lights
        self.export_cameras = export_cameras
        self.export_normals = export_normals
        self.export_uvs = export_uvs
        self.export_vertex_colors = export_vertex_colors
        self.format_version = format_version  # "3" = legacy Geometry, "4" = ObjectLoader 4+
        self.geometries: dict[str, dict] = {}
        self.materials: dict[str, dict] = {}
        
    def _collect_referenced_uuids(self, node: dict, geom_uuids: set, mat_uuids: set) -> None:
        """Recursively collect geometry and material UUIDs referenced in the scene tree."""
        g = node.get("geometry")
        if g is not None:
            if isinstance(g, list):
                geom_uuids.update(g)
            else:
                geom_uuids.add(g)
        m = node.get("material")
        if m is not None:
            if isinstance(m, list):
                mat_uuids.update(m)
            else:
                mat_uuids.add(m)
        for ch in node.get("children", ()):
            self._collect_referenced_uuids(ch, geom_uuids, mat_uuids)

    def export(self, context: Context, objects_override: Optional[list] = None) -> dict:
        """Main export entry point. If objects_override is set, export only those objects."""
        if self.format_version == "3":
            return self._export_format3(context, objects_override=objects_override)
        scene_data = self._export_scene(context, objects_override=objects_override)
        geom_refs, mat_refs = set(), set()
        self._collect_referenced_uuids(scene_data, geom_refs, mat_refs)
        geometries = [g for g in self.geometries.values() if g["uuid"] in geom_refs]
        materials = [m for m in self.materials.values() if m["uuid"] in mat_refs]
        output = {
            "metadata": {"version": 4.5, "type": "Object", "generator": "BlenderThreeJSExporter"},
            "geometries": geometries,
            "materials": materials,
            "object": scene_data,
        }
        return round_floats(output, self.precision)

    def _export_format3(self, context: Context, objects_override: Optional[list] = None) -> dict:
        """Export as legacy three.js Geometry format (v3: vertices, normals, faces at root). One mesh per export."""
        export_types = {"MESH"} if self.export_mesh else set()
        if objects_override is not None:
            objects = [o for o in objects_override if getattr(o, "type", None) == "MESH"]
        else:
            objects = [
                o for o in context.scene.objects
                if getattr(o, "type", None) == "MESH"
                and (not self.selected_only or o.select_get())
                and o.visible_get()
            ]
        if not objects:
            return {"metadata": {"version": 3, "type": "Geometry", "generator": "BlenderThreeJSExporter"}, "vertices": [], "normals": [], "faces": []}
        mesh = getattr(objects[0], "data", None)
        name = objects[0].name
        if not mesh or not hasattr(mesh, "polygons"):
            return {"metadata": {"version": 3, "type": "Geometry", "generator": "BlenderThreeJSExporter"}, "vertices": [], "normals": [], "faces": []}
        return self._export_legacy_geometry(mesh, name, self.precision)

    def _export_legacy_geometry(self, mesh: bpy.types.Mesh, name: str, precision: int) -> dict:
        """Build legacy Format 3 geometry dict (vertices, normals, faces)."""
        if hasattr(mesh, "calc_normals_split"):
            mesh.calc_normals_split()
        vertices_flat = []
        for v in mesh.vertices:
            vertices_flat.extend(round(v.co[i], precision) for i in range(3))
        normals_flat = []
        for loop in mesh.loops:
            normals_flat.extend(round(loop.normal[i], precision) for i in range(3))
        faces_flat = []
        for poly in mesh.polygons:
            verts = poly.vertices[:]
            loops = list(poly.loop_indices)
            for i in range(1, len(verts) - 1):
                faces_flat.append(32)
                faces_flat.extend([verts[0], verts[i], verts[i + 1]])
                faces_flat.extend([loops[0], loops[i], loops[i + 1]])
        return round_floats({
            "metadata": {
                "version": 3,
                "type": "Geometry",
                "generator": "BlenderThreeJSExporter",
                "vertices": len(mesh.vertices),
                "faces": len(faces_flat) // 7,
                "normals": len(mesh.loops),
            },
            "name": name,
            "vertices": vertices_flat,
            "normals": normals_flat,
            "faces": faces_flat,
        }, precision)

    def _export_scene(self, context: Context, objects_override: Optional[list] = None) -> dict:
        """Export scene root object with children. If objects_override given, use that list only."""
        export_types = set()
        if self.export_mesh:
            export_types.add("MESH")
        if self.export_lights:
            export_types.add("LIGHT")
        if self.export_cameras:
            export_types.add("CAMERA")

        if objects_override is not None:
            objects = [o for o in objects_override if getattr(o, "type", None) in export_types]
        else:
            objects = []
            for obj in context.scene.objects:
                if not hasattr(obj, "type") or getattr(obj, "type", None) not in export_types:
                    continue
                if self.selected_only and not obj.select_get():
                    continue
                if not obj.visible_get():
                    continue
                objects.append(obj)

        children = [o for o in (self._export_object(obj) for obj in objects) if o is not None]
        # Keep Scene node minimal: omit identity matrix (importer handles missing matrix).
        return {"uuid": generate_uuid(), "type": "Scene", "children": children}
    
    def _export_object(self, obj: bpy.types.Object) -> Optional[dict]:
        """Export a single Blender object to three.js object dict. Returns None for unsupported types."""
        bl_type = getattr(obj, "type", None)
        three_type = self._get_threejs_type(obj)
        if three_type == "Object3D":
            return None
        if bl_type == "MESH":
            mesh = getattr(obj, "data", None)
            if not mesh or not hasattr(mesh, "polygons") or len(mesh.polygons) == 0:
                return None
        base = {"uuid": generate_uuid(), "name": obj.name, "type": three_type}
        mat_list = matrix_to_list(obj.matrix_world)
        if mat_list != matrix_to_list(Matrix.Identity(4)):
            base["matrix"] = mat_list
        if obj.hide_get():
            base["visible"] = False
        if bl_type == "MESH":
            mesh_data = self._export_mesh(obj)
            if not mesh_data:
                return None
            base.update(mesh_data)
        elif bl_type == "LIGHT":
            base.update(self._export_light(obj))
        elif bl_type == "CAMERA":
            base.update(self._export_camera(obj))
        return base
    
    def _get_threejs_type(self, obj: bpy.types.Object) -> str:
        """Map Blender object type to three.js type. Uses getattr so Mesh (no .type) is safe."""
        obj_type = getattr(obj, "type", None)
        if obj_type == "MESH":
            return "Mesh"
        if obj_type == "LIGHT":
            data_type = getattr(obj.data, "type", "POINT") if obj.data else "POINT"
            return {"POINT": "PointLight", "SUN": "DirectionalLight", "SPOT": "SpotLight", "AREA": "PointLight"}.get(data_type, "PointLight")
        if obj_type == "CAMERA":
            data_type = getattr(obj.data, "type", "PERSP") if obj.data else "PERSP"
            return "PerspectiveCamera" if data_type == "PERSP" else "OrthographicCamera"
        return "Object3D"
    
    def _export_mesh(self, obj: bpy.types.Object) -> dict:
        """Export mesh geometry and material references. Only exports data that exists (no empty geometry/materials)."""
        mesh = getattr(obj, "data", None)
        if not mesh or not hasattr(mesh, "polygons"):
            return {}
        result = {}
        used_material_indices = {p.material_index for p in mesh.polygons}

        # Handle geometry
        if self.split_by_material:
            geom_uuids = []
            for slot_idx, slot in enumerate(obj.material_slots):
                if not slot.material or slot_idx not in used_material_indices:
                    continue
                geom_key = f"{mesh.name_full}_{slot_idx}"
                if geom_key not in self.geometries:
                    geom = self._export_buffer_geometry(mesh, slot_idx, self.precision)
                    if geom is None:
                        continue
                    self.geometries[geom_key] = geom
                geom_uuids.append(self.geometries[geom_key]["uuid"])
            if not geom_uuids:
                return {}
            if len(geom_uuids) == 1:
                result["geometry"] = geom_uuids[0]
            else:
                result["geometry"] = geom_uuids
        else:
            geom_key = mesh.name_full
            if geom_key not in self.geometries:
                geom = self._export_buffer_geometry(mesh, None, self.precision)
                if geom is None:
                    return {}
                self.geometries[geom_key] = geom
            result["geometry"] = self.geometries[geom_key]["uuid"]

        # Only export materials that are actually used by at least one polygon
        material_uuids = []
        for slot_idx, slot in enumerate(obj.material_slots):
            if not slot.material or slot_idx not in used_material_indices:
                continue
            mat_key = slot.material.name_full
            if mat_key not in self.materials:
                self.materials[mat_key] = self._export_material(slot.material)
            material_uuids.append(self.materials[mat_key]["uuid"])
        if material_uuids:
            result["material"] = material_uuids if len(material_uuids) > 1 else material_uuids[0]

        return result
    
    def _export_buffer_geometry(self, mesh: bpy.types.Mesh, material_index: Optional[int], precision: int) -> Optional[dict]:
        """Export mesh as THREE.BufferGeometry."""
        if hasattr(mesh, "calc_normals_split"):
            mesh.calc_normals_split()  # Blender 4.x; removed in 5.0 (corner_normals used instead)
        # Collect vertices per face for selected material
        positions, indices = [], []
        normals = [] if self.export_normals else None
        uvs = [] if self.export_uvs else None
        colors = [] if self.export_vertex_colors else None
        vertex_map = {}  # Deduplicate vertices
        idx_counter = 0

        # Active attribute checks (avoid filling large arrays when exporter is configured not to)
        uv_active = bool(mesh.uv_layers.active) if self.export_uvs else False
        color_active = bool(mesh.color_attributes.active) if self.export_vertex_colors else False
        
        # Triangulate so indices are always groups of 3.
        mesh.calc_loop_triangles()
        for loop_tri in mesh.loop_triangles:
            if material_index is not None and loop_tri.material_index != material_index:
                continue

            for loop_idx in loop_tri.loops:
                loop = mesh.loops[loop_idx]
                vert = mesh.vertices[loop.vertex_index]

                # Deduplicate by exported attribute values (rounded).
                # This avoids vertex explosion when multiple corners share the same position/UV/normal/color.
                pos_key = (
                    round(vert.co.x, precision),
                    round(vert.co.y, precision),
                    round(vert.co.z, precision),
                )
                key_parts: list[Any] = [pos_key]

                if normals is not None:
                    key_parts.append((
                        round(loop.normal.x, precision),
                        round(loop.normal.y, precision),
                        round(loop.normal.z, precision),
                    ))

                uv_key = None
                uv_val = None
                if uvs is not None and uv_active:
                    uv = mesh.uv_layers.active.data[loop_idx].uv
                    uv_u = round(uv.x, precision)
                    uv_v = round(1.0 - uv.y, precision)
                    uv_key = (uv_u, uv_v)
                    uv_val = [uv.x, 1.0 - uv.y]
                    key_parts.append(uv_key)

                color_key = None
                color_val = None
                if colors is not None and color_active:
                    attr = mesh.color_attributes.active
                    color_idx = loop.vertex_index if getattr(attr, "domain", "POINT") == "POINT" else loop_idx
                    if color_idx < len(attr.data):
                        val = attr.data[color_idx]
                        col = getattr(val, "color", None) or getattr(val, "vector", (1.0, 1.0, 1.0))
                        rgb = col[:3]
                    else:
                        rgb = (1.0, 1.0, 1.0)
                    color_val = list(rgb)
                    color_key = (
                        round(color_val[0], precision),
                        round(color_val[1], precision),
                        round(color_val[2], precision),
                    )
                    key_parts.append(color_key)

                vkey = tuple(key_parts)

                if vkey not in vertex_map:
                    vertex_map[vkey] = idx_counter
                    positions.extend(vert.co)
                    if normals is not None:
                        normals.extend(loop.normal)
                    if uvs is not None and uv_active and uv_val is not None:
                        uvs.extend(uv_val)
                    if colors is not None and color_active and color_val is not None:
                        colors.extend(color_val)
                    idx_counter += 1

                indices.append(vertex_map[vkey])

        if not positions:
            return None

        # Build geometry dict (only fields we use; omit normalized/boundingSphere to save space)
        attributes: dict[str, Any] = {
            "position": {
                "itemSize": 3,
                "type": "Float32Array",
                "array": [round(v, precision) for v in positions],
            }
        }
        if normals is not None:
            attributes["normal"] = {
                "itemSize": 3,
                "type": "Float32Array",
                "array": [round(v, precision) for v in normals],
            }
        if uvs is not None and uv_active:
            attributes["uv"] = {
                "itemSize": 2,
                "type": "Float32Array",
                "array": [round(v, precision) for v in uvs],
            }

        geom = {
            "uuid": generate_uuid(),
            "type": "BufferGeometry",
            "data": {
                "attributes": {
                    **attributes,
                }
            },
        }
        # Always export index when we build indexed triangles.
        if indices:
            geom["data"]["index"] = {
                "type": "Uint16Array" if max(indices) < 65535 else "Uint32Array",
                "array": indices,
            }
        if colors is not None and color_active and any(c != 1.0 for c in colors):
            geom["data"]["attributes"]["color"] = {
                "itemSize": 3,
                "type": "Float32Array",
                "array": [round(v, precision) for v in colors],
            }
        return geom
    
    def _export_material(self, mat: bpy.types.Material) -> dict:
        """Export Blender material to three.js material."""
        # Determine material type
        if mat.blend_method == 'BLEND' or mat.use_nodes:
            # Simplified: default to Phong for PBR-ish
            mat_type = "MeshPhongMaterial"
        elif not mat.use_nodes and mat.specular_intensity == 0:
            mat_type = "MeshLambertMaterial"
        else:
            mat_type = "MeshPhongMaterial"
        
        result = {"uuid": generate_uuid(), "type": mat_type, "color": color_to_hex(mat.diffuse_color[:3])}
        if hasattr(mat, "emission_color"):
            emissive = color_to_hex(mat.emission_color[:3])
            if emissive != 0:
                result["emissive"] = emissive
        if mat.specular_intensity > 0:
            result["specular"] = color_to_hex((0.5, 0.5, 0.5))
        shininess = mat.roughness * 100 if hasattr(mat, "roughness") else 30
        if shininess != 30:
            result["shininess"] = shininess
        if mat.alpha != 1.0:
            result["opacity"] = mat.alpha
        if mat.blend_method != "OPAQUE":
            result["transparent"] = True
        if mat.show_wire:
            result["wireframe"] = True
        if mat.use_backface_culling:
            result["side"] = 2
        return result
    
    def _export_light(self, obj: bpy.types.Object) -> dict:
        """Export Blender light to three.js light."""
        light = obj.data
        if not light:
            return {}
        result = {"color": color_to_hex(light.color), "intensity": light.energy}
        light_type = getattr(light, "type", "POINT")
        if light_type == "SPOT":
            result["angle"] = light.spot_size
            result["penumbra"] = light.spot_blend
        elif light_type == "AREA":
            result["distance"] = getattr(light, "shadow_soft_size", 0)
        return result

    def _export_camera(self, obj: bpy.types.Object) -> dict:
        """Export Blender camera to three.js camera."""
        cam = obj.data
        if not cam:
            return {}
        result = {
            "fov": cam.angle_y * 180 / math.pi,
            "aspect": DEFAULT_ASPECT_RATIO,
            "near": cam.clip_start,
            "far": cam.clip_end,
        }
        if getattr(cam, "type", "PERSP") == "ORTHO":
            result["zoom"] = 1.0  # Orthographic zoom factor
        return result


# =============================================================================
# IMPORTER
# =============================================================================

class ThreeJSObjectImporter:
    """Imports three.js ObjectLoader format 4+ into Blender."""

    def __init__(self) -> None:
        self.geometry_cache: dict[str, bpy.types.Mesh] = {}
        self.material_cache: dict[str, bpy.types.Material] = {}
        
    def import_file(
        self, filepath: str, context: Context, created_objects: Optional[list] = None
    ) -> tuple[bool, str]:
        """Main import entry point. Returns (success: bool, message: str). If created_objects is set, appends each created object."""
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            return False, f"Invalid JSON: {e}"
        except FileNotFoundError:
            return False, f"File not found: {filepath}"
        except Exception as e:
            return False, f"Read error: {e}"

        # Validate format (ObjectLoader JSON); support older versions and key order
        metadata = data.get("metadata") or {}
        if not isinstance(metadata, dict):
            metadata = {}
        meta_type = metadata.get("type", "Unknown")
        valid_types = {'Object', 'Scene', 'Geometry', 'Unknown'}

        # Accept if we have a known root key or geometries (legacy formats may omit metadata)
        root_obj = _get_first_key(data, ROOT_OBJECT_KEYS)
        has_geometries = _get_first_key(data, GEOMETRIES_KEYS) is not None

        has_legacy_geometry = (
            meta_type == "Geometry"
            and isinstance(data.get("vertices"), (list, tuple))
            and isinstance(data.get("faces"), (list, tuple))
        )
        if has_legacy_geometry:
            try:
                self._import_legacy_geometry(data, context, created_objects=created_objects)
                return True, "Import successful"
            except Exception as e:
                return False, f"Import failed: {e}\n{traceback.format_exc()}"

        if meta_type not in valid_types and not root_obj and not has_geometries:
            return False, (
                f"Invalid format: expected ObjectLoader JSON.\n"
                f"Found metadata.type='{meta_type}'. Need 'object'/'scene' or 'geometries'.\n"
                f"File: {Path(filepath).name}"
            )

        materials_list = _normalize_to_list(_get_first_key(data, MATERIALS_KEYS))
        geometries_list = _normalize_to_list(_get_first_key(data, GEOMETRIES_KEYS))

        try:
            self._preload_materials(materials_list)
            self._preload_geometries(geometries_list, context)

            if not root_obj:
                root_obj = data  # Some formats put scene at root level

            self._import_object(root_obj, None, context, created_objects=created_objects)
            return True, "Import successful"
        except Exception as e:
            return False, f"Import failed: {e}\n{traceback.format_exc()}"
    
    def _preload_materials(self, materials: list[dict]) -> None:
        """Pre-create Blender materials from three.js definitions (any key order)."""
        for idx, mat_data in enumerate(materials):
            if not isinstance(mat_data, dict):
                continue
            uuid_val = mat_data.get("uuid")
            name = mat_data.get("name") or f"Material_{(uuid_val or idx)!s}"[:63]
            mat = bpy.data.materials.new(name=name)
            mat.use_nodes = False
            color = hex_to_color(mat_data.get("color", DEFAULT_COLOR_WHITE))
            mat.diffuse_color = (*color, mat_data.get("opacity", 1.0))
            
            if mat_data.get("type") == "MeshPhongMaterial":
                mat.specular_intensity = (mat_data.get("specular", 0x888888) & 0xFFFFFF) / 0xFFFFFF
                mat.roughness = 1.0 - (mat_data.get("shininess", 30) / 100)
            
            if mat_data.get("transparent"):
                mat.blend_method = 'BLEND'
                mat.show_transparent_back = False
            
            cache_key = uuid_val if uuid_val is not None else f"__mat_{idx}"
            self.material_cache[cache_key] = mat
    
    def _preload_geometries(self, geometries: list[dict], context: Context) -> None:
        """Pre-create Blender meshes from three.js BufferGeometry (supports legacy key order)."""
        for idx, geom_data in enumerate(geometries):
            if not isinstance(geom_data, dict) or geom_data.get("type") != "BufferGeometry":
                continue
            # Format 4: data.attributes; older formats may nest differently
            data = geom_data.get("data") or geom_data
            if not isinstance(data, dict):
                continue
            attrs = data.get("attributes") or {}
            if not isinstance(attrs, dict):
                attrs = {}

            mesh = bpy.data.meshes.new(name=geom_data.get("name", "Geometry"))
            pos_attr = attrs.get("position") or {}
            positions = pos_attr.get("array", []) if isinstance(pos_attr, dict) else []
            verts = [tuple(positions[i:i+3]) for i in range(0, len(positions), 3)]

            # Index: in data or inside attributes (legacy)
            index_block = data.get("index") or attrs.get("index") or {}
            indices = index_block.get("array", []) if isinstance(index_block, dict) else []
            if indices:
                faces = [tuple(indices[i:i+3]) for i in range(0, len(indices), 3)]
            else:
                faces = [tuple(range(i, i+3)) for i in range(0, len(verts), 3)]
            
            if verts and faces:
                mesh.from_pydata(verts, [], faces)
                mesh.update()
            
            normal_attr = attrs.get("normal")
            if isinstance(normal_attr, dict) and normal_attr.get("array"):
                normals = normal_attr["array"]
                if len(normals) >= len(verts) * 3:
                    mesh.normals_split_custom_set_from_vertices(
                        [tuple(normals[i:i+3]) for i in range(0, len(normals), 3)]
                    )
                    if hasattr(mesh, "use_auto_smooth"):
                        mesh.use_auto_smooth = True
            
            uv_attr = attrs.get("uv")
            if isinstance(uv_attr, dict) and uv_attr.get("array") and mesh.uv_layers.new():
                uvs = uv_attr["array"]
                uv_layer = mesh.uv_layers.active
                for loop in mesh.loops:
                    idx = loop.index * 2
                    if idx + 1 < len(uvs):
                        uv_layer.data[loop.index].uv = (uvs[idx], 1.0 - uvs[idx + 1])

            geom_uuid = geom_data.get("uuid") or f"__geom_{idx}"
            self.geometry_cache[geom_uuid] = mesh

    def _import_legacy_geometry(
        self, data: dict, context: Context, created_objects: Optional[list] = None
    ) -> None:
        """Import legacy three.js Geometry format (v3: vertices, faces, normals at root)."""
        vertices_flat = data.get("vertices") or []
        faces_flat = data.get("faces") or []
        normals_flat = data.get("normals") or []
        name = data.get("name", "Geometry")

        verts = [
            (float(vertices_flat[i]), float(vertices_flat[i + 1]), float(vertices_flat[i + 2]))
            for i in range(0, len(vertices_flat), 3)
            if i + 2 < len(vertices_flat)
        ]
        if not verts:
            return

        # Faces: legacy format is 7 ints per triangle [type, v0, v1, v2, n0, n1, n2] or 6 [v0,v1,v2,n0,n1,n2]
        faces = []
        loop_normals = []
        i = 0
        while i + 5 < len(faces_flat):
            if i + 7 <= len(faces_flat) and faces_flat[i] == 32:
                # 7 values: 32, v0, v1, v2, n0, n1, n2
                v0, v1, v2 = int(faces_flat[i + 1]), int(faces_flat[i + 2]), int(faces_flat[i + 3])
                n0, n1, n2 = int(faces_flat[i + 4]), int(faces_flat[i + 5]), int(faces_flat[i + 6])
                i += 7
            else:
                v0, v1, v2 = int(faces_flat[i]), int(faces_flat[i + 1]), int(faces_flat[i + 2])
                n0, n1, n2 = int(faces_flat[i + 3]), int(faces_flat[i + 4]), int(faces_flat[i + 5])
                i += 6
            faces.append((v0, v1, v2))
            for ni in (n0, n1, n2):
                if ni * 3 + 2 < len(normals_flat):
                    loop_normals.append((
                        float(normals_flat[ni * 3]),
                        float(normals_flat[ni * 3 + 1]),
                        float(normals_flat[ni * 3 + 2]),
                    ))
                else:
                    loop_normals.append((0.0, 0.0, 1.0))

        if not faces:
            return

        mesh = bpy.data.meshes.new(name=name)
        mesh.from_pydata(verts, [], faces)
        mesh.update()

        if len(loop_normals) >= len(mesh.loops):
            mesh.normals_split_custom_set(loop_normals[: len(mesh.loops)])
            if hasattr(mesh, "use_auto_smooth"):
                mesh.use_auto_smooth = True

        obj = bpy.data.objects.new(name, mesh)
        context.collection.objects.link(obj)
        if created_objects is not None:
            created_objects.append(obj)

    def _import_object(
        self,
        obj_data: dict,
        parent: Optional[bpy.types.Object],
        context: Context,
        created_objects: Optional[list] = None,
    ) -> Optional[bpy.types.Object]:
        """Recursively import three.js object into Blender."""
        obj_type = obj_data.get("type", "Object3D")
        name = obj_data.get("name", obj_type)
        
        if obj_type == "Mesh":
            geom_ref = obj_data.get("geometry")
            mesh = None
            if isinstance(geom_ref, str):
                mesh = self.geometry_cache.get(geom_ref)
            elif isinstance(geom_ref, list) and geom_ref:
                mesh = self.geometry_cache.get(geom_ref[0])
            elif isinstance(geom_ref, (int, float)):
                mesh = self.geometry_cache.get(f"__geom_{int(geom_ref)}")  # Legacy index
            
            if not mesh:
                return None
            blender_obj = bpy.data.objects.new(name, mesh)
            
            mat_refs = obj_data.get("material")
            if mat_refs:
                if not isinstance(mat_refs, list):
                    mat_refs = [mat_refs]
                for ref in mat_refs:
                    # UUID string or legacy index (number)
                    mat_key = ref if ref is not None and not isinstance(ref, (int, float)) else f"__mat_{int(ref)}"
                    if mat_key in self.material_cache:
                        blender_obj.data.materials.append(self.material_cache[mat_key])
                            
        elif obj_type in ("PointLight", "DirectionalLight", "SpotLight"):
            light_type = {"PointLight": 'POINT', "DirectionalLight": 'SUN', "SpotLight": 'SPOT'}.get(obj_type, 'POINT')
            light = bpy.data.lights.new(name, type=light_type)
            light.color = hex_to_color(obj_data.get("color", 0xFFFFFF))
            light.energy = obj_data.get("intensity", 1.0)
            if obj_type == "SpotLight":
                light.spot_size = obj_data.get("angle", math.pi/4)
                light.spot_blend = obj_data.get("penumbra", 0.15)
            blender_obj = bpy.data.objects.new(name, light)
            
        elif obj_type in ("PerspectiveCamera", "OrthographicCamera"):
            cam = bpy.data.cameras.new(name)
            cam.angle_y = obj_data.get("fov", 45) * math.pi / 180
            if obj_type == "OrthographicCamera":
                cam.type = "ORTHO"
            cam.clip_start = obj_data.get("near", 0.1)
            cam.clip_end = obj_data.get("far", 2000)
            blender_obj = bpy.data.objects.new(name, cam)
        else:
            # Do not create empty objects; recurse into children with same parent
            children = obj_data.get("children")
            if isinstance(children, list):
                for child_data in children:
                    if isinstance(child_data, dict):
                        self._import_object(child_data, parent, context, created_objects=created_objects)
            return None

        if "matrix" in obj_data and len(obj_data["matrix"]) == MATRIX_ELEMENT_COUNT:
            try:
                blender_obj.matrix_world = list_to_matrix(obj_data["matrix"])
            except (ValueError, TypeError):
                pass
        blender_obj.hide_set(not obj_data.get("visible", True))
        context.collection.objects.link(blender_obj)
        if created_objects is not None:
            created_objects.append(blender_obj)
        if parent:
            blender_obj.parent = parent

        children = obj_data.get("children")
        if isinstance(children, list):
            for child_data in children:
                if isinstance(child_data, dict):
                    self._import_object(child_data, blender_obj, context, created_objects=created_objects)
        return blender_obj


# =============================================================================
# OPERATORS
# =============================================================================

class EXPORT_OT_threejs_objectloader(Operator, ExportHelper):
    """Export to three.js ObjectLoader format."""
    bl_idname = "export_scene.threejs_objectloader"
    bl_label = "Export three.js ObjectLoader"
    bl_description = "Export selected objects to three.js ObjectLoader JSON format 4+"
    
    filename_ext = ".json"
    filter_glob: StringProperty(default="*.json", options={"HIDDEN"})

    format_version: EnumProperty(
        name="Format version",
        description="Export for three.js Format 3 (legacy Geometry) or Format 4+ (ObjectLoader)",
        items=(
            ("4", "Format 4+ (ObjectLoader)", "BufferGeometry, scene hierarchy, materials"),
            ("3", "Format 3 (Legacy Geometry)", "vertices, normals, faces at root (io_three style)"),
        ),
        default="4",
    )
    precision: IntProperty(
        name="Decimal Precision",
        description="Number of decimal places for float values",
        default=4,
        min=0,
        max=10,
    )
    selected_only: BoolProperty(
        name="Selected Objects Only",
        description="Export only selected objects",
        default=True,
    )
    split_by_material: BoolProperty(
        name="Split by Material",
        description="Create separate geometries for each material",
        default=False,
    )
    export_normals: BoolProperty(
        name="Normals",
        description="Export per-corner normals (larger, but required for correct lighting)",
        default=True,
    )
    export_uvs: BoolProperty(
        name="UVs",
        description="Export UV coordinates (needed for textures)",
        default=True,
    )
    export_vertex_colors: BoolProperty(
        name="Vertex Colors",
        description="Export vertex color attribute 'color' (saves a lot if disabled)",
        default=True,
    )
    batch_export: BoolProperty(
        name="One file per object",
        description="Export each selected object to a separate JSON file in the same directory",
        default=False,
    )
    pretty_output: BoolProperty(
        name="Pretty print",
        description="Indent JSON for readability (larger file). Off = compact output, minimal size",
        default=False,
    )

    def execute(self, context: Context):
        exporter = ThreeJSObjectExporter(
            precision=self.precision,
            split_by_material=self.split_by_material,
            selected_only=self.selected_only,
            export_mesh=True,
            export_lights=True,
            export_cameras=True,
            export_normals=self.export_normals,
            export_uvs=self.export_uvs,
            export_vertex_colors=self.export_vertex_colors,
            format_version=self.format_version,
        )
        try:
            if self.batch_export:
                export_types = {"MESH", "LIGHT", "CAMERA"}
                objects = [
                    o for o in context.scene.objects
                    if getattr(o, "type", None) in export_types
                    and (not self.selected_only or o.select_get())
                    and o.visible_get()
                ]
                if not objects:
                    self.report({"WARNING"}, "No objects to export")
                    return {"CANCELLED"}
                if self.format_version == "3":
                    objects = [o for o in objects if getattr(o, "type", None) == "MESH"]
                    if not objects:
                        self.report({"WARNING"}, "No mesh objects for Format 3 export")
                        return {"CANCELLED"}
                directory = os.path.dirname(self.filepath)
                if not directory:
                    directory = os.path.dirname(os.path.abspath(self.filepath))
                exported = 0
                for obj in objects:
                    # Windows filename-safe, but keep spaces/unicode to match Blender naming as much as possible.
                    name = re.sub(r'[<>:"/\\|?*]', '_', obj.name)
                    name = name.rstrip(" .")
                    if not name:
                        name = f"object_{exported}"
                    path = os.path.join(directory, f"{name}.json")
                    batch_exporter = ThreeJSObjectExporter(
                        precision=self.precision,
                        split_by_material=self.split_by_material,
                        selected_only=self.selected_only,
                        export_mesh=True,
                        export_lights=True,
                        export_cameras=True,
                        export_normals=self.export_normals,
                        export_uvs=self.export_uvs,
                        export_vertex_colors=self.export_vertex_colors,
                        format_version=self.format_version,
                    )
                    data = batch_exporter.export(context, objects_override=[obj])
                    with open(path, "w", encoding="utf-8") as f:
                        if self.pretty_output:
                            json.dump(data, f, indent=2, ensure_ascii=False)
                        else:
                            json.dump(data, f, indent=None, separators=(",", ":"), ensure_ascii=False)
                    exported += 1
                self.report({"INFO"}, f"Exported {exported} file(s) to {directory}")
                return {"FINISHED"}
            data = exporter.export(context)
            base_name = Path(self.filepath).stem
            # In single export mode, if exactly one object is exported, use the user-provided filename stem
            # as the exported object's `name` (but never in batch mode).
            if self.format_version == "4":
                children = data.get("object", {}).get("children", [])
                if isinstance(children, list) and len(children) == 1 and base_name:
                    child = children[0]
                    if isinstance(child, dict):
                        child["name"] = base_name
            elif self.format_version == "3":
                if base_name and isinstance(data, dict) and "name" in data:
                    meshes = [
                        o for o in context.scene.objects
                        if getattr(o, "type", None) == "MESH"
                        and (not self.selected_only or o.select_get())
                        and o.visible_get()
                    ]
                    if len(meshes) == 1:
                        data["name"] = base_name
            with open(self.filepath, "w", encoding="utf-8") as f:
                if self.pretty_output:
                    json.dump(data, f, indent=2, ensure_ascii=False)
                else:
                    json.dump(data, f, indent=None, separators=(",", ":"), ensure_ascii=False)
            self.report({"INFO"}, f"Exported to {Path(self.filepath).name}")
            return {"FINISHED"}
        except Exception as e:
            self.report({"ERROR"}, f"Export failed: {e}")
            return {"CANCELLED"}
    
    def draw(self, context: Context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        
        col = layout.column(heading="Options")
        col.prop(self, "format_version")
        col.prop(self, "precision")
        col.prop(self, "selected_only")
        col.prop(self, "split_by_material")
        col.separator()
        col.prop(self, "export_normals")
        col.prop(self, "export_uvs")
        col.prop(self, "export_vertex_colors")
        col.prop(self, "batch_export")
        col.prop(self, "pretty_output")


class IMPORT_OT_threejs_objectloader(Operator, ImportHelper):
    bl_idname = "import_scene.threejs_objectloader"
    bl_label = "Import three.js ObjectLoader"
    bl_description = "Import three.js ObjectLoader JSON format 4+ (multi-select supported)"

    filename_ext = ".json"
    filter_glob: StringProperty(default="*.json", options={"HIDDEN"})
    files: CollectionProperty(type=OperatorFileListElement, options={"HIDDEN", "SKIP_SAVE"})
    directory: StringProperty(subtype="DIR_PATH", options={"HIDDEN", "SKIP_SAVE"})
    arrange_along_x: BoolProperty(
        name="Arrange along X",
        description="When importing multiple files, place each model along X with spacing by average bbox size",
        default=True,
    )

    def execute(self, context: Context):
        importer = ThreeJSObjectImporter()
        if self.files:
            # Multi-file import
            groups = []
            failed = []
            for f in self.files:
                path = os.path.join(self.directory, f.name)
                created = []
                success, message = importer.import_file(path, context, created_objects=created)
                if success:
                    groups.append(created)
                else:
                    failed.append(f"{f.name}: {message}")
            if failed:
                self.report({"WARNING"}, f"Failed: {'; '.join(failed[:3])}{'...' if len(failed) > 3 else ''}")
            if groups and self.arrange_along_x:
                arrange_objects_along_x(groups, gap_scale=1.0)
            count = sum(len(g) for g in groups)
            self.report({"INFO"}, f"Imported {len(groups)} file(s), {count} object(s)")
            return {"FINISHED"}
        # Single file
        success, message = importer.import_file(self.filepath, context)
        if success:
            self.report({"INFO"}, f"Imported: {Path(self.filepath).name}")
            return {"FINISHED"}
        self.report({"ERROR"}, message)
        return {"CANCELLED"}

    def draw(self, context: Context):
        layout = self.layout
        layout.use_property_split = True
        layout.prop(self, "arrange_along_x")


# =============================================================================
# UI & REGISTRATION
# =============================================================================

class FILE_PT_threejs_objectloader(Panel):
    """Panel in file browser for quick access."""
    bl_space_type = 'FILE_BROWSER'
    bl_region_type = 'TOOL_PROPS'
    bl_label = "three.js Options"
    bl_parent_id = "FILE_PT_operator"
    
    @classmethod
    def poll(cls, context: Context) -> bool:
        operator = context.space_data.active_operator
        return operator and operator.bl_idname in {
            "export_scene.threejs_objectloader",
            "import_scene.threejs_objectloader"
        }
    
    def draw(self, context: Context):
        layout = self.layout
        operator = context.space_data.active_operator
        
        if operator.bl_idname == "export_scene.threejs_objectloader":
            layout.prop(operator, "format_version")
            layout.prop(operator, "precision")
            layout.prop(operator, "selected_only")
            layout.prop(operator, "split_by_material")
            layout.prop(operator, "export_normals")
            layout.prop(operator, "export_uvs")
            layout.prop(operator, "export_vertex_colors")
            layout.prop(operator, "batch_export")
            layout.prop(operator, "pretty_output")
        elif operator.bl_idname == "import_scene.threejs_objectloader":
            layout.prop(operator, "arrange_along_x")


def menu_func_export(self, context: Context) -> None:
    """Add to File > Export menu."""
    self.layout.operator(
        EXPORT_OT_threejs_objectloader.bl_idname,
        text="three.js ObjectLoader (.json)"
    )


def menu_func_import(self, context: Context) -> None:
    """Add to File > Import menu."""
    self.layout.operator(
        IMPORT_OT_threejs_objectloader.bl_idname,
        text="three.js ObjectLoader (.json)"
    )


# =============================================================================
# REGISTER / UNREGISTER
# =============================================================================

classes = (
    EXPORT_OT_threejs_objectloader,
    IMPORT_OT_threejs_objectloader,
    FILE_PT_threejs_objectloader,
)

def register() -> None:
    """Register add-on classes and menu entries."""
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.TOPBAR_MT_file_export.append(menu_func_export)
    bpy.types.TOPBAR_MT_file_import.append(menu_func_import)


def unregister() -> None:
    """Unregister add-on classes and menu entries."""
    bpy.types.TOPBAR_MT_file_export.remove(menu_func_export)
    bpy.types.TOPBAR_MT_file_import.remove(menu_func_import)
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()