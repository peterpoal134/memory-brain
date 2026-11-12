"""带硬总时限的 HTTP(S) JSON POST（v2）。

v1 的错在哪：它用 http.client + 看门狗 `sock.shutdown()`。Windows 上这个办法不管用——
带 timeout 的 socket 在 CPython 里是非阻塞 + select，另一个线程 shutdown/close 都**唤不醒**
已经阻塞在 select 上的读，只有对端发数据或关连接时它才返回。所以「总时限 1s」实际
4s 才返回（等上游自己松手），而 `socket_shutdown=true` 只说明 shutdown 调用成功了。

v2 的做法：不依赖「唤醒阻塞的读」，而是**让每一次阻塞都有上界**。
所有 recv 都切成 ≤0.2s 的片，片间检查 deadline；到点立刻抛 UpstreamTimeout，
socket 在 finally 里关掉。这样退出时间由总时限决定，与上游配不配合无关。

同时：
- 自己拼 HTTP/1.1 请求，自己解 Content-Length / chunked / 读到 EOF 三种正文；
- 保留 urllib 的代理语义（registry/环境变量 + proxy_bypass），https 走 CONNECT 隧道；
- HTTP ≥400 抛 UpstreamHTTPError，只带状态码与原因短语，**不带响应正文**；
- 线程：默认不起看门狗。DNS 解析放进**固定的守护线程池**（`RESOLVE_WORKERS` 个）+ 有界队列
  （`RESOLVE_QUEUE` 个），调用方按剩余时间等结果。取消边界：`getaddrinfo` 不可取消，
  到点后仍在跑的任务继续占着工作线程，但**线程总数恒定**、队列满时新请求立即失败，
  不会随请求数增长。这个边界写在这里，测试也按它断言，不再宣称「DNS 已终止、无残留」。

环境变量：
  MEMORY_LLM_TOTAL_TIMEOUT   整个请求的总秒数，默认 60
  MEMORY_LLM_READ_TIMEOUT    单次 socket 操作的上限，默认 30（内部会再切成 0.2s 片）
  MEMORY_LLM_CAFILE          可选：TLS 校验用的 CA 包（默认系统信任库）
"""

from __future__ import annotations

import json
import math
import os
import queue
import socket
import ssl
import threading
import time
import urllib.request
from urllib.parse import urlsplit

__all__ = [
    "UpstreamTimeout", "UpstreamTransportError", "UpstreamHTTPError", "UpstreamResponseTooLarge",
    "post_json", "stream_sse", "last_timing", "resolve_stats",
]

SLICE = 0.2          # 每次阻塞的最长秒数；退出时间 = 总时限 + 最多一个 SLICE

RESOLVE_WORKERS = 4  # DNS 工作线程数：固定，不随请求增长
RESOLVE_QUEUE = 16   # DNS 等待队列长度：满则立即失败，不无限堆积
MAX_JSON_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_CHUNK_LINE_BYTES = 8192
MAX_TRAILER_BYTES = 64 * 1024


class UpstreamTimeout(TimeoutError):
    """到达总时限，已强制结束本次请求。"""


class UpstreamTransportError(IOError):
    """连接/协议层面的错误（对端断开、半包、TLS 失败、非 JSON 响应等）。"""


class UpstreamResponseTooLarge(UpstreamTransportError):
    """A non-streaming response exceeded its explicit bounded byte allowance."""


class UpstreamHTTPError(UpstreamTransportError):
    """上游返回 4xx/5xx。只保留状态码与原因短语，正文一律不带出。"""

    def __init__(self, status: int, reason: str, body_bytes: int,
                 retry_after: str | None = None):
        super().__init__(f"HTTP {status} {reason}".strip() + f"（正文 {body_bytes} 字节，未记录）")
        self.status = status
        self.reason = reason
        self.body_bytes = body_bytes
        self.retry_after = retry_after


_TIMING = threading.local()


def last_timing() -> dict:
    """最近一次请求的耗时明细，只含数字/枚举，不含请求与响应正文。"""
    return dict(getattr(_TIMING, "value", {}) or {})


def _env_float(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, "") or default)
    except Exception:
        return default
    return value if math.isfinite(value) and value > 0 else default


def _remaining(deadline: float) -> float:
    return deadline - time.monotonic()


# --------------------------------------------------------------------- 解析


_RESOLVE_LOCK = threading.Lock()
_RESOLVE_QUEUE_OBJ: "queue.Queue | None" = None
_RESOLVE_INFLIGHT = 0
_RESOLVE_STARTED = 0


