const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("electronAPI", {
  // Chạy 1 lần, trả về { success, output } sau khi tiến trình python kết thúc.
  runPython: (script, args = []) =>
    ipcRenderer.invoke("run-python", { script, args }),

  // Stream realtime: gọi onLine(line) cho mỗi dòng JSON in ra từ python,
  // onError(err) nếu có lỗi. Trả về { stop() } để dừng tiến trình.
  runPythonStream: (script, args = [], onLine, onError) => {
    const id =
      (globalThis.crypto && globalThis.crypto.randomUUID
        ? globalThis.crypto.randomUUID()
        : `${Date.now()}-${Math.random().toString(16).slice(2)}`);

    const lineChannel = `py-stream-line:${id}`;
    const errorChannel = `py-stream-error:${id}`;

    const lineListener = (_e, line) => onLine && onLine(line);
    const errorListener = (_e, err) => onError && onError(err);

    ipcRenderer.on(lineChannel, lineListener);
    ipcRenderer.on(errorChannel, errorListener);
    ipcRenderer.send("py-stream-start", { id, script, args });

    return {
      stop: () => {
        ipcRenderer.send("py-stream-stop", { id });
        ipcRenderer.removeListener(lineChannel, lineListener);
        ipcRenderer.removeListener(errorChannel, errorListener);
      },
    };
  },
});
