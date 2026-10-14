"""MongoDB-backed session storage with a Redis read cache for Replit."""

from __future__ import annotations

import hashlib
import json
import os
from contextlib import suppress
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from loguru import logger
from pymongo import ASCENDING, DESCENDING, MongoClient
from redis import Redis

from nanobot.providers.base import ProviderConversationState
from nanobot.session.manager import (
    JsonlSessionStore,
    Session,
    SessionInfo,
    SessionManager,
    SessionMetadataPayload,
    SessionPayload,
    _message_preview_text,
    _metadata_title,
)

_CACHE_PREFIX = "nanobot:session:v1:"
_CACHE_TTL_SECONDS = 30
_CACHE_MAX_BYTES = 512 * 1024
_MONGO_CHUNK_MAX_BYTES = 8 * 1024 * 1024
_SESSION_ENV_NAMES = ("MONGODB_URI", "MONGODB_DATABASE", "REDIS_HOST", "REDIS_PORT")


class MongoRedisSessionStore:
    """Persist sessions in MongoDB, cache recent session snapshots in Redis.

    JSONL remains a compatibility mirror because nanobot's memory and WebUI
    session index still scan the local session directory directly.
    """

    def __init__(
        self,
        sessions_collection: Any,
        message_chunks_collection: Any,
        redis_client: Any,
        *,
        cache_ttl_seconds: int = _CACHE_TTL_SECONDS,
    ) -> None:
        self._sessions = sessions_collection
        self._message_chunks_collection = message_chunks_collection
        self._redis = redis_client
        self._cache_ttl_seconds = cache_ttl_seconds
        self._jsonl_store: JsonlSessionStore | None = None
        self._sessions.create_index(
            [("updated_at", DESCENDING)],
            name="nanobot_sessions_updated_at",
        )
        self._message_chunks_collection.create_index(
            [
                ("session_key", ASCENDING),
                ("generation", ASCENDING),
                ("chunk_index", ASCENDING),
            ],
            unique=True,
            name="nanobot_session_chunks_order",
        )

    def bind_jsonl_store(self, store: JsonlSessionStore) -> None:
        """Attach the compatibility mirror and copy existing data without deleting it."""
        self._jsonl_store = store

        # Import old local histories only when MongoDB has no copy. MongoDB is
        # authoritative once a record exists there; the source JSONL is retained.
        for row in store.list_sessions():
            key = row.get("key")
            if not isinstance(key, str) or not key:
                continue
            if self._sessions.find_one({"_id": key}, {"_id": 1}) is not None:
                continue
            session = store.load(key)
            if session is None:
                continue
            self._persist_to_mongo(session)

        # Rebuild local compatibility files from MongoDB so existing consumers
        # (memory extraction, the sidebar index, and rollback tooling) keep working.
        for header in self._sessions.find({}):
            key = header.get("key") or header.get("_id")
            if not isinstance(key, str) or not key:
                continue
            try:
                session = self._session_from_document(
                    self._document_with_messages(header, key)
                )
                store.save(session)
            except Exception:
                logger.warning(
                    "A MongoDB session could not be mirrored to local JSONL; "
                    "the MongoDB copy remains intact."
                )

    @staticmethod
    def _cache_key(key: str) -> str:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return f"{_CACHE_PREFIX}{digest}"

    def _invalidate_cache(self, key: str) -> None:
        try:
            self._redis.delete(self._cache_key(key))
        except Exception:
            logger.debug("Redis session-cache invalidation failed; MongoDB remains authoritative.")

    def _cached_document(self, key: str) -> dict[str, Any] | None:
        try:
            raw = self._redis.get(self._cache_key(key))
        except Exception:
            logger.debug("Redis session-cache read failed; loading from MongoDB.")
            return None
        if raw is None:
            return None
        try:
            document = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
        except (TypeError, ValueError, UnicodeDecodeError):
            self._invalidate_cache(key)
            return None
        if (
            not isinstance(document, dict)
            or document.get("key") != key
            or not isinstance(document.get("messages"), list)
        ):
            self._invalidate_cache(key)
            return None
        return cast(dict[str, Any], document)

    def _cache_document(self, document: dict[str, Any]) -> None:
        try:
            serialized = json.dumps(document, ensure_ascii=False, separators=(",", ":"))
            if len(serialized.encode("utf-8")) > _CACHE_MAX_BYTES:
                return
            self._redis.setex(
                self._cache_key(str(document["key"])),
                self._cache_ttl_seconds,
                serialized,
            )
        except Exception:
            logger.debug("Redis session-cache write failed; MongoDB remains authoritative.")

    @staticmethod
    def _session_document(session: Session, generation: str) -> dict[str, Any]:
        return {
            "_id": session.key,
            "key": session.key,
            "created_at": session.created_at.isoformat(),
            "updated_at": session.updated_at.isoformat(),
            "metadata": deepcopy(session.metadata),
            "last_consolidated": session.last_consolidated,
            "provider_state": (
                session.provider_state.to_private_record()
                if session.provider_state is not None
                else None
            ),
            "generation": generation,
            "message_count": len(session.messages),
        }

    @staticmethod
    def _build_message_chunks(
        session: Session,
        generation: str,
    ) -> list[dict[str, Any]]:
        chunks: list[dict[str, Any]] = []
        messages: list[dict[str, Any]] = []
        size = 0

        def flush() -> None:
            nonlocal messages, size
            if not messages:
                return
            chunks.append(
                {
                    "session_key": session.key,
                    "generation": generation,
                    "chunk_index": len(chunks),
                    "messages": messages,
                }
            )
            messages = []
            size = 0

        for message in session.messages:
            encoded = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
            if len(encoded) > _MONGO_CHUNK_MAX_BYTES:
                raise ValueError(
                    "A single session message exceeds the MongoDB chunk size limit."
                )
            if messages and size + len(encoded) > _MONGO_CHUNK_MAX_BYTES:
                flush()
            messages.append(deepcopy(message))
            size += len(encoded)
        flush()
        return chunks

    def _persist_to_mongo(self, session: Session, *, fsync: bool = False) -> None:
        del fsync  # MongoDB acknowledges durable writes; retained for SessionStore compatibility.
        old = self._sessions.find_one({"_id": session.key}, {"generation": 1})
        old_generation = old.get("generation") if old else None
        generation = uuid4().hex
        chunks = self._build_message_chunks(session, generation)
        try:
            if chunks:
                self._message_chunks_collection_insert(chunks)
            self._sessions.replace_one(
                {"_id": session.key},
                self._session_document(session, generation),
                upsert=True,
            )
        except Exception:
            with suppress(Exception):
                self._message_chunks_collection.delete_many(
                    {"session_key": session.key, "generation": generation}
                )
            raise

        if isinstance(old_generation, str) and old_generation != generation:
            try:
                self._message_chunks_collection.delete_many(
                    {"session_key": session.key, "generation": old_generation}
                )
            except Exception:
                logger.warning(
                    "An obsolete MongoDB session generation could not be cleaned up."
                )

    def _message_chunks_collection_insert(self, chunks: list[dict[str, Any]]) -> None:
        self._message_chunks_collection.insert_many(chunks, ordered=True)

    def _document_with_messages(self, header: dict[str, Any], key: str) -> dict[str, Any]:
        document = dict(header)
        document.pop("_id", None)
        document["key"] = key
        messages: list[dict[str, Any]] = []
        generation = document.get("generation")
        if isinstance(generation, str) and generation:
            cursor = self._message_chunks_collection.find(
                {"session_key": key, "generation": generation}
            ).sort("chunk_index", ASCENDING)
            for chunk in cursor:
                chunk_messages = chunk.get("messages")
                if isinstance(chunk_messages, list):
                    messages.extend(
                        cast(list[dict[str, Any]], chunk_messages)
                    )
        document["messages"] = messages
        return document

    @staticmethod
    def _parse_datetime(value: object) -> datetime:
        if isinstance(value, datetime):
            return value
        if isinstance(value, str) and value:
            try:
                return datetime.fromisoformat(value)
            except ValueError:
                pass
        return datetime.now()

    @classmethod
    def _session_from_document(cls, document: dict[str, Any]) -> Session:
        key = document.get("key")
        if not isinstance(key, str) or not key:
            raise ValueError("MongoDB session is missing its key.")
        raw_messages = document.get("messages")
        messages = (
            [cast(dict[str, Any], message) for message in raw_messages if isinstance(message, dict)]
            if isinstance(raw_messages, list)
            else []
        )
        raw_metadata = document.get("metadata")
        metadata = deepcopy(raw_metadata) if isinstance(raw_metadata, dict) else {}
        raw_offset = document.get("last_consolidated", document.get("last_archived", 0))
        last_consolidated = (
            raw_offset
            if isinstance(raw_offset, int) and not isinstance(raw_offset, bool)
            else 0
        )
        raw_provider_state = document.get("provider_state")
        provider_state = (
            ProviderConversationState.from_private_record(raw_provider_state)
            if isinstance(raw_provider_state, dict)
            else None
        )
        return Session(
            key=key,
            messages=messages,
            created_at=cls._parse_datetime(document.get("created_at")),
            updated_at=cls._parse_datetime(document.get("updated_at")),
            metadata=metadata,
            last_consolidated=last_consolidated,
            provider_state=provider_state,
        )

    def _mirror_local(self, session: Session, *, fsync: bool = False) -> None:
        if self._jsonl_store is None:
            return
        try:
            self._jsonl_store.save(session, fsync=fsync)
        except Exception:
            logger.warning(
                "MongoDB saved a session, but its local JSONL compatibility mirror "
                "could not be refreshed."
            )

    def _load_document(self, key: str) -> dict[str, Any] | None:
        cached = self._cached_document(key)
        if cached is not None:
            return cached
        header = self._sessions.find_one({"_id": key})
        if header is None:
            return None
        document = self._document_with_messages(header, key)
        self._cache_document(document)
        return document

    def _mirror_if_newer(self, session: Session) -> None:
        if self._jsonl_store is None:
            return
        try:
            local_session = self._jsonl_store.load(session.key)
            if local_session is None or session.updated_at >= local_session.updated_at:
                self._jsonl_store.save(session)
        except Exception:
            logger.warning(
                "MongoDB loaded a session, but its local JSONL compatibility mirror "
                "could not be refreshed."
            )

    def load(self, key: str) -> Session | None:
        document = self._load_document(key)
        if document is not None:
            session = self._session_from_document(document)
            self._mirror_if_newer(session)
            return session
        if self._jsonl_store is None:
            return None
        session = self._jsonl_store.load(key)
        if session is not None:
            self._persist_to_mongo(session)
            self._cache_document(
                {
                    "key": session.key,
                    "created_at": session.created_at.isoformat(),
                    "updated_at": session.updated_at.isoformat(),
                    "metadata": deepcopy(session.metadata),
                    "last_consolidated": session.last_consolidated,
                    "provider_state": (
                        session.provider_state.to_private_record()
                        if session.provider_state is not None
                        else None
                    ),
                    "messages": deepcopy(session.messages),
                }
            )
        return session

    def save(self, session: Session, *, fsync: bool = False) -> None:
        self._persist_to_mongo(session, fsync=fsync)
        self._invalidate_cache(session.key)
        self._mirror_local(session, fsync=fsync)
        self._cache_document(
            {
                "key": session.key,
                "created_at": session.created_at.isoformat(),
                "updated_at": session.updated_at.isoformat(),
                "metadata": deepcopy(session.metadata),
                "last_consolidated": session.last_consolidated,
                "provider_state": (
                    session.provider_state.to_private_record()
                    if session.provider_state is not None
                    else None
                ),
                "messages": deepcopy(session.messages),
            }
        )

    def delete(self, key: str) -> bool:
        header = self._sessions.find_one({"_id": key}, {"_id": 1})
        if header is not None:
            self._sessions.delete_one({"_id": key})
        self._message_chunks_collection.delete_many({"session_key": key})
        self._invalidate_cache(key)
        local_deleted = self._jsonl_store.delete(key) if self._jsonl_store else False
        return header is not None or local_deleted

    def read(self, key: str) -> SessionPayload | None:
        session = self.load(key)
        if session is None:
            return None
        return {
            "key": session.key,
            "created_at": session.created_at.isoformat(),
            "updated_at": session.updated_at.isoformat(),
            "metadata": deepcopy(session.metadata),
            "messages": deepcopy(session.messages),
        }

    def read_metadata(self, key: str) -> SessionMetadataPayload | None:
        document = self._cached_document(key)
        if document is None:
            header = self._sessions.find_one({"_id": key})
            if header is None:
                if self._jsonl_store is None:
                    return None
                metadata = self._jsonl_store.read_metadata(key)
                if metadata is not None:
                    session = self._jsonl_store.load(key)
                    if session is not None:
                        self._persist_to_mongo(session)
                return metadata
            document = header
        return {
            "key": str(document.get("key") or key),
            "created_at": (
                document.get("created_at")
                if isinstance(document.get("created_at"), str)
                else None
            ),
            "updated_at": (
                document.get("updated_at")
                if isinstance(document.get("updated_at"), str)
                else None
            ),
            "metadata": (
                deepcopy(document.get("metadata"))
                if isinstance(document.get("metadata"), dict)
                else {}
            ),
        }

    def update_metadata(
        self,
        key: str,
        updates: dict[str, Any],
        *,
        fsync: bool = False,
    ) -> bool:
        del fsync
        result = self._sessions.update_one(
            {"_id": key},
            [
                {
                    "$set": {
                        "metadata": {
                            "$mergeObjects": [
                                {"$ifNull": ["$metadata", {}]},
                                {"$literal": deepcopy(updates)},
                            ]
                        }
                    }
                }
            ],
        )
        if not result.matched_count:
            if self._jsonl_store is None:
                return False
            session = self._jsonl_store.load(key)
            if session is None:
                return False
            session.metadata.update(deepcopy(updates))
            self.save(session)
            return True
        self._invalidate_cache(key)
        if self._jsonl_store is not None:
            if not self._jsonl_store.update_metadata(key, updates):
                session = self.load(key)
                if session is not None:
                    self._mirror_local(session)
        return True

    def list_sessions(self) -> list[SessionInfo]:
        cursor = self._sessions.find({}).sort("updated_at", DESCENDING)
        sessions: list[SessionInfo] = []
        for header in cursor:
            key = header.get("key") or header.get("_id")
            if not isinstance(key, str) or not key:
                continue
            title = _metadata_title(header.get("metadata"))
            preview = ""
            fallback_preview = ""
            generation = header.get("generation")
            if isinstance(generation, str) and generation:
                chunks = self._message_chunks_collection.find(
                    {"session_key": key, "generation": generation}
                ).sort("chunk_index", ASCENDING)
                for chunk in chunks:
                    raw_messages = chunk.get("messages")
                    if not isinstance(raw_messages, list):
                        continue
                    for raw_message in raw_messages:
                        if not isinstance(raw_message, dict):
                            continue
                        text = _message_preview_text(raw_message)
                        if not text:
                            continue
                        if raw_message.get("role") == "user":
                            preview = text
                            break
                        if not fallback_preview and raw_message.get("role") == "assistant":
                            fallback_preview = text
                    if preview:
                        break
            if not preview:
                preview = fallback_preview
            if self._jsonl_store is not None:
                path = str(self._jsonl_store.get_session_path(key))
            else:
                path = ""
            sessions.append(
                {
                    "key": key,
                    "created_at": str(header.get("created_at") or ""),
                    "updated_at": str(header.get("updated_at") or ""),
                    "title": title,
                    "preview": preview,
                    "path": path,
                }
            )
        return sessions


