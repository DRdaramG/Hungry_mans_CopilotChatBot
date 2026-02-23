# Hungry Man's Copilot ChatBot 🍽️

A Python-based desktop chatbot that connects to **GitHub Copilot**'s chat API,
giving Copilot Pro+ subscribers access to multiple premium AI models through a
friendly GUI — without paying for separate Claude, Gemini, or ChatGPT
subscriptions.

---

## Features

| Feature | Details |
|---|---|
| **Model toggle** | Switch between **Claude Opus 4.5**, **Gemini 3 Pro**, and **GPT-4.1** mid-conversation using radio buttons |
| **GitHub OAuth** | Device-flow authentication — no manual token copy-paste needed |
| **Multiple conversations** | Sidebar to create, rename, and delete named conversations; last active conversation is remembered between sessions |
| **Persistent chat history** | Conversations are stored in a local SQLite database (`~/.copilot_chatbot.db`); old JSON history is migrated automatically |
| **Token-aware context window** | Uses tiktoken to count tokens and trim history so every request fits within the model's context limit |
| **Image input** | Attach `.png`, `.jpg`, `.jpeg`, `.gif`, `.webp` files; sent as base64 to multimodal models |
| **Spreadsheet input** | Attach `.csv`, `.xls`, or `.xlsx` files; content is parsed and included as text |
| **Personal prompts** | Save, edit, and delete named prompts; activate multiple prompts with checkboxes (prepended automatically); drag-to-reorder |
| **System prompt** | Set a per-conversation system prompt directly from the Prompt Manager |
| **Import / export prompts** | Share prompt libraries as `.json` files |
| **Save chat** | Export the conversation as JSON or plain text |
| **Streaming responses** | Responses appear word-by-word as they arrive |
| **Lazy-load history** | Scroll to the top of the chat to load older messages on demand |

---

## Requirements

* Python **3.10** or later
* A **GitHub Copilot Pro+** subscription

### Python dependencies

```
pip install -r requirements.txt
```

| Package | Purpose |
|---|---|
| `requests` | HTTP calls to GitHub and Copilot APIs |
| `openpyxl` | Read `.xlsx` Excel files |
| `tiktoken` | Accurate token counting for context-window management |
| `typing_extensions` | Backport of newer `typing` features for Python < 3.11 |

`tkinter` is used for the GUI and ships with the standard Python installer on
Windows and macOS. On Linux you may need to install it separately:

```bash
# Debian / Ubuntu
sudo apt-get install python3-tk
```

---

## Quick start

```bash
# 1. Clone and install dependencies
git clone https://github.com/DRdaramG/Hungry_mans_CopilotChatBot.git
cd Hungry_mans_CopilotChatBot
pip install -r requirements.txt

# 2. Launch the app
python main.py
```

On first launch go to **Settings → GitHub Authentication…** and follow the
on-screen instructions to link your GitHub account.

---

## Project structure

```
Hungry_mans_CopilotChatBot/
├── main.py                 # Entry point
├── requirements.txt
├── README.md
└── src/
    ├── __init__.py
    ├── app.py              # tkinter GUI (main window + all dialogs)
    ├── auth.py             # GitHub device-flow OAuth + token persistence
    ├── chat_store.py       # SQLite-backed conversation & message storage
    ├── context_manager.py  # Token-aware context-window trimming (tiktoken)
    ├── copilot_api.py      # Copilot Chat API client (streaming)
    ├── file_handler.py     # Image / CSV / Excel file processing
    └── prompt_manager.py   # Named-prompt CRUD + import/export
```

---

## How authentication works

1. The app requests a **device code** from GitHub (using the public Copilot
   OAuth App client-id `Iv1.b507a08c87ecfe98`).
2. Your browser opens `https://github.com/login/device` — enter the displayed
   code to authorise.
3. The app polls GitHub until you complete authorisation, then stores the
   resulting GitHub token in `~/.copilot_chatbot_token.json`.
4. On every API call the GitHub token is exchanged for a short-lived
   **Copilot API bearer token** (valid ~30 minutes, refreshed automatically).

---

## Keyboard shortcuts

| Key | Action |
|---|---|
| `Enter` | Send message |
| `Shift + Enter` | Insert a newline in the input box |

---

## Development motivation

The author is a GitHub Copilot Pro+ subscriber (€39 / month — every lunch
skipped) and wanted to use the Claude, Gemini, and GPT-4o models already
included in that subscription through a comfortable desktop GUI, without
signing up for additional paid services.
