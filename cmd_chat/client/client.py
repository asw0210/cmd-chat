import asyncio
import json
import base64
import queue
import threading
from typing import Optional
from urllib.parse import urlparse

import srp
import requests
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes
import websockets
from rich.console import Console
from rich.panel import Panel

srp.rfc5054_enable()


class Client:
    def __init__(
        self, server: str, port: int, username: str, password: Optional[str] = None
    ):
        parsed = urlparse(server if "://" in server else f"//{server}")
        scheme = parsed.scheme or "http"

        self.server = parsed.netloc or parsed.path
        self.port = port
        self.username = username
        self.password = (password or "").encode()
        self.http_scheme = "https" if scheme == "https" else "http"
        self.ws_scheme = "wss" if self.http_scheme == "https" else "ws"
        self.user_id: Optional[str] = None
        self.fernet: Optional[Fernet] = None
        self.room_fernet: Optional[Fernet] = None

        self.console = Console()
        self.messages: list[dict] = []
        self.users: list[dict] = []
        self.connected = False
        self.running = False

    @property
    def base_url(self) -> str:
        return f"{self.http_scheme}://{self.server}:{self.port}"

    @property
    def ws_url(self) -> str:
        return f"{self.ws_scheme}://{self.server}:{self.port}"

    def success(self, message: str) -> None:
        self.console.print(f"[green]✓ {message}[/]")

    def error(self, message: str) -> None:
        self.console.print(f"[red]✗ {message}[/]")

    def info(self, message: str) -> None:
        self.console.print(f"[cyan]• {message}[/]")

    def update_room_key(self, room_salt: bytes) -> None:
        hkdf = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=room_salt,
            info=b"cmd-chat-room-key",
        )
        room_key = hkdf.derive(self.password)
        self.room_fernet = Fernet(base64.urlsafe_b64encode(room_key))

    def srp_authenticate(self) -> None:
        with self.console.status("[cyan]Starting SRP handshake...[/]", spinner="dots"):

            usr = srp.User(b"chat", self.password, hash_alg=srp.SHA256)
            _, A = usr.start_authentication()

            resp = requests.post(
                f"{self.base_url}/srp/init",
                json={
                    "username": self.username,
                    "A": base64.b64encode(A).decode(),
                },
                timeout=30,
            )
            resp.raise_for_status()
            init_data = resp.json()

            self.user_id = init_data["user_id"]
            B = base64.b64decode(init_data["B"])
            salt = base64.b64decode(init_data["salt"])
            room_salt = base64.b64decode(init_data["room_salt"])

            self.update_room_key(room_salt)

            M = usr.process_challenge(salt, B)

            if M is None:
                raise ValueError("SRP challenge processing failed")

            resp = requests.post(
                f"{self.base_url}/srp/verify",
                json={
                    "user_id": self.user_id,
                    "username": self.username,
                    "M": base64.b64encode(M).decode(),
                },
                timeout=30,
            )
            resp.raise_for_status()
            verify_data = resp.json()

            H_AMK = base64.b64decode(verify_data["H_AMK"])
            usr.verify_session(H_AMK)

            if not usr.authenticated():
                raise ValueError("Server authentication failed")

            session_key = base64.b64decode(verify_data["session_key"])
            self.fernet = Fernet(session_key)

        self.success(f"SRP authenticated (session: {self.user_id[:8]}...)")

    def decrypt_message(self, msg: dict) -> dict:
        if "text" in msg and msg["text"]:
            try:
                decrypted = self.room_fernet.decrypt(msg["text"].encode()).decode()
                msg["text"] = decrypted
            except Exception:
                msg["text"] = "[decrypt failed]"
        return msg

    def render_messages(self) -> None:
        self.console.clear()

        users_online = ", ".join(u.get("username", "?") for u in self.users) or "none"
        self.console.print(f"[dim]Online: {users_online}[/]")
        self.console.print("─" * 60)

        display_messages = (
            self.messages[-15:] if len(self.messages) > 15 else self.messages
        )

        for msg in display_messages:
            username = msg.get("username", "unknown")
            text = msg.get("text", "")
            timestamp = str(msg.get("timestamp", ""))[:19].replace("T", " ")

            style = "green" if username == self.username else "cyan"
            self.console.print(f"[dim]{timestamp}[/] [{style}]{username}[/]: {text}")

        if not display_messages:
            self.console.print("[dim italic]No messages yet...[/]")

        self.console.print("─" * 60)
        self.console.print("[dim]Type message and press Enter. 'q' to quit.[/]")

    async def receive_loop(self, ws) -> None:
        try:
            async for raw in ws:
                if not self.running:
                    break

                data = json.loads(raw)
                msg_type = data.get("type", "")

                if msg_type == "init":
                    messages = [
                        self.decrypt_message(m) for m in data.get("messages", [])
                    ]
                    self.messages = messages
                    self.users = data.get("users", [])
                    self.connected = True
                    self.render_messages()
                elif msg_type == "message":
                    msg_data = self.decrypt_message(data.get("data", {}))
                    self.messages.append(msg_data)
                    self.render_messages()
                elif msg_type == "user_joined":
                    user = data.get("data", {})
                    user_id = user.get("user_id")
                    if user_id and not any(
                        u.get("user_id") == user_id for u in self.users
                    ):
                        self.users.append(user)
                    self.render_messages()
                elif msg_type == "clear":
                    username = data.get("username", "unknown")
                    self.messages = []
                    self.render_messages()
                    self.info(f"Chat history cleared by {username}")
                elif msg_type == "rotate":
                    username = data.get("username", "unknown")
                    room_salt = base64.b64decode(data["room_salt"])
                    self.update_room_key(room_salt)
                    self.messages = []
                    self.render_messages()
                    self.info(f"Room key rotated by {username}")
                elif msg_type == "user_left":
                    left_id = data.get("user_id")
                    self.users = [u for u in self.users if u.get("user_id") != left_id]
                    self.render_messages()

        except websockets.ConnectionClosed:
            self.connected = False

    async def read_console_input(self) -> str:
        result: queue.Queue[str | BaseException] = queue.Queue(maxsize=1)

        def read_input() -> None:
            try:
                result.put(input())
            except BaseException as exc:
                result.put(exc)

        threading.Thread(target=read_input, daemon=True).start()

        while self.running:
            try:
                value = result.get_nowait()
            except queue.Empty:
                await asyncio.sleep(0.05)
                continue

            if isinstance(value, BaseException):
                raise value
            return value

        raise asyncio.CancelledError

    async def input_loop(self, ws) -> None:
        while self.running:
            try:
                text = await self.read_console_input()
                if text.lower() in ("q", "quit", "exit"):
                    self.running = False
                    break
                if text.strip():
                    if text.strip() in ("/clear", "/rotate"):
                        await ws.send(text.strip())
                    else:
                        encrypted = self.room_fernet.encrypt(text.encode()).decode()
                        await ws.send(encrypted)
            except (EOFError, KeyboardInterrupt):
                self.running = False
                break

    async def run_async(self) -> None:
        self.console.clear()
        self.console.print(Panel("[bold cyan]CMD Chat Client[/]", expand=False))
        self.console.print()

        try:
            self.srp_authenticate()

            self.info("Connecting to chat...")
            url = f"{self.ws_url}/ws/chat?user_id={self.user_id}"

            async with websockets.connect(
                url,
                ping_interval=20,
                ping_timeout=20,
                close_timeout=5,
            ) as ws:
                self.success("Connected to chat server")
                self.running = True

                receive_task = asyncio.create_task(self.receive_loop(ws))
                input_task = asyncio.create_task(self.input_loop(ws))

                done, pending = await asyncio.wait(
                    [receive_task, input_task], return_when=asyncio.FIRST_COMPLETED
                )

                self.running = False
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)

            self.console.print("\n[yellow]Disconnected[/]")

        except requests.exceptions.ConnectionError:
            self.error(f"Cannot connect to {self.base_url}")
        except requests.exceptions.HTTPError as e:
            self.error(f"Server error: {e.response.status_code} - {e.response.text}")
        except ValueError as e:
            self.error(f"Authentication failed: {e}")
        except Exception:
            import traceback

            self.error("Error occurred")
            traceback.print_exc()

    def run(self) -> None:
        asyncio.run(self.run_async())
