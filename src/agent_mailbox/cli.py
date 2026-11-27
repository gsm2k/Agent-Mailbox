"""Console client and identity-file handling shared with the MCP bridge."""

import argparse
import json
import os
import secrets
import sys
from pathlib import Path
from tempfile import NamedTemporaryFile
from uuid import UUID

import httpx


class MailboxError(Exception):
    pass


class MailboxClient:
    def __init__(self, port: int, identity: Path):
        self.base_url = f"http://127.0.0.1:{port}"
        self.identity = identity

    def _load_identity(self) -> dict:
        try:
            return json.loads(self.identity.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise MailboxError(f"Register an identity first: {self.identity}") from exc

    def _request(self, method: str, path: str, **kwargs):
        token = self._load_identity()["token"]
        try:
            with httpx.Client(
                base_url=self.base_url,
                headers={"Authorization": f"Bearer {token}"},
                timeout=30,
                trust_env=False,
            ) as client:
                response = client.request(method, path, **kwargs)
                response.raise_for_status()
                return response.json()
        except httpx.HTTPStatusError as exc:
            try:
                detail = exc.response.json().get("detail", exc.response.reason_phrase)
            except (ValueError, AttributeError):
                detail = exc.response.reason_phrase
            raise MailboxError(f"{exc.response.status_code}: {detail}") from exc
        except httpx.RequestError as exc:
            raise MailboxError(f"Request to {self.base_url} failed: {exc}") from exc

    def register(self, name: str):
        self.identity.parent.mkdir(parents=True, exist_ok=True)
        # Publish a complete identity file without replacing another session's key.
        if not self.identity.exists():
            with NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.identity.parent, delete=False,
            ) as file:
                staged = Path(file.name)
                json.dump({"name": name, "token": secrets.token_urlsafe(32)}, file)
            try:
                os.link(staged, self.identity)
            except FileExistsError:
                pass
            finally:
                staged.unlink()
        if self._load_identity()["name"] != name:
            raise MailboxError("This identity file belongs to a different name")
        return self._request("POST", "/sessions", json={"name": name})

    def send(self, to: str, text: str, request_id: UUID | None = None):
        data = {"to": to, "text": text}
        if request_id is not None:
            data["request_id"] = str(request_id)
        return self._request("POST", "/messages", json=data)

    def receive(self, wait: float = 0):
        return self._request("GET", "/inbox", params={"wait": wait})

    def ack(self, message_id: int):
        return self._request("POST", f"/inbox/{message_id}/ack")


def add_client_options(parser: argparse.ArgumentParser):
    parser.add_argument("--port", type=int, default=8765, help="Local mailbox port")
    parser.add_argument(
        "--identity",
        type=Path,
        default=os.getenv("AGENT_MAILBOX_IDENTITY"),
        help="A private JSON file for this session's credentials",
    )


def main():
    parser = argparse.ArgumentParser()
    add_client_options(parser)
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve")
    serve.add_argument("--db", type=Path, default=Path(".mailbox/mailbox.sqlite3"))
    commands.add_parser("register").add_argument("name")
    send = commands.add_parser("send")
    send.add_argument("to")
    send.add_argument("text")
    send.add_argument("--request-id", type=UUID)
    commands.add_parser("receive").add_argument("--wait", type=float, default=0)
    commands.add_parser("ack").add_argument("message_id", type=int)
    args = parser.parse_args()

    if args.command == "serve":
        import uvicorn

        from .server import create_app

        uvicorn.run(create_app(args.db), host="127.0.0.1", port=args.port)
        return

    if args.identity is None:
        parser.error("Provide --identity or set AGENT_MAILBOX_IDENTITY")

    client = MailboxClient(args.port, args.identity)
    try:
        match args.command:
            case "register":
                result = client.register(args.name)
            case "send":
                result = client.send(args.to, args.text, args.request_id)
            case "receive":
                result = client.receive(args.wait)
            case "ack":
                result = client.ack(args.message_id)
    except (MailboxError, OSError, ValueError, KeyError) as exc:
        print(f"agent-mailbox: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
