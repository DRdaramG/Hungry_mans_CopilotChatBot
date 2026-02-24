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
import logging
import os
import queue
import threading
import tkinter as tk
import webbrowser
from tkinter import filedialog, messagebox, scrolledtext, simpledialog, ttk

import requests

from .auth import delete_token, load_token, poll_for_token, request_device_code, save_token
from .chat_store import ChatStore
from .context_manager import build_messages_from_layout
from .copilot_api import MODELS, CopilotAPIError, CopilotClient
from .file_handler import process_file
from .prompt_manager import (
    SLOT_DESCRIPTIONS,
    SLOT_DISPLAY_NAMES,
    VALID_ROLES,
    PromptManager,
)

log = logging.getLogger("copilot_chatbot")


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
    """Multi-line text editor for a single prompt, with role selector."""

    def __init__(self, parent: tk.Widget, title: str, content: str,
                 role: str = "system") -> None:
        super().__init__(parent)
        self.title(title)
        self.geometry("520x350")
        self.grab_set()
        self.result: str | None = None
        self.role_result: str | None = None

        f = ttk.Frame(self, padding=10)
        f.pack(fill=tk.BOTH, expand=True)

        # Top row: role selector + buttons
        top = ttk.Frame(f)
        top.pack(fill=tk.X, pady=(0, 6))

        ttk.Label(top, text="Role:").pack(side=tk.LEFT, padx=(0, 4))
        self._role_var = tk.StringVar(value=role)
        role_cb = ttk.Combobox(
            top, textvariable=self._role_var,
            values=list(VALID_ROLES), state="readonly", width=10,
        )
        role_cb.pack(side=tk.LEFT, padx=(0, 12))

        ttk.Button(top, text="Save",
                   command=self._save).pack(side=tk.RIGHT, padx=4)
        ttk.Button(top, text="Cancel",
                   command=self.destroy).pack(side=tk.RIGHT)

        self._text = scrolledtext.ScrolledText(f, wrap=tk.WORD, font=("", 10))
        self._text.pack(fill=tk.BOTH, expand=True)
        self._text.insert(tk.END, content)

    def _save(self) -> None:
        self.result = self._text.get("1.0", tk.END).strip()
        self.role_result = self._role_var.get()
        self.destroy()


