# DevShell

DevShell is a lightweight, zero-database local script manager and execution environment. Built for developers and DevOps engineers, it provides a unified graphical interface to manage, edit, secure, and execute shell scripts across local directory structures.

## Core Features

- **Zero-DB Architecture:** Operates entirely on the local file system. Scripts are read directly from a structured `scripts/` directory. State and security parameters are maintained in local JSON files (`locks.json`, `favorites.json`).
- **Real-Time Execution Engine:** Utilizes Server-Sent Events (SSE) to stream subprocess outputs (stdout/stderr) immediately to the frontend.
- **Multi-Tab Terminal:** Supports multiple terminal tabs in the UI; each tab runs independently but shares the same backend process manager.
- **Persistent Execution History:** Stores timestamped command and script runs, failed executions, and per-run log files for later search and export.
- **Process Resource Monitoring:** Tracks CPU and memory usage of the script process via psutil during execution. Child processes spawned by the script are not individually tracked.
- **In-Memory Search Highlights:** Non-destructive DOM traversal algorithms allow live keyword grepping within active terminal streams.
- **Local Password Locking:** Scripts can be securely locked behind a SHA-256 local hash (single-round, no salt) to prevent unauthorized reading or execution.
- **GitHub Integration:** Pull scripts directly from public raw GitHub URLs via the interface.
- **Execution History & Logs:** All script and command runs are recorded to logs/history.jsonl with exit codes, duration, and output excerpts. Logs are auto-pruned after 30 days or 250 files.
- **Git PR Push (Experimental):** Scripts can be pushed to a branch and a GitHub pull request link generated via the /api/git/pr endpoint. This feature is experimental and requires git to be configured in the project directory.

## System Architecture

The project follows a hybrid architecture easily packaged into standalone desktop executables.

- **Frontend:** Vanilla JavaScript, HTML5, and CSS3.
- **Backend API:** Python (`Flask`). Handles file system I/O, cryptographic locking, OS subprocess management, and resource monitoring (`psutil`).
- **Desktop Wrapper:** Electron. The application supports running as a native desktop application through Electron and `electron-builder`.

## Security Specifications

To protect locked scripts from unauthorized access, DevShell uses a secure password storage and verification mechanism:

- **Algorithm**: PBKDF2-HMAC-SHA256
- **Salt**: 16-byte cryptographically secure random salt generated via `secrets.token_bytes()`
- **Iterations**: 100,000 rounds of key stretching to prevent brute-force attacks
- **Verification**: Constant-time comparison using `hmac.compare_digest()` to eliminate timing attack vectors
- **Backward Compatibility**: Existing users with legacy unsalted SHA-256 hashes are automatically migrated to the secure PBKDF2 format after their first successful unlock.

## Prerequisites

- Python 3.8+
- Node.js 18+ and npm (For the Electron wrapper)
- A shell available in system PATH — bash (Linux/macOS), Git Bash, or WSL on Windows. On Windows, cmd.exe is used as fallback if no bash is found.

## Installation

### 1. Clone the Repository

```bash
git clone <repository_url>
cd bashmanager
```

### 2. Backend Setup

It is highly recommended to use a virtual environment for the Python backend dependencies.

```bash
pip install -r requirements.txt
```

### 3. Frontend / Desktop Setup

Install Node dependencies required for the Electron frontend integration.

```bash
npm install
```

## Running the Application

### Desktop Mode (Electron)
To launch the application as a standalone desktop interface (Highly Recommended):

```bash
npm start
```

### Web Mode (Backend Only)
Alternatively, start the Python Flask server to host the web interface locally on your browser.

```bash
python app.py
```

By default the server listens on **port 5000** at `http://127.0.0.1:5000`. Set `DEVSHELL_PORT` to use another port (empty `DEVSHELL_PORT` is treated as unset):

```bash
DEVSHELL_PORT=5001 python app.py
```

### Port conflicts (macOS and Electron)

On macOS, **AirPlay Receiver** often uses port 5000. In **Desktop Mode** (`npm start`), DevShell automatically picks the next free port on `127.0.0.1` (5000–5100) and passes it to Flask via `DEVSHELL_PORT`. If the backend cannot start or bind, you get an error dialog instead of a blank window.

Override manually (both modes):

```bash
export DEVSHELL_PORT=8080
npm start
```

Invalid values (e.g. `abc`, `99999`, `-1`) show a startup error in Electron or exit with a clear message when running `python app.py` directly.

**Manual checks:** occupy port 5000 (`python -m http.server 5000`), then `npm start` — UI should load on 5001+; `DEVSHELL_PORT=5000 npm start` while 5000 is busy — startup error, not infinite loading.

### Building Distributables (Linux)
To compile the Electron application into a standalone `.AppImage` distribution:

```bash
npm run build
```

## Project Scope & Structure

- `app.py`: Main Flask application, API routing, subprocess handling, and SSE response generation.
- `logs/`: Persistent execution history, failed-command records, and per-run `.log` files.
- `scripts/`: Root directory for user scripts. Any subdirectory is automatically treated as a script category.
- `ui/`: Contains all frontend assets (`index.html`, `style.css`, `app.js`).
- `main.js`: Electron initialization and window management.

## Contributing Guidelines

Contributions from the open-source community are welcome. Please adhere to the following technical guidelines when submitting pull requests:

1. **Keep Dependencies Minimal:** The core philosophy of DevShell is lightweight execution. Avoid introducing complex external npm packages, compilers, or heavy Python frameworks.
2. **Vanilla Frontend System:** Core UI features should be implemented in Vanilla JS/CSS without reliance on build steps (e.g., React, Vue, Tailwind).
3. **Cross-Platform Compatibility:** Ensure OS subprocess invocations account for both Windows architectures (via Git Bash/WSL/cmd) and native POSIX Linux systems. Avoid hardcoding OS-specific file paths.
4. **Code Formatting:** Adhere strictly to PEP-8 for Python components and standard modern JS styling.

### Submission Process

1. Fork the project.
2. Create your designated feature branch (`git checkout -b feature/NewFeature`).
3. Commit your changes logically (`git commit -m 'Add NewFeature'`).
4. Push to the branch (`git push origin feature/NewFeature`).
5. Open a Pull Request detailing the technical changes and architectural impact.

## License

This project is licensed under the MIT License.
