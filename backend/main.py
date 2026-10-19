import base64
import json
import secrets
import json as json_module
import re
from datetime import datetime, timezone
from email.utils import parseaddr
from html.parser import HTMLParser
from threading import Lock
from contextlib import asynccontextmanager
from urllib.parse import unquote

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, StreamingResponse
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from pydantic import BaseModel
from sqlalchemy import delete, inspect, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from .config import settings
from .db import Base, SessionLocal, engine
from .local_models import classifier, reply_model
from .models import Draft, Email, OAuthCredential, utcnow

@asynccontextmanager
async def lifespan(_app):
    Base.metadata.create_all(engine)
    if "emails" in inspect(engine).get_table_names():
        columns = {column["name"] for column in inspect(engine).get_columns("emails")}
        with engine.begin() as connection:
            if "body_html" not in columns:
                connection.exec_driver_sql("ALTER TABLE emails ADD COLUMN body_html TEXT")
            if "body_html_checked" not in columns:
                connection.exec_driver_sql("ALTER TABLE emails ADD COLUMN body_html_checked BOOLEAN NOT NULL DEFAULT 0")
    try:
        yield
    finally:
        reply_model.stop()


app = FastAPI(title="Local Mail", version="0.1.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=[f"http://127.0.0.1:{settings.frontend_port}", f"http://localhost:{settings.frontend_port}"], allow_methods=["*"], allow_headers=["*"])
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly", "https://www.googleapis.com/auth/gmail.send"]
SYNC_LIMIT = 50
oauth_states: dict[str, str] = {}
oauth_lock = Lock()


class DraftInput(BaseModel):
    content: str


def _oauth_flow(state=None, code_verifier=None):
    if not settings.google_client_id or not settings.google_client_secret:
        raise HTTPException(503, "Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in .env to connect Gmail.")
    options = {}
    if state is not None:
        options.update(state=state, code_verifier=code_verifier, autogenerate_code_verifier=False)
    return Flow.from_client_config({"web": {"client_id": settings.google_client_id, "client_secret": settings.google_client_secret, "auth_uri": "https://accounts.google.com/o/oauth2/auth", "token_uri": "https://oauth2.googleapis.com/token", "redirect_uris": [settings.google_redirect_uri]}}, scopes=SCOPES, redirect_uri=settings.google_redirect_uri, **options)


def _oauth_failure_detail(exc):
    """Return a useful OAuth error without exposing response bodies or secrets."""
    error_code = None
    response = getattr(exc, "response", None)
    if response is not None:
        try:
            error_code = response.json().get("error")
        except (AttributeError, ValueError):
            pass
    error_code = error_code or type(exc).__name__

    if error_code in {"invalid_grant", "InvalidGrantError"}:
        return ("Google rejected the authorization code (invalid_grant). It may have expired or already been used. Start a fresh Gmail connection; if it repeats, verify the Google Console redirect URI exactly matches GOOGLE_REDIRECT_URI in .env.local.")
    if error_code in {"invalid_client", "InvalidClientError", "unauthorized_client"}:
        return "Google rejected the OAuth client. Verify GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET belong to the same Web application OAuth client."
    if error_code in {"redirect_uri_mismatch", "RedirectMismatchError"}:
        return "Google rejected the redirect URI. Make the Google Console URI exactly match GOOGLE_REDIRECT_URI in .env.local, including scheme, host, port, and path."
    if isinstance(exc, (TimeoutError, ConnectionError)) or type(exc).__name__ in {"ConnectTimeout", "ConnectionError", "ReadTimeout"}:
        return "The backend could not reach Google's token endpoint. Check backend internet access, then start a fresh Gmail connection."
    return f"Google token exchange failed ({type(exc).__name__}). Verify the OAuth Web client credentials and exact redirect URI, then start a fresh Gmail connection."


def _credentials(db):
    row = db.scalar(select(OAuthCredential).limit(1))
    if not row:
        raise HTTPException(401, "Gmail is not connected.")
    creds = Credentials.from_authorized_user_info(json.loads(row.token_json), SCOPES)
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(GoogleRequest())
            row.token_json = creds.to_json()
            db.commit()
        except Exception as exc:
            db.rollback()
            raise HTTPException(502, "Gmail authorization could not be refreshed. Reconnect Gmail in Settings.") from exc
    return creds


def _gmail_service(db):
    try:
        return build("gmail", "v1", credentials=_credentials(db), cache_discovery=False)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, "Could not initialize the Gmail API client.") from exc


