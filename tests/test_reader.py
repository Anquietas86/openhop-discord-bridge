import hashlib
import hmac
import sqlite3

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from openhop_discord_bridge import reader as r

SECRET = bytes(range(32))
OTHER = bytes(range(32, 64))


def encrypt(secret: bytes, body: str, device_ts: int = 1_700_000_000, flags: int = 0) -> str:
    plaintext = device_ts.to_bytes(4, "little") + bytes([flags]) + body.encode() + b"\x00"
    pad = (-len(plaintext)) % 16
    plaintext += b"\x00" * pad
    enc = Cipher(algorithms.AES(secret[:16]), modes.ECB()).encryptor()
    ciphertext = enc.update(plaintext) + enc.finalize()
    mac = hmac.new(secret, ciphertext, hashlib.sha256).digest()[:2]
    return (b"\x2a" + mac + ciphertext).hex()


def test_decrypts_matching_candidate():
    payload = encrypt(SECRET, "VK5ALG: hello mesh")
    out = r.decrypt_group_text(payload, {"Public": [SECRET]})
    assert out is not None
    assert out["channel"] == "Public"
    assert out["text"] == "VK5ALG: hello mesh"


def test_rejects_wrong_secret():
    payload = encrypt(SECRET, "hello")
    assert r.decrypt_group_text(payload, {"Public": [OTHER]}) is None


def test_tries_every_candidate_for_a_name():
    payload = encrypt(SECRET, "hello")
    out = r.decrypt_group_text(payload, {"Public": [OTHER, SECRET]})
    assert out is not None and out["text"] == "hello"


def test_rejects_unaligned_ciphertext():
    payload = encrypt(SECRET, "hello")
    raw = bytes.fromhex(payload)
    broken = (raw[:3] + raw[3:-1]).hex()
    assert r.decrypt_group_text(broken, {"Public": [SECRET]}) is None


def test_drops_unsupported_flags():
    payload = encrypt(SECRET, "hello", flags=0b0000_0100)
    assert r.decrypt_group_text(payload, {"Public": [SECRET]}) is None


def test_ignores_non_hex_payload():
    assert r.decrypt_group_text("zzzz", {"Public": [SECRET]}) is None


def _make_db(path, rows):
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE packets (id INTEGER PRIMARY KEY, timestamp REAL, type INT, "
        "payload TEXT, transmitted INT)"
    )
    conn.execute(
        "CREATE TABLE companion_channels (companion_hash TEXT, channel_idx INT, "
        "name TEXT, secret BLOB)"
    )
    conn.execute(
        "INSERT INTO companion_channels VALUES ('0x01', 0, 'Public', ?)", (SECRET,)
    )
    conn.executemany(
        "INSERT INTO packets (id, timestamp, type, payload, transmitted) VALUES (?,?,?,?,?)",
        rows,
    )
    conn.commit()
    conn.close()


def test_tailer_reads_new_rows_once(tmp_path):
    db = tmp_path / "repeater.db"
    payload = encrypt(SECRET, "Local Chat: test")
    _make_db(db, [(10, 0.0, 5, payload, 0)])

    tailer = r.PacketTailer(db, {"Public": [SECRET]}, start_from_latest=False)
    first = tailer.fetch_new()
    assert [m.text for m in first] == ["Local Chat: test"]
    assert first[0].packet_id == 10
    assert tailer.fetch_new() == []


def test_tailer_dedupes_repeated_hops(tmp_path):
    db = tmp_path / "repeater.db"
    payload = encrypt(SECRET, "hello", device_ts=99)
    _make_db(
        db,
        [
            (1, 0.0, 5, payload, 1),
            (2, 0.0, 5, payload, 0),  # same message, second hop
            (3, 0.0, 5, payload, 0),
        ],
    )
    tailer = r.PacketTailer(db, {"Public": [SECRET]}, start_from_latest=False)
    assert len(tailer.fetch_new()) == 1


def test_tailer_filters_unmapped_channels(tmp_path):
    db = tmp_path / "repeater.db"
    _make_db(db, [(1, 0.0, 5, encrypt(SECRET, "x"), 0)])
    tailer = r.PacketTailer(
        db, {"Public": [SECRET], "#other": [SECRET]}, wanted_channels=["#other"],
        start_from_latest=False,
    )
    assert tailer.fetch_new() == []


def test_start_from_latest_skips_history(tmp_path):
    db = tmp_path / "repeater.db"
    _make_db(db, [(1, 0.0, 5, encrypt(SECRET, "old"), 0)])
    tailer = r.PacketTailer(db, {"Public": [SECRET]}, start_from_latest=True)
    assert tailer.fetch_new() == []


def test_load_channel_secrets(tmp_path):
    db = tmp_path / "repeater.db"
    _make_db(db, [])
    secrets = r.load_channel_secrets(db)
    assert "Public" in secrets
    assert secrets["Public"][0] == SECRET
