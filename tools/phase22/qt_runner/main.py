"""PyRun's isolated Qt/Python 3.14 entry point.

This file is bundled into the optional Qt runtime. The main PyRun process
writes one private, revision-bound session document under filesDir; this
process revalidates that document and the script bytes before execution.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path
import sys
import threading
import time
import traceback


MAX_SOURCE_BYTES = 2 * 1024 * 1024
MAX_OUTPUT_BYTES = 256 * 1024
SESSION_SCHEMA = 1


def _inside(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def _read_session(private_root: Path) -> tuple[dict[str, object], Path, Path]:
    session_path = private_root / "pyrun" / "qt" / "session.json"
    raw = session_path.read_bytes()
    if len(raw) > 64 * 1024:
        raise ValueError("qt_session_too_large")
    session = json.loads(raw.decode("utf-8"))
    if not isinstance(session, dict) or set(session) != {
        "schemaVersion",
        "runId",
        "relativePath",
        "expectedRevision",
    }:
        raise ValueError("invalid_qt_session")
    if session["schemaVersion"] != SESSION_SCHEMA:
        raise ValueError("unsupported_qt_session")
    run_id = session["runId"]
    relative = session["relativePath"]
    revision = session["expectedRevision"]
    if (
        not isinstance(run_id, str)
        or not run_id
        or len(run_id) > 128
        or not isinstance(relative, str)
        or not relative
        or "\\" in relative
        or Path(relative).is_absolute()
        or any(part in ("", ".", "..") for part in Path(relative).parts)
        or not isinstance(revision, str)
        or len(revision) != 64
        or any(character not in "0123456789abcdef" for character in revision)
    ):
        raise ValueError("invalid_qt_session_fields")
    workspace = (private_root / "pyrun" / "workspace").resolve()
    source = (workspace / relative).resolve()
    if not _inside(workspace, source) or source.suffix.lower() != ".py" or not source.is_file():
        raise ValueError("invalid_qt_source")
    return session, source, session_path.parent


class _BoundedTextWriter:
    def __init__(self, path: Path, limit: int) -> None:
        self._file = path.open("w", encoding="utf-8", newline="")
        self._limit = limit
        self._written = 0
        self.truncated = False

    def write(self, text: object) -> int:
        value = str(text)
        encoded = value.encode("utf-8", errors="replace")
        remaining = max(0, self._limit - self._written)
        accepted = encoded[:remaining]
        if accepted:
            decoded = accepted.decode("utf-8", errors="ignore")
            self._file.write(decoded)
            self._file.flush()
            self._written += len(decoded.encode("utf-8"))
        if len(encoded) > remaining:
            self.truncated = True
        return len(value)

    def flush(self) -> None:
        self._file.flush()

    def close(self) -> None:
        self._file.close()


def _atomic_json(path: Path, value: dict[str, object]) -> None:
    temporary = path.with_suffix(path.suffix + ".new")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _atomic_text(path: Path, value: str) -> None:
    temporary = path.with_suffix(path.suffix + ".new")
    temporary.write_text(value, encoding="ascii")
    os.replace(temporary, path)


def _start_stop_monitor(result_dir: Path, run_id: str) -> None:
    """Allow Stop/Back to close the Qt event loop, with a bounded hard exit."""
    stop_path = result_dir / "stop"

    def monitor() -> None:
        while True:
            try:
                requested = stop_path.read_text(encoding="utf-8").strip() == run_id
            except OSError:
                requested = False
            if requested:
                break
            time.sleep(0.1)
        try:
            from PySide6.QtCore import QCoreApplication, QMetaObject, Qt

            application = QCoreApplication.instance()
            if application is not None:
                QMetaObject.invokeMethod(
                    application,
                    "quit",
                    Qt.ConnectionType.QueuedConnection,
                )
        except Exception:
            pass
        # A user loop which never yields to Qt must not keep the private process
        # alive forever after an explicit Stop request.
        time.sleep(5.0)
        os._exit(143)

    threading.Thread(target=monitor, name="pyrun-qt-stop", daemon=True).start()


def _configure_qt(project: Path) -> None:
    os.environ["QT_API"] = "PySide6"
    os.environ["DISPLAY"] = ":0"
    os.environ["MPLBACKEND"] = "QtAgg"
    os.environ.setdefault("QT_QPA_PLATFORM", "android")
    if str(project) not in sys.path:
        sys.path.insert(0, str(project))
    os.chdir(project)


def _force_qtagg_backend(source_bytes: bytes) -> None:
    """Select QtAgg before user imports pyplot, even if the image default is Agg."""
    if b"matplotlib" not in source_bytes and b"pylab" not in source_bytes:
        return
    import matplotlib

    try:
        matplotlib.use("QtAgg", force=True)
    except Exception as error:
        raise RuntimeError("PyRun could not activate Matplotlib's QtAgg backend") from error
    if str(matplotlib.get_backend()).casefold() != "qtagg":
        raise RuntimeError("PyRun failed to select Matplotlib's QtAgg backend")


def main() -> int:
    private = os.environ.get("ANDROID_PRIVATE")
    if not private:
        raise RuntimeError("ANDROID_PRIVATE is not set")
    private_root = Path(private).resolve()
    session: dict[str, object] | None = None
    result_dir = private_root / "pyrun" / "qt"
    stdout_writer: _BoundedTextWriter | None = None
    stderr_writer: _BoundedTextWriter | None = None
    succeeded = False
    failure = "startup_failed"
    try:
        session, source, result_dir = _read_session(private_root)
        _atomic_text(result_dir / "pid", str(os.getpid()))
        _start_stop_monitor(result_dir, str(session["runId"]))
        with source.open("rb") as source_file:
            source_bytes = source_file.read(MAX_SOURCE_BYTES + 1)
        if len(source_bytes) > MAX_SOURCE_BYTES:
            raise ValueError("qt_source_too_large")
        if hashlib.sha256(source_bytes).hexdigest() != session["expectedRevision"]:
            failure = "source_changed"
            raise ValueError(failure)
        stdout_writer = _BoundedTextWriter(result_dir / "stdout.txt", MAX_OUTPUT_BYTES)
        stderr_writer = _BoundedTextWriter(result_dir / "stderr.txt", MAX_OUTPUT_BYTES)
        failure = "script_error"
        code = compile(source_bytes, str(source), "exec")
        _configure_qt(source.parent)
        _force_qtagg_backend(source_bytes)
        previous_argv = sys.argv
        sys.argv = [str(source)]
        with contextlib.redirect_stdout(stdout_writer), contextlib.redirect_stderr(stderr_writer):
            try:
                exec(code, {
                    "__name__": "__main__",
                    "__file__": str(source),
                    "__package__": None,
                    "__builtins__": __builtins__,
                })
            finally:
                sys.argv = previous_argv
        succeeded = True
        failure = ""
        return 0
    except SystemExit as exit_signal:
        code = exit_signal.code
        succeeded = code is None or code == 0
        failure = "" if succeeded else "script_error"
        return 0 if succeeded else int(code) if isinstance(code, int) else 1
    except BaseException:
        if stderr_writer is not None:
            traceback.print_exc(file=stderr_writer)
        return 1
    finally:
        if stdout_writer is not None:
            stdout_writer.close()
        if stderr_writer is not None:
            stderr_writer.close()
        result_dir.mkdir(parents=True, exist_ok=True)
        _atomic_json(
            result_dir / "result.json",
            {
                "schemaVersion": SESSION_SCHEMA,
                "runId": session.get("runId", "") if session else "",
                "succeeded": succeeded,
                "failure": failure,
                "outputTruncated": bool(
                    (stdout_writer and stdout_writer.truncated)
                    or (stderr_writer and stderr_writer.truncated)
                ),
            },
        )


if __name__ == "__main__":
    raise SystemExit(main())