class _EmailHTMLParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.text = []
        self.cid_images = set()

    def handle_data(self, data):
        if data.strip():
            self.text.append(data.strip())

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "img":
            src = dict(attrs).get("src") or ""
            if src.lower().startswith("cid:"):
                self.cid_images.add(unquote(src[4:]).strip("<>"))


def _decode_attachment_data(data):
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _decode_body(payload, attachment_fetcher=None):
    plain_chunks, html_chunks, image_parts = [], [], []

    def visit(part):
        mime = part.get("mimeType", "")
        body = part.get("body", {})
        data = body.get("data")
        if mime in {"text/plain", "text/html"}:
            if not data and body.get("attachmentId") and attachment_fetcher and body.get("size", 0) <= 500_000:
                data = attachment_fetcher(body["attachmentId"])
            if data:
                decoded = _decode_attachment_data(data).decode("utf-8", "replace")
                (plain_chunks if mime == "text/plain" else html_chunks).append(decoded)
        elif mime.startswith("image/") and mime != "image/svg+xml":
            headers = {header.get("name", "").lower(): header.get("value", "") for header in part.get("headers", [])}
            content_id = headers.get("content-id", "").strip("<>")
            if content_id:
                image_parts.append((content_id, mime, body, data))
        for child in part.get("parts", []):
            visit(child)

    visit(payload)
    html_body = "\n".join(html_chunks)[:300_000]
    html_parser = _EmailHTMLParser()
    if html_body:
        html_parser.feed(html_body)
    refs = {ref.casefold() for ref in html_parser.cid_images}
    inline_size = 0
    for content_id, mime, body, data in image_parts:
        if content_id.casefold() not in refs or inline_size >= 4_000_000:
            continue
        if body.get("size", 0) > 2_000_000:
            continue
        if not data and body.get("attachmentId") and attachment_fetcher:
            try:
                data = attachment_fetcher(body["attachmentId"])
            except Exception:
                continue
        if not data:
            continue
        try:
            binary = _decode_attachment_data(data)
        except (ValueError, TypeError):
            continue
        if len(binary) > 2_000_000 or inline_size + len(binary) > 4_000_000:
            continue
        inline_size += len(binary)
        uri = f"data:{mime};base64,{base64.b64encode(binary).decode('ascii')}"
        html_body = re.sub(r"(?i)cid:" + re.escape(content_id), uri, html_body)

    plain_body = "\n".join(plain_chunks).strip()
    if not plain_body and html_parser.text:
        plain_body = "\n".join(html_parser.text)
    return plain_body[:50_000], html_body or None


def _message(message, attachment_fetcher=None):
    payload = message.get("payload", {})
    headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
    sender, sender_email = parseaddr(headers.get("from", ""))
    try:
        received = datetime.fromtimestamp(int(message.get("internalDate", 0)) / 1000, timezone.utc)
    except (ValueError, TypeError, OSError):
        received = utcnow()
    body, body_html = _decode_body(payload, attachment_fetcher)
    return {"gmail_id": message["id"], "thread_id": message.get("threadId", ""), "sender": sender or sender_email, "sender_email": sender_email, "recipient": headers.get("to", ""), "subject": headers.get("subject", "(no subject)"), "body": body, "body_html": body_html, "body_html_checked": True, "received_at": received}


