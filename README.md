# Viber Export Native

Read selected Viber Desktop chats and download photo originals on Windows without clicking through the app.

The collector uses the running Viber process and its existing Qt SQL connection. Photo downloads go through Viber's own `requestPayload` operation. It does not send chat messages, extract database keys, or use OCR. There is no cloud parser or external API account to configure.

This is an experimental, unofficial interface, not a replacement for Viber Desktop. Viber must be running and signed in.

## Compatibility

| Component | Tested version |
| --- | --- |
| Platform | Windows x64 |
| Python | 3.12 x64 |
| Viber Desktop | 29.1.0 |
| Qt | 6.11.2.0 |
| Frida | 17.24.3 |

The worker checks the Qt version before attaching native hooks. Other Qt versions are rejected. A Viber update can change private methods or database fields even if the Qt version stays the same.

## Install

Run these PowerShell commands from the repository folder:

```powershell
git clone https://github.com/GeoKrown/viber-export-native.git
cd viber-export-native
py -3.12 -m venv .venv
$viberPython = '.\.venv\Scripts\python.exe'
& $viberPython -m pip install .
```

Sign in to Viber Desktop normally using your phone, then leave it running. Use the same Windows account and session for Viber and the collector. Running the collector as administrator is not normally needed.

## Select chats

List the named chats available in the local desktop database:

```powershell
& $viberPython -m viber_native chats
```

Select chats by their exact names:

```powershell
& $viberPython -m viber_native configure --group 'Example group' --group 'Another group'
```

Configuration is written to `data/private/viber_config.json`. It contains the account fingerprint, selected chat IDs and names, and collection limits. The example under `examples/` uses dummy values; generate a real configuration rather than copying those values.

## Collect messages

```powershell
& $viberPython -m viber_native run --once
& $viberPython -m viber_native run
```

The first command performs one collection cycle. The second keeps collecting until interrupted with Ctrl+C. Messages are stored in `data/viber.sqlite`. Repeated cycles resume from the saved cursor and update a bounded tail to capture recent edits.

A cycle reads up to 100 new events and the latest 100 events in each selected chat by default. A single cycle may not cover the entire backlog. The continuous collector drains it over subsequent cycles.

An idle Viber connection may return `idle`; that does not advance the cursor. The incremental collector waits for an observed commit or rollback. A changed account, renamed chat, reused event ID, or invalid batch stops collection rather than advancing past uncertain data.

Custom paths go before the command:

```powershell
& $viberPython -m viber_native --config 'data/private/work.json' --db 'data/work.sqlite' run --once
```

## Export recent history

```powershell
& $viberPython -m viber_native history --days 14 --output 'data/private/history.json'
```

This exports the configured chats, including raw message bodies and available attachment paths. It reads a rolling UTC time window, with a limit of 1,000 messages per chat and 200 per page. `--days` accepts 1 to 90. If a group fails, the previous export is left unchanged; a partial result is not written as a complete export.

History snapshots can also run after an event handler returns on the SQL connection's owning thread. A transaction observed to be open blocks that read. The collector does not create its own transactions on Viber's connection.

## Download a photo

Find the photo's `event_id` in the history export. The observed message type for photos is `2`.

```powershell
& $viberPython -m viber_native download --chat-id 101 --event-id 12345
```

Several photos can be requested in one batch:

```powershell
& $viberPython -m viber_native download --chat-id 101 --event-id 12345 --event-id 12346
```

The chat must already be selected in the configuration. A batch accepts up to 20 unique event IDs. If your media folder differs from `Documents\ViberDownloads`, pass `--downloads 'D:\ViberDownloads'`.

The command checks the account, chat, message type, process ID, event token and timestamp. It then calls `Viber.Chat.requestPayload(eventID, true)` in Viber's GUI thread without showing the photo or changing the current chat. Existing valid originals are skipped.

A successful command acknowledgement is not treated as a completed download. The collector reads the message metadata again, checks that the original file exists in the allowed media folder, verifies the image, and records its dimensions and SHA-256. Thumbnails do not count as originals. Results are written to `data/private/download-<chat-id>.json`.

## Local storage

| Table | Contents |
| --- | --- |
| `viber_chats` | Selected chats and collection cursors |
| `viber_messages` | Current observed event content |
| `viber_message_revisions` | Observed changes to recent messages |
| `viber_runs` | Collection status and counts |

Configuration, logs, exports, databases and downloaded media may contain private information. The repository excludes runtime data by default. Do not upload your generated files when reporting a problem; use a redacted error code and version details.

## Limits

- Only history already synchronized to the desktop database is available. The tool does not fetch an entire account's server history.
- Photo downloading has been tested. Videos, voice messages and other file types are not supported by the download command.
- Media no longer available from Viber's servers may remain unavailable.
- Edits outside the configured recent tail and deletions are not reconciled automatically.
- A locked desktop, disconnected RDP session, Windows service deployment and restart recovery have not been validated.
- Native hooks run inside Viber. Compatibility checks reduce risk, but an incompatible build can still interrupt the app. Keep normal backups before experimenting.

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Command completed successfully |
| `1` | Configuration, compatibility, storage or verification error |
| `2` | A one-shot collection was idle/unavailable, or a photo remains unverified |

## Tests

```powershell
& $viberPython -m unittest discover -s tests -v
```

The tests use synthetic messages and temporary databases. They do not attach to Viber or read an account. The Windows CI workflow is configured for Python 3.12 and 3.13. Live compatibility has been checked separately on the versions listed above; the CI workflow does not certify new Viber releases.

## Related projects

- [viber-export](https://github.com/GhostingBit/viber-export): desktop database export and migration tools.
- [ViberDBViewer](https://github.com/mbv06/ViberDBViewer): an offline viewer for existing Viber databases.
- [viber-mcp](https://github.com/wawimundo/viber-mcp): macOS desktop integration using UI automation and OCR.

## License

MIT. Viber is a trademark of its respective owner. This project is independent of Viber and does not include Viber binaries.
