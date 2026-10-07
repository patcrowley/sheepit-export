"""Background export job, run as:

    blender -b --factory-startup <staged.blend> --python worker.py -- <config.json>

The staged .blend is a copy of the user's file saved inside an empty
project folder. This script makes it self-contained, copies every external
file next to it, removes hidden objects, zips the folder and shrinks
textures/videos if the zip is over the size limit.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import traceback
import zipfile

import bpy

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".tga", ".bmp", ".exr",
              ".hdr", ".webp", ".j2c", ".jp2", ".dpx", ".cin", ".sgi", ".rgb"}
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".m4v", ".avi", ".webm"}


def progress(msg):
    print("SHEEPIT_PROGRESS:", msg, flush=True)


class Job:
    def __init__(self, cfg):
        self.cfg = cfg
        self.messages = []
        self.proj = os.path.dirname(bpy.data.filepath)
        self.reducible = {}  # abs_path -> "IMAGE" | "VIDEO"
        self.protected = set()  # files also used where resizing would change the result

    def msg(self, level, text):
        self.messages.append([level, text])

    # ------------------------------------------------------------------ steps

    def make_local(self):
        progress("Making linked data local")
        try:
            bpy.ops.object.make_local(type="ALL")
        except Exception:
            pass
        for idb in list(D.all_ids()):
            try:
                if idb.override_library is not None and idb.library is None:
                    try:
                        idb.make_local(clear_liboverride=True)
                    except TypeError:
                        idb.make_local()
                elif idb.library is not None:
                    idb.make_local()
            except Exception:
                pass
        left = [i.name for i in D.all_ids()
                if i.library is not None or i.override_library is not None]
        if left:
            self.msg("WARNING", f"{len(left)} linked datablocks couldn't be made local "
                                f"(e.g. {', '.join(left[:5])}).")
        for lib in list(bpy.data.libraries):
            if not any(i.library == lib for i in D.all_ids()):
                bpy.data.libraries.remove(lib)

    def remove_hidden(self):
        if not self.cfg["remove_hidden"]:
            return
        progress("Removing hidden objects")
        obs, mods = H.plan(bpy.context.scene, None, self.cfg["remove_viewport_hidden"])
        n_obs, n_mods = H.apply(obs, mods)
        if n_obs or n_mods:
            self.msg("INFO", f"Left out {n_obs} hidden objects and {n_mods} "
                             "render-disabled modifiers.")

    def purge(self):
        progress("Purging unused data")
        try:
            bpy.data.orphans_purge(do_local_ids=True, do_linked_ids=True, do_recursive=True)
        except TypeError:
            bpy.data.orphans_purge()

    def gather(self):
        progress("Collecting external files")
        found = D.collect(self.cfg["original_dir"], self.cfg["include_ocio"])
        for level, text in found.notes:
            self.msg(level, text)

        skip = {os.path.normcase(os.path.normpath(p))
                for p in (self.cfg["out_dir"], self.cfg["stage_root"])}
        file_dirs = {}
        dir_dirs = {}
        assets = os.path.join(self.proj, "assets")
        caches = os.path.join(self.proj, "caches")

        def unique(parent, name):
            name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._") or "files"
            cand, n = os.path.join(parent, name), 1
            while os.path.exists(cand):
                n += 1
                cand = os.path.join(parent, f"{name}_{n}")
            os.makedirs(cand)
            return cand

        def ignore(dirpath, names):
            return [n for n in names
                    if os.path.normcase(os.path.normpath(os.path.join(dirpath, n))) in skip]

        total = len(found.deps)
        for i, d in enumerate(found.deps, 1):
            progress(f"Copying {i}/{total}: {d.label}")
            if d.missing:
                self.msg("WARNING", f"Missing, not included: {d.label} ({d.primary})")
                continue
            if d.setter is not None and not d.editable:
                self.msg("WARNING", f"{d.label} is linked from a library and couldn't be "
                                    "relinked; it may render without it.")
                continue

            if d.kind == D.FILES:
                src_dir = os.path.dirname(d.files[0])
                dest = file_dirs.get(src_dir)
                if dest is None:
                    dest = file_dirs[src_dir] = unique(assets, os.path.basename(src_dir))
                for f in d.files:
                    target = os.path.join(dest, os.path.basename(f))
                    if not os.path.exists(target):
                        shutil.copy2(f, target)
                    if d.reduce:
                        self.reducible[target] = d.reduce
                    else:
                        self.protected.add(target)
                if d.setter:
                    d.setter(os.path.join(dest, os.path.basename(d.primary)))
            else:
                src = os.path.normpath(d.primary)
                if d.dest:
                    dest = os.path.join(self.proj, d.dest)
                else:
                    dest = dir_dirs.get(src)
                    if dest is None:
                        dest = unique(caches, os.path.basename(src))
                        dir_dirs[src] = dest
                shutil.copytree(src, dest, ignore=ignore, dirs_exist_ok=True)
                if d.dest == "OCIO":
                    cfg = d.extra.get("config", "")
                    target = os.path.join(dest, "config.ocio")
                    if cfg and not os.path.exists(target):
                        shutil.copy2(cfg, target)
                if d.setter:
                    d.setter(dest)

        # Anything still pointing outside the project won't exist on SheepIt.
        proj = os.path.normcase(self.proj)
        outside = []
        try:
            paths = bpy.utils.blend_paths(absolute=True, packed=False, local=True)
        except TypeError:
            paths = bpy.utils.blend_paths(absolute=True)
        for p in paths:
            np = os.path.normcase(os.path.normpath(p))
            if not np.startswith(proj) and not p.startswith("<"):
                outside.append(p)
        if outside:
            shown = ", ".join(sorted(set(outside))[:8])
            self.msg("INFO", f"{len(set(outside))} paths still point outside the export "
                             f"(usually sounds, which aren't needed for rendering): {shown}")

    def save(self):
        progress("Saving the .blend")
        bpy.context.preferences.filepaths.save_version = 0
        try:
            bpy.ops.file.make_paths_relative()
        except Exception:
            pass
        bpy.ops.wm.save_as_mainfile(filepath=bpy.data.filepath, compress=True)
        for f in os.listdir(self.proj):
            if re.search(r"\.blend\d+$", f):
                os.remove(os.path.join(self.proj, f))

    def zip(self):
        out = self.cfg["output"]
        progress("Zipping")
        tmp = out + ".partial"
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6,
                             allowZip64=True) as zf:
            for root, _dirs, files in os.walk(self.proj):
                for f in sorted(files):
                    full = os.path.join(root, f)
                    zf.write(full, os.path.relpath(full, self.proj).replace(os.sep, "/"))
        os.replace(tmp, out)
        return os.path.getsize(out)

    # --------------------------------------------------------- size reduction

    def shrink_images(self, max_dim):
        changed = 0
        files = [p for p, kind in self.reducible.items()
                 if kind == "IMAGE" and p not in self.protected and os.path.splitext(p)[1].lower() in IMAGE_EXTS]
        for n, path in enumerate(files, 1):
            if n % 10 == 1:
                progress(f"Downscaling textures to {max_dim}px ({n}/{len(files)})")
            try:
                img = bpy.data.images.load(path, check_existing=False)
            except Exception:
                continue
            try:
                w, h = img.size
                if max(w, h) <= max_dim or w == 0 or h == 0:
                    continue
                s = max_dim / max(w, h)
                img.scale(max(1, round(w * s)), max(1, round(h * s)))
                img.filepath_raw = path
                img.save()
                changed += 1
            except Exception as e:
                self.msg("WARNING", f"Couldn't downscale {os.path.basename(path)}: {e}")
            finally:
                bpy.data.images.remove(img)
        return changed

    def shrink_videos(self, max_dim):
        files = [p for p, kind in self.reducible.items()
                 if kind == "VIDEO" and p not in self.protected and os.path.splitext(p)[1].lower() in VIDEO_EXTS]
        if not files:
            return 0
        ffmpeg = self.cfg.get("ffmpeg") or shutil.which("ffmpeg")
        if not ffmpeg:
            if not getattr(self, "_warned_ffmpeg", False):
                self._warned_ffmpeg = True
                self.msg("WARNING", "Video textures can't be shrunk because ffmpeg wasn't "
                                    "found. Set its path in the add-on preferences.")
            return 0
        changed = 0
        for path in files:
            dims = probe_size(ffmpeg, path)
            if dims and max(dims) <= max_dim:
                continue
            progress(f"Re-encoding {os.path.basename(path)} to {max_dim}px")
            ext = os.path.splitext(path)[1].lower()
            tmp = path + ".shrink" + ext
            m = max_dim
            vf = (f"scale=w='if(gte(iw,ih),trunc(min(iw,{m})/2)*2,-2)'"
                  f":h='if(gte(iw,ih),-2,trunc(min(ih,{m})/2)*2)'")
            codec = (["-c:v", "libvpx-vp9", "-crf", "34", "-b:v", "0"] if ext == ".webm"
                     else ["-c:v", "libx264", "-crf", "20", "-preset", "medium",
                           "-pix_fmt", "yuv420p"])
            cmd = [ffmpeg, "-y", "-loglevel", "error", "-i", path, "-vf", vf, *codec,
                   "-an", tmp]
            try:
                subprocess.run(cmd, check=True, capture_output=True, **no_window())
                if os.path.getsize(tmp) < os.path.getsize(path):
                    os.replace(tmp, path)
                    changed += 1
                else:
                    os.remove(tmp)
            except Exception as e:
                if os.path.exists(tmp):
                    os.remove(tmp)
                self.msg("WARNING", f"Couldn't re-encode {os.path.basename(path)}: {e}")
        return changed

    def reduce(self, size):
        limit = self.cfg["limit_bytes"]
        if size <= limit or not self.cfg["reduce"]:
            return size
        steps = [d for d in (4096, 2048, 1024, 512, 256) if d >= self.cfg["min_texture"]]
        for max_dim in steps:
            changed = self.shrink_images(max_dim) + self.shrink_videos(max_dim)
            if not changed:
                continue
            size = self.zip()
            self.msg("INFO", f"Shrunk {changed} textures/videos to max {max_dim}px: "
                             f"zip is now {mb(size)}.")
            if size <= limit:
                break
        return size

    def biggest_items(self, n=6):
        sizes = []
        for root, _dirs, files in os.walk(self.proj):
            for f in files:
                p = os.path.join(root, f)
                sizes.append((os.path.getsize(p), os.path.relpath(p, self.proj)))
        # Roll up cache folders so the list is readable.
        rolled = {}
        for s, rel in sizes:
            parts = rel.replace(os.sep, "/").split("/")
            key = "/".join(parts[:2]) if len(parts) > 2 else rel
            rolled[key] = rolled.get(key, 0) + s
        top = sorted(rolled.items(), key=lambda kv: -kv[1])[:n]
        return ", ".join(f"{k} ({mb(v)})" for k, v in top)

    # ------------------------------------------------------------------- main

    def run(self):
        self.make_local()
        self.remove_hidden()
        self.purge()
        self.gather()
        self.save()
        size = self.zip()
        size = self.reduce(size)
        limit = self.cfg["limit_bytes"]
        if size > limit:
            self.msg("ERROR", f"The zip is {mb(size)}, over the {mb(limit)} limit. "
                              f"Largest items: {self.biggest_items()}. Lower the minimum "
                              "texture size, or split long simulation caches into several "
                              "projects (see the SheepIt FAQ on fluids).")
        return size


def probe_size(ffmpeg, path):
    probe = os.path.join(os.path.dirname(ffmpeg), "ffprobe" + (".exe" if os.name == "nt" else ""))
    probe = probe if os.path.isfile(probe) else shutil.which("ffprobe")
    if not probe:
        return None
    try:
        r = subprocess.run([probe, "-v", "error", "-select_streams", "v:0", "-show_entries",
                            "stream=width,height", "-of", "csv=p=0", path],
                           capture_output=True, text=True, check=True, **no_window())
        w, h = r.stdout.strip().split(",")[:2]
        return int(w), int(h)
    except Exception:
        return None


def no_window():
    flag = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return {"creationflags": flag} if flag else {}


def mb(n):
    return f"{n / 1_000_000:,.0f} MB"


def main():
    cfg_path = sys.argv[sys.argv.index("--") + 1]
    with open(cfg_path, encoding="utf-8") as fh:
        cfg = json.load(fh)
    sys.path.insert(0, cfg["addon_dir"])
    global D, H
    import sheepit_deps as D  # noqa: E402
    import sheepit_hidden as H  # noqa: E402

    job = Job(cfg)
    result = {"ok": False, "size": 0, "output": cfg["output"]}
    try:
        result["size"] = job.run()
        result["ok"] = True
    except Exception:
        job.msg("ERROR", "Export failed:\n" + traceback.format_exc())
    result["messages"] = job.messages
    with open(cfg["result_path"], "w", encoding="utf-8") as fh:
        json.dump(result, fh)


D = H = None

if __name__ == "__main__":
    main()
