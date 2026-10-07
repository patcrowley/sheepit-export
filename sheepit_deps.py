"""Find every external file a .blend needs to render.

Shared by the add-on (preflight checks) and the background worker (export).
This module must not use relative imports: the worker imports it directly.
"""

import os
import re

import bpy

TOKEN_RE = re.compile(r"(<UDIM>|<UVTILE>)")

FILES = "FILES"
DIR = "DIR"

# What size reduction may do to a dependency's files.
REDUCE_NONE = ""
REDUCE_IMAGE = "IMAGE"  # still images / image sequences used as textures
REDUCE_VIDEO = "VIDEO"  # video files used as textures


# --------------------------------------------------------------------------
# Path helpers
# --------------------------------------------------------------------------

def abspath(path, library=None):
    if not path:
        return ""
    try:
        p = bpy.path.abspath(path, library=library)
    except TypeError:
        p = bpy.path.abspath(path)
    return os.path.normpath(p)


def to_rel(abs_path, is_dir=False):
    """Blend-relative path with forward slashes (works on every OS)."""
    rel = bpy.path.relpath(abs_path).replace("\\", "/")
    if is_dir and not rel.endswith("/"):
        rel += "/"
    return rel


def token_files(path):
    """Files matching a <UDIM>/<UVTILE> pattern."""
    d, base = os.path.split(path)
    if not os.path.isdir(d):
        return []
    parts = TOKEN_RE.split(base)
    rx = "".join(
        r"\d{4}" if p == "<UDIM>" else r"u\d+_v\d+" if p == "<UVTILE>" else re.escape(p)
        for p in parts
    )
    rx = re.compile("^" + rx + "$")
    return sorted(os.path.join(d, f) for f in os.listdir(d) if rx.match(f))


def sequence_files(path):
    """All frames of a numbered sequence, given any one frame."""
    d, base = os.path.split(path)
    stem, ext = os.path.splitext(base)
    m = re.search(r"\d+$", stem)
    if not m or not os.path.isdir(d):
        return [path] if os.path.isfile(path) else []
    rx = re.compile("^" + re.escape(stem[: m.start()]) + r"\d+" + re.escape(ext) + "$")
    return sorted(os.path.join(d, f) for f in os.listdir(d) if rx.match(f))


def single_file(path):
    return [path] if path and os.path.isfile(path) else []


