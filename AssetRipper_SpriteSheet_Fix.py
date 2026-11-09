#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fix_sprite_sheets.py

Repairs Sprite Sheets flattened by AssetRipper.
- Reads Container from AssetBundle / ResourceManager (globalgamemanagers)
- Resolves cross-file PPtr references via externals
- Picks the main Texture2D and collects all its sprites
- Writes spriteMode=2 meta with spriteSheet.sprites and nameFileIdTable
- Optionally removes orphaned sactx / standalone sprite .asset files
  when no scene, prefab, or asset references them

Unity: 2017.1+ (SpriteAtlas required)
Python: 3.8+
"""

import os
import re
import hashlib
import tkinter as tk
from tkinter import filedialog, messagebox
from collections import defaultdict
import UnityPy


# ============================================================
# UI helpers
# ============================================================

def select_folder(title):
    root = tk.Tk()
    root.withdraw()
    root.attributes('-topmost', True)
    folder = filedialog.askdirectory(title=title, initialdir=os.getcwd())
    root.destroy()
    return folder


def ask_yes_no(title, message):
    root = tk.Tk()
    root.withdraw()
    root.attributes('-topmost', True)
    ok = messagebox.askyesno(title, message)
    root.destroy()
    return ok


def find_data_dir(picked):
    if os.path.basename(picked).endswith('_Data'):
        return picked
    for name in os.listdir(picked):
        p = os.path.join(picked, name)
        if os.path.isdir(p) and name.endswith('_Data'):
            return p
    return picked


# ============================================================
# Low-level helpers
# ============================================================

def af_name_of(obj):
    af = getattr(obj, 'assets_file', None)
    if af is None:
        return ''
    n = getattr(af, 'name', '') or ''
    return os.path.basename(str(n)).lower()


def extract_ext_name(e):
    if hasattr(e, 'path'):
        return os.path.basename(str(e.path)).lower()
    s = str(e)
    m = re.search(r'\(([^)]+)\)', s)
    if m:
        return os.path.basename(m.group(1)).lower()
    return os.path.basename(s).lower()


def build_externals_map(env):
    ext_map = {}
    for obj in env.objects:
        af = getattr(obj, 'assets_file', None)
        if af is None:
            continue
        name = os.path.basename(str(getattr(af, 'name', '') or '')).lower()
        if not name or name in ext_map:
            continue
        ext_map[name] = [extract_ext_name(e) for e in (getattr(af, 'externals', None) or [])]
    return ext_map


def resolve_pptr(src_file, fid, pid, ext_map):
    if fid == 0:
        return (src_file, pid)
    exts = ext_map.get(src_file, [])
    idx = fid - 1
    if 0 <= idx < len(exts):
        return (exts[idx], pid)
    return (src_file, pid)


def iter_container(cont):
    if not cont:
        return
    for e in cont:
        if isinstance(e, tuple) and len(e) == 2:
            yield e[0], e[1]
        elif isinstance(e, dict):
            yield e.get('first', ''), e.get('second')


def safe_pptr(obj):
    if not isinstance(obj, dict):
        return (0, 0)
    return (int(obj.get('m_FileID', 0)), int(obj.get('m_PathID', 0)))


# ============================================================
# Data extraction
# ============================================================

def collect_all(env):
    ext_map = build_externals_map(env)
    objects = {}
    direct_refs = defaultdict(int)
    containers = defaultdict(set)

    for obj in env.objects:
        tn = obj.type.name
        af_name = af_name_of(obj)

        if tn == 'Texture2D':
            try:
                d = obj.read()
                objects[(af_name, obj.path_id)] = {
                    'type': 'Texture2D', 'path_id': obj.path_id, 'file': af_name,
                    'name': getattr(d, 'm_Name', '') or '',
                    'w': int(getattr(d, 'm_Width', 0)),
                    'h': int(getattr(d, 'm_Height', 0)),
                }
            except Exception:
                pass

        elif tn == 'Sprite':
            try:
                tt = obj.read_typetree()
                if not isinstance(tt, dict):
                    continue
                r = tt.get('m_Rect') or {}
                b = tt.get('m_Border') or {}
                p = tt.get('m_Pivot') or {}
                objects[(af_name, obj.path_id)] = {
                    'type': 'Sprite', 'path_id': obj.path_id, 'file': af_name,
                    'name': tt.get('m_Name', '') or '',
                    'rect': (int(r.get('x', 0)), int(r.get('y', 0)),
                             int(r.get('width', 0)), int(r.get('height', 0))),
                    'border': (int(b.get('x', 0)), int(b.get('y', 0)),
                               int(b.get('z', 0)), int(b.get('w', 0))),
                    'pivot': (float(p.get('x', 0.5)), float(p.get('y', 0.5))),
                    'alignment': int(tt.get('m_Alignment', 0)),
                }
                m_rd = tt.get('m_RD') or {}
                tex = m_rd.get('texture') or {}
                fid, pid = safe_pptr(tex)
                if pid:
                    target = resolve_pptr(af_name, fid, pid, ext_map)
                    direct_refs[target] += 1
            except Exception:
                pass

        elif tn in ('AssetBundle', 'ResourceManager'):
            try:
                tt = obj.read_typetree()
            except Exception:
                continue
            for key, val in iter_container(tt.get('m_Container')):
                fid, pid = safe_pptr(val)
                if pid:
                    containers[key].add((af_name, fid, pid))

    return objects, ext_map, direct_refs, containers


# ============================================================
# Candidate analysis
# ============================================================

def lookup_object(src_file, fid, pid, objects, ext_map):
    target = resolve_pptr(src_file, fid, pid, ext_map)
    o = objects.get(target)
    if o is not None:
        return o
    cands = [(k, v) for k, v in objects.items() if k[1] == pid]
    if len(cands) == 1:
        return cands[0][1]
    for k, v in cands:
        if 'resources' in k[0]:
            return v
    return None


def analyze(containers, objects, ext_map, direct_refs):
    candidates, skipped = [], []
    for path, pid_entries in sorted(containers.items()):
        texs, sprites = [], []
        seen_tex, seen_spr = set(), set()
        for src_file, fid, pid in pid_entries:
            o = lookup_object(src_file, fid, pid, objects, ext_map)
            if not o:
                continue
            key = (o['file'], o['path_id'])
            if o['type'] == 'Texture2D' and key not in seen_tex:
                seen_tex.add(key); texs.append(o)
            elif o['type'] == 'Sprite' and key not in seen_spr:
                seen_spr.add(key); sprites.append(o)

        if not texs or len(sprites) < 2:
            continue

        last_seg = path.split('/')[-1].lower()
        named = [t for t in texs if t['name'].lower() == last_seg]
        if not named:
            skipped.append((path, f"No Texture2D named {last_seg!r} in container"))
            continue

        def covers_all(t):
            for s in sprites:
                r = s['rect']
                if r[2] <= 0 or r[3] <= 0:
                    return False
                if r[0] < 0 or r[1] < 0 or r[0] + r[2] > t['w'] or r[1] + r[3] > t['h']:
                    return False
            return True

        cover = [t for t in named if covers_all(t)]
        pool = cover if cover else named
        main = max(pool, key=lambda t: (direct_refs.get((t['file'], t['path_id']), 0),
                                        t['w'] * t['h']))

        valid = [s for s in sprites
                 if s['rect'][2] > 0 and s['rect'][3] > 0
                 and s['rect'][0] >= 0 and s['rect'][1] >= 0
                 and s['rect'][0] + s['rect'][2] <= main['w']
                 and s['rect'][1] + s['rect'][3] <= main['h']]

        if len(valid) < 2:
            continue

        names = [s['name'] for s in valid]
        dup = [n for n in set(names) if names.count(n) > 1]
        if dup:
            skipped.append((path, f"Duplicate sprite names: {dup}"))
            continue

        warning = None
        if len(named) > 1:
            warning = "Multiple same-name Texture2D: " + ", ".join(
                f"PathID={t['path_id']}({t['w']}x{t['h']},refs={direct_refs.get((t['file'], t['path_id']), 0)})"
                for t in named)

        candidates.append({
            'path': path, 'main': main,
            'sprites': sorted(valid, key=lambda x: x['name']),
            'warning': warning,
        })
    return candidates, skipped


# ============================================================
# Meta generation
# ============================================================

def find_png_meta(project_root, container_path):
    res_dir = os.path.join(project_root, 'Assets', 'Resources')
    if not os.path.isdir(res_dir):
        return None
    target = (container_path + '.png.meta').replace('/', os.sep).lower()
    for dp, _, fs in os.walk(res_dir):
        for f in fs:
            if not f.lower().endswith('.png.meta'):
                continue
            full = os.path.join(dp, f)
            if os.path.relpath(full, res_dir).lower() == target:
                return full
    return None


def gen_sprite_id(guid, name, rect, border):
    s = f"{guid}:{name}:{rect[0]}:{rect[1]}:{rect[2]}:{rect[3]}:{border[0]}:{border[1]}:{border[2]}:{border[3]}"
    return hashlib.md5(s.encode()).hexdigest()[:16] + "0800000000000000"


def build_sprite_yaml(s, iid, guid):
    r, b, p = s['rect'], s['border'], s['pivot']
    sid = gen_sprite_id(guid, s['name'], r, b)
    return [
        "    - serializedVersion: 2",
        f"      name: {s['name']}",
        "      rect:",
        "        serializedVersion: 2",
        f"        x: {r[0]}", f"        y: {r[1]}",
        f"        width: {r[2]}", f"        height: {r[3]}",
        f"      alignment: {s['alignment']}",
        f"      pivot: {{x: {p[0]}, y: {p[1]}}}",
        f"      border: {{x: {b[0]}, y: {b[1]}, z: {b[2]}, w: {b[3]}}}",
        "      outline: []", "      physicsShape: []",
        "      tessellationDetail: 0", "      bones: []",
        f"      spriteID: {sid}", f"      internalID: {iid}",
        "      vertices: []", "      indices: ", "      edges: []", "      weights: []",
    ]


def patch_meta(meta, sprites, guid):
    lines, names = [], []
    for i, s in enumerate(sprites):
        iid = 21300000 + i
        lines.extend(build_sprite_yaml(s, iid, guid))
        names.append(f"      {s['name']}: {iid}")

    m, n = re.subn(r'^  spriteMode:\s*1\s*$', '  spriteMode: 2', meta,
                   count=1, flags=re.MULTILINE)
    if n == 0:
        return None, "spriteMode: 1 not found"

    if '    sprites: []' not in m:
        return None, "sprites: [] not found"
    m = m.replace('    sprites: []', '    sprites:\n' + '\n'.join(lines), 1)

    # Compatibility: nameFileIdTable may be absent in Unity < 2021.2
    if '    nameFileIdTable: {}' in m:
        m = m.replace('    nameFileIdTable: {}',
                      '    nameFileIdTable:\n' + '\n'.join(names), 1)
    elif '    nameFileIdTable:' in m:
        # Present but not empty
        m = re.sub(r'^    nameFileIdTable:.*$',
                   '    nameFileIdTable:\n' + '\n'.join(names),
                   m, count=1, flags=re.MULTILINE)
    else:
        # Absent: append after spriteSheet block
        m = re.sub(r'(^  spriteSheet:.*?)(?=^  \w|\Z)',
                   lambda match: match.group(1).rstrip() + '\n    nameFileIdTable:\n' + '\n'.join(names) + '\n',
                   m, count=1, flags=re.MULTILINE | re.DOTALL)

    return m, None


# ============================================================
# Orphan cleanup
# ============================================================

def collect_referenced_guids(project_root):
    """Scan all serialized files for GUID references (m_Script, m_Sprite, textures, etc.)."""
    referenced = set()
    assets_dir = os.path.join(project_root, 'Assets')
    if not os.path.isdir(assets_dir):
        return referenced

    guid_re = re.compile(r'guid:\s*([0-9a-fA-F]{32})')
    exts = ('.unity', '.prefab', '.asset', '.mat', '.anim', '.controller',
            '.overrideController', '.mask', '.playable', '.signal', '.spriteatlas')

    for dp, _, fs in os.walk(assets_dir):
        for f in fs:
            if not f.lower().endswith(exts):
                continue
            p = os.path.join(dp, f)
            try:
                with open(p, 'r', encoding='utf-8', errors='ignore') as fh:
                    for line in fh:
                        for m in guid_re.finditer(line):
                            referenced.add(m.group(1).lower())
            except Exception:
                pass
    return referenced


def find_orphans_for_candidate(candidate, project_root, referenced_guids):
    """
    Identify files that became redundant after this sheet repair:
    - sactx-*.png (and .meta) whose GUID is not referenced anywhere
    - Standalone sprite .asset files whose names match this sheet's sprites
      and whose GUID is not referenced anywhere
    """
    res_dir = os.path.join(project_root, 'Assets', 'Resources')
    orphans = []

    # Locate sactx textures (name contains sprite sheet name or atlas name)
    sheet_name = candidate['path'].split('/')[-1].lower()
    sprite_names = {s['name'].lower() for s in candidate['sprites']}

    for dp, _, fs in os.walk(res_dir):
        for f in fs:
            if not f.lower().endswith(('.png', '.asset')):
                continue
            name_no_ext = os.path.splitext(f)[0].lower()
            full = os.path.join(dp, f)

            is_sactx = name_no_ext.startswith('sactx-')
            is_standalone_sprite = (f.lower().endswith('.asset')
                                    and name_no_ext in sprite_names)

            if not (is_sactx or is_standalone_sprite):
                continue

            meta = full + '.meta'
            if not os.path.isfile(meta):
                continue
            try:
                with open(meta, 'r', encoding='utf-8', errors='ignore') as fh:
                    m = re.search(r'^guid:\s*([0-9a-fA-F]{32})', fh.read(4096), re.MULTILINE)
            except Exception:
                continue
            if not m:
                continue

            guid = m.group(1).lower()
            if guid in referenced_guids:
                continue

            orphans.append((full, meta, 'sactx' if is_sactx else 'standalone_sprite'))

    return orphans


def cleanup_orphans(candidates, project_root):
    print("\n" + "=" * 78)
    print("Orphan file analysis (post-repair cleanup)")
    print("=" * 78)

    print("Scanning project for GUID references ...")
    referenced = collect_referenced_guids(project_root)
    print(f"  Referenced GUIDs: {len(referenced)}\n")

    total_removed = 0
    for cand in candidates:
        orphans = find_orphans_for_candidate(cand, project_root, referenced)
        if not orphans:
            continue

        print(f"\nContainer: {cand['path']}")
        for full, meta, kind in orphans:
            print(f"  [{kind}] {os.path.relpath(full, project_root)}")
            print(f"      guid={os.path.basename(meta)}")

        if ask_yes_no("Delete orphan files?",
                      f"Container: {cand['path']}\n"
                      f"Found {len(orphans)} orphan file(s) not referenced by any scene/prefab/asset.\n\n"
                      f"Delete them?"):
            for full, meta, _ in orphans:
                try:
                    os.remove(full)
                    os.remove(meta)
                    total_removed += 1
                    print(f"    deleted: {os.path.relpath(full, project_root)}")
                except Exception as e:
                    print(f"    failed: {full}  ({e})")

    print(f"\nOrphans removed: {total_removed}")


# ============================================================
# Main
# ============================================================

def main():
    picked = select_folder("Step 1/2: Select game root (contains *_Data) or the _Data folder")
    if not picked:
        return
    data_dir = find_data_dir(picked)
    print(f"_Data: {data_dir}\n")

    print("[1/3] UnityPy.load(directory) ...")
    try:
        env = UnityPy.load(data_dir)
    except Exception as e:
        print(f"  Failed: {e}")
        try: input("\nPress Enter to exit...")
        except EOFError: pass
        return
    print(f"  env.objects: {len(env.objects)}\n")

    print("[2/3] Collecting data ...")
    objects, ext_map, direct_refs, containers = collect_all(env)
    print(f"  objects: {len(objects)}")
    print(f"  externals map: {len(ext_map)} files")
    print(f"  Container entries: {len(containers)}\n")

    print("[3/3] Analyzing candidates ...")
    candidates, skipped = analyze(containers, objects, ext_map, direct_refs)
    print(f"  Candidates: {len(candidates)}")
    print(f"  Skipped: {len(skipped)}\n")

    if not candidates and not skipped:
        print("No container found.")
        try: input("\nPress Enter to exit...")
        except EOFError: pass
        return

    proj_parent = select_folder("Step 2/2: Select AssetRipper parent folder (contains ExportedProject)")
    if not proj_parent:
        return
    if os.path.basename(proj_parent) == 'ExportedProject':
        project_root = proj_parent
    elif os.path.isdir(os.path.join(proj_parent, 'ExportedProject')):
        project_root = os.path.join(proj_parent, 'ExportedProject')
    else:
        project_root = proj_parent
    print(f"\nProject root: {project_root}\n")

    patched, already, user_skip, no_meta = 0, 0, 0, 0
    patched_list, user_skip_list, no_meta_list = [], [], []
    repaired_candidates = []

    for cand in candidates:
        path = cand['path']; main = cand['main']; sprites = cand['sprites']

        print("=" * 78)
        print(f"Container: {path}")
        print(f"  Main texture: {main['name']!r} ({main['w']}x{main['h']})  "
              f"[{main['file']}] PathID={main['path_id']}  "
              f"direct_refs={direct_refs.get((main['file'], main['path_id']), 0)}")
        if cand['warning']:
            print(f"  WARN: {cand['warning']}")
        print(f"  Sprites ({len(sprites)}):")
        for s in sprites:
            print(f"    {s['name']:35s} rect={s['rect']}")

        meta_path = find_png_meta(project_root, path)
        if not meta_path:
            print("  meta not found in project")
            no_meta += 1; no_meta_list.append(path)
            continue

        with open(meta_path, 'r', encoding='utf-8') as f:
            mc = f.read()
        if re.search(r'^  spriteMode:\s*2\s*$', mc, re.MULTILINE):
            print("  Already spriteMode=2")
            already += 1
            repaired_candidates.append(cand)
            continue

        mg = re.search(r'^guid:\s*([0-9a-fA-F]{32})', mc, re.MULTILINE)
        if not mg:
            print("  meta missing guid")
            no_meta += 1; no_meta_list.append(path); continue
        guid = mg.group(1)

        if not ask_yes_no("Confirm repair",
                          f"Container: {path}\nMain texture: {main['name']} ({main['w']}x{main['h']})\n"
                          f"Sprites: {len(sprites)}\n"
                          f"Target: {os.path.relpath(meta_path, project_root)}\n\nRepair?"):
            print("  User skipped")
            user_skip += 1; user_skip_list.append(path); continue

        new_m, err = patch_meta(mc, sprites, guid)
        if new_m is None:
            print(f"  Failed: {err}")
            continue

        with open(meta_path, 'w', encoding='utf-8') as f:
            f.write(new_m)
        print(f"  Written: {os.path.relpath(meta_path, project_root)}")
        patched += 1; patched_list.append(path)
        repaired_candidates.append(cand)

    print("\n" + "=" * 78)
    print(f"Done: patched={patched}  already={already}  user_skip={user_skip}  no_meta={no_meta}")
    if patched_list:
        print("\nPatched:")
        for p in patched_list: print(f"  + {p}")
    if skipped:
        print("\nAuto-skipped:")
        for p, why in skipped: print(f"  - {p}  ({why})")
    if user_skip_list:
        print("\nUser-skipped:")
        for p in user_skip_list: print(f"  - {p}")
    if no_meta_list:
        print("\nMeta not found:")
        for p in no_meta_list: print(f"  - {p}")

    # Optional cleanup
    if repaired_candidates:
        cleanup_orphans(repaired_candidates, project_root)

    try: input("\nPress Enter to exit...")
    except EOFError: pass


if __name__ == '__main__':
    main()