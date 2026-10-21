"""Metadata-first discovery; implementation names never come from remote documents."""
import json
from pathlib import Path
from pydantic import Field
from .models import Model, OperationName

ROOT = Path(__file__).resolve().parents[2] / 'skills'
TRUSTED = frozenset({'shipment-analysis', 'chart-management'})

class SkillMetadata(Model):
    name: str
    description: str
    version: str
    supported_report_types: list[str]
    supported_input_types: list[str]
    required_data_fields: list[str]
    required_permissions: list[str]
    supported_operations: list[OperationName]
    dependencies: list[str] = Field(default_factory=list)

class SkillRegistry:
    def __init__(self):
        self.metadata = {s.name: s for s in (SkillMetadata.model_validate(x)
                         for x in json.loads((ROOT / 'registry.json').read_text()))}
        if set(self.metadata) - TRUSTED:
            raise ValueError('Unreviewed skill implementation')
        for s in self.metadata.values():
            if set(s.dependencies) - self.metadata.keys():
                raise ValueError('Missing skill dependency')

    def select(self, operation: OperationName):
        return [s for s in self.metadata.values() if operation in s.supported_operations]

    def load(self, name: str):
        if name not in self.metadata or name not in TRUSTED:
            raise ValueError('Unreviewed skill')
        return (ROOT / name / 'SKILL.md').read_text()
