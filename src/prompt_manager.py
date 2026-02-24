"""
Personal-prompt manager.

Prompts are stored as a JSON object (``{name: content}``) in
``Asset/prompts.json`` inside the project root.  The class also supports
exporting that file to an arbitrary path and importing from one.
"""

import json
import os

from .paths import asset_path


class PromptManager:
    """CRUD wrapper around a JSON file of named prompt strings."""

    DEFAULT_FILE = asset_path("prompts.json")
    ACTIVE_FILE = asset_path("active_prompts.json")

    def __init__(self, storage_file: str | None = None,
                 active_file: str | None = None) -> None:
        self.storage_file = storage_file or self.DEFAULT_FILE
        self._active_file = active_file or self.ACTIVE_FILE
        self._prompts: dict[str, str] = {}
        self._active: set[str] = set()          # names of checked prompts
        self._order: list[str] = []             # display / activation order
        self._load()
        self._load_active()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _load(self) -> None:
        if os.path.exists(self.storage_file):
            with open(self.storage_file, "r", encoding="utf-8") as fh:
                self._prompts = json.load(fh)
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
                self._order = [n for n in saved_order if n in known]
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

    def add(self, name: str, content: str) -> None:
        """Add or overwrite a prompt."""
        is_new = name not in self._prompts
        self._prompts[name] = content
        if is_new:
            self._order.append(name)
        self._save()
        self._save_active()

    def delete(self, name: str) -> None:
        """Remove a prompt (no-op if the name does not exist)."""
        if name in self._prompts:
            del self._prompts[name]
            self._active.discard(name)
            if name in self._order:
                self._order.remove(name)
            self._save()
            self._save_active()

    def get(self, name: str) -> str:
        """Return a prompt's content, or an empty string if not found."""
        return self._prompts.get(name, "")

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

    def get_active_contents(self) -> list[tuple[str, str]]:
        """Return ``[(name, content), …]`` for all active prompts.

        Results follow the current display order so that drag-reordering
        in the Prompt Manager affects the activation order.
        """
        return [
            (name, self._prompts[name])
            for name in self._order
            if name in self._active and name in self._prompts
        ]

    def reorder(self, new_order: list[str]) -> None:
        """Set a new display / activation order.

        *new_order* should contain all existing prompt names.  Any missing
        names are appended at the end; unknown names are ignored.
        """
        known = set(self._prompts.keys())
        self._order = [n for n in new_order if n in known]
        for name in self._prompts:
            if name not in self._order:
                self._order.append(name)
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
            incoming: dict[str, str] = json.load(fh)
        if overwrite:
            self._prompts = incoming
            self._order = list(incoming.keys())
        else:
            for name in incoming:
                if name not in self._prompts:
                    self._order.append(name)
            self._prompts.update(incoming)
        self._save()
        self._save_active()
        return len(incoming)
