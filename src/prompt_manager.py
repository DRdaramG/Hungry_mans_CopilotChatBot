"""
Personal-prompt manager.

Prompts are stored as a JSON object in ``Asset/prompts.json`` inside the
project root.  Each entry maps a prompt name to
``{"content": "...", "role": "system"}``.

Legacy files that map name → plain string are upgraded transparently on
load (defaulting to ``role="system"``).
"""

import json
import os

from .paths import asset_path

# ---------------------------------------------------------------------------
# Special layout slots — always present, cannot be edited or deleted.
# They mark where chat history and user input are placed in the prompt layout.
# ---------------------------------------------------------------------------

SLOT_CHAT_HISTORY = "{{CHAT_HISTORY}}"
SLOT_USER_INPUT = "{{USER_INPUT}}"
SLOTS = frozenset({SLOT_CHAT_HISTORY, SLOT_USER_INPUT})

SLOT_DISPLAY_NAMES: dict[str, str] = {
    SLOT_CHAT_HISTORY: "[이전 채팅 기록]",
    SLOT_USER_INPUT: "[유저 인풋]",
}

SLOT_DESCRIPTIONS: dict[str, str] = {
    SLOT_CHAT_HISTORY: (
        "이전 채팅 기록이 이 위치에 삽입됩니다.\n"
        "남은 토큰 예산에 맞게 가장 최근 대화부터 채워집니다.\n\n"
        "Chat history will be inserted at this position.\n"
        "Most recent messages are included first within the "
        "remaining token budget."
    ),
    SLOT_USER_INPUT: (
        "현재 사용자의 입력이 이 위치에 삽입됩니다.\n\n"
        "The current user input will be inserted at this position."
    ),
}

#: Valid roles for prompts (matches the OpenAI/Anthropic API).
VALID_ROLES: tuple[str, ...] = ("system", "user", "assistant")
DEFAULT_ROLE: str = "system"


