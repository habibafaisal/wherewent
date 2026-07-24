"""wherewent demo jobs package.

An explicit `__init__.py` (rather than an implicit namespace package) so
`import demo.unit_job` behaves the same regardless of how sys.path ends up
arranged when `python -m demo.run_units` is launched as a subprocess -- see
demo/run_units.py's docstring for why the module identity matters here.
"""
