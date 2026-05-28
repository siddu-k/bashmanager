const { app, BrowserWindow, dialog } = require('electron');
const { spawn } = require('child_process');
const path = require('path');
const fs = require('fs');
const http = require('http');
const net = require('net');

let mainWindow;
let flaskProcess;
let activePort;
let serverReady = false;
let startupInterval = null;
let startupTimeout = null;

const HOST = '127.0.0.1';
const DEFAULT_PORT = 5000;
const MAX_SCAN_PORT = 5100;
const POLL_MS = 200;
const STARTUP_TIMEOUT_MS = 60_000;

// We must preserve the python path to our bundled app or local python
function resolvePythonCmd() {
    const venv = process.env.VIRTUAL_ENV;
    if (venv) {
        const venvPython = process.platform === 'win32'
            ? path.join(venv, 'Scripts', 'python.exe')
            : path.join(venv, 'bin', 'python');
        if (fs.existsSync(venvPython)) {
            return venvPython;
        }
    }
    return process.platform === 'win32' ? 'python' : 'python3';
}

const pythonCmd = resolvePythonCmd();

function parseDevShellPort(raw) {
    if (raw === undefined || raw === '') {
        return null;
    }
    const port = Number(raw);
    if (!Number.isInteger(port) || port < 1 || port > 65535) {
        throw new Error(
            `Invalid DEVSHELL_PORT: ${JSON.stringify(raw)} (must be integer 1-65535)`
        );
    }
    return port;
}

function isPortFree(port, host) {
    return new Promise((resolve, reject) => {
        const server = net.createServer();
        server.once('error', (err) => {
            if (err.code === 'EADDRINUSE') {
                resolve(false);
            } else {
                reject(err);
            }
        });
        server.once('listening', () => {
            server.close(() => resolve(true));
        });
        server.listen(port, host);
    });
}

async function resolvePort() {
    const override = parseDevShellPort(process.env.DEVSHELL_PORT);
    if (override !== null) {
        if (!(await isPortFree(override, HOST))) {
            throw new Error(
                `DEVSHELL_PORT ${override} is already in use on ${HOST}`
            );
        }
        return override;
    }
    for (let p = DEFAULT_PORT; p <= MAX_SCAN_PORT; p++) {
        if (await isPortFree(p, HOST)) {
            return p;
        }
    }
    throw new Error(
        `No free port on ${HOST} in range ${DEFAULT_PORT}-${MAX_SCAN_PORT}`
    );
}

function clearStartupTimers() {
    if (startupInterval) {
        clearInterval(startupInterval);
        startupInterval = null;
    }
    if (startupTimeout) {
        clearTimeout(startupTimeout);
        startupTimeout = null;
    }
}

function showStartupError(title, message) {
    clearStartupTimers();
    dialog.showErrorBox(title, message);
    if (mainWindow) {
        const body = `${message}\n\nIf port 5000 is in use (e.g. macOS AirPlay Receiver), unset DEVSHELL_PORT and restart, or set DEVSHELL_PORT to a free port.`;
        mainWindow.loadURL(
            `data:text/html,${encodeURIComponent(
                `<html><body style="font-family:sans-serif;padding:2em"><h1>${title}</h1><pre>${body}</pre></body></html>`
            )}`
        );
    }
}

function onServerReady(url) {
    if (serverReady) {
        return;
    }
    serverReady = true;
    clearStartupTimers();
    console.log('Server is ready. Loading UI...');
    mainWindow.loadURL(url);
}

function loadWhenReady(url) {
    clearStartupTimers();
    serverReady = false;

    const checkServer = () => {
        http.get(url, (res) => {
            if (res.statusCode && res.statusCode < 500) {
                onServerReady(url);
            }
            res.resume();
        }).on('error', () => {});
    };

    startupInterval = setInterval(checkServer, POLL_MS);
    checkServer();

    startupTimeout = setTimeout(() => {
        if (!serverReady) {
            showStartupError(
                'DevShell failed to start',
                'The backend server did not respond in time. Another process may be using the port, or Flask failed to start.'
            );
            app.quit();
        }
    }, STARTUP_TIMEOUT_MS);
}

function createWindow(port) {
    const baseUrl = `http://${HOST}:${port}`;

    mainWindow = new BrowserWindow({
        width: 1200,
        height: 800,
        minWidth: 900,
        minHeight: 600,
        title: "DevShell",
        autoHideMenuBar: true,
        webPreferences: {
            nodeIntegration: false,
            contextIsolation: true
        }
    });

    mainWindow.webContents.setWindowOpenHandler(({ url }) => {
        if (url.startsWith(baseUrl) || !url.startsWith('http')) {
            return { action: 'allow' };
        }
        require('electron').shell.openExternal(url);
        return { action: 'deny' };
    });

    loadWhenReady(baseUrl);

    mainWindow.on('closed', function () {
        mainWindow = null;
    });
}

function startFlaskServer(port) {
    console.log(`Starting Python server on ${HOST}:${port} (using ${pythonCmd})`);

    flaskProcess = spawn(pythonCmd, ['app.py'], {
        cwd: __dirname,
        env: {
            ...process.env,
            DEVSHELL_PORT: String(port),
            DEV_SHELL_DATA_DIR: app.getPath('userData'),
        }
    });

    flaskProcess.stdout.on('data', (data) => {
        console.log(`Flask: ${data}`);
    });

    flaskProcess.stderr.on('data', (data) => {
        console.error(`Flask Err: ${data}`);
    });

    flaskProcess.on('close', (code) => {
        console.log(`Flask process exited with code ${code}`);
        if (!serverReady && code !== 0) {
            showStartupError(
                'DevShell failed to start',
                `The backend server exited unexpectedly (code ${code}). If Flask fails to bind even after port resolution, this error is shown instead of polling forever.`
            );
            app.quit();
        }
    });
}

app.whenReady().then(async () => {
    try {
        const port = await resolvePort();
        activePort = port;
        startFlaskServer(port);
        createWindow(port);
    } catch (err) {
        showStartupError('DevShell failed to start', err.message);
        app.quit();
    }
});

app.on('window-all-closed', function () {
    if (process.platform !== 'darwin') {
        app.quit();
    }
});

app.on('activate', function () {
    if (mainWindow === null && activePort) {
        createWindow(activePort);
    }
});

app.on('will-quit', () => {
    clearStartupTimers();
    if (flaskProcess) {
        flaskProcess.kill();
    }
});
