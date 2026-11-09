"""desktop/plugin.js must parse as an ES module, the way Hermes Desktop loads it.

`node --check desktop/plugin.js` parses a .js file without a package.json as
CommonJS and missed a dropped comma that kept the board from loading at all
("Plugin "baro" failed to load: SyntaxError: Unexpected identifier 'h'").
"""

import shutil
import subprocess
from pathlib import Path

import pytest

PLUGIN_JS = Path(__file__).resolve().parent.parent / "desktop" / "plugin.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_plugin_js_parses_as_an_es_module(tmp_path):
    module = tmp_path / "plugin.mjs"
    module.write_text(PLUGIN_JS.read_text(encoding="utf-8"), encoding="utf-8")
    result = subprocess.run(["node", "--check", str(module)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