class PromptManager:
    """CRUD wrapper around a JSON file of named prompt strings."""

    DEFAULT_FILE = asset_path("prompts.json")
    ACTIVE_FILE = asset_path("active_prompts.json")

    def __init__(self, storage_file: str | None = None,
                 active_file: str | None = None) -> None:
        self.storage_file = storage_file or self.DEFAULT_FILE
        self._active_file = active_file or self.ACTIVE_FILE
        # Each value: {"content": str, "role": str}
        self._prompts: dict[str, dict] = {}
        self._active: set[str] = set()          # names of checked prompts
        self._order: list[str] = []             # display / activation order
        self._load()
        self._load_active()
        self._ensure_slots()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _load(self) -> None:
        if os.path.exists(self.storage_file):
            with open(self.storage_file, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            # Upgrade legacy format:  {name: "content_str"}
            # to current format:      {name: {"content": ..., "role": ...}}
            self._prompts = {}
            for name, value in raw.items():
                if isinstance(value, str):
                    self._prompts[name] = {
                        "content": value,
                        "role": DEFAULT_ROLE,
                    }
                elif isinstance(value, dict):
                    self._prompts[name] = {
                        "content": value.get("content", ""),
                        "role": value.get("role", DEFAULT_ROLE),
                    }
        # Default order = dict insertion order
        self._order = list(self._prompts.keys())

    def _save(self) -> None:
        with open(self.storage_file, "w", encoding="utf-8") as fh:
            json.dump(self._prompts, fh, ensure_ascii=False, indent=2)

    def _load_active(self) -> None:
        """Load the active set **and** display order from disk.

        Supports two on-disk formats:
        * Legacy — plain list of active names ``["a", "b"]``
        * Current — ``{"order": [...], "active": [...]}``
        """
        if not os.path.exists(self._active_file):
            return
        try:
            with open(self._active_file, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            known = set(self._prompts.keys())
            if isinstance(data, dict):
                # Current format
                saved_order = data.get("order", [])
                active_list = data.get("active", [])
                known_with_slots = known | SLOTS
                self._order = [n for n in saved_order if n in known_with_slots]
                for name in self._prompts:
                    if name not in self._order:
                        self._order.append(name)
                self._active = set(active_list) & known
            elif isinstance(data, list):
                # Legacy format (just active names)
                self._active = set(data) & known
        except (json.JSONDecodeError, OSError):
            self._active = set()

    def _save_active(self) -> None:
        """Persist active set **and** display order."""
        data = {
            "order": self._order,
            "active": sorted(self._active),
        }
        with open(self._active_file, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def add(self, name: str, content: str, role: str = DEFAULT_ROLE) -> None:
        """Add or overwrite a prompt.  Slot names are rejected."""
        if name in SLOTS:
            return
        if role not in VALID_ROLES:
            role = DEFAULT_ROLE
        is_new = name not in self._prompts
        self._prompts[name] = {"content": content, "role": role}
        if is_new:
            # Insert before the user-input slot so new prompts default
            # to appearing right before the user's message.
            if SLOT_USER_INPUT in self._order:
                idx = self._order.index(SLOT_USER_INPUT)
                self._order.insert(idx, name)
            else:
                self._order.append(name)
        self._save()
        self._save_active()

    def delete(self, name: str) -> None:
        """Remove a prompt (no-op if the name does not exist or is a slot)."""
        if name in SLOTS:
            return
        if name in self._prompts:
            del self._prompts[name]
            self._active.discard(name)
            if name in self._order:
                self._order.remove(name)
            self._save()
            self._save_active()

    def get(self, name: str) -> str:
        """Return a prompt's content, or an empty string if not found."""
        entry = self._prompts.get(name)
        if entry is None:
            return ""
        return entry["content"]

    def get_role(self, name: str) -> str:
        """Return the role of a prompt (defaults to ``"system"``)."""
        entry = self._prompts.get(name)
        if entry is None:
            return DEFAULT_ROLE
        return entry.get("role", DEFAULT_ROLE)

    def set_role(self, name: str, role: str) -> None:
        """Change the role of an existing prompt and persist."""
        if name not in self._prompts or name in SLOTS:
            return
        if role not in VALID_ROLES:
            role = DEFAULT_ROLE
        self._prompts[name]["role"] = role
        self._save()

    def list_names(self) -> list[str]:
        """Return all prompt names in display order."""
        return list(self._order)

    # ------------------------------------------------------------------
    # Active (checked) prompt management
    # ------------------------------------------------------------------

    def is_active(self, name: str) -> bool:
        """Return whether *name* is checked (active)."""
        return name in self._active

    def set_active(self, name: str, active: bool) -> None:
        """Mark *name* as active or inactive and persist."""
        if active:
            self._active.add(name)
        else:
            self._active.discard(name)
        self._save_active()

    def get_active_contents(self) -> list[tuple[str, str, str]]:
        """Return ``[(name, content, role), …]`` for all active prompts.

        Results follow the current display order so that drag-reordering
        in the Prompt Manager affects the activation order.
        """
        return [
            (name, self._prompts[name]["content"], self._prompts[name]["role"])
            for name in self._order
            if name in self._active and name in self._prompts
        ]

    def reorder(self, new_order: list[str]) -> None:
        """Set a new display / activation order.

        *new_order* should contain all existing prompt names and slots.
        Any missing names are appended at the end; unknown names are ignored.
        """
        known = set(self._prompts.keys()) | SLOTS
        self._order = [n for n in new_order if n in known]
        for name in self._prompts:
            if name not in self._order:
                self._order.append(name)
        for slot in (SLOT_CHAT_HISTORY, SLOT_USER_INPUT):
            if slot not in self._order:
                self._order.append(slot)
        self._save_active()

    # ------------------------------------------------------------------
    # Slot helpers
    # ------------------------------------------------------------------

    @staticmethod
    def is_slot(name: str) -> bool:
        """Return *True* if *name* is a special layout slot."""
        return name in SLOTS

    def get_ordered_layout(self) -> list[tuple[str, str, str, str]]:
        """Return the ordered layout as ``[(type, name, content, role), …]``.

        *type* is ``"slot"`` for the two special slots and ``"prompt"``
        for active user prompts.  Inactive prompts are excluded.
        """
        result: list[tuple[str, str, str, str]] = []
        for name in self._order:
            if name in SLOTS:
                result.append(("slot", name, "", ""))
            elif name in self._active and name in self._prompts:
                entry = self._prompts[name]
                result.append((
                    "prompt", name,
                    entry["content"], entry.get("role", DEFAULT_ROLE),
                ))
        return result

    # ------------------------------------------------------------------
    # Internal — slot management
    # ------------------------------------------------------------------

    def _ensure_slots(self) -> None:
        """Ensure the two special slots exist in the display order."""
        changed = False
        if SLOT_CHAT_HISTORY not in self._order:
            self._order.insert(0, SLOT_CHAT_HISTORY)
            changed = True
        if SLOT_USER_INPUT not in self._order:
            self._order.append(SLOT_USER_INPUT)
            changed = True
        if changed:
            self._save_active()

    # ------------------------------------------------------------------
    # Import / export
    # ------------------------------------------------------------------

    def export(self, file_path: str) -> None:
        """Write all prompts to *file_path* as JSON."""
        with open(file_path, "w", encoding="utf-8") as fh:
            json.dump(self._prompts, fh, ensure_ascii=False, indent=2)

    def import_from(self, file_path: str, overwrite: bool = False) -> int:
        """
        Load prompts from *file_path*.

        Parameters
        ----------
        overwrite : When *True* the existing prompts are replaced entirely;
                    when *False* (default) imported entries are merged in
                    (existing entries with the same name are overwritten).

        Returns
        -------
        Number of prompts imported.
        """
        with open(file_path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        # Normalise + filter out slot names
        incoming: dict[str, dict] = {}
        for k, v in raw.items():
            if k in SLOTS:
                continue
            if isinstance(v, str):
                incoming[k] = {"content": v, "role": DEFAULT_ROLE}
            elif isinstance(v, dict):
                incoming[k] = {
                    "content": v.get("content", ""),
                    "role": v.get("role", DEFAULT_ROLE),
                }
        if overwrite:
            self._prompts = incoming
            self._order = list(incoming.keys())
        else:
            for name in incoming:
                if name not in self._prompts:
                    self._order.append(name)
            self._prompts.update(incoming)
        self._save()
        self._ensure_slots()          # re-add slots removed by overwrite
        return len(incoming)
