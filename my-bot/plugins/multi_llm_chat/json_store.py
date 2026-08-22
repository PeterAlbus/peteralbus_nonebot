import os
import tempfile
from pathlib import Path
from typing import Callable, Type, TypeVar

from pydantic import BaseModel

ModelT = TypeVar("ModelT", bound=BaseModel)


def load_json_model(
    path: Path,
    model_type: Type[ModelT],
    default_factory: Callable[[], ModelT],
) -> ModelT:
    if not path.is_file():
        return default_factory()
    return model_type.model_validate_json(path.read_text(encoding="utf-8"))


def atomic_write_json_model(path: Path, value: BaseModel) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = (value.model_dump_json(indent=2) + "\n").encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as temporary_file:
            temporary_file.write(payload)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, path)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary_path.unlink(missing_ok=True)
        raise
