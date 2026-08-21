from paperboy.secrets import MemorySecrets


def test_memory_secrets_round_trip():
    store = MemorySecrets()
    assert store.get_api_hash() is None
    assert store.get_session() is None
    store.set_api_hash("abc123")
    store.set_session("sess:xyz")
    assert store.get_api_hash() == "abc123"
    assert store.get_session() == "sess:xyz"


def test_memory_secrets_seeded():
    store = MemorySecrets({"api_hash": "seeded"})
    assert store.get_api_hash() == "seeded"
