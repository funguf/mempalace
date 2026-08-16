from types import SimpleNamespace

from mempalace import mcp_server, service
from mempalace.searcher import _metadata_has_allowed_tag, build_where_filter


class _FakeCollection:
    def __init__(self):
        self.rows = {}

    def upsert(self, *, ids, documents, metadatas):
        for drawer_id, document, metadata in zip(ids, documents, metadatas):
            self.rows[drawer_id] = {"document": document, "metadata": metadata}

    def get(self, *, ids=None, where=None, include=None):
        selected = self.rows.items()
        if ids is not None:
            selected = [
                (drawer_id, self.rows[drawer_id]) for drawer_id in ids if drawer_id in self.rows
            ]
        if where is not None:
            clauses = where.get("$and", [where])
            selected = [
                (drawer_id, row)
                for drawer_id, row in selected
                if all(
                    row["metadata"].get(key) == value
                    for clause in clauses
                    for key, value in clause.items()
                )
            ]
        selected = list(selected)
        return {
            "ids": [drawer_id for drawer_id, _row in selected],
            "metadatas": [row["metadata"] for _drawer_id, row in selected],
        }

    def delete(self, *, ids):
        for drawer_id in ids:
            self.rows.pop(drawer_id, None)


def _patch_collection(monkeypatch):
    collection = _FakeCollection()
    monkeypatch.setattr(mcp_server, "_get_collection", lambda create=False: collection)
    monkeypatch.setattr(mcp_server, "_config", SimpleNamespace(chunk_size=10_000))
    monkeypatch.setattr(mcp_server, "_wal_log", lambda *args, **kwargs: None)
    return collection


def test_sidecar_add_metadata_and_message_idempotency(monkeypatch):
    collection = _patch_collection(monkeypatch)
    args = {
        "wing": "chirpa",
        "room": "chirpa_chat",
        "content": "Prefers dark roast coffee.",
        "tenant_id": "tenant-a",
        "chat_id": "chat-1",
        "message_id": "message-1",
        "namespace": "chirpa_chat",
        "tags": ["preference", "coffee"],
        "written_by": "chirpa-pool",
        "written_from": "chatId:chat-1",
    }

    first = mcp_server.tool_add_drawer(**args)
    second = mcp_server.tool_add_drawer(**{**args, "content": "retry body changed"})

    assert first["success"] is True
    assert second == {
        "success": True,
        "reason": "already_exists",
        "drawer_id": first["drawer_id"],
    }
    metadata = collection.rows[first["drawer_id"]]["metadata"]
    assert metadata["tenant_id"] == "tenant-a"
    assert metadata["chat_id"] == "chat-1"
    assert metadata["message_id"] == "message-1"
    assert metadata["namespace"] == "chirpa_chat"
    assert metadata["tags_json"] == '["preference", "coffee"]'


def test_sidecar_add_rejects_pii_without_opening_collection(monkeypatch):
    monkeypatch.setattr(
        mcp_server,
        "_get_collection",
        lambda create=False: (_ for _ in ()).throw(AssertionError("must not open palace")),
    )
    result = mcp_server.tool_add_drawer(
        "chirpa", "chat", "password = hunter2", tenant_id="tenant-a"
    )
    assert result["success"] is False
    assert result["error_code"] == "pii"
    assert result["pattern_type"] == "password_phrase"


def test_forget_is_tenant_chat_and_namespace_scoped(monkeypatch):
    collection = _patch_collection(monkeypatch)
    collection.rows = {
        "keep-other-chat": {
            "document": "x",
            "metadata": {"tenant_id": "a", "chat_id": "other", "namespace": "chirpa_chat"},
        },
        "keep-other-tenant": {
            "document": "x",
            "metadata": {"tenant_id": "b", "chat_id": "chat", "namespace": "chirpa_chat"},
        },
        "delete": {
            "document": "x",
            "metadata": {"tenant_id": "a", "chat_id": "chat", "namespace": "chirpa_chat"},
        },
    }

    result = mcp_server.tool_forget_drawers("a", "chat", "chirpa_chat")

    assert result == {"success": True, "deleted": 1}
    assert set(collection.rows) == {"keep-other-chat", "keep-other-tenant"}


def test_search_scope_and_tag_contract_helpers():
    assert build_where_filter(tenant_id="a", namespace="chirpa_chat") == {
        "$and": [{"tenant_id": "a"}, {"namespace": "chirpa_chat"}]
    }
    metadata = {"tags_json": '["family", "coffee"]'}
    assert _metadata_has_allowed_tag(metadata, ["coffee"])
    assert not _metadata_has_allowed_tag(metadata, ["work"])
    assert not _metadata_has_allowed_tag(metadata, [])


def test_daemon_write_contract_covers_sidecar_mutations():
    contract = service.tool_contract()
    assert contract["version"] == 1
    assert {
        "mempalace_add_drawer",
        "mempalace_forget_drawers",
        "mempalace_delete_drawer",
        "mempalace_memories_filed_away",
    } <= set(contract["write_tools"])
    assert service.WRITE_TOOLS <= set(mcp_server.TOOLS)
    assert not service.READ_TOOLS.intersection(service.WRITE_TOOLS)