def _resolve_worker() -> None:
    """常驻守护线程：从队列取任务解析，结果写回任务自己的 box。"""
    global _RESOLVE_INFLIGHT
    while True:
        task = _RESOLVE_QUEUE_OBJ.get()
        if task is None:
            return
        try:
            task["box"]["infos"] = socket.getaddrinfo(task["host"], task["port"],
                                                     type=socket.SOCK_STREAM)
        except Exception as exc:            # noqa: BLE001 - 原样带回去分类
            task["box"]["error"] = exc
        finally:
            task["done"].set()
            with _RESOLVE_LOCK:
                _RESOLVE_INFLIGHT -= 1


def _ensure_resolver() -> "queue.Queue":
    global _RESOLVE_QUEUE_OBJ, _RESOLVE_STARTED
    with _RESOLVE_LOCK:
        if _RESOLVE_QUEUE_OBJ is None:
            _RESOLVE_QUEUE_OBJ = queue.Queue(maxsize=RESOLVE_QUEUE)
            for i in range(RESOLVE_WORKERS):
                threading.Thread(target=_resolve_worker, name=f"fish-dns-{i + 1}",
                                 daemon=True).start()
            _RESOLVE_STARTED = RESOLVE_WORKERS
        return _RESOLVE_QUEUE_OBJ


def resolve_stats() -> dict:
    """解析池现状：线程数恒定、in_flight 最多 RESOLVE_WORKERS+RESOLVE_QUEUE。"""
    with _RESOLVE_LOCK:
        return {"workers": _RESOLVE_STARTED, "in_flight": _RESOLVE_INFLIGHT,
                "queued": _RESOLVE_QUEUE_OBJ.qsize() if _RESOLVE_QUEUE_OBJ else 0,
                "threads": sum(1 for t in threading.enumerate()
                               if t.name.startswith("fish-dns"))}


def _resolve(host: str, port: int, deadline: float, timing: dict) -> list:
    """DNS 走固定线程池 + 有界队列；调用方最多等到 deadline。

    取消边界：`getaddrinfo` 不可取消。到点后调用方立刻返回，仍在解析的任务继续占一个
    工作线程直到系统解析返回；但线程总数恒定（RESOLVE_WORKERS），队列满（RESOLVE_QUEUE）
    时新请求立即失败——资源上界与请求数无关。
    """
    global _RESOLVE_INFLIGHT
    pool = _ensure_resolver()
    task = {"host": host, "port": port, "done": threading.Event(), "box": {}}
    with _RESOLVE_LOCK:
        _RESOLVE_INFLIGHT += 1
    try:
        pool.put_nowait(task)
    except queue.Full:
        with _RESOLVE_LOCK:
            _RESOLVE_INFLIGHT -= 1
        timing["phase"] = "dns"
        raise UpstreamTransportError(
            f"DNS 解析排队已满（{RESOLVE_WORKERS} 工作线程 + {RESOLVE_QUEUE} 排队）") from None
    if not task["done"].wait(max(0.0, _remaining(deadline))):
        timing["phase"] = "dns"
        raise UpstreamTimeout("DNS 解析未在总时限内完成（底层任务不可取消）")
    box = task["box"]
    if "error" in box:
        exc = box["error"]
        timing["phase"] = "dns"
        raise UpstreamTransportError(f"DNS 解析失败：{type(exc).__name__}") from exc
    infos = box.get("infos") or []
    if not infos:
        timing["phase"] = "dns"
        raise UpstreamTransportError("DNS 无结果")
    return infos


def _proxy_for(scheme: str, host: str):
    """沿用 urllib 的代理语义：registry/环境变量，且尊重 proxy_bypass。"""
    try:
        if urllib.request.proxy_bypass(host):
            return None
    except Exception:
        pass
    try:
        proxies = urllib.request.getproxies()
    except Exception:
        return None
    raw = proxies.get(scheme) or proxies.get("all")
    if not raw:
        return None
    if "://" not in raw:
        raw = "http://" + raw
    parts = urlsplit(raw)
    if not parts.hostname:
        return None
    return (parts.hostname, parts.port or 8080)


# --------------------------------------------------------------------- 连接


