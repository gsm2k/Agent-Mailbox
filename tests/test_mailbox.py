import asyncio
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from httpx import ASGITransport, AsyncClient

from agent_mailbox.server import create_app


def identity():
    return {"Authorization": f"Bearer {uuid4().hex}{uuid4().hex}"}


def client_for(db):
    return AsyncClient(transport=ASGITransport(app=create_app(db)), base_url="http://127.0.0.1")


class MailboxTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db = Path(directory.name) / "mailbox.sqlite3"
        self.client = client_for(self.db)
        self.addAsyncCleanup(self.client.aclose)
        self.claude, self.codex = identity(), identity()
        await self.register("claude", self.claude)
        await self.register("codex", self.codex)

    async def register(self, name, headers):
        result = await self.client.post("/sessions", headers=headers, json={"name": name})
        self.assertEqual(result.status_code, 200, result.text)

    async def send(self, text="Review 7c32ab1", request_id=None):
        result = await self.client.post(
            "/messages",
            headers=self.codex,
            json={"to": "claude", "text": text, "request_id": request_id},
        )
        return result

    async def test_restart_preserves_pending_and_acknowledged_messages(self):
        sent = await self.send()
        self.assertEqual(sent.status_code, 200, sent.text)
        message_id = sent.json()["id"]

        async with client_for(self.db) as restarted:
            first = (await restarted.get("/inbox", headers=self.claude)).json()["messages"]
            self.assertEqual(first[0]["id"], message_id)
            self.assertEqual(first[0]["sender"], "codex")
            self.assertEqual(first[0]["text"], "Review 7c32ab1")
            self.assertEqual(
                (await restarted.get("/inbox", headers=self.claude)).json()["messages"], first
            )
            for _ in range(2):
                result = await restarted.post(f"/inbox/{message_id}/ack", headers=self.claude)
                self.assertEqual(result.status_code, 200)

        async with client_for(self.db) as restarted:
            self.assertEqual((await restarted.get("/inbox", headers=self.claude)).json(), {"messages": []})

    async def test_registration_retry_and_name_conflict(self):
        await self.register("claude", self.claude)
        for name, headers in (("claude", identity()), ("new-name", self.claude)):
            result = await self.client.post("/sessions", headers=headers, json={"name": name})
            self.assertEqual(result.status_code, 409)

    async def test_authentication_and_recipient_scope(self):
        message_id = (await self.send()).json()["id"]
        self.assertEqual((await self.client.get("/inbox")).status_code, 401)
        self.assertEqual((await self.client.get("/inbox", headers=identity())).status_code, 401)
        self.assertEqual((await self.client.get("/inbox", headers=self.codex)).json(), {"messages": []})
        result = await self.client.post(f"/inbox/{message_id}/ack", headers=self.codex)
        self.assertEqual(result.status_code, 404)
        result = await self.client.post(
            "/messages", headers=self.codex, json={"to": "unknown", "text": "Hello"},
        )
        self.assertEqual(result.status_code, 404)

    async def test_send_can_be_retried_with_same_request_id(self):
        key = str(uuid4())
        first = await self.send(request_id=key)
        repeated = await self.send(request_id=key)
        conflict = await self.send(text="Different text", request_id=key)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(repeated.json(), first.json())
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(len((await self.client.get("/inbox", headers=self.claude)).json()["messages"]), 1)

    async def test_waiting_receive_returns_when_peer_sends(self):
        waiting = asyncio.create_task(
            self.client.get("/inbox", headers=self.claude, params={"wait": 3}),
        )
        await asyncio.sleep(0.05)
        self.assertFalse(waiting.done())
        self.assertEqual((await self.send()).status_code, 200)
        received = await asyncio.wait_for(waiting, timeout=2)
        self.assertEqual(received.json()["messages"][0]["sender"], "codex")
        result = await self.client.get("/inbox", headers=self.codex, params={"wait": 0.05})
        self.assertEqual(result.json(), {"messages": []})


if __name__ == "__main__":
    unittest.main()
