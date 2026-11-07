"""One bounded, cancellable HTTP SSE read; paid operations are never issued here."""

import time

import httpx
from httpx_sse import SSEError, connect_sse

from agent.domains.image_generation.state import TERMINAL, checked, show_progress
from agent.runtime.control import model_options
from agent.runtime.progress import received
from agent.runtime.turns import check_cancelled
from tools import base

MAX_STREAM_BYTES = 2 * 1024 * 1024
FINISH_RESERVE_SECONDS = 15
IO_TIMEOUT_SECONDS = 3


class WaitElapsed(Exception):
    """The requested observation window elapsed without a terminal host state."""


class CheckedStream(httpx.SyncByteStream):
    def __init__(self, original, config, deadline):
        self.original, self.config, self.deadline = original, config, deadline

    def _check(self):
        check_cancelled(self.config)
        if time.monotonic() >= self.deadline:
            raise WaitElapsed()

    def __iter__(self):
        size, original = 0, iter(self.original)
        while True:
            self._check()
            try:
                chunk = next(original)
            except StopIteration:
                return
            self._check()
            size += len(chunk)
            if size > MAX_STREAM_BYTES:
                raise ValueError("Image job stream exceeds its byte limit")
            if chunk:
                received(self.config)
            yield chunk

    def close(self):
        self.original.close()


def wait(job_id, seconds, config, path):
    check_cancelled(config)
    # Budget startup connect/headers and the final blocking read before returning to the model.
    overhead = 2 * IO_TIMEOUT_SECONDS + 1
    window = seconds + FINISH_RESERVE_SECONDS + overhead
    remaining = model_options(window).get("timeout", window)
    seconds = min(seconds, max(0, int(remaining - FINISH_RESERVE_SECONDS - overhead)))
    if seconds < 1:
        return base.unavailable("本轮剩余时间不足，尚未等待核实生图结果；可继续查看该任务。")
    deadline, last = time.monotonic() + seconds + 1, None
    uid = base._configured_user_id(config)
    if uid <= 0:
        return base.unavailable("当前用户身份未核实，未等待生图任务。")
    headers = {"Authorization": "Bearer " + base.host_credential(uid),
               "Accept-Encoding": "identity"}
    try:
        with connect_sse(base._client, "GET", f"{base.ADMIN_BASE}{path}/jobs/{job_id}/events",
                params={"wait_seconds": seconds}, headers=headers,
                timeout=httpx.Timeout(remaining, connect=min(IO_TIMEOUT_SECONDS, remaining),
                                      read=min(IO_TIMEOUT_SECONDS, remaining))) as source:
            response = source.response
            response.raise_for_status()
            if response.headers.get("content-encoding", "identity") != "identity":
                raise ValueError("Image event streams must use identity encoding")
            response.stream = CheckedStream(response.stream, config, deadline)
            for event in source.iter_sse():
                check_cancelled(config)
                if event.event == "image_job_error":
                    raise ValueError("Host could not verify the image job")
                if event.event not in {"image_job", "image_job_timeout"}:
                    raise ValueError("Unexpected image event")
                job = checked(event.json(), job_id)
                if last is not None and job.requestHash != last.requestHash:
                    raise ValueError("Frozen image request changed while waiting")
                last = job
                show_progress(job, config)
                if job.status in TERMINAL:
                    return job, "observed"
                if event.event == "image_job_timeout":
                    return job, "timeout"
            raise ValueError("Image job stream ended without a verified terminal or timeout event")
    except WaitElapsed:
        if last is not None:
            return last, "timeout"
    except (httpx.HTTPError, SSEError, ValueError):
        check_cancelled(config)
    return base.unavailable("生图任务结果暂不可用或未能核实；不会重新调用计费 API，"
                            "请稍后继续查看。")
