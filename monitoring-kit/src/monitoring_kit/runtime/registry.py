"""启动时显式注册扩展，核心不自动扫描或下载插件。"""

from __future__ import annotations

from ..collection.ports import CollectionAdapter
from ..errors import ConfigurationError, UnsupportedCollectionTypeError
from ..history.ports import ContentPolicy


class ExtensionRegistry:
    def __init__(self) -> None:
        self._collection_adapters: list[CollectionAdapter] = []
        self._content_policies: list[ContentPolicy] = []

    def register_collection_adapter(self, adapter: CollectionAdapter) -> None:
        if any(existing.adapter_key == adapter.adapter_key for existing in self._collection_adapters):
            raise ConfigurationError(f"重复注册采集适配器: {adapter.adapter_key}")
        self._collection_adapters.append(adapter)

    def register_content_policy(self, policy: ContentPolicy) -> None:
        if any(existing.policy_ref == policy.policy_ref for existing in self._content_policies):
            raise ConfigurationError(f"重复注册内容策略: {policy.policy_ref}")
        self._content_policies.append(policy)

    def collection_adapter(self, collection_type: str, schema_version: str) -> CollectionAdapter:
        matches = [
            adapter
            for adapter in self._collection_adapters
            if adapter.supports(collection_type, schema_version)
        ]
        if len(matches) != 1:
            if not matches:
                raise UnsupportedCollectionTypeError(
                    f"没有注册采集类型 {collection_type}@{schema_version}"
                )
            raise ConfigurationError(f"采集类型 {collection_type}@{schema_version} 存在多个适配器")
        return matches[0]

    def content_policy(self, content_type_key: str, schema_version: str) -> ContentPolicy:
        matches = [
            policy
            for policy in self._content_policies
            if policy.supports(content_type_key, schema_version)
        ]
        if len(matches) != 1:
            if not matches:
                raise ConfigurationError(f"没有注册内容策略 {content_type_key}@{schema_version}")
            raise ConfigurationError(f"内容类型 {content_type_key}@{schema_version} 存在多个策略")
        return matches[0]

    def content_policy_by_namespace(self, namespace: str) -> ContentPolicy:
        matches = [
            policy
            for policy in self._content_policies
            if getattr(policy, "subject_namespace", getattr(policy, "content_type_key", None)) == namespace
        ]
        if len(matches) != 1:
            if not matches:
                raise ConfigurationError(f"没有注册身份命名空间 {namespace} 对应的内容策略")
            raise ConfigurationError(f"身份命名空间 {namespace} 存在多个内容策略")
        return matches[0]
