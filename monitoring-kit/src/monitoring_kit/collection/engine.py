"""可靠采集运行引擎。"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Any

from ..contracts.audit import AuditEvent
from ..contracts.primitives import utc_now
from ..contracts.run import (
    CancellationResult,
    ExecutionContext,
    RecoverySummary,
    RunError,
    RunRef,
    RunRequest,
    RunStatus,
    RunSummary,
    WorkSummary,
)
from ..errors import (
    ConfigurationError,
    ExecutionLimitRejectedError,
    IdempotencyConflictError,
    InvalidCollectionSpecError,
    InvalidRequestError,
    MonitoringError,
    RunNotFoundError,
)
from ..history.history import ContentHistory
from ..runtime.model import AllocationRequest
from ..runtime.registry import ExtensionRegistry
from ..runtime.ports import AuditSink, TelemetrySink, WorkAllocator
from .model import (
    AdapterContext,
    Attempt,
    RecordFailure,
    RunRecord,
    UpstreamState,
    ValidationResult,
    observation_from_draft,
)
from .ports import (
    CollectionAdapter,
    RunStateConflictError,
    RunStateStore,
    UpstreamError,
    UpstreamJobGateway,
)


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """运行内部使用的重试和轮询策略，不进入 RunRequest。"""

    max_attempts: int = 3
    base_delay_seconds: float = 1.0
    max_delay_seconds: float = 60.0
    poll_interval_seconds: float = 1.0
    lease_seconds: float = 60.0
    max_batches_per_wake: int = 100

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts 必须大于 0")
        if self.base_delay_seconds < 0 or self.max_delay_seconds < 0:
            raise ValueError("退避时间不能为负数")
        if self.max_delay_seconds < self.base_delay_seconds:
            raise ValueError("max_delay_seconds 不能小于 base_delay_seconds")
        if self.poll_interval_seconds < 0 or self.lease_seconds <= 0:
            raise ValueError("轮询时间不能为负数，租约时间必须大于 0")
        if self.max_batches_per_wake < 1:
            raise ValueError("max_batches_per_wake 必须大于 0")


class CollectionEngine:
    """隐藏 Run、Attempt、上游任务、游标和恢复顺序的深模块。"""

    def __init__(
        self,
        state_store: RunStateStore,
        history: ContentHistory,
        registry: ExtensionRegistry,
        gateway: UpstreamJobGateway,
        *,
        clock: Any | None = None,
        retry_policy: RetryPolicy | None = None,
        worker_id: str | None = None,
        audit_sink: AuditSink | None = None,
        telemetry_sink: TelemetrySink | None = None,
        allocator: WorkAllocator | None = None,
        global_concurrency_limit: int | None = None,
    ) -> None:
        self._state_store = state_store
        self._history = history
        self._registry = registry
        self._gateway = gateway
        self._clock = clock or _SystemClock()
        self._retry_policy = retry_policy or RetryPolicy()
        self._worker_id = worker_id or f"worker-{uuid.uuid4()}"
        self._audit_sink = audit_sink
        self._telemetry_sink = telemetry_sink
        if global_concurrency_limit is not None:
            if isinstance(global_concurrency_limit, bool) or global_concurrency_limit < 1:
                raise ValueError("global_concurrency_limit 必须大于 0")
        self._global_concurrency_limit = global_concurrency_limit
        self._allocator = allocator or state_store

    def submit_run(self, request: RunRequest, context: ExecutionContext) -> RunRef:
        if not isinstance(request, RunRequest) or not isinstance(context, ExecutionContext):
            raise InvalidRequestError("submit_run 需要 RunRequest 和 ExecutionContext")
        if context.limits and context.limits.max_records == 0:
            raise ExecutionLimitRejectedError("max_records=0 不允许启动采集运行")

        existing = self._state_store.find_by_idempotency(
            context.scope_key,
            context.idempotency_key,
        )
        if existing is not None:
            if existing.request_fingerprint != request.fingerprint():
                raise IdempotencyConflictError("同一 scope 和幂等键对应了不同 RunRequest")
            return RunRef(existing.run_id, existing.accepted_at, existing.status)

        adapter = self._registry.collection_adapter(
            request.collection.type_key,
            request.collection.schema_version,
        )
        validation = adapter.validate(request.collection)
        if not isinstance(validation, ValidationResult):
            raise ConfigurationError("CollectionAdapter.validate 必须返回 ValidationResult")
        if not validation.valid:
            details = "; ".join(f"{item.path}: {item.message}" for item in validation.issues)
            raise InvalidCollectionSpecError(details or "采集规格未通过验证")

        run_id = f"run_{uuid.uuid4()}"
        accepted_at = self._clock.now()
        try:
            upstream_request = adapter.build_upstream_request(
                request.collection,
                AdapterContext(
                    scope_key=context.scope_key,
                    run_id=run_id,
                    source_ref=request.source_ref,
                ),
            )
        except Exception as exc:
            raise InvalidCollectionSpecError(f"无法构造上游任务规格: {exc}") from exc

        if context.runtime_policy and context.runtime_policy.gateway_limits and upstream_request.gateway_hint is None:
            raise ConfigurationError(
                "配置 gateway_limits 时，CollectionAdapter 必须提供可确定的 gateway_hint"
            )

        record = RunRecord(
            run_id=run_id,
            scope_key=context.scope_key,
            request=request,
            context=context,
            request_fingerprint=request.fingerprint(),
            adapter_key=adapter.adapter_key,
            upstream_request=upstream_request,
            status=RunStatus.QUEUED,
            accepted_at=accepted_at,
            updated_at=accepted_at,
            next_wakeup_at=accepted_at,
        )
        try:
            self._state_store.create(record)
        except ValueError:
            # 并发提交时以存储中的幂等记录为准。
            existing = self._state_store.find_by_idempotency(
                context.scope_key,
                context.idempotency_key,
            )
            if existing is None:
                raise
            if existing.request_fingerprint != request.fingerprint():
                raise IdempotencyConflictError("同一 scope 和幂等键对应了不同 RunRequest")
            return RunRef(existing.run_id, existing.accepted_at, existing.status)
        self._emit_audit(record, "run.accepted", "accepted")
        self._emit_metric("monitoring.run.accepted", 1, record)
        return RunRef(run_id, accepted_at, RunStatus.QUEUED)

    def cancel_run(self, run_id: str, context: ExecutionContext) -> CancellationResult:
        record = self._get_scoped_record(run_id, context.scope_key)
        if record.terminal:
            return CancellationResult(record.run_id, record.status, True)
        record.cancel_requested = True
        if record.upstream_job is None:
            self._finish(record, RunStatus.CANCELLED)
            self._state_store.save(record)
            return CancellationResult(record.run_id, record.status, True)

        self._cancel_upstream(record)
        self._state_store.save(record)
        return CancellationResult(record.run_id, record.status, True)

    def get_run(self, run_id: str, scope_key: str) -> RunSummary:
        return self._get_scoped_record(run_id, scope_key).summary()

    def query_changes(self, query, scope_key: str):
        return self._history.query_changes(query, scope_key)

    def wake(self, limit: int = 10) -> WorkSummary:
        if limit < 1:
            raise ValueError("wake 的 limit 必须大于 0")
        now = self._clock.now()
        records = self._allocator.allocate(
            AllocationRequest(
                worker_id=self._worker_id,
                now=now,
                batch_size=limit,
                lease_seconds=self._retry_policy.lease_seconds,
                global_concurrency_limit=self._global_concurrency_limit,
            )
        )
        completed = failed = progressed = deferred = 0
        for record in records:
            before = (record.status, record.cursor, record.processed_count, record.failed_count)
            try:
                self._advance(record)
            except RunStateConflictError:
                # 另一个 Worker 已经推进了这个 Run；当前副本不能再写回，
                # 也不能把它误标为 ENGINE_FAILURE。
                continue
            except Exception as exc:
                try:
                    self._fail_run(record, "ENGINE_FAILURE", str(exc))
                except RunStateConflictError:
                    continue
            try:
                record.lease_owner = None
                record.lease_until = None
                self._state_store.save(record)
            except RunStateConflictError:
                continue
            after = (record.status, record.cursor, record.processed_count, record.failed_count)
            if after != before:
                progressed += 1
            if record.status in (RunStatus.COMPLETED, RunStatus.COMPLETED_WITH_ERRORS, RunStatus.CANCELLED):
                completed += 1
            elif record.status is RunStatus.FAILED:
                failed += 1
            else:
                deferred += 1
        return WorkSummary(
            inspected=len(records),
            progressed=progressed,
            completed=completed,
            failed=failed,
            deferred=deferred,
        )

    def recover_interrupted_runs(self) -> RecoverySummary:
        now = self._clock.now()
        recovered = 0
        for record in self._state_store.list_incomplete():
            if record.lease_until and record.lease_until > now:
                continue
            record.lease_owner = None
            record.lease_until = None
            record.next_wakeup_at = now
            if record.status is RunStatus.RUNNING:
                record.status = RunStatus.QUEUED
            record.updated_at = now
            try:
                self._state_store.save(record)
            except RunStateConflictError:
                continue
            recovered += 1
        return RecoverySummary(recovered)

    def _advance(self, record: RunRecord) -> None:
        now = self._clock.now()
        if record.context.limits and record.context.limits.deadline and now >= record.context.limits.deadline:
            self._stop_for_error(record, "DEADLINE_EXCEEDED", "Run 已超过执行截止时间")
            return
        if record.started_at is None:
            record.started_at = now
        if record.status is RunStatus.QUEUED:
            record.status = RunStatus.RUNNING
        record.updated_at = now

        if record.cancel_requested:
            if record.upstream_job is None:
                self._finish(record, RunStatus.CANCELLED)
            else:
                self._cancel_upstream(record)
            return

        if record.upstream_job is None:
            if not self._submit_upstream(record):
                return

        if record.cancel_requested:
            self._cancel_upstream(record)
            return
        self._poll_and_consume(record)

    def _adapter_for(self, record: RunRecord) -> CollectionAdapter:
        adapter = self._registry.collection_adapter(
            record.request.collection.type_key,
            record.request.collection.schema_version,
        )
        if adapter.adapter_key != record.adapter_key:
            raise ConfigurationError("Run 原来使用的采集适配器已被替换")
        return adapter

    def _submit_upstream(self, record: RunRecord) -> bool:
        started = self._clock.now()
        try:
            result = self._gateway.submit(record.upstream_request, record.run_id)
        except UpstreamError as exc:
            self._handle_upstream_error(record, "submit", None, exc, started)
            return False
        except Exception as exc:
            self._handle_upstream_error(
                record,
                "submit",
                None,
                UpstreamError("UPSTREAM_UNAVAILABLE", str(exc), retryable=True),
                started,
            )
            return False
        self._record_attempt(record, "submit", result.gateway_key, started, "success")
        record.upstream_job = result
        record.status = RunStatus.RUNNING
        record.next_wakeup_at = self._clock.now()
        self._state_store.save(record)
        return True

    def _poll_and_consume(self, record: RunRecord) -> None:
        assert record.upstream_job is not None
        ref = record.upstream_job
        status_started = self._clock.now()
        try:
            status = self._gateway.get_status(ref)
        except UpstreamError as exc:
            self._handle_upstream_error(record, "status", ref.gateway_key, exc, status_started)
            return
        except Exception as exc:
            self._handle_upstream_error(
                record,
                "status",
                ref.gateway_key,
                UpstreamError("UPSTREAM_UNAVAILABLE", str(exc), retryable=True),
                status_started,
            )
            return
        self._record_attempt(record, "status", ref.gateway_key, status_started, "success")
        if status.job_ref != ref:
            self._fail_run(record, "UPSTREAM_PROTOCOL_ERROR", "状态记录不属于当前上游任务")
            return

        try:
            drained = self._consume_batches(record)
        except UpstreamError as exc:
            self._handle_upstream_error(record, "read_results", ref.gateway_key, exc, self._clock.now())
            return
        except Exception as exc:
            self._handle_upstream_error(
                record,
                "read_results",
                ref.gateway_key,
                UpstreamError("ENGINE_FAILURE", str(exc), retryable=True),
                self._clock.now(),
            )
            return

        if record.terminal:
            return
        if not drained:
            record.status = RunStatus.RUNNING
            record.next_wakeup_at = self._clock.now()
            return
        if status.state is UpstreamState.COMPLETED:
            if record.failed_count or status.failed_count:
                self._stop_for_error(record, "UPSTREAM_COMPLETED_WITH_ERRORS", "上游任务完成但包含失败记录")
            else:
                self._finish(record, RunStatus.COMPLETED)
        elif status.state is UpstreamState.COMPLETED_WITH_ERRORS:
            self._stop_for_error(record, "UPSTREAM_COMPLETED_WITH_ERRORS", "上游任务完成但包含错误")
        elif status.state is UpstreamState.FAILED:
            message = status.error.message if status.error else "上游任务失败"
            code = status.error.code if status.error else "UPSTREAM_JOB_FAILED"
            self._fail_run(record, code, message)
        elif status.state is UpstreamState.CANCELLED:
            self._finish(record, RunStatus.CANCELLED)
        else:
            record.status = RunStatus.RUNNING
            record.next_wakeup_at = self._clock.now() + timedelta(
                seconds=self._retry_policy.poll_interval_seconds
            )
            record.updated_at = self._clock.now()

    def _consume_batches(self, record: RunRecord) -> bool:
        assert record.upstream_job is not None
        adapter = self._adapter_for(record)
        cursor = record.cursor
        for _ in range(self._retry_policy.max_batches_per_wake):
            started = self._clock.now()
            batch = self._gateway.read_batch(record.upstream_job, cursor)
            read_attempt = self._record_attempt(
                record,
                "read_results",
                record.upstream_job.gateway_key,
                started,
                "success",
            )
            for upstream_record in batch.records:
                if getattr(upstream_record, "job_ref", record.upstream_job) != record.upstream_job:
                    raise UpstreamError("UPSTREAM_PROTOCOL_ERROR", "结果记录不属于当前上游任务")
                if not self._consume_record(record, adapter, upstream_record, read_attempt.attempt_id):
                    return True
            next_cursor = batch.next_cursor if batch.has_more else cursor
            if batch.has_more and next_cursor == cursor:
                raise UpstreamError("UPSTREAM_CURSOR_STALLED", "上游游标没有前进")
            record.cursor = next_cursor
            record.updated_at = self._clock.now()
            self._state_store.save(record)
            if not batch.has_more:
                return True
            cursor = next_cursor
        record.next_wakeup_at = self._clock.now()
        return False

    def _consume_record(
        self,
        record: RunRecord,
        adapter: CollectionAdapter,
        upstream_record: object,
        attempt_ref: str,
    ) -> bool:
        key = (
            record.upstream_job.gateway_key if record.upstream_job else "",
            getattr(upstream_record, "record_id", ""),
        )
        limits = record.context.limits
        if limits and limits.deadline and self._clock.now() >= limits.deadline:
            self._stop_for_error(record, "DEADLINE_EXCEEDED", "Run 已超过执行截止时间")
            self._try_cancel_without_failure(record)
            return False
        if limits and limits.max_records is not None and record.processed_count >= limits.max_records:
            self._stop_for_error(record, "RESULT_LIMIT_REACHED", "已达到宿主提供的结果数量上限")
            self._try_cancel_without_failure(record)
            return False
        context = AdapterContext(
            scope_key=record.scope_key,
            run_id=record.run_id,
            source_ref=record.request.source_ref,
            upstream_job_ref=record.upstream_job.job_id if record.upstream_job else None,
            attempt_ref=attempt_ref,
        )
        try:
            draft = adapter.map_record(upstream_record, context)
            if draft.provenance.attempt_ref is None:
                draft = replace(
                    draft,
                    provenance=replace(draft.provenance, attempt_ref=attempt_ref),
                )
            policy = self._registry.content_policy(
                draft.content_type_key,
                draft.content_schema_version,
            )
            subject = policy.identify(draft)
            observation = observation_from_draft(
                draft,
                scope_key=record.scope_key,
                run_id=record.run_id,
                subject=subject,
                received_at=self._clock.now(),
            )
            history_result = self._history.record(observation)
        except ConfigurationError:
            # 注册缺失或装配冲突是 Run 级问题，不能伪装成单条记录失败。
            raise
        except (MonitoringError, ValueError) as exc:
            # 单条记录的映射/策略错误不会阻塞同一批其它合法结果；记录会被
            # 计入失败并推进游标，避免坏记录造成永久重试风暴。
            record.record_failures.append(
                RecordFailure(
                    upstream_record_id=str(getattr(upstream_record, "record_id", "unknown")),
                    code=getattr(exc, "code", "RECORD_PROCESSING_FAILED"),
                    message=str(exc) or "记录处理失败",
                )
            )
            record.failed_count += 1
            record.processed_ingest_keys.add(key)
            record.updated_at = self._clock.now()
            return True
        except Exception:
            # 存储、线程或其它基础设施异常不能推进 ingest_key/cursor；交给
            # 上层运行重试，避免把暂时性故障误判为坏数据。
            raise

        if key in record.processed_ingest_keys or history_result.duplicate:
            record.processed_ingest_keys.add(key)
            record.updated_at = self._clock.now()
            return True
        record.processed_count += 1
        record.change_count += len(history_result.events)
        record.processed_ingest_keys.add(key)
        record.updated_at = self._clock.now()
        return True

    def _cancel_upstream(self, record: RunRecord) -> None:
        if record.upstream_job is None:
            self._finish(record, RunStatus.CANCELLED)
            return
        started = self._clock.now()
        try:
            result = self._gateway.cancel(record.upstream_job)
        except UpstreamError as exc:
            self._handle_upstream_error(record, "cancel", record.upstream_job.gateway_key, exc, started)
            return
        except Exception as exc:
            self._handle_upstream_error(
                record,
                "cancel",
                record.upstream_job.gateway_key,
                UpstreamError("UPSTREAM_UNAVAILABLE", str(exc), retryable=True),
                started,
            )
            return
        self._record_attempt(record, "cancel", record.upstream_job.gateway_key, started, "success")
        if result.accepted or result.state is UpstreamState.CANCELLED:
            self._finish(record, RunStatus.CANCELLED)
        else:
            record.status = RunStatus.RUNNING
            record.next_wakeup_at = self._clock.now() + timedelta(
                seconds=self._retry_policy.poll_interval_seconds
            )

    def _try_cancel_without_failure(self, record: RunRecord) -> None:
        if record.upstream_job is None:
            return
        try:
            self._gateway.cancel(record.upstream_job)
        except Exception:
            pass

    def _handle_upstream_error(
        self,
        record: RunRecord,
        operation: str,
        gateway_key: str | None,
        error: UpstreamError,
        started,
    ) -> None:
        record.consecutive_failures += 1
        self._record_attempt(record, operation, gateway_key, started, "failed", error.code)
        if not error.retryable or record.consecutive_failures >= self._retry_policy.max_attempts:
            self._fail_run(record, error.code, error.message)
            return
        delay = self._retry_policy.base_delay_seconds * math.pow(2, record.consecutive_failures - 1)
        delay = min(delay, self._retry_policy.max_delay_seconds)
        if error.retry_after_seconds is not None:
            delay = max(delay, error.retry_after_seconds)
        record.status = RunStatus.RUNNING if record.started_at else RunStatus.QUEUED
        record.next_wakeup_at = self._clock.now() + timedelta(seconds=delay)
        record.updated_at = self._clock.now()

    def _record_attempt(
        self,
        record: RunRecord,
        operation: str,
        gateway_key: str | None,
        started,
        outcome: str,
        error_code: str | None = None,
    ) -> Attempt:
        finished = self._clock.now()
        attempt = Attempt(
            attempt_id=f"attempt_{uuid.uuid4()}",
            run_id=record.run_id,
            operation=operation,
            gateway_key=gateway_key,
            started_at=started,
            finished_at=finished,
            outcome=outcome,
            error_code=error_code,
        )
        record.attempts.append(attempt)
        if outcome == "success":
            record.consecutive_failures = 0
        record.updated_at = finished
        return attempt

    def _finish(self, record: RunRecord, status: RunStatus) -> None:
        record.status = status
        record.finished_at = self._clock.now()
        record.updated_at = record.finished_at
        record.next_wakeup_at = None
        record.lease_owner = None
        record.lease_until = None
        self._emit_audit(record, "run.finished", status.value)
        self._emit_metric("monitoring.run.finished", 1, record)

    def _fail_run(self, record: RunRecord, code: str, message: str) -> None:
        record.error = RunError(code, message or code)
        self._finish(record, RunStatus.FAILED)

    def _stop_for_error(self, record: RunRecord, code: str, message: str) -> None:
        record.error = RunError(code, message)
        self._finish(record, RunStatus.COMPLETED_WITH_ERRORS)

    def _get_scoped_record(self, run_id: str, scope_key: str) -> RunRecord:
        record = self._state_store.get(scope_key, run_id)
        if record is None:
            raise RunNotFoundError(f"Run 不存在: {run_id}")
        return record

    def _emit_audit(self, record: RunRecord, action: str, outcome: str) -> None:
        if self._audit_sink is None:
            return
        event = AuditEvent(
            event_id=f"audit_{uuid.uuid4()}",
            scope_key=record.scope_key,
            action=action,
            object_type="run",
            object_id=record.run_id,
            occurred_at=self._clock.now(),
            outcome=outcome,
            actor_ref=record.context.actor_ref,
            trace_ref=record.context.trace_ref,
        )
        try:
            self._audit_sink.record(event)
        except Exception:
            pass

    def _emit_metric(self, name: str, value: float, record: RunRecord) -> None:
        if self._telemetry_sink is None:
            return
        try:
            self._telemetry_sink.emit(
                name,
                value,
                {"scope_key": record.scope_key, "run_id": record.run_id, "status": record.status.value},
            )
        except Exception:
            pass


class _SystemClock:
    def now(self):
        return utc_now()
