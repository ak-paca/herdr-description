import json
import os
import socket
import uuid


class HerdrError(RuntimeError):
    pass


class Herdr:
    def __init__(self, socket_path=None, timeout=5):
        self.socket_path = socket_path or os.environ.get("HERDR_SOCKET_PATH")
        if not self.socket_path:
            raise HerdrError("Run this command through a Herdr plugin action.")
        self.timeout = timeout

    def call(self, method, **params):
        request_id = str(uuid.uuid4())
        request = {"id": request_id, "method": method, "params": params}
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(self.timeout)
            connection.connect(self.socket_path)
            connection.sendall((json.dumps(request) + "\n").encode())
            with connection.makefile("rb") as stream:
                response = json.loads(stream.readline(16 * 1024 * 1024))
        if response.get("id") != request_id:
            raise HerdrError("Herdr returned a response for a different request.")
        if response.get("error"):
            error = response["error"]
            raise HerdrError(error.get("message", str(error)))
        if "result" not in response:
            raise HerdrError("Herdr returned an invalid response.")
        return response["result"]

    def snapshot(self):
        return self.call("session.snapshot")["snapshot"]
