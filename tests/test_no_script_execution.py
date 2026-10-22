"""The server never runs user-supplied code and no longer stores scripts."""
import ast
import pathlib

import pytest

import root

ROOT = pathlib.Path(__file__).resolve().parent.parent
MODULES = sorted(p for p in ROOT.glob("*.py"))
FORBIDDEN_CALLS = {"exec", "eval", "compile", "__import__"}
FORBIDDEN_IMPORTS = {"selenium", "pickle", "marshal"}


def _calls_and_imports(path):
    tree = ast.parse(path.read_text())
    calls, imports = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            calls.add(node.func.id)
        elif isinstance(node, ast.Import):
            imports.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module.split(".")[0])
    return calls, imports


@pytest.mark.parametrize("path", MODULES, ids=lambda p: p.name)
def test_no_dynamic_code_execution_in_application_modules(path):
    calls, imports = _calls_and_imports(path)
    assert not calls & FORBIDDEN_CALLS
    assert not imports & FORBIDDEN_IMPORTS


@pytest.mark.parametrize("action", ["SAVE_TEST", "FETCH_TESTS_LIST", "FETCH_TEST", "RUN_TEST"])
async def test_script_actions_are_gone_and_store_nothing(dash, db, action):
    response = await dash.post("/api", data={"action": action, "machine": "box", "testName": "t",
                                             "testCode": "x = 1", "test_name": "t", "instancesCount": "1"})
    assert response.status_code == 400
    assert "[box]-tests" not in await db.list_collection_names()


def test_the_simulator_service_no_longer_exists():
    assert not hasattr(root, "make_sim_app") and not hasattr(root, "SimHandler")
    assert not hasattr(root, "runTests") and not hasattr(root, "ObserverDriver")


async def test_perf_page_is_a_parameter_form_without_any_code_editor(dash):
    page = (await dash.get("/perf")).text
    assert "Page-load tests" in page and "<textarea" not in page and "mode-python" not in page
    for field in ("lt-url", "lt-concurrency", "lt-duration"):
        assert 'id="%s"' % field in page
