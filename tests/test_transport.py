import asyncio
import json
import socket

import pytest

from zundamon_jarvis import control, transport


class Target:
    def __init__(self):
        self.state = "listening"
        self.stopped = False

    async def pause(self):
        self.state = "paused"

    async def resume(self):
        self.state = "listening"

    async def toggle(self):
        self.state = "listening" if self.state == "paused" else "paused"

    def stop(self):
        self.stopped = True


def test_json_path_means_tcp(tmp_path):
    assert transport.is_tcp(tmp_path / "control.json")
    assert not transport.is_tcp(tmp_path / "zundamon-jarvis.sock")


async def test_tcp_round_trip_writes_port_and_token(tmp_path):
    path = tmp_path / "control.json"
    server = await control.serve(Target(), path)
    try:
        data = json.loads(path.read_text())
        assert isinstance(data["port"], int) and len(data["token"]) == 32
        resp = await asyncio.to_thread(control.request, "toggle", path)
        assert resp == {"state": "paused", "text": "Paused"}
        assert await asyncio.to_thread(control.is_live, path)
    finally:
        server.close()
        await server.wait_closed()
    assert not await asyncio.to_thread(control.is_live, path)


async def test_tcp_without_the_token_gets_nothing(tmp_path):
    path = tmp_path / "control.json"
    t = Target()
    server = await control.serve(t, path)
    port = json.loads(path.read_text())["port"]

    def intruder() -> bytes:
        with socket.create_connection(("127.0.0.1", port), timeout=3) as s:
            s.sendall(b"wrong-token\ntoggle\n")
            return s.recv(4096)

    try:
        assert await asyncio.to_thread(intruder) == b""
        assert t.state == "listening"
    finally:
        server.close()
        await server.wait_closed()


def test_missing_endpoint_file_is_not_running(tmp_path):
    with pytest.raises(OSError):
        control.request("status", tmp_path / "control.json", timeout=1)
    assert not control.is_live(tmp_path / "control.json")


def test_garbled_endpoint_file_is_not_running(tmp_path):
    path = tmp_path / "control.json"
    path.write_text("{not json")
    with pytest.raises(OSError):
        transport.connect(path, 1)


async def test_tcp_stale_endpoint_is_replaced(tmp_path):
    path = tmp_path / "control.json"
    path.write_text(json.dumps({"port": 1, "token": "x"}))  # left by a crash
    server = await control.serve(Target(), path)
    try:
        resp = await asyncio.to_thread(control.request, "status", path)
        assert resp["state"] == "listening"
    finally:
        server.close()
        await server.wait_closed()


async def test_quit_stops_the_target():
    t = Target()
    resp = await control.handle("quit", t)
    assert t.stopped and resp["quitting"] is True and resp["state"] == "listening"


async def test_quit_without_stop_is_an_error():
    class NoStop(Target):
        stop = None

    assert "error" in await control.handle("quit", NoStop())
