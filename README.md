# Order Notes

A lightweight Windows desktop application for tracking customer requests, purchase orders, SMS notifications, and reminders.

**Features:** customer request lifecycle; optional product and private notes; editable Swedish SMS templates; Android SMSGate integration; reminder scheduling; high-contrast interface designed for small screens; right-click actions; layout persistence; single-instance guard; portable SQLite storage and backups.

## Windows setup

1. Install Python 3.12+ for Windows.
2. Download the source code and extract it.
3. Run `BUILD_WINDOWS.bat` to run the tests and build a portable EXE.
4. Launch `OrderNotes_Portable/OrderNotes.exe`.

The SMS gateway URL, credentials, and contact numbers are **not preconfigured**. Add your own values in Settings. All messages require confirmation, and a queued SMS is not proof of delivery.

## Data protection

Customer records are stored locally in the unencrypted portable SQLite database under `OrderNotes_Portable/data/`. Back up that directory and restrict access to it. Gateway passwords are stored per Windows user using DPAPI. Never upload data, configuration exports, secrets, or backup archives to GitHub.

The program supports best-effort concurrent access to a database on a Windows network share, but SQLite locking on SMB/NAS is not universally reliable. For business-critical shared use, a central database server is safer.

## Tests

```shell
python -m unittest discover -s tests -v
python tests/gui_smoke.py
```

## License

No license has been granted yet; public visibility by itself does not grant reuse or distribution rights.