def dir_size(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def dir_has_files(path):
    if not os.path.isdir(path):
        return False
    for _root, _dirs, files in os.walk(path):
        if files:
            return True
    return False


# --------------------------------------------------------------------------
# Dependency record
# --------------------------------------------------------------------------

class Dep:
    """One external dependency.

    kind FILES: ``files`` are absolute paths that all live in one folder;
                ``primary`` is the path as stored (may contain <UDIM>, or be a
                folder ending in a separator for VSE image strips).
    kind DIR:   ``primary`` is a folder copied whole.
    ``setter(new_abs)`` rewrites the datablock to point at the copied location.
    ``dest`` pins a DIR to a fixed name at the project root (e.g. "OCIO").
    """

    def __init__(self, label, kind, primary, files=None, setter=None,
                 editable=True, dest=None, reduce=REDUCE_NONE, extra=None):
        self.label = label
        self.kind = kind
        self.primary = primary
        self.files = files if files is not None else []
        self.setter = setter
        self.editable = editable
        self.dest = dest
        self.reduce = reduce
        self.extra = extra or {}

    @property
    def missing(self):
        if self.kind == DIR:
            return not os.path.isdir(self.primary)
        return not self.files

    def size(self):
        if self.kind == DIR:
            return dir_size(self.primary) if os.path.isdir(self.primary) else 0
        total = 0
        for f in self.files:
            try:
                total += os.path.getsize(f)
            except OSError:
                pass
        return total


class Collected:
    def __init__(self):
        self.deps = []
        self.notes = []  # (level, message)

    def note(self, level, msg):
        self.notes.append((level, msg))


# --------------------------------------------------------------------------
# Helpers for walking data
# --------------------------------------------------------------------------

ID_COLLECTIONS = (
    "actions", "armatures", "brushes", "cache_files", "cameras", "collections",
    "curves", "fonts", "grease_pencils", "grease_pencils_v3", "hair_curves",
    "images", "lattices", "lightprobes", "lights", "linestyles", "masks",
    "materials", "meshes", "metaballs", "movieclips", "node_groups", "objects",
    "paint_curves", "palettes", "particles", "pointclouds", "scenes", "shape_keys",
    "sounds", "speakers", "texts", "textures", "volumes", "workspaces", "worlds",
)


def all_ids():
    for name in ID_COLLECTIONS:
        coll = getattr(bpy.data, name, None)
        if coll is None:
            continue
        for idb in coll:
            yield idb


def all_node_trees():
    trees = list(bpy.data.node_groups)
    for name in ("materials", "worlds", "lights", "scenes", "textures", "linestyles"):
        for idb in getattr(bpy.data, name, []):
            nt = getattr(idb, "node_tree", None)
            if nt is not None:
                trees.append(nt)
    return trees


def is_unused(idb):
    return idb.users == 0 or (idb.use_fake_user and idb.users == 1)


def tree_has_node(tree, idnames, _seen=None):
    seen = _seen if _seen is not None else set()
    if tree is None or tree.as_pointer() in seen:
        return False
    seen.add(tree.as_pointer())
    for node in tree.nodes:
        if node.bl_idname in idnames:
            return True
        sub = getattr(node, "node_tree", None)
        if sub is not None and tree_has_node(sub, idnames, seen):
            return True
    return False


def strips_of(scene):
    se = scene.sequence_editor
    if se is None:
        return []
    strips = getattr(se, "strips_all", None)
    if strips is None:
        strips = getattr(se, "sequences_all", [])
    return list(strips)


# --------------------------------------------------------------------------
# Collection
# --------------------------------------------------------------------------

def collect(original_dir=None, include_ocio=True):
    """Return a Collected with every external dependency of the open file."""
    out = Collected()
    blend = bpy.data.filepath
    stem = os.path.splitext(os.path.basename(blend))[0]
    original_dir = original_dir or os.path.dirname(blend)

    def add_files(label, primary, files, setter, editable=True, reduce=REDUCE_NONE):
        out.deps.append(Dep(label, FILES, primary, files, setter, editable, reduce=reduce))

    def add_dir(label, path, setter, editable=True, dest=None, extra=None):
        out.deps.append(Dep(label, DIR, path, None, setter, editable, dest, extra=extra))

    # ---- Images (textures, image sequences, UDIMs, video textures)
    for img in bpy.data.images:
        if img.source not in {"FILE", "SEQUENCE", "TILED", "MOVIE"}:
            continue
        if img.packed_file is not None or len(getattr(img, "packed_files", [])) > 0:
            continue
        if is_unused(img):
            continue
        p = abspath(img.filepath, img.library)
        if TOKEN_RE.search(p):
            files = token_files(p)
        elif img.source == "SEQUENCE":
            files = sequence_files(p)
        else:
            files = single_file(p)
        kind = {"MOVIE": "Video texture", "SEQUENCE": "Image sequence",
                "TILED": "UDIM image"}.get(img.source, "Image")

        def set_img(new, img=img):
            img.filepath = to_rel(new)

        add_files(f"{kind} '{img.name}'", p, files, set_img, img.library is None,
                  REDUCE_VIDEO if img.source == "MOVIE" else REDUCE_IMAGE)

    # ---- Movie clips (camera tracking, compositor, camera backgrounds)
    for clip in bpy.data.movieclips:
        if is_unused(clip):
            continue
        p = abspath(clip.filepath, clip.library)
        files = sequence_files(p) if clip.source == "SEQUENCE" else single_file(p)

        def set_clip(new, clip=clip):
            clip.filepath = to_rel(new)

        add_files(f"Movie clip '{clip.name}'", p, files, set_clip, clip.library is None)

    # ---- Fonts
    for font in bpy.data.fonts:
        if font.filepath in ("", "<builtin>") or font.packed_file is not None or is_unused(font):
            continue
        p = abspath(font.filepath, font.library)

        def set_font(new, font=font):
            font.filepath = to_rel(new)

        add_files(f"Font '{font.name}'", p, single_file(p), set_font, font.library is None)

    # ---- OpenVDB volumes
    for vol in getattr(bpy.data, "volumes", []):
        if getattr(vol, "packed_file", None) is not None or is_unused(vol):
            continue
        p = abspath(vol.filepath, vol.library)
        files = sequence_files(p) if getattr(vol, "is_sequence", False) else single_file(p)

        def set_vol(new, vol=vol):
            vol.filepath = to_rel(new)

        add_files(f"Volume '{vol.name}'", p, files, set_vol, vol.library is None)

    # ---- Alembic / USD caches
    for cf in getattr(bpy.data, "cache_files", []):
        if is_unused(cf):
            continue
        p = abspath(cf.filepath, cf.library)
        files = sequence_files(p) if getattr(cf, "is_sequence", False) else single_file(p)

        def set_cf(new, cf=cf):
            cf.filepath = to_rel(new)

        add_files(f"Cache file '{cf.name}'", p, files, set_cf, cf.library is None)

    # ---- Nodes with external files (IES lights, OSL scripts)
    for tree in all_node_trees():
        lib = getattr(tree, "library", None)
        for node in tree.nodes:
            if node.bl_idname not in {"ShaderNodeTexIES", "ShaderNodeScript"}:
                continue
            if getattr(node, "mode", "") != "EXTERNAL" or not node.filepath:
                continue
            p = abspath(node.filepath, lib)

            def set_node(new, node=node):
                node.filepath = to_rel(new)

            kind = "IES file" if node.bl_idname == "ShaderNodeTexIES" else "OSL script"
            add_files(f"{kind} '{node.name}' ({tree.name})", p, single_file(p),
                      set_node, lib is None)

    # ---- Video sequencer strips (only when the sequencer is used in the render)
    for sc in bpy.data.scenes:
        if not sc.render.use_sequencer:
            continue
        for s in strips_of(sc):
            if s.type == "IMAGE":
                d = abspath(s.directory, sc.library)
                files = [os.path.join(d, e.filename) for e in s.elements
                         if os.path.isfile(os.path.join(d, e.filename))]

                def set_dir(new, s=s):
                    s.directory = to_rel(new, True)

                add_files(f"VSE image strip '{s.name}'", d + os.sep, files, set_dir,
                          sc.library is None)
            elif s.type == "MOVIE":
                p = abspath(s.filepath, sc.library)

                def set_mov(new, s=s):
                    s.filepath = to_rel(new)

                add_files(f"VSE movie strip '{s.name}'", p, single_file(p), set_mov,
                          sc.library is None)

    # ---- Simulation caches
    need_blendcache = False

    def handle_pc(pc, label, editable):
        nonlocal need_blendcache
        if pc is None:
            return
        if pc.use_external:
            p = abspath(pc.filepath)

            def set_pc(new, pc=pc):
                pc.filepath = to_rel(new, True)

            add_dir(f"External cache '{label}'", p, set_pc, editable)
        elif pc.use_disk_cache:
            need_blendcache = True
        if not pc.is_baked and not pc.use_external:
            out.note("WARNING", f"{label} is not baked. Each SheepIt machine renders a "
                                "different frame, so unbaked simulations come out wrong. "
                                "Bake it (Bake All Dynamics) before exporting.")

    for sc in bpy.data.scenes:
        rbw = sc.rigidbody_world
        if rbw is not None and rbw.enabled and rbw.collection is not None:
            handle_pc(rbw.point_cache, f"Rigid body world ({sc.name})", sc.library is None)

    for ob in bpy.data.objects:
        if is_unused(ob):
            continue
        ed = ob.library is None
        for psys in ob.particle_systems:
            st = psys.settings
            if st.type == "EMITTER" or psys.use_hair_dynamics:
                handle_pc(psys.point_cache, f"Particles '{psys.name}' on {ob.name}", ed)
        for mod in ob.modifiers:
            t = mod.type
            if t in {"CLOTH", "SOFT_BODY"}:
                handle_pc(mod.point_cache, f"{mod.name} on {ob.name}", ed)
            elif t == "DYNAMIC_PAINT" and mod.ui_type == "CANVAS" and mod.canvas_settings:
                for surf in mod.canvas_settings.canvas_surfaces:
                    if surf.surface_format == "VERTEX":
                        handle_pc(surf.point_cache, f"Dynamic paint '{surf.name}' on {ob.name}", ed)
            elif t == "FLUID" and mod.fluid_type == "DOMAIN":
                ds = mod.domain_settings
                p = abspath(ds.cache_directory)
                if ds.cache_type == "REPLAY":
                    out.note("WARNING", f"Fluid domain {ob.name} uses a Replay cache, which "
                                        "isn't saved to disk. Switch Cache Type to All or "
                                        "Modular and bake.")
                if dir_has_files(p):
                    def set_fluid(new, ds=ds):
                        ds.cache_directory = to_rel(new, True)

                    add_dir(f"Fluid cache ({ob.name})", p, set_fluid, ed)
                else:
                    out.note("WARNING", f"Fluid domain {ob.name} has no baked cache in {p}.")
            elif t == "OCEAN" and getattr(mod, "is_cached", False) and mod.filepath:
                p = abspath(mod.filepath)

                def set_ocean(new, mod=mod):
                    mod.filepath = to_rel(new, True)

                add_dir(f"Ocean cache ({ob.name})", p, set_ocean, ed)
            elif t == "NODES":
                handle_nodes_modifier(out, ob, mod, ed, add_dir)

    if need_blendcache:
        bc = os.path.join(original_dir, "blendcache_" + stem)
        if os.path.isdir(bc):
            add_dir("Disk point caches", bc, None, dest="blendcache_" + stem)
        else:
            out.note("WARNING", f"Some caches are set to Disk Cache but {bc} doesn't exist.")

    # ---- Custom OCIO config (e.g. ACES)
    cfg = os.environ.get("OCIO", "")
    if include_ocio and cfg and os.path.isfile(cfg):
        add_dir("OCIO color config", os.path.dirname(cfg), None, dest="OCIO",
                extra={"config": cfg})

    return out


def handle_nodes_modifier(out, ob, mod, editable, add_dir):
    group = mod.node_group
    has_sim = group is not None and tree_has_node(
        group, {"GeometryNodeSimulationOutput", "GeometryNodeBake"})
    bakes = list(getattr(mod, "bakes", []))
    mod_target = getattr(mod, "bake_target", "DISK")
    mod_dir = abspath(getattr(mod, "bake_directory", ""))
    uses_mod_dir = False
    found = False

    for b in bakes:
        target = getattr(b, "bake_target", "INHERIT")
        if target == "INHERIT":
            target = mod_target
        if target == "PACKED":
            found = True
            continue
        if getattr(b, "use_custom_path", False) and b.directory:
            p = abspath(b.directory)
            if dir_has_files(p):
                found = True

                def set_b(new, b=b):
                    b.directory = to_rel(new, True)

                add_dir(f"Geometry Nodes bake ({ob.name}/{mod.name})", p, set_b, editable)
        else:
            uses_mod_dir = True

    if uses_mod_dir and mod_dir and dir_has_files(mod_dir):
        found = True

        def set_m(new, mod=mod):
            mod.bake_directory = to_rel(new, True)

        add_dir(f"Geometry Nodes bakes ({ob.name}/{mod.name})", mod_dir, set_m, editable)

    if has_sim and not found:
        out.note("WARNING", f"Geometry Nodes on {ob.name} ({mod.name}) has a simulation or "
                            "bake node but no baked data was found. Bake it first.")