def _open_socket(host: str, port: int, deadline: float, read: float, timing: dict) -> socket.socket:
    infos = _resolve(host, port, deadline, timing)
    last: Exception | None = None
    for family, socktype, proto, _canon, sockaddr in infos:
        sock = socket.socket(family, socktype, proto)
        try:
            sock.settimeout(max(0.05, min(read, _remaining(deadline))))
            sock.connect(sockaddr)
            return sock
        except Exception as exc:            # noqa: BLE001
            last = exc
            try:
                sock.close()
            except Exception:
                pass
            if _remaining(deadline) <= 0:
                timing["phase"] = "connect"
                raise UpstreamTimeout("连接未在总时限内建立") from exc
    timing["phase"] = "connect"
    raise UpstreamTransportError(f"连接失败：{type(last).__name__ if last else 'unknown'}")


def _send_all(sock: socket.socket, data: bytes, deadline: float, timing: dict) -> None:
    """分片发送，每片检查 deadline（sendall 在极端情况下可能长时间阻塞）。"""
    timing["phase"] = "send"
    view = memoryview(data)
    while view:
        if _remaining(deadline) <= 0:
            raise UpstreamTimeout("发送未在总时限内完成")
        sock.settimeout(max(0.05, min(SLICE, _remaining(deadline))))
        try:
            sent = sock.send(view[:65536])
        except socket.timeout:
            continue
        except OSError as exc:
            raise UpstreamTransportError(f"发送阶段 {type(exc).__name__}") from exc
        if sent <= 0:
            raise UpstreamTransportError("发送阶段连接被关闭")
        view = view[sent:]


class _Reader:
    """有上界的读：每次 recv 至多 SLICE 秒，片间看 deadline。"""

    def __init__(self, sock: socket.socket, deadline: float, timing: dict):
        self.sock = sock
        self.deadline = deadline
        self.timing = timing
        self.buf = b""
        self.total = 0
        self.eof = False

    def _fill(self, phase: str) -> bytes | None:
        """读一片：返回数据；`None` = 这一片等满了 SLICE 还没数据；b"" = 对端关连接。

        区分 `None` 与 `b""` 很重要——混在一起会变成「EOF 之后空转直到总时限」。
        """
        budget = _remaining(self.deadline)
        if budget <= 0:
            self.timing["phase"] = phase
            raise UpstreamTimeout(f"{phase} 阶段到达总时限")
        self.timing["phase"] = phase
        self.sock.settimeout(max(0.05, min(SLICE, budget)))
        try:
            chunk = self.sock.recv(65536)
        except socket.timeout:
            return None
        except OSError as exc:
            if _remaining(self.deadline) <= 0:
                raise UpstreamTimeout(f"{phase} 阶段到达总时限") from exc
            raise UpstreamTransportError(f"{phase} 阶段 {type(exc).__name__}") from exc
        if not chunk:
            self.eof = True
            return b""
        self.total += len(chunk)
        self.timing["bytes"] = self.total
        return chunk

    def _need(self, phase: str) -> None:
        """补一片进 buf；EOF 直接按传输错误抛出，绝不空转。"""
        chunk = self._fill(phase)
        if chunk is None:
            return
        if chunk == b"":
            raise UpstreamTransportError(f"{phase} 阶段对端提前关闭连接")
        self.buf += chunk

    def until(self, marker: bytes, limit: int = 262144) -> bytes:
        while marker not in self.buf:
            self._need("headers")
            if len(self.buf) > limit:
                raise UpstreamTransportError("响应头过长")
        head, _, rest = self.buf.partition(marker)
        self.buf = rest
        return head

    def exact(self, want: int, phase: str = "body") -> bytes:
        while len(self.buf) < want:
            self._need(phase)
        out, self.buf = self.buf[:want], self.buf[want:]
        return out

    def to_eof(self, phase: str = "body", limit: int | None = None) -> bytes:
        if limit is not None and len(self.buf) > limit:
            raise UpstreamResponseTooLarge("upstream_response_too_large")
        while True:
            chunk = self._fill(phase)
            if chunk is None:
                continue
            if chunk == b"":
                break                       # 对端关连接，正常收尾
            self.buf += chunk
            if limit is not None and len(self.buf) > limit:
                raise UpstreamResponseTooLarge("upstream_response_too_large")
        out, self.buf = self.buf, b""
        return out

    def line(self, phase: str = "body", limit: int = MAX_CHUNK_LINE_BYTES) -> bytes:
        while b"\r\n" not in self.buf:
            if len(self.buf) > limit:
                raise UpstreamTransportError("upstream_chunk_line_too_large")
            self._need(phase)
        line, _, self.buf = self.buf.partition(b"\r\n")
        if len(line) > limit:
            raise UpstreamTransportError("upstream_chunk_line_too_large")
        return line

    def iter_exact(self, want: int):
        while want:
            size = min(want, 65536)
            yield self.exact(size)
            want -= size

    def iter_eof(self):
        if self.buf:
            buffered, self.buf = self.buf, b""
            yield buffered
        while True:
            chunk = self._fill("body")
            if chunk is None:continue
            if not chunk:return
            yield chunk


