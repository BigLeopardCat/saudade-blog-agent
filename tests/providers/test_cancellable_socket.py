"""A real local HTTP peer observes connection closure before the client pool shuts down."""

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from langchain_core.messages import HumanMessage
from openai import APITimeoutError

from agent.runtime.control import AgentCancelled, RequestControl, request_scope
from models.llm import get_llm
from models.transport import ModelIO, io_scope


@pytest.mark.enable_socket
@pytest.mark.parametrize("headers_sent", [False, True])
@pytest.mark.parametrize("termination", ["cancel", "timeout"])
def test_cancel_or_timeout_closes_the_actual_socket(monkeypatch, headers_sent, termination):
    monkeypatch.setattr("config.settings.llm_provider", "opencode")
    if termination == "timeout":
        monkeypatch.setattr("config.settings.llm_timeout", 0.15)
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
                    except (AgentCancelled, APITimeoutError, httpx.ReadTimeout) as exc:
                        errors.append(exc)

                worker = threading.Thread(target=run)
                worker.start()
                assert entered.wait(3)
                if termination == "cancel":
                    stopped.set()
                worker.join(2)
                assert not worker.is_alive() and len(errors) == 1
                expected = (AgentCancelled if termination == "cancel" else
                            httpx.ReadTimeout if headers_sent else APITimeoutError)
                assert isinstance(errors[0], expected)
                assert disconnected.wait(1), "the peer did not see a closed connection"
                assert io.pending_cleanup == 0 and io.ready
                assert not io.client.is_closed
        finally:
            stopped.set()
            server.shutdown()
            serving.join(2)
