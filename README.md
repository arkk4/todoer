# doer

> Terminal task & calendar manager on top of Apple Reminders & Calendar.

![doer badge](public/badge.svg)

**doer** is a fast, keyboard-driven terminal environment for managing tasks and calendar events directly synced with macOS Reminders & Calendar. Includes travel-time alarms computed via Apple Maps routing.

---

## Features

- ⚡️ **Reminders & Calendar sync** — native integration with macOS EventKit.
- 🚗 **Travel-time alarms** — transit, walking, and driving estimates via Apple Maps.
- 🧭 **Interactive TUI & one-shot CLI** — fast fuzzy command line and interactive dashboard.
- 🎨 **Rich visual styling** — catppuccin-mocha color scheme with inline logo banner in iTerm2.

---

## Quick Start

```bash
# Run interactive TUI
python3 cli/doer.py

# Or one-shot task creation
python3 cli/doer.py "Pay electricity bill @tomorrow 10:00 !"
```

---

## Requirements

- macOS 12+
- `reminders-cli` (`brew install reminders-cli`)
- Python 3.9+
- Node.js (for asset generation tools)

---

## Development

```bash
# Setup hooks and submodules
make setup

# Regenerate logo & badge
make badge

# Bump version
make bump
```
