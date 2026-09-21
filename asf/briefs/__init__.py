"""asf.briefs — every session's brief, as code.

A brief is not prose somebody writes per launch: it is a template per row kind plus a
code-generated preamble of the facts the runner already holds. :mod:`asf.briefs.preamble` says
why that preamble is the cost lever; :mod:`asf.briefs.build` is the one entry point::

    from asf import briefs
    brief = briefs.build(product, row, index, inflight, repo_facts)
    brief.text, brief.model, brief.add_dirs, brief.id_ranges_needed

Note that ``asf.briefs.build`` is that function, not the submodule of the same name — anything
wanting the module itself (its tables, its templates) asks for it by name:
``importlib.import_module('asf.briefs.build')``.
"""
from asf.briefs.build import (KINDS, Brief, BriefError, build, model_for,  # noqa: F401
                              normalize_kind, register)
