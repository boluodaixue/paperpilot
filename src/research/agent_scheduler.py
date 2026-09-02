"""Fair in-process execution slots backed by durable Blackboard queue state."""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Generic, TypeVar

from .research_blackboard import ResearchBlackboard


T = TypeVar("T")


class ScheduledAgentCancelled(RuntimeError):
    """A queued Assignment never started because its safety budget closed."""

    def __init__(self, assignment_id: str) -> None:
        super().__init__(f"queued assignment {assignment_id} cancelled due to budget")
        self.assignment_id = assignment_id


@dataclass
class ScheduledAgent(Generic[T]):
    assignment_id: str
    thread_id: str
    parent_assignment_id: str
    runner: Callable[[], Awaitable[T]]
    can_start: Callable[[], bool]


@dataclass
class _Ticket(Generic[T]):
    queue_key: str
    assignment_id: str
    thread_id: str
    future: asyncio.Future[T | None]
    queued_at: float
    runner: Callable[[], Awaitable[T]] | None = None
    can_start: Callable[[], bool] | None = None
    resume: bool = False


class FairAgentScheduler:
    """Bound active Agents and fairly interleave queued work across parents.

    A parent yields its slot while awaiting children and reacquires a normal
    scheduler slot before merge/finalization.  The Blackboard remains the
    recovery source of truth; this object only owns live coroutine slots.
    """

    def __init__(
        self,
        *,
        run_id: str,
        max_concurrent_agents: int,
        max_total_agents: int = 24,
        board: ResearchBlackboard | None = None,
    ) -> None:
        self.run_id = run_id
        self.max_concurrent_agents = max(1, int(max_concurrent_agents))
        self.max_total_agents = max(1, int(max_total_agents))
        self.board = board
        self._lock = asyncio.Lock()
        self._active: set[str] = set()
        self._active_queue_by_thread: dict[str, str] = {}
        self._queues: dict[str, deque[_Ticket[Any]]] = {}
        self._parent_order: deque[str] = deque()
        self._tasks: set[asyncio.Task[Any]] = set()
        self.active_peak = 0
        self.queued_peak = 0
        self.waiting_peak = 0
        self._waiting_parents: set[str] = set()
        self._known_assignments: set[str] = set()
        self._last_started_queue_key = ""

    async def activate_root(self, thread_id: str) -> None:
        async with self._lock:
            self._active.add(thread_id)
            self._known_assignments.add(f"root:{thread_id}")
            self._record_snapshot("root_activated", thread_id)

    def _queued_count(self) -> int:
        return sum(len(items) for items in self._queues.values())

    def _record_snapshot(self, kind: str, actor_thread_id: str) -> None:
        self.active_peak = max(self.active_peak, len(self._active))
        self.queued_peak = max(self.queued_peak, self._queued_count())
        self.waiting_peak = max(self.waiting_peak, len(self._waiting_parents))
        if self.board is None:
            return
        try:
            self.board.record_event(
                self.run_id,
                "scheduler_state",
                actor_thread_id=actor_thread_id,
                payload={
                    "transition": kind,
                    "active": len(self._active),
                    "queued": self._queued_count(),
                    "waiting": len(self._waiting_parents),
                    "active_peak": self.active_peak,
                    "queued_peak": self.queued_peak,
                    "waiting_peak": self.waiting_peak,
                },
            )
        except Exception:
            pass

    def _set_assignment_status(
        self,
        assignment_id: str,
        thread_id: str,
        status: str,
    ) -> None:
        if self.board is None:
            return
        try:
            self.board.update_assignment_node(
                self.run_id,
                assignment_id,
                owner_thread_id=thread_id,
                status=status,
            )
        except Exception:
            pass

    def _enqueue(self, ticket: _Ticket[Any]) -> None:
        queue = self._queues.get(ticket.queue_key)
        if queue is None:
            queue = deque()
            self._queues[ticket.queue_key] = queue
            self._parent_order.append(ticket.queue_key)
        queue.append(ticket)

    def _next_ticket(self) -> _Ticket[Any] | None:
        while self._parent_order:
            active_counts: dict[str, int] = {}
            for key in self._active_queue_by_thread.values():
                active_counts[key] = active_counts.get(key, 0) + 1
            minimum = min(active_counts.get(key, 0) for key in self._parent_order)
            eligible = [
                (index, key)
                for index, key in enumerate(self._parent_order)
                if active_counts.get(key, 0) == minimum
            ]
            preferred = next(
                (
                    index
                    for index, key in eligible
                    if key != self._last_started_queue_key
                ),
                eligible[0][0],
            )
            self._parent_order.rotate(-preferred)
            key = self._parent_order.popleft()
            queue = self._queues.get(key)
            if not queue:
                self._queues.pop(key, None)
                continue
            ticket = queue.popleft()
            if queue:
                self._parent_order.append(key)
            else:
                self._queues.pop(key, None)
            return ticket
        return None

    def _pump_locked(self) -> None:
        while len(self._active) < self.max_concurrent_agents:
            ticket = self._next_ticket()
            if ticket is None:
                return
            if not ticket.resume and ticket.can_start is not None and not ticket.can_start():
                self._set_assignment_status(
                    ticket.assignment_id,
                    ticket.thread_id,
                    "cancelled_due_to_budget",
                )
                if not ticket.future.done():
                    ticket.future.set_exception(
                        ScheduledAgentCancelled(ticket.assignment_id)
                    )
                self._record_snapshot("queued_cancelled", ticket.thread_id)
                continue
            self._active.add(ticket.thread_id)
            self._active_queue_by_thread[ticket.thread_id] = ticket.queue_key
            self._last_started_queue_key = ticket.queue_key
            if ticket.resume:
                self._waiting_parents.discard(ticket.thread_id)
                self._set_assignment_status(
                    ticket.assignment_id,
                    ticket.thread_id,
                    "researching",
                )
                if not ticket.future.done():
                    ticket.future.set_result(None)
                self._record_snapshot("parent_resumed", ticket.thread_id)
                continue
            self._set_assignment_status(
                ticket.assignment_id,
                ticket.thread_id,
                "researching",
            )
            if self.board is not None:
                try:
                    self.board.record_event(
                        self.run_id,
                        "queued_assignment_started",
                        actor_thread_id=ticket.thread_id,
                        payload={
                            "assignment_id": ticket.assignment_id,
                            "parent_assignment_id": ticket.queue_key,
                            "queue_wait_seconds": max(0.0, time.time() - ticket.queued_at),
                        },
                    )
                except Exception:
                    pass
            task = asyncio.create_task(self._execute(ticket))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            self._record_snapshot("queued_started", ticket.thread_id)

    async def _execute(self, ticket: _Ticket[T]) -> None:
        try:
            if ticket.runner is None:
                raise RuntimeError("scheduled child has no runner")
            result = await ticket.runner()
            if not ticket.future.done():
                ticket.future.set_result(result)
        except asyncio.CancelledError:
            if not ticket.future.done():
                ticket.future.cancel()
            raise
        except Exception as exc:
            if not ticket.future.done():
                ticket.future.set_exception(exc)
        finally:
            async with self._lock:
                self._active.discard(ticket.thread_id)
                self._active_queue_by_thread.pop(ticket.thread_id, None)
                self._record_snapshot("agent_released", ticket.thread_id)
                self._pump_locked()

    async def run_children(
        self,
        *,
        parent_thread_id: str,
        parent_assignment_id: str,
        children: list[ScheduledAgent[T]],
    ) -> list[T | BaseException]:
        """Queue a sibling batch, suspend the parent, then resume it for merge."""

        if not children:
            return []
        loop = asyncio.get_running_loop()
        tickets: list[_Ticket[T]] = []
        async with self._lock:
            self._active.discard(parent_thread_id)
            self._active_queue_by_thread.pop(parent_thread_id, None)
            self._waiting_parents.add(parent_thread_id)
            self._set_assignment_status(
                parent_assignment_id,
                parent_thread_id,
                "waiting_children",
            )
            for child in children:
                self._known_assignments.add(child.assignment_id)
                self._set_assignment_status(
                    child.assignment_id,
                    child.thread_id,
                    "queued",
                )
                ticket = _Ticket[T](
                    queue_key=child.parent_assignment_id,
                    assignment_id=child.assignment_id,
                    thread_id=child.thread_id,
                    future=loop.create_future(),
                    queued_at=time.time(),
                    runner=child.runner,
                    can_start=child.can_start,
                )
                tickets.append(ticket)
                self._enqueue(ticket)
            self._record_snapshot("parent_waiting", parent_thread_id)
            self._pump_locked()

        results = await asyncio.gather(
            *(ticket.future for ticket in tickets),
            return_exceptions=True,
        )

        resume = _Ticket[None](
            queue_key=f"resume:{parent_assignment_id}",
            assignment_id=parent_assignment_id,
            thread_id=parent_thread_id,
            future=loop.create_future(),
            queued_at=time.time(),
            resume=True,
        )
        async with self._lock:
            self._enqueue(resume)
            self._record_snapshot("parent_resume_queued", parent_thread_id)
            self._pump_locked()
        await resume.future
        return list(results)

    def metrics(self) -> dict[str, int]:
        return {
            "active_peak": self.active_peak,
            "queued_peak": self.queued_peak,
            "waiting_peak": self.waiting_peak,
            "total_agents": len(self._known_assignments),
        }

    def remaining_total_capacity(self) -> int:
        return max(0, self.max_total_agents - len(self._known_assignments))


__all__ = [
    "FairAgentScheduler",
    "ScheduledAgent",
    "ScheduledAgentCancelled",
]
