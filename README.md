# Order Notes

Lightweight, offline-first Windows application for managing customer requests, purchase orders, and optional SMS notifications. Built with Python, Tkinter, ttk, SQLite, and urllib; the desktop application does not require an online account or a cloud service.

## Features

- Track customer requests from **To order → Ordered → Arrived → Notified → Collected**.
- Store contact numbers, optional names, item descriptions, and private notes.
- High-contrast, compact interface for 800×600 displays; no main-window scrollbars, with paginated lists.
- Resize the sidebar and private-notes split; remember window sizes and positions locally per computer.
- Right-click context menu, confirmation before deleting a request, and confirmation before exiting.
- Single application instance per Windows PC; different PCs can access a common network share, subject to SQLite locking limitations described below.
- Editable SMS templates (Swedish examples included), two SIM options, message preview and send confirmation.
- Optional personal reminders, SMS activity history, backup/export, and protection against accidentally repeating uncertain SMS sends.
- Portable data in `OrderNotes_Portable/data/`; gateway credentials secured per Windows user using Windows DPAPI.

## Build for Windows

1. Install Python 3.12+ on Windows and ensure the `py` launcher is available.
2. Download or clone the repository and open its root directory.
3. Run `BUILD_WINDOWS.bat`. The script runs unit tests and a compact graphical-interface test before packaging with PyInstaller.
4. Run `OrderNotes_Portable/OrderNotes.exe` after a successful build.

The build script does not intentionally delete existing portable customer data. Before upgrading, close Order Notes on all PCs and **back up the complete `OrderNotes_Portable/data/` folder**. Keep that directory when replacing or moving program files. Do not install the portable application under a write-protected directory such as `Program Files`.

## Configure SMS

The app ships **without a preconfigured gateway address, login, password, or phone number**. In **Settings → SMS Gateway**, enter your own Android SMSGate URL, username, password, dialing country code, and preferred SIM. Swedish dialing code `46` is offered as a starting example and can be changed. Network traffic over plain HTTP is unencrypted: only use a trusted private network or HTTPS if supported by the gateway.

Customer messages require review and confirmation; an accepted/queued response from a gateway does not guarantee carrier delivery. After an ambiguous timeout, the app deliberately avoids automatic retries.

Starter SMS templates are generic and editable; customize them to identify your organization if desired.

## Data privacy

- Customer records, SMS history, and templates are stored in **local SQLite**, not uploaded to this repository or any cloud service by the application.
- By default, user records are **not encrypted** in the portable database. Restrict folder permissions and maintain backups.
- Gateway passwords are stored per Windows account using DPAPI, separately from the portable database. Some migrations may read previously saved credentials locally, but credentials are never distributed with this code.
- `.gitignore` excludes local databases, backups, drafts, secrets, environment files, and build output. **Do not commit real customer data or configuration exports.**

## Multi-computer use

Using the same executable and SQLite database over a Windows file share is supported on a best-effort basis, with rollback journaling, busy timeouts, revision checks, and periodic refresh. Correct filesystem locking is required and **cannot be guaranteed on all SMB/NAS setups**. For critical concurrent work, use a central database service instead. Avoid running older application versions against the same shared database; make backups before changing versions.

## Tests

Run unit tests in the repository root:

```shell
python -m unittest discover -s tests -v
```

A GUI smoke test is available with a display server:

```shell
python tests/gui_smoke.py
```

The build has been validated primarily through automated logic and graphical tests. Windows packaging, taskbar behavior, and real network sharing should be tested on the target systems before depending on them operationally.

## License

No open-source license is granted yet. Public visibility alone does not give permission to reuse, redistribute, or modify the source. A license can be added separately when the maintainer chooses one.
