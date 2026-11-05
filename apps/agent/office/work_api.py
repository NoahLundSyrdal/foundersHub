"""Local operator workflow API. External sends and real execution are unavailable."""
import asyncio
import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict

from office import bus
from office.work_bridge import WorkBridge
from office.work_models import EvidenceCreate, WorkAction, WorkCreate, WorkDecision
from office.work_routines import RoutineCreate, WorkScheduler
from office.work_service import WorkConflict, WorkNotFound, WorkService, canonical
from office.work_templates import templates


logger = logging.getLogger(__name__)


class RoutineEnabled(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    enabled: bool


class WorkRuntime:
    def __init__(self, conn, lock, clock=None):
        self.conn, self.lock = conn, lock
        self.service = WorkService(conn, clock=clock)
        self.bridge = WorkBridge(conn, self.service)
        self.scheduler = WorkScheduler(conn, self.service, clock=clock)

    def notify(self):
        bus.publish("work.updated", {}, source=self.conn)
        bus.publish("state.changed", {}, source=self.conn)

    def tick(self):
        with self.service._atomic():
            changed = self.bridge.sync()
            changed = self.scheduler.tick() or changed
        if changed:
            self.notify()
        return changed

    def mutate(self, event_key, callback):
        # Include adapter consequences in the same command and replay snapshot.
        with self.service._atomic():
            replay = self.conn.execute("SELECT 1 FROM office_work_commands WHERE event_key=?", (event_key,)).fetchone()
            result = callback()
            if not replay:
                self.bridge.sync()
                result = self.service.get(result["id"])
                self.conn.execute("UPDATE office_work_commands SET response=? WHERE event_key=?", (canonical(result), event_key))
        self.notify()
        return result

    async def watch(self):
        while True:
            await asyncio.sleep(10)
            try:
                async with self.lock:
                    self.tick()
            except Exception:
                # Do not expose exception messages, which may contain private data.
                logger.warning("Work monitor tick failed; will retry local coordination on the next tick")


def router(runtime):
    routes = APIRouter(prefix="/api/work")

    def invoke(callback):
        try:
            return callback()
        except WorkNotFound as error:
            raise HTTPException(404, str(error)) from error
        except (WorkConflict, ValueError) as error:
            raise HTTPException(409, str(error)) from error

    @routes.get("")
    async def list_work():
        async with runtime.lock:
            runtime.tick()
            return {"items": runtime.service.list()}

    @routes.get("/templates")
    async def list_templates():
        return {"templates": templates()}

    @routes.get("/inventory")
    async def inventory():
        async with runtime.lock:
            runtime.tick()
            return {"items": runtime.bridge.inventory(), "basis": "recorded_receipts", "includes_consumption": False}

    @routes.get("/routines")
    async def list_routines():
        async with runtime.lock:
            return {"routines": runtime.scheduler.list()}

    @routes.post("/routines")
    async def create_routine(body: RoutineCreate):
        async with runtime.lock:
            result = invoke(lambda: runtime.scheduler.create(body))
            runtime.notify()
            return result

    @routes.post("/routines/{routine_id}/enabled")
    async def enable_routine(routine_id: str, body: RoutineEnabled):
        async with runtime.lock:
            result = invoke(lambda: runtime.scheduler.set_enabled(routine_id, body.enabled))
            runtime.notify()
            return result

    @routes.get("/notifications")
    async def notifications():
        async with runtime.lock:
            runtime.tick()
            return {"notifications": runtime.scheduler.notifications()}

    @routes.post("/notifications/{notification_id}/acknowledge")
    async def acknowledge(notification_id: str):
        async with runtime.lock:
            result = invoke(lambda: runtime.scheduler.acknowledge(notification_id))
            runtime.notify()
            return result

    @routes.post("")
    async def create_work(body: WorkCreate):
        async with runtime.lock:
            return invoke(lambda: runtime.mutate(body.event_key, lambda: runtime.service.create(body)))

    @routes.get("/{work_id}")
    async def get_work(work_id: str):
        async with runtime.lock:
            runtime.tick()
            return invoke(lambda: runtime.service.get(work_id))

    def guarded(work_id, operation, body, callback):
        runtime.bridge.guard(work_id, operation, body)
        return runtime.mutate(body.event_key, callback)

    @routes.post("/{work_id}/actions")
    async def action(work_id: str, body: WorkAction):
        async with runtime.lock:
            return invoke(lambda: guarded(work_id, "action", body, lambda: runtime.service.act(work_id, body)))

    @routes.post("/{work_id}/evidence")
    async def evidence(work_id: str, body: EvidenceCreate):
        async with runtime.lock:
            return invoke(lambda: guarded(work_id, "evidence", body, lambda: runtime.service.add_evidence(work_id, body)))

    @routes.post("/{work_id}/decisions")
    async def decision(work_id: str, body: WorkDecision):
        async with runtime.lock:
            return invoke(lambda: guarded(work_id, "decision", body, lambda: runtime.service.decide(work_id, body)))

    return routes