def _email_out(row, include_html=False):
    draft = row.drafts[0] if row.drafts else None
    output = {"id": row.id, "sender": row.sender, "sender_email": row.sender_email, "recipient": row.recipient, "subject": row.subject, "body": row.body, "received_at": row.received_at.isoformat(), "classification": row.classification, "classification_source": row.classification_source, "classification_status": row.classification_status, "draft": _draft_out(draft) if draft else None}
    if include_html:
        output["body_html"] = row.body_html
    return output


def _draft_out(row):
    return {"id": row.id, "content": row.content, "status": row.status, "source": row.source, "updated_at": row.updated_at.isoformat()}


@app.get("/api/status")
def status():
    with SessionLocal() as db:
        connected = db.scalar(select(OAuthCredential.id).limit(1)) is not None
    return {"gmail": {"connected": connected, "configured": bool(settings.google_client_id and settings.google_client_secret)}, "classifier": classifier.status(), "reply_model": reply_model.status()}


@app.get("/api/auth/start")
def auth_start():
    flow = _oauth_flow()
    state = secrets.token_urlsafe(24)
    flow.redirect_uri = settings.google_redirect_uri
    url, _ = flow.authorization_url(access_type="offline", prompt="consent", include_granted_scopes="true", state=state)
    with oauth_lock:
        oauth_states[state] = flow.code_verifier
    return {"url": url}


@app.get("/api/auth/callback")
@app.get("/api/gmail/callback")
def auth_callback(state: str, code: str):
    with oauth_lock:
        code_verifier = oauth_states.pop(state, None)
        if not code_verifier:
            raise HTTPException(400, "OAuth state is invalid or expired. Start the Gmail connection again.")
    flow = _oauth_flow(state, code_verifier)
    try:
        flow.fetch_token(code=code)
        token_json = flow.credentials.to_json()
    except Exception as exc:
        raise HTTPException(502, _oauth_failure_detail(exc)) from None
    with SessionLocal() as db:
        row = db.get(OAuthCredential, 1)
        if row:
            row.token_json = token_json
        else:
            db.add(OAuthCredential(id=1, token_json=token_json))
        db.commit()
    return RedirectResponse(f"http://127.0.0.1:{settings.frontend_port}/?connected=1")


@app.get("/api/emails")
def list_emails():
    with SessionLocal() as db:
        rows = db.scalars(select(Email).order_by(Email.received_at.desc()).limit(250)).all()
        return [_email_out(row) for row in rows]


@app.get("/api/emails/{email_id}")
def get_email(email_id: int):
    with SessionLocal() as db:
        row = db.get(Email, email_id)
        if not row:
            raise HTTPException(404, "Email not found in local data.")
        gmail_id = row.gmail_id if not row.body_html_checked else None

    # Older cached rows contain only plain text. Fetch their rich body once on
    # demand, without holding a SQLite transaction during Gmail network calls.
    if gmail_id:
        try:
            with SessionLocal() as db:
                service = _gmail_service(db)
            raw = service.users().messages().get(userId="me", id=gmail_id, format="full").execute()
            def fetch_attachment(attachment_id):
                return service.users().messages().attachments().get(userId="me", messageId=gmail_id, id=attachment_id).execute().get("data", "")
            refreshed = _message(raw, fetch_attachment)
            with SessionLocal.begin() as db:
                row = db.get(Email, email_id)
                if row and not row.body_html_checked:
                    row.body_html = refreshed["body_html"]
                    row.body_html_checked = True
                    if not row.body:
                        row.body = refreshed["body"]
        except Exception:
            # The cached plain-text copy remains readable if Gmail is offline.
            pass
    with SessionLocal() as db:
        row = db.get(Email, email_id)
        if not row:
            raise HTTPException(404, "Email not found in local data.")
        return _email_out(row, include_html=True)


