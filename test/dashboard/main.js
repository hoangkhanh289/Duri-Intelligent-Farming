const { app, BrowserWindow, ipcMain } = require("electron");
const path = require("path");
const { spawn, execFile } = require("child_process");
const fs = require("fs");

// ============================================================
// Chọn lệnh python: ưu tiên "python3" (Linux/macOS), fallback "python"
// (thường là Windows). Có thể ép cứng qua biến môi trường PYTHON_BIN.
// ============================================================
const PYTHON_CANDIDATES = process.env.PYTHON_BIN
  ? [process.env.PYTHON_BIN]
  : process.platform === "win32"
  ? ["python", "python3"]
  : ["python3", "python"];

let resolvedPythonBin = null;

function resolvePythonBin() {
  return new Promise((resolve) => {
    if (resolvedPythonBin) return resolve(resolvedPythonBin);
    const tryNext = (idx) => {
      if (idx >= PYTHON_CANDIDATES.length) {
        resolvedPythonBin = PYTHON_CANDIDATES[0]; // vẫn thử cái đầu, để lỗi rõ ràng hiện ra sau
        return resolve(resolvedPythonBin);
      }
      const bin = PYTHON_CANDIDATES[idx];
      execFile(bin, ["--version"], (err) => {
        if (!err) {
          resolvedPythonBin = bin;
          resolve(bin);
        } else {
          tryNext(idx + 1);
        }
      });
    };
    tryNext(0);
  });
}

// Resolve script path (vd "firebase/dashboard/firebase_runner.py") tương
// đối theo thư mục gốc app, chặn path traversal ra ngoài project.
function resolveScriptPath(script) {
  const root = path.resolve(__dirname);
  const full = path.resolve(root, script);
  if (!full.startsWith(root)) {
    throw new Error("Đường dẫn script không hợp lệ: " + script);
  }
  if (!fs.existsSync(full)) {
    throw new Error("Không tìm thấy script: " + full);
  }
  return full;
}

// ============================================================
// IPC: chạy 1 lần, trả toàn bộ stdout khi tiến trình kết thúc
// (dùng cho get-all, get-node, set-node, update-node, system-info, ...)
// ============================================================
ipcMain.handle("run-python", async (_evt, { script, args }) => {
  try {
    const scriptPath = resolveScriptPath(script);
    const pythonBin = await resolvePythonBin();
    return await new Promise((resolve) => {
      const child = spawn(pythonBin, [scriptPath, ...(args || [])], {
        cwd: path.dirname(scriptPath),
      });
      let stdout = "";
      let stderr = "";
      child.stdout.on("data", (chunk) => (stdout += chunk.toString()));
      child.stderr.on("data", (chunk) => (stderr += chunk.toString()));
      child.on("error", (err) => {
        resolve({ success: false, output: `Không chạy được python (${pythonBin}): ${err.message}` });
      });
      child.on("close", (code) => {
        if (code === 0) {
          resolve({ success: true, output: stdout });
        } else {
          resolve({ success: false, output: stderr || stdout || `python thoát với mã lỗi ${code}` });
        }
      });
    });
  } catch (e) {
    return { success: false, output: String(e.message || e) };
  }
});

// ============================================================
// IPC: stream realtime — dùng cho action "stream" (db.reference("/").listen()
// chạy vô hạn). Mỗi dòng JSON stdout được đẩy về renderer qua kênh
// py-stream-line:<id>. Renderer gọi py-stream-stop để kill tiến trình.
// ============================================================
const streams = new Map();

ipcMain.on("py-stream-start", async (evt, { id, script, args }) => {
  try {
    const scriptPath = resolveScriptPath(script);
    const pythonBin = await resolvePythonBin();
    const child = spawn(pythonBin, [scriptPath, ...(args || [])], {
      cwd: path.dirname(scriptPath),
    });
    streams.set(id, child);

    let buf = "";
    child.stdout.on("data", (chunk) => {
      buf += chunk.toString();
      let idx;
      while ((idx = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, idx);
        buf = buf.slice(idx + 1);
        if (line.trim() && !evt.sender.isDestroyed()) {
          evt.sender.send(`py-stream-line:${id}`, line);
        }
      }
    });
    child.stderr.on("data", (chunk) => {
      if (!evt.sender.isDestroyed()) {
        evt.sender.send(`py-stream-error:${id}`, chunk.toString());
      }
    });
    child.on("close", (code) => {
      streams.delete(id);
      if (!evt.sender.isDestroyed() && code !== 0) {
        evt.sender.send(`py-stream-error:${id}`, `Tiến trình stream đã dừng (mã ${code})`);
      }
    });
    child.on("error", (err) => {
      streams.delete(id);
      if (!evt.sender.isDestroyed()) {
        evt.sender.send(`py-stream-error:${id}`, `Không chạy được python: ${err.message}`);
      }
    });
  } catch (e) {
    evt.sender.send(`py-stream-error:${id}`, String(e.message || e));
  }
});

ipcMain.on("py-stream-stop", (_evt, { id }) => {
  const child = streams.get(id);
  if (child) {
    child.kill();
    streams.delete(id);
  }
});

// ============================================================
// Cửa sổ chính
// ============================================================
function createWindow() {
  const win = new BrowserWindow({
    width: 1280,
    height: 860,
    minWidth: 900,
    minHeight: 600,
    title: "sam-team · Trạm giám sát Firebase",
    backgroundColor: "#0b0f14",
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
    },
  });

  win.setMenuBarVisibility(false);
  win.loadFile(path.join(__dirname, "renderer", "firebase_ui.html"));

  // Mở DevTools nếu chạy với biến môi trường DEBUG=1
  if (process.env.DEBUG === "1") {
    win.webContents.openDevTools();
  }
}

app.whenReady().then(() => {
  createWindow();

  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on("window-all-closed", () => {
  // Kill mọi tiến trình python stream còn sống trước khi thoát
  for (const child of streams.values()) child.kill();
  streams.clear();
  if (process.platform !== "darwin") app.quit();
});
