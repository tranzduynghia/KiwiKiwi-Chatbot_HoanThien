"""Fixture dùng chung cho kiểm thử: mô phỏng MongoDB bất đồng bộ trong RAM, Gemini client giả và Bot giả."""
from __future__ import annotations

import copy
from types import SimpleNamespace
from typing import Any, Optional

import pytest

from app.core.settings import AppSettings
from app.main import AppContainer, build_container


class FakeCursor:
    """Mô phỏng cursor truy vấn bất đồng bộ của PyMongo (`sort`, `to_list`)."""

    def __init__(self, docs: list[dict[str, Any]]):
        self._docs = docs

    def sort(self, field: str, direction: int = 1) -> "FakeCursor":
        reverse = direction < 0
        self._docs.sort(key=lambda item: item.get(field) or "", reverse=reverse)
        return self

    async def to_list(self, length: Optional[int] = None) -> list[dict[str, Any]]:
        if length is None:
            return copy.deepcopy(self._docs)
        return copy.deepcopy(self._docs[:length])


class FakeCollection:
    """Mô phỏng collection MongoDB trong bộ nhớ hỗ trợ find, update, replace, delete và index."""

    def __init__(self, name: str):
        self.name = name
        self.docs: list[dict[str, Any]] = []
        self.indexes: dict[str, Any] = {}
        self.fail_writes: bool = False

    def _matches(self, doc: dict[str, Any], flt: dict[str, Any]) -> bool:
        for key, expected in (flt or {}).items():
            actual = doc.get(key)
            if isinstance(expected, dict):
                if "$lte" in expected and not (actual is not None and actual <= expected["$lte"]):
                    return False
                if "$gt" in expected and not (actual is not None and actual > expected["$gt"]):
                    return False
            elif actual != expected:
                return False
        return True

    def _apply_projection(self, doc: dict[str, Any], projection: Optional[dict[str, int]]) -> dict[str, Any]:
        result = copy.deepcopy(doc)
        if not projection:
            return result
        if all(val == 0 for val in projection.values()):
            for key in projection:
                result.pop(key, None)
            return result
        out = {}
        for key, val in projection.items():
            if val and key in result:
                out[key] = result[key]
        if "_id" not in projection and "_id" in result:
            out["_id"] = result["_id"]
        return out

    async def create_index(self, keys, **kwargs):
        self.indexes[str(keys)] = kwargs
        return str(keys)

    async def index_information(self) -> dict[str, Any]:
        return copy.deepcopy(self.indexes)

    async def drop_index(self, name: str) -> None:
        self.indexes.pop(name, None)

    async def count_documents(self, flt: dict[str, Any]) -> int:
        return sum(1 for doc in self.docs if self._matches(doc, flt))

    async def estimated_document_count(self) -> int:
        return len(self.docs)

    async def find_one(self, flt: dict[str, Any]) -> Optional[dict[str, Any]]:
        for doc in self.docs:
            if self._matches(doc, flt):
                return copy.deepcopy(doc)
        return None

    def find(self, flt: dict[str, Any], projection: Optional[dict[str, int]] = None) -> FakeCursor:
        matched = [self._apply_projection(doc, projection) for doc in self.docs if self._matches(doc, flt)]
        return FakeCursor(matched)

    async def insert_one(self, doc: dict[str, Any]):
        if self.fail_writes:
            raise ConnectionError("Mô phỏng lỗi ghi MongoDB")
        self.docs.append(copy.deepcopy(doc))
        return SimpleNamespace(inserted_id=doc.get("_id"))

    async def replace_one(self, flt: dict[str, Any], replacement: dict[str, Any], upsert: bool = False):
        if self.fail_writes:
            raise ConnectionError("Mô phỏng lỗi ghi MongoDB")
        for idx, doc in enumerate(self.docs):
            if self._matches(doc, flt):
                self.docs[idx] = copy.deepcopy(replacement)
                return SimpleNamespace(modified_count=1)
        if upsert:
            self.docs.append(copy.deepcopy(replacement))
            return SimpleNamespace(modified_count=1)
        return SimpleNamespace(modified_count=0)

    async def update_one(self, flt: dict[str, Any], update: dict[str, Any], upsert: bool = False):
        if self.fail_writes:
            raise ConnectionError("Mô phỏng lỗi ghi MongoDB")
        target = None
        for doc in self.docs:
            if self._matches(doc, flt):
                target = doc
                break
        is_insert = target is None
        if is_insert:
            if not upsert:
                return SimpleNamespace(modified_count=0)
            target = copy.deepcopy(flt)
            self.docs.append(target)
            for key, val in (update.get("$setOnInsert") or {}).items():
                target.setdefault(key, copy.deepcopy(val))
        for key, val in (update.get("$set") or {}).items():
            target[key] = copy.deepcopy(val)
        for key, delta in (update.get("$inc") or {}).items():
            target[key] = target.get(key, 0) + delta
        for key, spec in (update.get("$push") or {}).items():
            arr = target.setdefault(key, [])
            if isinstance(spec, dict) and "$each" in spec:
                arr.extend(copy.deepcopy(spec["$each"]))
                if "$slice" in spec:
                    sl = spec["$slice"]
                    target[key] = arr[sl:] if sl < 0 else arr[:sl]
            else:
                arr.append(copy.deepcopy(spec))
        return SimpleNamespace(modified_count=1)

    async def delete_one(self, flt: dict[str, Any]):
        if self.fail_writes:
            raise ConnectionError("Mô phỏng lỗi xóa MongoDB")
        for idx, doc in enumerate(self.docs):
            if self._matches(doc, flt):
                self.docs.pop(idx)
                return SimpleNamespace(deleted_count=1)
        return SimpleNamespace(deleted_count=0)


