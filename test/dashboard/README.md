# sam-team · Trạm giám sát Firebase (Electron)

App Electron bọc quanh 3 file gốc (giữ nguyên, không sửa):

```
renderer/firebase_ui.html        ← giao diện (UI)
renderer/firebase_style.css      ← CSS
firebase/dashboard/firebase_runner.py  ← cầu nối Firebase (chạy qua python)
```

Phần Electron mới thêm vào:

```
package.json
main.js       ← main process: mở cửa sổ + IPC "run-python" / "py-stream-*"
preload.js    ← expose window.electronAPI.runPython / runPythonStream cho UI
```

## 1. Cài đặt

### a) Node.js / Electron
Cần Node.js >= 18. Trong thư mục project:

```bash
npm install
```

### b) Python
Cần Python 3 (lệnh `python3`, hoặc `python` trên Windows) có sẵn trong PATH.

```bash
cd firebase/dashboard
pip install -r requirements.txt
```

`joblib` + `scikit-learn` chỉ cần nếu bạn dùng model đã train (`predict-models`,
phần model trong `analyze-labels`). Không cài vẫn chạy được `get-all`,
`set-node`, `update-node`, `stream`, ...

### c) Credential Firebase
Đặt file service account của bạn tại:

```
firebase/dashboard/credentials/serviceAccountKey.json
```

(thư mục `credentials/` đã được tạo sẵn, chỉ cần copy file JSON key vào).

## 2. Chạy app

```bash
npm start
```

Muốn mở DevTools để debug:

```bash
DEBUG=1 npm start
```

## 3. Cách hoạt động

- `firebase_ui.html` gọi `window.electronAPI.runPython("firebase/dashboard/firebase_runner.py", ["--action", "get-all"])`.
- `preload.js` chuyển lời gọi này qua IPC (`ipcRenderer.invoke("run-python", ...)`).
- `main.js` nhận IPC, `spawn` tiến trình `python3 firebase/dashboard/firebase_runner.py --action get-all`,
  gom toàn bộ stdout, trả về `{ success, output }` cho renderer — đúng định dạng
  mà `firebase_ui.html` đã parse (`r.success`, `r.output`).
- Với action `stream` (realtime thật, không polling), UI gọi
  `window.electronAPI.runPythonStream(...)`. `main.js` giữ tiến trình python
  sống, mỗi dòng JSON in ra từ `db.reference("/").listen()` được đẩy ngay
  về renderer qua kênh `py-stream-line:<id>`. Nếu không dùng được stream,
  UI tự rơi về polling mỗi 3 giây (`get-all`).
- Không cần cấu hình gì thêm ở CSP: `firebase_ui.html` không gọi `fetch()`
  hay Firebase SDK trực tiếp trong renderer, toàn bộ network đi qua tiến
  trình Python ở main process.

## 4. Đóng gói (tuỳ chọn)

Muốn build file cài đặt (.exe/.dmg/.AppImage), có thể thêm `electron-builder`:

```bash
npm install --save-dev electron-builder
```

và cấu hình thêm trong `package.json` (`build` field + script `dist`).
Lưu ý: khi đóng gói, cần đảm bảo Python + các thư viện (`firebase-admin`, ...)
cũng có sẵn trên máy người dùng cuối, hoặc đóng gói kèm theo môi trường Python.
