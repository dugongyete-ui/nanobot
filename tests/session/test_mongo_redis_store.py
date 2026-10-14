from __future__ import annotations

from copy import deepcopy

from nanobot.session import Session, SessionManager
from nanobot.session.mongo_redis_store import MongoRedisSessionStore


class FakeCursor:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows

    def sort(self, key: str, direction: int) -> FakeCursor:
        return FakeCursor(
            sorted(self.rows, key=lambda row: row.get(key, ""), reverse=direction < 0)
        )

    def __iter__(self):
        return iter(self.rows)


class FakeCollection:
    def __init__(self) -> None:
        self.documents: list[dict] = []

    def create_index(self, *_args, **_kwargs) -> None:
        pass

    @staticmethod
    def _matches(document: dict, query: dict) -> bool:
        return all(document.get(key) == value for key, value in query.items())

    def find_one(self, query: dict, projection: dict | None = None) -> dict | None:
        for document in self.documents:
            if self._matches(document, query):
                result = deepcopy(document)
                if projection is not None:
                    result = {
                        key: result[key]
                        for key, include in projection.items()
                        if include and key in result
                    }
                return result
        return None

    def find(self, query: dict) -> FakeCursor:
        return FakeCursor(
            [
                deepcopy(document)
                for document in self.documents
                if self._matches(document, query)
            ]
        )

    def replace_one(self, query: dict, replacement: dict, *, upsert: bool = False) -> None:
        for index, document in enumerate(self.documents):
            if self._matches(document, query):
                self.documents[index] = deepcopy(replacement)
                return
        if upsert:
            self.documents.append(deepcopy(replacement))

    def insert_many(self, documents: list[dict], *, ordered: bool = True) -> None:
        del ordered
        self.documents.extend(deepcopy(documents))

    def delete_one(self, query: dict) -> None:
        for index, document in enumerate(self.documents):
            if self._matches(document, query):
                del self.documents[index]
                return

    def delete_many(self, query: dict) -> None:
        self.documents = [
            document
            for document in self.documents
            if not self._matches(document, query)
        ]

    def update_one(self, query: dict, update: list[dict]) -> object:
        for document in self.documents:
            if not self._matches(document, query):
                continue
            metadata_update = update[0]["$set"]["metadata"]["$mergeObjects"][1]["$literal"]
            metadata = deepcopy(document.get("metadata") or {})
            metadata.update(deepcopy(metadata_update))
            document["metadata"] = metadata
            return type("Result", (), {"matched_count": 1})()
        return type("Result", (), {"matched_count": 0})()


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self.values.get(key)

    def setex(self, key: str, _ttl: int, value: str) -> None:
        self.values[key] = value

    def delete(self, key: str) -> int:
        return int(self.values.pop(key, None) is not None)


def test_mongo_redis_session_store_imports_and_round_trips_legacy_sessions(
    tmp_path,
) -> None:
    workspace = tmp_path / "workspace"
    sessions_root = tmp_path / "sessions"
    legacy_manager = SessionManager(workspace, sessions_root=sessions_root)
    legacy_session = legacy_manager.get_or_create("webui:legacy-session")
    legacy_session.add_message("user", "Keep this existing history")
    legacy_session.add_message("assistant", "History retained")
    legacy_session.metadata["title"] = "Legacy conversation"
    legacy_manager.save(legacy_session)
    legacy_path = legacy_manager._get_session_path(legacy_session.key)
    original_contents = legacy_path.read_bytes()

    headers = FakeCollection()
    chunks = FakeCollection()
    redis = FakeRedis()
    store = MongoRedisSessionStore(headers, chunks, redis)
    manager = SessionManager(
        workspace,
        store=store,
        sessions_root=sessions_root,
    )

    assert headers.find_one({"_id": legacy_session.key}) is not None
    assert legacy_path.exists()
    assert legacy_path.read_bytes() == original_contents
    assert len(chunks.documents) == 1

    payload = store.read(legacy_session.key)
    assert payload is not None
    assert [message["content"] for message in payload["messages"]] == [
        "Keep this existing history",
        "History retained",
    ]
    assert store.read_metadata(legacy_session.key)["metadata"]["title"] == (
        "Legacy conversation"
    )
    assert store.list_sessions()[0]["preview"] == "Keep this existing history"
    assert redis.values

    assert store.update_metadata(legacy_session.key, {"pinned": True})
    assert store.read_metadata(legacy_session.key)["metadata"]["pinned"] is True

    assert manager.delete_session(legacy_session.key) is True
    assert headers.find_one({"_id": legacy_session.key}) is None
    assert chunks.documents == []
    assert not legacy_path.exists()
