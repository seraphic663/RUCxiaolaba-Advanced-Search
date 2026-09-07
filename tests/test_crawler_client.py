from crawler.client import MiniProgramClient, load_cookie


def test_load_cookie_accepts_current_session_name(tmp_path):
    config = tmp_path / "config.txt"
    config.write_text("ys_ysxy_sess=current-session\n", encoding="utf-8")

    assert load_cookie(config) == "current-session"


def test_client_sends_both_supported_session_names():
    client = MiniProgramClient("current-session")
    try:
        cookies = client.session.cookies.get_dict()
    finally:
        client.session.close()

    assert cookies["ys7_ysxy_session"] == "current-session"
    assert cookies["ys_ysxy_sess"] == "current-session"
