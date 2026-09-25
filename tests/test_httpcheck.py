import http.server
import threading
import time

import pytest

from nomad.httpcheck import HttpResult, certificate_from_dict, check_url, check_with_redirects, normalize_url


@pytest.mark.parametrize("text, expected", [("example.com", "https://example.com/"),
                                            ("http://10.0.0.1:8080", "http://10.0.0.1:8080/"),
                                            (" https://host/path?q=1 ", "https://host/path?q=1")])
def test_normalize_url(text, expected):
    assert normalize_url(text) == expected


@pytest.mark.parametrize("text, message", [("", "Enter a web address"), ("ftp://host", "Only http"),
                                           ("https://", "doesn't include a host"),
                                           ("https://host:99999", "invalid port")])
def test_normalize_url_rejects(text, message):
    with pytest.raises(ValueError, match=message):
        normalize_url(text)


def test_certificate_from_dict():
    info = {"subject": ((("commonName", "router.lan"),),),
            "issuer": ((("domainComponent", "lan"),), (("commonName", "Lab CA"),)),
            "subjectAltName": (("DNS", "router.lan"), ("IP Address", "10.0.0.1")),
            "notAfter": "Jan  1 00:00:00 2030 GMT", "serialNumber": "01"}
    now = time.mktime((2029, 12, 22, 0, 0, 0, 0, 0, 0)) - time.timezone
    certificate = certificate_from_dict(info, now=now)
    assert certificate.subject == "CN=router.lan" and certificate.issuer == "DC=lan, CN=Lab CA"
    assert certificate.names == ["router.lan", "IP Address:10.0.0.1"]
    assert certificate.days_left == 10 and not certificate.self_signed
    info["issuer"] = info["subject"]
    assert certificate_from_dict(info, now=now).self_signed


def test_redirects_are_followed_and_loops_stop():
    hops = {"http://a/": HttpResult("http://a/", status=301, location="https://a/"),
            "https://a/": HttpResult("https://a/", status=302, location="https://a/login"),
            "https://a/login": HttpResult("https://a/login", status=200)}
    results = check_with_redirects("http://a/", check=lambda url, timeout: hops[url])
    assert [result.url for result in results] == ["http://a/", "https://a/", "https://a/login"]
    loop = {"http://b/": HttpResult("http://b/", status=302, location="http://b/")}
    assert len(check_with_redirects("http://b/", check=lambda url, timeout: loop[url])) == 1


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/old":
            self.send_response(301)
            self.send_header("Location", "/new")
            self.end_headers()
            return
        body = b"hello"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def test_check_url_against_a_local_server():
    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        results = check_with_redirects(base + "/old", timeout=5)
    finally:
        server.shutdown()
    assert [result.status for result in results] == [301, 200]
    assert results[0].location == base + "/new"
    final = results[-1]
    assert final.error == "" and final.connect_ms is not None and final.first_byte_ms is not None
    assert final.verified is None and final.dns_ms is None  # Plain HTTP to an address


def test_check_url_reports_refused_connection():
    import socket
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]  # Nothing listening once closed
    result = check_url(f"http://127.0.0.1:{port}/", timeout=5)
    assert "refused" in result.error and result.status is None