class _PromptManagerDialog(tk.Toplevel):
    """Dialog to browse, edit, and select saved prompts.

    Also provides Import / Export and per-prompt role selection.
    """

    def __init__(
        self,
        parent: tk.Widget,
        pm: PromptManager,
        on_select=None,
    ) -> None:
        super().__init__(parent)
        self.title("Prompt Manager")
        self.geometry("640x480")
        self.grab_set()

        self._pm = pm
        self._on_select = on_select
        self._check_vars: dict[str, tk.BooleanVar] = {}  # name → checkbox var
        self._role_vars: dict[str, tk.StringVar] = {}    # name → role var
        self._row_widgets: dict[str, ttk.Frame] = {}     # name → row frame
        self._name_labels: dict[str, ttk.Label] = {}     # name → label widget
        self._selected_name_val: str | None = None
        self._drag_name: str | None = None

        self._build_ui()
        self._refresh()

    def _build_ui(self) -> None:
        # ---- Prompt list + buttons --------------------------------------
        paned = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        paned.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        left = ttk.Frame(paned)
        paned.add(left, weight=3)

        # Scrollable frame of checkboxes
        self._list_canvas = tk.Canvas(left, highlightthickness=0)
        self._list_scrollbar = ttk.Scrollbar(
            left, orient=tk.VERTICAL, command=self._list_canvas.yview,
        )
        self._list_inner = ttk.Frame(self._list_canvas)
        self._list_inner.bind(
            "<Configure>",
            lambda _e: self._list_canvas.configure(
                scrollregion=self._list_canvas.bbox("all"),
            ),
        )
        self._list_canvas_window = self._list_canvas.create_window(
            (0, 0), window=self._list_inner, anchor="nw",
        )
        self._list_canvas.configure(yscrollcommand=self._list_scrollbar.set)

        self._list_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self._list_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # Resize inner frame width to match canvas width
        self._list_canvas.bind(
            "<Configure>",
            lambda e: self._list_canvas.itemconfig(
                self._list_canvas_window, width=e.width,
            ),
        )

        right = ttk.Frame(paned)
        paned.add(right, weight=1)

        for text, cmd in [
            ("New",       self._new),
            ("Edit",      self._edit),
            ("Delete",    self._delete),
            ("Use",       self._use),
            ("",          None),  # separator
            ("Import…",   self._import_prompts),
            ("Export…",   self._export_prompts),
            ("",          None),  # separator
            ("Close",     self.destroy),
        ]:
            if not text:
                ttk.Separator(right, orient=tk.HORIZONTAL).pack(
                    fill=tk.X, padx=4, pady=4,
                )
                continue
            ttk.Button(right, text=text, command=cmd).pack(
                fill=tk.X, padx=4, pady=2)

        # ---- Bottom: Preview -------------------------------------------
        preview_frame = ttk.LabelFrame(self, text="Preview", padding=4)
        preview_frame.pack(fill=tk.X, padx=8, pady=(0, 8))

        self._preview_text = tk.Text(preview_frame, height=4,
                                     wrap=tk.WORD, state=tk.DISABLED,
                                     font=("", 9))
        self._preview_text.pack(fill=tk.X)

    # -- list helpers -----------------------------------------------------

    def _refresh(self) -> None:
        # Clear existing rows
        for w in self._list_inner.winfo_children():
            w.destroy()
        self._check_vars.clear()
        self._role_vars.clear()
        self._row_widgets.clear()
        self._name_labels.clear()
        self._selected_name_val = None

        for name in self._pm.list_names():
            row = ttk.Frame(self._list_inner)
            row.pack(fill=tk.X, padx=2, pady=1)
            self._row_widgets[name] = row

            # Drag handle (always present)
            handle = ttk.Label(row, text="\u2261", font=("", 11),
                               cursor="fleur", width=2)
            handle.pack(side=tk.LEFT, padx=(0, 2))
            handle.bind("<ButtonPress-1>",
                        lambda e, n=name: self._drag_start(e, n))
            handle.bind("<B1-Motion>", self._drag_motion)
            handle.bind("<ButtonRelease-1>", self._drag_end)

            if PromptManager.is_slot(name):
                # --- Slot row: lock icon, italic label, no checkbox ---
                lock = ttk.Label(row, text="\U0001F512", font=("", 9),
                                 width=2)
                lock.pack(side=tk.LEFT)

                display = SLOT_DISPLAY_NAMES.get(name, name)
                lbl = ttk.Label(row, text=display,
                                font=("", 10, "italic"),
                                foreground="#888888", cursor="hand2")
                lbl.pack(side=tk.LEFT, fill=tk.X, expand=True)
                lbl.bind("<Button-1>",
                         lambda _e, n=name: self._select_row(n))
            else:
                # --- Regular prompt row: checkbox + role + label ---
                var = tk.BooleanVar(value=self._pm.is_active(name))
                self._check_vars[name] = var

                cb = ttk.Checkbutton(
                    row, variable=var,
                    command=lambda n=name, v=var: self._on_check_toggled(n, v),
                )
                cb.pack(side=tk.LEFT)

                # Role dropdown
                role_var = tk.StringVar(value=self._pm.get_role(name))
                self._role_vars[name] = role_var
                role_cb = ttk.Combobox(
                    row, textvariable=role_var,
                    values=list(VALID_ROLES), state="readonly", width=8,
                )
                role_cb.pack(side=tk.LEFT, padx=(2, 4))
                role_cb.bind(
                    "<<ComboboxSelected>>",
                    lambda _e, n=name, rv=role_var: self._on_role_changed(n, rv),
                )

                lbl = ttk.Label(row, text=name, font=("", 10),
                                cursor="hand2")
                lbl.pack(side=tk.LEFT, fill=tk.X, expand=True)
                lbl.bind("<Button-1>",
                         lambda _e, n=name: self._select_row(n))

            self._name_labels[name] = lbl

        self._list_inner.update_idletasks()

    def _on_check_toggled(self, name: str, var: tk.BooleanVar) -> None:
        """Called when a checkbox is toggled — persist active state."""
        self._pm.set_active(name, var.get())

    def _on_role_changed(self, name: str, var: tk.StringVar) -> None:
        """Called when a role dropdown is changed — persist role."""
        self._pm.set_role(name, var.get())

    def _select_row(self, name: str) -> None:
        """Highlight a row and show its preview (for edit/delete/use)."""
        self._selected_name_val = name
        # Visual feedback: bold the selected label, un-bold others
        for n, lbl in self._name_labels.items():
            if n == name:
                lbl.configure(font=("", 10, "bold"),
                              foreground="#005cc5")
            else:
                if PromptManager.is_slot(n):
                    lbl.configure(font=("", 10, "italic"),
                                  foreground="#888888")
                else:
                    lbl.configure(font=("", 10),
                                  foreground="")
        self._preview()

    def _preview(self, _event=None) -> None:
        name = self._selected_name_val
        if not name:
            return
        if PromptManager.is_slot(name):
            content = SLOT_DESCRIPTIONS.get(name, "")
        else:
            content = self._pm.get(name)
        self._preview_text.config(state=tk.NORMAL)
        self._preview_text.delete("1.0", tk.END)
        self._preview_text.insert(tk.END, content if content else "(empty)")
        self._preview_text.config(state=tk.DISABLED)
        self._preview_text.update_idletasks()

    def _selected_name(self) -> str | None:
        return self._selected_name_val

    # -- Drag-to-reorder -------------------------------------------------

    def _drag_start(self, event, name: str) -> None:
        """Begin dragging a prompt row."""
        self._drag_name = name
        self._select_row(name)

    def _drag_motion(self, event) -> None:
        """While dragging, swap rows as the mouse crosses midpoints."""
        if not self._drag_name:
            return
        mouse_y = event.widget.winfo_rooty() + event.y
        names = self._pm.list_names()
        if self._drag_name not in names:
            return
        drag_idx = names.index(self._drag_name)

        for i, name in enumerate(names):
            row = self._row_widgets.get(name)
            if not row or not row.winfo_exists():
                continue
            row_top = row.winfo_rooty()
            row_h = row.winfo_height()
            row_mid = row_top + row_h // 2

            if i < drag_idx and mouse_y < row_mid:
                new_order = list(names)
                new_order.pop(drag_idx)
                new_order.insert(i, self._drag_name)
                self._pm.reorder(new_order)
                self._repack_rows()
                break
            elif i > drag_idx and mouse_y > row_mid:
                new_order = list(names)
                new_order.pop(drag_idx)
                new_order.insert(i, self._drag_name)
                self._pm.reorder(new_order)
                self._repack_rows()
                break

    def _drag_end(self, _event) -> None:
        """Finish dragging."""
        self._drag_name = None

    def _repack_rows(self) -> None:
        """Re-pack row widgets in the current order without recreating."""
        for name in self._pm.list_names():
            row = self._row_widgets.get(name)
            if row and row.winfo_exists():
                row.pack_forget()
        for name in self._pm.list_names():
            row = self._row_widgets.get(name)
            if row and row.winfo_exists():
                row.pack(fill=tk.X, padx=2, pady=1)

    # -- CRUD -------------------------------------------------------------

    def _new(self) -> None:
        name = simpledialog.askstring("New Prompt", "Prompt name:", parent=self)
        if not name:
            return
        dlg = _PromptEditDialog(self, f"New Prompt — {name}", "",
                                role="system")
        self.wait_window(dlg)
        if dlg.result is not None:
            self._pm.add(name, dlg.result,
                         role=dlg.role_result or "system")
            self._refresh()

    def _edit(self) -> None:
        name = self._selected_name()
        if not name or PromptManager.is_slot(name):
            return
        dlg = _PromptEditDialog(self, f"Edit — {name}",
                                self._pm.get(name),
                                role=self._pm.get_role(name))
        self.wait_window(dlg)
        if dlg.result is not None:
            self._pm.add(name, dlg.result,
                         role=dlg.role_result or "system")
            self._refresh()

    def _delete(self) -> None:
        name = self._selected_name()
        if not name or PromptManager.is_slot(name):
            return
        if messagebox.askyesno("Delete",
                               f"Delete prompt '{name}'?", parent=self):
            self._pm.delete(name)
            self._refresh()

    def _use(self) -> None:
        name = self._selected_name()
        if not name or PromptManager.is_slot(name):
            return
        if self._on_select:
            self._on_select(self._pm.get(name))
        self.destroy()

    # -- Import / Export --------------------------------------------------

    def _import_prompts(self) -> None:
        path = filedialog.askopenfilename(
            title="Import Prompts",
            filetypes=[("JSON", "*.json"), ("All files", "*.*")],
            parent=self,
        )
        if not path:
            return
        try:
            count = self._pm.import_from(path)
            messagebox.showinfo("Import", f"{count} prompt(s) imported.",
                                parent=self)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Import Error", str(exc), parent=self)
            return
        # Refresh after the messagebox is dismissed
        self._refresh()

    def _export_prompts(self) -> None:
        path = filedialog.asksaveasfilename(
            title="Export Prompts",
            defaultextension=".json",
            filetypes=[("JSON", "*.json"), ("All files", "*.*")],
            parent=self,
        )
        if path:
            self._pm.export(path)
            messagebox.showinfo("Export", f"Prompts exported to:\n{path}",
                                parent=self)


