import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from app.services.log_redaction import redact


@contextmanager
def locked(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_name(f".{path.name}.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def append(path: Path, record: dict) -> None:
    line = json.dumps(redact(record), ensure_ascii=False, default=str) + "\n"
    with locked(path):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as stream:
            stream.write(line)


def purge(path: Path, cutoff: datetime) -> None:
    if not path.parent.is_dir():
        return
    with locked(path):
        # 같은 잠금 아래에서는 현재 쓰는 사본이 없다. 잠금 파일은 이름이 일치하지 않는다.
        for orphan in path.parent.glob(f".{path.name}." + "[a-z0-9_]" * 8):
            orphan.unlink(missing_ok=True)
        if not path.exists():
            return
        temporary = None
        try:
            with (
                path.open(encoding="utf-8", errors="replace") as source,
                tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    dir=path.parent,
                    prefix=f".{path.name}.",
                    delete=False,
                ) as target,
            ):
                temporary = Path(target.name)
                for line in source:
                    try:
                        row = json.loads(line)
                        at = datetime.fromisoformat(row["at"]).astimezone(cutoff.tzinfo)
                    except (ValueError, TypeError, KeyError, OverflowError):
                        continue
                    if at >= cutoff:
                        target.write(json.dumps(redact(row), ensure_ascii=False) + "\n")
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
