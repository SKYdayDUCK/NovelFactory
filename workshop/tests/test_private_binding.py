"""Read the same protected service binding on a native POSIX or Windows host."""
import json
import os

import pytest

from agent_workshop.auth import Denied, private_binding
from agent_workshop.private_files import prepare_private_directory, private_temporary_file


def test_native_binding_reader(tmp_path):
    private = tmp_path / "service"
    prepare_private_directory(private)
    fd, filename = private_temporary_file(private)
    with os.fdopen(fd, "wb") as f:
        f.write(b'{"database_url":"test","token":"fixture"}')
    assert private_binding(filename) == {"database_url": "test", "token": "fixture"}
    with open(filename, "w", encoding="utf-8") as f:
        json.dump({"token": "fixture"}, f)
    with pytest.raises(Denied, match="INVALID_BINDING"):
        private_binding(filename)
