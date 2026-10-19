from datetime import datetime, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import main
from backend.db import Base
from backend.models import Email


@pytest.fixture
def database(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.sqlite'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(main, "engine", engine)
    monkeypatch.setattr(main, "SessionLocal", sessions)
    with sessions.begin() as db:
        db.add(Email(gmail_id="synthetic-1", sender="Alex", sender_email="alex@example.test", recipient="me@example.test", subject="Lunch tomorrow?", body="Can we meet for lunch tomorrow?", body_html="<p>Can we meet for lunch tomorrow?</p>", body_html_checked=True, received_at=datetime.now(timezone.utc)))
    yield
    engine.dispose()


def test_reclassify_uses_classifier_component(database, monkeypatch):
    monkeypatch.setattr(main.classifier, "classify", lambda _subject, _body: ("personal", "local:test-classifier"))
    assert main.reclassify(1) == {"classification": "personal", "source": "local:test-classifier", "status": "classified"}
    assert main.get_email(1)["classification_source"] == "local:test-classifier"


def test_generated_draft_persists_and_reload_does_not_send(database, monkeypatch):
    monkeypatch.setattr(main.reply_model, "generate", lambda *_args: ("Thanks, lunch tomorrow works for me.", "local-qwen:synthetic"))
    monkeypatch.setattr(main, "_gmail_service", lambda _db: pytest.fail("Draft generation must not access Gmail send."))
    generated = main.generate_draft(1)
    assert generated["content"]
    reloaded = main.get_email(1)["draft"]
    assert reloaded["content"] == generated["content"]
    assert reloaded["source"] == "local-qwen:synthetic"
    assert reloaded["status"] == "generated"


def test_sending_requires_approval_and_separate_send_action(database, monkeypatch):
    main.save_draft(1, main.DraftInput(content="See you tomorrow."))
    with pytest.raises(HTTPException) as exc:
        main.send_email(1)
    assert exc.value.status_code == 400

    class SendCall:
        def execute(self):
            return {"id": "synthetic-sent-id"}

    class Messages:
        def send(self, **_kwargs):
            return SendCall()

    class Users:
        def messages(self):
            return Messages()

    class Gmail:
        def users(self):
            return Users()

    called = []
    monkeypatch.setattr(main, "_gmail_service", lambda _db: (called.append(True) or Gmail()))
    assert main.approve_draft(1)["status"] == "approved"
    assert called == []
    assert main.send_email(1) == {"sent": True}
    assert called == [True]


def test_oauth_error_detail_is_actionable_without_echoing_response_body():
    class Response:
        def json(self):
            return {"error": "invalid_grant", "error_description": "private response detail"}

    class ExchangeError(Exception):
        response = Response()

    detail = main._oauth_failure_detail(ExchangeError())
    assert "invalid_grant" in detail
    assert "Start a fresh Gmail connection" in detail
    assert "private response detail" not in detail


def test_oauth_callback_restores_pkce_verifier(database, monkeypatch):
    state = "test-oauth-state"
    verifier = "test-pkce-verifier"
    captured = []

    class Credentials:
        def to_json(self):
            return '{"token":"synthetic-test-token"}'

    class Flow:
        credentials = Credentials()

        def fetch_token(self, *, code):
            assert code == "synthetic-test-code"

    def create_flow(actual_state, actual_verifier):
        captured.append((actual_state, actual_verifier))
        return Flow()

    monkeypatch.setattr(main, "_oauth_flow", create_flow)
    with main.oauth_lock:
        main.oauth_states[state] = verifier

    response = main.auth_callback(state=state, code="synthetic-test-code")
    assert response.status_code == 307
    assert captured == [(state, verifier)]
    assert state not in main.oauth_states


def test_email_detail_includes_rich_body_without_sending_html_in_inbox_list(database):
    assert "body_html" not in main.list_emails()[0]
    assert main.get_email(1)["body_html"] == "<p>Can we meet for lunch tomorrow?</p>"


def test_html_email_extracts_text_and_inlines_local_cid_images():
    def encoded(value):
        import base64
        return base64.urlsafe_b64encode(value).decode().rstrip("=")

    payload = {"mimeType": "multipart/related", "parts": [
        {"mimeType": "text/html", "body": {"data": encoded(b"<h1>Hello</h1><img src='cid:logo'>")}},
        {"mimeType": "image/png", "headers": [{"name": "Content-ID", "value": "<logo>"}], "body": {"data": encoded(b"synthetic-image")}},
    ]}
    plain, html = main._decode_body(payload)
    assert plain == "Hello"
    assert "src='data:image/png;base64," in html


def test_sync_requests_only_the_50_newest_inbox_messages(database, monkeypatch):
    class Call:
        def execute(self):
            return {"messages": []}

    class Messages:
        def list(self, **kwargs):
            assert kwargs == {"userId": "me", "labelIds": ["INBOX"], "maxResults": 50}
            return Call()

    class Users:
        def messages(self):
            return Messages()

    class Gmail:
        def users(self):
            return Users()

    monkeypatch.setattr(main, "_gmail_service", lambda _db: Gmail())
    events = list(main._sync_events())
    assert len(events) == 2
    assert '"total": 0' in events[0]
    assert '"type": "complete"' in events[1]
