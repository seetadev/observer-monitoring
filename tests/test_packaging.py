import ast
import os
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_dockerfile_copies_every_module_the_server_imports():
    """Every local module reachable from the entry points must be copied into the image."""
    local = {p.stem for p in ROOT.glob("*.py")}
    needed, todo = set(), ["run", "root", "admin_cli"]
    while todo:
        name = todo.pop()
        if name in needed:
            continue
        needed.add(name)
        for node in ast.walk(ast.parse((ROOT / (name + ".py")).read_text())):
            if isinstance(node, ast.Import):
                modules = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules = [node.module.split(".")[0]]
            else:
                modules = []
            todo.extend(m for m in modules if m in local)
    copy_line = next(line for line in (ROOT / "Dockerfile").read_text().splitlines() if line.startswith("COPY config"))
    copied = {os.path.splitext(name)[0] for name in copy_line.split()[1:-1]}
    assert needed <= copied, sorted(needed - copied)
