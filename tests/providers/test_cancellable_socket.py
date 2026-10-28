"""A real local HTTP peer observes connection closure before the client pool shuts down."""

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from langchain_core.messages import HumanMessage

from agent.runtime.control import AgentCancelled, RequestControl, request_scope
from models.llm import get_llm
from models.transport import ModelIO, io_scope


@pytest.mark.enable_socket
@pytest.mark.parametrize("headers_sent", [False, True])
def test_cancel_closes_the_actual_socket_without_waiting_for_server_data(monkeypatch, headers_sent):
    monkeypatch.setattr("config.settings.llm_provider", "opencode")
    entered, disconnected, stopped = threading.Event(), threading.Event(), threading.Event()
    errors = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            if headers_sent:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.flush()
            entered.set()
            self.connection.settimeout(3)
            if self.rfile.read(1) == b"":
                disconnected.set()

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        serving = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01))
        serving.start()
        try:
            with ModelIO() as io:
                def run():
                    try:
                        with io_scope(io), request_scope(RequestControl(
                                stopped, time.monotonic() + 10)):
                            model = get_llm(api_key="synthetic-only",
                                base_url=f"http://127.0.0.1:{server.server_port}/v1",
                                model="deepseek-v4.1-flash", streaming=True, max_retries=0)
                            model.invoke([HumanMessage(content="Hello.")])
                    except AgentCancelled as exc:
                        errors.append(exc)

                worker = threading.Thread(target=run)
                worker.start()
                assert entered.wait(3)
                stopped.set()
                worker.join(2)
                assert not worker.is_alive() and len(errors) == 1
                assert disconnected.wait(1), "the peer did not see a closed connection"
                assert not io.client.is_closed
        finally:
            stopped.set()
            server.shutdown()
            serving.join(2)
