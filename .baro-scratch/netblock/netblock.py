import socket

def _refuse(*a, **k):
    raise RuntimeError("network blocked by netblock plugin")

def pytest_configure(config):
    socket.socket.connect = _refuse
    socket.socket.connect_ex = _refuse
    socket.create_connection = _refuse
