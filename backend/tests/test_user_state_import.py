from app.db.engine import Database
from app.db.models import Channel, Library, Video
from app.repositories.user_state import import_state, export_state
from app.services.jobs import ensure_default_handlers_registered


def _seed(db):
    with db.session() as s:
        lib = Library(name="main")
        s.add(lib); s.flush()
        ch = Channel(library_id=lib.id, yt_id="UC1", name="Chan One")
        s.add(ch); s.flush()
        v = Video(channel_id=ch.id, yt_id="vid_a", title="A")
        s.add(v); s.commit()


def test_import_state_safe_idempotent_mapping(tmp_path):
    db = Database.open(tmp_path / "t.db"); db.migrate(); _seed(db)
    state = {
        "ratings": {"UC1": 5, "UC_missing": 3},
        "hidden-ch": ["UC1"],
        "history": [{"id": "vid_a", "t": 1700000000000, "w": 1.0}, {"id": "vid_missing", "t": 1}],
        "reco": {"affinity": 70},
        "filters": {"shorts": True},
    }
    with db.session() as s:
        counts = import_state(s, state)
        s.commit()
        assert counts["ratings"] == 2  # attempted; only existing mapped
        assert counts["history"] == 1   # only known video
    # export reflects mapped state
    with db.session() as s:
        out = export_state(s)
        assert out["ratings"].get("UC1") == 5
        assert any(h["id"] == "vid_a" for h in out["history"])
        assert "UC1" in out["hidden-ch"]
        assert out["reco"].get("affinity") == 70
        assert out["filters"].get("shorts") is True