class FakeDatabase:
    """Mô phỏng database MongoDB trả về các FakeCollection theo tên."""

    def __init__(self):
        self._collections: dict[str, FakeCollection] = {}

    def __getitem__(self, name: str) -> FakeCollection:
        if name not in self._collections:
            self._collections[name] = FakeCollection(name)
        return self._collections[name]


class FakeAsyncPager:
    """Mô phỏng bộ phân trang bất đồng bộ cho `gemini.aio.models.list()`."""

    def __init__(self, items: list[Any]):
        self._items = items

    def __aiter__(self):
        self._iter = iter(self._items)
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


class FakeGeminiClient:
    """Mô phỏng tối giản Google GenAI client phục vụ kiểm thử offline."""

    def __init__(self, models: Optional[list[Any]] = None):
        self._models = models or [
            SimpleNamespace(name="models/gemini-2.5-flash-lite", display_name="Gemini 2.5 Flash Lite", supported_actions=["generateContent"]),
            SimpleNamespace(name="models/gemini-2.5-flash", display_name="Gemini 2.5 Flash", supported_actions=["generateContent"]),
            SimpleNamespace(name="models/gemini-2.5-pro", display_name="Gemini 2.5 Pro", supported_actions=["generateContent"]),
            SimpleNamespace(name="models/gemini-3.5-flash", display_name="Gemini 3.5 Flash", supported_actions=["generateContent"]),
            SimpleNamespace(name="models/gemini-embedding-001", display_name="Embedding", supported_actions=["embedContent"]),
        ]
        self.aio = SimpleNamespace(models=SimpleNamespace(list=self._list_models, get=self._get_model))

    async def _list_models(self, config=None):
        return FakeAsyncPager(self._models)

    async def _get_model(self, model: str):
        return SimpleNamespace(name=f"models/{model}")


class FakeBot:
    """Mô phỏng client Discord tối thiểu phục vụ API `/health` và `/api/status`."""

    def __init__(self):
        self.user = SimpleNamespace(id=999999999999999999, __str__=lambda _s: "KiwiKiwi#0001")
        self.guilds = [SimpleNamespace(id=111, name="Test Guild", member_count=42)]
        self.latency = 0.045
        self.ready_at = 1700000000.0

    def is_ready(self) -> bool:
        return True


@pytest.fixture
def fake_db() -> FakeDatabase:
    """Tạo database giả sạch cho mỗi test case."""
    return FakeDatabase()


@pytest.fixture
def fake_gemini() -> FakeGeminiClient:
    """Tạo Gemini client giả cho mỗi test case."""
    return FakeGeminiClient()


@pytest.fixture
def fake_bot() -> FakeBot:
    """Tạo đối tượng Bot giả cho mỗi test case."""
    return FakeBot()


@pytest.fixture
def app_container(monkeypatch, fake_gemini) -> AppContainer:
    """Khởi tạo AppContainer hoàn toàn độc lập với biến môi trường thật."""
    monkeypatch.delenv("INITIAL_ADMIN_USERNAME", raising=False)
    monkeypatch.delenv("INITIAL_ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("INITIAL_ADMIN_DISCORD_IDS", raising=False)
    monkeypatch.setenv("STATUS_ALLOWED_IDS", "1240503090185109536")
    settings = AppSettings(
        discord_token="test-discord-token",
        gemini_api_key="test-gemini-key",
        session_secret="test-session-secret-key-for-pytest",
        mongodb_uri="",
    )
    return build_container(settings, gemini_client=fake_gemini)
