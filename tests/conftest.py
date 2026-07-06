from __future__ import annotations

import importlib.machinery
import importlib.util
import pathlib
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load_script(script_name: str, module_name: str) -> types.ModuleType:
    path = ROOT / "bin" / script_name
    loader = importlib.machinery.SourceFileLoader(module_name, str(path))
    spec = importlib.util.spec_from_loader(module_name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@pytest.fixture(name="load_script")
def load_script_fixture():
    return load_script
