from base64 import b64decode
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, HTTPServer
from io import BytesIO
import json
from threading import Thread

from PIL import Image
import pytest
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader

from d2md import cli
from d2md.errors import ConversionError
from d2md.typhoon import MODEL, local_endpoint


FIRST = "ข้อความภาษาไทยจากหน้าที่หนึ่ง"
SECOND = "ข้อความภาษาไทยจากหน้าที่สอง"


@pytest.fixture
def ollama(monkeypatch):
    state = {"posts": [], "fail_second": True, "digest": "one"}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send_json(self, response):
            data = json.dumps(response).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            assert self.path == "/api/tags"
            self.send_json({
                "models": [{"name": f"{MODEL}:latest", "digest": state["digest"]}],
            })

        def do_POST(self):
            assert self.path == "/api/chat"
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["posts"].append(body)
            if state.get("redirect"):
                self.send_response(307)
                self.send_header("Location", "/unexpected")
                self.end_headers()
                return
            with Image.open(BytesIO(b64decode(body["messages"][0]["images"][0]))) as image:
                assert max(image.size) <= 1536
                first = image.getpixel((0, 0))[0] > 128
            length_limited = not first and state["fail_second"]
            self.send_json({
                "done": True,
                "done_reason": "length" if length_limited else "stop",
                "message": {"content": FIRST if first else SECOND},
            })

    with HTTPServer(("127.0.0.1", 0), Handler) as server:
        monkeypatch.setenv("OLLAMA_HOST", f"localhost:{server.server_port}")
        monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield state
        finally:
            server.shutdown()
            thread.join()


@pytest.fixture
def scanned_pdf(tmp_path):
    source = tmp_path / "scan.pdf"
    document = canvas.Canvas(str(source), pagesize=(820, 400))
    for color in ("white", "black"):
        with Image.new("RGB", (820, 400), color) as image:
            document.drawImage(ImageReader(image), 0, 0)
        document.showPage()
    document.save()
    return source


def test_cli_resumes_a_failed_scan(scanned_pdf, ollama, tmp_path, capsys):
    cache = tmp_path / "cache"
    out = tmp_path / "out"
    args = [
        str(scanned_pdf), "--ocr", "--ocr-engine", "typhoon",
        "--ocr-cache", str(cache), "--json", "-o", str(out),
    ]

    assert cli.main(args) == 1
    failed = json.loads(capsys.readouterr().out)
    assert "Typhoon page 2" in failed["errors"][0]["message"]
    assert not (out / "scan.md").exists()
    assert len(list(cache.glob("*.cache"))) == 1
    assert len(ollama["posts"]) == 2
    post = ollama["posts"][0]
    assert post["model"] == MODEL
    assert not post["stream"]
    assert post["options"]["temperature"] == 0.1

    ollama["fail_second"] = False
    assert cli.main(args) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["results"][0]["backend"] == "typhoon"
    assert len(ollama["posts"]) == 3
    markdown = (out / "scan.md").read_text(encoding="utf-8")
    assert markdown.index("<!-- Page number: 1 -->") < markdown.index(FIRST)
    assert markdown.index(FIRST) < markdown.index("<!-- Page number: 2 -->")
    assert markdown.index("<!-- Page number: 2 -->") < markdown.index(SECOND)

    ollama["digest"] = "two"
    assert cli.main([*args, "--force"]) == 0
    assert len(ollama["posts"]) == 5


def test_text_pdf_does_not_connect(multi_page_text_pdf, monkeypatch, capsys):
    monkeypatch.setenv("OLLAMA_HOST", "https://example.com")
    assert cli.main([
        str(multi_page_text_pdf), "--ocr", "--ocr-engine", "typhoon", "--stdout",
    ]) == 0
    assert "Page 2 of the specification document." in capsys.readouterr().out


def test_input_size_override_keeps_page_limit(scanned_pdf, monkeypatch, capsys):
    monkeypatch.setattr(
        cli, "DEFAULT_LIMITS", replace(cli.DEFAULT_LIMITS, max_input_bytes=64, max_pdf_pages=1)
    )
    args = [str(scanned_pdf), "--ocr", "--ocr-engine", "typhoon", "--stdout"]
    assert cli.main(args) == 1
    assert "input limit exceeded" in capsys.readouterr().err
    assert cli.main([*args, "--max-input-mib", "1"]) == 1
    assert "PDF page limit exceeded" in capsys.readouterr().err


def test_redirect_is_rejected(scanned_pdf, ollama, capsys):
    ollama["redirect"] = True
    assert cli.main([
        str(scanned_pdf), "--ocr", "--ocr-engine", "typhoon", "--stdout",
    ]) == 1
    assert "Ollama returned HTTP 307" in capsys.readouterr().err
    assert len(ollama["posts"]) == 1


def test_cache_refuses_a_symlink(tmp_path):
    target = tmp_path / "target"
    target.write_text('"outside the cache"', encoding="utf-8")
    cache = tmp_path / "cache"
    cache.mkdir()
    key = "a" * 64
    try:
        (cache / f"typhoon-{key}.cache").symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("creating symlinks is unavailable")

    with pytest.raises(ConversionError, match="cache entry must be a regular file"):
        cli._cached_ocr_page(cache, key, lambda: pytest.fail("Ollama called"))
    assert target.read_text(encoding="utf-8") == '"outside the cache"'


@pytest.mark.parametrize("endpoint", [
    "192.0.2.1:11434", "https://localhost:11434", "http://localhost@192.0.2.1:80",
    "127.0.0.1:0", "http://127.0.0.1:11434/api/chat",
])
def test_endpoint_requires_direct_loopback(endpoint, monkeypatch):
    monkeypatch.setenv("OLLAMA_HOST", endpoint)
    with pytest.raises(ConversionError, match="loopback"):
        local_endpoint()


@pytest.mark.parametrize("options", [
    ["--ocr-engine", "typhoon"],
    ["--ocr", "--ocr-engine", "typhoon", "--docling"],
    ["--ocr", "--ocr-engine", "typhoon", "--lang", "thai"],
    ["--ocr", "--ocr-cache", "cache"],
    ["--max-input-mib", "0"],
    ["--max-input-mib", "150", "--unsafe-unlimited"],
])
def test_invalid_options_are_refused(options):
    with pytest.raises(SystemExit) as exc:
        cli.main(["scan.pdf", *options])
    assert exc.value.code == 2
