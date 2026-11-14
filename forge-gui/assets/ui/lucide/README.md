# Forge UI icon sources

Selected unmodified SVGs from `lucide-static@0.468.0` (ISC; see LICENSE).
Source: https://github.com/lucide-icons/lucide/tree/0.468.0
Distribution: https://cdn.jsdelivr.net/npm/lucide-static@0.468.0/icons/

`desktop/make_lucide_assets.py` maps Forge's existing semantic names to these
icons and renders 1.75/24 strokes at 8–64 physical pixels. Runtime combines the
precomputed alpha masks with the exact requested foreground color. The shipped
PNG sprite sheets remain a fallback for installations without the new masks.

Build dependencies only: Pillow and resvg-py 0.5.0. No downloads or third-party
Python dependencies are required when running Forge. Original SVG geometry and
license remain here for inspection and reproducible builds.