def _read_chunked(reader: _Reader, max_bytes: int = MAX_JSON_RESPONSE_BYTES) -> bytes:
    out = bytearray()
    while True:
        size_line = reader.line("body")
        if not size_line:
            continue
        size = int(size_line.split(b";")[0].strip() or b"0", 16)
        if size < 0:raise UpstreamTransportError("upstream_invalid_chunk_size")
        if size > max_bytes - len(out):
            raise UpstreamResponseTooLarge("upstream_response_too_large")
        if size == 0:
            trailer_bytes = 0
            while True:                     # trailer
                trailer = reader.line("body")
                trailer_bytes += len(trailer) + 2
                if trailer_bytes > MAX_TRAILER_BYTES:
                    raise UpstreamTransportError("upstream_trailers_too_large")
                if not trailer:
                    break
            return bytes(out)
        out += reader.exact(size)
        reader.exact(2)                     # \r\n


def _parse_head(head: bytes) -> tuple[int, str, dict]:
    lines = head.decode("iso-8859-1").split("\r\n")
    if not lines or not lines[0].startswith("HTTP/"):
        raise UpstreamTransportError("响应头无法解析")
    parts = lines[0].split(" ", 2)
    try:
        status = int(parts[1])
    except Exception as exc:
        raise UpstreamTransportError("响应状态行无法解析") from exc
    reason = parts[2] if len(parts) > 2 else ""
    headers = {}
    for line in lines[1:]:
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower()
        value = value.strip()
        headers[key] = f"{headers[key]}, {value}" if key in headers else value
    return status, reason, headers


def _tls_wrap(sock: socket.socket, host: str, deadline: float, timing: dict) -> ssl.SSLSocket:
    timing["phase"] = "tls"
    budget = _remaining(deadline)
    if budget <= 0:
        raise UpstreamTimeout("TLS 握手前到达总时限")
    cafile = os.environ.get("MEMORY_LLM_CAFILE") or None
    context = ssl.create_default_context(cafile=cafile)
    sock.settimeout(max(0.05, min(budget, 30.0)))
    try:
        return context.wrap_socket(sock, server_hostname=host)
    except socket.timeout as exc:
        raise UpstreamTimeout("TLS 握手未在总时限内完成") from exc
    except ssl.SSLError as exc:
        raise UpstreamTransportError(f"TLS 握手失败：{exc.reason or 'SSLError'}") from exc


# --------------------------------------------------------------------- 主流程