class _EditMessageDialog(tk.Toplevel):
    """Dialog for editing a chat message."""

    def __init__(self, parent: tk.Tk, title: str, content: str) -> None:
        super().__init__(parent)
        self.title(title)
        self.geometry("600x400")
        self.resizable(True, True)
        self.grab_set()
        self.result: str | None = None

        f = ttk.Frame(self, padding=10)
        f.pack(fill=tk.BOTH, expand=True)

        ttk.Label(
            f, text="메시지 내용을 수정하세요:",
            font=("", 10),
        ).pack(anchor=tk.W, pady=(0, 4))

        self._text = scrolledtext.ScrolledText(
            f, wrap=tk.WORD, font=("", 10),
        )
        self._text.pack(fill=tk.BOTH, expand=True)
        self._text.insert("1.0", content)

        btn_frame = ttk.Frame(f)
        btn_frame.pack(fill=tk.X, pady=(8, 0))
        ttk.Button(btn_frame, text="Save",
                   command=self._save).pack(side=tk.RIGHT, padx=4)
        ttk.Button(btn_frame, text="Cancel",
                   command=self.destroy).pack(side=tk.RIGHT)

        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self._text.focus_set()

    def _save(self) -> None:
        self.result = self._text.get("1.0", tk.END).rstrip("\n")
        self.destroy()


# ---------------------------------------------------------------------------
# Main application
# ---------------------------------------------------------------------------

