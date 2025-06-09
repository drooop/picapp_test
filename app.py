#!/usr/bin/env python3
"""Demo application with a matrix worker running in a thread."""

import asyncio
import logging
from collections import deque
from pathlib import Path
import mimetypes
import aiofiles
import uuid
import io
import sys
import threading
import os

from PIL import Image
from aiortc import RTCPeerConnection
from aiortc.contrib.media import MediaPlayer
from aiortc.sdp import candidate_to_sdp

# Silence nio warnings
logging.getLogger("nio.events.misc").setLevel(logging.ERROR)

import toga
from toga.style import Pack
from toga.style.pack import COLUMN, ROW
from nio import (
    AsyncClient,
    RoomMessageText,
    MessageDirection,
    CallInviteEvent,
    CallHangupEvent,
    CallAnswerEvent,
    CallCandidatesEvent,
)

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s: %(message)s")

log = logging.getLogger(__name__)

HOMESERVER = "https://nsynapse.lexon.tq.i3s.io"
USER_ID = "@drop:nr.lexon.tq.i3s.io"
PASSWORD = "TQcps@123_"
ROOM_ID = "!HgYKfoJqHVWzEOQyDK:nr.lexon.tq.i3s.io"


class DemoApp(toga.App):
    def run_on_ui(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self._ui_loop)

    def run_on_matrix(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self._matrix_loop)

    def startup(self):
        self._ui_loop = asyncio.get_event_loop()
        self.matrix = AsyncClient(HOMESERVER, USER_ID, ssl=False)
        self.msg_cache = deque(maxlen=200)
        self.known_room_ids = set()
        self.current_room_id = ROOM_ID
        self._refresh_lock = asyncio.Lock()
        self.pc = None
        self.player = None
        self._local_video_task = None
        self._remote_video_task = None

        self._build_app()

        def worker():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._matrix_loop = loop
            loop.create_task(self._matrix_worker())
            loop.run_forever()

        threading.Thread(target=worker, daemon=True).start()

    # ------------------------------------------------------------------ UI -----
    def _build_app(self):
        self.chat_view = self.get_chat_view()
        self.photo_view = self.get_photo_view()

        container = toga.OptionContainer(content=[("Chat", self.chat_view), ("Camera", self.photo_view)])

        self.main_window = toga.MainWindow(title="PICAPP Demo", content=container, size=(800, 600), position=(300, 100))
        self.main_window.show()

    def get_photo_view(self):
        self.photo = toga.ImageView(style=Pack(height=280))
        view = toga.Box(style=Pack(direction=COLUMN, flex=1, align_items="end"))
        view.add(self.photo)
        view.add(toga.Button("Take Photo", on_press=self.take_photo))
        return view

    async def take_photo(self, *_):
        try:
            if not self.camera.has_permission:
                await self.camera.request_permission()
            img = await self.camera.take_photo()
            if img:
                self.photo.image = img
        except Exception as exc:
            await self.main_window.dialog(toga.InfoDialog("Camera Error", str(exc)))

    def get_chat_view(self):
        self.chat_box = toga.Box(style=Pack(direction=COLUMN, background_color="black"))
        self.chat_history = toga.ScrollContainer(content=self.chat_box, style=Pack(flex=1, background_color="black"))
        self.chat_input = toga.TextInput(placeholder="Type a message...", style=Pack(flex=1))
        self.send_button = toga.Button("Send", on_press=self._send_click, style=Pack(margin_left=10))
        self.video_call_button = toga.Button("Video Call", on_press=self._start_video_call, style=Pack(margin_left=10))
        self.file_send_button = toga.Button("Upload File", on_press=self._send_file, style=Pack(margin_left=10))
        self.room_list = toga.Box(style=Pack(direction=COLUMN, width=140, background_color="black"))

        view = toga.Box()
        view_chat = toga.Box(style=Pack(direction=COLUMN, flex=1))
        self.input_box = toga.Box(
            children=[self.chat_input, self.send_button, self.video_call_button, self.file_send_button],
            style=Pack(direction=ROW, margin_top=10),
        )
        view_chat.add(self.chat_history)
        view_chat.add(self.input_box)
        self.view_chat = view_chat

        view.add(self.room_list)
        view.add(view_chat)
        return view

    def _add_room_button(self, room_id, title):
        self.room_list.add(
            toga.Button(title, on_press=lambda w, rid=room_id: self._change_current_room(rid), style=Pack(margin=6, background_color="black", color="white"))
        )

    def _change_current_room(self, room_id):
        self.current_room_id = room_id
        log.warning("Current room changed to: %s", room_id)
        self.chat_box.clear()
        self.msg_cache.clear()
        self.chat_history.position = toga.Position(0, 10000)
        self.run_on_matrix(self._refresh_history())

    # ------------------------------------------------------------- Matrix loop --
    async def _matrix_worker(self):
        print("Matrix worker started", flush=True)
        log.warning("Matrix worker started")
        await self.matrix.login(PASSWORD)
        log.warning("Login OK")
        self.matrix.add_event_callback(self._on_call_invite, CallInviteEvent)
        self.matrix.add_event_callback(self._on_call_hangup, CallHangupEvent)
        self.matrix.add_event_callback(self._on_call_answer, CallAnswerEvent)
        self.matrix.add_event_callback(self._on_call_candidates, CallCandidatesEvent)
        await self.matrix.sync(timeout=500)
        while True:
            try:
                await self._update_rooms()
                await self._refresh_history()
            except Exception as err:
                log.warning("sync error: %s", err)
            await asyncio.sleep(0.5)

    # ---------------------------------------------------------------- Send -----
    def _send_click(self, *_):
        text = self.chat_input.value.strip()
        if not text:
            return
        self.chat_input.value = ""

        async def _send():
            try:
                await self.matrix.room_send(
                    room_id=self.current_room_id,
                    message_type="m.room.message",
                    content={"msgtype": "m.text", "body": text},
                )
            except Exception as err:
                msg = str(err)
                log.error("send failed: %s", msg)
                await self.main_window.dialog(toga.InfoDialog("Send Failed", msg))

        self.run_on_matrix(_send())

    async def _send_file_to_room(self, path):
        try:
            p = Path(path)
            size = p.stat().st_size
            mime, _ = mimetypes.guess_type(p.name)
            async with aiofiles.open(p, "rb") as f:
                resp, _ = await self.matrix.upload(
                    f,
                    content_type=mime or "application/octet-stream",
                    filename=p.name,
                    filesize=size,
                )
            msgtype = "m.image" if mime and mime.startswith("image/") else "m.file"
            await self.matrix.room_send(
                room_id=self.current_room_id,
                message_type="m.room.message",
                content={
                    "msgtype": msgtype,
                    "body": p.name,
                    "url": resp.content_uri,
                    "info": {"mimetype": mime or "application/octet-stream", "size": size},
                },
            )
        except Exception as err:
            msg = str(err)
            log.error("send file failed: %s", msg)
            await self.main_window.dialog(toga.InfoDialog("Send Failed", msg))

    def _send_file(self, *_):
        async def _task():
            try:
                dialog = toga.OpenFileDialog("Choose File")
                fut = asyncio.get_event_loop().create_future()
                task = asyncio.create_task(self.main_window.dialog(dialog))
                task.add_done_callback(lambda t: fut.set_result(t.result()))
                path = await fut
                if path:
                    await asyncio.wrap_future(self.run_on_matrix(self._send_file_to_room(path)))
            except Exception as exc:
                await self.main_window.dialog(toga.InfoDialog("File Error", str(exc)))

        asyncio.create_task(_task())

    def _download_file(self, widget, mxc_url, filename):
        async def _task():
            try:
                dialog = toga.SaveFileDialog("Save To", suggested_filename=filename)
                fut = asyncio.get_event_loop().create_future()
                task = asyncio.create_task(self.main_window.dialog(dialog))
                task.add_done_callback(lambda t: fut.set_result(t.result()))
                path = await fut
                if path:
                    await asyncio.wrap_future(self.run_on_matrix(self.matrix.download(mxc_url, save_to=path)))
            except Exception as exc:
                await self.main_window.dialog(toga.InfoDialog("Download Error", str(exc)))

        asyncio.create_task(_task())

    def _start_video_call(self, *_):
        self.call_id = str(uuid.uuid4())
        self._show_call_overlay()
        self.run_on_matrix(self._call_invite())

    async def _call_invite(self):
        await asyncio.sleep(0)
        try:
            await self.matrix.room_send(
                room_id=self.current_room_id,
                message_type="m.call.invite",
                content={"call_id": self.call_id, "version": "1", "lifetime": 60000, "offer": {"type": "offer", "sdp": ""}},
            )
        except Exception as exc:
            await self.main_window.dialog(toga.InfoDialog("Call Error", str(exc)))

    def _show_call_overlay(self):
        if getattr(self, "call_overlay", None):
            return
        self.remote_view = toga.ImageView(style=Pack(height=360))
        self.local_view = toga.ImageView(style=Pack(width=160, height=120))
        btn = toga.Button("End Call", on_press=self._end_video_call, style=Pack(padding_top=10))
        overlay = toga.Box(style=Pack(direction=COLUMN, flex=1, padding=10))
        overlay.add(self.remote_view)
        overlay.add(self.local_view)
        overlay.add(btn)
        self.view_chat.clear()
        self.view_chat.add(overlay)
        self.call_overlay = overlay

    async def _create_peer_connection(self):
        self.pc = RTCPeerConnection()
        await self._setup_local_media()

        @self.pc.on("icecandidate")
        async def on_icecandidate(candidate):
            if candidate and candidate.sdpMid is not None:
                await self.matrix.room_send(
                    room_id=self.current_room_id,
                    message_type="m.call.candidates",
                    content={
                        "call_id": self.call_id,
                        "version": "1",
                        "candidates": [
                            {
                                "candidate": "candidate:" + candidate_to_sdp(candidate),
                                "sdpMid": candidate.sdpMid,
                                "sdpMLineIndex": candidate.sdpMLineIndex,
                            }
                        ],
                    },
                )

        @self.pc.on("track")
        def on_track(track):
            if track.kind == "video":
                if self._remote_video_task:
                    self._remote_video_task.cancel()
                self._remote_video_task = asyncio.create_task(self._display_track(track, self.remote_view))

    async def _setup_local_media(self):
        options = {"framerate": "30", "video_size": "640x480"}
        if sys.platform == "darwin":
            self.player = MediaPlayer("default:none", format="avfoundation", options=options)
        elif sys.platform.startswith("linux"):
            self.player = MediaPlayer("/dev/video0", format="v4l2", options=options)
        else:
            self.player = None
        if self.player:
            if self.player.video:
                self.pc.addTrack(self.player.video)
                self._local_video_task = asyncio.create_task(self._display_track(self.player.video, self.local_view))
            if self.player.audio:
                self.pc.addTrack(self.player.audio)

    async def _display_track(self, track, view):
        try:
            while True:
                frame = await track.recv()
                img = frame.to_image()
                buf = io.BytesIO()
                img.save(buf, format="PNG")
                toga_img = toga.Image(src=buf.getvalue())
                view.image = toga_img
        except Exception:
            pass

    async def _close_peer_connection(self):
        if self._remote_video_task:
            self._remote_video_task.cancel()
            self._remote_video_task = None
        if self._local_video_task:
            self._local_video_task.cancel()
            self._local_video_task = None
        if self.player:
            if self.player.audio:
                self.player.audio.stop()
            if self.player.video:
                self.player.video.stop()
            self.player = None
        if self.pc:
            await self.pc.close()
            self.pc = None
        if getattr(self, "call_overlay", None):
            self.view_chat.clear()
            self.view_chat.add(self.chat_history)
            self.view_chat.add(self.input_box)
            self.call_overlay = None

    def _end_video_call(self, *_):
        async def _task():
            try:
                if getattr(self, "call_id", None):
                    await self.matrix.room_send(
                        room_id=self.current_room_id,
                        message_type="m.call.hangup",
                        content={"call_id": self.call_id, "version": "1"},
                    )
            except Exception as exc:
                await self.main_window.dialog(toga.InfoDialog("Call Error", str(exc)))
            await asyncio.wrap_future(self.run_on_ui(self._close_peer_connection()))

        self.run_on_matrix(_task())

    async def _on_call_invite(self, room, event):
        if event.expired:
            return
        if event.sender == self.matrix.user_id:
            return
        self.call_id = event.call_id
        fut = self.run_on_ui(self.main_window.dialog(toga.QuestionDialog("Video Call", "Accept call?")))
        accept = await asyncio.wrap_future(fut)
        if not accept:
            try:
                await self.matrix.room_send(
                    room_id=room.room_id,
                    message_type="m.call.hangup",
                    content={"call_id": event.call_id, "version": event.version},
                )
            except Exception as exc:
                fut2 = self.run_on_ui(self.main_window.dialog(toga.InfoDialog("Call Error", str(exc))))
                await asyncio.wrap_future(fut2)
            return

        async def show():
            self._show_call_overlay()

        await asyncio.wrap_future(self.run_on_ui(show()))
        answer = {"type": "answer", "sdp": ""}
        try:
            await self.matrix.room_send(
                room_id=room.room_id,
                message_type="m.call.answer",
                content={"call_id": event.call_id, "version": event.version, "answer": answer},
            )
        except Exception as exc:
            fut3 = self.run_on_ui(self.main_window.dialog(toga.InfoDialog("Call Error", str(exc))))
            await asyncio.wrap_future(fut3)

    async def _on_call_hangup(self, room, event):
        if getattr(self, "call_id", None) == event.call_id:
            self.run_on_ui(self._close_peer_connection())

    async def _on_call_answer(self, room, event):
        if getattr(self, "call_id", None) != event.call_id:
            return
        return

    async def _on_call_candidates(self, room, event):
        if getattr(self, "call_id", None) != event.call_id:
            return
        return

    # --------------------------------------------------------------- History ----
    async def _refresh_history(self):
        async with self._refresh_lock:
            room_id = self.current_room_id
            sync_resp = await self.matrix.sync(timeout=200)
            hist_resp = await self.matrix.room_messages(
                room_id=room_id,
                start=sync_resp.next_batch,
                direction=MessageDirection.back,
                limit=200,
            )

            if room_id != self.current_room_id:
                return

            messages = []
            widgets = []

            for ev in reversed(hist_resp.chunk):
                sender = ev.sender.split(":", 1)[0]
                if isinstance(ev, RoomMessageText):
                    messages.append(f"{sender}: {ev.body}")
                    widgets.append(toga.Label(f"{sender}: {ev.body}", style=Pack(color="white")))
                elif ev.source.get("type") == "m.room.message":
                    msgtype = ev.source.get("content", {}).get("msgtype")
                    body = ev.source.get("content", {}).get("body")
                    url = ev.source.get("content", {}).get("url")
                    if msgtype == "m.image" and url:
                        messages.append(f"{sender}: [image] {body}")
                        try:
                            resp = await self.matrix.download(url)
                            img = toga.Image(src=resp.body)
                            box = toga.Box(style=Pack(direction=COLUMN, margin=5, background_color="#1b2f4b"))
                            box.add(toga.Label(f"{sender}: {body}", style=Pack(color="white")))
                            box.add(toga.ImageView(img, style=Pack(height=150, margin_top=5)))
                            box.add(
                                toga.Button(
                                    "Download",
                                    on_press=lambda w, u=url, n=body: self._download_file(w, u, n),
                                    style=Pack(width=60, margin_top=5, background_color="#003d99", color="white"),
                                )
                            )
                            widgets.append(box)
                        except Exception:
                            widgets.append(toga.Label(f"{sender}: [image] {body}", style=Pack(color="white")))
                    elif msgtype == "m.file" and url:
                        messages.append(f"{sender}: [file] {body}")
                        box = toga.Box(style=Pack(direction=COLUMN, margin=5, background_color="#1b2f4b"))
                        box.add(toga.Label(f"{sender}: {body}", style=Pack(color="white")))
                        box.add(
                            toga.Button(
                                "Download",
                                on_press=lambda w, u=url, n=body: self._download_file(w, u, n),
                                style=Pack(width=60, margin_top=5, background_color="#003d99", color="white"),
                            )
                        )
                        widgets.append(box)

            if messages != list(self.msg_cache):
                self.msg_cache.clear()
                self.msg_cache.extend(messages)

                async def ui():
                    self.chat_box.clear()
                    for w in widgets:
                        self.chat_box.add(w)
                    self.chat_history.position = toga.Position(0, 10000)

                self.run_on_ui(ui())
                log.info("\n".join(messages))

    # -------------------------------------------------------------- Room list ---
    async def _update_rooms(self):
        new_rooms = []
        for rid, room in self.matrix.rooms.items():
            if rid not in self.known_room_ids:
                self.known_room_ids.add(rid)
                new_rooms.append((rid, room.display_name or rid))
        if new_rooms:
            async def ui():
                for rid, title in new_rooms:
                    self._add_room_button(rid, title)
            self.run_on_ui(ui())
            log.warning("Room list updated")


def main():
    if sys.platform.startswith("linux"):
        os.environ.setdefault("TOGA_BACKEND", "toga_gtk")
    return DemoApp()