def post_json(url: str, payload: dict, headers: dict | None = None,
              total_timeout: float | None = None,
              read_timeout: float | None = None,
              max_response_bytes: int = MAX_JSON_RESPONSE_BYTES) -> dict:
    """POST 一个 JSON 拿回 JSON；到总时限抛 UpstreamTimeout。

    保证：本函数返回或抛出时，自己打开的 socket 已关闭；不遗留看门狗/计时线程。
    """
    if isinstance(max_response_bytes, bool) or not isinstance(max_response_bytes, int) or max_response_bytes < 1:
        raise ValueError("max_response_bytes must be a positive integer")
    total = float(total_timeout if total_timeout is not None
                  else _env_float("MEMORY_LLM_TOTAL_TIMEOUT", 60.0))
    read = float(read_timeout if read_timeout is not None
                 else _env_float("MEMORY_LLM_READ_TIMEOUT", min(30.0, total)))
    if not math.isfinite(total) or total <= 0 or not math.isfinite(read) or read <= 0:
        raise UpstreamTransportError("upstream_invalid_timeout")
    parts = urlsplit(url)
    scheme = (parts.scheme or "http").lower()
    if scheme not in ("http", "https"):
        raise UpstreamTransportError(f"不支持的协议：{scheme}")
    host = parts.hostname or ""
    port = parts.port or (443 if scheme == "https" else 80)
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    if not host:
        raise UpstreamTransportError("URL 缺少主机名")

    started = time.monotonic()
    deadline = started + total
    timing = {"url": f"{scheme}://{host}:{port}{path}", "total_timeout": total,
              "read_timeout": read, "phase": "start", "elapsed_ms": 0.0,
              "bytes": 0, "status": None, "deadline_hit": False,
              "tls": scheme == "https", "proxy": None}

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    default_port = 443 if scheme == "https" else 80
    head = {
        "Host": host if port == default_port else f"{host}:{port}",
        "Content-Type": "application/json",
        "Content-Length": str(len(body)),
        "User-Agent": "fish-memory/1.0",
    }
    for key, value in (headers or {}).items():
        if value is not None:
            head[str(key)] = str(value)

    sock: socket.socket | None = None
    try:
        proxy = _proxy_for(scheme, host)
        if proxy:
            timing["proxy"] = f"{proxy[0]}:{proxy[1]}"
            sock = _open_socket(proxy[0], proxy[1], deadline, read, timing)
            if scheme == "https":
                target = f"{host}:{port}"
                _send_all(sock, (f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n"
                                 "Proxy-Connection: keep-alive\r\n\r\n").encode(), deadline, timing)
                reader = _Reader(sock, deadline, timing)
                status, reason, _hdrs = _parse_head(reader.until(b"\r\n\r\n"))
                if status != 200:
                    raise UpstreamTransportError(f"代理 CONNECT 被拒：HTTP {status}")
                sock = _tls_wrap(sock, host, deadline, timing)
            request_target = url
        else:
            sock = _open_socket(host, port, deadline, read, timing)
            if scheme == "https":
                sock = _tls_wrap(sock, host, deadline, timing)
            request_target = path

        # Host 一律保留：绝对 URI 的代理请求也必须带 Host（RFC 7230 5.4），
        # 缺 Host 会被合规代理直接 400；CONNECT 隧道内这是面向源站的 Host。
        request = [f"POST {request_target} HTTP/1.1"]
        request += [f"{key}: {value}" for key, value in head.items()]
        request.append("Connection: close")
        raw = ("\r\n".join(request) + "\r\n\r\n").encode("utf-8") + body
        _send_all(sock, raw, deadline, timing)

        reader = _Reader(sock, deadline, timing)
        status, reason, resp_headers = _parse_head(reader.until(b"\r\n\r\n"))
        timing["status"] = status
        chunked = "chunked" in (resp_headers.get("transfer-encoding") or "").lower()
        if chunked:
            data = _read_chunked(reader, max_response_bytes)
        elif "content-length" in resp_headers:
            try:
                want = int(resp_headers["content-length"])
            except Exception:
                want = -1
            if want > max_response_bytes:
                raise UpstreamResponseTooLarge("upstream_response_too_large")
            data = reader.exact(want) if want >= 0 else reader.to_eof(limit=max_response_bytes)
        else:
            data = reader.to_eof(limit=max_response_bytes)

        if _remaining(deadline) <= 0:
            raise UpstreamTimeout("正文读完时已过总时限")
        if status >= 400:
            raise UpstreamHTTPError(status, reason, len(data),
                                    retry_after=resp_headers.get("retry-after"))
        if status >= 300:
            raise UpstreamTransportError(f"未跟随重定向：HTTP {status}")
        try:
            return json.loads(data.decode("utf-8", "replace"))
        except Exception as exc:
            raise UpstreamTransportError(f"响应不是 JSON（{len(data)} 字节）") from exc
    except UpstreamTimeout as exc:
        timing["deadline_hit"] = True
        timing["phase"] = timing.get("phase") or "timeout"
        raise UpstreamTimeout(f"{exc}（阶段 {timing['phase']}）") from exc
    except (UpstreamHTTPError, UpstreamTransportError):
        raise
    except Exception as exc:                # noqa: BLE001
        if _remaining(deadline) <= 0:
            timing["deadline_hit"] = True
            raise UpstreamTimeout(f"总时限 {total}s 到达（阶段 {timing.get('phase')}）") from exc
        raise UpstreamTransportError(f"{timing.get('phase')} 阶段 {type(exc).__name__}") from exc
    finally:
        try:
            if sock is not None:
                sock.close()
        except Exception:
            pass
        timing["elapsed_ms"] = round((time.monotonic() - started) * 1000.0, 1)
        timing["phase"] = timing.get("phase") or "closed"
        _TIMING.value = timing