class CopilotChatApp:
    """Hungry Man's Copilot ChatBot — main application class."""

    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("Hungry Man's Copilot ChatBot 🍽️")
        self.root.geometry("1100x720")
        self.root.minsize(800, 520)

        self._client: CopilotClient | None = None
        self._store = ChatStore()
        self._attachments: list[dict] = []   # FileData dicts waiting to send
        self._pm = PromptManager()
        self._queue: queue.Queue = queue.Queue()
        self._model_var = tk.StringVar()
        self._status_var = tk.StringVar(value="⚠️  Not authenticated")

        # Lazy-loading state: how many messages are currently displayed
        self._displayed_offset: int = 0  # newest msgs already skipped (0 = none)
        self._all_loaded: bool = False     # True when no more older msgs exist
        self._loading_more: bool = False   # guard against concurrent loads

        # Edit-button tracking for streaming assistant messages
        self._last_edit_btn: tk.Button | None = None
        self._streaming_conv_id: str | None = None
        self._expected_asst_seq: int | None = None

        self._build_menu()
        self._build_layout()
        self._build_model_bar()
        self._build_chat_area()
        self._build_attach_area()
        self._build_input_area()

        self._refresh_sidebar()
        self._render_history()
        self._check_saved_token()
        self._pump_queue()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_menu(self) -> None:
        bar = tk.Menu(self.root)
        self.root.config(menu=bar)

        file_menu = tk.Menu(bar, tearoff=False)
        bar.add_cascade(label="File", menu=file_menu)
        file_menu.add_command(label="Save Chat…", command=self._save_chat)
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self.root.quit)

        settings_menu = tk.Menu(bar, tearoff=False)
        bar.add_cascade(label="Settings", menu=settings_menu)
        settings_menu.add_command(label="GitHub Authentication…",
                                   command=self._run_auth)
        settings_menu.add_command(label="Clear Saved Token",
                                   command=self._clear_token)

    def _build_layout(self) -> None:
        """Create the sidebar + right-content-area split."""
        self._main_paned = ttk.PanedWindow(self.root, orient=tk.HORIZONTAL)
        self._main_paned.pack(fill=tk.BOTH, expand=True)

        # -- Sidebar --
        sidebar = ttk.Frame(self._main_paned, width=200)
        self._main_paned.add(sidebar, weight=0)

        btn_frame = ttk.Frame(sidebar)
        btn_frame.pack(fill=tk.X, padx=4, pady=4)
        self._new_chat_btn = ttk.Button(
            btn_frame, text="+ New Chat", command=self._new_conversation,
        )
        self._new_chat_btn.pack(fill=tk.X, pady=(0, 2))
        self._del_chat_btn = ttk.Button(
            btn_frame, text="🗑 Delete Chat", command=self._delete_conversation,
        )
        self._del_chat_btn.pack(fill=tk.X)

        self._conv_listbox = tk.Listbox(
            sidebar, selectmode=tk.SINGLE, font=("", 10),
            activestyle="none",
        )
        self._conv_listbox.pack(fill=tk.BOTH, expand=True, padx=4, pady=(0, 4))
        self._conv_listbox.bind("<<ListboxSelect>>", self._on_conv_selected)
        self._conv_listbox.bind("<Double-Button-1>",
                                self._rename_conversation)

        # Right-click context menu
        self._conv_menu = tk.Menu(self._conv_listbox, tearoff=False)
        self._conv_menu.add_command(label="Rename…",
                                    command=self._rename_conversation)
        self._conv_menu.add_command(label="Delete",
                                    command=self._delete_conversation)
        self._conv_listbox.bind("<Button-3>", self._show_conv_menu)

        # -- Right content area --
        self._right_frame = ttk.Frame(self._main_paned)
        self._main_paned.add(self._right_frame, weight=1)

    def _build_model_bar(self) -> None:
        frame = ttk.LabelFrame(self._right_frame, text="Model",
                               padding=(8, 4))
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
        frame = ttk.Frame(self._right_frame)
        frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=6)

        self._chat = scrolledtext.ScrolledText(
            frame, wrap=tk.WORD, state=tk.DISABLED,
            font=("", 10), relief=tk.SUNKEN, borderwidth=1,
        )
        self._chat.pack(fill=tk.BOTH, expand=True)

        # Detect scroll-to-top for lazy loading
        self._chat.vbar.config(command=self._on_scroll)
        self._chat.bind("<MouseWheel>", self._on_mousewheel)

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
            self._right_frame, text="Attachments", padding=(6, 4)
        )
        # Not packed yet — shown only when attachments are present.
        self._attach_inner = ttk.Frame(self._attach_outer)
        self._attach_inner.pack(fill=tk.X)

    def _build_input_area(self) -> None:
        outer = ttk.Frame(self._right_frame, padding=(10, 4))
        outer.pack(fill=tk.X, side=tk.BOTTOM)
        self._input_frame = outer  # saved reference used by _refresh_attach_bar

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
        ttk.Button(act_col, text="Preview 👁", command=self._preview_payload,
                   width=9).pack(pady=2)
        ttk.Button(act_col, text="Clear 🗑", command=self._clear_chat,
                   width=9).pack(pady=2)

        outer.columnconfigure(1, weight=1)

    # ------------------------------------------------------------------
    # Chat display helpers
    # ------------------------------------------------------------------

    def _append(self, label: str, body: str, role: str,
                *, seq: int | None = None, conv_id: str | None = None) -> None:
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

        # Edit button for user / assistant messages
        if role in ("user", "asst") and conv_id and seq is not None:
            cid, s = conv_id, seq
            btn = tk.Button(
                self._chat, text="\u270f", font=("", 7),
                relief=tk.FLAT, cursor="hand2", padx=2, pady=0,
                command=lambda c=cid, sq=s: self._edit_message(c, sq),
            )
            self._chat.window_create(tk.END, window=btn)
            self._chat.insert(tk.END, " ")

        # Message number for user / assistant
        if role in ("user", "asst") and seq is not None:
            label = f"{seq + 1} {label}"

        self._chat.insert(tk.END, f"{label}\n", lbl_tag)
        if body:
            self._chat.insert(tk.END, body, body_tag)
        self._chat.config(state=tk.DISABLED)
        self._chat.see(tk.END)

    def _append_header(self, label: str, tag: str,
                       *, seq: int | None = None,
                       conv_id: str | None = None) -> None:
        """Insert only the speaker header (stream start)."""
        self._chat.config(state=tk.NORMAL)
        if self._chat.get("1.0", tk.END).strip():
            self._chat.insert(tk.END, "\n\n")

        # Edit button (command updated in _pump_queue "done")
        if conv_id and seq is not None:
            btn = tk.Button(
                self._chat, text="\u270f", font=("", 7),
                relief=tk.FLAT, cursor="hand2", padx=2, pady=0,
            )
            self._chat.window_create(tk.END, window=btn)
            self._chat.insert(tk.END, " ")
            self._last_edit_btn = btn
            label = f"{seq + 1} {label}"

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

    def _edit_message(self, conv_id: str, seq: int) -> None:
        """Open a dialog to edit a message in-place."""
        msg = self._store.get_message_by_seq(conv_id, seq)
        if not msg:
            return
        role = msg["role"]
        display = self._content_to_display(msg["content"])
        role_name = "You" if role == "user" else "Assistant"
        title = f"Edit Message #{seq + 1} ({role_name})"

        dlg = _EditMessageDialog(self.root, title, display)
        self.root.wait_window(dlg)
        if dlg.result is not None:
            self._store.update_message(conv_id, seq, dlg.result)
            self._render_history()

    # ------------------------------------------------------------------
    # Lazy-loading (scroll-to-top triggers older-message fetch)
    # ------------------------------------------------------------------

    def _on_scroll(self, *args) -> None:
        """Intercept scrollbar commands to detect scroll-to-top."""
        self._chat.yview(*args)
        self._check_scroll_top()

    def _on_mousewheel(self, event) -> None:
        """Detect mouse-wheel scroll reaching the top."""
        # Schedule check after the default scroll handler runs
        self.root.after(50, self._check_scroll_top)

    def _check_scroll_top(self) -> None:
        """If scrolled to the very top, load older messages."""
        if self._all_loaded or self._loading_more:
            return
        if not self._store.active_id:
            return
        top = self._chat.yview()[0]
        if top <= 0.0:
            self._load_more_messages()

    def _load_more_messages(self) -> None:
        """Prepend an older page of messages to the chat display."""
        conv_id = self._store.active_id
        if not conv_id:
            return

        from .chat_store import PAGE_SIZE

        self._loading_more = True
        older = self._store.get_messages(
            conv_id, limit=PAGE_SIZE, offset=self._displayed_offset,
        )

        if not older:
            self._all_loaded = True
            self._loading_more = False
            return

        self._chat.config(state=tk.NORMAL)
        old_end = self._chat.index(tk.END)

        # Insert messages in reverse order at "1.0" so oldest ends up at top
        for msg in reversed(older):
            role = msg.get("role", "user")
            content = msg.get("content", "")
            seq = msg.get("seq")
            display = self._content_to_display(content)

            tag_map = {
                "system":    ("sys_lbl",  "sys_msg"),
                "user":      ("user_lbl", "user_msg"),
                "assistant": ("asst_lbl", "asst_msg"),
            }
            lbl_tag, body_tag = tag_map.get(role, ("sys_lbl", "sys_msg"))

            # Separator
            self._chat.insert("1.0", "\n\n")

            # Body
            if display:
                self._chat.insert("1.0", display, body_tag)

            # Label (with number for user / assistant)
            if role == "system":
                self._chat.insert("1.0", "🔧 System:\n", lbl_tag)
            elif role == "user":
                num = f"{seq + 1} " if seq is not None else ""
                self._chat.insert("1.0", f" {num}You:\n", lbl_tag)
            else:
                num = f"{seq + 1} " if seq is not None else ""
                self._chat.insert("1.0", f" {num}Assistant:\n", lbl_tag)

            # Edit button (user and assistant only)
            if role in ("user", "assistant") and seq is not None:
                s = seq
                btn = tk.Button(
                    self._chat, text="✏", font=("", 7),
                    relief=tk.FLAT, cursor="hand2", padx=2, pady=0,
                    command=lambda c=conv_id, sq=s: self._edit_message(c, sq),
                )
                self._chat.window_create("1.0", window=btn)

        self._chat.config(state=tk.DISABLED)
        self._chat.see(old_end)

        self._displayed_offset += len(older)
        total = self._store.message_count(conv_id)
        if self._displayed_offset >= total:
            self._all_loaded = True

        self._loading_more = False

    @staticmethod
    def _content_to_display(content) -> str:
        """Convert message content (str or list) to display text."""
        if isinstance(content, list):
            texts = []
            for p in content:
                if isinstance(p, dict) and p.get("type") == "text":
                    texts.append(p.get("text", ""))
            return "\n".join(texts)
        return content

    # ------------------------------------------------------------------
    # Queue pump (bridges worker thread → main thread)
    # ------------------------------------------------------------------

    def _pump_queue(self) -> None:
        try:
            while True:
                kind, payload = self._queue.get_nowait()
                if kind == "start":
                    self._append_header(
                        f"{payload}:", "asst_lbl",
                        seq=self._expected_asst_seq,
                        conv_id=self._streaming_conv_id,
                    )
                elif kind == "chunk":
                    self._stream_chunk(payload)
                elif kind == "done":
                    if self._store.active_id:
                        actual_seq = self._store.add_message(
                            self._store.active_id,
                            "assistant", payload,
                        )
                        self._store.auto_title(self._store.active_id)
                        # Bind edit button now that we know the real seq
                        btn = self._last_edit_btn
                        if btn:
                            cid = self._store.active_id
                            btn.config(
                                command=lambda c=cid, s=actual_seq:
                                    self._edit_message(c, s),
                            )
                            self._last_edit_btn = None
                    self._send_btn.config(state=tk.NORMAL)
                    self._conv_listbox.config(state=tk.NORMAL)
                    self._new_chat_btn.config(state=tk.NORMAL)
                    self._refresh_sidebar()
                elif kind == "error":
                    self._append("⚠️  Error", payload, "error")
                    self._send_btn.config(state=tk.NORMAL)
                    self._conv_listbox.config(state=tk.NORMAL)
                    self._new_chat_btn.config(state=tk.NORMAL)
                elif kind == "sys":
                    self._sys_msg(payload)
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
            threading.Thread(
                target=self._load_model_limits, daemon=True,
            ).start()
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
            threading.Thread(
                target=self._load_model_limits, daemon=True,
            ).start()

    def _clear_token(self) -> None:
        delete_token()
        self._client = None
        self._status_var.set("⚠️  Not authenticated")
        self._sys_msg("Saved token removed. Please re-authenticate.")

    def _load_model_limits(self) -> None:
        """Background: fetch per-model token limits from the API."""
        if not self._client:
            return
        try:
            limits = self._client.fetch_model_limits()
            if limits:
                lines = ["Model token limits loaded:"]
                seen: set[int] = set()  # deduplicate normalised aliases
                for mid, lim in limits.items():
                    obj_id = id(lim)
                    if obj_id in seen:
                        continue
                    seen.add(obj_id)
                    lines.append(
                        f"  \u2022 {mid}: prompt {lim.max_prompt_tokens:,}"
                        f" / output {lim.max_output_tokens:,} tokens"
                    )
                self._queue.put(("sys", "\n".join(lines)))
        except Exception as exc:  # noqa: BLE001
            log.warning("[APP] Could not load model limits: %s", exc)

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
                fill=tk.X, padx=10, pady=2, before=self._input_frame
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
        _PromptManagerDialog(
            self.root,
            self._pm,
            on_select=self._insert_prompt,
        )

    def _insert_prompt(self, content: str) -> None:
        self._input.delete("1.0", tk.END)
        self._input.insert(tk.END, content)

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
            messages = (self._store.get_all_messages(self._store.active_id)
                        if self._store.active_id else [])
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(messages, fh, ensure_ascii=False, indent=2)
        else:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(self._chat.get("1.0", tk.END))
        messagebox.showinfo("Saved", f"Chat saved to:\n{path}")

    def _clear_chat(self) -> None:
        if not messagebox.askyesno("Clear Chat",
                                   "Clear all messages from this session?"):
            return
        conv_id = self._store.active_id
        if conv_id:
            self._store.clear_messages(conv_id)

        self._chat.config(state=tk.NORMAL)
        self._chat.delete("1.0", tk.END)
        self._chat.config(state=tk.DISABLED)

        self._displayed_offset = 0
        self._all_loaded = True  # nothing to load after clear

        self._sys_msg("Chat cleared.")

    # ------------------------------------------------------------------
    # Preview payload
    # ------------------------------------------------------------------

    def _preview_payload(self) -> None:
        """Build the full API payload from the current input and show it
        in a read-only popup — **without** sending anything to the LLM."""
        if not self._client:
            messagebox.showwarning(
                "Not Authenticated",
                "Please authenticate via Settings → GitHub Authentication…",
            )
            return

        text = self._input.get("1.0", tk.END).strip()
        if not text and not self._attachments:
            return

        # ---- Build user content (same logic as _send) ----
        if self._attachments:
            parts: list[dict] = []
            if text:
                parts.append({"type": "text", "text": text})
            for att in self._attachments:
                if att["type"] == "image":
                    parts.append({
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{att['mime']};base64,{att['data'][:40]}…(truncated)"
                        },
                    })
                elif att["type"] in ("text", "error"):
                    parts.append({"type": "text", "text": att["data"]})
            content: list | str = parts
        else:
            content = text

        # ---- Assemble messages via layout ----
        layout = self._pm.get_ordered_layout()
        current_user_msg = {"role": "user", "content": content}

        all_msgs = (self._store.get_all_messages(self._store.active_id)
                    if self._store.active_id else [])

        system_prompt_msg = None
        history: list[dict] = []
        for msg in all_msgs:
            if msg.get("role") == "system":
                system_prompt_msg = msg
            else:
                history.append(msg)

        model_id = MODELS[self._model_var.get()]
        limits = (self._client.get_model_limits(model_id)
                  if self._client else None)
        max_prompt_tokens = limits.max_prompt_tokens if limits else 8_192

        try:
            assembled = build_messages_from_layout(
                layout, history, current_user_msg,
                system_prompt_msg=system_prompt_msg,
                max_tokens=max_prompt_tokens,
            )
        except ValueError as exc:
            messagebox.showerror("Token Limit", str(exc))
            return

        # ---- Build model-specific payload via CopilotClient ----
        preview_data = self._client.build_preview_payload(
            assembled, model=model_id,
        )

        pretty = json.dumps(preview_data, indent=2, ensure_ascii=False,
                            default=str)

        # ---- Show in a popup window ----
        win = tk.Toplevel(self.root)
        win.title("Payload Preview")
        win.geometry("760x600")
        win.transient(self.root)
        win.grab_set()

        info_frame = ttk.Frame(win, padding=6)
        info_frame.pack(fill=tk.X)
        ttk.Label(info_frame,
                  text=f"Endpoint: {preview_data['endpoint']}").pack(anchor="w")
        ttk.Label(info_frame,
                  text=f"Model family: {preview_data['model_family']}  |  "
                       f"Messages: {preview_data['message_count']}").pack(anchor="w")

        txt = scrolledtext.ScrolledText(win, wrap=tk.WORD, font=("Consolas", 10))
        txt.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))
        txt.insert(tk.END, pretty)
        txt.config(state=tk.DISABLED)

        btn_frame = ttk.Frame(win, padding=(8, 4))
        btn_frame.pack(fill=tk.X)
        ttk.Button(btn_frame, text="Copy to Clipboard",
                   command=lambda: (win.clipboard_clear(),
                                    win.clipboard_append(pretty))
                   ).pack(side=tk.LEFT, padx=4)
        ttk.Button(btn_frame, text="Close",
                   command=win.destroy).pack(side=tk.RIGHT, padx=4)

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

        # ---- Build DB content (user input only, NO prompt prefix) --------
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

        # ---- Append to DB (raw user input only) ----
        user_seq = None
        if self._store.active_id:
            user_seq = self._store.add_message(
                self._store.active_id, "user", content,
            )
            self._store.auto_title(self._store.active_id)
            self._refresh_sidebar()

        # ---- Display in chat ----
        layout = self._pm.get_ordered_layout()
        prompt_names = [name for t, name, _, _r in layout if t == "prompt"]
        display = text
        if prompt_names:
            names = ", ".join(prompt_names)
            display = f"[📌 {names}]\n{text}" if text else f"[📌 {names}]"
        for att in self._attachments:
            display += f"\n[📎 {att['name']}]"
        self._append("You:", display, "user",
                     seq=user_seq, conv_id=self._store.active_id)

        # Track expected seq for streaming assistant message
        self._streaming_conv_id = self._store.active_id
        self._expected_asst_seq = user_seq + 1 if user_seq is not None else None

        # ---- Reset input / attachments ----
        self._input.delete("1.0", tk.END)
        self._attachments = []
        self._refresh_attach_bar()

        # ---- Separate message components for layout-based assembly ----
        self._send_btn.config(state=tk.DISABLED)
        self._conv_listbox.config(state=tk.DISABLED)
        self._new_chat_btn.config(state=tk.DISABLED)
        model_id = MODELS[self._model_var.get()]
        model_label = self._model_var.get()

        all_msgs = (self._store.get_all_messages(self._store.active_id)
                    if self._store.active_id else [])
        if not all_msgs:
            all_msgs = [{"role": "user", "content": content}]

        system_prompt_msg = None
        history: list[dict] = []
        current_user_msg = all_msgs[-1]          # always the latest user msg

        for msg in all_msgs[:-1]:
            if msg.get("role") == "system":
                system_prompt_msg = msg
            else:
                history.append(msg)

        # Determine token budget from cached model limits
        limits = (self._client.get_model_limits(model_id)
                  if self._client else None)
        max_prompt_tokens = limits.max_prompt_tokens if limits else 8_192

        try:
            assembled = build_messages_from_layout(
                layout, history, current_user_msg,
                system_prompt_msg=system_prompt_msg,
                max_tokens=max_prompt_tokens,
            )
        except ValueError as exc:
            self._send_btn.config(state=tk.NORMAL)
            self._conv_listbox.config(state=tk.NORMAL)
            self._new_chat_btn.config(state=tk.NORMAL)
            messagebox.showerror("Token Limit", str(exc))
            return

        threading.Thread(
            target=self._worker,
            args=(assembled, model_id, model_label),
            daemon=True,
        ).start()

    def _worker(self, messages: list[dict],
                model_id: str, model_label: str) -> None:
        """Background thread: call the Copilot API and push results to queue."""
        try:
            self._queue.put(("start", model_label))
            full = ""
            for chunk in self._client.chat(messages, model=model_id,
                                           stream=True,
                                           pre_assembled=True):
                full += chunk
                self._queue.put(("chunk", chunk))
            self._queue.put(("done", full))
        except CopilotAPIError as exc:
            # Rich error — already contains structured diagnostic info
            log.error("[APP] CopilotAPIError while chatting with %s: %s",
                      model_id, exc)
            self._queue.put(("error", str(exc)))
        except requests.HTTPError as exc:
            # Fallback for any requests error not caught internally
            status = exc.response.status_code if exc.response is not None else "?"
            body_preview = ""
            if exc.response is not None:
                body_preview = exc.response.text[:300] if exc.response.text else ""
            error_msg = (
                f"HTTP {status} error while calling Copilot API.\n"
                f"  Model: {model_id} ({model_label})\n"
                f"  {body_preview}"
            )
            log.error("[APP] %s", error_msg)
            self._queue.put(("error", error_msg))
        except requests.ConnectionError as exc:
            error_msg = (
                f"Network error — could not connect to Copilot API.\n"
                f"  Model: {model_id}\n"
                f"  Detail: {exc}"
            )
            log.error("[APP] %s", error_msg)
            self._queue.put(("error", error_msg))
        except Exception as exc:  # noqa: BLE001
            error_msg = (
                f"{type(exc).__name__}: {exc}\n"
                f"  Model: {model_id} ({model_label})\n"
                f"  This is an unexpected error. Copy this message to an LLM for debugging."
            )
            log.error("[APP] Unexpected error in _worker: %s", error_msg,
                      exc_info=True)
            self._queue.put(("error", error_msg))

    # ------------------------------------------------------------------
    # Conversation management (sidebar)
    # ------------------------------------------------------------------

    def _refresh_sidebar(self) -> None:
        """Update the conversation list in the sidebar."""
        cur_state = str(self._conv_listbox.cget("state"))
        # Temporarily enable to modify if disabled during streaming
        self._conv_listbox.config(state=tk.NORMAL)
        self._conv_listbox.delete(0, tk.END)
        active_idx = 0
        for i, conv in enumerate(self._store.list_conversations()):
            title = conv.title or "New Chat"
            self._conv_listbox.insert(tk.END, title)
            if conv.id == self._store.active_id:
                active_idx = i
        if self._conv_listbox.size() > 0:
            self._conv_listbox.selection_clear(0, tk.END)
            self._conv_listbox.selection_set(active_idx)
            self._conv_listbox.see(active_idx)
        # Restore previous state
        self._conv_listbox.config(state=cur_state)

    def _on_conv_selected(self, _event=None) -> None:
        sel = self._conv_listbox.curselection()
        if not sel:
            return
        convs = self._store.list_conversations()
        if sel[0] < len(convs):
            target = convs[sel[0]]
            if target.id != self._store.active_id:
                self._switch_conversation(target.id)

    def _switch_conversation(self, conv_id: str) -> None:
        self._store.save()
        self._store.active_id = conv_id
        self._render_history()
        self._refresh_sidebar()

    def _new_conversation(self) -> None:
        self._store.save()
        self._store.new_conversation()
        self._render_history()
        self._refresh_sidebar()

    def _rename_conversation(self, _event=None) -> None:
        sel = self._conv_listbox.curselection()
        if not sel:
            return
        convs = self._store.list_conversations()
        if sel[0] >= len(convs):
            return
        conv = convs[sel[0]]
        new_name = simpledialog.askstring(
            "Rename", "New name for this conversation:",
            initialvalue=conv.title, parent=self.root,
        )
        if new_name:
            self._store.rename_conversation(conv.id, new_name)
            self._refresh_sidebar()

    def _delete_conversation(self, _event=None) -> None:
        sel = self._conv_listbox.curselection()
        if not sel:
            return
        convs = self._store.list_conversations()
        if sel[0] >= len(convs):
            return
        conv = convs[sel[0]]
        if not messagebox.askyesno(
            "Delete Chat",
            f"'{conv.title}' 채팅을 정말 삭제할까요?\n\n삭제하면 되돌릴 수 없습니다.",
            icon=messagebox.WARNING,
            parent=self.root,
        ):
            return
        self._store.delete_conversation(conv.id)
        self._render_history()
        self._refresh_sidebar()

    def _show_conv_menu(self, event) -> None:
        try:
            idx = self._conv_listbox.nearest(event.y)
            self._conv_listbox.selection_clear(0, tk.END)
            self._conv_listbox.selection_set(idx)
            self._conv_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self._conv_menu.grab_release()

    def _render_history(self) -> None:
        """Re-render the chat display from the active conversation.

        Shows only the latest PAGE_SIZE messages.  Older messages are
        loaded on demand when the user scrolls to the top.
        """
        from .chat_store import PAGE_SIZE

        self._chat.config(state=tk.NORMAL)
        self._chat.delete("1.0", tk.END)
        self._chat.config(state=tk.DISABLED)

        conv_id = self._store.active_id
        if not conv_id:
            self._displayed_offset = 0
            self._all_loaded = True
            return

        messages = self._store.get_messages(conv_id, limit=PAGE_SIZE)
        total = self._store.message_count(conv_id)
        self._displayed_offset = len(messages)
        self._all_loaded = (self._displayed_offset >= total)

        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            seq = msg.get("seq")
            display = self._content_to_display(content)

            if role == "system":
                self._append("🔧 System:", display, "system")
            elif role == "user":
                self._append("You:", display, "user",
                             seq=seq, conv_id=conv_id)
            elif role == "assistant":
                self._append("Assistant:", display, "asst",
                             seq=seq, conv_id=conv_id)

        if not self._all_loaded:
            self._chat.config(state=tk.NORMAL)
            self._chat.insert("1.0", "⬆️  Scroll up to load older messages\n\n",
                              "sys_msg")
            self._chat.config(state=tk.DISABLED)

    def _on_close(self) -> None:
        """Save all state and exit."""
        self._store.save()
        self._store.close()
        self.root.destroy()

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Start the Tk main loop."""
        self.root.mainloop()
