__import__("sys").dont_write_bytecode = True  # first statement: catalog rule 17, no bytecode

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _load_plugin_package() -> None:
    """Load the repository root (not an importable name) as package ``baro_plugin``."""
    if "baro_plugin" in sys.modules:
        return
    spec = importlib.util.spec_from_file_location(
        "baro_plugin",
        ROOT / "__init__.py",
        submodule_search_locations=[str(ROOT)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["baro_plugin"] = module
    spec.loader.exec_module(module)


_load_plugin_package()


def _read_fixture(name: str) -> list[str]:
    return (FIXTURES / name).read_text(encoding="utf-8").splitlines()


@pytest.fixture
def load_fixture():
    """Return ``load_fixture(name) -> list[str]``: the recorded lines of a fixture."""
    return _read_fixture


@pytest.fixture
def memory_store():
    """A fresh in-memory ``BoardStore`` (JSON-only values)."""
    from baro_plugin.core.store import MemoryStore

    return MemoryStore()
