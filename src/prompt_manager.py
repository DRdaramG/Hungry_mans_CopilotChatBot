"""
Personal-prompt manager.

Prompts are stored as a JSON object (``{name: content}``) in
``~/.copilot_chatbot_prompts.json`` by default.  The class also supports
exporting that file to an arbitrary path and importing from one.
"""

import json
import os


class PromptManager:
    """CRUD wrapper around a JSON file of named prompt strings."""

    DEFAULT_FILE = os.path.expanduser("~/.copilot_chatbot_prompts.json")

    def __init__(self, storage_file: str | None = None) -> None:
        self.storage_file = storage_file or self.DEFAULT_FILE
        self._prompts: dict[str, str] = {}
        self._load()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _load(self) -> None:
        if os.path.exists(self.storage_file):
            with open(self.storage_file, "r", encoding="utf-8") as fh:
                self._prompts = json.load(fh)

    def _save(self) -> None:
        with open(self.storage_file, "w", encoding="utf-8") as fh:
            json.dump(self._prompts, fh, ensure_ascii=False, indent=2)

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def add(self, name: str, content: str) -> None:
        """Add or overwrite a prompt."""
        self._prompts[name] = content
        self._save()

    def delete(self, name: str) -> None:
        """Remove a prompt (no-op if the name does not exist)."""
        if name in self._prompts:
            del self._prompts[name]
            self._save()

    def get(self, name: str) -> str:
        """Return a prompt's content, or an empty string if not found."""
        return self._prompts.get(name, "")

    def list_names(self) -> list[str]:
        """Return all prompt names in insertion order."""
        return list(self._prompts.keys())

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
        else:
            self._prompts.update(incoming)
        self._save()
        return len(incoming)
