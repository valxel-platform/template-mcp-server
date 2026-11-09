"""The data behind the tools. The template keeps it in memory; replace `Store` with your
product's repository (its database, or its service APIs), keeping the same guarantees:
writes are idempotent per (caller, idempotency key), and a reused key with different input is
an error rather than a silent second write. Every record belongs to a tenant (the clinic or
organization), and every read and write is limited to the caller's tenants: a record of another
tenant reads as not found. `tenants=None` means a single-tenant product (no limit)."""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass, field
from typing import Any


class Conflict(Exception):
    pass


class NotFound(Exception):
    pass


@dataclass
class Patient:
    patient_id: str
    name: str
    date_of_birth: str
    mrn: str
    phone: str
    tenant: str = ""


@dataclass
class Task:
    task_id: str
    patient_id: str
    title: str
    priority: str
    status: str = "open"


@dataclass
class Store:
    patients: dict[str, Patient] = field(default_factory=dict)
    tasks: dict[str, Task] = field(default_factory=dict)
    _keys: dict[tuple[str, str], tuple[str, str]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @classmethod
    def seeded(cls) -> Store:
        s = cls()
        s.patients["p-102"] = Patient(
            "p-102", "Ana Lopez", "1984-03-02", "MRN-55821", "555-0142", tenant="north-clinic"
        )
        return s

    def find_patient(self, name: str, date_of_birth: str, tenants: set[str] | None = None) -> Patient | None:
        want = name.strip().lower()
        return next(
            (
                p
                for p in self.patients.values()
                if p.name.lower() == want and p.date_of_birth == date_of_birth and _visible(p, tenants)
            ),
            None,
        )

    def open_tasks(self, patient_id: str, tenants: set[str] | None = None) -> list[Task]:
        self._patient(patient_id, tenants)
        return [t for t in self.tasks.values() if t.patient_id == patient_id and t.status == "open"]

    def _patient(self, patient_id: str, tenants: set[str] | None = None) -> Patient:
        p = self.patients.get(patient_id)
        if p is None or not _visible(p, tenants):  # another tenant's record reads as not found
            raise NotFound(f"No patient {patient_id}.")
        return p

    def _idempotent(self, caller: str, key: str, payload: dict[str, Any], create: Any) -> Task:
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        with self._lock:
            if key and (caller, key) in self._keys:
                task_id, seen = self._keys[(caller, key)]
                if seen != digest:
                    raise Conflict("This idempotency key was used for a different request.")
                return self.tasks[task_id]
            task: Task = create()
            if key:
                self._keys[(caller, key)] = (task.task_id, digest)
            return task

    def create_task(
        self,
        caller: str,
        key: str,
        patient_id: str,
        title: str,
        priority: str,
        tenants: set[str] | None = None,
    ) -> Task:
        self._patient(patient_id, tenants)

        def create() -> Task:
            task = Task(f"t-{len(self.tasks) + 1}", patient_id, title, priority)
            self.tasks[task.task_id] = task
            return task

        return self._idempotent(
            caller,
            key,
            {"op": "create", "patient_id": patient_id, "title": title, "priority": priority},
            create,
        )

    def complete_task(self, caller: str, key: str, task_id: str, tenants: set[str] | None = None) -> Task:
        task = self.tasks.get(task_id)
        if task is None or not _visible(self.patients[task.patient_id], tenants):
            raise NotFound(f"No task {task_id}.")

        def complete() -> Task:
            self.tasks[task_id].status = "done"
            return self.tasks[task_id]

        return self._idempotent(caller, key, {"op": "complete", "task_id": task_id}, complete)


def _visible(p: Patient, tenants: set[str] | None) -> bool:
    return tenants is None or p.tenant in tenants
