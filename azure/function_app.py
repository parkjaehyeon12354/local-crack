"""크랙빌더 — Azure Functions 다리.

★**server.py 를 고쳐 쓰지 않는다.** 그 파일은 `BaseHTTPRequestHandler` 로 도는
  완성된 서버이고, 여기서는 요청을 그 핸들러에 **그대로 흘려 넣고** 답을 받아
  Functions 응답으로 옮기기만 한다. 라우트가 늘어도 이 파일은 그대로다.

★왜 화면(HTML)까지 여기서 내주나: 화면과 API 가 **같은 주소**여야
  `fetch('/api/...')` 를 한 글자도 안 고치고, CORS 설정도 필요 없다.
  주소가 갈리면 페이지마다 절대 주소를 박아야 한다 (지난 배포에서 그랬다).

★저장은 Blob 이다 — `CRACK_BLOB` 이 있으면 server.py 가 알아서 BlobStore 를 쓴다.
"""
import io
import logging
import os
import sys
from pathlib import Path

import azure.functions as func

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

# ★import 하기 전에 켠다 — server.py 는 모듈을 읽는 동안 store 를 정한다.
os.environ.setdefault("CRACK_DATA", "/tmp/crack")

import server  # noqa: E402

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)
log = logging.getLogger("crack")

# ★모델 목록은 로컬에서 `main()` 이 기동할 때 채운다. Functions 는 main 을
#   부르지 않아 목록이 계속 비어 있었다(제공사 0개). 첫 요청에 한 번 채운다.
_ready = False


def _boot():
    global _ready
    if _ready:
        return
    _ready = True                     # 실패해도 매 요청마다 다시 긁지 않는다
    try:
        server.refresh_models()
    except Exception:
        log.exception("모델 목록을 못 받았다")


class _Sock:
    """핸들러가 소켓인 줄 알고 쓰는 가짜 통로.

    ★`BaseHTTPRequestHandler` 는 `rfile`/`wfile` 로만 바깥과 말한다.
      그 둘을 메모리 버퍼로 바꾸면 소켓 없이도 그대로 돈다.
    """

    def __init__(self, body):
        self.rfile = io.BytesIO(body)
        self.wfile = io.BytesIO()

    def makefile(self, mode, *a, **kw):
        return self.rfile if "r" in mode else self.wfile


class _Handler(server.Handler):
    """소켓 대신 버퍼에 답을 쓴다. 로직은 손대지 않는다."""

    def __init__(self, req_line, headers, body):
        self._sock = _Sock(body)
        self.rfile = self._sock.rfile
        self.wfile = self._sock.wfile
        self.requestline = req_line
        self.client_address = ("127.0.0.1", 0)
        self.server = None
        self.command, self.path, self.request_version = req_line.split(" ")
        self.headers = headers
        self.close_connection = True
        # ★부모 __init__ 을 부르지 않는다 — 그쪽이 소켓을 받아 곧바로 처리를 시작한다.
        self.directory = str(HERE)

    def log_message(self, fmt, *a):
        log.info(fmt, *a)

    def send_response(self, code, message=None):
        # 날짜·서버 머리글은 Functions 가 붙인다
        self.send_response_only(code, message)


def _run(req: func.HttpRequest) -> func.HttpResponse:
    import email.parser
    _boot()

    body = req.get_body() or b""
    path = "/" + req.url.split("/", 3)[-1] if "://" in req.url else req.url
    if not path.startswith("/"):
        path = "/" + path

    raw = "\r\n".join(f"{k}: {v}" for k, v in req.headers.items()) + "\r\n\r\n"
    headers = email.parser.Parser().parsestr(raw)

    h = _Handler(f"{req.method} {path} HTTP/1.1", headers, body)
    try:
        getattr(h, "do_" + req.method)()
    except AttributeError:
        return func.HttpResponse("Method Not Allowed", status_code=405)
    except Exception as e:                      # 한 요청이 죽어도 앱은 살아야 한다
        log.exception("handler failed")
        return func.HttpResponse(f"서버 오류: {e}", status_code=500)

    out = h.wfile.getvalue()
    head, _, payload = out.partition(b"\r\n\r\n")
    lines = head.decode("latin1").split("\r\n")
    status = int(lines[0].split(" ")[1]) if lines and " " in lines[0] else 200
    hdrs = {}
    for ln in lines[1:]:
        if ": " in ln:
            k, v = ln.split(": ", 1)
            if k.lower() not in ("transfer-encoding", "connection", "content-length"):
                hdrs[k] = v
    return func.HttpResponse(payload, status_code=status, headers=hdrs)


@app.route(route="{*path}", methods=["GET", "POST", "PUT", "DELETE"])
def all_routes(req: func.HttpRequest) -> func.HttpResponse:
    return _run(req)
