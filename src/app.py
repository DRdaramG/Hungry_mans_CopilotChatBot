"""
Main GUI application — Hungry Man's Copilot ChatBot.

Layout
------
┌─────────────────────────────────────────┐
│ Menu: File | Settings                    │
├─────────────────────────────────────────┤
│ Model: ○ Claude Opus  ○ Gemini  ○ GPT-4o│  ← model_frame
│                                 [status] │
├─────────────────────────────────────────┤
│                                          │
│   Chat display (scrollable)              │  ← chat_frame
│                                          │
├─────────────────────────────────────────┤
│  [📎 file1.csv ✕]  [🖼 img.png ✕]        │  ← attach_frame (hidden when empty)
├─────────────────────────────────────────┤
│ [📎][💡] │ Input text area… │ [Send][Clr]│  ← input_frame
└─────────────────────────────────────────┘
"""

import json
import os
import queue
import threading
import tkinter as tk
import webbrowser
from tkinter import filedialog, messagebox, scrolledtext, simpledialog, ttk

from .auth import delete_token, load_token, poll_for_token, request_device_code, save_token
from .copilot_api import MODELS, CopilotClient
from .file_handler import process_file
from .prompt_manager import PromptManager


# ---------------------------------------------------------------------------
# Small helper dialogs
# ---------------------------------------------------------------------------

class _AuthDialog(tk.Toplevel):
    """GitHub device-flow authentication dialog."""

    def __init__(self, parent: tk.Tk) -> None:
        super().__init__(parent)
        self.title("GitHub Authentication")
        self.resizable(False, False)
        self.grab_set()

        self.result: str | None = None
        self._cancelled = False
        self._verification_url = ""

        self._build_ui()
        self._start()

    def _build_ui(self) -> None:
        f = ttk.Frame(self, padding=20)
        f.pack(fill=tk.BOTH, expand=True)

        ttk.Label(f, text="GitHub Authentication",
                  font=("", 13, "bold")).pack(pady=(0, 8))
        ttk.Label(
            f,
            text="Authenticate with GitHub to use your Copilot subscription.",
            wraplength=380,
        ).pack(pady=4)

        self._status = ttk.Label(f, text="Requesting device code…",
                                 wraplength=380, foreground="#555")
        self._status.pack(pady=4)

        code_box = ttk.LabelFrame(f, text="Device Code", padding=10)
        code_box.pack(fill=tk.X, pady=8)

        self._code_lbl = ttk.Label(code_box, text="—",
                                   font=("Courier", 22, "bold"))
        self._code_lbl.pack()

        self._url_lbl = ttk.Label(code_box, text="", foreground="#0055cc",
                                  cursor="hand2")
        self._url_lbl.pack()
        self._url_lbl.bind("<Button-1>",
                           lambda _e: webbrowser.open(self._verification_url))

        btn_row = ttk.Frame(f)
        btn_row.pack(pady=6)

        self._open_btn = ttk.Button(btn_row, text="Open Browser",
                                    command=self._open_browser,
                                    state=tk.DISABLED)
        self._open_btn.pack(side=tk.LEFT, padx=4)

        ttk.Button(btn_row, text="Cancel",
                   command=self._cancel).pack(side=tk.LEFT, padx=4)

    def _start(self) -> None:
        threading.Thread(target=self._flow, daemon=True).start()

    def _flow(self) -> None:
        try:
            data = request_device_code()
            device_code = data["device_code"]
            user_code = data["user_code"]
            self._verification_url = data["verification_uri"]
            interval = int(data.get("interval", 5))
            self.after(0, self._show_code, user_code, self._verification_url)
            token = poll_for_token(device_code, interval,
                                   lambda: self._cancelled)
            if token:
                save_token(token)
                self.after(0, self._success, token)
        except Exception as exc:  # noqa: BLE001
            self.after(0, self._error, str(exc))

    def _show_code(self, code: str, url: str) -> None:
        self._code_lbl.config(text=code)
        self._url_lbl.config(text=url)
        self._open_btn.config(state=tk.NORMAL)
        self._status.config(
            text="Enter the code above at the URL, then wait…"
        )
        self._open_browser()

    def _open_browser(self) -> None:
        if self._verification_url:
            webbrowser.open(self._verification_url)

    def _success(self, token: str) -> None:
        self.result = token
        self.destroy()

    def _error(self, msg: str) -> None:
        messagebox.showerror("Authentication Error", msg, parent=self)
        self.destroy()

    def _cancel(self) -> None:
        self._cancelled = True
        self.destroy()