def _sync_events():
    """Yield progress while persisting each newly classified message briefly."""
    def event(payload):
        return f"data: {json_module.dumps(payload)}\n\n"

    try:
        with SessionLocal() as db:
            service = _gmail_service(db)
        page = service.users().messages().list(userId="me", labelIds=["INBOX"], maxResults=SYNC_LIMIT).execute()
        refs = page.get("messages", [])
        with SessionLocal() as db:
            existing = set(db.scalars(select(Email.gmail_id).where(Email.gmail_id.in_([r["id"] for r in refs]))).all()) if refs else set()
    except HTTPException:
        raise
    except Exception as exc:
        yield event({"type": "error", "message": "Gmail inbox fetch failed. Check your connection and Gmail authorization."})
        return
    new_refs = [ref for ref in refs if ref["id"] not in existing]
    yield event({"type": "start", "total": len(new_refs), "already_cached": len(refs) - len(new_refs)})
    errors = []
    stored = 0
    for index, ref in enumerate(new_refs, 1):
        yield event({"type": "progress", "processed": index - 1, "total": len(new_refs), "message": "Fetching message…"})
        try:
            raw = service.users().messages().get(userId="me", id=ref["id"], format="full").execute()
            def fetch_attachment(attachment_id):
                return service.users().messages().attachments().get(userId="me", messageId=ref["id"], id=attachment_id).execute().get("data", "")
            data = _message(raw, fetch_attachment)
            yield event({"type": "progress", "processed": index - 1, "total": len(new_refs), "message": f"Classifying: {data['subject']}"})
            try:
                data["classification"], data["classification_source"] = classifier.classify(data["subject"], data["body"])
                data["classification_status"] = "classified"
            except Exception as exc:
                data["classification"] = None
                data["classification_source"] = None
                data["classification_status"] = "failed"
                errors.append(f"Classification failed for {data['subject']}: {exc}")
            with SessionLocal.begin() as db:
                result = db.execute(sqlite_insert(Email).values(**data).on_conflict_do_nothing(index_elements=["gmail_id"]))
                was_stored = bool(result.rowcount)
            stored += int(was_stored)
            if was_stored:
                with SessionLocal() as db:
                    saved = db.scalar(select(Email).where(Email.gmail_id == data["gmail_id"]))
                    item = _email_out(saved)
                yield event({"type": "email", "processed": index, "total": len(new_refs), "email": item, "message": "Saved and classified" if item["classification_status"] == "classified" else "Saved · classification failed"})
            else:
                yield event({"type": "progress", "processed": index, "total": len(new_refs), "message": f"Already saved: {data['subject']}"})
        except Exception:
            errors.append("A Gmail message could not be fetched or saved locally; retry sync to try again.")
            yield event({"type": "item_error", "processed": index, "total": len(new_refs), "message": errors[-1]})
    yield event({"type": "complete", "fetched": len(new_refs) - sum("could not be fetched or saved" in error for error in errors), "stored": stored, "errors": errors})


@app.post("/api/sync")
def sync():
    return StreamingResponse(_sync_events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/emails/{email_id}/reclassify")
def reclassify(email_id: int):
    with SessionLocal() as db:
        row = db.get(Email, email_id)
        if not row:
            raise HTTPException(404, "Email not found in local data.")
        subject, body = row.subject, row.body
    try:
        label, source = classifier.classify(subject, body)
    except Exception as exc:
        raise HTTPException(503, str(exc)) from exc
    try:
        with SessionLocal.begin() as db:
            row = db.get(Email, email_id)
            if not row:
                raise HTTPException(404, "Email was deleted while classification was running.")
            row.classification, row.classification_source, row.classification_status = label, source, "classified"
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, "Classification completed locally, but its result could not be saved to SQLite.") from exc
    return {"classification": label, "source": source, "status": "classified"}


