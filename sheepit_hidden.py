"""Work out which objects are hidden from the render and safe to delete.

An object is only removed when nothing that still renders depends on it:
boolean cutters, instanced collections, particle instances, constraint or
modifier targets, parents, driver targets and rigid-body members are kept.
No relative imports: the background worker imports this module directly.
"""

import bpy


def render_visible_objects(scene, view_layer):
    """Objects that end up in the final render of this view layer."""
    visible = set()

    def walk(lc, hidden):
        coll = lc.collection
        hidden = hidden or lc.exclude or getattr(coll, "hide_render", False)
        if not hidden:
            for ob in coll.objects:
                if not ob.hide_render:
                    visible.add(ob)
        for child in lc.children:
            walk(child, hidden)

    walk(view_layer.layer_collection, False)

    # Objects inside instanced collections render too, even when their own
    # collection is excluded from the view layer.
    def walk_collection(coll, seen):
        if coll is None or coll.as_pointer() in seen or coll.hide_render:
            return
        seen.add(coll.as_pointer())
        for ob in coll.objects:
            if not ob.hide_render and ob not in visible:
                visible.add(ob)
                queue.append(ob)
        for child in coll.children:
            walk_collection(child, seen)

    queue = list(visible)
    seen = set()
    while queue:
        ob = queue.pop()
        if ob.instance_type == "COLLECTION":
            walk_collection(ob.instance_collection, seen)
    return visible


def plan(scene=None, view_layer=None, include_viewport_hidden=False):
    """Return (objects_to_remove, modifiers_to_remove) for the given scene.

    modifiers_to_remove is a list of (object, modifier_name) that are switched
    off for rendering on objects that are kept.
    """
    scene = scene or bpy.context.scene
    if view_layer is None:
        view_layer = bpy.context.view_layer
        if view_layer is None or bpy.context.scene != scene:
            view_layer = scene.view_layers[0]

    # Every view layer that renders contributes visible objects.
    layers = [vl for vl in scene.view_layers if vl.use] or [view_layer]
    visible = set()
    for vl in layers:
        visible |= render_visible_objects(scene, vl)
    if include_viewport_hidden:
        visible = {ob for ob in visible if _viewport_visible(ob, view_layer)}

    # Always keep cameras the scene switches to, and rigid-body members
    # (removing them would scramble a baked rigid-body cache).
    seed = set(visible)
    if scene.camera is not None:
        seed.add(scene.camera)
    for m in scene.timeline_markers:
        if m.camera is not None:
            seed.add(m.camera)
    rbw = scene.rigidbody_world
    if rbw is not None:
        for coll in (rbw.collection, getattr(rbw, "constraints", None)):
            if coll is not None:
                seed.update(coll.all_objects)

    # Objects shared with other scenes are left alone.
    other = set()
    for sc in bpy.data.scenes:
        if sc != scene:
            other.update(sc.objects)

    candidates = {ob for ob in scene.objects if ob not in seed and ob not in other
                  and ob.library is None}
    if not candidates:
        return [], _disabled_modifiers(seed | set(scene.objects))

    umap = bpy.data.user_map(subset=list(candidates) + list(bpy.data.collections))
    keep = set()
    removable = lambda o: o in candidates and o not in keep  # noqa: E731

    def collection_needed(coll, stack):
        if coll.as_pointer() in stack:
            return False
        stack = stack | {coll.as_pointer()}
        for u in umap.get(coll, ()):
            if isinstance(u, bpy.types.Scene):
                continue  # just linked into the scene tree
            if isinstance(u, bpy.types.Collection):
                if collection_needed(u, stack):
                    return True
            elif isinstance(u, bpy.types.Object):
                if not removable(u):
                    return True  # instanced by something that stays
            else:
                return True  # particle settings, node trees, etc.
        return False

    changed = True
    while changed:
        changed = False
        for ob in list(candidates - keep):
            for u in umap.get(ob, ()):
                if isinstance(u, bpy.types.Scene):
                    continue
                if isinstance(u, bpy.types.Collection):
                    needed = collection_needed(u, set())
                elif isinstance(u, bpy.types.Object):
                    needed = not removable(u)
                else:
                    needed = True
                if needed:
                    keep.add(ob)
                    changed = True
                    break

    to_remove = sorted(candidates - keep, key=lambda o: o.name)
    kept = set(scene.objects) - set(to_remove)
    return to_remove, _disabled_modifiers(kept)


def _viewport_visible(ob, view_layer):
    try:
        return ob.visible_get(view_layer=view_layer)
    except Exception:
        return not ob.hide_get() and not ob.hide_viewport


def _disabled_modifiers(objects):
    out = []
    for ob in objects:
        if ob.library is not None:
            continue
        for mod in ob.modifiers:
            if not mod.show_render:
                out.append((ob, mod.name))
    return out


def apply(objects, modifiers):
    """Delete the planned objects and render-disabled modifiers."""
    removed_mods = 0
    for ob, name in modifiers:
        mod = ob.modifiers.get(name)
        if mod is not None:
            ob.modifiers.remove(mod)
            removed_mods += 1
    removed_obs = 0
    for ob in objects:
        bpy.data.objects.remove(ob, do_unlink=True)
        removed_obs += 1
    return removed_obs, removed_mods
