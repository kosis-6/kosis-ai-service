"""JSONL 기록: 비밀값을 가리고, 쓰기와 기간 정리가 겹쳐도 기록을 잃지 않는다.

서비스 실행 환경(Linux/macOS)의 파일 잠금을 쓴다. 파일을 교체해도 같은 잠금 파일을 사용하므로
여러 서버 프로세스의 추가 기록과 정리가 서로 덮어쓰지 않는다.
"""

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
    """기한 지난 행을 지우고 기존 비밀값도 가린다. 시각 불명·손상된 행은 남기지 않는다."""
    if not path.exists():
        return
    with locked(path):
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
