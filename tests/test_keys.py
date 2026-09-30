from pathlib import Path

from financial_data_collector.connections import keys as K


def test_parse_strips_and_requires():
    st = K.parse("snaptrade", {"client_id": " id \n", "consumer_key": "key ", "user_id": "", "user_secret": None})
    assert st == K.SnapTradeKeys("id", "key")
    assert st.values() == {"client_id": "id", "consumer_key": "key"} and st.secrets() == ["id", "key"]
    assert K.parse("snaptrade", {"client_id": "id"}) is None
    full = K.parse("snaptrade", {"client_id": "id", "consumer_key": "key", "user_id": "u", "user_secret": "s"})
    assert full.user_id == "u" and full.secrets() == ["id", "key", "u", "s"]
    sf = K.parse("simplefin", {"access_url": " https://u%40x:p%40ss@bridge.example.org/simplefin "})
    assert sf == K.SimpleFinKey("https://u%40x:p%40ss@bridge.example.org/simplefin")
    assert set(sf.secrets()) >= {sf.access_url, "u%40x", "p%40ss", "u@x", "p@ss"}
    assert K.parse("simplefin", {}) is None and K.parse("other", {"x": "y"}) is None


def test_env_file_reads_environment_over_file_and_writes_only_its_own_lines(tmp_path: Path):
    env = tmp_path / ".env"
    env.write_text("SEC_USER_AGENT=Sample Person s@example.com\nFDC_SIMPLEFIN_ACCESS_URL=https://a:b@h/x\n")
    f = K.EnvFile(env, environ={"FDC_SNAPTRADE_CLIENT_ID": "from-env"})
    assert f.read("simplefin") == {"access_url": "https://a:b@h/x"}
    assert f.read("snaptrade") == {"client_id": "from-env"}
    f.write("snaptrade", {"client_id": "id", "consumer_key": "key"})
    text = env.read_text()
    assert "SEC_USER_AGENT=Sample Person s@example.com" in text
    assert "FDC_SNAPTRADE_CLIENT_ID=id" in text and "FDC_SNAPTRADE_CONSUMER_KEY=key" in text
    f.write("snaptrade", {"client_id": "id2", "consumer_key": "key2"})
    assert env.read_text().count("FDC_SNAPTRADE_CLIENT_ID=") == 1 and "id2" in env.read_text()
    f.remove("snaptrade")
    assert "FDC_SNAPTRADE" not in env.read_text() and "SEC_USER_AGENT" in env.read_text()
    assert "FDC_SIMPLEFIN_ACCESS_URL" in env.read_text()


def test_key_home_saves_in_the_store_and_loads_back(tmp_path: Path):
    store = K.MemoryKeyStore()
    home = K.KeyHome(store, K.EnvFile(tmp_path / ".env", environ={}))
    ref = home.save("snaptrade", K.SnapTradeKeys("id", "key"))
    assert ref != K.ENV_REF and len(ref) == 32
    assert home.load("snaptrade", ref) == K.SnapTradeKeys("id", "key")
    assert not (tmp_path / ".env").exists()
    home.forget("snaptrade", ref)
    assert home.load("snaptrade", ref) is None and store.entries == {}


def test_two_warehouses_never_share_an_entry(tmp_path: Path):
    store = K.MemoryKeyStore()
    a = K.KeyHome(store, K.EnvFile(tmp_path / "a.env", environ={}))
    b = K.KeyHome(store, K.EnvFile(tmp_path / "b.env", environ={}))
    ra = a.save("simplefin", K.SimpleFinKey("https://a:a@h/a"))
    rb = b.save("simplefin", K.SimpleFinKey("https://b:b@h/b"))
    assert ra != rb and len(store.entries) == 2
    assert a.load("simplefin", ra).access_url.endswith("/a") and b.load("simplefin", rb).access_url.endswith("/b")


def test_without_a_key_store_the_key_goes_to_env(tmp_path: Path):
    env = K.EnvFile(tmp_path / ".env", environ={})
    home = K.KeyHome(K.MemoryKeyStore(broken=True), env)
    ref = home.save("simplefin", K.SimpleFinKey("https://u:p@h/x"))
    assert ref == K.ENV_REF
    assert "FDC_SIMPLEFIN_ACCESS_URL=https://u:p@h/x" in (tmp_path / ".env").read_text()
    assert home.load("simplefin", ref) == K.SimpleFinKey("https://u:p@h/x")
    home.forget("simplefin", ref)
    assert home.load("simplefin", ref) is None


def test_a_key_put_in_env_by_hand_is_found(tmp_path: Path):
    home = K.KeyHome(K.MemoryKeyStore(), K.EnvFile(tmp_path / ".env", environ={"FDC_SIMPLEFIN_ACCESS_URL": "https://u:p@h/x"}))
    assert home.in_env("simplefin") == K.SimpleFinKey("https://u:p@h/x")
    assert home.in_env("snaptrade") is None
    assert home.load("simplefin", "missing-ref") == K.SimpleFinKey("https://u:p@h/x")


def test_the_os_key_store_turns_every_failure_into_no_key_store():
    class Backend:
        def get_password(self, service, ref):
            raise RuntimeError("the secret is s3cr3t")

        def set_password(self, service, ref, secret):
            raise RuntimeError("nope")

        def delete_password(self, service, ref):
            raise RuntimeError("nope")

    store = K.OsKeyStore(Backend())
    for call in (lambda: store.get("r"), lambda: store.set("r", "v")):
        try:
            call()
            assert False
        except K.NoKeyStore as e:
            assert "s3cr3t" not in str(e)
    store.delete("r")  # never raises


def test_for_root_uses_the_patched_store_in_tests(tmp_path: Path, no_real_key_store):
    home = K.KeyHome.for_root(tmp_path)
    ref = home.save("snaptrade", K.SnapTradeKeys("id", "key"))
    assert ref in no_real_key_store.entries