def create_replit_session_manager(
    workspace: Path,
    *,
    sessions_root: Path | None = None,
) -> SessionManager:
    """Use configured MongoDB/Redis services; keep local JSONL when none are set."""
    if not any(name in os.environ for name in _SESSION_ENV_NAMES):
        return SessionManager(workspace, sessions_root=sessions_root)

    missing = [name for name in _SESSION_ENV_NAMES if not os.environ.get(name, "").strip()]
    if missing:
        raise RuntimeError(
            "MongoDB/Redis session storage is partially configured. Missing or empty "
            "environment variables: " + ", ".join(missing)
        )

    try:
        redis_port = int(os.environ["REDIS_PORT"])
        if not 1 <= redis_port <= 65535:
            raise ValueError
    except ValueError:
        raise RuntimeError("REDIS_PORT must be an integer between 1 and 65535.") from None

    mongo_client = MongoClient(
        os.environ["MONGODB_URI"],
        serverSelectionTimeoutMS=5000,
        connectTimeoutMS=5000,
        appname="nanobot-replit",
    )
    redis_client = Redis(
        host=os.environ["REDIS_HOST"],
        port=redis_port,
        password=os.environ.get("REDIS_PASSWORD") or None,
        decode_responses=True,
        socket_connect_timeout=5,
        socket_timeout=5,
        health_check_interval=30,
    )
    try:
        mongo_client.admin.command("ping")
        redis_client.ping()
    except Exception:
        mongo_client.close()
        redis_client.close()
        raise RuntimeError(
            "MongoDB or Redis is configured but unavailable. Check the Replit "
            "environment variables and service network access."
        ) from None

    try:
        database = mongo_client[os.environ["MONGODB_DATABASE"]]
        store = MongoRedisSessionStore(
            database["nanobot_sessions"],
            database["nanobot_session_chunks"],
            redis_client,
        )
        manager = SessionManager(
            workspace,
            store=store,
            sessions_root=sessions_root,
        )
    except Exception:
        mongo_client.close()
        redis_client.close()
        raise RuntimeError(
            "MongoDB/Redis session storage could not be initialized. Check the "
            "database permissions and existing session data."
        ) from None

    logger.info("Session storage active: MongoDB primary with Redis cache.")
    return manager
