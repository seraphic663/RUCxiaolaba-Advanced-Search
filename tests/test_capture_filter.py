import importlib.util
import json
import sys
import types
from pathlib import Path


class FakeLog:
    def __init__(self):
        self.messages = []

    def info(self, message):
        self.messages.append(("info", message))

    def error(self, message):
        self.messages.append(("error", message))


class FakeRequest:
    method = "GET"
    pretty_host = "ys.qimiaoyuanfen.com"
    pretty_url = "https://ys.qimiaoyuanfen.com/article/article/lists?page=1"
    pretty_path = "/article/article/lists"
    path = "/article/article/lists?page=1"
    headers = {"Cookie": "<redacted>"}
    content = b""

    def __init__(self, cookie, *, cookie_name="ys7_ysxy_session", parsed=True):
        self.cookies = {cookie_name: cookie} if parsed else {}
        self.headers = {"cookie": cookie_name + "=" + cookie}


class FakeResponse:
    status_code = 200
    headers = {"content-type": "application/json"}

    def __init__(self, payload):
        self.content = json.dumps(payload).encode("utf-8")


class FakeFlow:
    def __init__(self, cookie, payload, *, cookie_name="ys7_ysxy_session", parsed=True):
        self.request = FakeRequest(cookie, cookie_name=cookie_name, parsed=parsed)
        self.response = FakeResponse(payload)
        self.metadata = {}


def load_filter(monkeypatch, tmp_path, *, label=None):
    log = FakeLog()
    mitmproxy = types.ModuleType("mitmproxy")
    mitmproxy.ctx = types.SimpleNamespace(log=log)
    mitmproxy.http = types.SimpleNamespace(HTTPFlow=object)
    monkeypatch.setitem(sys.modules, "mitmproxy", mitmproxy)
    if label is not None:
        monkeypatch.setenv("RUC_CAPTURE_LABEL", label)

    path = Path(__file__).resolve().parents[1] / "tools" / "capture" / "mitm_filter.py"
    spec = importlib.util.spec_from_file_location("test_mitm_filter", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    module.COOKIE_FILE = tmp_path / "config_small.txt"
    module.CANDIDATE_FILE = tmp_path / "config_small.txt.candidate"
    module.STATUS_FILE = tmp_path / "capture_status.json"
    module.LOG_FILE = str(tmp_path / "captured_requests.jsonl")
    return module


def test_promotes_only_after_successful_api_response(monkeypatch, tmp_path):
    module = load_filter(monkeypatch, tmp_path)
    module.COOKIE_FILE.write_text("ys7_ysxy_session=old\n", encoding="utf-8")

    flow = FakeFlow("fresh", {"code": "0000", "data": {"list": []}})
    assert module.save_session_cookie(flow) is True
    assert "fresh" not in module.COOKIE_FILE.read_text(encoding="utf-8")

    module.response(flow)

    assert module.COOKIE_FILE.read_text(encoding="utf-8") == "ys7_ysxy_session=fresh\n"
    assert not module.CANDIDATE_FILE.exists()
    status = json.loads(module.STATUS_FILE.read_text(encoding="utf-8"))
    assert status["state"] == "validated"
    assert status["validated"] is True


def test_rejected_response_does_not_replace_existing_cookie(monkeypatch, tmp_path):
    module = load_filter(monkeypatch, tmp_path)
    module.COOKIE_FILE.write_text("ys7_ysxy_session=old\n", encoding="utf-8")

    flow = FakeFlow("bad", {"code": "7001", "message": "请先登录1"})
    assert module.save_session_cookie(flow) is True
    module.response(flow)

    assert module.COOKIE_FILE.read_text(encoding="utf-8") == "ys7_ysxy_session=old\n"
    status = json.loads(module.STATUS_FILE.read_text(encoding="utf-8"))
    assert status["state"] == "rejected"
    assert status["validated"] is False


def test_ignores_non_target_hosts(monkeypatch, tmp_path):
    module = load_filter(monkeypatch, tmp_path)
    flow = FakeFlow("fresh", {"code": "0000"})
    flow.request.pretty_host = "jw.ruc.edu.cn"

    assert module.is_api_request(flow) is False
    module.request(flow)

    assert not module.CANDIDATE_FILE.exists()
    assert not module.LOG_FILE or not (tmp_path / "captured_requests.jsonl").exists()


def test_reads_raw_cookie_header_when_parser_is_empty(monkeypatch, tmp_path):
    module = load_filter(monkeypatch, tmp_path)
    flow = FakeFlow("fresh", {"code": "0000"}, parsed=False)

    assert module.save_session_cookie(flow) is True
    module.response(flow)

    assert module.COOKIE_FILE.read_text(encoding="utf-8") == "ys7_ysxy_session=fresh\n"


def test_preserves_new_session_cookie_name(monkeypatch, tmp_path):
    module = load_filter(monkeypatch, tmp_path)
    flow = FakeFlow(
        "fresh",
        {"code": "0000"},
        cookie_name="ys_ysxy_sess",
    )

    assert module.save_session_cookie(flow) is True
    module.response(flow)

    assert module.COOKIE_FILE.read_text(encoding="utf-8") == "ys_ysxy_sess=fresh\n"


def test_records_requested_cookie_mode_without_value(monkeypatch, tmp_path):
    module = load_filter(monkeypatch, tmp_path, label="old")
    flow = FakeFlow("fresh", {"code": "0000"})

    module.save_session_cookie(flow)
    module.response(flow)

    status = json.loads(module.STATUS_FILE.read_text(encoding="utf-8"))
    assert status["cookie_label"] == "old"
    assert "fresh" not in module.STATUS_FILE.read_text(encoding="utf-8")
