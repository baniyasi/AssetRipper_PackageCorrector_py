#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AssetRipper_SpriteSheet_Fix.py

Repairs sprite sheets flattened by AssetRipper. Rules:

1. For each usable container (>=2 sprites), find all Texture2D named as the
   container's last segment.
2. Pick the main texture:
   - Only textures whose dimensions can contain every sprite rect are eligible.
   - If multiple with different dimensions: pick the smallest one.
   - If multiple with identical dimensions: SKIP container (ambiguous).
   - If none: SKIP.
3. Classify sprites:
   - Sprite.name == last_segment AND <container_dir>/<last_segment>.asset exists
     → skip (independent .asset already covers it)
   - Otherwise → write into the sprite sheet meta
4. If fewer than 2 sprites remain, or duplicate names exist → SKIP.
5. If the on-disk <last_segment>.png size differs from the main texture size,
   look for a sibling <stem>_N.png matching the size. If found and not
   referenced externally, use its pixels. Otherwise SKIP.
6. Write spriteMode=2 meta with the sprites to write.

No backup files are created.

Requires: Unity 2017.1+ | Python 3.8+ | pip install UnityPy Pillow
"""

import os
import re
import hashlib
import shutil
import tkinter as tk
from tkinter import filedialog, messagebox
from collections import defaultdict

import UnityPy
from PIL import Image


# ------------------------------------------------------------------
# UI
# ------------------------------------------------------------------

def select_folder(title):
    root = tk.Tk()
    root.withdraw()
    root.attributes('-topmost', True)
    p = filedialog.askdirectory(title=title, initialdir=os.getcwd())
    root.destroy()
    return p


def confirm(title, msg):
    root = tk.Tk()
    root.withdraw()
    root.attributes('-topmost', True)
    ok = messagebox.askyesno(title, msg)
    root.destroy()
    return ok


def show_info(title, msg):
    root = tk.Tk()
    root.withdraw()
    root.attributes('-topmost', True)
    messagebox.showinfo(title, msg)
    root.destroy()


# ------------------------------------------------------------------
# Paths / UnityPy helpers
# ------------------------------------------------------------------

def find_data_dir(p):
    if os.path.basename(p).endswith('_Data'):
        return p
    for name in os.listdir(p):
        f = os.path.join(p, name)
        if os.path.isdir(f) and name.endswith('_Data'):
            return f
    return p


def find_project_root(p):
    if os.path.basename(p) == 'ExportedProject':
        return p
    nested = os.path.join(p, 'ExportedProject')
    if os.path.isdir(nested):
        return nested
    return p


def af_name_of(obj):
    af = getattr(obj, 'assets_file', None)
    return os.path.basename(str(getattr(af, 'name', '') or '')).lower() if af else ''


def extract_ext_name(e):
    if hasattr(e, 'path'):
        return os.path.basename(str(e.path)).lower()
    m = re.search(r'\(([^)]+)\)', str(e))
    return os.path.basename(m.group(1)).lower() if m else os.path.basename(str(e)).lower()


def build_ext_map(env):
    out = {}
    for o in env.objects:
        af = getattr(o, 'assets_file', None)
        if af is None:
            continue
        n = os.path.basename(str(getattr(af, 'name', '') or '')).lower()
        if not n or n in out:
            continue
        out[n] = [extract_ext_name(e) for e in (getattr(af, 'externals', None) or [])]
    return out


def resolve(src, fid, pid, ext):
    if fid == 0:
        return (src, pid)
    exts = ext.get(src, [])
    return (exts[fid - 1], pid) if 0 <= fid - 1 < len(exts) else (src, pid)


def safe_pptr(o):
    if not isinstance(o, dict):
        return (0, 0)
    return (int(o.get('m_FileID', 0)), int(o.get('m_PathID', 0)))


def iter_container(c):
    if not c:
        return
    for e in c:
        if isinstance(e, tuple) and len(e) == 2:
            yield e[0], e[1]
        elif isinstance(e, dict):
            yield e.get('first', ''), e.get('second')


def find_file_ci(folder, target):
    if not os.path.isdir(folder):
        return None
    tl = target.lower()
    for f in os.listdir(folder):
        if f.lower() == tl:
            return os.path.join(folder, f)
    return None


# ------------------------------------------------------------------
# Sprite classification
# ------------------------------------------------------------------

def sprite_fits(sprite, w, h):
    x, y, sw, sh = sprite['rect']
    if sw <= 0 or sh <= 0 or x < 0 or y < 0:
        return False
    return x + sw <= w and y + sh <= h


def pick_main_texture(named_texs, sprites):
    """
    Returns (main_tex, error). error is a string when skip is needed.
    """
    fitting = [t for t in named_texs if all(sprite_fits(s, t['width'], t['height']) for s in sprites)]
    if not fitting:
        return None, "no texture can contain all sprites"
    if len(fitting) == 1:
        return fitting[0], None
    # Multiple textures can contain all sprites
    sizes = set((t['width'], t['height']) for t in fitting)
    if len(sizes) == 1:
        # Ambiguous — multiple textures with same size
        pids = [t['path_id'] for t in fitting]
        return None, f"ambiguous: multiple textures with same size {list(sizes)[0]}, PathIDs={pids}"
    # Different sizes: pick smallest by area
    fitting.sort(key=lambda t: t['width'] * t['height'])
    return fitting[0], None


# ------------------------------------------------------------------
# Meta writing
# ------------------------------------------------------------------

def gen_sprite_id(texture_guid, name, rect, border):
    key = (f"{texture_guid}:{name}:{rect[0]}:{rect[1]}:{rect[2]}:{rect[3]}:"
           f"{border[0]}:{border[1]}:{border[2]}:{border[3]}")
    return hashlib.md5(key.encode('utf-8')).hexdigest()[:16] + "0800000000000000"


def build_sprite_yaml(sprite, iid, guid):
    r = sprite['rect']
    b = sprite.get('border', (0, 0, 0, 0))
    p = sprite.get('pivot', (0.5, 0.5))
    a = sprite.get('alignment', 0)
    sid = gen_sprite_id(guid, sprite['name'], r, b)
    return [
        "    - serializedVersion: 2",
        f"      name: {sprite['name']}",
        "      rect:",
        "        serializedVersion: 2",
        f"        x: {r[0]}",
        f"        y: {r[1]}",
        f"        width: {r[2]}",
        f"        height: {r[3]}",
        f"      alignment: {a}",
        f"      pivot: {{x: {p[0]}, y: {p[1]}}}",
        f"      border: {{x: {b[0]}, y: {b[1]}, z: {b[2]}, w: {b[3]}}}",
        "      outline: []",
        "      physicsShape: []",
        "      tessellationDetail: 0",
        "      bones: []",
        f"      spriteID: {sid}",
        f"      internalID: {iid}",
        "      vertices: []",
        "      indices: ",
        "      edges: []",
        "      weights: []",
    ]


def patch_meta(meta, sprites, guid):
    sprite_lines = []
    name_lines = []
    for idx, s in enumerate(sprites):
        iid = 21300000 + idx
        sprite_lines.extend(build_sprite_yaml(s, iid, guid))
        name_lines.append(f"      {s['name']}: {iid}")

    result = meta
    result = re.sub(r'^  spriteMode:\s*\d+\s*$', '  spriteMode: 2',
                    result, count=1, flags=re.MULTILINE)
    result = re.sub(r'^  textureType:\s*\d+\s*$', '  textureType: 8',
                    result, count=1, flags=re.MULTILINE)

    sprites_block = '    sprites:\n' + '\n'.join(sprite_lines)
    if re.search(r'^    sprites:', result, re.MULTILINE):
        result = re.sub(r'^    sprites:.*?(?=^  \S|\Z)', sprites_block + '\n',
                        result, count=1, flags=re.MULTILINE | re.DOTALL)
    elif '    sprites: []' in result:
        result = result.replace('    sprites: []', sprites_block, 1)

    names_block = '    nameFileIdTable:\n' + '\n'.join(name_lines)
    if re.search(r'^    nameFileIdTable:', result, re.MULTILINE):
        result = re.sub(r'^    nameFileIdTable:.*?(?=^  \S|\Z)', names_block + '\n',
                        result, count=1, flags=re.MULTILINE | re.DOTALL)
    else:
        result = re.sub(r'(^  spriteSheet:.*?)(?=^  \S|\Z)',
                        lambda m: m.group(1).rstrip() + '\n' + names_block + '\n',
                        result, count=1, flags=re.MULTILINE | re.DOTALL)
    return result


def read_guid(meta_path):
    if not os.path.isfile(meta_path):
        return None
    try:
        with open(meta_path, 'r', encoding='utf-8', errors='ignore') as f:
            m = re.search(r'^guid:\s*([0-9a-fA-F]{32})', f.read(2048), re.MULTILINE)
        return m.group(1).lower() if m else None
    except Exception:
        return None


# ------------------------------------------------------------------
# GUID reference scan
# ------------------------------------------------------------------

def scan_guid_refs(project_root):
    counts = defaultdict(int)
    assets_dir = os.path.join(project_root, 'Assets')
    if not os.path.isdir(assets_dir):
        return counts
    guid_re = re.compile(r'guid:\s*([0-9a-fA-F]{32})')
    scan_exts = ('.unity', '.prefab', '.asset', '.mat', '.anim', '.controller',
                 '.overrideController', '.spriteatlas', '.playable', '.mask')
    for dp, _, files in os.walk(assets_dir):
        for f in files:
            if not f.lower().endswith(scan_exts):
                continue
            p = os.path.join(dp, f)
            try:
                with open(p, 'r', encoding='utf-8', errors='ignore') as fh:
                    for line in fh:
                        for m in guid_re.finditer(line):
                            counts[m.group(1).lower()] += 1
            except Exception:
                pass
    return counts


# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------

def main():
    game = select_folder("Step 1/3: Select game folder (contains *_Data)")
    if not game:
        return
    data_dir = find_data_dir(game)
    print(f"_Data: {data_dir}\n")

    print("[1/4] Loading game data ...")
    try:
        env = UnityPy.load(data_dir)
    except Exception as e:
        show_info("Load error", str(e))
        return
    print(f"  objects: {len(env.objects)}\n")

    print("[2/4] Extracting ...")
    ext_map = build_ext_map(env)
    textures = {}
    sprites = {}
    containers = defaultdict(set)
    for o in env.objects:
        tn = o.type.name
        src = af_name_of(o)
        if tn == 'Texture2D':
            try:
                d = o.read()
                textures[(src, o.path_id)] = {
                    'name': getattr(d, 'm_Name', '') or '',
                    'width': int(getattr(d, 'm_Width', 0)),
                    'height': int(getattr(d, 'm_Height', 0)),
                }
            except Exception:
                pass
        elif tn == 'Sprite':
            try:
                tt = o.read_typetree()
                if not isinstance(tt, dict):
                    continue
                r = tt.get('m_Rect') or {}
                b = tt.get('m_Border') or {}
                p = tt.get('m_Pivot') or {}
                sprites[(src, o.path_id)] = {
                    'name': tt.get('m_Name', '') or '',
                    'rect': (int(r.get('x', 0)), int(r.get('y', 0)),
                             int(r.get('width', 0)), int(r.get('height', 0))),
                    'border': (int(b.get('x', 0)), int(b.get('y', 0)),
                               int(b.get('z', 0)), int(b.get('w', 0))),
                    'pivot': (float(p.get('x', 0.5)), float(p.get('y', 0.5))),
                    'alignment': int(tt.get('m_Alignment', 0)),
                }
            except Exception:
                pass
        elif tn in ('AssetBundle', 'ResourceManager'):
            try:
                tt = o.read_typetree()
            except Exception:
                continue
            for key, val in iter_container(tt.get('m_Container')):
                fid, pid = safe_pptr(val)
                if pid:
                    containers[key].add((src, fid, pid))

    usable = {}
    for path, entries in containers.items():
        texs, sprs = [], []
        seen_tex, seen_spr = set(), set()
        for src, fid, pid in entries:
            tgt = resolve(src, fid, pid, ext_map)
            if tgt in textures and tgt not in seen_tex:
                seen_tex.add(tgt)
                info = dict(textures[tgt], file=tgt[0], path_id=tgt[1])
                texs.append(info)
            elif tgt in sprites and tgt not in seen_spr:
                seen_spr.add(tgt)
                sprs.append(sprites[tgt])
        if texs and len(sprs) >= 2:
            usable[path] = {'textures': texs, 'sprites': sprs}

    print(f"  usable containers: {len(usable)}\n")

    proj = select_folder("Step 2/3: Select AssetRipper parent folder")
    if not proj:
        return
    root = find_project_root(proj)
    print(f"  project root: {root}\n")
    res_dir = os.path.join(root, 'Assets', 'Resources')

    print("[3/4] Scanning project GUID references ...")
    ref_counts = scan_guid_refs(root)
    print(f"  referenced GUIDs: {len(ref_counts)}\n")

    print("[4/4] Repairing ...\n")

    fixed, skipped = [], []

    for path in sorted(usable.keys()):
        last = path.split('/')[-1].lower()
        container_dir = os.path.join(res_dir, os.path.dirname(path))
        sprs = usable[path]['sprites']
        texs_named = [t for t in usable[path]['textures'] if t['name'].lower() == last]

        if not texs_named:
            skipped.append((path, "no texture name matches last segment"))
            continue

        # Deduplicate sprite names
        names_set = set(s['name'] for s in sprs)
        if len(names_set) != len(sprs):
            skipped.append((path, "duplicate sprite names in container"))
            continue

        # Classify
        eq_asset = find_file_ci(container_dir, last + '.asset')
        to_write, to_skip = [], []
        for s in sprs:
            if s['name'].lower() == last and eq_asset:
                to_skip.append(s)
            else:
                to_write.append(s)

        if len(to_write) < 2:
            skipped.append((path, f"only {len(to_write)} sprites to write "
                                 f"(skipped {len(to_skip)})"))
            continue

        # Main texture
        main_tex, err = pick_main_texture(texs_named, to_write)
        if main_tex is None:
            skipped.append((path, err))
            continue

        # Locate disk png/meta
        disk_png = find_file_ci(container_dir, last + '.png')
        if not disk_png:
            skipped.append((path, f"no {last}.png on disk"))
            continue
        disk_meta = disk_png + '.meta'
        if not os.path.isfile(disk_meta):
            skipped.append((path, "no .meta"))
            continue
        texture_guid = read_guid(disk_meta)
        if not texture_guid:
            skipped.append((path, "meta has no guid"))
            continue

        try:
            with Image.open(disk_png) as im:
                cur_w, cur_h = im.size
        except Exception:
            skipped.append((path, "cannot read png"))
            continue

        # Pixel source
        source_png = disk_png
        delete_after = []
        if (cur_w, cur_h) != (main_tex['width'], main_tex['height']):
            sibling = None
            stem = os.path.splitext(os.path.basename(disk_png))[0]
            for f in os.listdir(container_dir):
                if not f.lower().endswith('.png'):
                    continue
                b = f[:-4]
                if not re.match(r'^' + re.escape(stem) + r'_\d+$', b, re.IGNORECASE):
                    continue
                full = os.path.join(container_dir, f)
                try:
                    with Image.open(full) as im:
                        w, h = im.size
                except Exception:
                    continue
                if (w, h) == (main_tex['width'], main_tex['height']):
                    sibling = full
                    break
            if not sibling:
                skipped.append((path, f"disk {cur_w}x{cur_h} != tex "
                                     f"{main_tex['width']}x{main_tex['height']}, no sibling"))
                continue
            sg = read_guid(sibling + '.meta') if os.path.isfile(sibling + '.meta') else None
            if sg and ref_counts.get(sg, 0) > 0:
                skipped.append((path, "sibling has external references"))
                continue
            source_png = sibling
            delete_after.append(sibling)

        # Check if already correct (idempotency)
        try:
            with open(disk_meta, 'r', encoding='utf-8', errors='ignore') as f:
                mc = f.read()
        except Exception:
            skipped.append((path, "cannot read meta"))
            continue

        current_multi = bool(re.search(r'^  spriteMode:\s*2\s*$', mc, re.MULTILINE))
        existing_names = set()
        in_sp = False
        for line in mc.split('\n'):
            if re.match(r'^    sprites:\s*$', line):
                in_sp = True
                continue
            if in_sp:
                if re.match(r'^    nameFileIdTable:', line) or \
                   (re.match(r'^  \S', line) and 'sprites' not in line):
                    in_sp = False
                    continue
                m = re.match(r'^\s+name:\s*(.+)$', line)
                if m and m.group(1).strip():
                    existing_names.add(m.group(1).strip())

        target_names = set(s['name'] for s in to_write)
        if current_multi and existing_names == target_names and source_png == disk_png:
            skipped.append((path, "already correct"))
            continue

        # Confirmation dialog
        lines = [
            f"Container: {path}",
            f"Main texture: {main_tex['name']} ({main_tex['width']}x{main_tex['height']})",
            f"Write into meta: {len(to_write)} sprites",
        ]
        for s in sorted(to_write, key=lambda x: x['name']):
            lines.append(f"  + {s['name']}")
        if to_skip:
            lines.append(f"Skip (covered by .asset): {len(to_skip)}")
            for s in to_skip:
                lines.append(f"  - {s['name']}")
        if source_png != disk_png:
            lines.append(f"Pixel source: {os.path.relpath(source_png, root)}")
            lines.append(f"  (will overwrite {os.path.basename(disk_png)} "
                         f"and delete the source)")
        lines.append("")
        lines.append("Apply?")

        if not confirm("Confirm", "\n".join(lines)):
            skipped.append((path, "user skipped"))
            continue

        if source_png != disk_png:
            shutil.copy2(source_png, disk_png)

        new_meta = patch_meta(mc, sorted(to_write, key=lambda x: x['name']), texture_guid)
        with open(disk_meta, 'w', encoding='utf-8') as f:
            f.write(new_meta)
        print(f"  Fixed: {path}  ({len(to_write)} sprites, {len(to_skip)} skipped)")

        fixed.append({
            'container': path,
            'meta_path': disk_meta,
            'png_path': disk_png,
            'delete_after': delete_after,
            'slice_names': target_names,
        })

    # Summary
    print("\n" + "=" * 78)
    print(f"Fixed: {len(fixed)}   Skipped: {len(skipped)}")
    print("=" * 78)
    if fixed:
        print("\nFixed:")
        for f in fixed:
            print(f"  + {f['container']}  ({len(f['slice_names'])} sprites)")
    if skipped:
        print("\nSkipped:")
        for p, why in skipped:
            print(f"  - {p}  ({why})")

    # Cleanup
    if fixed and confirm("Cleanup",
                         f"{len(fixed)} repaired.\n\n"
                         "Delete orphaned files (replaced sibling pngs, sactx textures, "
                         "standalone sprite .asset whose names match repaired slices, "
                         "leftover .assetripper_bak)? This is irreversible."):
        perform_cleanup(fixed, root, ref_counts)

    show_info("Done", "Reimport the project in Unity.")


def perform_cleanup(fixed, root, ref_counts):
    print("\n" + "=" * 78)
    print("Orphan cleanup")
    print("=" * 78)

    res_dir = os.path.join(root, 'Assets', 'Resources')
    ref_counts = scan_guid_refs(root)  # refresh after meta writes
    to_delete = []

    for f in fixed:
        for p in f.get('delete_after', []):
            if os.path.isfile(p) and os.path.isfile(p + '.meta'):
                g = read_guid(p + '.meta')
                if g and ref_counts.get(g, 0) == 0:
                    to_delete.append(p)

    for dp, _, files in os.walk(res_dir):
        for name in files:
            if not name.lower().startswith('sactx-') or not name.lower().endswith('.png'):
                continue
            full = os.path.join(dp, name)
            meta = full + '.meta'
            if not os.path.isfile(meta):
                continue
            g = read_guid(meta)
            if g and ref_counts.get(g, 0) == 0:
                to_delete.append(full)

    all_slice_names = set()
    for f in fixed:
        all_slice_names.update(f.get('slice_names', set()))

    for dp, _, files in os.walk(res_dir):
        for name in files:
            if not name.lower().endswith('.asset'):
                continue
            stem = name[:-6].lower()
            if stem not in all_slice_names:
                continue
            full = os.path.join(dp, name)
            meta = full + '.meta'
            if not os.path.isfile(meta):
                continue
            g = read_guid(meta)
            if g and ref_counts.get(g, 0) == 0:
                to_delete.append(full)

    for dp, _, files in os.walk(root):
        for name in files:
            if name.endswith('.assetripper_bak'):
                to_delete.append(os.path.join(dp, name))

    if not to_delete:
        print("No orphans.")
        show_info("Cleanup", "No orphaned files.")
        return

    print(f"\nOrphans ({len(to_delete)}):")
    for p in to_delete:
        print(f"  {os.path.relpath(p, root)}")

    if not confirm("Confirm", f"Delete {len(to_delete)} files?"):
        return

    removed = 0
    for p in to_delete:
        try:
            os.remove(p)
            m = p + '.meta'
            if os.path.isfile(m):
                os.remove(m)
            removed += 1
        except Exception as e:
            print(f"  Failed: {p}  ({e})")
    print(f"\nRemoved {removed}")
    show_info("Cleanup", f"Removed {removed} files")


if __name__ == '__main__':
    main()