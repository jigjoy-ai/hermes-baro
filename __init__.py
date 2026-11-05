"""Baro plugin for Hermes Agent: registration entry point.

Hermes loads this package and calls ``register(ctx)`` exactly once at startup.
The guide puts that function in ``__init__.py`` and has it call
``ctx.register_tool`` (reference-hermes-agent/website/docs/developer-guide/plugins/index.md:895-930).
Hermes has no manifest key that names another entry module, so this file only
delegates to ``bridge.register``. That function registers the four ``baro``
toolset handlers. Each handler follows the contract
``handler(args, **kwargs) -> str`` (JSON) and never raises
(reference-hermes-agent/website/docs/developer-guide/plugins/index.md:885-892).
bridge.py is the only module that touches ``ctx``. It starts Baro through
``ctx.dispatch_tool`` and persists boards through ``ctx.state``
(reference-hermes-agent/website/docs/developer-guide/plugins/index.md:934-935).

desktop/plugin.js reads boards through the package backend
dashboard/plugin_api.py, which dashboard/manifest.json pairs with it the way
the built-in kanban plugin pairs its own backend
(reference-hermes-agent/plugins/kanban/dashboard/manifest.json:13).

The bridge is imported lazily inside ``register`` for two reasons. Importing
this package must never import a Hermes module. And tests/conftest.py loads
this file as ``baro_plugin`` without a Hermes context.
"""


def register(ctx):
    from .bridge import register as _register

    return _register(ctx)
