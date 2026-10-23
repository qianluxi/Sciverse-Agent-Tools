import socket
import sys

from research_app.desktop_launcher import app_path, find_free_port, prepare_environment


def test_app_path_exists_in_source_tree():
    assert app_path().name == "app.py"
    assert app_path().exists()


def test_prepare_environment_adds_project_root():
    prepare_environment()
    assert str(app_path().parent.parent) in sys.path


def test_find_free_port_returns_bindable_port():
    port = find_free_port()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", port))
