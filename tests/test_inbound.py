import telegram.inbound as inbound
from telegram.inbound import Command, Plain, Reply, classify, fetch_events


def test_classify_reply():
    assert classify("use oauth", 55) == Reply(reply_to_msg_id=55, text="use oauth")


def test_classify_commands():
    assert classify("/status", 0) == Command(name="status", issue=0)
    assert classify("/attach 42", 0) == Command(name="attach", issue=42)
    assert classify("/attach", 0) is None
    assert classify("/attach x", 0) is None


def test_classify_plain():
    assert classify("go ahead", 0) == Plain(text="go ahead")


def _updates_payload():
    return {"result": [
        {"update_id": 10, "message": {"message_id": 1, "text": "/status",
         "chat": {"id": 777}}},
        {"update_id": 11, "message": {"message_id": 2, "text": "yes",
         "chat": {"id": 777}, "reply_to_message": {"message_id": 55}}},
        {"update_id": 12, "message": {"message_id": 3, "text": "intruder",
         "chat": {"id": 999}}},  # wrong chat: dropped
    ]}


def test_fetch_events_classifies_and_advances_offset(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "T")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "777")
    seen_urls = []
    monkeypatch.setattr(inbound, "_http_get",
                        lambda url: (seen_urls.append(url), _updates_payload())[1])
    events = fetch_events(tmp_path)
    assert events == [Command(name="status", issue=0),
                      Reply(reply_to_msg_id=55, text="yes")]
    assert (tmp_path / "telegram-offset").read_text() == "13"
    assert "offset=" in seen_urls[0]


def test_fetch_events_without_env_is_empty(tmp_path, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    assert fetch_events(tmp_path) == []