def stream_sse(url: str, payload: dict, headers: dict | None = None,
               total_timeout: float | None = None,
               read_timeout: float | None = None):
    """POST 一个 SSE 流，边收边 yield 原始字节；到总时限抛 UpstreamTimeout。

    与 post_json 共用同一套「每次阻塞都有上界」的读法：慢速滴流的流不会被
    read(2048) 这种整块读钉死，消费方停止或超时都会关闭 socket。
    """
    total = float(total_timeout if total_timeout is not None
                  else _env_float("MEMORY_PROXY_TOTAL_TIMEOUT", 300.0))
    read = float(read_timeout if read_timeout is not None
                 else _env_float("MEMORY_LLM_READ_TIMEOUT", min(30.0, total)))
    if not math.isfinite(total) or total <= 0 or not math.isfinite(read) or read <= 0:
        raise UpstreamTransportError("upstream_invalid_timeout")
    parts = urlsplit(url)
    scheme = (parts.scheme or "http").lower()
    if scheme not in ("http", "https"):
        raise UpstreamTransportError(f"不支持的协议：{scheme}")
    host = parts.hostname or ""
    port = parts.port or (443 if scheme == "https" else 80)
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query

    started = time.monotonic()
    deadline = started + total
    timing = {"url": f"{scheme}://{host}:{port}{path}", "total_timeout": total,
              "read_timeout": read, "phase": "start", "elapsed_ms": 0.0,
              "bytes": 0, "status": None, "deadline_hit": False,
              "tls": scheme == "https", "proxy": None, "stream": True}
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    default_port = 443 if scheme == "https" else 80
    head = {"Host": host if port == default_port else f"{host}:{port}",
            "Content-Type": "application/json",
            "Content-Length": str(len(body)), "Accept": "text/event-stream",
            "User-Agent": "fish-memory/1.0"}
    for key, value in (headers or {}).items():
        if value is not None:
            head[str(key)] = str(value)

    sock = None
    try:
        proxy = _proxy_for(scheme, host)
        if proxy:
            timing["proxy"] = f"{proxy[0]}:{proxy[1]}"
            sock = _open_socket(proxy[0], proxy[1], deadline, read, timing)
            if scheme == "https":
                target = f"{host}:{port}"
                _send_all(sock, (f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n").encode(),
                          deadline, timing)
                reader = _Reader(sock, deadline, timing)
                status, _reason, _hdrs = _parse_head(reader.until(b"\r\n\r\n"))
                if status != 200:
                    raise UpstreamTransportError(f"代理 CONNECT 被拒：HTTP {status}")
                sock = _tls_wrap(sock, host, deadline, timing)
            request_target = url
        else:
            sock = _open_socket(host, port, deadline, read, timing)
            if scheme == "https":
                sock = _tls_wrap(sock, host, deadline, timing)
            request_target = path

        request = [f"POST {request_target} HTTP/1.1"]
        request += [f"{k}: {v}" for k, v in head.items()]
        request.append("Connection: close")
        _send_all(sock, ("\r\n".join(request) + "\r\n\r\n").encode("utf-8") + body, deadline, timing)

        reader = _Reader(sock, deadline, timing)
        status, reason, resp_headers = _parse_head(reader.until(b"\r\n\r\n"))
        timing["status"] = status
        if status >= 400:
            raise UpstreamHTTPError(status, reason, 0, retry_after=resp_headers.get("retry-after"))
        chunked = "chunked" in (resp_headers.get("transfer-encoding") or "").lower()
        if chunked:
            while True:
                size_line = reader.line("body")
                if not size_line:
                    continue
                size = int(size_line.split(b";")[0].strip() or b"0", 16)
                if size < 0:raise UpstreamTransportError("upstream_invalid_chunk_size")
                if size == 0:
                    break
                yield from reader.iter_exact(size)
                reader.exact(2)
        elif "content-length" in resp_headers:
            try:
                want = int(resp_headers["content-length"])
            except Exception:
                want = -1
            if want >= 0:
                yield from reader.iter_exact(want)
            else:
                yield from reader.iter_eof()
        else:
            # 无 Content-Length / Transfer-Encoding：正文到 EOF 结束。
            # until("\r\n\r\n") 已把头后面的正文留在 reader.buf 里，必须先发出去，
            # 否则「响应头与正文同一次写入」的对端会让我们静默丢掉首段。
            yield from reader.iter_eof()
    except UpstreamTimeout as exc:
        timing["deadline_hit"] = True
        raise UpstreamTimeout(f"{exc}（阶段 {timing.get('phase')}）") from exc
    finally:
        try:
            if sock is not None:
                sock.close()
        except Exception:
            pass
        timing["elapsed_ms"] = round((time.monotonic() - started) * 1000.0, 1)
        timing["phase"] = timing.get("phase") or "closed"
        _TIMING.value = timing
