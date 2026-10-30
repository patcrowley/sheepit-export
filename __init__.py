"""SheepIt Export: package the current scene into a zip ready to upload to the
SheepIt render farm, with every external file included and paths made relative.
"""

bl_info = {
    "name": "SheepIt Export",
    "author": "Pat",
    "version": (1, 0, 2),
    "blender": (4, 2, 0),
    "location": "Properties > Render > SheepIt Export",
    "description": "Package the scene and all external files into a SheepIt-ready zip",
    "category": "Render",
}

import json
import os
import shutil
import subprocess
import tempfile

import bpy
from bpy.props import (BoolProperty, CollectionProperty, EnumProperty, IntProperty,
                       PointerProperty, StringProperty)

from . import checks

ADDON_DIR = os.path.dirname(os.path.abspath(__file__))
SHEEPIT_URL = "https://www.sheepit-renderfarm.com/"


# Blender 4.5+ marks "//" paths red unless the property says it accepts them.
REL_PATH = ({"PATH_SUPPORTS_BLEND_RELATIVE"}
            if "is_path_supports_blend_relative" in bpy.types.Property.bl_rna.properties
            else set())


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------

class SheepItPrefs(bpy.types.AddonPreferences):
    bl_idname = __package__

    ffmpeg_path: StringProperty(
        name="ffmpeg",
        description="Optional path to an ffmpeg executable, used to shrink video "
                    "textures when the zip is over the limit",
        subtype="FILE_PATH",
        options=REL_PATH,
    )

    def draw(self, context):
        self.layout.prop(self, "ffmpeg_path")
        found = self.ffmpeg_path or shutil.which("ffmpeg")
        self.layout.label(text=f"Using: {found}" if found else
                          "ffmpeg not found: video textures can't be shrunk",
                          icon="CHECKMARK" if found else "INFO")


class SheepItSettings(bpy.types.PropertyGroup):
    output_dir: StringProperty(
        name="Output Folder", subtype="DIR_PATH", default="//sheepit_export/",
        options=REL_PATH,
        description="Where the zip is written")
    remove_hidden: BoolProperty(
        name="Remove Hidden", default=True,
        description="Leave out objects hidden from the render (camera icon off, or in "
                    "excluded/render-disabled collections) and render-disabled modifiers. "
                    "Objects something visible depends on, like boolean cutters or "
                    "instanced collections, are kept")
    remove_viewport_hidden: BoolProperty(
        name="Also Viewport-Hidden", default=False,
        description="Also leave out objects hidden in the viewport (eye or monitor icon). "
                    "These normally still render, so this can change the image")
    include_ocio: BoolProperty(
        name="Include OCIO Config", default=True,
        description="Copy the custom OCIO color config (from the OCIO environment variable) "
                    "into an OCIO folder next to the .blend")
    reduce_size: BoolProperty(
        name="Shrink if Too Big", default=True,
        description="If the zip is over the size limit, downscale image textures and image "
                    "sequences step by step (4096, 2048, 1024 ... px) and re-encode video "
                    "textures until it fits")
    min_texture: EnumProperty(
        name="Smallest Allowed", default="1024",
        description="Shrinking stops at this texture size",
        items=[(s, f"{s} px", "") for s in ("2048", "1024", "512", "256")])
    size_limit_mb: IntProperty(
        name="Size Limit (MB)", default=2048, min=50,
        description="SheepIt's upload limit per project")
    ignore_errors: BoolProperty(
        name="Export Even With Errors", default=False,
        description="Build the zip even if the check finds errors")


class SheepItReport(bpy.types.PropertyGroup):
    level: StringProperty()
    text: StringProperty()


def set_reports(wm, messages):
    wm.sheepit_reports.clear()
    order = {"ERROR": 0, "WARNING": 1, "INFO": 2, "OK": 3}
    for level, text in sorted(messages, key=lambda m: order.get(m[0], 9)):
        item = wm.sheepit_reports.add()
        item.level, item.text = level, text


def redraw(context):
    for window in context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == "PROPERTIES":
                area.tag_redraw()


# --------------------------------------------------------------------------
# Operators
# --------------------------------------------------------------------------

class SHEEPIT_OT_check(bpy.types.Operator):
    bl_idname = "sheepit.check"
    bl_label = "Check Scene"
    bl_description = "Check the scene against SheepIt's project rules"

    def execute(self, context):
        res = checks.run(context, context.scene.sheepit_export)
        set_reports(context.window_manager, res.messages)
        e, w = res.count("ERROR"), res.count("WARNING")
        self.report({"ERROR" if e else "WARNING" if w else "INFO"},
                    f"SheepIt check: {e} errors, {w} warnings")
        return {"FINISHED"}


