import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cmd_chat.server.models import Message, UserSession
from cmd_chat.server.views import chat_ws


class TestWebSocket:

    def test_ws_connect_no_user_id(self, test_client):
        _, ws = test_client.websocket("/ws/chat")
        assert ws is not None

    def test_ws_connect_invalid_session(self, test_client):

        _, ws = test_client.websocket("/ws/chat?user_id=invalid123")
        assert ws is not None

    @pytest.mark.asyncio
    async def test_clear_command_clears_history(self, app):
        user_id = "user-1"
        app.ctx.session_store.add(
            UserSession(user_id=user_id, ip="127.0.0.1", username="alice")
        )
        app.ctx.message_store.add(Message(text="encrypted-old", username="alice"))

        broadcasts = []

        class Manager:
            async def connect(self, user_id, ws):
                pass

            async def disconnect(self, user_id):
                pass

            async def broadcast(self, message, exclude_user=None):
                broadcasts.append(json.loads(message))

        app.ctx.connection_manager = Manager()

        request = SimpleNamespace(args={"user_id": user_id})
        ws = AsyncMock()
        ws.__aiter__.return_value = ["/clear"]

        await chat_ws(request, ws, app)

        assert app.ctx.message_store.count() == 0
        assert not any(
            message.get("type") == "message"
            and message.get("data", {}).get("text") == "/clear"
            for message in broadcasts
        )
        assert any(message.get("type") == "clear" for message in broadcasts)

    @pytest.mark.asyncio
    async def test_rotate_command_rotates_key_and_clears_history(self, app):
        user_id = "user-1"
        app.ctx.session_store.add(
            UserSession(user_id=user_id, ip="127.0.0.1", username="alice")
        )
        app.ctx.message_store.add(Message(text="encrypted-old", username="alice"))
        old_salt = app.ctx.room_salt

        broadcasts = []

        class Manager:
            async def connect(self, user_id, ws):
                pass

            async def disconnect(self, user_id):
                pass

            async def broadcast(self, message, exclude_user=None):
                broadcasts.append(json.loads(message))

        app.ctx.connection_manager = Manager()

        request = SimpleNamespace(args={"user_id": user_id})
        ws = AsyncMock()
        ws.__aiter__.return_value = ["/rotate"]

        await chat_ws(request, ws, app)

        rotate_messages = [m for m in broadcasts if m.get("type") == "rotate"]

        assert app.ctx.message_store.count() == 0
        assert app.ctx.room_salt != old_salt
        assert len(rotate_messages) == 1
        assert base64.b64decode(rotate_messages[0]["room_salt"]) == app.ctx.room_salt
