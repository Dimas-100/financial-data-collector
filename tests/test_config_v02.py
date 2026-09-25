from pathlib import Path

from financial_data_collector import config as C


def test_v02_config_defaults(tmp_path: Path):
    C.init_project(tmp_path)
    cfg = C.load_config(tmp_path)
    assert cfg.investing_dir == (tmp_path / ".." / "investing").resolve()
    assert cfg.export_cockpit is False
    assert cfg.export_dir == (tmp_path / ".." / "investing" / "data").resolve()


def test_v02_config_overrides(tmp_path: Path):
    (tmp_path / "config.toml").write_text(
        '[sources.investing]\ncontent_dir = "content"\n[export.cockpit]\nenabled = true\ndir = "out"\n'
    )
    cfg = C.load_config(tmp_path)
    assert cfg.investing_dir == (tmp_path / "content").resolve()
    assert cfg.export_cockpit is True and cfg.export_dir == (tmp_path / "out").resolve()


def test_v02_config_sections_absent(tmp_path: Path):
    (tmp_path / "config.toml").write_text("[paths]\n")
    cfg = C.load_config(tmp_path)
    assert cfg.investing_dir is not None and cfg.export_cockpit is False