class SHEEPIT_OT_export(bpy.types.Operator):
    bl_idname = "sheepit.export"
    bl_label = "Export for SheepIt"
    bl_description = ("Copy the scene and every external file into a folder, make paths "
                      "relative and zip it for upload")

    _timer = None
    _proc = None

    @classmethod
    def poll(cls, context):
        return not context.window_manager.sheepit_running

    def invoke(self, context, event):
        wm = context.window_manager
        props = context.scene.sheepit_export
        res = checks.run(context, props)
        set_reports(wm, res.messages)
        if res.count("ERROR") and not props.ignore_errors:
            self.report({"ERROR"}, "Fix the errors in the SheepIt panel first "
                                   "(or turn on 'Export Even With Errors')")
            return {"CANCELLED"}

        src = bpy.data.filepath
        stem = os.path.splitext(os.path.basename(src))[0]
        out_dir = os.path.normpath(bpy.path.abspath(props.output_dir))
        os.makedirs(out_dir, exist_ok=True)

        # Stage next to the output (same drive as the project, so relative
        # paths can always be remapped).
        self.stage_root = tempfile.mkdtemp(prefix=".sheepit_stage_", dir=out_dir)
        proj = os.path.join(self.stage_root, "project")
        os.makedirs(proj)
        staged = os.path.join(proj, os.path.basename(src))
        bpy.ops.wm.save_as_mainfile(filepath=staged, copy=True, relative_remap=True,
                                    compress=False)

        prefs = context.preferences.addons[__package__].preferences
        self.output = os.path.join(out_dir, f"{stem}_sheepit.zip")
        self.result_path = os.path.join(self.stage_root, "result.json")
        self.log_path = os.path.join(self.stage_root, "worker.log")
        cfg = {
            "addon_dir": ADDON_DIR,
            "original_dir": os.path.dirname(src),
            "out_dir": out_dir,
            "stage_root": self.stage_root,
            "output": self.output,
            "result_path": self.result_path,
            "remove_hidden": props.remove_hidden,
            "remove_viewport_hidden": props.remove_viewport_hidden,
            "include_ocio": props.include_ocio,
            "reduce": props.reduce_size,
            "min_texture": int(props.min_texture),
            "limit_bytes": int(props.size_limit_mb * 1_000_000),
            "ffmpeg": bpy.path.abspath(prefs.ffmpeg_path) if prefs.ffmpeg_path else "",
        }
        cfg_path = os.path.join(self.stage_root, "config.json")
        with open(cfg_path, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh)

        cmd = [bpy.app.binary_path, "--background", "--factory-startup",
               "--disable-autoexec", staged, "--python-exit-code", "1",
               "--python", os.path.join(ADDON_DIR, "worker.py"), "--", cfg_path]
        self._log = open(self.log_path, "w", encoding="utf-8", errors="replace")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self._proc = subprocess.Popen(cmd, stdout=self._log, stderr=subprocess.STDOUT,
                                      creationflags=flags)

        wm.sheepit_running = True
        wm.sheepit_status = "Starting..."
        self._timer = wm.event_timer_add(0.5, window=context.window)
        wm.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def modal(self, context, event):
        wm = context.window_manager
        if event.type == "ESC" and event.value == "PRESS":
            self._proc.terminate()
            self._finish(context, cancelled=True)
            self.report({"WARNING"}, "SheepIt export cancelled")
            return {"CANCELLED"}
        if event.type != "TIMER":
            return {"PASS_THROUGH"}

        status = self._last_progress()
        if status and status != wm.sheepit_status:
            wm.sheepit_status = status
            redraw(context)
        if self._proc.poll() is None:
            return {"PASS_THROUGH"}

        ok = self._finish(context)
        return {"FINISHED"} if ok else {"CANCELLED"}

    def _last_progress(self):
        try:
            with open(self.log_path, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                fh.seek(max(0, fh.tell() - 4096))
                tail = fh.read().decode("utf-8", "replace")
        except OSError:
            return ""
        lines = [ln for ln in tail.splitlines() if ln.startswith("SHEEPIT_PROGRESS:")]
        return lines[-1].split(":", 1)[1].strip() if lines else ""

    def _finish(self, context, cancelled=False):
        wm = context.window_manager
        wm.event_timer_remove(self._timer)
        self._log.close()
        wm.sheepit_running = False
        wm.sheepit_status = ""

        messages, ok = [], False
        if not cancelled and os.path.exists(self.result_path):
            with open(self.result_path, encoding="utf-8") as fh:
                result = json.load(fh)
            messages = [tuple(m) for m in result.get("messages", [])]
            ok = result.get("ok", False) and not any(m[0] == "ERROR" for m in messages)
            if result.get("ok"):
                size = result.get("size", 0)
                wm.sheepit_last_output = self.output
                messages.append(("OK" if ok else "WARNING",
                                 f"Wrote {os.path.basename(self.output)} "
                                 f"({size / 1_000_000:,.0f} MB)."))
        elif not cancelled:
            try:
                with open(self.log_path, encoding="utf-8", errors="replace") as fh:
                    tail = fh.read()[-1500:]
            except OSError:
                tail = ""
            messages.append(("ERROR", "The export process stopped unexpectedly:\n" + tail))

        if not cancelled and not ok and os.path.exists(self.log_path):
            try:
                shutil.copy2(self.log_path, os.path.join(os.path.dirname(self.output),
                                                         "sheepit_export.log"))
            except OSError:
                pass
        shutil.rmtree(self.stage_root, ignore_errors=True)
        if cancelled:
            messages.append(("WARNING", "Export cancelled."))
        set_reports(wm, messages)
        redraw(context)
        if ok:
            self.report({"INFO"}, f"SheepIt zip ready: {self.output}")
        elif not cancelled:
            self.report({"ERROR"}, "SheepIt export finished with errors; see the panel")
        return ok


class SHEEPIT_OT_open_output(bpy.types.Operator):
    bl_idname = "sheepit.open_output"
    bl_label = "Show Zip"
    bl_description = "Open the folder containing the exported zip"

    def execute(self, context):
        path = context.window_manager.sheepit_last_output
        folder = os.path.dirname(path) if path else bpy.path.abspath(
            context.scene.sheepit_export.output_dir)
        bpy.ops.wm.path_open(filepath=folder)
        return {"FINISHED"}


# --------------------------------------------------------------------------
# UI
# --------------------------------------------------------------------------

def ui_scale():
    prefs = bpy.context.preferences.system
    return getattr(prefs, "ui_scale", 1.0) * getattr(prefs, "pixel_size", 1.0)


def wrap_px(text, max_px):
    """Word-wrap text to a pixel width using Blender's UI font metrics."""
    try:
        import blf
        style = bpy.context.preferences.ui_styles[0].widget
        size = getattr(style, "points", 11) * bpy.context.preferences.system.ui_scale
        blf.size(0, size)
        measure = lambda t: blf.dimensions(0, t)[0]  # noqa: E731
    except Exception:
        measure = lambda t: len(t) * 7 * ui_scale()  # noqa: E731
    lines, cur = [], ""
    for word in text.split(" "):
        trial = f"{cur} {word}" if cur else word
        if cur and measure(trial) > max_px:
            lines.append(cur)
            cur = word
        else:
            cur = trial
    if cur:
        lines.append(cur)
    return lines


ICONS = {"ERROR": "CANCEL", "WARNING": "ERROR", "INFO": "INFO", "OK": "CHECKMARK"}


class SHEEPIT_PT_export(bpy.types.Panel):
    bl_label = "SheepIt Export"
    bl_space_type = "PROPERTIES"
    bl_region_type = "WINDOW"
    bl_context = "render"

    def draw(self, context):
        layout = self.layout
        props = context.scene.sheepit_export
        wm = context.window_manager

        layout.use_property_split = True
        layout.use_property_decorate = False
        layout.prop(props, "output_dir")

        col = layout.column(heading="Clean Up")
        col.prop(props, "remove_hidden")
        sub = col.column()
        sub.active = props.remove_hidden
        sub.prop(props, "remove_viewport_hidden")
        if os.environ.get("OCIO"):
            col.prop(props, "include_ocio")

        col = layout.column(heading="File Size")
        col.prop(props, "size_limit_mb")
        col.prop(props, "reduce_size")
        sub = col.column()
        sub.active = props.reduce_size
        sub.prop(props, "min_texture")
        layout.prop(props, "ignore_errors")

        layout.use_property_split = False
        row = layout.row(align=True)
        row.scale_y = 1.4
        row.operator("sheepit.check", icon="VIEWZOOM")
        row.operator("sheepit.export", icon="EXPORT")

        if wm.sheepit_running:
            box = layout.box()
            box.label(text=wm.sheepit_status or "Working...", icon="SORTTIME")
            box.label(text="Press Esc to cancel")

        if len(wm.sheepit_reports):
            box = layout.box()
            # Margins: panel padding, box border and the icon column.
            avail = context.region.width - 64 * ui_scale()
            for item in wm.sheepit_reports:
                col = box.column(align=True)
                lines = []
                for para in item.text.splitlines() or [""]:
                    lines += wrap_px(para, avail) or [""]
                for i, line in enumerate(lines[:12]):
                    col.label(text=line, icon=ICONS.get(item.level, "DOT") if i == 0 else "BLANK1")

        row = layout.row(align=True)
        if wm.sheepit_last_output:
            row.operator("sheepit.open_output", icon="FILE_FOLDER")
        row.operator("wm.url_open", text="Open SheepIt", icon="URL").url = SHEEPIT_URL


# --------------------------------------------------------------------------
# Registration
# --------------------------------------------------------------------------

classes = (SheepItPrefs, SheepItSettings, SheepItReport, SHEEPIT_OT_check,
           SHEEPIT_OT_export, SHEEPIT_OT_open_output, SHEEPIT_PT_export)


def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.Scene.sheepit_export = PointerProperty(type=SheepItSettings)
    wm = bpy.types.WindowManager
    wm.sheepit_reports = CollectionProperty(type=SheepItReport)
    wm.sheepit_running = BoolProperty(default=False)
    wm.sheepit_status = StringProperty()
    wm.sheepit_last_output = StringProperty()


def unregister():
    wm = bpy.types.WindowManager
    for attr in ("sheepit_reports", "sheepit_running", "sheepit_status", "sheepit_last_output"):
        delattr(wm, attr)
    del bpy.types.Scene.sheepit_export
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
