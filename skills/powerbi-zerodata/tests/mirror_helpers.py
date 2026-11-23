"""Small isolated workspaces for mirror regression tests."""
from pathlib import Path

from pbz.common import Workspace
from test_mirror_writers_root import _tiny


def make_workspace(directory):
    root = Path(directory, "ws")
    root.mkdir()
    ws = Workspace(str(root))
    ws.private = str(Path(directory, "private"))
    Path(ws.private).mkdir()
    return ws


def make_project(ws):
    _tiny(ws.root)
    return ws.project()