class _PromptEditDialog(tk.Toplevel):
    """Simple multi-line text editor for a single prompt."""

    def __init__(self, parent: tk.Widget, title: str, content: str) -> None:
        super().__init__(parent)
        self.title(title)
        self.geometry("520x320")
        self.grab_set()
        self.result: str | None = None

        f = ttk.Frame(self, padding=10)
        f.pack(fill=tk.BOTH, expand=True)

        self._text = scrolledtext.ScrolledText(f, wrap=tk.WORD, font=("", 10))
        self._text.pack(fill=tk.BOTH, expand=True)
        self._text.insert(tk.END, content)

        btn = ttk.Frame(f)
        btn.pack(fill=tk.X, pady=(6, 0))
        ttk.Button(btn, text="Save",
                   command=self._save).pack(side=tk.RIGHT, padx=4)
        ttk.Button(btn, text="Cancel",
                   command=self.destroy).pack(side=tk.RIGHT)

    def _save(self) -> None:
        self.result = self._text.get("1.0", tk.END).strip()
        self.destroy()


class _PromptManagerDialog(tk.Toplevel):
    """Dialog to browse, edit, and select saved prompts."""

    def __init__(self, parent: tk.Widget, pm: PromptManager,
                 on_select=None) -> None:
        super().__init__(parent)
        self.title("Prompt Manager")
        self.geometry("540x420")
        self.grab_set()

        self._pm = pm
        self._on_select = on_select

        self._build_ui()
        self._refresh()

    def _build_ui(self) -> None:
        paned = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        paned.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        left = ttk.Frame(paned)
        paned.add(left, weight=3)

        self._listbox = tk.Listbox(left, selectmode=tk.SINGLE, font=("", 10))
        self._listbox.pack(fill=tk.BOTH, expand=True)
        self._listbox.bind("<<ListboxSelect>>", self._preview)

        right = ttk.Frame(paned)
        paned.add(right, weight=1)

        for text, cmd in [
            ("New",    self._new),
            ("Edit",   self._edit),
            ("Delete", self._delete),
            ("Use",    self._use),
            ("Close",  self.destroy),
        ]:
            ttk.Button(right, text=text, command=cmd).pack(
                fill=tk.X, padx=4, pady=2)

        preview_frame = ttk.LabelFrame(self, text="Preview", padding=4)
        preview_frame.pack(fill=tk.X, padx=8, pady=(0, 8))

        self._preview_text = tk.Text(preview_frame, height=4,
                                     wrap=tk.WORD, state=tk.DISABLED,
                                     font=("", 9))
        self._preview_text.pack(fill=tk.X)

    def _refresh(self) -> None:
        self._listbox.delete(0, tk.END)
        for name in self._pm.list_names():
            self._listbox.insert(tk.END, name)

    def _preview(self, _event=None) -> None:
        sel = self._listbox.curselection()
        if not sel:
            return
        content = self._pm.get(self._listbox.get(sel[0]))
        self._preview_text.config(state=tk.NORMAL)
        self._preview_text.delete("1.0", tk.END)
        self._preview_text.insert(tk.END, content)
        self._preview_text.config(state=tk.DISABLED)

    def _selected_name(self) -> str | None:
        sel = self._listbox.curselection()
        return self._listbox.get(sel[0]) if sel else None

    def _new(self) -> None:
        name = simpledialog.askstring("New Prompt", "Prompt name:", parent=self)
        if not name:
            return
        dlg = _PromptEditDialog(self, f"New Prompt — {name}", "")
        self.wait_window(dlg)
        if dlg.result is not None:
            self._pm.add(name, dlg.result)
            self._refresh()

    def _edit(self) -> None:
        name = self._selected_name()
        if not name:
            return
        dlg = _PromptEditDialog(self, f"Edit — {name}", self._pm.get(name))
        self.wait_window(dlg)
        if dlg.result is not None:
            self._pm.add(name, dlg.result)
            self._refresh()

    def _delete(self) -> None:
        name = self._selected_name()
        if not name:
            return
        if messagebox.askyesno("Delete",
                               f"Delete prompt '{name}'?", parent=self):
            self._pm.delete(name)
            self._refresh()

    def _use(self) -> None:
        name = self._selected_name()
        if not name:
            return
        if self._on_select:
            self._on_select(self._pm.get(name))
        self.destroy()


