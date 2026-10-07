"""Preflight checks against SheepIt's project rules."""

import os

import bpy

from . import sheepit_deps as D
from . import sheepit_hidden as H

SUPPORTED_ENGINES = {"CYCLES", "BLENDER_EEVEE", "BLENDER_EEVEE_NEXT", "BLENDER_WORKBENCH"}
EXR_CODECS_OK = {"PXR24", "ZIP", "PIZ", "RLE", "ZIPS", "DWAA", "DWAB"}
EXR_MAX = (4096, 2160)


class Result:
    def __init__(self):
        self.messages = []  # (level, text)

    def add(self, level, text):
        self.messages.append((level, text))

    def count(self, level):
        return sum(1 for lvl, _ in self.messages if lvl == level)


def run(context, props):
    res = Result()
    add = res.add

    if not bpy.data.filepath:
        add("ERROR", "Save the .blend file first. The export is built from its folder.")
        return res
    if bpy.data.is_dirty:
        add("INFO", "Unsaved changes will be included in the export.")

    scene = context.scene
    r = scene.render

    if bpy.app.version_cycle != "release":
        add("WARNING", f"Blender {bpy.app.version_string} isn't a stable release. SheepIt "
                       "only runs stable versions, so pick the closest one when uploading.")

    # ---- engine
    if r.engine not in SUPPORTED_ENGINES:
        add("ERROR", f"Render engine '{r.engine}' isn't available on SheepIt. Use Cycles, "
                     "EEVEE or Workbench.")
    engines = {sc.render.engine for sc in bpy.data.scenes}
    if len(engines) > 1:
        add("WARNING", "Scenes use different render engines. SheepIt needs every scene in "
                       "the file on the same engine.")

    if scene.camera is None:
        add("ERROR", f"Scene '{scene.name}' has no active camera.")

    # ---- frames and output
    frames = len(range(scene.frame_start, scene.frame_end + 1, max(1, scene.frame_step)))
    add("INFO", f"{frames} frame{'s' if frames != 1 else ''} "
                f"({scene.frame_start}-{scene.frame_end}) at "
                f"{r.resolution_x * r.resolution_percentage // 100}x"
                f"{r.resolution_y * r.resolution_percentage // 100}.")

    ims = r.image_settings
    media = getattr(ims, "media_type", "")
    fmt = ims.file_format
    if media == "VIDEO" or fmt in {"FFMPEG", "AVI_JPEG", "AVI_RAW"}:
        add("WARNING", "Output is set to a video format. SheepIt renders individual frames "
                       "and builds the video for you; set the output to PNG or EXR.")
    if fmt in {"OPEN_EXR", "OPEN_EXR_MULTILAYER"} or media == "MULTI_LAYER_IMAGE":
        codec = getattr(ims, "exr_codec", "NONE")
        if codec not in EXR_CODECS_OK:
            add("ERROR", f"EXR codec '{codec}' isn't allowed. Use PXR24, ZIP, PIZ, RLE, "
                         "ZIPS, DWAA or DWAB.")
        w = r.resolution_x * r.resolution_percentage // 100
        h = r.resolution_y * r.resolution_percentage // 100
        if w > EXR_MAX[0] or h > EXR_MAX[1]:
            add("ERROR", f"EXR output is limited to {EXR_MAX[0]}x{EXR_MAX[1]}; "
                         f"this renders at {w}x{h}.")
        if frames < 2:
            add("ERROR", "EXR output is only accepted for animations, not single images.")
        add("INFO", "EXR frames aren't split across machines, so each frame renders whole "
                    "on one computer.")

    if r.use_border:
        add("WARNING", "Render Region is on. SheepIt splits frames into tiles itself, so "
                       "the region may be ignored.")

    # ---- scripts
    py_drivers = []
    for idb in D.all_ids():
        for owner in (idb, getattr(idb, "node_tree", None)):
            ad = getattr(owner, "animation_data", None) if owner is not None else None
            if ad is None:
                continue
            for fc in ad.drivers:
                drv = fc.driver
                if drv.type == "SCRIPTED" and not drv.is_simple_expression:
                    py_drivers.append(f"{owner.name}: {fc.data_path}")
    if py_drivers:
        add("WARNING", f"{len(py_drivers)} drivers need Python, which SheepIt disables, so "
                       f"they won't update (e.g. {py_drivers[0]}). Bake them to keyframes.")
    for t in bpy.data.texts:
        if t.use_module:
            add("WARNING", f"Text '{t.name}' is set to run on load; scripts don't run on "
                           "SheepIt.")
    if getattr(getattr(scene, "cycles", None), "shading_system", False) and r.engine == "CYCLES":
        add("WARNING", "Open Shading Language is on. OSL scripts may not run on SheepIt.")

    # ---- compositor File Output nodes
    tree = getattr(scene, "compositing_node_group", None) or getattr(scene, "node_tree", None)
    if r.use_compositing and tree is not None:
        if any(n.bl_idname == "CompositorNodeOutputFile" for n in tree.nodes):
            add("WARNING", "The compositor has a File Output node. SheepIt only returns the "
                           "main render output, not extra files.")

    # ---- hidden objects
    if props.remove_hidden:
        obs, mods = H.plan(scene, context.view_layer, props.remove_viewport_hidden)
        if obs or mods:
            add("INFO", f"{len(obs)} hidden objects and {len(mods)} render-disabled modifiers "
                        "will be left out of the export.")

    # ---- linked libraries
    if bpy.data.libraries:
        add("INFO", f"{len(bpy.data.libraries)} linked libraries will be made local.")

    # ---- external files
    found = D.collect(include_ocio=props.include_ocio)
    for level, text in found.notes:
        add(level, text)
    total = 0
    counts = {}
    for d in found.deps:
        if d.missing:
            if d.kind == D.FILES:
                add("ERROR", f"Missing file: {d.label} ({d.primary})")
            else:
                add("WARNING", f"Folder not found: {d.label} ({d.primary})")
            continue
        total += d.size()
        key = d.label.split(" '")[0].split(" (")[0]
        counts[key] = counts.get(key, 0) + 1
    if counts:
        add("INFO", "Will include: " + ", ".join(f"{v} {k.lower()}" for k, v in counts.items()))

    try:
        total += os.path.getsize(bpy.data.filepath)
    except OSError:
        pass
    limit = int(props.size_limit_mb * 1_000_000)
    add("INFO", f"About {total / 1_000_000:,.0f} MB before compression "
                f"(limit {props.size_limit_mb:,} MB).")
    if total > limit:
        if props.reduce_size:
            add("WARNING", "This may go over the limit; textures and video textures will be "
                           f"shrunk (down to {props.min_texture}px) if the zip is too big.")
        else:
            add("WARNING", "This may go over the limit. Turn on 'Shrink if too big'.")
    return res