@app.post("/api/emails/{email_id}/draft/generate")
def generate_draft(email_id: int):
    with SessionLocal() as db:
        row = db.get(Email, email_id)
        if not row:
            raise HTTPException(404, "Email not found in local data.")
        subject, sender, body, recipient = row.subject, row.sender, row.body, row.sender_email
    try:
        content, source = reply_model.generate(subject, sender, body)
    except Exception as exc:
        raise HTTPException(503, str(exc)) from exc
    try:
        with SessionLocal.begin() as db:
            if not db.get(Email, email_id):
                raise HTTPException(404, "Email was deleted while the draft was generating.")
            draft = db.scalar(select(Draft).where(Draft.email_id == email_id))
            statement = sqlite_insert(Draft).values(email_id=email_id, content=content, status="generated", source=source)
            db.execute(statement.on_conflict_do_update(index_elements=["email_id"], set_={"content": content, "status": "generated", "source": source, "updated_at": utcnow()}))
            draft = db.scalar(select(Draft).where(Draft.email_id == email_id))
            db.flush()
            result = _draft_out(draft)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, "Qwen generated a draft, but it could not be saved to SQLite.") from exc
    return result


@app.put("/api/emails/{email_id}/draft")
def save_draft(email_id: int, payload: DraftInput):
    if not payload.content.strip():
        raise HTTPException(400, "Draft cannot be empty.")
    try:
        with SessionLocal.begin() as db:
            if not db.get(Email, email_id):
                raise HTTPException(404, "Email not found in local data.")
            draft = db.scalar(select(Draft).where(Draft.email_id == email_id))
            if not draft:
                draft = Draft(email_id=email_id, content=payload.content, status="saved", source="user-edited")
                db.add(draft)
            else:
                draft.content, draft.status = payload.content, "saved"
            db.flush()
            result = _draft_out(draft)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, "Draft could not be saved to local SQLite data.") from exc
    return result


@app.post("/api/emails/{email_id}/draft/approve")
def approve_draft(email_id: int):
    with SessionLocal.begin() as db:
        draft = db.scalar(select(Draft).where(Draft.email_id == email_id))
        if not draft or not draft.content.strip():
            raise HTTPException(404, "Save a non-empty draft before approving it.")
        draft.status = "approved"
        result = _draft_out(draft)
    return result


@app.delete("/api/emails/{email_id}/draft")
def discard_draft(email_id: int):
    with SessionLocal.begin() as db:
        db.execute(delete(Draft).where(Draft.email_id == email_id))
    return {"discarded": True}


@app.post("/api/emails/{email_id}/send")
def send_email(email_id: int):
    with SessionLocal() as db:
        row = db.get(Email, email_id)
        if not row:
            raise HTTPException(404, "Email not found in local data.")
        draft = db.scalar(select(Draft).where(Draft.email_id == email_id))
        if not draft or draft.status != "approved" or not draft.content.strip():
            raise HTTPException(400, "Save and approve a non-empty draft before sending.")
        recipient, subject, content, thread_id = row.sender_email, row.subject, draft.content, row.thread_id
        service = _gmail_service(db)
    if not recipient:
        raise HTTPException(400, "The original sender address is unavailable.")
    from email.message import EmailMessage
    message = EmailMessage()
    message["To"], message["Subject"] = recipient, "Re: " + subject
    message.set_content(content)
    encoded = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
    try:
        message_body = {"raw": encoded}
        if thread_id:
            message_body["threadId"] = thread_id
        service.users().messages().send(userId="me", body=message_body).execute()
    except Exception as exc:
        raise HTTPException(502, "Gmail did not confirm the message was sent. Check Gmail before retrying.") from exc
    try:
        with SessionLocal.begin() as db:
            draft = db.scalar(select(Draft).where(Draft.email_id == email_id))
            if draft:
                draft.status = "sent"
    except Exception as exc:
        raise HTTPException(500, "Gmail confirmed the message was sent, but the local sent status could not be saved.") from exc
    return {"sent": True}


@app.delete("/api/local-data")
def delete_local_data():
    with SessionLocal.begin() as db:
        db.execute(delete(Draft))
        db.execute(delete(Email))
    return {"deleted": True, "gmail_connection_preserved": True}