# ---------------------------------------------------------------------------
# Main application
# ---------------------------------------------------------------------------

class CopilotChatApp:
    """Hungry Man's Copilot ChatBot — main application class."""

    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("Hungry Man's Copilot ChatBot 🍽️")
        self.root.geometry("960x720")
        self.root.minsize(640, 520)

        self._client: CopilotClient | None = None
        self._history: list[dict] = []       # OpenAI-format message list
        self._attachments: list[dict] = []   # FileData dicts waiting to send
        self._pm = PromptManager()
        self._queue: queue.Queue = queue.Queue()
        self._model_var = tk.StringVar()
        self._status_var = tk.StringVar(value="⚠️  Not authenticated")

        self._build_menu()
        self._build_model_bar()
        self._build_chat_area()
        self._build_attach_area()
        self._build_input_area()

        self._check_saved_token()
        self._pump_queue()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_menu(self) -> None:
        bar = tk.Menu(self.root)
        self.root.config(menu=bar)

        file_menu = tk.Menu(bar, tearoff=False)
        bar.add_cascade(label="File", menu=file_menu)
        file_menu.add_command(label="Import Prompts…",
                              command=self._import_prompts)
        file_menu.add_command(label="Export Prompts…",
                              command=self._export_prompts)
        file_menu.add_separator()
        file_menu.add_command(label="Save Chat…", command=self._save_chat)
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self.root.quit)

        settings_menu = tk.Menu(bar, tearoff=False)
        bar.add_cascade(label="Settings", menu=settings_menu)
        settings_menu.add_command(label="GitHub Authentication…",
                                   command=self._run_auth)
        settings_menu.add_command(label="System Prompt…",
                                   command=self._set_system_prompt)
        settings_menu.add_command(label="Clear Saved Token",
                                   command=self._clear_token)

    def _build_model_bar(self) -> None:
        frame = ttk.LabelFrame(self.root, text="Model", padding=(8, 4))
        frame.pack(fill=tk.X, padx=10, pady=(6, 0))

        model_names = list(MODELS.keys())
        self._model_var.set(model_names[0])

        for name in model_names:
            ttk.Radiobutton(
                frame, text=name,
                variable=self._model_var, value=name,
            ).pack(side=tk.LEFT, padx=12)

        ttk.Label(frame, textvariable=self._status_var,
                  foreground="#444").pack(side=tk.RIGHT, padx=10)

    def _build_chat_area(self) -> None:
        frame = ttk.Frame(self.root)
        frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=6)

        self._chat = scrolledtext.ScrolledText(
            frame, wrap=tk.WORD, state=tk.DISABLED,
            font=("", 10), relief=tk.SUNKEN, borderwidth=1,
        )
        self._chat.pack(fill=tk.BOTH, expand=True)

        # Colour / font tags
        self._chat.tag_config("user_lbl",
                              foreground="#005cc5", font=("", 10, "bold"))
        self._chat.tag_config("asst_lbl",
                              foreground="#6f42c1", font=("", 10, "bold"))
        self._chat.tag_config("sys_lbl",
                              foreground="#6c757d", font=("", 9, "italic"))
        self._chat.tag_config("user_msg", foreground="#1a1a2e")
        self._chat.tag_config("asst_msg", foreground="#1a1a2e")
        self._chat.tag_config("sys_msg",
                              foreground="#6c757d", font=("", 9, "italic"))
        self._chat.tag_config("err_msg", foreground="#c0392b")

    def _build_attach_area(self) -> None:
        """Build the (initially hidden) attachment bar."""
        self._attach_outer = ttk.LabelFrame(
            self.root, text="Attachments", padding=(6, 4)
        )
        # Not packed yet — shown only when attachments are present.
        self._attach_inner = ttk.Frame(self._attach_outer)
        self._attach_inner.pack(fill=tk.X)

    def _build_input_area(self) -> None:
        outer = ttk.Frame(self.root, padding=(10, 4))
        outer.pack(fill=tk.X, side=tk.BOTTOM)

        # Left column: icon buttons
        btn_col = ttk.Frame(outer)
        btn_col.grid(row=0, column=0, sticky="ns", padx=(0, 6))

        ttk.Button(btn_col, text="📎", width=4,
                   command=self._attach_file).pack(pady=2)
        ttk.Button(btn_col, text="💡", width=4,
                   command=self._show_prompt_mgr).pack(pady=2)

        # Centre: text input
        self._input = scrolledtext.ScrolledText(
            outer, height=4, wrap=tk.WORD, font=("", 10),
            relief=tk.SUNKEN, borderwidth=1,
        )
        self._input.grid(row=0, column=1, sticky="nsew")
        self._input.bind("<Return>", self._on_enter_key)
        # Shift+Return → literal newline (handled by default)

        # Right column: action buttons
        act_col = ttk.Frame(outer)
        act_col.grid(row=0, column=2, sticky="ns", padx=(6, 0))

        self._send_btn = ttk.Button(act_col, text="Send ➤",
                                    command=self._send, width=9)
        self._send_btn.pack(pady=2)
        ttk.Button(act_col, text="Clear 🗑", command=self._clear_chat,
                   width=9).pack(pady=2)

        outer.columnconfigure(1, weight=1)

    # ------------------------------------------------------------------
    # Chat display helpers
    # ------------------------------------------------------------------

    def _append(self, label: str, body: str, role: str) -> None:
        """Append a complete message block to the chat display."""
        self._chat.config(state=tk.NORMAL)
        if self._chat.get("1.0", tk.END).strip():
            self._chat.insert(tk.END, "\n\n")

        tag_map = {
            "user":   ("user_lbl",  "user_msg"),
            "asst":   ("asst_lbl",  "asst_msg"),
            "system": ("sys_lbl",   "sys_msg"),
            "error":  ("sys_lbl",   "err_msg"),
        }
        lbl_tag, body_tag = tag_map.get(role, ("sys_lbl", "sys_msg"))
        self._chat.insert(tk.END, f"{label}\n", lbl_tag)
        if body:
            self._chat.insert(tk.END, body, body_tag)
        self._chat.config(state=tk.DISABLED)
        self._chat.see(tk.END)

    def _append_header(self, label: str, tag: str) -> None:
        """Insert only the speaker header (stream start)."""
        self._chat.config(state=tk.NORMAL)
        if self._chat.get("1.0", tk.END).strip():
            self._chat.insert(tk.END, "\n\n")
        self._chat.insert(tk.END, f"{label}\n", tag)
        self._chat.config(state=tk.DISABLED)
        self._chat.see(tk.END)

    def _stream_chunk(self, text: str) -> None:
        """Append a streaming text delta to the current message."""
        self._chat.config(state=tk.NORMAL)
        self._chat.insert(tk.END, text, "asst_msg")
        self._chat.config(state=tk.DISABLED)
        self._chat.see(tk.END)

    def _sys_msg(self, text: str) -> None:
        self._append("ℹ️  System", text, "system")

    # ------------------------------------------------------------------
    # Queue pump (bridges worker thread → main thread)
    # ------------------------------------------------------------------

    def _pump_queue(self) -> None:
        try:
            while True:
                kind, payload = self._queue.get_nowait()
                if kind == "start":
                    self._append_header(f"{payload}:", "asst_lbl")
                elif kind == "chunk":
                    self._stream_chunk(payload)
                elif kind == "done":
                    self._history.append({"role": "assistant",
                                          "content": payload})
                    self._send_btn.config(state=tk.NORMAL)
                elif kind == "error":
                    self._append("⚠️  Error", payload, "error")
                    self._send_btn.config(state=tk.NORMAL)
        except queue.Empty:
            pass
        self.root.after(40, self._pump_queue)

    # ------------------------------------------------------------------
    # Token / auth management
    # ------------------------------------------------------------------

    def _check_saved_token(self) -> None:
        token = load_token()
        if token:
            self._client = CopilotClient(token)
            self._status_var.set("✅  Authenticated")
            self._sys_msg(
                "Copilot ChatBot is ready.\n"
                "Select a model above and start chatting.\n"
                "Use Shift+Enter for multi-line input; Enter to send."
            )
        else:
            self._sys_msg(
                "Welcome to Hungry Man's Copilot ChatBot! 🍽️\n"
                "Go to Settings → GitHub Authentication… to sign in."
            )

    def _run_auth(self) -> None:
        dlg = _AuthDialog(self.root)
        self.root.wait_window(dlg)
        if dlg.result:
            self._client = CopilotClient(dlg.result)
            self._status_var.set("✅  Authenticated")
            self._sys_msg("Authentication successful! Ready to chat.")

    def _clear_token(self) -> None:
        delete_token()
        self._client = None
        self._status_var.set("⚠️  Not authenticated")
        self._sys_msg("Saved token removed. Please re-authenticate.")

    # ------------------------------------------------------------------
    # System prompt
    # ------------------------------------------------------------------

    def _set_system_prompt(self) -> None:
        current = (
            self._history[0]["content"]
            if self._history and self._history[0]["role"] == "system"
            else ""
        )
        dlg = _PromptEditDialog(self.root, "System Prompt", current)
        self.root.wait_window(dlg)
        if dlg.result is None:
            return
        if self._history and self._history[0]["role"] == "system":
            self._history.pop(0)
        if dlg.result:
            self._history.insert(0, {"role": "system", "content": dlg.result})
            self._sys_msg(
                f"System prompt set ({len(dlg.result)} chars)."
            )

    # ------------------------------------------------------------------
    # File attachments
    # ------------------------------------------------------------------

    def _attach_file(self) -> None:
        path = filedialog.askopenfilename(
            title="Attach File",
            filetypes=[
                ("Supported files",
                 "*.png *.jpg *.jpeg *.gif *.webp *.csv *.xls *.xlsx"),
                ("Images",        "*.png *.jpg *.jpeg *.gif *.webp"),
                ("Spreadsheets",  "*.csv *.xls *.xlsx"),
                ("All files",     "*.*"),
            ],
        )
        if path:
            data = process_file(path)
            self._attachments.append(data)
            self._refresh_attach_bar()

    def _refresh_attach_bar(self) -> None:
        for w in self._attach_inner.winfo_children():
            w.destroy()

        if self._attachments:
            self._attach_outer.pack(
                fill=tk.X, padx=10, pady=2, before=self.root.winfo_children()[-1]
            )
            for idx, att in enumerate(self._attachments):
                icon = "🖼️" if att["type"] == "image" else "📄"
                chip = ttk.Frame(self._attach_inner)
                chip.pack(side=tk.LEFT, padx=4)
                ttk.Label(chip, text=f"{icon} {att['name']}",
                          font=("", 9)).pack(side=tk.LEFT)
                ttk.Button(
                    chip, text="✕", width=2,
                    command=lambda i=idx: self._remove_attachment(i),
                ).pack(side=tk.LEFT)
        else:
            self._attach_outer.pack_forget()

    def _remove_attachment(self, idx: int) -> None:
        if 0 <= idx < len(self._attachments):
            self._attachments.pop(idx)
            self._refresh_attach_bar()

    # ------------------------------------------------------------------
    # Prompt manager
    # ------------------------------------------------------------------

    def _show_prompt_mgr(self) -> None:
        _PromptManagerDialog(self.root, self._pm,
                             on_select=self._insert_prompt)

    def _insert_prompt(self, content: str) -> None:
        self._input.delete("1.0", tk.END)
        self._input.insert(tk.END, content)

    def _import_prompts(self) -> None:
        path = filedialog.askopenfilename(
            title="Import Prompts",
            filetypes=[("JSON", "*.json"), ("All files", "*.*")],
        )
        if path:
            count = self._pm.import_from(path)
            messagebox.showinfo("Import", f"{count} prompt(s) imported.")

    def _export_prompts(self) -> None:
        path = filedialog.asksaveasfilename(
            title="Export Prompts",
            defaultextension=".json",
            filetypes=[("JSON", "*.json"), ("All files", "*.*")],
        )
        if path:
            self._pm.export(path)
            messagebox.showinfo("Export", f"Prompts exported to:\n{path}")

    # ------------------------------------------------------------------
    # Chat management
    # ------------------------------------------------------------------

    def _save_chat(self) -> None:
        path = filedialog.asksaveasfilename(
            title="Save Chat",
            defaultextension=".json",
            filetypes=[
                ("JSON", "*.json"),
                ("Text", "*.txt"),
                ("All files", "*.*"),
            ],
        )
        if not path:
            return
        if path.endswith(".json"):
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(self._history, fh, ensure_ascii=False, indent=2)
        else:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(self._chat.get("1.0", tk.END))
        messagebox.showinfo("Saved", f"Chat saved to:\n{path}")

    def _clear_chat(self) -> None:
        if not messagebox.askyesno("Clear Chat",
                                   "Clear all messages from this session?"):
            return
        system_msg = (
            self._history[0]
            if self._history and self._history[0]["role"] == "system"
            else None
        )
        self._history = [system_msg] if system_msg else []

        self._chat.config(state=tk.NORMAL)
        self._chat.delete("1.0", tk.END)
        self._chat.config(state=tk.DISABLED)

        self._sys_msg("Chat cleared.")

    # ------------------------------------------------------------------
    # Sending messages
    # ------------------------------------------------------------------

    def _on_enter_key(self, event: tk.Event) -> str | None:
        # Shift+Enter → insert a newline (default behaviour)
        if event.state & 0x1:  # Shift held
            return None
        self._send()
        return "break"

    def _send(self) -> None:
        if not self._client:
            messagebox.showwarning(
                "Not Authenticated",
                "Please authenticate via Settings → GitHub Authentication…",
            )
            return

        text = self._input.get("1.0", tk.END).strip()
        if not text and not self._attachments:
            return

        # ---- Build OpenAI-style message content ----
        if self._attachments:
            parts: list[dict] = []
            if text:
                parts.append({"type": "text", "text": text})
            for att in self._attachments:
                if att["type"] == "image":
                    parts.append({
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{att['mime']};base64,{att['data']}"
                        },
                    })
                elif att["type"] in ("text", "error"):
                    parts.append({"type": "text", "text": att["data"]})
            content: list | str = parts
        else:
            content = text

        # ---- Append to history ----
        self._history.append({"role": "user", "content": content})

        # ---- Display in chat ----
        display = text
        for att in self._attachments:
            display += f"\n[📎 {att['name']}]"
        self._append("You:", display, "user")

        # ---- Reset input / attachments ----
        self._input.delete("1.0", tk.END)
        self._attachments = []
        self._refresh_attach_bar()

        # ---- Kick off background request ----
        self._send_btn.config(state=tk.DISABLED)
        model_id = MODELS[self._model_var.get()]
        model_label = self._model_var.get()
        threading.Thread(
            target=self._worker,
            args=(list(self._history), model_id, model_label),
            daemon=True,
        ).start()

    def _worker(self, messages: list[dict],
                model_id: str, model_label: str) -> None:
        """Background thread: call the Copilot API and push results to queue."""
        try:
            self._queue.put(("start", model_label))
            full = ""
            for chunk in self._client.chat(messages, model=model_id,
                                           stream=True):
                full += chunk
                self._queue.put(("chunk", chunk))
            self._queue.put(("done", full))
        except Exception as exc:  # noqa: BLE001
            self._queue.put(("error", str(exc)))

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Start the Tk main loop."""
        self.root.mainloop()
