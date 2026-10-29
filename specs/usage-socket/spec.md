# Usage socket: read provider priority and usage from the box only — Spec

## Requirements

1. **Same usage view.** When a socket path is configured, the console process serves `GET /usage` on that Unix socket with the same body the console's `GET /api/usage` returns at that moment: per-provider usage windows, the gate verdict and the priority mode.
2. **Read-only and narrow.** The socket serves only `GET /usage`. Every other path returns 404 and every other method on `/usage` returns 405; no console route, write route, stream or static file is reachable through it.
3. **No Tailscale header.** A request on the socket needs no `Tailscale-User-Login` header, and a header it carries grants nothing more.
4. **File-system boundary.** The socket lives in a directory the console creates with mode 0750, owned by the console's user and the configured group; the socket itself has mode 0660 with that group. No other local user can connect, including during start-up.
5. **Safe start.** On start the console removes a leftover socket at the configured path, refuses to start when that path is anything other than a socket (a regular file, a directory or a symlink), and refuses to start when the configured group does not exist. Each refusal names the path or group.
6. **Off by default.** With no socket path configured, no socket is created and the console behaves as today.

## Scenarios

### Scenario: R1 — Socket returns the console's usage view

- **Given** the priority mode is `openai`, Anthropic's weekly window is at 62% used and OpenAI's at 20%, and the socket is configured at `/run/agent-ops-usage/usage.sock`
- **When** a client sends `GET /usage` on the socket and the operator loads `GET /api/usage` through Tailscale
- **Then** both responses report mode `openai`, first provider `openai`, and the same windows and gate verdict

### Scenario: R1 — A priority change shows on the next read

- **Given** the socket reports mode `auto`
- **When** the operator sets the mode to `anthropic` in the console and the client reads `GET /usage` again
- **Then** the socket reports mode `anthropic`

### Scenario: R2 — Writes and other console routes are not served

- **Given** the socket is running
- **When** a client sends `POST /api/priority` with `{"mode": "openai"}`, `POST /usage`, and `GET /api/board` on the socket
- **Then** the responses are 404, 405 and 404, and the stored priority mode is unchanged

### Scenario: R3 — No header needed, a forged header grants nothing

- **Given** the socket is running
- **When** a client sends `GET /usage` with no `Tailscale-User-Login` header, and `GET /api/health` with `Tailscale-User-Login: operator@example.com`
- **Then** the first returns 200 with the usage view and the second returns 404

### Scenario: R4 — Only the configured group can connect

- **Given** the socket is configured with group `tracker`, and local users `tracker` (in that group) and `other` (not in it) exist
- **When** each user connects to the socket
- **Then** `tracker` reads the usage view and `other` is refused by the operating system

### Scenario: R4 — Permissions are in place before the socket accepts connections

- **Given** a process umask of 000
- **When** the console starts and creates the socket
- **Then** the socket directory is never more permissive than 0750 and the socket never more permissive than 0660 while it exists

### Scenario: R5 — Leftover socket is replaced

- **Given** a socket file left at the configured path by a previous run that was killed
- **When** the console starts
- **Then** it starts, and the socket at that path answers `GET /usage`

### Scenario: R5 — Non-socket path is not deleted

- **Given** a regular file containing `keep me` at the configured socket path
- **When** the console starts
- **Then** it exits non-zero with a message naming the path, and the file still contains `keep me`

### Scenario: R5 — Symlink at the socket path is refused

- **Given** the configured socket path is a symlink to another file
- **When** the console starts
- **Then** it exits non-zero naming the path, and neither the link nor its target is changed

### Scenario: R5 — Unknown group is refused

- **Given** the configured group `trakcer` does not exist
- **When** the console starts
- **Then** it exits non-zero with a message naming `trakcer`, and no socket is created

### Scenario: R6 — No configuration, no socket

- **Given** no socket path is configured
- **When** the console starts
- **Then** no socket file is created and `GET /api/usage` over TCP behaves as before

## Out of scope

Write routes on the socket; any console data other than the usage view; changes to the TCP listener, its Tailscale boundary, or the `GET /api/usage` body; new usage reading or caching; authentication beyond file-system permissions; the loopback header-forgery fix.
