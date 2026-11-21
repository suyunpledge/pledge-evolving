"""Parametric 3D modelling for the agent, standard library only.

Pipeline: JSON spec → mesh (``spec``/``csg``/``profiles``) → checks
(``validate``) → files (``io``) → preview (``render``). The agent-facing tools
live in ``keystone.model3d_tools``; this package has no Keystone dependencies,
so it can be tested and reused on its own.
"""
from .mesh import Matrix, Mesh
from .spec import SpecError, compile_spec, parse_spec
from .validate import analyze, summary_lines
from .io import decode, encode, format_for
from .render import ascii_view, contact_sheet

__all__ = ["Matrix", "Mesh", "SpecError", "analyze", "ascii_view", "compile_spec", "contact_sheet",
           "decode", "encode", "format_for", "parse_spec", "summary_lines"]
