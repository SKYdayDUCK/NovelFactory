"""Shared Pydantic types generated from the frozen source schema on import."""
from .registry import model_for

WorldRef = model_for("WorldRef")
StoryPoint = model_for("StoryPoint")
StoryInterval = model_for("StoryInterval")
ArtifactRef = model_for("ArtifactRef")
EventRef = model_for("EventRef")
EvidenceRef = model_for("EvidenceRef")
OccurrenceTime = model_for("OccurrenceTime")
AcceptedEvent = model_for("AcceptedEvent")
AcceptedEventR1 = model_for("AcceptedEventR1", "2.1")
KnowledgeCut = model_for("KnowledgeCut", "2.1")
ReadContract = model_for("ReadContract", "2.1")
UnifiedQueryRequest = model_for("UnifiedQueryRequest", "2.1")
QueryBatchRequest = model_for("QueryBatchRequest", "2.1")
ContextBundle = model_for("ContextBundle")
TaskSpec = model_for("TaskSpec")
TaskResult = model_for("TaskResult")
