---
name: model3d
version: 1.0.0
description: 3D 建模 playbook — build printable/exportable 3D models (STL/OBJ/glTF) with model3d_build in a plan → build → check → export loop
allowed-tools: [model3d_build, model3d_inspect, read_file, list_dir]
---
# 3D modelling playbook (model3d)

Use `model3d_build` for every 3D model request (furniture, brackets, gears, enclosures, stands,
game props). Never write STL/OBJ text or vertex lists by hand: the tool compiles a short JSON
spec, checks the mesh and writes the file in one call.

## Loop: plan → build → check → export
1. **Plan (no tool call).** Fix the units (default mm), the overall size and every key dimension
   the user gave. Decide the part tree: base solid, added parts (union), cut features (difference).
   If a size is missing, pick a sensible real-world value and say so in the answer.
2. **Build + check + export in one call.** Call `model3d_build` with `spec`, `path`
   (`models/<name>.stl` for printing, `.glb` for games/web, add `formats` for more) and an
   `expect` block restating the request: `{"size":[40,40,10],"through_holes":1}`.
3. **Read the result.** `checks: PASS` with every `expect` line ok → done. Otherwise fix exactly
   what the `problem:`/`expect ... FAIL` lines name and call again with the same `path`.
   Do not re-read the file or inspect it separately: the result already re-read it.
4. **Answer** with the file path(s), the measured size, and any assumption you made.
   Two build calls is normal; more than three means the plan was wrong — re-plan.

## Spec essentials
- `{"units":"mm","model":NODE}`. Transforms on any node: `translate`, `rotate` (degrees, X→Y→Z),
  `scale`, `mirror`; `name` labels a part in the report.
- Placement (OpenSCAD habits): `box` spans origin→size (`center:true` centres it); `cylinder`
  stands on z=0 around the Z axis. Gear, circle and regular_polygon profiles are centred.
- `difference`: first child is the base, the rest are cut away. **Cutters must overshoot**:
  for a through-hole in a 10 mm plate use `h:12` and `translate z:-1`.
- Parts that only touch along an edge make non-manifold geometry: overlap them by ≥0.5 mm and
  wrap them in `union`. Separate solids placed by `translate` alone stay separate shells.
- Repeated features: `linear_array` (`step`), `polar_array` (around Z through the origin; move
  the child out with `translate` first), `mirror_copy` for symmetric parts.
- Profiles for `extrude`/`revolve`: `polygon` (points counter-clockwise or not, no crossings),
  `rect` (with corner `radius`), `circle`, `regular_polygon`, `gear` (`teeth` + `module` or
  `outer_diameter`); `holes` cut through an extrusion. `revolve` profiles live in the (r, z)
  half-plane with r ≥ 0 (x = distance from the axis).
- Curves: default facets ≈ one per 1.5 mm; set `segments` only when needed (big booleans on
  many high-segment curves are slow).

## Units, scale, printability (purpose "print", the default)
- STL/OBJ are written in mm whatever `units` says; glTF in metres, Y up. A 2 m table is
  `{"units":"m"}` or 2000 mm — never 2 mm.
- Rest the model on z=0; put the largest flat face down; keep overhangs ≤45° or accept supports.
- Typical FDM minimums: walls ≥1.2 mm, holes +0.2–0.4 mm clearance for fits (an M3 screw hole is
  d 3.2–3.4), text/details ≥0.8 mm, bed ≈ 220–250 mm.
- Phone stands, brackets, hooks: think about the load path; fillet-free but ≥3 mm thick parts.

## Recipes
- Plate with a hole: `difference` of `box` and a `cylinder` overshooting top and bottom.
- Gear: `extrude` a `gear` profile with a `holes:[{"shape":"circle","d":5}]` bore.
- Chair/table: seat `box` + legs via `linear_array`/`mirror_copy` overlapping the seat by 1 mm,
  all in one `union`; low-poly look = low `segments`.
- Bowl/vase/cup: `revolve` a closed wall profile; lathe-like parts likewise.
- Existing file from the user: `tool_search` "model3d_inspect", then call it with `expect` to
  check size and watertightness.
