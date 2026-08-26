import pytest


@pytest.fixture(autouse=True)
def isolate_runtime_data(monkeypatch, tmp_path):
    """Keep every test away from the user's real config/database directory."""
    data_dir = tmp_path / "deskguardian-data"
    monkeypatch.setenv("DESKGUARDIAN_DATA_DIR", str(data_dir))
    return data_dir
